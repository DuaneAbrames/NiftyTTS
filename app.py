import os
import re
import time
import uuid
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# --- Config (tweak as you like) ---
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
IN_DIR = JOBS_DIR / "incoming"
OUT_DIR = JOBS_DIR / "outgoing"
TIMEOUT_SECONDS = 120          # how long the request will wait for the .mp3
POLL_INTERVAL = 2.0            # how often to check for the .mp3
MAX_DOWNLOAD_BYTES = 5_000_000 # 5 MB safety cap; increase if needed

IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="One-Page URL→TTS Jobber")

SAFE_SCHEMES = {"http", "https"}

HTML_TEMPLATE = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>URL → MP3 (TTS) – Simple</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; max-width: 820px; }
  form { display: flex; gap: .5rem; }
  input[type=url] { flex: 1; padding: .6rem .8rem; border: 1px solid #ccc; border-radius: .5rem; }
  button { padding: .6rem 1rem; border: 0; border-radius: .5rem; background: #111; color: #fff; cursor: pointer; }
  .card { border: 1px solid #e5e5e5; border-radius: .75rem; padding: 1rem; margin-top: 1rem; }
  .muted { color: #666; }
  .ok { color: #0a7f2e; }
  .warn { color: #b35b00; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .row { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
  a.btn { display:inline-block; padding:.5rem .8rem; border:1px solid #111; border-radius:.5rem; text-decoration:none; }
</style>
<h1>URL → MP3 (Text-to-Speech)</h1>
<p class="muted">Paste a public URL pointing to a <b>text/plain</b> or <b>HTML</b> page. We'll fetch it, save a job, and (if ready) give you an MP3.</p>
<form method="get" action="/">
  <input type="url" name="u" placeholder="https://example.com/chapter-1" value="{prefill}" required />
  <button type="submit">Convert</button>
</form>

{content}
"""

def page(content: str = "", prefill: str = "") -> HTMLResponse:
    return HTMLResponse(HTML_TEMPLATE.format(content=content, prefill=prefill))

def sanitize_filename(name: str) -> str:
    # conservative, filesystem-safe
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return name or "file"

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts/styles/nav/footer for a cheap first pass
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]  # drop empties
    return "\n".join(lines)

def extract_text_from_response(resp: httpx.Response) -> str:
    ctype = resp.headers.get("content-type", "")
    body = resp.content

    if len(body) > MAX_DOWNLOAD_BYTES:
        raise HTTPException(413, f"Downloaded content exceeds {MAX_DOWNLOAD_BYTES/1_000_000:.1f} MB limit")

    # Handle plain text
    if "text/plain" in ctype:
        return body.decode(resp.encoding or "utf-8", errors="replace")

    # Handle HTML
    if "text/html" in ctype or body.strip().startswith(b"<!DOCTYPE html") or b"<html" in body[:2048].lower():
        decoded = body.decode(resp.encoding or "utf-8", errors="replace")
        return html_to_text(decoded)

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
    return base_name  # used for both incoming and outgoing naming

def mp3_path_for(base_name: str) -> Path:
    return OUT_DIR / f"{base_name}.mp3"

def job_status_block(base_name: str, url: str) -> str:
    return f"""
<div class="card">
  <p>Job created for <span class="mono">{url}</span>.</p>
  <p class="muted">We are waiting for <span class="mono">{base_name}.mp3</span> to appear in <span class="mono">jobs/outgoing/</span>.</p>
  <div class="row">
    <a class="btn" href="/status/{base_name}">Check status</a>
    <a class="btn" href="/">Start another</a>
  </div>
</div>
"""

@app.get("/", response_class=HTMLResponse)
async def index(u: str | None = Query(default=None, description="URL to convert")):
    # No URL yet — show the form
    if not u:
        return page()

    # Validate scheme
    try:
        parsed = urlparse(u)
    except Exception:
        raise HTTPException(400, "Invalid URL.")
    if parsed.scheme not in SAFE_SCHEMES:
        raise HTTPException(400, "Only http(s) URLs are allowed.")

    # Fetch the page
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(u, headers={"User-Agent": "TTS-Proxy/0.1 (+personal-use)"})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Failed to fetch URL: {e}")

    # Extract plain text from the response
    text = extract_text_from_response(resp)

    # Write incoming job (.txt + .json)
    base_name = build_job(u, text)
    out_mp3 = mp3_path_for(base_name)

    # Poll for the outgoing MP3
    start = time.time()
    while time.time() - start < TIMEOUT_SECONDS:
        if out_mp3.exists() and out_mp3.stat().st_size > 0:
            # Done — render success page with download link
            content = f"""
<div class="card">
  <p class="ok">Your MP3 is ready.</p>
  <p><a class="btn" href="/download/{base_name}">Download MP3</a></p>
  <p class="muted">File: <span class="mono">{out_mp3.name}</span></p>
</div>
"""
            return page(content=content, prefill=u)
        await _sleep(POLL_INTERVAL)

    # Timeout — show status page prompt
    content = f"""
<div class="card">
  <p class="warn">Still processing…</p>
  <p>You can check back here, or use this status link:</p>
  <p><a class="btn" href="/status/{base_name}">/status/{base_name}</a></p>
  <p class="muted">When ready, your MP3 will be downloadable from <code>/download/{base_name}</code>.</p>
</div>
"""
    return page(content=content, prefill=u)

async def _sleep(seconds: float):
    # Local helper to avoid importing asyncio at top
    import asyncio
    await asyncio.sleep(seconds)

@app.get("/status/{base_name}", response_class=HTMLResponse)
def status(base_name: str):
    out_mp3 = mp3_path_for(base_name)
    if out_mp3.exists() and out_mp3.stat().st_size > 0:
        content = f"""
<div class="card">
  <p class="ok">Done!</p>
  <p><a class="btn" href="/download/{base_name}">Download MP3</a></p>
  <p class="muted">File: <span class="mono">{out_mp3.name}</span></p>
</div>
"""
    else:
        content = f"""
<div class="card">
  <p>Job <span class="mono">{base_name}</span> is not ready yet.</p>
  <div class="row">
    <a class="btn" href="/status/{base_name}">Refresh</a>
    <a class="btn" href="/">Start another</a>
  </div>
</div>
"""
    return page(content=content)

@app.get("/download/{base_name}")
def download(base_name: str):
    out_mp3 = mp3_path_for(base_name)
    if not out_mp3.exists() or out_mp3.stat().st_size == 0:
        raise HTTPException(404, "MP3 not found yet.")
    # Use a stable filename for the browser save dialog
    return FileResponse(path=out_mp3, media_type="audio/mpeg", filename=out_mp3.name)

# Optional: static mount if you ever want to expose folders (off by default)
# app.mount("/out", StaticFiles(directory=OUT_DIR), name="out")
