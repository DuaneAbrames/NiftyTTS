import json
import os
import re
from datetime import datetime
import uuid
from html import escape as _html_escape
from email.parser import Parser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
import subprocess
import shutil
import sys

import httpx

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3
from mutagen.id3._frames import TXXX, COMM, TPUB
from mutagen.id3._util import ID3NoHeaderError


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
    """Apply ID3 tags, create folder OPF, and touch file mtime.

    We no longer emit a sidecar .json in the output directory to avoid
    confusing downstream apps. If one exists from a previous run, remove it.
    """
    json_path = mp3_path.with_suffix(".json")
    try:
        if json_path.exists():
            json_path.unlink()
    except Exception:
        # If deletion fails, continue without blocking audio output
        pass

    _ensure_id3(mp3_path)
    tags = EasyID3(mp3_path)
    # Basic tags
    if meta.get("from"):
        tags["artist"] = meta["from"]
    title_val = (meta.get("subject") or "")
    if title_val:
        tags["title"] = title_val
    if meta.get("track") is not None:
        tags["tracknumber"] = str(meta["track"])
    # Composer: backend + voice label if provided
    comp = (meta.get("composer") or "").strip()
    if comp:
        try:
            tags["composer"] = comp
        except Exception:
            pass
    # Album should be the story title (or filename stem if missing)
    album_title = title_val or mp3_path.stem
    if album_title:
        tags["album"] = album_title
    # Fixed metadata per request
    try:
        tags["genre"] = ["erotica"]
    except Exception:
        pass
    try:
        tags["publisher"] = ["nifty.org"]
    except Exception:
        pass
    # Write EasyID3 (v2.3 + ID3v1) first
    tags.save(v1=2, v2_version=3)

    # Now set extended frames not covered (series + long description/publisher fallback)
    try:
        id3 = ID3(mp3_path)

        # Series name stored in TXXX:series (prefer explicit meta['album'] as series).
        # Only fall back to folder-derived name for series layouts (when a track is present).
        series_name = (meta.get("series") or meta.get("album") or "")
        if not series_name and meta.get("track") is not None:
            try:
                # Author/Series/Item/Title.mp3 → take grandparent as series if present
                series_name = mp3_path.parent.parent.name
            except Exception:
                series_name = ""
        # Update existing TXXX:series if present; otherwise add
        updated_series = False
        for f in id3.getall("TXXX"):
            if isinstance(f, TXXX) and str(getattr(f, "desc", "")).lower() == "series":
                f.text = [series_name]
                updated_series = True
                break
        if series_name and not updated_series:
            id3.add(TXXX(encoding=3, desc="series", text=[series_name]))

        # Description/comment text with original URL if available
        url = (meta.get("url") or "").strip()
        desc_lines = [
            "This file was converted from a story posted to nifty.org, the original author retains all copyright, and this file may ONLY be used for personal use and not distributed in any way.",
        ]
        if url:
            desc_lines.append("")  # blank line
            desc_lines.append("")  # second blank line
            desc_lines.append(f"Original URL:  {url}")
        desc_text = "\n".join(desc_lines)
        # Update existing english description/comment if present; else add
        updated_comm = False
        for f in id3.getall("COMM"):
            if isinstance(f, COMM) and str(getattr(f, "lang", "eng")) == "eng" and str(getattr(f, "desc", "")).lower() in ("", "description", "desc"):
                f.text = [desc_text]
                # Normalize descriptor
                f.desc = "description"
                updated_comm = True
                break
        if not updated_comm:
            id3.add(COMM(encoding=3, lang="eng", desc="description", text=[desc_text]))

        # Ensure publisher present via TPUB if EasyID3 mapping wasn't available
        has_tpub = any(f.FrameID == "TPUB" for f in id3.values())
        if not has_tpub:
            id3.add(TPUB(encoding=3, text=["nifty.org"]))

        id3.save(v2_version=3)
    except Exception:
        # Do not block output on extended tag failures
        pass

    ts: float | None = None
    date_str = meta.get("date")
    if isinstance(date_str, str) and date_str:
        try:
            dt = datetime.fromisoformat(date_str)
            ts = dt.timestamp()
            os.utime(mp3_path, (ts, ts))
        except Exception:
            ts = None

    # Ensure an OPF file exists for this folder capturing series metadata
    try:
        # Pass series and ID3 title to OPF generator; prefer explicit series
        _ensure_folder_opf(
            mp3_path.parent,
            meta
            | {
                "series": (meta.get("series") or meta.get("album") or ""),
                "album": album_title or "",
                "title": title_val or album_title or "",
            },
        )
    except Exception:
        # OPF creation failures should not block audio output
        pass

    # If we have a timestamp, touch supporting files and containing folder
    try:
        if ts is not None:
            _touch_supporting_and_folder(mp3_path, ts)
    except Exception:
        pass

    # As the last step, normalize permissions/ownership for host access
    try:
        _fix_perms_and_ownership()
    except Exception:
        # Never block on permission adjustments
        pass


