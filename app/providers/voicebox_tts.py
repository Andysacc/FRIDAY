"""Output-only Voicebox adapter. Audio always plays through the LiveKit call."""

import asyncio
import io
import os
import time
import wave

import httpx
from livekit.agents import tts
from livekit.agents.types import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.utils import shortuuid

from app.providers.voice_presets import AMERICAN_FEMALE_VOICES


class VoiceboxTTS(tts.TTS):
    def __init__(self, *, log_event=None):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000, num_channels=1,
        )
        self.voice_id = os.getenv("VOICEBOX_VOICE", "af_heart")
        if self.voice_id not in AMERICAN_FEMALE_VOICES:
            raise ValueError(f"Unsupported American female voice: {self.voice_id}")
        self.base_url = os.getenv("VOICEBOX_BASE_URL", "http://host.docker.internal:17493").rstrip("/")
        self.timeout = float(os.getenv("VOICEBOX_TIMEOUT", "60"))
        self._profile_id = None
        self._profile_lock = asyncio.Lock()
        self._log_event = log_event

    @property
    def model(self):
        return f"kokoro/{self.voice_id}"

    @property
    def provider(self):
        return "voicebox"

    async def _profile(self, client):
        async with self._profile_lock:
            if self._profile_id:
                return self._profile_id
            response = await client.get("/profiles")
            response.raise_for_status()
            for profile in response.json():
                if (profile.get("voice_type") == "preset"
                        and profile.get("preset_engine") == "kokoro"
                        and profile.get("preset_voice_id") == self.voice_id):
                    self._profile_id = profile["id"]
                    return self._profile_id
            response = await client.post("/profiles", json={
                "name": f"FRIDAY — {AMERICAN_FEMALE_VOICES[self.voice_id]}",
                "language": "en", "voice_type": "preset",
                "preset_engine": "kokoro", "preset_voice_id": self.voice_id,
                "default_engine": "kokoro",
            })
            response.raise_for_status()
            self._profile_id = response.json()["id"]
            return self._profile_id

    async def generate_audio(self, text):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            profile_id = await self._profile(client)
            response = await client.post("/generate/stream", json={
                "profile_id": profile_id, "text": text,
                "language": "en", "engine": "kokoro", "personality": False,
                "effects_chain": [], "normalize": True,
            })
            response.raise_for_status()
            with wave.open(io.BytesIO(response.content), "rb") as audio:
                if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (24000, 1, 2):
                    raise ValueError("Voicebox must return 24 kHz mono PCM16 WAV for Kokoro")
                pcm = audio.readframes(audio.getnframes())
                if not pcm:
                    raise ValueError("Voicebox returned empty speech")
                return pcm

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS):
        return PCMChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class PCMChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter):
        voice = self._tts
        started = time.monotonic()
        pcm = await voice.generate_audio(self.input_text)
        if voice._log_event:
            await voice._log_event("tts", "first audio ready", provider=voice.provider,
                                   model=voice.model, elapsed=round(time.monotonic() - started, 3))
        output_emitter.initialize(request_id=shortuuid(), sample_rate=24000,
                                  num_channels=1, mime_type="audio/pcm")
        for offset in range(0, len(pcm), 9600):
            output_emitter.push(pcm[offset:offset + 9600])
        output_emitter.flush()
