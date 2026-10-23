"""Local output-only Kokoro, matching the voice audition engine."""

import asyncio
import os
from pathlib import Path
import threading

from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from app.providers.voice_presets import AMERICAN_FEMALE_VOICES
from app.providers.voicebox_tts import PCMChunkedStream


class KokoroTTS(tts.TTS):
    def __init__(self, *, log_event=None):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False),
                         sample_rate=24000, num_channels=1)
        self.voice_id = os.getenv("KOKORO_VOICE", "af_heart")
        if self.voice_id not in AMERICAN_FEMALE_VOICES:
            raise ValueError(f"Unsupported American female voice: {self.voice_id}")
        self.model_dir = Path(os.getenv("KOKORO_MODEL_DIR", "models/kokoro-onnx"))
        self._pipeline = None
        # Keep the model protected even when an interrupted asyncio task has
        # stopped waiting for its background inference thread.
        self._lock = threading.Lock()
        self._log_event = log_event

    @property
    def provider(self):
        return "kokoro"

    @property
    def model(self):
        return f"kokoro/{self.voice_id}"

    def _load(self):
        if self._pipeline is None:
            from kokoro_onnx import Kokoro
            import onnxruntime as ort

            model = self.model_dir / "kokoro-v1.0.onnx"
            voices = self.model_dir / "voices-v1.0.bin"
            if not model.is_file() or not voices.is_file():
                raise FileNotFoundError(f"Kokoro model and voices are required in {self.model_dir}")
            options = ort.SessionOptions()
            options.intra_op_num_threads = int(os.getenv("KOKORO_NUM_THREADS", "4"))
            session = ort.InferenceSession(str(model), sess_options=options,
                                           providers=["CPUExecutionProvider"])
            self._pipeline = Kokoro.from_session(session, str(voices))

    def _generate(self, text):
        import numpy as np

        with self._lock:
            self._load()
            audio, rate = self._pipeline.create(text, voice=self.voice_id,
                                                 speed=1.0, lang="en-us")
            if rate != 24000 or audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
                raise ValueError("Kokoro returned invalid audio")
            return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()

    async def generate_audio(self, text):
        return await asyncio.to_thread(self._generate, text)

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return PCMChunkedStream(tts=self, input_text=text, conn_options=conn_options)