def _touch_supporting_and_folder(mp3_path: Path, ts: float) -> None:
    """Touch supporting files in the same folder and the folder itself.

    - Applies atime/mtime to all non-temporary files alongside the MP3
      (including the OPF, cover images, lyric files, etc.).
    - Skips obvious temporary artifacts like *.tmp and *.download.
    - Also touches the containing folder so its modified date matches.
    """
    folder = mp3_path.parent
    # Files (non-recursive)
    try:
        for p in folder.iterdir():
            try:
                if p.is_file():
                    name = p.name.lower()
                    if name.endswith(".tmp") or name.endswith(".download"):
                        continue
                    os.utime(p, (ts, ts))
            except Exception:
                # Continue touching others on individual errors
                pass
    except Exception:
        pass
    # Folder itself
    try:
        os.utime(folder, (ts, ts))
    except Exception:
        pass


def touch_folder_and_supporting_from_meta(mp3_path: Path, meta: dict) -> None:
    """Public helper to apply meta['date'] timestamp to folder and files.

    Parses meta['date'] as ISO-8601; on success, touches all supporting files
    and the containing folder to match that timestamp.
    """
    try:
        date_str = meta.get("date")
        if isinstance(date_str, str) and date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                ts = dt.timestamp()
                _touch_supporting_and_folder(mp3_path, ts)
            except Exception:
                pass
    except Exception:
        pass


def _is_webp(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(12)
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    except Exception:
        return False


def _convert_to_png(src_path: Path, dst_path: Path) -> bool:
    """Convert image at src_path to PNG at dst_path.

    Tries ffmpeg first (present in Docker image). If that fails, tries Pillow if
    available. Returns True on success, False otherwise.
    """
    # Try ffmpeg
    ffmpeg_path = os.environ.get("NIFTYTTS_FFMPEG_PATH", "ffmpeg")
    try:
        proc = subprocess.run([ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_path), str(dst_path)], capture_output=True, check=False, timeout=30)
        if proc.returncode == 0 and dst_path.exists() and dst_path.stat().st_size > 0:
            return True
    except Exception:
        pass

    # Try Pillow
    try:
        from PIL import Image  # type: ignore

        with Image.open(src_path) as im:
            im.save(dst_path, format="PNG")
        return dst_path.exists() and dst_path.stat().st_size > 0
    except Exception:
        return False


