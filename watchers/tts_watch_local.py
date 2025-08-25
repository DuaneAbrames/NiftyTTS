# watchers/tts_watch_local.py
"""
Local TTS watcher for NiftyTTS:
- Watches jobs/incoming/*.txt
- Uses pyttsx3 (Windows SAPI) to synthesize a WAV
- Converts WAV -> MP3 via ffmpeg
- Atomically writes jobs/outgoing/<base>.mp3
- Skips jobs already processed

Config via environment variables (optional):
  NIFTYTTS_VOICE_SUBSTR   : case-insensitive substring to choose a voice
  NIFTYTTS_RATE_WPM       : integer words-per-minute (default 180)
  NIFTYTTS_VOLUME         : 0.0..1.0 (default 1.0)
"""

import os
import time
import subprocess
from pathlib import Path
from typing import Optional

import pyttsx3


# Add near the top
FFMPEG_PATH = os.environ.get(
    "NIFTYTTS_FFMPEG_PATH",
    r"C:\Users\Downloads\ffmpeg\ffmpeg.exe"
)



ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "jobs" / "incoming"
OUT_DIR = ROOT / "jobs" / "outgoing"
TMP_DIR = ROOT / "jobs" / "tmp"

VOICE_SUBSTR = os.environ.get("NIFTYTTS_VOICE_SUBSTR", "").strip()
RATE_WPM = int(os.environ.get("NIFTYTTS_RATE_WPM", "180"))
VOLUME = float(os.environ.get("NIFTYTTS_VOLUME", "1.0"))

POLL_INTERVAL = 0.5   # seconds
SILENT_SECONDS_AFTER_DONE = 1.0

def ensure_dirs():
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

def ffmpeg_exists() -> bool:
    try:
        subprocess.run([FFMPEG_PATH, "-version"], capture_output=True, check=False)
        return True
    except FileNotFoundError:
        return False

def wav_to_mp3(wav_path: Path, mp3_tmp: Path):
    # 64 kbps, mono, 44.1kHz — tweak to taste
    cmd = [
        FFMPEG_PATH, "-y",
        "-hide_banner", "-loglevel", "error",
        "-i", str(wav_path),
        "-vn",
        "-ac", "1",
        "-ar", "44100",
        "-b:a", "64k",
        str(mp3_tmp),
    ]
    subprocess.run(cmd, check=True)



def pick_voice(engine: pyttsx3.Engine, substr: str) -> Optional[str]:
    """Choose a voice id whose name matches substr (case-insensitive)."""
    if not substr:
        return None
    s = substr.lower()
    chosen = None
    for v in engine.getProperty("voices"):
        # On Windows SAPI, 'name' contains a friendly string like "Microsoft David Desktop - English (United States)"
        name = getattr(v, "name", "") or ""
        if s in name.lower():
            chosen = v.id
            break
    return chosen

def synth_to_wav(text_path: Path, wav_path: Path):
    engine = pyttsx3.init()  # SAPI5 on Windows
    # Voice selection
    vid = pick_voice(engine, VOICE_SUBSTR)
    if vid:
        engine.setProperty("voice", vid)
    # Speed & volume
    engine.setProperty("rate", RATE_WPM)
    engine.setProperty("volume", VOLUME)
    # Load text
    text = text_path.read_text(encoding="utf-8", errors="replace")
    # Save to WAV (blocking until done)
    engine.save_to_file(text, str(wav_path))
    engine.runAndWait()

def process_job(txt_path: Path):
    base = txt_path.stem  # e.g. example.com-a1b2c3d4e5f6a7b8
    out_mp3 = OUT_DIR / f"{base}.mp3"
    if out_mp3.exists() and out_mp3.stat().st_size > 0:
        return  # already done

    # Temporary files in tmp/
    wav_tmp = TMP_DIR / f"{base}.wav"
    mp3_tmp = TMP_DIR / f"{base}.mp3"

    try:
        print(f"[+] Synthesizing: {txt_path.name}")
        synth_to_wav(txt_path, wav_tmp)

        if not ffmpeg_exists():
            print("[!] ffmpeg not found; install it or add to PATH.")
            print(f"    Leaving WAV at: {wav_tmp}")
            return

        wav_to_mp3(wav_tmp, mp3_tmp)

        # Atomic move: tmp -> outgoing
        mp3_tmp.replace(out_mp3)
        print(f"[✓] Wrote: {out_mp3.name}")

    except Exception as e:
        print(f"[x] Error processing {txt_path.name}: {e}")
    finally:
        # Clean temp WAV/MP3 if present
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
    # tiny delay if the web app just wrote a fresh file and is about to check
    time.sleep(SILENT_SECONDS_AFTER_DONE)

    while True:
        for txt in IN_DIR.glob("*.txt"):
            base = txt.stem
            out_mp3 = OUT_DIR / f"{base}.mp3"
            if base in seen or (out_mp3.exists() and out_mp3.stat().st_size > 0):
                continue
            seen.add(base)
            process_job(txt)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
