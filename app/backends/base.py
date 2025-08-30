from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List


class TTSBackend(ABC):
    """Backend interface for TTS engines."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable identifier (e.g., 'edge', 'pyttsx3')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-friendly name (e.g., 'Microsoft Edge TTS')."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if dependencies/config are available to synthesize."""

    @abstractmethod
    def list_voices(self) -> List[Dict[str, Any]]:
        """Return a list of voices as dictionaries with fields like name/locale/gender.

        Keep fields best-effort and engine-specific. At minimum include 'name'.
        """

    @abstractmethod
    def synthesize_to_mp3(self, text: str, out_mp3: Path, meta: Dict[str, Any]) -> int:
        """Synthesize text to MP3 file path. Return the number of bytes written.

        Implementations should write to a temporary file and atomically move
        to `out_mp3`.
        """

