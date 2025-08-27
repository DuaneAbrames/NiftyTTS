# watchers/tts_watch_edge.py
import os
import time
import asyncio
from pathlib import Path
import traceback
import edge_tts

ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "jobs" / "incoming"
OUT_DIR = ROOT / "jobs" / "outgoing"

VOICE = os.environ.get("NIFTYTTS_EDGE_VOICE", "en-US-AriaNeural")
RATE = os.environ.get("NIFTYTTS_EDGE_RATE", "+0%")
PITCH = os.environ.get("NIFTYTTS_EDGE_PITCH", "+0Hz")
# Explicitly ask for MP3:
OUTPUT_FORMAT = os.environ.get("NIFTYTTS_EDGE_FORMAT", "audio-24khz-48kbitrate-mono-mp3")

POLL_INTERVAL = float(os.environ.get("NIFTYTTS_POLL_INTERVAL", "0.5"))
SYNTH_TIMEOUT = int(os.environ.get("NIFTYTTS_SYNTH_TIMEOUT", "600"))  # seconds
MIN_MP3_BYTES = int(os.environ.get("NIFTYTTS_MIN_MP3_BYTES", "1024")) # consider smaller as failure

def ensure_dirs():
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clean any stale tmp files in OUT_DIR
    for p in OUT_DIR.glob("*.mp3.tmp"):
        try:
            p.unlink()
        except Exception:
            pass

def write_err(base: str, msg: str, exc: BaseException | None = None, text_sample: str = ""):
    err = OUT_DIR / f"{base}.err.txt"  # put next to outputs so you see it on host volume
    blob = [f"ERROR: {msg}"]
    if exc:
        blob.append("\nTRACEBACK:\n" + "".join(traceback.format_exception(exc)))
    if text_sample:
        blob.append("\nTEXT SAMPLE (first 400 chars):\n" + text_sample[:400])
    err.write_text("\n\n".join(blob), encoding="utf-8")
    print(f"[x] {base}: {msg}. Details -> {err.name}")

async def synth_to_mp3(txt: str, tmp_mp3: Path):
    """
    Stream audio chunks from edge-tts and write them to tmp_mp3.
    """
    communicate = edge_tts.Communicate(
        txt, VOICE, rate=RATE, pitch=PITCH, output_format=OUTPUT_FORMAT
    )
    wrote = 0
    # Stream with a timeout
    async def _do():
        nonlocal wrote
        with open(tmp_mp3, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                    wrote += len(chunk["data"])
    await asyncio.wait_for(_do(), timeout=SYNTH_TIMEOUT)
    return wrote

def main():
    ensure_dirs()
    print(f"[edge-tts] Watching {IN_DIR} → {OUT_DIR}")
    print(f"[edge-tts] Voice={VOICE} Rate={RATE} Pitch={PITCH} Format={OUTPUT_FORMAT} Timeout={SYNTH_TIMEOUT}s")

    seen = set()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        for txt_path in IN_DIR.glob("*.txt"):
            base = txt_path.stem
            out_mp3 = OUT_DIR / f"{base}.mp3"

            if base in seen or (out_mp3.exists() and out_mp3.stat().st_size > 0):
                continue
            seen.add(base)

            raw = txt_path.read_text(encoding="utf-8", errors="replace")
            txt = raw.strip()
            print(f"[+] {base}: text_len={len(txt)}")

            if len(txt) == 0:
                write_err(base, "Empty text after preprocessing", None, raw)
                continue

            tmp_mp3 = out_mp3.with_name(out_mp3.name + ".tmp")

            start = time.time()
            try:
                bytes_written = loop.run_until_complete(synth_to_mp3(txt, tmp_mp3))
                dur = time.time() - start

                # Sanity checks
                if not tmp_mp3.exists():
                    write_err(base, "No tmp MP3 produced", None, txt)
                    continue

                size = tmp_mp3.stat().st_size
                print(f"[=] {base}: wrote tmp {size} bytes in {dur:.1f}s")
                if size < MIN_MP3_BYTES or bytes_written == 0:
                    write_err(base, f"MP3 too small ({size} bytes) or zero audio chunks", None, txt)
                    try: tmp_mp3.unlink()
                    except: pass
                    continue

                # Atomic replace inside OUT_DIR
                os.replace(tmp_mp3, out_mp3)
                print(f"[✓] {base}: finalized {out_mp3.name} ({out_mp3.stat().st_size} bytes)")
            except Exception as e:
                write_err(base, "Exception during synthesis", e, txt)
                try:
                    if tmp_mp3.exists():
                        tmp_mp3.unlink()
                except Exception:
                    pass
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
