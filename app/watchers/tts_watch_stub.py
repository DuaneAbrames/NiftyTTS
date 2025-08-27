# watchers/tts_watch.py
import time
from pathlib import Path
import shutil
import json

IN_DIR = Path(__file__).resolve().parents[1] / "jobs" / "incoming"
OUT_DIR = Path(__file__).resolve().parents[1] / "jobs" / "outgoing"

def write_stub_mp3(target: Path):
    # Tiny silent MP3 (1 second of silence at 11025 Hz, CBR 32kbps) – good enough for testing.
    # If you prefer, generate with ffmpeg once and copy; here we just drop a prebuilt blob.
    data = bytes.fromhex(
        "4944330300000000000F5449543200000000000354657300545045"
        "310000000000035465730054434F4E0000000000030000000000FFFB"
        "B0040000000000000000000000000000000000000000000000000000"
        "00000000"
    )
    target.write_bytes(data)

def main():
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    seen = set()

    print(f"Watching {IN_DIR} → {OUT_DIR}")

    def out_path(base: str) -> Path:
        meta = IN_DIR / f"{base}.json"
        out = OUT_DIR / f"{base}.mp3"
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                rel = data.get("output_rel")
                if rel:
                    out = OUT_DIR / rel
            except Exception:
                pass
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    while True:
        for txt in IN_DIR.glob("*.txt"):
            base = txt.stem  # e.g. example.com-abcdef1234567890
            if base in seen:
                continue
            seen.add(base)
            out = out_path(base)

            # If an mp3 already exists, skip
            if out.exists():
                continue

            # ---- replace this with REAL TTS ----
            write_stub_mp3(out)
            print(f"Created {out.name}")

        time.sleep(1.0)

if __name__ == "__main__":
    main()
