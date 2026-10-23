import asyncio
import io
import json
import os
import unittest
import wave
from unittest.mock import patch

import httpx

from app.providers.voicebox_tts import VoiceboxTTS


def wav_bytes(rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * 240)
    return buffer.getvalue()


class VoiceboxTests(unittest.IsolatedAsyncioTestCase):
    def voice(self):
        with patch.dict(os.environ, {"VOICEBOX_VOICE": "af_bella"}):
            return VoiceboxTTS()

    async def test_reuses_matching_preset_and_returns_pcm_without_playback(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/profiles":
                return httpx.Response(200, json=[{
                    "id": "bella", "voice_type": "preset", "preset_engine": "kokoro",
                    "preset_voice_id": "af_bella",
                }])
            self.assertEqual(request.url.path, "/generate/stream")
            data = json.loads(request.content)
            self.assertEqual(data["profile_id"], "bella")
            self.assertFalse(data["personality"])
            self.assertEqual(data["effects_chain"], [])
            return httpx.Response(200, content=wav_bytes())

        client = httpx.AsyncClient(base_url="http://voicebox", transport=httpx.MockTransport(handler))
        with patch("app.providers.voicebox_tts.httpx.AsyncClient", return_value=client):
            self.assertEqual(await self.voice().generate_audio("Hello"), b"\x01\x00" * 240)
        self.assertEqual(paths, ["/profiles", "/generate/stream"])

    async def test_creates_only_one_profile_for_concurrent_requests(self):
        created = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "new-bella"})

        voice = self.voice()
        async with httpx.AsyncClient(base_url="http://voicebox", transport=httpx.MockTransport(handler)) as client:
            ids = await asyncio.gather(voice._profile(client), voice._profile(client))
        self.assertEqual(ids, ["new-bella", "new-bella"])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["preset_voice_id"], "af_bella")

    async def test_rejects_wrong_audio_format(self):
        voice = self.voice()
        voice._profile_id = "bella"
        client = httpx.AsyncClient(base_url="http://voicebox", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=wav_bytes(22050))))
        with patch("app.providers.voicebox_tts.httpx.AsyncClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "24 kHz"):
                await voice.generate_audio("Hello")

    async def test_backend_failure_is_reported(self):
        voice = self.voice()
        voice._profile_id = "bella"
        client = httpx.AsyncClient(base_url="http://voicebox", transport=httpx.MockTransport(
            lambda request: httpx.Response(503)))
        with patch("app.providers.voicebox_tts.httpx.AsyncClient", return_value=client):
            with self.assertRaises(httpx.HTTPStatusError):
                await voice.generate_audio("Hello")

    def test_rejects_unlisted_voice(self):
        with patch.dict(os.environ, {"VOICEBOX_VOICE": "unlisted"}):
            with self.assertRaises(ValueError):
                VoiceboxTTS()
