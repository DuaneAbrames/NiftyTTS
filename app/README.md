# NiftyTTS
Text to speech for Nifty stories.

## Environment Variables

* `NIFTYTTS_UID` – user ID that should own generated files (default `99`)
* `NIFTYTTS_GID` – group ID that should own generated files (default `100`)

Set these to your host user's UID/GID when running the container; the entrypoint
will `chown` everything under `jobs/` to that user and group when it exits.
