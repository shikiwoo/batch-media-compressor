# Batch Media Compressor

A small web app that batch-compresses videos, audio and images with **ffmpeg**, either without visible quality loss or, if you want, lossy for much smaller files. It runs as a Docker container and shows live progress for each file, with speed and ETA.

## Quick start

```sh
mkdir -p media/input media/output
# put your files (folders are fine) in media/input
docker compose pull && docker compose up -d   # use the prebuilt image from GHCR
# or: docker compose up -d --build            # build it locally instead
```

Then open http://localhost:8080:

1. **Settings**: pick a preset for each media type, or "Don't touch" to skip that type.
2. **Pick files**: tick files or use select-all. Filter by name or type.
3. **Compress selected**: watch the per-file and overall progress.

Compressed files go to `media/output` with the same folder structure. Your originals are mounted **read-only** and are never modified.

Without compose:

```sh
docker run -d -p 8080:8080 \
  -v /path/to/originals:/input:ro \
  -v /path/to/compressed:/output \
  ghcr.io/shikiwoo/batch-media-compressor:latest
```

### Prebuilt images

GitHub Actions (`.github/workflows/docker.yml`) builds the image for `linux/amd64` and `linux/arm64` and pushes it to GHCR:

| Event | Tags pushed |
|---|---|
| Push to `main` | `latest`, `sha-<commit>` |
| Push a tag `v1.2.0` | `1.2.0`, `1.2`, `sha-<commit>` |
| Pull request | builds only, nothing pushed |

## Presets

Each media type has its own preset, grouped by how much quality you're willing to trade:

- **Lossless**: the decoded result is bit-for-bit (audio) or pixel-for-pixel (images) identical.
- **Visually lossless**: technically lossy, but tuned so you can't tell it apart from the source.
- **Lossy**: much smaller files, and you may see or hear quality loss at aggressive settings.

| Type | Preset | Category | Quality setting | Notes |
|---|---|---|---|---|
| Video | **H.265 / HEVC** (default) | Visually lossless | CRF 20 | Best all-rounder. Usually 30–60% smaller. Plays almost everywhere. |
| Video | **AV1 (SVT-AV1)** | Visually lossless | CRF 24 | Smaller still. Slower to encode and needs newer players. |
| Video | **H.264 lossless** | Lossless | – | Only helps with raw or intermediate sources (screen recordings, ProRes). Normal videos get bigger. |
| Video | **H.265 / HEVC — small** | Lossy | CRF 28 + max resolution | Big savings for archiving or sharing. |
| Video | **AV1 — small** | Lossy | CRF 35 + max resolution | Smallest video files. Slow to encode. |
| Video | **H.264 — max compatibility** | Lossy | CRF 23 + max resolution | Plays on everything, including old TVs. |
| Audio | **FLAC** (default) | Lossless | – | WAV/AIFF/APE/WavPack → FLAC. |
| Audio | **Opus** | Lossy | 128 kbps | Best quality per MB. |
| Audio | **AAC (.m4a)** | Lossy | 192 kbps | Native on Apple devices. |
| Audio | **MP3** | Lossy | 192 kbps | Plays on anything. |
| Image | **WebP lossless** (default) | Lossless | – | PNG/BMP/TIFF → WebP. Pixel-exact for 8-bit images. |
| Image | **PNG re-compress** | Lossless | – | Keeps PNG format and bit depth. |
| Image | **WebP lossy** | Lossy | 80 | ~30% smaller than JPEG, keeps transparency. |
| Image | **JPEG** | Lossy | 85 | Opens anywhere. Transparency is flattened. |

Truly lossless video compression can't shrink a file that is already compressed, such as a normal H.264 MP4. The math doesn't allow it. That's why the default video presets are *visually* lossless. For video, the quality setting is CRF: **lower CRF = better quality and a bigger file**. For audio it's the bitrate, and for images it's 1–100.

**Max resolution** (lossy video presets only) caps the *short* side of the video. "1080p" turns 4K landscape into 1920×1080 and 4K portrait into 1080×1920. It never upscales.

Lossless audio and image presets only run on lossless sources. Converting an MP3 to FLAC, or a JPEG to lossless WebP, can't bring back lost quality and only makes the file bigger, so those files are marked *skipped* with a reason. The lossy presets accept everything, including MP3/M4A/OGG and JPEG/WebP inputs.

Lossy image presets drop EXIF metadata (camera, date, GPS). Photos are rotated upright first, so they don't end up sideways.

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
