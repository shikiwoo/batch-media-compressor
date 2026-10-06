# Batch Media Compressor

A small web app that batch-compresses videos, audio and images with **ffmpeg** without visible quality loss. It runs as a Docker container and shows live progress for each file, with speed and ETA.

## Quick start

```sh
mkdir -p media/input media/output
# put your files (folders are fine) in media/input
docker compose up -d --build
```

Then open http://localhost:8080:

1. **Settings**: pick a preset for each media type, or "Don't touch" to skip that type.
2. **Pick files**: tick files or use select-all. Filter by name or type.
3. **Compress selected**: watch the per-file and overall progress.

Compressed files go to `media/output` with the same folder structure. Your originals are mounted **read-only** and are never modified.

Without compose:

```sh
docker build -t batch-media-compressor .
docker run -d -p 8080:8080 \
  -v /path/to/originals:/input:ro \
  -v /path/to/compressed:/output \
  batch-media-compressor
```

## Presets: what "without losing quality" means here

| Type | Preset | Lossless? | Notes |
|---|---|---|---|
| Video | **H.265 / HEVC** (default, CRF 20) | Visually lossless | Best all-rounder. Usually 30–60% smaller. Plays almost everywhere. |
| Video | **AV1 (SVT-AV1)** (CRF 24) | Visually lossless | Smallest files. Slower to encode and needs newer players. |
| Video | **H.264 lossless** | Bit-exact | Only helps with raw or intermediate sources (screen recordings, ProRes). Normal videos get bigger. |
| Audio | **FLAC** | Bit-exact | WAV/AIFF/APE/WavPack → FLAC. |
| Image | **WebP lossless** | Pixel-exact (8-bit) | PNG/BMP/TIFF → WebP. Use PNG re-compress for 16-bit images. |
| Image | **PNG re-compress** | Pixel-exact | Keeps PNG format and bit depth. |

Truly lossless video compression can't shrink a file that is already compressed, such as a normal H.264 MP4. The math doesn't allow it. That's why the video presets are *visually* lossless: the CRF slider sets the quality, and around 18–22 for HEVC you can't tell the result apart from the source. Lower CRF means better quality and a bigger file.

Safety features:
- **Audio tracks, subtitles, chapters and metadata are stream-copied**, bit-for-bit. Only the video stream is re-encoded.
- If a compressed file **isn't smaller** than the original, the original is copied to the output instead ("Kept original").
- Encoding writes to a hidden `.name.partial.ext` file first. It is renamed only when ffmpeg finishes cleanly, so cancelled or failed jobs never leave half-written files.
- The original modification time is kept on the output file.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `INPUT_DIR` | `/input` | Where to look for media |
| `OUTPUT_DIR` | `/output` | Where compressed files go |
| `CONCURRENCY` | `1` | Files encoded at the same time. x265 and AV1 already use all CPU cores, so 1 is usually fastest overall. |

Output files are owned by root unless you set `user: "UID:GID"` in `docker-compose.yml` (see the commented line).

Keep `/input` and `/output` as separate folders, otherwise outputs show up in the file list.

There is **no login**. Don't expose port 8080 to the internet. Keep it on your LAN or put it behind a reverse proxy with auth.

## How it works

```
app/
  main.py      FastAPI routes: list files, start/cancel jobs, serve the UI
  jobs.py      job queue + worker(s) that run ffmpeg and parse progress
  presets.py   preset definitions → ffmpeg arguments
  static/      the web UI (plain HTML/CSS/JS, no build step)
```

- **Progress:** ffmpeg runs with `-progress pipe:1`, which prints lines like `out_time_us=5000000` and `speed=1.2x` several times a second. The worker reads them and divides by the file's duration (from `ffprobe`) to get a percentage and ETA. The browser polls `GET /api/jobs` every second and redraws.
- **Queue:** an `asyncio.Queue` with `CONCURRENCY` worker tasks. Cancel sends SIGTERM to that job's ffmpeg process.
- **Overall progress** is weighted by file size, so a 10 GB video counts more than a 100 KB image.

### Running without Docker (for development)

Needs Python 3.11+ and ffmpeg built with libx265, libsvtav1, libx264 and libwebp.

```fish
python3 -m venv .venv
source .venv/bin/activate.fish
pip install -r requirements.txt
env INPUT_DIR=./media/input OUTPUT_DIR=./media/output uvicorn app.main:app --reload --port 8080
```
