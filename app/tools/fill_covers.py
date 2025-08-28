"""
Fill missing cover.png files for leaf item folders.

Definition:
- Leaf folder: contains no subdirectories.
- Eligible: leaf folder containing at least one .mp3 file.
- Action: if cover.png is missing or empty, download via watchers.job_utils.download_cover_image.

Usage:
  python -m tools.fill_covers [--base PATH] [--dry-run]

Defaults to scanning jobs/outgoing under the repo's app directory.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from watchers.job_utils import download_cover_image


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "jobs" / "outgoing"


def is_leaf_folder(folder: Path) -> bool:
    return any(folder.iterdir()) and not any(p.is_dir() for p in folder.iterdir())


def has_mp3(folder: Path) -> bool:
    return any(p.suffix.lower() == ".mp3" for p in folder.iterdir() if p.is_file())


def has_cover_png(folder: Path) -> bool:
    p = folder / "cover.png"
    return p.exists() and p.stat().st_size > 0


def scan_and_fill(base: Path, dry_run: bool = False) -> tuple[int, int]:
    created = 0
    skipped = 0
    if not base.exists():
        return (0, 0)

    # Walk directories breadth-first
    for dirpath, dirnames, filenames in os.walk(base):
        folder = Path(dirpath)
        # leaf: no subdirectories
        if dirnames:
            continue
        # eligible: contains at least one mp3
        if not any(fn.lower().endswith(".mp3") for fn in filenames):
            continue
        if has_cover_png(folder):
            skipped += 1
            continue
        if dry_run:
            print(f"[dry-run] would create cover.png in: {folder}")
            created += 1
            continue
        try:
            download_cover_image(folder)
            if has_cover_png(folder):
                print(f"[✓] cover.png created in: {folder}")
                created += 1
            else:
                # download may leave a cover.webp if conversion failed; treat as created
                if (folder / "cover.webp").exists():
                    print(f"[~] cover.webp created in: {folder} (conversion to PNG unavailable)")
                    created += 1
                else:
                    print(f"[x] failed to create cover in: {folder}")
        except Exception as e:
            print(f"[x] error creating cover in {folder}: {e}")
    return created, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description="Fill missing cover.png for leaf item folders")
    ap.add_argument("--base", type=Path, default=OUT_DIR, help="Base directory to scan (default: jobs/outgoing)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be created without downloading")
    args = ap.parse_args()

    created, skipped = scan_and_fill(args.base, args.dry_run)
    print(f"Done. Created: {created} | Existing covers skipped: {skipped}")


if __name__ == "__main__":
    main()

