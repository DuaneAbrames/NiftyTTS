import re
import time
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from email.parser import Parser
from email.utils import parseaddr, parsedate_to_datetime
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from string import Template

# --- Config ---
BACKEND = os.environ.get("BACKEND", "edge")
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
IN_DIR = JOBS_DIR / "incoming"
OUT_DIR = JOBS_DIR / "outgoing"
TIMEOUT_SECONDS = 120           # how long to wait for the .mp3 after submit
POLL_INTERVAL = 2.0
RECENT_SECONDS = 2 * 60 * 60    # window for recent jobs on status page
MAX_DOWNLOAD_BYTES = 5_000_000  # cap the fetched page size (bytes)
MAX_TEXTAREA_BYTES = 2_000_000  # server-side guard on large pasted text

IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="NiftyTTS – Simple 2-Step")

SAFE_SCHEMES = {"http", "https"}

HTML_TEMPLATE = Template(r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>NiftyTTS – URL → MP3</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; max-width: 980px; }
  form { display: grid; gap: .75rem; }
  input[type=url], textarea { width: 100%; padding: .6rem .8rem; border: 1px solid #ccc; border-radius: .5rem; }
  textarea { min-height: 420px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  button { padding: .7rem 1rem; border: 0; border-radius: .5rem; background: #111; color: #fff; cursor: pointer; width: fit-content; }
  .card { border: 1px solid #e5e5e5; border-radius: .75rem; padding: 1rem; margin-top: 1rem; }
  .muted { color: #666; }
  .ok { color: #0a7f2e; }
  .warn { color: #b35b00; }
  .row { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
  a.btn { display:inline-block; padding:.5rem .8rem; border:1px solid #111; border-radius:.5rem; text-decoration:none; }
  audio { width: 100%; margin-top: .5rem; }
  code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

  /* backend badge */
  .hdr { display:flex; align-items:center; justify-content:space-between; gap:1rem; margin-bottom: .75rem; }
  .badge { font-size: .85rem; padding: .2rem .5rem; border: 1px solid currentColor; border-radius: .5rem; white-space: nowrap; }
</style>

<div class="hdr">
  <h1 style="margin:0;">NiftyTTS – URL → Text → MP3</h1>
  <div class="badge">Backend: $backend</div>
</div>

$body
""")

def render(body: str) -> HTMLResponse:
    return HTMLResponse(HTML_TEMPLATE.substitute(body=body, backend=BACKEND))


# --------- helpers ---------

def _meta_for(base_name: str) -> dict:
    meta_path = IN_DIR / f"{base_name}.json"
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").strip().title()

def _sanitize_segment(name: str) -> str:
    """Filesystem-safe human-readable segment (for folders).

    Replaces Windows-invalid chars, collapses whitespace, trims trailing dots.
    """
    name = str(name or "").strip()
    # Replace Windows-invalid filename characters
    name = re.sub(r'[\\/<>:"|?*]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Untitled"

def output_relpath_from_url(url: str) -> Path:
    """Legacy helper: single-level <Folder>/<Title[ XXX]>.mp3 derived from URL.

    Retained for compatibility. New jobs use output_relpath_for() which includes
    Author/Series/Item folder structure.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    file_part = parts[-1] if parts else parsed.netloc
    folder_slug = parts[-2] if len(parts) >= 2 else file_part
    if "." in file_part:
        file_part = file_part.rsplit(".", 1)[0]
    m = re.match(r"^(.*?)(?:-(\d+))?$", file_part)
    if m:
        base_slug = m.group(1)
        digits = m.group(2)
    else:
        base_slug = file_part
        digits = None
    title = _slug_to_title(base_slug)
    folder = _slug_to_title(folder_slug)
    fname = f"{title} {int(digits):03d}.mp3" if digits else f"{title}.mp3"
    return Path(folder) / fname

def output_relpath_for(url: str, headers: dict[str, str] | None) -> tuple[Path, dict]:
    """Compute Author/Series/NNN - Title/Title.mp3 and return (relpath, extra_meta).

    - Author comes from headers["from"] (name part), else "Unknown".
    - Series derives from the URL folder slug (if any).
    - Track number derives from trailing -NNN in the URL's last segment (if any).
    - Title prefers headers["subject"], else title from URL slug.
    - MP3 filename is Title.mp3 inside the item folder.
    """
    headers = headers or {}
    parsed = urlparse(url)
    # Keep all parts for depth calc; also prepare a non-empty list for convenience
    all_parts = parsed.path.strip("/").split("/") if parsed.path else []
    nonempty_parts = [p for p in all_parts if p]
    file_part = nonempty_parts[-1] if nonempty_parts else parsed.netloc
    folder_slug = nonempty_parts[-2] if len(nonempty_parts) >= 2 else ""
    if "." in file_part:
        file_part = file_part.rsplit(".", 1)[0]

    m = re.match(r"^(.*?)(?:-(\d+))?$", file_part)
    if m:
        base_slug = m.group(1)
        digits = m.group(2)
    else:
        base_slug = file_part
        digits = None

    # Derive fields
    series_title = _slug_to_title(folder_slug) if folder_slug else ""
    url_title = _slug_to_title(base_slug)

    raw_from = headers.get("from", "").strip()
    author_name = parseaddr(raw_from)[0].strip() if raw_from else ""
    author_dir = _sanitize_segment(author_name or "Unknown")

    raw_subject = headers.get("subject", "").strip()
    title = raw_subject or url_title
    title_clean = _sanitize_segment(title)

    track_num = int(digits) if digits else None
    item_folder = f"{track_num:03d} - {title_clean}" if track_num else title_clean

    series_dir = _sanitize_segment(series_title) if series_title else None

    # MP3 filename (do not repeat track since folder has it)
    mp3_name = f"{title_clean}.mp3"

    # Special-case Nifty depth semantics:
    # - Series URLs: /nifty/SECTION/SUBSECTION/SERIES/Story.html → 4 non-empty folders
    # - Non-series URLs: /nifty/SECTION/SUBSECTION//Story.html → 3 non-empty folders
    host = (parsed.netloc or "").lower()
    host = host[4:] if host.startswith("www.") else host
    pre_file_depth = max(0, len(nonempty_parts) - 1)  # count folders before file
    is_nifty = host.endswith("nifty.org") and (nonempty_parts[:1] == ["nifty"])  # path starts with /nifty
    is_nifty_non_series = is_nifty and pre_file_depth == 3

    if is_nifty_non_series:
        # Non-series story: create an item folder under the author.
        # Desired: Author/Title/Title.mp3 (no series directory)
        rel = Path(author_dir) / item_folder / mp3_name
        extra_meta = {}
        # Do not set album/series for non-series works
        # Track is also omitted (rare/non-sensical in this case)
        return rel, extra_meta

    # Default/series path: Author/Series/Item/Title.mp3 (or Author/Item/Title.mp3 if no series)
    segments = [author_dir]
    if series_dir:
        segments.append(series_dir)
    segments.append(item_folder)
    rel = Path(*segments) / mp3_name

    extra_meta = {}
    if series_title:
        extra_meta["album"] = series_title
    if track_num is not None:
        extra_meta["track"] = track_num
    return rel, extra_meta


def mp3_path_for(base_name: str) -> Path:
    meta = _meta_for(base_name)
    rel = meta.get("output_rel")
    if rel:
        p = OUT_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return OUT_DIR / f"{base_name}.mp3"


def err_path_for(base_name: str) -> Path:
    return mp3_path_for(base_name).with_suffix(".err.txt")

def read_error_text(err_path: Path, max_chars: int = 8000) -> str:
    try:
        raw = err_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(Failed to read error file: {e})"
    # trim very large errors
    return raw[:max_chars] + ("…\n(truncated)" if len(raw) > max_chars else "")

def job_error_block(base_name: str, url: str | None, err_text: str) -> str:
    safe_url = html_escape(url or "(unknown)")
    dl = f"/error/{base_name}"
    return f"""
<div class="card">
  <p class="warn"><b>There was an error while creating your audio.</b></p>
  <p class="muted">Source: <span class="mono">{safe_url}</span></p>
  <p><a class="btn" href="{dl}">Download error log</a> <a class="btn" href="/">Start over</a></p>
  <details open>
    <summary>Error details</summary>
    <pre class="mono" style="white-space:pre-wrap; max-height: 50vh; overflow:auto;">{html_escape(err_text)}</pre>
  </details>
</div>
"""


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return name or "file"

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # crude removal of common nav/footer by role tags if present
    for tag in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "complementary"]}):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)

def extract_text_from_response(resp: httpx.Response) -> tuple[str, str]:
    """
    Returns (text, kind) where kind is 'text' for text/plain and 'html' for text/html.
    """
    ctype = (resp.headers.get("content-type") or "").lower()
    body = resp.content

    if len(body) > MAX_DOWNLOAD_BYTES:
        raise HTTPException(413, f"Downloaded content exceeds {MAX_DOWNLOAD_BYTES/1_000_000:.1f} MB limit")

    # Plain text
    if "text/plain" in ctype:
        return body.decode(resp.encoding or "utf-8", errors="replace"), "text"

    # HTML
    if (
        "text/html" in ctype
        or body.strip().lower().startswith(b"<!doctype html")
        or b"<html" in body[:4096].lower()
    ):
        decoded = body.decode(resp.encoding or "utf-8", errors="replace")
        return html_to_text(decoded), "html"

    raise HTTPException(415, "Unsupported content-type. Please supply a text/plain or HTML page.")

def build_job(url: str, text: str, headers: dict[str, str] | None = None) -> str:
    """
    Create a job basename from the URL and current timestamp. Optionally embed
    email-style headers (From/Subject/Date) into the job text and JSON meta.

    Example:
      http://www.xxx.com/foo/bar/baz/A totally effed-up story.html
      -> xxx-com-foo-bar-baz-a totally effed-up story (08-25-25@18-31)
    """
    parsed = urlparse(url)

    # 1. domain without scheme or "www."
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.replace(".", "-")

    # 2. path components
    path = parsed.path.strip("/")
    parts = []
    if path:
        parts = path.split("/")

        # drop extension from the last segment
        last = parts[-1]
        if "." in last:
            last = last.rsplit(".", 1)[0]
        parts[-1] = last

    # 3. combine host + path into a single string
    combined = "-".join([host] + parts) if parts else host

    # 4. lowercase
    combined = combined.lower()

    # 5. add timestamp
    timestamp = datetime.now().strftime("(%m-%d-%y@%H-%M)")
    base_name = f"{combined} {timestamp}"

    # 6. sanitize (remove bad filesystem chars)
    base_name = re.sub(r"[^a-z0-9 _\-().@]", "_", base_name)

    # Write files
    text_path = IN_DIR / f"{base_name}.txt"
    meta_path = IN_DIR / f"{base_name}.json"

    body = text
    meta = {
        "url": url,
        "created_ts": int(time.time()),
        "text_file": text_path.name,
    }

    if headers:
        hdr_lines: list[str] = []
        from_raw = headers.get("from", "").strip()
        subj_raw = headers.get("subject", "").strip()
        date_raw = headers.get("date", "").strip()
        if from_raw:
            hdr_lines.append(f"From: {from_raw}")
            from_name = parseaddr(from_raw)[0].strip()
            if from_name:
                meta["from"] = from_name
        if subj_raw:
            hdr_lines.append(f"Subject: {subj_raw}")
            meta["subject"] = subj_raw
        if date_raw:
            hdr_lines.append(f"Date: {date_raw}")
            try:
                meta["date"] = parsedate_to_datetime(date_raw).isoformat()
            except Exception:
                meta["date"] = date_raw
        if hdr_lines:
            body = "\n".join(hdr_lines) + "\n\n" + body

    # Compute output relative path using available headers
    rel, extra = output_relpath_for(url, headers or {})
    meta["output_rel"] = rel.as_posix()
    # Enrich meta for downstream tagging
    meta.update(extra)

    text_path.write_text(body, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return base_name

async def _sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)

def unwrap_email_wrapped(text: str) -> str:
    """
    Convert 'email-style' soft-wrapped paragraphs to single lines:
      - Keep blank lines as paragraph boundaries
      - Join single newlines inside a paragraph with spaces
      - Handle hyphen wraps at line-end: 'fo-' + 'obar' -> 'foobar'
      - Preserve list items (-, *, •, '1.') as their own lines
    """
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = re.split(r"\n\s*\n+", t)  # split on 1+ blank lines

    bullet_re = re.compile(r"^(\s*([*\-\u2022]|\d+\.)\s+)")
    out_paras: list[str] = []

    for p in paras:
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if not lines:
            out_paras.append("")
            continue

        rebuilt: list[str] = []
        cur = ""

        for ln in lines:
            # keep list items intact on their own line
            if bullet_re.match(ln):
                if cur:
                    rebuilt.append(cur)
                    cur = ""
                rebuilt.append(ln)
                continue

            if not cur:
                cur = ln
                continue

            # if previous ends with a hyphen (likely wrap), join without space
            if cur.endswith("-") and not re.search(r"\w-\w$", cur):
                cur = cur[:-1] + ln.lstrip()
            else:
                cur = cur + " " + ln.lstrip()

        if cur:
            rebuilt.append(cur)

        out_paras.append("\n".join(rebuilt))

    return "\n\n".join(out_paras).strip()


def strip_leading_email_headers(text: str) -> tuple[str, dict[str, str]]:
    """Split off RFC822-style headers from the very top of ``text``.

    Returns a tuple of ``(body, headers)`` where ``headers`` is a mapping of
    header names to their raw values. Only consecutive ``Header: value`` lines
    starting at column 0 are considered part of the header block. Any folded
    continuation lines are included.
    """
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = t.split("\n")

    header_re = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*:\s.*$")
    hdr_lines: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln == "":
            i += 1
            break
        if header_re.match(ln):
            hdr_lines.append(ln)
            i += 1
            # include folded continuation lines
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                hdr_lines.append(lines[i])
                i += 1
            continue
        break

    header_blob = "\n".join(hdr_lines)
    parsed = Parser().parsestr(header_blob)
    headers = {
        "from": parsed.get("From", "").strip(),
        "subject": parsed.get("Subject", "").strip(),
        "date": parsed.get("Date", "").strip(),
    }

    body = "\n".join(lines[i:]).lstrip("\n")
    return body, headers


# --------- UI fragments ---------

def form_step1(prefill: str = "", message: str = "") -> str:
    msg = f'<p class="muted">{message}</p>' if message else '<p class="muted">Paste a public URL to a <b>text/plain</b> or <b>HTML</b> page.</p>'
    return f"""
<h1>NiftyTTS – URL → Text → MP3</h1>
{msg}
<form method="get" action="/">
  <label>
    <div><b>Step 1:</b> Paste URL</div>
    <input type="url" name="u" placeholder="https://example.com/chapter-1" value="{prefill}" required />
  </label>
  <button type="submit">Fetch &amp; Edit Text</button>
</form>
"""

def form_step2(u: str, text: str, meta: dict | None = None) -> str:
    safe_u = (u or "").replace('"', "&quot;")
    meta = meta or {}
    hidden = []
    for key in ("from", "subject", "date"):
        val = meta.get(key, "")
        if val:
            hidden.append(f'<input type="hidden" name="{key}" value="{html_escape(val)}">')
    hidden_inputs = "\n  ".join(hidden)
    # textarea contains sanitized text for HTML pages or raw text for text/plain
    return f"""
<h1>Review &amp; Edit Text</h1>
<p class="muted">We fetched and sanitized the page text. Make any edits (trim headers/footers, etc.), then submit to synthesize.</p>
<form method="post" action="/submit">
  <input type="hidden" name="u" value="{safe_u}">
  {hidden_inputs}
  <label>
    <div><b>Step 2:</b> Edit the text below</div>
    <textarea id="text" name="text" required>{html_escape(text)}</textarea>
  </label>
  <div class="row">
    <button type="button" id="scroll-top">Scroll to Top</button>
    <button type="button" id="scroll-bottom">Scroll to Bottom</button>
  </div>
  <div class="row">
    <button type="submit">Create Audio</button>
    <a class="btn" href="/">Start over</a>
  </div>
</form>
<script>
  const ta = document.getElementById('text');
  document.getElementById('scroll-top').addEventListener('click', () => ta.scrollTop = 0);
  document.getElementById('scroll-bottom').addEventListener('click', () => ta.scrollTop = ta.scrollHeight);
</script>
"""

def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))

def job_ready_block(base_name: str, url: str) -> str:
    src = f"/download/{base_name}"
    fname = mp3_path_for(base_name).name
    return f"""
<div class="card">
  <p class="ok">Your MP3 is ready.</p>
  <audio controls src="{src}" preload="metadata"></audio>
  <p class="row">
    <a class="btn" href="{src}">Download MP3</a>
    <a class="btn" href="/">Convert another</a>
  </p>
  <p class="muted">Source: <span class="mono">{html_escape(url)}</span> &middot; File: <span class="mono">{fname}</span></p>
</div>
"""

def job_wait_block(base_name: str) -> str:
    return f"""
<div class="card">
  <p class="warn">Still processing…</p>
  <div class="row">
    <a class="btn" href="/status/{base_name}">Check status</a>
    <a class="btn" href="/">Start another</a>
  </div>
  <p class="muted">When ready, your audio will be playable on this page and downloadable from <code>/download/{base_name}</code>.</p>
</div>
"""

# --------- routes ---------

@app.get("/", response_class=HTMLResponse)
async def index(u: str | None = Query(default=None, description="URL to convert")):
    """
    Step 1 (GET without ?u=): show URL form.
    Step 2 (GET with ?u=): fetch URL, sanitize text, show textarea to edit.
    """
    if not u:
        return render(form_step1())

    # Validate URL scheme
    try:
        parsed = urlparse(u)
    except Exception:
        raise HTTPException(400, "Invalid URL.")
    if parsed.scheme not in SAFE_SCHEMES:
        raise HTTPException(400, "Only http(s) URLs are allowed.")

    # Fetch the page and extract text
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(u, headers={"User-Agent": "NiftyTTS/0.2 (+personal-use)"})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        # Return the form again with a message
        return render(form_step1(prefill=u, message=f"Fetch failed: {html_escape(str(e))}"))

    text, kind = extract_text_from_response(resp)

    meta: dict[str, str] = {}

    # For text/plain only: strip top-of-file headers and unwrap soft-wrapped lines
    if kind == "text":
        text, meta = strip_leading_email_headers(text)
        text = unwrap_email_wrapped(text)

    return render(form_step2(u, text, meta))

@app.post("/submit", response_class=HTMLResponse)
async def submit(
    u: str = Form(...),
    text: str = Form(...),
    from_hdr: str = Form("", alias="from"),
    subject_hdr: str = Form("", alias="subject"),
    date_hdr: str = Form("", alias="date"),
):
    headers = {"from": from_hdr, "subject": subject_hdr, "date": date_hdr}
    base_name = build_job(u, text, headers)
    # Immediately redirect to the status page (list view) and highlight this job
    return RedirectResponse(url=f"/status?focus={base_name}", status_code=303)

def _fmt_ts(ts: float | int | None) -> str:
    try:
        if ts is None:
            return ""
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

def _human_bytes(n: float | int) -> str:
    try:
        val = float(n)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0:
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"

def _read_text_headers(text_path: Path) -> dict[str, str]:
    try:
        raw = text_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    body, headers = strip_leading_email_headers(raw)
    return headers or {}

def _recent_jobs(now: float, focus: str | None = None) -> tuple[list[str], str]:
    rows: list[str] = []
    cutoff = now - RECENT_SECONDS
    # Gather candidate bases from incoming JSON meta files
    items: list[tuple[str, dict]] = []
    for meta_path in sorted(IN_DIR.glob("*.json")):
        base = meta_path.stem
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        created_ts = data.get("created_ts")
        # Fallback: use file mtime if missing
        if not created_ts:
            try:
                created_ts = int(meta_path.stat().st_mtime)
            except Exception:
                created_ts = int(now)
        if created_ts < cutoff:
            continue
        items.append((base, data))

    # Sort newest first by created_ts
    def _sort_key(pair: tuple[str, dict]):
        ts = pair[1].get("created_ts") or 0
        return ts
    items.sort(key=_sort_key, reverse=True)

    for base, data in items:
        anchor = f" id=\"job-{html_escape(base)}\"" if focus == base else ""
        out_path = mp3_path_for(base)
        err_path = err_path_for(base)
        tmp_path = out_path.with_name(out_path.name + ".tmp")

        # Determine status
        status_txt = "queued"
        extra = ""
        completed_ts: float | None = None
        if err_path.exists() and err_path.stat().st_size > 0:
            status_txt = "error"
            extra = f"<a href=\"/error/{html_escape(base)}\">error log</a>"
        elif out_path.exists() and out_path.stat().st_size > 0:
            status_txt = "ready"
            completed_ts = out_path.stat().st_mtime
            extra = (
                f"<a class=\"btn\" href=\"/download/{html_escape(base)}\">Download</a>"
                f"<audio controls src=\"/download/{html_escape(base)}\" preload=\"metadata\"></audio>"
            )
        elif tmp_path.exists():
            status_txt = "running"
            try:
                sz = tmp_path.stat().st_size
                extra = f"tmp: {_human_bytes(sz)}"
            except Exception:
                extra = "tmp: (size unavailable)"

        # Display fields
        created_ts = data.get("created_ts")
        url = data.get("url", "")
        # Series from output_rel grandparent (Author/Series/Item/Title.mp3)
        output_rel = data.get("output_rel", "")
        if output_rel:
            p = Path(output_rel)
            # Only treat as series when path depth is >= 4: Author/Series/Item/Title.mp3
            parts = p.parts
            series = html_escape(parts[-3]) if len(parts) >= 4 else ""
        else:
            series = ""

        # If started (running/ready/error), attempt to show author/title/series
        started = status_txt != "queued"
        title_block = ""
        if started:
            # Prefer finalized meta if available
            meta_json = out_path.with_suffix(".json")
            author = ""
            title = ""
            album = series
            if meta_json.exists():
                try:
                    fin = json.loads(meta_json.read_text(encoding="utf-8"))
                    author = fin.get("from", "") or ""
                    title = fin.get("subject", "") or ""
                    album = fin.get("album", album) or album
                except Exception:
                    pass
            if not author and not title:
                # Fallback to parsing headers from incoming text
                text_path = IN_DIR / f"{base}.txt"
                headers = _read_text_headers(text_path)
                author = headers.get("from", "")
                title = headers.get("subject", "")
            parts = []
            if author:
                parts.append(html_escape(author))
            if title:
                parts.append(html_escape(title))
            if album:
                parts.append(f"[{html_escape(album)}]")
            title_block = " 00 ".join(p for p in parts if p)
            if not title_block:
                title_block = html_escape(Path(output_rel).name or base)
        else:
            # queued: last directory and filename from the URL
            try:
                parsed = urlparse(url)
                segs = [s for s in parsed.path.split("/") if s]
                last_dir = segs[-2] if len(segs) >= 2 else (segs[0] if segs else parsed.netloc)
                fname = segs[-1] if segs else parsed.netloc
                title_block = f"{html_escape(last_dir)}/{html_escape(fname)}"
            except Exception:
                title_block = html_escape(base)

        created_str = _fmt_ts(created_ts)
        completed_str = _fmt_ts(completed_ts) if completed_ts else ""
        when = f"<span class=\"muted\">submitted {created_str}</span>"
        if completed_str:
            when += f" 0 0 <span class=\"muted\">completed {completed_str}</span>"

        rows.append(
            f"<div class=\"card\"{anchor}>"
            f"  <div class=\"row\"><b>{title_block}</b></div>"
            f"  <div class=\"row\">Status: <span class=\"mono\">{html_escape(status_txt)}</span> {extra}</div>"
            f"  <div class=\"row\">{when}</div>"
            f"</div>"
        )

    if not rows:
        return [], "<p class=\"muted\">No recent jobs in the last 2 hours.</p>"
    return rows, ""

@app.get("/status", response_class=HTMLResponse)
def status_list(request: Request, focus: str | None = None):
    now = time.time()
    rows, empty_msg = _recent_jobs(now, focus)
    if empty_msg:
        body = (
            "<h1>Recent Jobs</h1>" +
            empty_msg +
            "<p><a class=\"btn\" href=\"/\">Convert another</a></p>"
        )
        return render(body)

    body = ["<h1>Recent Jobs</h1>"]
    if focus:
        body.append(f"<p class=\"muted\">Jumped to job <code>{html_escape(focus)}</code></p>")
    body.extend(rows)
    body.append("<p><a class=\"btn\" href=\"/\">Convert another</a></p>")
    return render("\n".join(body))

@app.get("/status/{base_name}", response_class=HTMLResponse)
def status_compat(base_name: str):
    # Backward-compatible: redirect to list view highlighting this job
    return RedirectResponse(url=f"/status?focus={base_name}", status_code=307)
    
@app.get("/error/{base_name}")
def download_error(base_name: str):
    err_path = err_path_for(base_name)
    if not err_path.exists() or err_path.stat().st_size == 0:
        raise HTTPException(404, "No error log found.")
    return FileResponse(path=err_path, media_type="text/plain", filename=err_path.name)

@app.get("/download/{base_name}")
def download(base_name: str):
    out_mp3 = mp3_path_for(base_name)
    if not out_mp3.exists() or out_mp3.stat().st_size == 0:
        raise HTTPException(404, "MP3 not found yet.")
    return FileResponse(path=out_mp3, media_type="audio/mpeg", filename=out_mp3.name)
