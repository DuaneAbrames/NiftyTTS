# watchers/tts_watch_stub.py
import os
import time
import traceback
from pathlib import Path

import shutil
from job_utils import parse_job_file, finalize_output
import json


IN_DIR = Path(__file__).resolve().parents[1] / "jobs" / "incoming"
OUT_DIR = Path(__file__).resolve().parents[1] / "jobs" / "outgoing"

POLL_INTERVAL = float(os.environ.get("NIFTYTTS_POLL_INTERVAL", "0.5"))

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
    

def write_err(base: str, msg: str, exc: BaseException | None = None, text_sample: str = ""):
    err = OUT_DIR / f"{base}.err.txt"
    blob = [f"ERROR: {msg}"]
    if exc:
        blob.append("\nTRACEBACK:\n" + "".join(traceback.format_exception(exc)))
    if text_sample:
        blob.append("\nTEXT SAMPLE (first 400 chars):\n" + text_sample[:400])
    err.write_text("\n\n".join(blob), encoding="utf-8")
    print(f"[x] {base}: {msg}. Details -> {err.name}")


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
            base = txt.stem
            out = OUT_DIR / f"{base}.mp3"
            err_file = OUT_DIR / f"{base}.err.txt"
            if base in seen or (out.exists() and out.stat().st_size > 0) or (err_file.exists() and err_file.stat().st_size > 0):
                continue
            seen.add(base)
            out = out_path(base)

            raw = txt.read_text(encoding="utf-8", errors="replace")
            text = raw.strip()
            if len(text) == 0:
                write_err(base, "Empty text after preprocessing", None, raw)
                continue
            try:
                meta, _ = parse_job_file(txt, base)
                write_stub_mp3(out)
                finalize_output(out, meta)
                print(f"Created {out.name}")
                write_stub_mp3(out)
                print(f"Created {out.name}")
                if err_file.exists():
                    try:
                        err_file.unlink()
                        print(f"[-] {base}: cleared stale error log")
                    except Exception:
                        pass
            except Exception as e:
                write_err(base, "Failed to write stub MP3", e, text)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
