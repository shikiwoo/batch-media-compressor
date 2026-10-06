"""Job queue + ffmpeg runner.

How progress works: ffmpeg is started with `-progress pipe:1`, which makes it
print `key=value` lines to stdout a few times per second (out_time_us, speed,
...). We divide out_time by the file's duration (from ffprobe) to get a 0–1
fraction. The web UI just polls /api/jobs to read that state.

How resume works: every time a job is added or changes status, the whole job
list is written to a small JSON file (state.json). When the app starts it reads
that file back and puts everything that was queued or running back in the
queue. Files that were finished are never redone (their output exists).
A file that was halfway through starts again from 0%: ffmpeg can't continue a
half-finished encode.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass, field, fields
from pathlib import Path

from .presets import PRESETS

log = logging.getLogger("compressor")

# If a job has been started this many times without ever finishing, the app
# (or the whole container) is probably dying on it — e.g. out of memory.
# Stop retrying instead of crash-looping forever.
MAX_ATTEMPTS = 3

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # HDR10/Dolby Vision and HLG


@dataclass
class Job:
    id: int
    src: str  # path relative to INPUT_DIR
    dst: str  # path relative to OUTPUT_DIR
    preset: str
    quality: int | None
    max_height: int | None
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
    attempts: int = 0  # how many times ffmpeg was started for this job
    # internal, not sent to the browser or saved
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def public(self) -> dict:
        # not asdict(): that deep-copies _proc, which can't be copied
        return {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}


ACTIVE = {"queued", "running"}
JOB_FIELDS = {f.name for f in fields(Job) if not f.name.startswith("_")}


class JobManager:
    def __init__(self, input_dir: Path, output_dir: Path, concurrency: int, state_dir: Path):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.concurrency = concurrency
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self.jobs: dict[int, Job] = {}
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._next_id = 1  # only ever goes up, even after 'Clear finished', so ids are never reused
        self._workers: list[asyncio.Task] = []
        self._save_scheduled = False

    def start(self):
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def stop(self):
        """Called on shutdown (docker stop). Jobs that were running go back to
        'queued' so they restart cleanly next time, and don't count as a failed attempt."""
        for job in self.jobs.values():
            if job.status == "running":
                job.status = "queued"
                job.attempts = max(0, job.attempts - 1)
                job.progress, job.eta, job.speed = 0.0, None, ""
                if job._proc and job._proc.returncode is None:
                    job._proc.kill()
                partial_path(self.output_dir / job.dst).unlink(missing_ok=True)
        for w in self._workers:
            w.cancel()
        self._flush()

    # ---- saving & resuming ------------------------------------------------

    def _save(self):
        """Write the state file soon. Several changes in one go (e.g. queueing
        500 files) are batched into one write."""
        if self._save_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush()
            return
        self._save_scheduled = True
        loop.call_soon(self._flush)

    def _flush(self):
        self._save_scheduled = False
        data = {
            "version": 1,
            "next_id": self._next_id,
            "jobs": [j.public() for j in self.jobs.values()],
        }
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.state_file)  # atomic: a crash can't leave a half-written file
        except OSError as e:
            log.warning("Couldn't save state to %s: %s", self.state_file, e)

    def load(self):
        """Restore jobs from the last run and re-queue unfinished ones."""
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            raw_jobs = data["jobs"]
        except (OSError, ValueError, KeyError) as e:
            backup = self.state_file.with_suffix(".corrupt")
            log.warning("Ignoring unreadable state file (%s); moved to %s", e, backup)
            try:
                os.replace(self.state_file, backup)
            except OSError:
                pass
            return

        resumed = 0
        for raw in sorted(raw_jobs, key=lambda r: r.get("id", 0)):
            try:
                job = Job(**{k: v for k, v in raw.items() if k in JOB_FIELDS})
            except TypeError:
                continue  # entry from an incompatible version
            self.jobs[job.id] = job
            if job.status not in ACTIVE:
                continue
            if job.attempts >= MAX_ATTEMPTS:
                self._finish(job, "failed",
                             f"Gave up after {job.attempts} interrupted attempts (the app or container "
                             "kept stopping on this file — out of memory?)")
                continue
            if job.status == "running":
                job.message = "Restarted after the app was interrupted"
            job.status = "queued"
            job.progress, job.eta, job.speed = 0.0, None, ""
            job.started = None
            self.queue.put_nowait(job.id)
            resumed += 1

        self._next_id = max(data.get("next_id", 1), max(self.jobs, default=0) + 1)
        if resumed:
            log.info("Resumed %d unfinished job(s) from the last run", resumed)
        self._save()

    def remove_stale_partials(self):
        """Half-written `.name.partial.ext` files left behind by a crash.
        Nothing is running yet at startup, so every one of them is garbage."""
        removed = 0
        for root, dirs, names in os.walk(self.output_dir):
            dirs[:] = [d for d in dirs if Path(root, d) != self.state_dir]
            for name in names:
                if name.startswith(".") and ".partial." in name:
                    try:
                        Path(root, name).unlink()
                        removed += 1
                    except OSError:
                        pass
        if removed:
            log.info("Removed %d leftover partial file(s) from an interrupted run", removed)

    # ---- public API -------------------------------------------------------

    def add(self, src_rel: str, preset_id: str, quality: int | None, max_height: int | None,
            skip_existing: bool) -> Job:
        preset = PRESETS[preset_id]
        src = Path(src_rel)
        dst = src.with_suffix(preset.output_ext(src))
        job = Job(id=self._next_id, src=src_rel, dst=str(dst), preset=preset_id,
                  quality=quality, max_height=max_height)
        self._next_id += 1
        job.in_size = (self.input_dir / src).stat().st_size
        self.jobs[job.id] = job

        if not preset.accepts(src):
            self._finish(job, "skipped", f"{preset.label} doesn't apply to {src.suffix} files "
                                         "(re-encoding an already-lossy file losslessly only makes it bigger)")
        elif any(j.src == src_rel and j.status in ACTIVE for j in self.jobs.values() if j is not job):
            self._finish(job, "skipped", "Already in the queue")
        elif skip_existing and (self.output_dir / dst).exists():
            job.out_size = (self.output_dir / dst).stat().st_size
            self._finish(job, "skipped", "Output already exists")
        else:
            self.queue.put_nowait(job.id)
            self._save()
        return job

    def cancel(self, job_id: int):
        job = self.jobs.get(job_id)
        if not job or job.status not in ACTIVE:
            return
        if job.status == "running" and job._proc and job._proc.returncode is None:
            job._proc.terminate()  # the worker notices and cleans up
        job.status = "cancelled"  # a queued job is simply ignored when the worker reaches it
        job.finished = time.time()
        self._save()

    def clear_finished(self):
        self.jobs = {i: j for i, j in self.jobs.items() if j.status in ACTIVE}
        self._save()

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
        if not src.is_file():
            self._finish(job, "failed", "Source file no longer exists")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp name first so a half-finished file never looks complete
        tmp = partial_path(dst)

        job.status = "running"
        job.started = time.time()
        job.attempts += 1
        self._save()  # saved BEFORE ffmpeg starts, so a crash is counted as an attempt
        job.duration, transfer = await probe(src) if preset.kind != "image" else (None, None)

        if preset.hw_encoder and transfer in HDR_TRANSFERS:
            # the GPU path converts to 8-bit SDR, which would wash out HDR colours
            self._finish(job, "skipped", "HDR video: the GPU preset would flatten the colours. "
                                         "Use a software preset (H.265 / AV1) for HDR files.")
            return

        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            *preset.input_args(),  # global options that must come before -i
            "-i", str(src),
            *preset.ffmpeg_args(dst, job.quality, job.max_height),
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
        self._save()


def partial_path(dst: Path) -> Path:
    """Where a file is written while ffmpeg is still working on it. The real
    extension stays last so ffmpeg still knows the output format."""
    return dst.with_name(f".{dst.stem}.partial{dst.suffix}")


async def probe(path: Path) -> tuple[float | None, str | None]:
    """Returns (duration in seconds, colour transfer of the first video stream)."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=color_transfer", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        data = json.loads(out.decode())
    except ValueError:
        return None, None
    try:
        d = float(data.get("format", {}).get("duration"))
        duration = d if d > 0 else None
    except (TypeError, ValueError):
        duration = None
    streams = data.get("streams") or [{}]
    return duration, streams[0].get("color_transfer")


async def _tail(stream: asyncio.StreamReader, keep: int = 15) -> str:
    lines: deque[str] = deque(maxlen=keep)
    async for raw in stream:
        line = raw.decode(errors="replace").rstrip()
        if line:
            lines.append(line)
    return "\n".join(lines)
