# watchers/tts_watch_edge.py
"""
Edge TTS watcher:
- Watches jobs/incoming/*.txt
- Uses edge-tts (Microsoft neural voices) to synthesize directly to MP3
- Atomically writes jobs/outgoing/<base>.mp3

Config via env (optional):
  NIFTYTTS_EDGE_VOICE   : e.g. "en-US-AriaNeural", "en-US-GuyNeural"
  NIFTYTTS_EDGE_RATE    : e.g. "+0%" "-10%" "+15%"
  NIFTYTTS_EDGE_PITCH   : e.g. "+0Hz" "-2Hz" "+5Hz"
"""

import os
import time
import asyncio
from pathlib import Path
import edge_tts

ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "jobs" / "incoming"
OUT_DIR = ROOT / "jobs" / "outgoing"
TMP_DIR = ROOT / "jobs" / "outgoing"

VOICE = os.environ.get("NIFTYTTS_EDGE_VOICE", "en-US-GuyNeural")
RATE = os.environ.get("NIFTYTTS_EDGE_RATE", "+0%")
PITCH = os.environ.get("NIFTYTTS_EDGE_PITCH", "+0Hz")
POLL_INTERVAL = 0.5

def ensure_dirs():
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # clean stray temp files in OUT_DIR
    for p in OUT_DIR.glob("*.mp3.tmp"):
        try:
            p.unlink()
        except Exception:
            pass

async def synth_file(text_path: Path, out_mp3: Path):
    # temp file in same dir to avoid cross-device errors
    tmp_mp3 = out_mp3.with_name(out_mp3.name + ".tmp")

    txt = text_path.read_text(encoding="utf-8", errors="replace")

    communicate = edge_tts.Communicate(txt, VOICE, rate=RATE, pitch=PITCH)
    # Await directly, with timeout
    await asyncio.wait_for(communicate.save(str(tmp_mp3)), timeout=SYNTH_TIMEOUT)

    if not tmp_mp3.exists() or tmp_mp3.stat().st_size == 0:
        raise RuntimeError(f"Synthesis produced empty file: {tmp_mp3}")

    os.replace(tmp_mp3, out_mp3)


def main():
    ensure_dirs()
    print(f"Edge TTS watching {IN_DIR} → {OUT_DIR}")
    print(f"Voice={VOICE} Rate={RATE} Pitch={PITCH}")
    seen = set()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        for txt in IN_DIR.glob("*.txt"):
            base = txt.stem
            out_mp3 = OUT_DIR / f"{base}.mp3"
            if base in seen or (out_mp3.exists() and out_mp3.stat().st_size > 0):
                continue
            seen.add(base)
            try:
                print(f"[+] {base}: synthesizing with edge-tts…")
                loop.run_until_complete(synth_file(txt, out_mp3))
                print(f"[✓] wrote {out_mp3.name}")
            except Exception as e:
                print(f"[x] {base}: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
