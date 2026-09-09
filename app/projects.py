import asyncio
import fcntl
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import Settings
from .service import Recorder


class Projects:
    """A durable catalog with a separate recorder and data directory per project."""

    def __init__(self, root: Path, demo=False):
        self.root, self.demo = root, demo
        root.mkdir(parents=True, exist_ok=True)
        self.camera_lock, self.export_gate = asyncio.Lock(), asyncio.Lock()
        self.recorders = {}
        self.owner = None
        self.active = False
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at REAL NOT NULL)")
            # Retain the original data in place. This migration is safe to repeat.
            db.execute("INSERT OR IGNORE INTO projects VALUES ('default', ?)", (time.time(),))
            for row in db.execute("SELECT id FROM projects ORDER BY created_at"):
                self.recorders[row[0]] = self.make_recorder(row[0])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "projects.sqlite3", timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def make_recorder(self, project_id):
        path = self.root if project_id == "default" else self.root / "projects" / project_id
        return Recorder(path, self.demo, self.camera_lock, self.export_gate)

    def get(self, project_id):
        return self.recorders.get(project_id)

    async def start(self):
        self.owner = (self.root / "projects.lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            raise RuntimeError("This data volume already has a recorder. Run exactly one worker.")
        started = []
        try:
            for recorder in self.recorders.values():
                await recorder.start()
                started.append(recorder)
            self.active = True
        except BaseException:
            await asyncio.gather(*(recorder.stop() for recorder in started))
            self.owner.close()
            raise

    async def stop(self):
        self.active = False
        await asyncio.gather(*(recorder.stop() for recorder in self.recorders.values()))
        if self.owner:
            self.owner.close()

    async def create(self, settings: Settings):
        project_id = uuid.uuid4().hex
        recorder = self.make_recorder(project_id)
        await recorder.save_settings(settings)
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES (?,?)", (project_id, time.time()))
        self.recorders[project_id] = recorder
        if self.active:
            await recorder.start()
        return {"id": project_id, **recorder.status()}

    def list(self):
        return [{"id": key, **recorder.status()} for key, recorder in self.recorders.items()]
