import json
import os
import re
from datetime import datetime
from email.parser import Parser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError


def extract_track_number(stem: str) -> int | None:
    """Return trailing integer from filename stem, if present."""
    m = re.search(r"(\d+)$", stem)
    return int(m.group(1)) if m else None


def parse_job_file(text_path: Path, base: str) -> tuple[dict, str]:
    """Parse SMTP style headers and body from a job text file.

    Returns metadata dict and body text (stripped).
    """
    raw = text_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    header_blob, body = ("", raw)
    if "\n\n" in raw:
        header_blob, body = raw.split("\n\n", 1)
    headers = Parser().parsestr(header_blob)
    artist = parseaddr(headers.get("From", ""))[0].strip()
    subject = headers.get("Subject", "").strip()
    date_str = headers.get("Date", "").strip()
    iso_date = ""
    if date_str:
        try:
            iso_date = parsedate_to_datetime(date_str).isoformat()
        except Exception:
            iso_date = ""
    track = extract_track_number(base)
    meta: dict[str, object] = {"from": artist, "subject": subject, "date": iso_date}
    if track is not None:
        meta["track"] = track
    return meta, body.strip()


def _ensure_id3(mp3_path: Path) -> None:
    try:
        ID3(mp3_path)
    except ID3NoHeaderError:
        ID3().save(mp3_path)
    except Exception:
        try:
            ID3().delete(mp3_path)
        except Exception:
            pass
        ID3().save(mp3_path)


def finalize_output(mp3_path: Path, meta: dict) -> None:
    """Write JSON metadata, apply ID3 tags, and touch file mtime."""
    json_path = mp3_path.with_suffix(".json")
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    _ensure_id3(mp3_path)
    tags = EasyID3(mp3_path)
    if meta.get("from"):
        tags["artist"] = meta["from"]
    if meta.get("subject"):
        tags["title"] = meta["subject"]
    if meta.get("track") is not None:
        tags["tracknumber"] = str(meta["track"])
    # Write a v2.3 tag and include a v1 tag for broader player compatibility
    tags.save(v1=2, v2_version=3)

    date_str = meta.get("date")
    if isinstance(date_str, str) and date_str:
        try:
            dt = datetime.fromisoformat(date_str)
            ts = dt.timestamp()
            os.utime(mp3_path, (ts, ts))
        except Exception:
            pass
