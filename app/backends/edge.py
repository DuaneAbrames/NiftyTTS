from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List

from .base import TTSBackend


class EdgeBackend(TTSBackend):
    def __init__(self) -> None:
        self._edge_tts = None
        try:
            import edge_tts  # type: ignore

            self._edge_tts = edge_tts
        except Exception:
            self._edge_tts = None

        self.voice = os.environ.get("NIFTYTTS_EDGE_VOICE", "en-US-AriaNeural")
        self.rate = os.environ.get("NIFTYTTS_EDGE_RATE", "+0%")
        self.pitch = os.environ.get("NIFTYTTS_EDGE_PITCH", "+0Hz")
        self.format = os.environ.get(
            "NIFTYTTS_EDGE_FORMAT", "audio-24khz-48kbitrate-mono-mp3"
        )
        self.min_bytes = int(os.environ.get("NIFTYTTS_MIN_MP3_BYTES", "1024"))
        self.timeout = int(os.environ.get("NIFTYTTS_SYNTH_TIMEOUT", "600"))

    @property
    def backend_id(self) -> str:
        return "edge"

    @property
    def display_name(self) -> str:
        return "Microsoft Edge TTS"

    def available(self) -> bool:
        return self._edge_tts is not None

    def list_voices(self) -> List[Dict[str, Any]]:
        if not self.available():
            return []
        edge_tts = self._edge_tts
        assert edge_tts is not None

        async def _fetch() -> List[Dict[str, Any]]:
            try:
                voices = await edge_tts.list_voices()
                out: List[Dict[str, Any]] = []
                for v in voices:
                    # Normalize keys we care about
                    out.append(
                        {
                            "name": v.get("ShortName") or v.get("Name") or "",
                            "locale": v.get("Locale"),
                            "gender": v.get("Gender"),
                            "style": ", ".join(v.get("StyleList") or []),
                            "friendly": v.get("FriendlyName"),
                        }
                    )
                return out
            except Exception:
                return []

        # Run in a fresh loop (caller may be sync context)
        try:
            return asyncio.run(_fetch())
        except RuntimeError:
            # Already inside loop: create a temporary loop
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_fetch())
            finally:
                loop.close()

    def synthesize_to_mp3(self, text: str, out_mp3: Path, meta: Dict[str, Any]) -> int:
        if not self.available():
            raise RuntimeError("edge_tts is not available")
        edge_tts = self._edge_tts
        assert edge_tts is not None

        tmp_mp3 = out_mp3.with_name(out_mp3.name + ".tmp")

        async def _run() -> int:
            voice = str(meta.get("voice") or self.voice)
            communicate = edge_tts.Communicate(text, voice, rate=self.rate, pitch=self.pitch)
            try:
                await asyncio.wait_for(
                    communicate.save(str(tmp_mp3), **{"format": self.format}),
                    timeout=self.timeout,
                )
            except TypeError:
                await asyncio.wait_for(communicate.save(str(tmp_mp3)), timeout=self.timeout)

            if tmp_mp3.exists():
                size = tmp_mp3.stat().st_size
                if size < self.min_bytes:
                    try:
                        tmp_mp3.unlink()
                    except Exception:
                        pass
                    raise RuntimeError(f"edge-tts produced a too-small MP3 ({size} bytes)")
                os_replace(tmp_mp3, out_mp3)
                return size

            if out_mp3.exists():
                size = out_mp3.stat().st_size
                if size >= self.min_bytes:
                    return size
            raise RuntimeError("edge-tts did not produce an MP3 file")

        try:
            # prefer reusing current loop if present in a worker
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None  # no loop

        if loop and loop.is_running():
            # run in a new temporary loop
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(_run())
            finally:
                new_loop.close()
        else:
            return asyncio.run(_run())


def os_replace(src: Path, dst: Path) -> None:
    # Cross-platform atomic replace
    import os

    os.replace(src, dst)


backend = EdgeBackend()
