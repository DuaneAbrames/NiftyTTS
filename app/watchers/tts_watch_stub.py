# watchers/tts_watch.py
import time
from pathlib import Path
import shutil
from job_utils import parse_job_file, finalize_output

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
    while True:
        for txt in IN_DIR.glob("*.txt"):
            base = txt.stem  # e.g. example.com-abcdef1234567890
            if base in seen:
                continue
            seen.add(base)
            out = OUT_DIR / f"{base}.mp3"

            # If an mp3 already exists, skip
            if out.exists():
                continue

            meta, _ = parse_job_file(txt, base)
            write_stub_mp3(out)
            finalize_output(out, meta)
            print(f"Created {out.name}")

        time.sleep(1.0)

if __name__ == "__main__":
    main()
