# American female voice auditions

FRIDAY supports output-only Voicebox TTS with the eleven Kokoro American female
presets: Alloy, Aoede, Bella, Heart, Jessica, Kore, Nicole, Nova, River, Sarah,
and Sky. Listening, transcription, the response model, and LiveKit remain in
FRIDAY. Heart (`af_heart`) is the selected voice, running directly through the
local Kokoro ONNX engine; the standalone Voicebox studio is optional.

## Selected Heart output

The active `.env` uses:

```env
TTS_PROVIDER=kokoro
KOKORO_VOICE=af_heart
KOKORO_MODEL_DIR=models/kokoro-onnx
KOKORO_NUM_THREADS=4
```

The agent loads the same full-precision model and preset file used for the
auditions from its mounted `models/` folder. The Docker image includes the
lightweight output runtime. It generates speech locally and sends it through
the existing LiveKit call. Rebuild and recreate `friday-agent` after changing
the provider or voice. The code falls back to the Piper default only when
`TTS_PROVIDER` is unset; it does not silently change voices on engine errors.

## Generate optional voice previews

Preview recordings are generated locally and excluded from version control. They
are not required for live speech synthesis. Install the optional dependencies
and run the generator:

```sh
.venv/bin/python -m pip install -r requirements-voice-samples.txt
.venv/bin/python -m scripts.generate_voice_auditions
```

Open `app/static/voice-auditions/index.html` locally. With the dashboard serving
these updated files, the same page is available at `/voice-auditions/`.
It includes individual recordings and a combined, numbered comparison.
The recordings use the full-precision Kokoro v1.0 ONNX model with the same
preset IDs used by Voicebox. The preview runtime uses different text
preprocessing, so final delivery should also be checked in Voicebox.
This does not require installing the Voicebox studio.

## Optional: use the Voicebox server instead

1. Run the Voicebox backend on the host and download its Kokoro model.
2. Set these values in `.env`, replacing the voice ID with the chosen preset:

   ```env
   TTS_PROVIDER=voicebox
   VOICEBOX_BASE_URL=http://host.docker.internal:17493
   VOICEBOX_VOICE=af_heart
   VOICEBOX_TIMEOUT=60
   ```

3. Rebuild and recreate `friday-agent`. For a locally running agent, use
   `VOICEBOX_BASE_URL=http://127.0.0.1:17493` instead.

The adapter reuses or creates a matching preset profile, requests WAV audio
through `/generate/stream`, and publishes PCM audio through LiveKit. It does
not invoke Voicebox speaker playback, dictation, or personality rewriting.
The Voicebox backend must be reachable from the agent container; its default
loopback-only bind may need adjusting for Docker Desktop. Limit backend access
to the local machine/container network.

Despite the endpoint name, Voicebox finishes rendering each phrase before
transferring its WAV. Existing phrase splitting keeps replies bounded, but
end-to-end latency and interruption behavior require a live call test once
Voicebox is running. Synthesis timing in the call log is not total call latency.

Set `TTS_PROVIDER=piper` and recreate the agent to restore Piper.
