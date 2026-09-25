import asyncio
import fcntl
import logging
import os
import re
import shutil
import subprocess
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from . import camera, cinematic, dslr, lighting, timing_overlay
from .models import ExportRequest, Settings
from .store import Store
from .video import export_details, export_filters
from .export_progress import ExportProgress, FFmpegProgress

logger = logging.getLogger(__name__)


def in_window(now: float, settings: Settings) -> bool:
    if not settings.daylight_only:
        return True
    local = datetime.fromtimestamp(now, ZoneInfo(settings.timezone)).strftime("%H:%M")
    start, end = settings.day_start, settings.day_end
    return start <= local < end if start < end else local >= start or local < end


def next_allowed(now: float, settings: Settings) -> float:
    if in_window(now, settings):
        return now
    # Walk real UTC minutes: handles missing/repeated wall-clock times at DST changes.
    candidate = datetime.fromtimestamp(now, timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(2880):
        if in_window(candidate.timestamp(), settings):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)
    raise RuntimeError("Unable to find next capture window")


class Recorder:
    def __init__(self, root: Path, demo: bool = False, camera_lock=None, export_gate=None):
        self.store = Store(root)
        self.root, self.demo = root, demo
        self.camera_lock = camera_lock if camera_lock is not None else asyncio.Lock()
        self.export_gate = export_gate if export_gate is not None else asyncio.Lock()
        self.stopping = threading.Event()
        self.tick_lock = asyncio.Lock()
        self.state_lock = asyncio.Lock()
        self.preview_lock = asyncio.Lock()
        self.export_task = None
        self.scheduler_task = None
        self.owner = None
        self.deleting = False

    def ensure_available(self):
        if self.deleting:
            raise ValueError("This project is being deleted")

    def settings(self):
        return Settings.model_validate(self.store.get("settings"))

    async def start(self):
        self.owner = (self.root / "recorder.lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            raise RuntimeError("This data directory already has a recorder. Run exactly one worker.")
        # A killed process can leave temporary images or half-rendered videos behind.
        self.cleanup_deleted_frames()
        for path in (self.root / "exports").iterdir():
            if path.is_dir() and re.fullmatch(r"\.[0-9a-f]{32}\.lighting", path.name):
                shutil.rmtree(path)
        for folder, pattern in (("frames", r"\.[0-9a-f]{32}\.jpg"),
                                ("previews", r"\.[0-9a-f]{32}\.jpg"),
                                ("exports", r"\.?[0-9a-f]{32}\.(txt|log|progress)|\.[0-9a-f]{32}\.(mp4|timing\.ass)")):
            for path in (self.root / folder).iterdir():
                if path.is_file() and re.fullmatch(pattern, path.name):
                    path.unlink()
        with self.store.connect() as db:
            db.execute("UPDATE exports SET status='queued', error=NULL, progress=NULL WHERE status='running'")
            db.execute("UPDATE exports SET progress=NULL WHERE status='queued'")
        state = self.store.get("runtime")
        if state["running"]:
            state["next_capture_at"] = next_allowed(time.time(), self.settings())
            self.store.put("runtime", state)
            self.store.event("info", "Recording restored after restart. Next capture scheduled automatically.")
        self.stopping.clear()
        self.scheduler_task = asyncio.create_task(self.scheduler())
        if self.store.rows("SELECT id FROM exports WHERE status='queued' LIMIT 1"):
            self.export_task = asyncio.create_task(self.run_exports())

    async def stop(self):
        self.stopping.set()
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        # Wait for an in-flight camera thread before releasing ownership.
        async with self.camera_lock:
            pass
        async with self.preview_lock:
            pass
        if self.export_task:
            await self.export_task
        if self.owner:
            self.owner.close()

    async def save_settings(self, settings):
        if not self.demo:
            settings = settings.model_copy(update={"camera_device": dslr.stable_device(settings.camera_device)})
        async with self.state_lock:
            self.ensure_available()
            state = self.store.get("runtime")
            if state["started_at"]:
                state["ends_at"] = state["started_at"] + settings.duration_days * 86400
            if state["running"]:
                state["next_capture_at"] = next_allowed(time.time() + settings.interval_minutes * 60, settings)
            self.store.put_many({"settings": settings.model_dump(), "runtime": state})
            self.store.event("info", "Capture settings updated")

    async def set_running(self, running):
        async with self.state_lock:
            self.ensure_available()
            state = self.store.get("runtime")
            now = time.time()
            if running and state["ends_at"] and state["ends_at"] <= now:
                raise ValueError("This study has ended. Extend its duration in settings to resume.")
            if running and not state["started_at"]:
                state["started_at"] = now
                state["ends_at"] = now + self.settings().duration_days * 86400
            state["running"] = running
            state["next_capture_at"] = next_allowed(now, self.settings()) if running else None
            self.store.put("runtime", state)
            self.store.event("info", "Recording started / resumed" if running else "Recording paused")

    def check_space(self):
        if shutil.disk_usage(self.root).free < self.settings().reserve_mb * 1024 * 1024:
            raise RuntimeError("Free disk space is below the configured reserve. Free space or lower the reserve to continue.")

    def capture_sync(self, settings, save):
        self.check_space()
        frame_id = uuid.uuid4().hex
        temp = self.root / "frames" / f".{frame_id}.jpg"
        thumbnail = self.root / "thumbs" / f"{frame_id}.jpg"
        target = self.root / "frames" / f"{frame_id}.jpg"
        try:
            camera.capture(temp, settings, self.demo)
            if not save:
                return temp.read_bytes()
            with Image.open(temp) as source, ImageOps.exif_transpose(source) as image:
                image.thumbnail((480, 320))
                image.convert("RGB").save(thumbnail, "JPEG", quality=80)
            # Publish the complete image before committing its database record.
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            temp.replace(target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            frame = {"id": frame_id, "captured_at": time.time(), "bytes": target.stat().st_size}
            with self.store.connect() as db:
                db.execute("INSERT INTO frames(id,captured_at,bytes) VALUES (:id,:captured_at,:bytes)", frame)
            return frame
        except Exception:
            thumbnail.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        finally:
            temp.unlink(missing_ok=True)

    async def take_photo(self, save=True, scheduled=False, preview_settings=None):
        if self.camera_lock.locked() and not scheduled:
            raise ValueError("Camera is busy. Try again in a moment.")
        async with self.camera_lock:
            self.ensure_available()
            settings = preview_settings if not save and preview_settings is not None else self.settings()
            original_device = settings.camera_device
            if not self.demo:
                settings = settings.model_copy(update={"camera_device": dslr.stable_device(original_device)})
            if scheduled:
                state = self.store.get("runtime")
                now = time.time()
                if not state["running"] or (state["ends_at"] and now >= state["ends_at"]) or not in_window(now, settings):
                    return None
            task = asyncio.create_task(asyncio.to_thread(self.capture_sync, settings, save))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            except Exception as exc:
                if save:
                    async with self.state_lock:
                        state = self.store.get("runtime")
                        state["last_error"] = str(exc)
                        if state["running"]:
                            state["next_capture_at"] = next_allowed(time.time() + 60, self.settings())
                        self.store.put("runtime", state)
                        self.store.event("error", str(exc))
                raise RuntimeError(str(exc)) from exc
            if save:
                async with self.state_lock:
                    current = self.settings()
                    if current.camera_device == original_device and settings.camera_device != original_device:
                        current.camera_device = settings.camera_device
                        self.store.put("settings", current.model_dump())
                    state = self.store.get("runtime")
                    state["last_error"] = None
                    self.store.put("runtime", state)
            return result

    async def tick(self):
        async with self.tick_lock:
            await self._tick()

    async def _tick(self):
        due = False
        async with self.state_lock:
            state, settings, now = self.store.get("runtime"), self.settings(), time.time()
            if not state["running"]:
                return
            if now >= state["ends_at"]:
                state.update(running=False, next_capture_at=None)
                self.store.event("info", "Study complete: scheduled end date reached")
            elif state["next_capture_at"] is None or now >= state["next_capture_at"]:
                due = in_window(now, settings)
                # Persist the next due time before capture. Never replay missed intervals after downtime.
                state["next_capture_at"] = next_allowed(now + settings.interval_minutes * 60 if due else now, settings)
            else:
                return
            self.store.put("runtime", state)
        if due:
            await self.take_photo(scheduled=True)

    async def scheduler(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Scheduled capture failed; will retry in one minute within the capture window")
            await asyncio.sleep(1)

    def status(self):
        frames = self.store.rows("SELECT COUNT(*) AS count, COALESCE(SUM(bytes),0) AS bytes, MIN(captured_at) AS first_at, MAX(captured_at) AS last_at, COALESCE(SUM(excluded=0),0) AS included, COALESCE(SUM(excluded=1),0) AS excluded FROM frames")[0]
        latest = self.store.rows("SELECT * FROM frames ORDER BY captured_at DESC LIMIT 1")
        disk = shutil.disk_usage(self.root)
        return {"settings": self.settings().model_dump(), "runtime": self.store.get("runtime"),
                "frames": frames, "latest": latest[0] if latest else None, "demo": self.demo,
                "camera_busy": self.camera_lock.locked(), "disk": {"free": disk.free, "total": disk.total},
                "server_time": time.time()}

    def frame_range(self, db, cutoff, start_frame_id=None, end_frame_id=None):
        predicate, params = "captured_at<=?", [cutoff]
        bounds = []
        for frame_id, operator in ((start_frame_id, ">="), (end_frame_id, "<=")):
            if frame_id is None:
                bounds.append(None)
                continue
            row = db.execute("SELECT captured_at,id FROM frames WHERE id=? AND captured_at<=?", (frame_id, cutoff)).fetchone()
            if row is None:
                raise ValueError("An export range endpoint is no longer available in this timeline. Choose the full range or new endpoints.")
            bound = (row["captured_at"], row["id"])
            bounds.append(bound)
            predicate += f" AND (captured_at,id){operator}(?,?)"
            params.extend(bound)
        if all(bound is not None for bound in bounds) and bounds[0] > bounds[1]:
            raise ValueError("The export range start must be before or equal to its end.")
        return predicate, params, bounds[0]

    def timeline(self, cutoff=None, offset=0, limit=24, included_only=False, start_frame_id=None, end_frame_id=None):
        cutoff = min(time.time(), cutoff) if cutoff is not None else time.time()
        with self.store.connect() as db:
            db.execute("BEGIN")
            counts = dict(db.execute("SELECT COUNT(*) AS total, COALESCE(SUM(excluded=0),0) AS included, COALESCE(SUM(excluded=1),0) AS excluded FROM frames WHERE captured_at<=?", (cutoff,)).fetchone())
            predicate, params, start = self.frame_range(db, cutoff, start_frame_id, end_frame_id)
            range_count = db.execute(f"SELECT COUNT(*) FROM frames WHERE {predicate} AND excluded=0", params).fetchone()[0]
            range_start = db.execute("SELECT COUNT(*) FROM frames WHERE captured_at<=? AND excluded=0 AND (captured_at,id)<(?,?)", (cutoff, *start)).fetchone()[0] if start else 0
            rows = db.execute("SELECT id,captured_at,excluded, SUM(excluded=0) OVER (ORDER BY captured_at,id ROWS UNBOUNDED PRECEDING)-1 AS video_index FROM frames WHERE captured_at<=? " + ("AND excluded=0 " if included_only else "") + "ORDER BY captured_at,id LIMIT ? OFFSET ?", (cutoff, limit, offset))
            return {"cutoff": cutoff, **counts, "range": {"included": range_count, "start_index": range_start, "end_index": range_start + range_count - 1}, "frames": [dict(row) for row in rows]}

    def select_frames(self, frame_ids, excluded):
        self.ensure_available()
        ids = list(set(frame_ids))
        marks = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute(f"SELECT COUNT(*) FROM frames WHERE id IN ({marks})", ids).fetchone()[0]
            if found != len(ids):
                raise ValueError("Some selected frames do not belong to this project. Refresh the frame list.")
            db.execute(f"UPDATE frames SET excluded=? WHERE id IN ({marks})", [int(excluded), *ids])
        self.store.event("info", f"{len(ids)} frame(s) {'removed from' if excluded else 'restored to'} future videos")

    def cleanup_deleted_frames(self):
        for row in self.store.rows("SELECT id FROM deleted_frames"):
            frame_id = row["id"]
            if not re.fullmatch(r"[0-9a-f]{32}", frame_id):
                raise RuntimeError("Invalid photo ID in deletion queue")
            for kind in ("frames", "thumbs", "previews"):
                (self.root / kind / f"{frame_id}.jpg").unlink(missing_ok=True)
            with self.store.connect() as db:
                db.execute("DELETE FROM deleted_frames WHERE id=?", (frame_id,))

    async def delete_frame(self, frame_id):
        # Serialize with captures; exports must keep their queued frame snapshots.
        async with self.camera_lock, self.preview_lock:
            self.ensure_available()
            if self.store.rows("SELECT id FROM exports WHERE status IN ('queued','running') LIMIT 1"):
                raise ValueError("Wait for the current video export to finish before deleting photos")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT id FROM frames WHERE id=?", (frame_id,)).fetchone():
                    raise KeyError(frame_id)
                db.execute("INSERT INTO deleted_frames VALUES (?)", (frame_id,))
                db.execute("DELETE FROM export_frames WHERE frame_id=?", (frame_id,))
                db.execute("DELETE FROM frames WHERE id=?", (frame_id,))
            # Commit the cleanup intent first so a restart finishes interrupted deletes.
            self.cleanup_deleted_frames()
            self.store.event("info", "Photo permanently deleted")

    def preview_sync(self, frame_id):
        if not self.store.rows("SELECT id FROM frames WHERE id=?", (frame_id,)):
            raise KeyError(frame_id)
        source_path = self.root / "frames" / f"{frame_id}.jpg"
        target = self.root / "previews" / f"{frame_id}.jpg"
        if not target.is_file():
            if not source_path.is_file():
                raise KeyError(frame_id)
            self.check_space()
            temporary = target.with_name(f".{frame_id}.jpg")
            try:
                with Image.open(source_path) as source:
                    # JPEG can decode at reduced resolution before allocating the
                    # full sensor image; keep enough pixels for the sharp preview.
                    source.draft("RGB", (1920, 1920))
                    with ImageOps.exif_transpose(source) as image:
                        image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                        image.convert("RGB").save(temporary, "JPEG", quality=90)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target.read_bytes()

    async def preview_frame(self, frame_id):
        # Bound decoding memory, and finish in-flight work before deleting files.
        async with self.preview_lock:
            self.ensure_available()
            task = asyncio.create_task(asyncio.to_thread(self.preview_sync, frame_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def create_export(self, cutoff=None, normalize_lighting=False, *, interpolation="none", intermediate_frames=5, fps=None, start_frame_id=None, end_frame_id=None, resolution="project", timing_overlay=False, cinematic_focus=False):
        options = ExportRequest(cutoff=cutoff, normalize_lighting=normalize_lighting,
                                interpolation=interpolation, intermediate_frames=intermediate_frames, fps=fps,
                                start_frame_id=start_frame_id, end_frame_id=end_frame_id, resolution=resolution,
                                timing_overlay=timing_overlay, cinematic_focus=cinematic_focus)
        normalize_lighting = options.normalize_lighting or options.cinematic_focus
        self.ensure_available()
        # No await between checking and registering the task: requests cannot overlap here.
        if self.export_task and not self.export_task.done():
            raise ValueError("An export is already running")
        self.check_space()
        cutoff = min(time.time(), cutoff) if cutoff is not None else time.time()
        settings = self.settings()
        settings.export_fps = options.fps or settings.export_fps
        if options.resolution != "project":
            settings.width, settings.height = {"720p": (1280, 720), "1080p": (1920, 1080),
                                               "2160p": (3840, 2160)}[options.resolution]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            predicate, params, _ = self.frame_range(db, cutoff, options.start_frame_id, options.end_frame_id)
            count = db.execute(f"SELECT COUNT(*) FROM frames WHERE {predicate} AND excluded=0", params).fetchone()[0]
            if not count:
                raise ValueError("No frames are included in this video range. Choose a wider range, capture a photo or restore a removed frame.")
            job = {"id": uuid.uuid4().hex, "created_at": time.time(), "status": "queued", "frames": count, "fps": settings.export_fps, "error": None, "normalize_lighting": normalize_lighting,
                   "interpolation": options.interpolation, "intermediate_frames": options.intermediate_frames if options.interpolation != "none" else 0,
                   "start_frame_id": options.start_frame_id, "end_frame_id": options.end_frame_id, "timing_overlay": options.timing_overlay,
                   "cinematic_focus": options.cinematic_focus}
            db.execute("INSERT INTO exports(id,created_at,status,frames,fps,error,settings,snapshot,normalize_lighting,interpolation,intermediate_frames,start_frame_id,end_frame_id,timing_overlay,cinematic_focus) VALUES (:id,:created_at,:status,:frames,:fps,:error,:settings,1,:normalize_lighting,:interpolation,:intermediate_frames,:start_frame_id,:end_frame_id,:timing_overlay,:cinematic_focus)", {**job, "settings": settings.model_dump_json()})
            db.execute(f"INSERT INTO export_frames SELECT ?,id FROM frames WHERE {predicate} AND excluded=0", (job["id"], *params))
        self.export_task = asyncio.create_task(self.run_exports())
        return export_details({**job, "settings": settings.model_dump_json()})

    async def run_exports(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.export_gate.acquire(), timeout=1)
                break
            except TimeoutError:
                pass
        else:
            return
        try:
            for job in self.store.rows("SELECT * FROM exports WHERE status='queued' ORDER BY created_at"):
                if self.stopping.is_set():
                    break
                settings = Settings.model_validate_json(job["settings"]) if job["settings"] else self.settings()
                settings.export_fps = job["fps"]
                with self.store.connect() as db:
                    db.execute("UPDATE exports SET status='running', error=NULL, progress=NULL WHERE id=?", (job["id"],))
                await asyncio.to_thread(self.export_sync, job, settings)
        finally:
            self.export_gate.release()

    def export_sync(self, job, settings):
        directory = self.root / "exports"
        manifest = directory / f"{job['id']}.txt"
        temp = directory / f".{job['id']}.mp4"
        log = directory / f".{job['id']}.log"
        progress_file = directory / f".{job['id']}.progress"
        final = directory / f"{job['id']}.mp4"
        corrected = directory / f".{job['id']}.lighting"
        overlay = directory / f".{job['id']}.timing.ass"
        deadline = time.monotonic() + 21600
        progress = ExportProgress(self.store, job['id'])

        def check_export():
            if self.stopping.is_set():
                raise InterruptedError("Export will resume after restart")
            self.check_space()
            if time.monotonic() >= deadline:
                raise RuntimeError("Export exceeded the six-hour time limit")

        try:
            query = "SELECT f.id,f.captured_at FROM export_frames e JOIN frames f ON f.id=e.frame_id WHERE e.export_id=? ORDER BY f.captured_at,f.id" if job.get("snapshot") else "SELECT id,captured_at FROM frames WHERE captured_at<=? ORDER BY captured_at,id"
            rows = self.store.rows(query, (job["id"] if job.get("snapshot") else job["created_at"],))
            if job.get("normalize_lighting"):
                corrected.mkdir()
                lighting.prepare_frames([self.root / "frames" / f"{row['id']}.jpg" for row in rows],
                                        corrected, check_export, progress.report)
                if job.get("cinematic_focus"):
                    cinematic.prepare_frames([corrected / f"{row['id']}.png" for row in rows], check_export, progress.report)
            with manifest.open("w") as handle:
                for row in rows:
                    # Relative paths contain only internally generated hex IDs.
                    path = f"{corrected.name}/{row['id']}.png" if job.get("normalize_lighting") else f"../frames/{row['id']}.jpg"
                    handle.write(f"file '{path}'\n")
            factor = job.get("intermediate_frames", 0) + 1 if job.get("interpolation", "none") != "none" and job["frames"] > 1 else 1
            filters = export_filters(job, settings)
            if job.get("timing_overlay"):
                bounds = timing_overlay.photo_bounds([self.root / "frames" / f"{row['id']}.jpg" for row in rows], settings, check_export)
                timing_overlay.write_overlay(overlay, [row['captured_at'] for row in rows], job, settings, check_export, progress.report, bounds)
                # Escape both filter-option and filtergraph parsing layers.
                filename = str(overlay.resolve()).replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
                filename = filename.replace('\\', '\\\\').replace("'", "\\'").replace(',', '\\,').replace(';', '\\;').replace('[', '\\[').replace(']', '\\]')
                filters += f",subtitles=filename={filename}"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-nostats", "-stats_period", "1", "-progress", str(progress_file),
                       "-r", f"{job['fps']}/{factor}", "-f", "concat", "-safe", "0", "-i", str(manifest),
                       "-vf", filters, "-r", str(job["fps"]),
                       "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", str(temp)]
            # Keep FFmpeg errors out of RAM and check the disk reserve during long exports.
            total = export_details(job)['output_frames']
            progress.report('encoding', 0, total)
            reader = FFmpegProgress(progress.report, total)
            progress_file.touch()
            with progress_file.open() as updates, log.open("wb") as errors, subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors) as process:
                try:
                    while process.poll() is None:
                        reader.read(updates)
                        check_export()
                        time.sleep(.5)
                    reader.read(updates)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                if process.returncode:
                    with log.open("rb") as error_log:
                        error_log.seek(max(0, log.stat().st_size - 1500))
                        raise RuntimeError(error_log.read().decode(errors="replace"))
            progress.report('finalizing', 0, 0)
            temp.replace(final)
            with self.store.connect() as db:
                db.execute("UPDATE exports SET status='complete', progress=NULL WHERE id=?", (job["id"],))
            self.store.event("info", f"MP4 ready: {export_details(job)['output_frames']} video frames from {job['frames']} photos at {job['fps']} fps")
        except Exception as exc:
            with self.store.connect() as db:
                db.execute("UPDATE exports SET status=?, error=?, progress=NULL WHERE id=?", ("queued" if isinstance(exc, InterruptedError) else "failed", None if isinstance(exc, InterruptedError) else str(exc), job["id"]))
            if not isinstance(exc, InterruptedError):
                self.store.event("error", "Export failed: " + str(exc))
        finally:
            manifest.unlink(missing_ok=True)
            temp.unlink(missing_ok=True)
            log.unlink(missing_ok=True)
            progress_file.unlink(missing_ok=True)
            overlay.unlink(missing_ok=True)
            if corrected.exists():
                shutil.rmtree(corrected)
