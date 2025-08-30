from __future__ import annotations

"""Piper CLI backend.

Environment:
  - NIFTYTTS_PIPER_EXE    path to piper executable (default 'piper')
  - NIFTYTTS_PIPER_MODEL  path to .onnx model or directory containing models (default '/models')
  - NIFTYTTS_FFMPEG_PATH  path to ffmpeg (default 'ffmpeg')
  - NIFTYTTS_PIPER_LENGTH speaking rate (e.g., 1.0 normal, 0.9 slower)
  - NIFTYTTS_PIPER_NOISE  noise scale (0.667 default)
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import TTSBackend


class PiperBackend(TTSBackend):
    def __init__(self) -> None:
        self.piper_exe = os.environ.get("NIFTYTTS_PIPER_EXE", "piper")
        self.model_path = os.environ.get("NIFTYTTS_PIPER_MODEL", "/models")
        self.ffmpeg_path = os.environ.get("NIFTYTTS_FFMPEG_PATH", "ffmpeg")
        self.length = os.environ.get("NIFTYTTS_PIPER_LENGTH", "1.0")
        self.noise = os.environ.get("NIFTYTTS_PIPER_NOISE", "0.667")
        self.min_bytes = int(os.environ.get("NIFTYTTS_MIN_MP3_BYTES", "1024"))
        self.timeout = int(os.environ.get("NIFTYTTS_SYNTH_TIMEOUT", "600"))

    @property
    def backend_id(self) -> str:
        return "piper"

    @property
    def display_name(self) -> str:
        return "Piper TTS"

    def _check_tool(self, path: str, args: list[str]) -> bool:
        try:
            proc = subprocess.run([path, *args], capture_output=True, check=False, timeout=15)
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    def _resolve_model(self, override: Optional[str] = None) -> Optional[Path]:
        # explicit override path
        if override:
            p = Path(override)
            if p.is_file():
                return p
            # try to find by stem under directory
            base = Path(self.model_path)
            if base.is_dir():
                cand = base / f"{override}.onnx"
                if cand.is_file():
                    return cand
        # defaults
        p = Path(self.model_path)
        if p.is_file():
            return p
        if p.is_dir():
            first = next(p.glob("*.onnx"), None)
            return first
        return None

    def available(self) -> bool:
        exe_ok = self._check_tool(self.piper_exe, ["--help"])
        model_ok = self._resolve_model() is not None
        ffmpeg_ok = self._check_tool(self.ffmpeg_path, ["-version"])
        return exe_ok and model_ok and ffmpeg_ok

    def list_voices(self) -> List[Dict[str, Any]]:
        p = Path(self.model_path)
        out: List[Dict[str, Any]] = []
        if p.is_file() and p.suffix.lower() == ".onnx":
            out.append({"name": p.stem, "path": str(p)})
        elif p.is_dir():
            for f in sorted(p.glob("*.onnx")):
                out.append({"name": f.stem, "path": str(f)})
        return out

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
            "80k",
            str(mp3_tmp),
        ]
        subprocess.run(cmd, check=True, timeout=self.timeout)

    def synthesize_to_mp3(self, text: str, out_mp3: Path, meta: Dict[str, Any]) -> int:
        model = self._resolve_model(str(meta.get("voice") or None))
        if model is None:
            raise RuntimeError(f"Piper model not found (NIFTYTTS_PIPER_MODEL={self.model_path})")
        if not self._check_tool(self.piper_exe, ["--help"]):
            raise RuntimeError(f"Piper not found or failed to run: {self.piper_exe}")
        if not self._check_tool(self.ffmpeg_path, ["-version"]):
            raise RuntimeError(f"ffmpeg not found or failed to run: {self.ffmpeg_path}")

        tmp_dir = out_mp3.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        wav_tmp = tmp_dir / (out_mp3.stem + ".wav")
        mp3_tmp = tmp_dir / (out_mp3.stem + ".mp3")

        # Piper: synth to WAV
        cmd = [
            self.piper_exe,
            "--model",
            str(model),
            "--length_scale",
            str(self.length),
            "--noise_scale",
            str(self.noise),
            "--output_file",
            str(wav_tmp),
        ]
        subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=self.timeout)

        # ffmpeg: WAV -> MP3
        self._wav_to_mp3(wav_tmp, mp3_tmp)
        size = mp3_tmp.stat().st_size
        if size < self.min_bytes:
            raise RuntimeError(f"Generated MP3 too small ({size} bytes)")

        os_replace(mp3_tmp, out_mp3)

        # Clean up WAV; ignore errors
        try:
            if wav_tmp.exists():
                wav_tmp.unlink()
        except Exception:
            pass

        # Composer label hint
        try:
            if "composer" not in meta:
                meta["composer"] = f"piper TTS - {model.stem}"
        except Exception:
            pass

        return size


def os_replace(src: Path, dst: Path) -> None:
    import os

    os.replace(src, dst)


backend = PiperBackend()
