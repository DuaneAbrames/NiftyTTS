from __future__ import annotations

"""
Backend plugin registry for NiftyTTS.

Each backend implements the TTSBackend protocol defined in base.py. Backends
should be imported lazily and may mark themselves unavailable if dependencies
are missing.
"""

from typing import Dict, List

from .base import TTSBackend


_REGISTRY: Dict[str, TTSBackend] | None = None


def _load_backends() -> Dict[str, TTSBackend]:
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    backends: Dict[str, TTSBackend] = {}

    # Attempt to import known backends. Each module must expose `backend`.
    for mod_name in (
        "app.backends.edge",
        "app.backends.pyttsx3",
        "app.backends.piper",
    ):
        try:
            mod = __import__(mod_name, fromlist=["backend"])
            be = getattr(mod, "backend", None)
            if be and isinstance(be, TTSBackend):
                backends[be.backend_id] = be
        except Exception:
            # Silently ignore import errors; backend will not be listed
            pass

    _REGISTRY = backends
    return backends


def all_backends() -> List[TTSBackend]:
    """Return all registered backend instances (available or not)."""
    return list(_load_backends().values())


def available_backends() -> List[TTSBackend]:
    """Return only backends that report available()."""
    return [b for b in all_backends() if b.available()]


def get_backend(backend_id: str | None) -> TTSBackend | None:
    if not backend_id:
        return None
    return _load_backends().get(backend_id)
