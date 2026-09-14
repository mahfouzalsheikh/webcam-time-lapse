import asyncio
import fcntl
import re
import shutil
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
        self.deletions = {}
        with self.connect() as db:
            initialized = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at REAL NOT NULL)")
            if "deleting" not in {row[1] for row in db.execute("PRAGMA table_info(projects)")}:
                db.execute("ALTER TABLE projects ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0")
            # Retain the original data in place. This migration is safe to repeat.
            if not initialized:
                db.execute("INSERT INTO projects(id,created_at) VALUES ('default', ?)", (time.time(),))
            for row in db.execute("SELECT id FROM projects WHERE deleting=0 ORDER BY created_at"):
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
        recorder = self.recorders.get(project_id)
        return recorder if recorder and not recorder.deleting else None

    def cleanup_project(self, project_id):
        if project_id == 'default':
            # The legacy project shares the catalog root with all other projects.
            for folder in ('frames', 'thumbs', 'exports'):
                path = self.root / folder
                if path.exists():
                    shutil.rmtree(path)
            for filename in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm', 'state.sqlite3-journal', 'recorder.lock'):
                (self.root / filename).unlink(missing_ok=True)
        else:
            if not re.fullmatch(r'[0-9a-f]{32}', project_id):
                raise RuntimeError('Invalid project ID in deletion queue')
            path = self.root / 'projects' / project_id
            if path.exists():
                shutil.rmtree(path)
        with self.connect() as db:
            db.execute('DELETE FROM projects WHERE id=? AND deleting=1', (project_id,))

    async def delete(self, project_id):
        recorder = self.recorders.get(project_id)
        if recorder is None:
            raise KeyError(project_id)
        if project_id in self.deletions and not self.deletions[project_id].done():
            raise ValueError('This project is already being deleted')
        with self.connect() as db:
            db.execute('UPDATE projects SET deleting=1 WHERE id=?', (project_id,))
        recorder.deleting = True
        task = asyncio.create_task(self._delete(project_id, recorder))
        self.deletions[project_id] = task
        # Complete cleanup even if the browser disconnects while waiting.
        await asyncio.shield(task)

    async def _delete(self, project_id, recorder):
        await recorder.stop()
        await asyncio.to_thread(self.cleanup_project, project_id)
        self.recorders.pop(project_id, None)

    async def start(self):
        self.owner = (self.root / "projects.lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            raise RuntimeError("This data volume already has a recorder. Run exactly one worker.")
        started = []
        try:
            with self.connect() as db:
                pending = [row[0] for row in db.execute('SELECT id FROM projects WHERE deleting=1')]
            for project_id in pending:
                await asyncio.to_thread(self.cleanup_project, project_id)
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
        await asyncio.gather(*self.deletions.values(), return_exceptions=True)
        await asyncio.gather(*(recorder.stop() for recorder in self.recorders.values() if not recorder.deleting))
        if self.owner:
            self.owner.close()

    async def create(self, settings: Settings):
        project_id = uuid.uuid4().hex
        recorder = self.make_recorder(project_id)
        await recorder.save_settings(settings)
        with self.connect() as db:
            db.execute("INSERT INTO projects(id,created_at) VALUES (?,?)", (project_id, time.time()))
        self.recorders[project_id] = recorder
        if self.active:
            await recorder.start()
        return {"id": project_id, **recorder.status()}

    def list(self):
        return [{"id": key, **recorder.status()} for key, recorder in self.recorders.items() if not recorder.deleting]
