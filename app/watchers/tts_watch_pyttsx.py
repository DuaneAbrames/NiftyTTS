# watchers/tts_watch_pyttsx.py
"""
Local TTS watcher for NiftyTTS:
- Watches jobs/incoming/*.txt
- Uses pyttsx3 to synthesize a WAV
- Converts WAV -> MP3 via ffmpeg
- Atomically writes jobs/outgoing/<base>.mp3
- Skips jobs already processed

Config via environment variables (optional):
  NIFTYTTS_VOICE_SUBSTR : case-insensitive substring to choose a voice
  NIFTYTTS_RATE_WPM     : integer words-per-minute (default 180)
  NIFTYTTS_VOLUME       : 0.0..1.0 (default 1.0)
"""

import os
import time
import subprocess
import traceback
from pathlib import Path
from typing import Optional
import json

import pyttsx3
from job_utils import parse_job_file, finalize_output, download_cover_image

ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "jobs" / "incoming"
OUT_DIR = ROOT / "jobs" / "outgoing"
TMP_DIR = ROOT / "jobs" / "tmp"

VOICE_SUBSTR = os.environ.get("NIFTYTTS_VOICE_SUBSTR", "").strip()
RATE_WPM = int(os.environ.get("NIFTYTTS_RATE_WPM", "180"))
VOLUME = float(os.environ.get("NIFTYTTS_VOLUME", "1.0"))

POLL_INTERVAL = float(os.environ.get("NIFTYTTS_POLL_INTERVAL", "0.5"))
SILENT_SECONDS_AFTER_DONE = 1.0
SYNTH_TIMEOUT = int(os.environ.get("NIFTYTTS_SYNTH_TIMEOUT", "600"))
MIN_MP3_BYTES = int(os.environ.get("NIFTYTTS_MIN_MP3_BYTES", "1024"))

FFMPEG_PATH = os.environ.get("NIFTYTTS_FFMPEG_PATH", "ffmpeg")


def ensure_dirs():
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    for p in TMP_DIR.glob("*.tmp"):
        try:
            p.unlink()
        except Exception:
            pass


def ffmpeg_exists() -> bool:
    try:
        proc = subprocess.run(
            [FFMPEG_PATH, "-version"], capture_output=True, check=False, timeout=15
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def wav_to_mp3(wav_path: Path, mp3_tmp: Path):
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "64k",
        str(mp3_tmp),
    ]
    subprocess.run(cmd, check=True, timeout=SYNTH_TIMEOUT)


def pick_voice(engine: pyttsx3.Engine, substr: str) -> Optional[str]:
    """Choose a voice id whose name matches substr (case-insensitive)."""
    if not substr:
        return None
    s = substr.lower()
    chosen = None
    for v in engine.getProperty("voices"):
        name = getattr(v, "name", "") or ""
        if s in name.lower():
            chosen = v.id
            break
    return chosen

def synth_to_wav(text: str, wav_path: Path) -> str:
    """Synthesize WAV and return the voice label used (name or id)."""
    engine = pyttsx3.init()
    vid = pick_voice(engine, VOICE_SUBSTR)
    if vid:
        engine.setProperty("voice", vid)
    engine.setProperty("rate", RATE_WPM)
    engine.setProperty("volume", VOLUME)
    engine.save_to_file(text, str(wav_path))
    engine.runAndWait()
    # Determine used voice label
    try:
        used_id = engine.getProperty("voice")
        name = None
        for v in engine.getProperty("voices"):
            if getattr(v, "id", None) == used_id:
                name = getattr(v, "name", None)
                break
        return name or str(used_id)
    except Exception:
        return VOICE_SUBSTR or "default"

def write_err(base: str, msg: str, exc: BaseException | None = None, text_sample: str = ""):
    err = OUT_DIR / f"{base}.err.txt"
    blob = [f"ERROR: {msg}"]
    if exc:
        blob.append("\nTRACEBACK:\n" + "".join(traceback.format_exception(exc)))
    if text_sample:
        blob.append("\nTEXT SAMPLE (first 400 chars):\n" + text_sample[:400])
    err.write_text("\n\n".join(blob), encoding="utf-8")
    print(f"[x] {base}: {msg}. Details -> {err.name}")


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

def process_job(txt_path: Path):
    base = txt_path.stem
    out_mp3 = out_path(base)
    err_file = out_mp3.with_suffix(".err.txt")
    if out_mp3.exists() and out_mp3.stat().st_size > 0 or (err_file.exists() and err_file.stat().st_size > 0):
        return


    wav_tmp = TMP_DIR / f"{base}.wav"
    mp3_tmp = TMP_DIR / f"{base}.mp3"

    raw = txt_path.read_text(encoding="utf-8", errors="replace")
    text = raw.strip()
    if len(text) == 0:
        write_err(base, "Empty text after preprocessing", None, raw)
        return

    try:
        print(f"[+] Synthesizing: {txt_path.name}")

        meta, body = parse_job_file(txt_path, base)
        # Enrich with album/track/url from incoming job JSON if available
        try:
            j = IN_DIR / f"{base}.json"
            if j.exists():
                data = json.loads(j.read_text(encoding="utf-8"))
                for k in ("album", "track", "url"):
                    if k in data and k not in meta:
                        meta[k] = data[k]
        except Exception:
            pass
        used_voice = synth_to_wav(body, wav_tmp)
        try:
            meta["composer"] = f"pyttsx3 TTS - {used_voice}"
        except Exception:
            pass

        if not ffmpeg_exists():
            raise RuntimeError(f"ffmpeg not found or failed to run: {FFMPEG_PATH}")

        wav_to_mp3(wav_tmp, mp3_tmp)
        size = mp3_tmp.stat().st_size
        if size < MIN_MP3_BYTES:
            raise RuntimeError(f"Generated MP3 too small ({size} bytes)")

        mp3_tmp.replace(out_mp3)
        finalize_output(out_mp3, meta)
        try:
            download_cover_image(out_mp3.parent)
        except Exception:
            pass
        print(f"[✓] Wrote: {out_mp3.name}")

        if err_file.exists():
            try:
                err_file.unlink()
                print(f"[-] {base}: cleared stale error log")
            except Exception:
                pass

    except Exception as e:
        write_err(base, "Exception during synthesis", e, text)
    finally:
        for p in (wav_tmp, mp3_tmp):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


def list_voices():
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    print("Available voices (use NIFTYTTS_VOICE_SUBSTR to select by substring):")
    for v in voices:
        print("-", getattr(v, "name", v.id))


def main():
    ensure_dirs()
    print(f"Watching: {IN_DIR}  →  {OUT_DIR}")
    print(f"Voice filter: {VOICE_SUBSTR or '(default)'} | Rate:{RATE_WPM} | Vol:{VOLUME}")
    if os.environ.get("NIFTYTTS_LIST_VOICES") == "1":
        list_voices()

    seen = set()
    time.sleep(SILENT_SECONDS_AFTER_DONE)

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