def download_cover_image(folder: Path) -> None:
    """Download a random cover image to folder/cover.png.

    - Uses nekos.best API as a simple image source.
    - Writes to cover.png and, if bytes are actually WebP, converts to true PNG.
    - Skips if a non-empty cover.png already exists.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        cover_path = folder / "cover.png"
        if cover_path.exists() and cover_path.stat().st_size > 0:
            return

        # Fetch a random image URL
        with httpx.Client(timeout=30.0) as client:
            r = client.get("https://nekos.best/api/v2/husbando")
            r.raise_for_status()
            data = r.json()
            url = data["results"][0]["url"]

            # Download with browser-like headers
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/115.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://nekos.best/",
            }

            tmp_path = cover_path.with_suffix(".download")
            with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        if chunk:
                            f.write(chunk)

        # If content is actually WebP (even if named .png), convert
        if _is_webp(tmp_path):
            conv_tmp = cover_path.with_suffix(".png.tmp")
            if _convert_to_png(tmp_path, conv_tmp):
                os.replace(conv_tmp, cover_path)
                tmp_path.unlink(missing_ok=True)
                return
            # Conversion failed; fall back to renaming .webp to make type explicit
            try:
                fallback = folder / "cover.webp"
                os.replace(tmp_path, fallback)
                return
            except Exception:
                pass

        # Not WebP: write as cover.png directly
        os.replace(tmp_path, cover_path)
    except Exception:
        # Do not fail the calling watcher due to cover issues
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

    # Metadata aligned with ID3 tag logic
    # Title should mirror ID3 title (subject in our pipeline)
    title = (meta.get("title") or meta.get("subject") or meta.get("album") or folder.name or "").strip()
    # Series: only include explicit series (no fallback to album/folder for non-series)
    series = (meta.get("series") or "").strip()
    # Author/creator
    artist = (meta.get("from") or "").strip()
    # Narrator maps from ID3 composer
    narrator = (meta.get("composer") or "").strip()
    # Date and language
    date_str = (meta.get("date") or "").strip()
    language = (meta.get("language") or "en").strip()
    # Genre/subject and publisher
    genre = (meta.get("genre") or "erotica").strip()
    publisher = (meta.get("publisher") or "nifty.org").strip()
    # Track index for series index
    series_index = str(meta.get("track")).strip() if meta.get("track") is not None else ""
    # Source URL (also included in ID3 description)
    url = (meta.get("url") or "").strip()

    # Description to mirror ID3 comment/description
    desc_lines = [
        "This file was converted from a story posted to nifty.org, the original author retains all copyright, and this file may ONLY be used for personal use and not distributed in any way.",
    ]
    if url:
        desc_lines.append("")
        desc_lines.append("")
        desc_lines.append(f"Original URL:  {url}")
    description = "\n".join(desc_lines)

    book_id = f"urn:uuid:{uuid.uuid4()}"

    content = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<package version=\"2.0\" unique-identifier=\"BookId\" xmlns=\"http://www.idpf.org/2007/opf\">\n"
        "  <metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:opf=\"http://www.idpf.org/2007/opf\">\n"
        f"    <dc:identifier id=\"BookId\">{_xml(book_id)}</dc:identifier>\n"
        f"    <dc:title>{_xml(title)}</dc:title>\n"
        + (f"    <dc:creator opf:role=\"aut\">{_xml(artist)}</dc:creator>\n" if artist else "")
        + (f"    <dc:contributor opf:role=\"nrt\">{_xml(narrator)}</dc:contributor>\n" if narrator else "")
        + (f"    <dc:publisher>{_xml(publisher)}</dc:publisher>\n" if publisher else "")
        + (f"    <dc:date>{_xml(date_str)}</dc:date>\n" if date_str else "")
        + f"    <dc:language>{_xml(language)}</dc:language>\n"
        + (f"    <dc:subject>{_xml(genre)}</dc:subject>\n" if genre else "")
        + (f"    <dc:description>{_xml(description)}</dc:description>\n" if description else "")
        + (f"    <dc:source>{_xml(url)}</dc:source>\n" if url else "")
        + (f"    <meta name=\"calibre:series\" content=\"{_xml(series)}\"/>\n" if series else "")
        + (f"    <meta name=\"calibre:series_index\" content=\"{_xml(series_index)}\"/>\n" if series and series_index else "")
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


def _fix_perms_and_ownership(root: Path | None = None) -> None:
    """Recursively chmod/chown within jobs/ for host access.

    - Sets dirs and files to mode 0o777.
    - If env `NIFTYTTS_UID/GID` are set (defaults 99/100), attempts to chown.
    - Silently ignores platforms that do not support chown (e.g., Windows).
    """
    try:
        # Default to the repository's jobs directory
        if root is None:
            root = Path(__file__).resolve().parents[1] / "jobs"
        if not root.exists():
            return

        mode = 0o777
        # Use numeric IDs; default mirrors entrypoint.sh
        uid_str = os.environ.get("NIFTYTTS_UID", "99")
        gid_str = os.environ.get("NIFTYTTS_GID", "100")
        try:
            uid = int(uid_str)
            gid = int(gid_str)
        except Exception:
            uid = -1
            gid = -1

        # Walk without following symlinks
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dpath = Path(dirpath)
            # Directories first
            try:
                os.chmod(dpath, mode)
            except Exception:
                pass
            if uid >= 0 and gid >= 0:
                try:
                    shutil.chown(dpath, user=uid, group=gid)
                except Exception:
                    pass
            # Files
            for name in dirnames + filenames:
                p = dpath / name
                try:
                    os.chmod(p, mode)
                except Exception:
                    pass
                if uid >= 0 and gid >= 0:
                    try:
                        shutil.chown(p, user=uid, group=gid)
                    except Exception:
                        pass
    except Exception:
        # Absolutely never block the pipeline on permission issues
        pass
