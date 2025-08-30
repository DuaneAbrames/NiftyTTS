from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import TTSBackend


class Pyttsx3Backend(TTSBackend):
    def __init__(self) -> None:
        self._pyttsx3 = None
        try:
            import pyttsx3  # type: ignore

            self._pyttsx3 = pyttsx3
        except Exception:
            self._pyttsx3 = None

        self.voice_substr = os.environ.get("NIFTYTTS_VOICE_SUBSTR", "").strip()
        self.rate_wpm = int(os.environ.get("NIFTYTTS_RATE_WPM", "180"))
        self.volume = float(os.environ.get("NIFTYTTS_VOLUME", "1.0"))
        self.ffmpeg_path = os.environ.get("NIFTYTTS_FFMPEG_PATH", "ffmpeg")
        self.min_bytes = int(os.environ.get("NIFTYTTS_MIN_MP3_BYTES", "1024"))
        self.timeout = int(os.environ.get("NIFTYTTS_SYNTH_TIMEOUT", "600"))

    @property
    def backend_id(self) -> str:
        return "pyttsx3"

    @property
    def display_name(self) -> str:
        return "Local TTS (pyttsx3)"

    def available(self) -> bool:
        return self._pyttsx3 is not None

    def list_voices(self) -> List[Dict[str, Any]]:
        if not self.available():
            return []
        engine = self._pyttsx3.init()  # type: ignore[union-attr]
        out: List[Dict[str, Any]] = []
        try:
            for v in engine.getProperty("voices"):
                name = getattr(v, "name", None) or getattr(v, "id", "")
                lang = next((l for l in getattr(v, "languages", []) if l), None)
                out.append({"name": str(name), "lang": str(lang or "")})
        except Exception:
            pass
        return out

    def _pick_voice(self, engine, substr: str) -> Optional[str]:
        if not substr:
            return None
        s = substr.lower()
        for v in engine.getProperty("voices"):
            name = getattr(v, "name", "") or ""
            if s in name.lower():
                return v.id
        return None

    def _ffmpeg_exists(self) -> bool:
        try:
            proc = subprocess.run([self.ffmpeg_path, "-version"], capture_output=True, check=False, timeout=15)
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    def _wav_to_mp3(self, wav_path: Path, mp3_tmp: Path) -> None:
        cmd = [
            self.ffmpeg_path,
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
        subprocess.run(cmd, check=True, timeout=self.timeout)

    def synthesize_to_mp3(self, text: str, out_mp3: Path, meta: Dict[str, Any]) -> int:
        if not self.available():
            raise RuntimeError("pyttsx3 is not available")
        if not self._ffmpeg_exists():
            raise RuntimeError(f"ffmpeg not found or failed to run: {self.ffmpeg_path}")

        engine = self._pyttsx3.init()  # type: ignore[union-attr]
        substr = str(meta.get("voice") or self.voice_substr)
        vid = self._pick_voice(engine, substr)
        if vid:
            engine.setProperty("voice", vid)
        engine.setProperty("rate", self.rate_wpm)
        engine.setProperty("volume", self.volume)

        tmp_dir = out_mp3.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        wav_tmp = tmp_dir / (out_mp3.stem + ".wav")
        mp3_tmp = tmp_dir / (out_mp3.stem + ".mp3")

        engine.save_to_file(text, str(wav_tmp))
        engine.runAndWait()

        self._wav_to_mp3(wav_tmp, mp3_tmp)
        size = mp3_tmp.stat().st_size
        if size < self.min_bytes:
            raise RuntimeError(f"Generated MP3 too small ({size} bytes)")
        os_replace(mp3_tmp, out_mp3)

        # Clean up
        try:
            if wav_tmp.exists():
                wav_tmp.unlink()
        except Exception:
            pass

        # set composer meta hint if not present
        try:
            if "composer" not in meta:
                meta["composer"] = f"pyttsx3 TTS - {substr or 'default'}"
        except Exception:
            pass

        return size


def os_replace(src: Path, dst: Path) -> None:
    import os

    os.replace(src, dst)


backend = Pyttsx3Backend()
