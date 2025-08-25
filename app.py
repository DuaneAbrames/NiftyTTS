import re
import time
import uuid
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from string import Template

# --- Config ---
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
IN_DIR = JOBS_DIR / "incoming"
OUT_DIR = JOBS_DIR / "outgoing"
TIMEOUT_SECONDS = 120           # how long to wait for the .mp3 after submit
POLL_INTERVAL = 2.0
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
  button { padding: .7rem 1rem; border: 0; border-radius: .5rem; background: #111; color: #fff; cursor: pointer; width: fit-content;}
  .card { border: 1px solid #e5e5e5; border-radius: .75rem; padding: 1rem; margin-top: 1rem; }
  .muted { color: #666; }
  .ok { color: #0a7f2e; }
  .warn { color: #b35b00; }
  .row { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
  a.btn { display:inline-block; padding:.5rem .8rem; border:1px solid #111; border-radius:.5rem; text-decoration:none; }
  audio { width: 100%; margin-top: .5rem; }
  code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
</style>

$body
""")

def render(body: str) -> HTMLResponse:
    return HTMLResponse(HTML_TEMPLATE.substitute(body=body))

# --------- helpers ---------

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


def build_job(url: str, text: str) -> str:
    job_id = uuid.uuid4().hex[:16]
    safe_host = sanitize_filename(urlparse(url).netloc or "unknown")
    base_name = f"{safe_host}-{job_id}"
    text_path = IN_DIR / f"{base_name}.txt"
    meta_path = IN_DIR / f"{base_name}.json"
    text_path.write_text(text, encoding="utf-8")
    meta = {"url": url, "job_id": job_id, "created_ts": int(time.time()), "text_file": text_path.name}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return base_name

def mp3_path_for(base_name: str) -> Path:
    return OUT_DIR / f"{base_name}.mp3"

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


def strip_leading_email_headers(text: str) -> str:
    """
    Remove RFC822-style headers only from the very top of the text.
    Stops at the first blank line OR the first non 'Header: value' line.
    Only matches at column 0.
    """
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = t.split("\n")

    header_re = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*:\s.*$")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln == "":
            # consume the blank line separating headers from body
            i += 1
            break
        if header_re.match(ln):
            i += 1
            continue
        break

    return "\n".join(lines[i:]).lstrip("\n")


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

def form_step2(u: str, text: str) -> str:
    safe_u = (u or "").replace('"', "&quot;")
    # textarea contains sanitized text for HTML pages or raw text for text/plain
    return f"""
<h1>Review &amp; Edit Text</h1>
<p class="muted">We fetched and sanitized the page text. Make any edits (trim headers/footers, etc.), then submit to synthesize.</p>
<form method="post" action="/submit">
  <input type="hidden" name="u" value="{safe_u}">
  <label>
    <div><b>Step 2:</b> Edit the text below</div>
    <textarea name="text" required>{html_escape(text)}</textarea>
  </label>
  <div class="row">
    <button type="submit">Create Audio</button>
    <a class="btn" href="/">Start over</a>
  </div>
</form>
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

    # For text/plain only: strip top-of-file headers and unwrap soft-wrapped lines
    if kind == "text":
        text = strip_leading_email_headers(text)
        text = unwrap_email_wrapped(text)
        
    return render(form_step2(u, text))

@app.post("/submit", response_class=HTMLResponse)
async def submit(u: str = Form(...), text: str = Form(...)):
    """
    Step 3 (POST): write job from edited text, then wait briefly for MP3.
    """
    if not u:
        raise HTTPException(400, "Missing URL (u).")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXTAREA_BYTES:
        raise HTTPException(413, "Submitted text is too large.")

    base_name = build_job(u, text)
    out_mp3 = mp3_path_for(base_name)

    # Wait/poll for watcher to produce MP3
    start = time.time()
    while time.time() - start < TIMEOUT_SECONDS:
        if out_mp3.exists() and out_mp3.stat().st_size > 0:
            return render(job_ready_block(base_name, u))
        await _sleep(POLL_INTERVAL)

    # Timed out; show wait block with status link
    return render(job_wait_block(base_name))

@app.get("/status/{base_name}", response_class=HTMLResponse)
def status(base_name: str, request: Request):
    """
    Status page; if ready, show inline player + download link.
    """
    out_mp3 = mp3_path_for(base_name)
    # Try to pull original URL (best-effort)
    url = None
    for meta in IN_DIR.glob(f"{base_name}.json"):
        try:
            url = json.loads(meta.read_text(encoding="utf-8")).get("url")
        except Exception:
            pass
        break

    if out_mp3.exists() and out_mp3.stat().st_size > 0:
        # show audio player + links
        body = job_ready_block(base_name, url or "(unknown)")
    else:
        body = job_wait_block(base_name)
    return render(body)

@app.get("/download/{base_name}")
def download(base_name: str):
    out_mp3 = mp3_path_for(base_name)
    if not out_mp3.exists() or out_mp3.stat().st_size == 0:
        raise HTTPException(404, "MP3 not found yet.")
    return FileResponse(path=out_mp3, media_type="audio/mpeg", filename=out_mp3.name)
