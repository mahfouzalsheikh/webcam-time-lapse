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

from . import camera, dslr
from .models import Settings
from .store import Store

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
        self.export_task = None
        self.scheduler_task = None
        self.owner = None

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
        for folder, pattern in (("frames", r"\.[0-9a-f]{32}\.jpg"),
                                ("exports", r"\.?[0-9a-f]{32}\.(txt|log)|\.[0-9a-f]{32}\.mp4")):
            for path in (self.root / folder).iterdir():
                if path.is_file() and re.fullmatch(pattern, path.name):
                    path.unlink()
        with self.store.connect() as db:
            db.execute("UPDATE exports SET status='queued', error=NULL WHERE status='running'")
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
        if self.export_task:
            await self.export_task
        if self.owner:
            self.owner.close()

    async def save_settings(self, settings):
        if not self.demo:
            settings = settings.model_copy(update={"camera_device": dslr.stable_device(settings.camera_device)})
        async with self.state_lock:
            state = self.store.get("runtime")
            if state["started_at"]:
                state["ends_at"] = state["started_at"] + settings.duration_days * 86400
            if state["running"]:
                state["next_capture_at"] = next_allowed(time.time() + settings.interval_minutes * 60, settings)
            self.store.put_many({"settings": settings.model_dump(), "runtime": state})
            self.store.event("info", "Capture settings updated")

    async def set_running(self, running):
        async with self.state_lock:
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

    def timeline(self, cutoff=None, offset=0, limit=24, included_only=False):
        cutoff = min(time.time(), cutoff) if cutoff is not None else time.time()
        with self.store.connect() as db:
            db.execute("BEGIN")
            counts = dict(db.execute("SELECT COUNT(*) AS total, COALESCE(SUM(excluded=0),0) AS included, COALESCE(SUM(excluded=1),0) AS excluded FROM frames WHERE captured_at<=?", (cutoff,)).fetchone())
            rows = db.execute("SELECT id,captured_at,excluded, SUM(excluded=0) OVER (ORDER BY captured_at,id ROWS UNBOUNDED PRECEDING)-1 AS video_index FROM frames WHERE captured_at<=? " + ("AND excluded=0 " if included_only else "") + "ORDER BY captured_at,id LIMIT ? OFFSET ?", (cutoff, limit, offset))
            return {"cutoff": cutoff, **counts, "frames": [dict(row) for row in rows]}

    def select_frames(self, frame_ids, excluded):
        ids = list(set(frame_ids))
        marks = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute(f"SELECT COUNT(*) FROM frames WHERE id IN ({marks})", ids).fetchone()[0]
            if found != len(ids):
                raise ValueError("Some selected frames do not belong to this project. Refresh the frame list.")
            db.execute(f"UPDATE frames SET excluded=? WHERE id IN ({marks})", [int(excluded), *ids])
        self.store.event("info", f"{len(ids)} frame(s) {'removed from' if excluded else 'restored to'} future videos")

    async def create_export(self, cutoff=None):
        # No await between checking and registering the task: requests cannot overlap here.
        if self.export_task and not self.export_task.done():
            raise ValueError("An export is already running")
        self.check_space()
        cutoff = min(time.time(), cutoff) if cutoff is not None else time.time()
        settings = self.settings()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM frames WHERE captured_at<=? AND excluded=0", (cutoff,)).fetchone()[0]
            if not count:
                raise ValueError("No frames are included in this video. Capture a photo or restore a removed frame.")
            job = {"id": uuid.uuid4().hex, "created_at": time.time(), "status": "queued", "frames": count, "fps": settings.export_fps, "error": None}
            db.execute("INSERT INTO exports(id,created_at,status,frames,fps,error,settings,snapshot) VALUES (:id,:created_at,:status,:frames,:fps,:error,:settings,1)", {**job, "settings": settings.model_dump_json()})
            db.execute("INSERT INTO export_frames SELECT ?,id FROM frames WHERE captured_at<=? AND excluded=0", (job["id"], cutoff))
        self.export_task = asyncio.create_task(self.run_exports())
        return job

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
                    db.execute("UPDATE exports SET status='running', error=NULL WHERE id=?", (job["id"],))
                await asyncio.to_thread(self.export_sync, job, settings)
        finally:
            self.export_gate.release()

    def export_sync(self, job, settings):
        directory = self.root / "exports"
        manifest = directory / f"{job['id']}.txt"
        temp = directory / f".{job['id']}.mp4"
        log = directory / f".{job['id']}.log"
        final = directory / f"{job['id']}.mp4"
        try:
            with manifest.open("w") as handle, self.store.connect() as db:
                query = "SELECT f.id FROM export_frames e JOIN frames f ON f.id=e.frame_id WHERE e.export_id=? ORDER BY f.captured_at,f.id" if job.get("snapshot") else "SELECT id FROM frames WHERE captured_at<=? ORDER BY captured_at,id"
                for row in db.execute(query, (job["id"] if job.get("snapshot") else job["created_at"],)):
                    # Relative paths contain only internally generated hex IDs.
                    handle.write(f"file '../frames/{row['id']}.jpg'\n")
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-r", str(job["fps"]), "-f", "concat", "-safe", "0", "-i", str(manifest),
                       "-vf", f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease,pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                       "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", str(temp)]
            # Keep FFmpeg errors out of RAM and check the disk reserve during long exports.
            with log.open("wb") as errors, subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors) as process:
                deadline = time.monotonic() + 21600
                try:
                    while process.poll() is None:
                        if self.stopping.is_set():
                            raise InterruptedError("Export will resume after restart")
                        self.check_space()
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Export exceeded the six-hour time limit")
                        time.sleep(.5)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                if process.returncode:
                    with log.open("rb") as error_log:
                        error_log.seek(max(0, log.stat().st_size - 1500))
                        raise RuntimeError(error_log.read().decode(errors="replace"))
            temp.replace(final)
            with self.store.connect() as db:
                db.execute("UPDATE exports SET status='complete' WHERE id=?", (job["id"],))
            self.store.event("info", f"MP4 ready: {job['frames']} frames at {job['fps']} fps")
        except Exception as exc:
            with self.store.connect() as db:
                db.execute("UPDATE exports SET status=?, error=? WHERE id=?", ("queued" if isinstance(exc, InterruptedError) else "failed", None if isinstance(exc, InterruptedError) else str(exc), job["id"]))
            if not isinstance(exc, InterruptedError):
                self.store.event("error", "Export failed: " + str(exc))
        finally:
            manifest.unlink(missing_ok=True)
            temp.unlink(missing_ok=True)
            log.unlink(missing_ok=True)
