"""Run with python -m scripts.generate_voice_auditions (optional requirements)."""

import html
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]

from app.providers.voice_presets import AMERICAN_FEMALE_VOICES

TEXT = ("Hello, I'm Friday, your personal assistant. Your systems are online, "
        "and everything is ready. What would you like to work on today?")


def main():
    import numpy as np
    import soundfile as sf
    import httpx
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    output = ROOT / "app" / "static" / "voice-auditions"
    output.mkdir(parents=True, exist_ok=True)
    model_dir = ROOT / "models" / "kokoro-onnx"
    model_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        target = model_dir / filename
        if not target.exists():
            print(f"Downloading {filename}…", flush=True)
            url = f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/{filename}"
            partial = target.with_suffix(".partial")
            for attempt in range(3):
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                try:
                    with httpx.stream("GET", url, headers=headers,
                                      follow_redirects=True, timeout=60) as response:
                        response.raise_for_status()
                        mode = "ab" if offset and response.status_code == 206 else "wb"
                        with partial.open(mode) as file:
                            for chunk in response.iter_bytes(1024 * 1024):
                                file.write(chunk)
                    break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    print(f"Resuming interrupted download of {filename}…", flush=True)
            partial.replace(target)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(model_dir / "kokoro-v1.0.onnx"),
                                   sess_options=options, providers=["CPUExecutionProvider"])
    pipeline = Kokoro.from_session(session, str(model_dir / "voices-v1.0.bin"))
    playlist, records, cards = [], [], []
    for index, (voice_id, name) in enumerate(AMERICAN_FEMALE_VOICES.items(), 1):
        path = output / f"{voice_id}.wav"
        start = time.monotonic()
        if not path.exists():
            audio, rate = pipeline.create(
                f"Voice {index}. {name}. {TEXT}", voice=voice_id, speed=1.0, lang="en-us",
            )
            sf.write(path, audio, rate, subtype="PCM_16")
        audio, rate = sf.read(path, dtype="float32")
        if rate != 24000 or not len(audio) or not np.isfinite(audio).all():
            raise RuntimeError(f"Invalid audio: {name}")
        playlist.extend([audio, np.zeros(24000, dtype="float32")])
        record = {"name": name, "voice_id": voice_id, "file": path.name,
                  "duration_seconds": round(len(audio) / rate, 2),
                  "generation_seconds": round(time.monotonic() - start, 2)}
        records.append(record)
        print(json.dumps(record), flush=True)
        cards.append(f'<article><h2>{index:02d} · {html.escape(name)}</h2>'
                     f'<audio controls preload="none" src="{path.name}"></audio>'
                     f'<p>{voice_id}</p></article>')
    sf.write(output / "all-american-female-voices.wav", np.concatenate(playlist),
             24000, subtype="PCM_16")
    (output / "manifest.json").write_text(json.dumps(
        {"engine": "Kokoro-82M v1.0 (ONNX, full precision)", "text": TEXT, "voices": records}, indent=2))
    (output / "index.html").write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Choose FRIDAY's voice</title><style>
body{margin:0;background:#101720;color:#eef3fa;font:16px system-ui;padding:32px;max-width:1000px;margin:auto}
h1{font-size:32px}p{color:#a8bbce;line-height:1.6}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
article{background:#1b2838;border:1px solid #31465c;border-radius:14px;padding:20px}h2{font-size:20px;margin-top:0}audio{width:100%}
.all{padding:24px 0}a{color:#9ecbff}</style>
<h1>Choose FRIDAY's voice</h1><p>11 American female voices. Each reads the same passage at its normal speed.
Tell me the name you prefer to use for FRIDAY.</p><section class="all"><h2>Listen to all 11</h2>
<audio controls preload="metadata" src="all-american-female-voices.wav"></audio></section><main>'''
        + "".join(cards) + '''</main><script>
document.addEventListener('play',e=>{if(e.target.tagName==='AUDIO')document.querySelectorAll('audio').forEach(a=>{if(a!==e.target)a.pause()})},true);
</script></html>''')
    print(f"Audition page: {output / 'index.html'}", flush=True)


if __name__ == "__main__":
    main()
