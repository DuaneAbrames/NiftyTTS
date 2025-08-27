# NiftyTTS

NiftyTTS is designed for converting stories from nifty.org into MP3 audio files.
The project provides a small [FastAPI](https://fastapi.tiangolo.com/) web
application that fetches a URL, extracts text, and posts conversion jobs
into a `jobs` directory.  Background **watcher** processes pick up those
jobs and run a text‑to‑speech engine to produce MP3 files.

The repository includes helper scripts and a Docker image so the service
can run entirely locally or inside a container.

Note that this is designed to work with the 'old' nifty, not the new beta.

The tampermonkey script can be imported, and it will give you a "TTS" button
next to any link it thinks is a story (it's pretty stupid about what constitutes
a story).

Output folders will be based on the series name (or category name if the story is
not serial) 

```diff
- In no way does this project encourage anyone to violate the author's 
- copyright in their stories.  It may only be used to create audio files
- for the user's own **PERSONAL** use.  If you make files with this and
- distribute them on the internet, an angry skunk will appear in your 
- room and cause you to regret the fact of your birth!
```

## Architecture

* `app/app.py` – web interface that accepts a URL or pasted text and
  displays progress while the audio is generated.
* `app/watchers/` – backend workers for different TTS engines.  Each
  watcher monitors `jobs/incoming` and writes finished MP3s to
  `jobs/outgoing`.
* `entrypoint.sh` – convenience script that launches the web app and the
  selected watcher.  It is used as the Docker container entrypoint.

## Running

1. Install dependencies listed in `app/requirements.txt` and ensure
   `ffmpeg` is available.
2. Start the web app with `uvicorn`:

   ```bash
   uvicorn app:app --port 7230
   ```

3. In another terminal, run one of the watcher scripts from
   `app/watchers/`.  The watcher converts jobs into MP3 files.

The `entrypoint.sh` script automates these steps and is used by the
provided Dockerfile.  Building the image and running it with the default
settings uses the hosted Microsoft Edge TTS service:

```bash
docker build -t niftytts .
docker run -p 7230:7230 niftytts
```

## Configuration

Most behaviour is configured through environment variables.

### Common

* `BACKEND` – choose which watcher to run (`edge` is the default).  Valid
  options are `edge`, `piper`, or `local`.
* `NIFTYTTS_POLL_INTERVAL` – seconds between checks for new jobs.
* `NIFTYTTS_SYNTH_TIMEOUT` – maximum seconds to wait for a synthesis
  operation before giving up.
* `NIFTYTTS_MIN_MP3_BYTES` – minimum size of a successful MP3 file.

### Edge backend

Variables recognised by `app/watchers/tts_watch_edge.py`:

* `NIFTYTTS_EDGE_VOICE` – voice to use (for example `en-US-AriaNeural`).
* `NIFTYTTS_EDGE_RATE` – speaking rate such as `+0%` or `-20%`.
* `NIFTYTTS_EDGE_PITCH` – pitch adjustment like `+0Hz` or `-2Hz`.
* `NIFTYTTS_EDGE_FORMAT` – explicit output audio format
  (`audio-24khz-48kbitrate-mono-mp3` by default).

### Piper backend

Variables recognised by `app/watchers/tts_watch_piper.py`:

* `NIFTYTTS_PIPER_EXE` – path to the `piper` executable.
* `NIFTYTTS_PIPER_MODEL` – path to a voice model `.onnx` file or a
  directory containing models.
* `NIFTYTTS_FFMPEG_PATH` – path to the `ffmpeg` binary.
* `NIFTYTTS_PIPER_LENGTH` – speaking rate (1.0 normal).
* `NIFTYTTS_PIPER_NOISE` – noise scale control (0.667 default).

### Local (pyttsx3) backend

Variables recognised by `app/watchers/tts_watch_pyttsx.py`:

* `NIFTYTTS_VOICE_SUBSTR` – case-insensitive substring used to choose a
  voice.
* `NIFTYTTS_RATE_WPM` – speaking rate in words per minute (default 180).
* `NIFTYTTS_VOLUME` – output volume 0.0–1.0 (default 1.0).
* `NIFTYTTS_FFMPEG_PATH` – path to the `ffmpeg` binary.
* Setting `NIFTYTTS_LIST_VOICES=1` prints available voice names and
  exits.

### Job output

Generated MP3 files and JSON metadata are written to
`app/jobs/outgoing`.  Errors are reported in corresponding `.err.txt`
files.  Temporary job input lives in `app/jobs/incoming`.

## License

See `app/LICENSE` for licensing information.

