import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .jobs import JobManager
from .presets import PRESETS, kind_of, presets_for

INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/input")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output")).resolve()
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "1")))
STATIC = Path(__file__).parent / "static"

manager = JobManager(INPUT_DIR, OUTPUT_DIR, CONCURRENCY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
        "presets": {
            kind: [
                {
                    "id": p.id, "label": p.label, "description": p.description,
                    "lossless": p.lossless, "uses_crf": p.uses_crf, "default_crf": p.default_crf,
                }
                for p in presets_for(kind)
            ]
            for kind in ("video", "audio", "image")
        },
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
    crf: int | None = Field(default=None, ge=0, le=51)
    skip_existing: bool = True


@app.post("/api/jobs")
def start_jobs(req: StartRequest):
    chosen = {"video": req.video_preset, "audio": req.audio_preset, "image": req.image_preset}
    for kind, pid in chosen.items():
        if pid and (pid not in PRESETS or PRESETS[pid].kind != kind):
            raise HTTPException(400, f"Unknown {kind} preset: {pid}")

    added = 0
    for rel in req.files:
        path = safe_input_path(rel)
        pid = chosen.get(kind_of(path))
        if not pid:
            continue
        crf = req.crf if PRESETS[pid].uses_crf else None
        manager.add(str(path.relative_to(INPUT_DIR)), pid, crf, req.skip_existing)
        added += 1
    return {"added": added}


@app.get("/api/jobs")
def get_jobs():
    return manager.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    manager.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/cancel-all")
def cancel_all():
    for job_id in list(manager.jobs):
        manager.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_finished():
    manager.clear_finished()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
