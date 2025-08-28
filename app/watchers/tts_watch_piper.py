# watchers/tts_watch_piper.py
"""
Piper watcher:
- Watches jobs/incoming/*.txt
- Calls piper with a selected model to produce WAV
- Encodes WAV → MP3 using ffmpeg
- Atomic move into jobs/outgoing/<base>.mp3

Env vars (or edit defaults below):
  NIFTYTTS_PIPER_EXE   : path to piper executable (default ``piper`` in PATH)
  NIFTYTTS_PIPER_MODEL : path to voice model .onnx or directory containing models (default ``/models``)
  NIFTYTTS_FFMPEG_PATH : path to ffmpeg (default ``ffmpeg`` in PATH)
  NIFTYTTS_PIPER_LENGTH: speaking rate (e.g., 1.0 normal, 0.9 slower, 1.1 faster)
  NIFTYTTS_PIPER_NOISE : noise scale (0.667 default)
"""

import os
import time
import subprocess
import traceback
from pathlib import Path
from job_utils import parse_job_file, finalize_output
import json


ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "jobs" / "incoming"
OUT_DIR = ROOT / "jobs" / "outgoing"
TMP_DIR = ROOT / "jobs" / "tmp"

PIPER_EXE   = os.environ.get("NIFTYTTS_PIPER_EXE",   "piper")
PIPER_MODEL = os.environ.get("NIFTYTTS_PIPER_MODEL", "/models")
FFMPEG_PATH = os.environ.get("NIFTYTTS_FFMPEG_PATH", "ffmpeg")

# Resolve model directory to first .onnx if needed
_model_path = Path(PIPER_MODEL)
if _model_path.is_dir():
    first = next(_model_path.glob("*.onnx"), None)
    if first:
        PIPER_MODEL = str(first)

PIPER_LENGTH = os.environ.get("NIFTYTTS_PIPER_LENGTH", "1.0")
PIPER_NOISE  = os.environ.get("NIFTYTTS_PIPER_NOISE",  "0.667")

POLL_INTERVAL = float(os.environ.get("NIFTYTTS_POLL_INTERVAL", "0.5"))
SYNTH_TIMEOUT = int(os.environ.get("NIFTYTTS_SYNTH_TIMEOUT", "600"))
MIN_MP3_BYTES = int(os.environ.get("NIFTYTTS_MIN_MP3_BYTES", "1024"))

def ensure_dirs():
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # Clean any stale tmp files
    for p in TMP_DIR.glob("*.tmp"):
        try:
            p.unlink()
        except Exception:
            pass

def check_tool(path: str, args: list[str]) -> bool:
    """Return True only if the tool runs successfully.

    Previously this helper only verified that the executable existed. If the
    command failed (non-zero exit status) the function still returned True,
    causing the watcher to proceed with a broken tool and later fail deeper in
    the pipeline. We now inspect the return code so invalid binaries are
    detected early."""

    try:
        proc = subprocess.run([path, *args], capture_output=True, check=False, timeout=15)
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def write_err(base: str, msg: str, exc: BaseException | None = None, text_sample: str = ""):
    err = OUT_DIR / f"{base}.err.txt"
    blob = [f"ERROR: {msg}"]
    if exc:
        blob.append("\nTRACEBACK:\n" + "".join(traceback.format_exception(exc)))
    if text_sample:
        blob.append("\nTEXT SAMPLE (first 400 chars):\n" + text_sample[:400])
    err.write_text("\n\n".join(blob), encoding="utf-8")
    print(f"[x] {base}: {msg}. Details -> {err.name}")


def piper_to_wav(text: str, wav_path: Path):
    cmd = [
        PIPER_EXE,
        "--model", PIPER_MODEL,
        "--length_scale", PIPER_LENGTH,
        "--noise_scale", PIPER_NOISE,
        "--output_file", str(wav_path),
    ]
    subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=SYNTH_TIMEOUT)

def wav_to_mp3(wav_path: Path, mp3_tmp: Path):
    cmd = [
        FFMPEG_PATH, "-y",
        "-hide_banner", "-loglevel", "error",
        "-i", str(wav_path),
        "-vn", "-ac", "1", "-ar", "44100", "-b:a", "80k",
        str(mp3_tmp),
    ]
    subprocess.run(cmd, check=True, timeout=SYNTH_TIMEOUT)

def out_path(base: str) -> Path:
    meta = IN_DIR / f"{base}.json"
    out_mp3 = OUT_DIR / f"{base}.mp3"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            rel = data.get("output_rel")
            if rel:
                out_mp3 = OUT_DIR / rel
        except Exception:
            pass
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    return out_mp3


def process_job(txt: Path):
    base = txt.stem

    out_mp3 = out_path(base)
    err_file = out_mp3.with_suffix(".err.txt")
    if out_mp3.exists() and out_mp3.stat().st_size > 0 or (err_file.exists() and err_file.stat().st_size > 0):
        return

    tmp_wav = TMP_DIR / f"{base}.wav"
    tmp_mp3 = TMP_DIR / f"{base}.mp3"

    raw = txt.read_text(encoding="utf-8", errors="replace")
    text = raw.strip()
    if len(text) == 0:
        write_err(base, "Empty text after preprocessing", None, raw)
        return

    try:
        print(f"[+] Piper synth: {txt.name}")
        if not check_tool(PIPER_EXE, ["--help"]):
            raise RuntimeError(f"Piper not found or failed to run: {PIPER_EXE}")
        if not check_tool(FFMPEG_PATH, ["-version"]):
            raise RuntimeError(f"ffmpeg not found or failed to run: {FFMPEG_PATH}")
        if not Path(PIPER_MODEL).is_file():
            raise FileNotFoundError(f"Piper model not found: {PIPER_MODEL}")
        meta, body = parse_job_file(txt, base)
        # Enrich with album/track from incoming job JSON if available
        try:
            j = IN_DIR / f"{base}.json"
            if j.exists():
                data = json.loads(j.read_text(encoding="utf-8"))
                for k in ("album", "track"):
                    if k in data and k not in meta:
                        meta[k] = data[k]
        except Exception:
            pass
        piper_to_wav(body, tmp_wav)

        wav_to_mp3(tmp_wav, tmp_mp3)
        size = tmp_mp3.stat().st_size
        if size < MIN_MP3_BYTES:
            raise RuntimeError(f"Generated MP3 too small ({size} bytes)")
        tmp_mp3.replace(out_mp3)
        finalize_output(out_mp3, meta)
        print(f"[✓] wrote {out_mp3.name}")

        if err_file.exists():
            try:
                err_file.unlink()
                print(f"[-] {base}: cleared stale error log")
            except Exception:
                pass
    except Exception as e:
        write_err(base, "Exception during synthesis", e, text)
    finally:
        for p in (tmp_wav, tmp_mp3):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

def main():
    ensure_dirs()
    print(f"Piper watcher on {IN_DIR} → {OUT_DIR}")
    print(f"Model={PIPER_MODEL} length={PIPER_LENGTH} noise={PIPER_NOISE}")
    seen = set()
    while True:
        for txt in IN_DIR.glob("*.txt"):
            base = txt.stem
            out_mp3 = out_path(base)
            err_file = out_mp3.with_suffix(".err.txt")
            if base in seen or (out_mp3.exists() and out_mp3.stat().st_size > 0) or (err_file.exists() and err_file.stat().st_size > 0):

                continue
            seen.add(base)
            process_job(txt)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
