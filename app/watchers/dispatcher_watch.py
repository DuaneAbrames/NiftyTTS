from __future__ import annotations

"""
Generic watcher that monitors jobs/incoming and dispatches to the selected TTS
backend. Backends are implemented in app.backends and discovered via the
registry.
"""

import json
import os
import time
import traceback
from pathlib import Path
from typing import Tuple

from app.backends import available_backends, all_backends, get_backend
from .job_utils import (
    parse_job_file,
    finalize_output,
    download_cover_image,
    touch_folder_and_supporting_from_meta,
)


ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "jobs" / "incoming"
OUT_DIR = ROOT / "jobs" / "outgoing"

POLL_INTERVAL = float(os.environ.get("NIFTYTTS_POLL_INTERVAL", "0.5"))


def _ensure_dirs() -> None:
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for p in OUT_DIR.glob("*.mp3.tmp"):
        try:
            p.unlink()
        except Exception:
            pass


def _out_paths(base: str) -> Tuple[Path, Path]:
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
    err_file = out_mp3.with_suffix(".err.txt")
    return out_mp3, err_file


def _write_err(err_file: Path, base: str, msg: str, exc: BaseException | None = None, text_sample: str = "") -> None:
    blob = [f"ERROR: {msg}"]
    if exc:
        blob.append("\nTRACEBACK:\n" + "".join(traceback.format_exception(exc)))
    if text_sample:
        blob.append("\nTEXT SAMPLE (first 400 chars):\n" + text_sample[:400])
    err_file.write_text("\n\n".join(blob), encoding="utf-8")
    print(f"[x] {base}: {msg}. Details -> {err_file.name}")


def run() -> None:
    _ensure_dirs()

    # Default backend if a job does not specify one
    selected = os.environ.get("NIFTYTTS_BACKEND", os.environ.get("BACKEND", "edge")).strip()
    # Log discovered backends
    discovered = [(b.backend_id, b.display_name, b.available()) for b in all_backends()]
    if discovered:
        print("[watch] discovered backends:")
        for bid, name, ok in discovered:
            print(f"  - {bid:<8} {'(ok)' if ok else '(unavailable)'}  {name}")

    be = get_backend(selected)
    if be and be.available():
        print(f"[watch] Default backend: {be.backend_id} - {be.display_name}")
    else:
        print(f"[watch] Default backend '{selected}' not available; will require per-job backend selection.")
    print(f"[watch] jobs: {IN_DIR} -> {OUT_DIR}")

    seen: set[str] = set()
    while True:
        for txt_path in IN_DIR.glob("*.txt"):
            base = txt_path.stem
            out_mp3, err_file = _out_paths(base)

            # Skip if processed or errored already
            if base in seen or (out_mp3.exists() and out_mp3.stat().st_size > 0) or (err_file.exists() and err_file.stat().st_size > 0):
                continue
            seen.add(base)

            # Read and validate text
            raw = txt_path.read_text(encoding="utf-8", errors="replace")
            text = raw.strip()
            if not text:
                _write_err(err_file, base, "Empty text after preprocessing", None, raw)
                continue

            # Build meta from job file and enrich with JSON
            meta, body = parse_job_file(txt_path, base)
            try:
                j = IN_DIR / f"{base}.json"
                if j.exists():
                    data = json.loads(j.read_text(encoding="utf-8"))
                    for k in ("album", "track", "url", "backend", "voice"):
                        if k in data and k not in meta:
                            meta[k] = data[k]
            except Exception:
                pass

            # Pick backend for this job
            be_id = str(meta.get("backend") or selected or "").strip()
            be_for_job = get_backend(be_id) if be_id else None
            if not be_for_job or not be_for_job.available():
                _write_err(err_file, base, f"Requested backend '{be_id or '(none)'}' is not available", None, body)
                continue

            # Let backend hint a composer if desired
            try:
                if "composer" not in meta:
                    meta["composer"] = f"{be_for_job.display_name}"
            except Exception:
                pass

            start = time.time()
            try:
                print(f"[+] {base}: dispatching to {be_for_job.backend_id}")
                bytes_written = be_for_job.synthesize_to_mp3(body, out_mp3, meta)
                dur = time.time() - start
                print(f"[✓] {base}: wrote {out_mp3.name} ({bytes_written} bytes) in {dur:.1f}s")

                finalize_output(out_mp3, meta)
                try:
                    download_cover_image(out_mp3.parent)
                except Exception:
                    pass
                try:
                    touch_folder_and_supporting_from_meta(out_mp3, meta)
                except Exception:
                    pass
                if err_file.exists():
                    try:
                        err_file.unlink()
                        print(f"[-] {base}: cleared stale error log")
                    except Exception:
                        pass
            except Exception as e:
                _write_err(err_file, base, f"Exception during synthesis via {be_for_job.backend_id}", e, body)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
