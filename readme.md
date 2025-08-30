# NiftyTTS

## :warning: In no way does this project encourage anyone to violate the author's copyright in their stories.  It may only be used to create audio files for the user's own **PERSONAL** use.  If you make files with this and distribute them on the internet a sleep-deprived raccoon armed with a kazoo will invade your home and narrate your every failure in song!  

NiftyTTS is designed for converting stories from nifty.org into MP3 audio files.
The project provides a small [FastAPI](https://fastapi.tiangolo.com/) web
application that fetches a URL, extracts text, and posts conversion jobs
into a `jobs` directory. A single background dispatcher watches for jobs and
invokes a pluggable TTS backend to produce MP3 files.

The repository includes helper scripts and a Docker image so the service
can run entirely locally or inside a container.

Note that this is designed to work with the 'old' nifty, not the new beta.

The tampermonkey script can be imported, and it will give you a "TTS" button
next to any link it thinks is a story (it's pretty stupid about what constitutes
a story).

Output folders will be based on the series name (or category name if the story is
not serial) 



## Architecture

- `app/app.py`: Web UI and HTTP API. Accepts a URL or pasted text, lets you
  choose a backend and voice, and shows progress while audio is generated.
- `app/backends/`: Pluggable TTS backends implementing a small interface
  (`base.py`). Current adapters: `edge.py`, `piper.py`, and `pyttsx3.py`.
  Visit `/backends` to see installed backends and available voices.
- `app/watchers/dispatcher_watch.py`: Single watcher that monitors
  `jobs/incoming` and dispatches each job to the selected backend (per job or
  default).
- `entrypoint.sh`: Launches the web app and dispatcher together (used by
  the Docker image).

## Running

Local (Python 3.12+, ffmpeg required):

1. Install deps in `app/requirements.txt` and ensure `ffmpeg` is available.
2. Start the web app and dispatcher in two terminals:

   - Web app:
     ```bash
     uvicorn app.app:app --host 0.0.0.0 --port 7230
     ```
   - Dispatcher:
     ```bash
     python -m app.watchers.dispatcher_watch
     ```

Docker:

```bash
docker build -t niftytts .
docker run -p 7230:7230 \
  -e NIFTYTTS_BACKEND=edge \
  -v $(pwd)/models:/models \
  niftytts
```

Open http://localhost:7230/ and paste a URL. On the second step you can pick a
backend and a voice; leave voice blank to use the backend default. The job will
appear on the status page and be written under `app/jobs/outgoing`.

## Configuration

Most behaviour is configured through environment variables. A backend can be
chosen globally by env var, and overridden per job from the web UI. Voices can
also be selected per job.

### Common

- `NIFTYTTS_BACKEND` (or `BACKEND`): default backend if a job does not specify
  one. Options: `edge`, `piper`, `pyttsx3`. Default: `edge`.
- `NIFTYTTS_POLL_INTERVAL`: seconds between checks for new jobs. Default 0.5.
- `NIFTYTTS_SYNTH_TIMEOUT`: max seconds to wait per synthesis. Default 600.
- `NIFTYTTS_MIN_MP3_BYTES`: minimum size of a successful MP3. Default 1024.

### Edge backend (app/backends/edge.py)

- `NIFTYTTS_EDGE_VOICE`: default voice (e.g., `en-US-AriaNeural`).
- `NIFTYTTS_EDGE_RATE`: speaking rate (e.g., `+0%`, `-20%`).
- `NIFTYTTS_EDGE_PITCH`: pitch (e.g., `+0Hz`, `-2Hz`).
- `NIFTYTTS_EDGE_FORMAT`: output audio format (default
  `audio-24khz-48kbitrate-mono-mp3`).

### Piper backend (app/backends/piper.py)

- `NIFTYTTS_PIPER_EXE`: path to `piper` executable. Default `piper`.
- `NIFTYTTS_PIPER_MODEL`: path to a `.onnx` model or a directory containing
  models. Default `/models` (mount this into the container).
- `NIFTYTTS_FFMPEG_PATH`: path to `ffmpeg`. Default `ffmpeg`.
- `NIFTYTTS_PIPER_LENGTH`: speaking rate (e.g., `1.0`).
- `NIFTYTTS_PIPER_NOISE`: noise scale (default `0.667`).

Voice selection: enter either a full path to a `.onnx` model or the basename of
the model present under `NIFTYTTS_PIPER_MODEL` directory (e.g., `en_US-amy-low`).

### Local (pyttsx3) backend (app/backends/pyttsx3.py)

- `NIFTYTTS_VOICE_SUBSTR`: default voice name substring to match.
- `NIFTYTTS_RATE_WPM`: words-per-minute (default 180).
- `NIFTYTTS_VOLUME`: 0.0..1.0 (default 1.0).
- `NIFTYTTS_FFMPEG_PATH`: path to `ffmpeg`.

### Job output & file layout

Generated MP3 files and JSON metadata are written to
`app/jobs/outgoing`.  Errors are reported in corresponding `.err.txt`
files.  Temporary job input lives in `app/jobs/incoming`.

For text submissions containing RFC822-style headers, the app extracts the
`From`, `Subject`, and `Date` values and records them in each job's JSON
metadata and ID3 tags.

Output paths are organized as:

```
Author/Series/NNN - Title/Title.mp3
```

Non-series stories from nifty.org are placed under `Author/Title/Title.mp3`.
Series and track numbers are derived from the URL and headers when available.

## License

See `app/LICENSE` for licensing information.

