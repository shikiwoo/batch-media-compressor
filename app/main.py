import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import hw
from .jobs import JobManager
from .presets import MAX_HEIGHTS, PRESETS, kind_of, presets_for

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")

INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/input")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output")).resolve()
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "1")))
# where the queue is saved so it survives restarts (kept next to the outputs, so it
# also survives the container being recreated, e.g. a stack redeploy in Portainer)
STATE_DIR = Path(os.environ.get("STATE_DIR", str(OUTPUT_DIR / ".compressor"))).resolve()
RESUME = os.environ.get("AUTO_RESUME", "1").lower() not in ("0", "false", "no")
STATIC = Path(__file__).parent / "static"

manager = JobManager(INPUT_DIR, OUTPUT_DIR, CONCURRENCY, STATE_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    await hw.detect()  # ~1 second: tries a tiny GPU encode so the UI knows what works
    manager.remove_stale_partials()
    if RESUME:
        manager.load()
    manager.start()
    yield
    await manager.stop()


app = FastAPI(title="Batch Media Compressor", lifespan=lifespan)


def safe_input_path(rel: str) -> Path:
    """Resolve a path from the browser and make sure it can't escape INPUT_DIR (no ../../etc/passwd)."""
    p = (INPUT_DIR / rel).resolve()
    if not p.is_relative_to(INPUT_DIR) or not p.is_file():
        raise HTTPException(400, f"Invalid file: {rel}")
    return p


@app.get("/api/config")
def config():
    return {
        "input_dir": str(INPUT_DIR),
        "output_dir": str(OUTPUT_DIR),
        "concurrency": CONCURRENCY,
        "presets": {kind: [p.public() for p in presets_for(kind)] for kind in ("video", "audio", "image")},
        "max_heights": MAX_HEIGHTS,
        "hardware": hw.HW.public(),
        "auto_resume": RESUME,
    }


@app.get("/api/files")
def list_files():
    files = []
    for root, dirs, names in os.walk(INPUT_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(root) / name
            kind = kind_of(path)
            if kind:
                files.append({
                    "path": str(path.relative_to(INPUT_DIR)),
                    "kind": kind,
                    "size": path.stat().st_size,
                })
    return files


class StartRequest(BaseModel):
    files: list[str]
    # preset id per kind; None/"" means "don't touch files of this kind"
    video_preset: str | None = None
    audio_preset: str | None = None
    image_preset: str | None = None
    # quality per kind (CRF / kbps / 1–100); None = the preset's default
    video_quality: int | None = None
    audio_quality: int | None = None
    image_quality: int | None = None
    max_height: int | None = None  # only used by presets with scalable=True
    skip_existing: bool = True


@app.post("/api/jobs")
async def start_jobs(req: StartRequest):
    chosen = {"video": req.video_preset, "audio": req.audio_preset, "image": req.image_preset}
    qualities = {"video": req.video_quality, "audio": req.audio_quality, "image": req.image_quality}
    for kind, pid in chosen.items():
        if not pid:
            continue
        if pid not in PRESETS or PRESETS[pid].kind != kind:
            raise HTTPException(400, f"Unknown {kind} preset: {pid}")
        if not PRESETS[pid].available:
            raise HTTPException(400, f"{PRESETS[pid].label} isn't available: {PRESETS[pid].unavailable_reason}")
        q, spec = qualities[kind], PRESETS[pid].quality
        if q is not None and spec and not spec.min <= q <= spec.max:
            raise HTTPException(400, f"{kind} {spec.label} must be between {spec.min} and {spec.max}")
    if req.max_height is not None and req.max_height not in MAX_HEIGHTS:
        raise HTTPException(400, f"max_height must be one of {MAX_HEIGHTS}")

    added = 0
    for rel in req.files:
        path = safe_input_path(rel)
        pid = chosen.get(kind_of(path))
        if not pid:
            continue
        preset = PRESETS[pid]
        quality = None
        if preset.quality:
            quality = qualities[preset.kind] if qualities[preset.kind] is not None else preset.quality.default
        max_height = req.max_height if preset.scalable else None
        manager.add(str(path.relative_to(INPUT_DIR)), pid, quality, max_height, req.skip_existing)
        added += 1
    return {"added": added}


@app.get("/api/jobs")
def get_jobs():
    return manager.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    manager.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/cancel-all")
async def cancel_all():
    for job_id in list(manager.jobs):
        manager.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_finished():
    manager.clear_finished()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
