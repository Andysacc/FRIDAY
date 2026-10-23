import asyncio
import io
import json
import os
import re
from collections.abc import AsyncIterable
from contextlib import aclosing
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, LanguageCode, WorkerOptions, cli, inference, stt, tts
from livekit.agents.types import (
    APIConnectOptions,
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, shortuuid
from piper import PiperVoice, SynthesisConfig
from app.providers.voicebox_tts import VoiceboxTTS
from app.providers.kokoro_tts import KokoroTTS

load_dotenv(".env")


def _env_value(name: str, legacy_name: str, default: str) -> str:
    return os.getenv(name) or os.getenv(legacy_name) or default


def _optional_env_int(name: str, legacy_name: str) -> int | None:
    value = os.getenv(name) or os.getenv(legacy_name)
    return int(value) if value else None


@dataclass
class FasterWhisperOptions:
    model: str = os.getenv("WHISPER_MODEL", "tiny.en")
    device: str = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    language: str = os.getenv("WHISPER_LANGUAGE", "en")
    beam_size: int = int(os.getenv("WHISPER_BEAM_SIZE", "1"))
    load_timeout: float = float(os.getenv("WHISPER_LOAD_TIMEOUT", "120"))
    recognize_timeout: float = float(os.getenv("WHISPER_RECOGNIZE_TIMEOUT", "30"))


@dataclass
class OllamaOptions:
    model: str = os.getenv("OLLAMA_VOICE_MODEL", os.getenv("DEFAULT_MODEL", "qwen2.5:0.5b"))
    base_url: str = os.getenv("OLLAMA_OPENAI_BASE_URL", "http://ollama:11434/v1")
    api_key: str = os.getenv("OLLAMA_API_KEY", "ollama")
    timeout: float = float(os.getenv("OLLAMA_VOICE_TIMEOUT", "25"))
    prewarm_timeout: float = float(os.getenv("OLLAMA_PREWARM_TIMEOUT", "60"))
    num_predict: int = int(os.getenv("OLLAMA_VOICE_NUM_PREDICT", "32"))
    temperature: float = float(os.getenv("OLLAMA_VOICE_TEMPERATURE", "0.2"))
    num_ctx: int = int(_env_value("OLLAMA_NUM_CTX", "OLLAMA_VOICE_NUM_CTX", "2048"))
    num_thread: int | None = _optional_env_int("OLLAMA_NUM_THREAD", "OLLAMA_VOICE_NUM_THREAD")
    keep_alive: str = _env_value("OLLAMA_KEEP_ALIVE", "OLLAMA_VOICE_KEEP_ALIVE", "5m")

    @property
    def chat_url(self) -> str:
        root = self.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        return f"{root}/api/chat"

    @property
    def generate_url(self) -> str:
        root = self.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        return f"{root}/api/generate"


@dataclass
class PiperOptions:
    model_path: str = os.getenv("PIPER_MODEL", "models/piper/en_US-ryan-high.onnx")
    speaker_id: int | None = None
    length_scale: float | None = None
    noise_scale: float | None = None
    noise_w_scale: float | None = None
    volume: float = float(os.getenv("PIPER_VOLUME", "1.0"))


@dataclass
class VoiceDetectionOptions:
    activation_threshold: float = float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.65"))
    deactivation_threshold: float = float(os.getenv("VAD_DEACTIVATION_THRESHOLD", "0.45"))
    min_speech_duration: float = float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.25"))
    min_silence_duration: float = float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.35"))
    prefix_padding_duration: float = float(os.getenv("VAD_PREFIX_PADDING_DURATION", "0.25"))
    min_endpointing_delay: float = float(os.getenv("VOICE_MIN_ENDPOINTING_DELAY", "0.3"))
    max_endpointing_delay: float = float(os.getenv("VOICE_MAX_ENDPOINTING_DELAY", "1.5"))


class FasterWhisperSTT(stt.STT):
    def __init__(self, opts: FasterWhisperOptions | None = None, log_event=None):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._opts = opts or FasterWhisperOptions()
        self._model: WhisperModel | None = None
        self._model_lock = asyncio.Lock()
        self._log_event = log_event

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "faster-whisper"

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            self._model = WhisperModel(
                self._opts.model,
                device=self._opts.device,
                compute_type=self._opts.compute_type,
            )
        return self._model

    async def _log(self, stage: str, message: str, **extra) -> None:
        if self._log_event is not None:
            await self._log_event(stage, message, **extra)

    async def prewarm(self) -> None:
        await self._ensure_model_loaded()

    async def _ensure_model_loaded(self) -> None:
        if self._model is not None:
            return

        async with self._model_lock:
            if self._model is not None:
                return

            started_at = time.monotonic()
            await self._log(
                "stt",
                "loading faster-whisper model",
                model=self._opts.model,
                device=self._opts.device,
                compute_type=self._opts.compute_type,
                timeout=self._opts.load_timeout,
            )
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._get_model),
                    timeout=self._opts.load_timeout,
                )
            except TimeoutError:
                await self._log(
                    "error",
                    "faster-whisper model load timed out",
                    level="error",
                    model=self._opts.model,
                    timeout=self._opts.load_timeout,
                )
                raise
            except Exception as exc:
                await self._log(
                    "error",
                    "faster-whisper model load failed",
                    level="error",
                    error=str(exc),
                    model=self._opts.model,
                )
                raise

            await self._log(
                "stt",
                "faster-whisper model loaded",
                model=self._opts.model,
                elapsed=round(time.monotonic() - started_at, 3),
            )

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        audio = rtc.combine_audio_frames(buffer)
        audio_file = io.BytesIO(audio.to_wav_bytes())
        language_code = self._opts.language if language is NOT_GIVEN else str(language)
        await self._ensure_model_loaded()
        await self._log(
            "stt",
            "recognize start",
            language=language_code,
            duration=round(getattr(audio, "duration", 0.0), 3),
            sample_rate=getattr(audio, "sample_rate", None),
            timeout=self._opts.recognize_timeout,
        )

        started_at = time.monotonic()
        try:
            text, detected_language, confidence = await asyncio.wait_for(
                asyncio.to_thread(
                    self._transcribe,
                    audio_file,
                    language_code,
                ),
                timeout=self._opts.recognize_timeout,
            )
        except TimeoutError:
            await self._log(
                "error",
                "faster-whisper recognition timed out",
                level="error",
                timeout=self._opts.recognize_timeout,
                language=language_code,
            )
            raise
        except Exception as exc:
            await self._log(
                "error",
                "faster-whisper recognition failed",
                level="error",
                error=str(exc),
                language=language_code,
            )
            raise

        await self._log(
            "stt",
            "recognize complete",
            transcript=text,
            language=detected_language or language_code,
            confidence=round(confidence or 0, 4),
            elapsed=round(time.monotonic() - started_at, 3),
        )

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(
                    language=LanguageCode(detected_language or language_code),
                    text=text,
                    confidence=confidence,
                )
            ],
        )

    def _transcribe(self, audio_file: io.BytesIO, language: str) -> tuple[str, str, float]:
        segments, info = self._get_model().transcribe(
            audio_file,
            language=language,
            beam_size=self._opts.beam_size,
            # LiveKit already trims speech with Silero VAD.
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, info.language, info.language_probability


class PiperTTS(tts.TTS):
    def __init__(self, opts: PiperOptions | None = None, log_event=None):
        self._opts = opts or PiperOptions()
        self._log_event = log_event
        self._voice = PiperVoice.load(Path(self._opts.model_path))
        self._syn_config = SynthesisConfig(
            speaker_id=self._opts.speaker_id,
            length_scale=self._opts.length_scale,
            noise_scale=self._opts.noise_scale,
            noise_w_scale=self._opts.noise_w_scale,
            volume=self._opts.volume,
        )

        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=self._voice.config.sample_rate,
            num_channels=1,
        )

    @property
    def model(self) -> str:
        return self._opts.model_path

    @property
    def provider(self) -> str:
        return "piper"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return PiperChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )

    async def log(self, stage: str, message: str, **extra) -> None:
        if self._log_event is not None:
            await self._log_event(stage, message, **extra)


class PiperChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        piper_tts = self._tts
        if not isinstance(piper_tts, PiperTTS):
            raise TypeError("PiperChunkedStream requires PiperTTS")

        output_emitter.initialize(
            request_id=shortuuid(),
            sample_rate=piper_tts.sample_rate,
            num_channels=piper_tts.num_channels,
            mime_type="audio/pcm",
        )

        await piper_tts.log(
            "tts",
            "synthesize start",
            chars=len(self.input_text),
            model=piper_tts.model,
        )
        started_at = time.monotonic()
        chunks = 0
        iterator = piper_tts._voice.synthesize(self.input_text, piper_tts._syn_config)
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            output_emitter.push(chunk.audio_int16_bytes)
            chunks += 1
            if chunks == 1:
                await piper_tts.log(
                    "tts", "first audio ready",
                    elapsed=round(time.monotonic() - started_at, 3),
                )

        output_emitter.flush()
        await piper_tts.log(
            "tts",
            "synthesize complete",
            chunks=chunks,
            sample_rate=piper_tts.sample_rate,
        )


class Assistant(Agent):
    def __init__(self, ollama: OllamaOptions, log_event=None):
        self._ollama = ollama
        self._log_event = log_event
        self._friday_api_base_url = os.getenv("FRIDAY_API_BASE_URL", "http://friday:8080").rstrip("/")
        self._llm_lock = asyncio.Lock()
        self._ollama_ready = asyncio.Event()
        super().__init__(
            instructions="""You are an assistant for a tech developer who needs to keep up with multiple tasks and projects.
Be critical as if you helped with the thinking and planning of the project."""
        )

    async def _log(self, stage: str, message: str, level: str = "info", **extra) -> None:
        if self._log_event is not None:
            await self._log_event(stage, message, level=level, **extra)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        user_text = _message_text(new_message)
        if not user_text:
            await self._log("llm", "skipping empty user transcript")
            return

        if _is_project_testing_intent(user_text):
            reply = await self._run_project_tests()
        else:
            reply = self._generate_voice_reply(user_text)

        try:
            self.session.say(reply, allow_interruptions=True)
        except RuntimeError as exc:
            if "closing" not in str(exc).lower():
                raise
            await self._log("tts", "skipping assistant speech because session is closing")

    async def tts_node(self, text: AsyncIterable[str], model_settings):
        async for phrase in _speech_phrases(text):
            async with self.session.tts.synthesize(phrase) as stream:
                async for event in stream:
                    yield event.frame

    async def prewarm(self) -> None:
        if self._ollama.keep_alive.strip().lower() in {"0", "0s", "0m", "0h"}:
            await self._log(
                "llm",
                "ollama prewarm disabled; model will load on demand",
            )
            self._ollama_ready.set()
            return

        await self._log(
            "llm",
            "ollama prewarm start",
            model=self._ollama.model,
            timeout=self._ollama.prewarm_timeout,
        )
        started_at = time.monotonic()
        try:
            await self._load_ollama()
            await self._log(
                "llm",
                "ollama prewarm complete",
                elapsed=round(time.monotonic() - started_at, 3),
            )
        except Exception as exc:
            await self._log(
                "llm",
                "ollama prewarm failed; continuing on demand",
                level="warning",
                error=str(exc),
                elapsed=round(time.monotonic() - started_at, 3),
            )
        finally:
            self._ollama_ready.set()

    async def _generate_voice_reply(self, user_text: str):
        async with self._llm_lock:
            await self._ollama_ready.wait()
            started_at = time.monotonic()
            chars = 0
            await self._log("llm", "ollama request start", model=self._ollama.model)
            try:
                deadline = started_at + self._ollama.timeout
                async with aclosing(self._ask_ollama(user_text)) as tokens:
                    while True:
                        try:
                            token = await asyncio.wait_for(
                                anext(tokens), timeout=max(0, deadline - time.monotonic()),
                            )
                        except StopAsyncIteration:
                            break
                        if not chars:
                            await self._log(
                                "llm", "first text ready",
                                elapsed=round(time.monotonic() - started_at, 3),
                            )
                        chars += len(token)
                        yield token
            except (TimeoutError, httpx.HTTPError, ValueError) as exc:
                await self._log("error", "ollama stream failed", level="error", error=str(exc))
                # Do not append a contradictory fallback after a partial answer.
                if not chars:
                    yield "My local language model could not answer, Sir. Please try again."
            else:
                if not chars:
                    yield "My local language model returned an empty answer, Sir."
            finally:
                await self._log(
                    "llm", "ollama stream finished", chars=chars,
                    elapsed=round(time.monotonic() - started_at, 3),
                )

    async def _ask_ollama(self, user_text: str):
        options: dict[str, float | int] = {
            "num_predict": self._ollama.num_predict,
            "temperature": self._ollama.temperature,
            "num_ctx": self._ollama.num_ctx,
        }
        if self._ollama.num_thread is not None:
            options["num_thread"] = self._ollama.num_thread

        payload = {
            "model": self._ollama.model,
            "stream": True,
            "keep_alive": self._ollama.keep_alive,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are FRIDAY, a concise local voice assistant. "
                        "When addressing or referring to the user, call him Sir. "
                        "Reply in one short sentence using at most 18 words."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            "options": options,
        }
        timeout = httpx.Timeout(
            connect=5.0,
            read=self._ollama.timeout,
            write=10.0,
            pool=5.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self._ollama.chat_url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise ValueError(str(data["error"]))
                    token = (data.get("message") or {}).get("content") or ""
                    if token:
                        yield str(token)
                    if data.get("done"):
                        break

    async def _run_project_tests(self) -> str:
        await self._log("orchestrator", "voice test request started")
        timeout = httpx.Timeout(connect=5.0, read=150.0, write=10.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                active_response = await client.get(f"{self._friday_api_base_url}/api/projects/active")
                active_response.raise_for_status()
                project = active_response.json().get("project")
                if not project:
                    await self._log("orchestrator", "voice test request has no active project", level="warning")
                    return "No active project is registered yet, Sir. Open Projects in the dashboard first."

                project_id = project["project_id"]
                response = await client.post(
                    f"{self._friday_api_base_url}/api/projects/{project_id}/tests",
                    json={"source": "voice", "framework": "unittest", "targets": ["tests"]},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            await self._log(
                "orchestrator",
                "voice test request failed",
                level="error",
                error=str(exc),
                api=self._friday_api_base_url,
            )
            return "The project test pipeline could not be reached, Sir."

        result = payload.get("result") or {}
        passed = result.get("status") == "succeeded"
        task_id = result.get("task_id")
        await self._log(
            "orchestrator",
            "voice test request completed",
            level="info" if passed else "warning",
            project_id=project_id,
            task_id=task_id,
            status=result.get("status"),
        )
        project_name = project.get("name", "the active project")
        if passed:
            return f"Tests passed for {project_name}, Sir, and the project is ready for review."
        return f"Tests failed for {project_name}, Sir. I kept it in execution and logged the result."

    async def _load_ollama(self) -> None:
        payload = {
            "model": self._ollama.model,
            "keep_alive": self._ollama.keep_alive,
        }
        timeout = httpx.Timeout(
            connect=5.0,
            read=self._ollama.prewarm_timeout,
            write=10.0,
            pool=5.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await asyncio.wait_for(
                client.post(self._ollama.generate_url, json=payload),
                timeout=self._ollama.prewarm_timeout,
            )
            response.raise_for_status()


async def _speech_phrases(text: AsyncIterable[str]):
    """Bound synthesis latency while avoiding tiny, unnatural token-sized speech."""
    pending = ""
    async for token in text:
        pending += token
        while True:
            boundary = re.search(r"[.!?;:,](?:\s|$)|(?:\S+\s+){7}", pending)
            if boundary is None:
                break
            phrase, pending = pending[:boundary.end()], pending[boundary.end():]
            if phrase.strip():
                yield phrase.strip()
    if pending.strip():
        yield pending.strip()


def _message_text(message) -> str:
    text_content = getattr(message, "text_content", None)
    if text_content:
        return str(text_content).strip()

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(str(part) for part in content).strip()
    return str(content).strip()


def _is_project_testing_intent(text: str) -> bool:
    normalized = " ".join(text.lower().strip().rstrip(".!?").split())
    direct_phrases = {
        "run tests",
        "run the tests",
        "run project tests",
        "run the project tests",
        "start tests",
        "start the tests",
        "trigger tests",
        "test the active project",
        "test the current project",
    }
    return normalized in direct_phrases or normalized.startswith("please run the tests")


async def entrypoint(ctx: agents.JobContext):
    async def emit_call_log(stage: str, message: str, level: str = "info", **extra) -> None:
        payload = {
            "stage": stage,
            "message": message,
            "level": level,
            "ts": time.time(),
            **extra,
        }
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps(payload, default=str),
                reliable=True,
                topic="friday.pipeline",
            )
        except Exception as exc:
            print(f"[voice-log] failed to publish call log: {exc}")

    def queue_call_log(stage: str, message: str, level: str = "info", **extra) -> None:
        asyncio.create_task(emit_call_log(stage, message, level=level, **extra))

    local_stt = FasterWhisperSTT(log_event=emit_call_log)
    ollama = OllamaOptions()
    detection = VoiceDetectionOptions()
    assistant = Assistant(ollama=ollama, log_event=emit_call_log)

    tts_provider = os.getenv("TTS_PROVIDER", "piper").lower()
    voice_providers = {"piper": PiperTTS, "voicebox": VoiceboxTTS, "kokoro": KokoroTTS}
    if tts_provider not in voice_providers:
        raise ValueError("TTS_PROVIDER must be piper, voicebox, or kokoro")
    output_voice = voice_providers[tts_provider](log_event=emit_call_log)
    session = AgentSession(
        stt=stt.StreamAdapter(
            stt=local_stt,
            vad=inference.VAD(
                model="silero",
                activation_threshold=detection.activation_threshold,
                deactivation_threshold=detection.deactivation_threshold,
                min_speech_duration=detection.min_speech_duration,
                min_silence_duration=detection.min_silence_duration,
                prefix_padding_duration=detection.prefix_padding_duration,
            ),
        ),
        tts=output_voice,
        min_endpointing_delay=detection.min_endpointing_delay,
        max_endpointing_delay=detection.max_endpointing_delay,
    )

    @session.on("agent_state_changed")
    def _on_agent_state_changed(event) -> None:
        queue_call_log("agent", f"state {event.old_state} -> {event.new_state}")

    @session.on("user_state_changed")
    def _on_user_state_changed(event) -> None:
        queue_call_log("user", f"state {event.old_state} -> {event.new_state}")

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event) -> None:
        queue_call_log(
            "transcript",
            "user input transcribed",
            transcript=event.transcript,
            is_final=event.is_final,
            language=event.language,
            speaker_id=event.speaker_id,
        )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event) -> None:
        item = event.item
        role = getattr(item, "role", "unknown")
        content = getattr(item, "content", "")
        queue_call_log(
            "llm",
            "conversation item added",
            role=role,
            content=content,
        )

    @session.on("metrics_collected")
    def _on_metrics_collected(event) -> None:
        metrics = event.metrics
        model_dump = getattr(metrics, "model_dump", None)
        queue_call_log(
            "metrics",
            "metrics collected",
            metrics=model_dump() if model_dump else repr(metrics),
        )

    @session.on("error")
    def _on_error(event) -> None:
        queue_call_log(
            "error",
            str(event.error),
            level="error",
            source=event.source,
        )

    await emit_call_log(
        "agent",
        "voice session starting",
        stt=local_stt.model,
        llm=ollama.model,
        tts=output_voice.model,
        vad_activation=detection.activation_threshold,
        vad_min_speech=detection.min_speech_duration,
        vad_min_silence=detection.min_silence_duration,
        endpoint_min=detection.min_endpointing_delay,
    )

    await session.start(
        room=ctx.room,
        agent=assistant,
    )
    await emit_call_log("agent", "voice session started")

    async def prewarm_stt() -> None:
        try:
            await local_stt.prewarm()
            await emit_call_log("stt", "prewarm complete")
        except Exception as exc:
            await emit_call_log(
                "error",
                "stt prewarm failed",
                level="error",
                error=str(exc),
            )

    asyncio.create_task(prewarm_stt())

    async def prewarm_ollama() -> None:
        await assistant.prewarm()

    asyncio.create_task(prewarm_ollama())


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=os.getenv("LIVEKIT_AGENT_NAME", "friday"),
        )
    )
