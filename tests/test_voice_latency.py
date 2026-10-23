import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.providers.voice_agent import (
    Assistant, OllamaOptions, PiperChunkedStream, PiperTTS, _speech_phrases,
)


class VoiceLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_speech_starts_before_text_stream_finishes(self):
        release = asyncio.Event()

        async def tokens():
            yield 'Here are the results for your project '
            await release.wait()
            yield 'Sir.'

        phrases = _speech_phrases(tokens())
        self.assertEqual(await asyncio.wait_for(anext(phrases), 1),
                         'Here are the results for your project')
        release.set()
        self.assertEqual([p async for p in phrases], ['Sir.'])

    async def test_short_reply_and_split_words_preserve_text(self):
        async def tokens():
            for token in ['Tests pa', 'ssed, ', 'Sir.']:
                yield token
        self.assertEqual(' '.join([p async for p in _speech_phrases(tokens())]),
                         'Tests passed, Sir.')

    async def test_ollama_yields_before_response_completes_and_closes(self):
        release = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"message":{"content":"Hello "},"done":false}\n'
                await release.wait()
                yield b'{"message":{"content":"Sir."},"done":true}\n'

            async def aclose(self):
                self.closed = True

        stream = Stream()
        def handler(request):
            self.assertTrue(json.loads(request.content)['stream'])
            return httpx.Response(200, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assistant = Assistant(OllamaOptions())
        with patch('app.providers.voice_agent.httpx.AsyncClient', return_value=client):
            reply = assistant._ask_ollama('hello')
            self.assertEqual(await asyncio.wait_for(anext(reply), 1), 'Hello ')
            await reply.aclose()
        self.assertTrue(stream.closed)

    async def test_stream_failure_before_and_after_first_token(self):
        assistant = Assistant(OllamaOptions(), log_event=AsyncMock())
        assistant._ollama_ready.set()

        async def broken(text):
            raise httpx.ReadError('disconnected')
            yield

        assistant._ask_ollama = broken
        reply = [p async for p in assistant._generate_voice_reply('hello')]
        self.assertIn('Please try again', ''.join(reply))

        async def partial(text):
            yield 'The result is '
            raise httpx.ReadError('disconnected')

        assistant._ask_ollama = partial
        self.assertEqual([p async for p in assistant._generate_voice_reply('hello')],
                         ['The result is '])
        self.assertFalse(assistant._llm_lock.locked())

    async def test_cancelled_generation_releases_lock(self):
        assistant = Assistant(OllamaOptions())
        assistant._ollama_ready.set()
        async def tokens(text):
            yield 'Hello '
            await asyncio.Event().wait()
        assistant._ask_ollama = tokens
        reply = assistant._generate_voice_reply('hello')
        self.assertEqual(await anext(reply), 'Hello ')
        await reply.aclose()
        self.assertFalse(assistant._llm_lock.locked())

    async def test_interruption_cancels_network_and_releases_lock(self):
        assistant = Assistant(OllamaOptions())
        assistant._ollama_ready.set()
        reading = asyncio.Event()
        closed = asyncio.Event()
        async def tokens(text):
            try:
                reading.set()
                await asyncio.Event().wait()
                yield 'unused'
            finally:
                closed.set()
        assistant._ask_ollama = tokens
        reply = assistant._generate_voice_reply('hello')
        task = asyncio.create_task(anext(reply))
        await reading.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertFalse(assistant._llm_lock.locked())

    async def test_piper_emits_audio_before_synthesis_finishes(self):
        emitted = asyncio.Event()
        voice = PiperTTS.__new__(PiperTTS)
        voice._opts = SimpleNamespace(model_path='fake')
        voice._sample_rate = 22050
        voice._num_channels = 1
        voice._syn_config = None
        voice._log_event = None
        def synthesize(*args):
            yield SimpleNamespace(audio_int16_bytes=b'first')
            # The old list(...) buffering would reach here before emitting audio.
            self.assertTrue(emitted.is_set())
            yield SimpleNamespace(audio_int16_bytes=b'second')
        voice._voice = SimpleNamespace(synthesize=synthesize)
        audio = []
        def push(chunk):
            audio.append(chunk)
            emitted.set()
        emitter = SimpleNamespace(initialize=lambda **kw: None, push=push, flush=lambda: None)
        stream = SimpleNamespace(_tts=voice, input_text='Hello Sir.')
        await PiperChunkedStream._run(stream, emitter)
        self.assertEqual(audio, [b'first', b'second'])
