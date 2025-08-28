import json
import os
import re
from datetime import datetime
import uuid
from html import escape as _html_escape
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
    """Write JSON metadata, apply ID3 tags, create folder OPF, and touch file mtime."""
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
    # Album: use provided meta or fallback to output folder name
    album = meta.get("album")
    if not album:
        try:
            album = mp3_path.parent.name
        except Exception:
            album = None
    if album:
        tags["album"] = album
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

    # Ensure an OPF file exists for this folder capturing series metadata
    try:
        _ensure_folder_opf(mp3_path.parent, meta | {"album": album or ""})
    except Exception:
        # OPF creation failures should not block audio output
        pass


def _xml(txt: str) -> str:
    return _html_escape(str(txt or ""), quote=True)


def _ensure_folder_opf(folder: Path, meta: dict) -> None:
    """Create a minimal OPF 2.0 file in the given folder if missing.

    The OPF captures high-level metadata parallel to our ID3 tags and uses the
    folder name as the series title. We intentionally only create the file if it
    does not already exist so manual edits are preserved.
    """
    if not folder or not isinstance(folder, Path):
        return

    opf_path = folder / "metadata.opf"
    if opf_path.exists():
        return

    series = (meta.get("album") or folder.name or "").strip()
    artist = (meta.get("from") or "").strip()
    date_str = (meta.get("date") or "").strip()
    # OPF 2.0 requires a language; default to English if unknown
    language = (meta.get("language") or "en").strip()

    book_id = f"urn:uuid:{uuid.uuid4()}"

    content = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<package version=\"2.0\" unique-identifier=\"BookId\" xmlns=\"http://www.idpf.org/2007/opf\">\n"
        "  <metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:opf=\"http://www.idpf.org/2007/opf\">\n"
        f"    <dc:identifier id=\"BookId\">{_xml(book_id)}</dc:identifier>\n"
        f"    <dc:title>{_xml(series)}</dc:title>\n"
        + (f"    <dc:creator opf:role=\"aut\">{_xml(artist)}</dc:creator>\n" if artist else "")
        + (f"    <dc:date>{_xml(date_str)}</dc:date>\n" if date_str else "")
        + f"    <dc:language>{_xml(language)}</dc:language>\n"
        + f"    <meta name=\"calibre:series\" content=\"{_xml(series)}\"/>\n"
        + "  </metadata>\n"
        + "  <manifest/>\n"
        + "  <spine toc=\"ncx\"/>\n"
        + "</package>\n"
    )
    try:
        opf_path.write_text(content, encoding="utf-8")
    except Exception:
        # swallow errors; OPF is optional metadata
        pass
