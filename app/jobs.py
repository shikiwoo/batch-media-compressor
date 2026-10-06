"""Job queue + ffmpeg runner.

How progress works: ffmpeg is started with `-progress pipe:1`, which makes it
print `key=value` lines to stdout a few times per second (out_time_us, speed,
...). We divide out_time by the file's duration (from ffprobe) to get a 0–1
fraction. The web UI just polls /api/jobs to read that state.
"""

import asyncio
import itertools
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass, field, fields
from pathlib import Path

from .presets import PRESETS


@dataclass
class Job:
    id: int
    src: str  # path relative to INPUT_DIR
    dst: str  # path relative to OUTPUT_DIR
    preset: str
    crf: int | None
    status: str = "queued"  # queued | running | done | kept_original | skipped | failed | cancelled
    progress: float = 0.0
    speed: str = ""
    eta: float | None = None
    duration: float | None = None
    in_size: int = 0
    out_size: int | None = None
    message: str = ""
    started: float | None = None
    finished: float | None = None
    # internal, not sent to the browser
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def public(self) -> dict:
        # not asdict(): that deep-copies _proc, which can't be copied
        return {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}


ACTIVE = {"queued", "running"}


class JobManager:
    def __init__(self, input_dir: Path, output_dir: Path, concurrency: int):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.concurrency = concurrency
        self.jobs: dict[int, Job] = {}
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._workers: list[asyncio.Task] = []

    def start(self):
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def stop(self):
        for job in self.jobs.values():
            if job._proc and job._proc.returncode is None:
                job._proc.kill()
        for w in self._workers:
            w.cancel()

    # ---- public API -------------------------------------------------------

    def add(self, src_rel: str, preset_id: str, crf: int | None, skip_existing: bool) -> Job:
        preset = PRESETS[preset_id]
        src = Path(src_rel)
        dst = src.with_suffix(preset.output_ext(src))
        job = Job(id=next(self._ids), src=src_rel, dst=str(dst), preset=preset_id, crf=crf)
        job.in_size = (self.input_dir / src).stat().st_size
        self.jobs[job.id] = job

        if any(j.src == src_rel and j.status in ACTIVE for j in self.jobs.values() if j is not job):
            self._finish(job, "skipped", "Already in the queue")
        elif skip_existing and (self.output_dir / dst).exists():
            job.out_size = (self.output_dir / dst).stat().st_size
            self._finish(job, "skipped", "Output already exists")
        else:
            self.queue.put_nowait(job.id)
        return job

    def cancel(self, job_id: int):
        job = self.jobs.get(job_id)
        if not job or job.status not in ACTIVE:
            return
        if job.status == "running" and job._proc and job._proc.returncode is None:
            job._proc.terminate()  # the worker notices and cleans up
        job.status = "cancelled"  # a queued job is simply ignored when the worker reaches it
        job.finished = time.time()

    def clear_finished(self):
        self.jobs = {i: j for i, j in self.jobs.items() if j.status in ACTIVE}

    def snapshot(self) -> dict:
        jobs = [j.public() for j in self.jobs.values()]
        finished = [j for j in self.jobs.values() if j.status not in ACTIVE]
        saved_in = sum(j.in_size for j in finished if j.out_size is not None)
        saved_out = sum(j.out_size for j in finished if j.out_size is not None)
        # overall progress, weighted by file size so one huge video counts more than a tiny one
        total = sum(j.in_size for j in self.jobs.values() if j.status != "cancelled") or 1
        done = sum(
            j.in_size * (1.0 if j.status not in ACTIVE else j.progress)
            for j in self.jobs.values()
            if j.status != "cancelled"
        )
        counts: dict[str, int] = {}
        for j in self.jobs.values():
            counts[j.status] = counts.get(j.status, 0) + 1
        return {
            "jobs": jobs,
            "summary": {
                "counts": counts,
                "overall_progress": done / total,
                "bytes_in": saved_in,
                "bytes_out": saved_out,
            },
        }

    # ---- worker -----------------------------------------------------------

    async def _worker(self):
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            try:
                if job and job.status == "queued":
                    await self._run(job)
            except Exception as e:  # never let one bad file kill the worker
                if job:
                    self._finish(job, "failed", f"Internal error: {e}")
            finally:
                self.queue.task_done()

    async def _run(self, job: Job):
        preset = PRESETS[job.preset]
        src = self.input_dir / job.src
        dst = self.output_dir / job.dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp name first so a half-finished file never looks complete.
        # the real extension stays last so ffmpeg still knows the output format.
        tmp = dst.with_name(f".{dst.stem}.partial{dst.suffix}")

        job.status = "running"
        job.started = time.time()
        job.duration = await probe_duration(src) if preset.kind != "image" else None

        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", str(src),
            *preset.ffmpeg_args(src, dst, job.crf),
            str(tmp),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        job._proc = proc
        # stderr must be drained at the same time as stdout, otherwise a chatty
        # ffmpeg can fill the pipe buffer and freeze
        stderr_task = asyncio.create_task(_tail(proc.stderr))
        await self._read_progress(job, proc.stdout)
        await proc.wait()
        stderr = await stderr_task
        job._proc = None

        if job.status == "cancelled":
            tmp.unlink(missing_ok=True)
            job.message = "Cancelled"
            return
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            self._finish(job, "failed", stderr or f"ffmpeg exited with code {proc.returncode}")
            return

        out_size = tmp.stat().st_size
        if out_size >= job.in_size:
            # compression didn't help — keep the original so the output folder is still complete
            tmp.unlink()
            orig_dst = self.output_dir / job.src
            shutil.copy2(src, orig_dst)
            job.dst = job.src
            job.out_size = job.in_size
            self._finish(job, "kept_original", "Compressed file wasn't smaller, copied the original instead")
        else:
            os.replace(tmp, dst)
            shutil.copystat(src, dst)  # keep the original modification time
            job.out_size = out_size
            self._finish(job, "done")

    async def _read_progress(self, job: Job, stdout: asyncio.StreamReader):
        async for raw in stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and job.duration:
                try:
                    secs = int(value) / 1_000_000
                except ValueError:
                    continue
                job.progress = max(0.0, min(secs / job.duration, 0.999))
                elapsed = time.time() - job.started
                if job.progress > 0.01:
                    job.eta = elapsed / job.progress - elapsed
            elif key == "speed":
                job.speed = value
            elif key == "progress" and value == "end":
                job.progress = 1.0

    def _finish(self, job: Job, status: str, message: str = ""):
        job.status = status
        job.message = message
        job.finished = time.time()
        job.eta = None
        if status in ("done", "kept_original", "skipped"):
            job.progress = 1.0


async def probe_duration(path: Path) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        d = float(out.decode().strip())
        return d if d > 0 else None
    except ValueError:
        return None


async def _tail(stream: asyncio.StreamReader, keep: int = 15) -> str:
    lines: deque[str] = deque(maxlen=keep)
    async for raw in stream:
        line = raw.decode(errors="replace").rstrip()
        if line:
            lines.append(line)
    return "\n".join(lines)
