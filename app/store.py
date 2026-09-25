import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import Settings


class Store:
    def __init__(self, root: Path):
        self.root = root
        for folder in (root, root / "frames", root / "thumbs", root / "previews", root / "exports"):
            folder.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS frames (
                    id TEXT PRIMARY KEY, captured_at REAL NOT NULL, bytes INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS frames_time ON frames(captured_at);
                CREATE TABLE IF NOT EXISTS deleted_frames (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, created_at REAL NOT NULL, status TEXT NOT NULL,
                    frames INTEGER NOT NULL, fps INTEGER NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS export_frames (
                    export_id TEXT NOT NULL, frame_id TEXT NOT NULL,
                    PRIMARY KEY(export_id, frame_id));
            """)
            if "settings" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN settings TEXT")
            if "snapshot" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN snapshot INTEGER NOT NULL DEFAULT 0")
            if "normalize_lighting" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN normalize_lighting INTEGER NOT NULL DEFAULT 0")
            if "timing_overlay" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN timing_overlay INTEGER NOT NULL DEFAULT 0")
            if "cinematic_focus" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN cinematic_focus INTEGER NOT NULL DEFAULT 0")
            if "cinematic_zoom_percent" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN cinematic_zoom_percent INTEGER NOT NULL DEFAULT 20")
            if "interpolation" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN interpolation TEXT NOT NULL DEFAULT 'none'")
            if "intermediate_frames" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                db.execute("ALTER TABLE exports ADD COLUMN intermediate_frames INTEGER NOT NULL DEFAULT 0")
            for column in ("start_frame_id", "end_frame_id", "progress"):
                if column not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
                    db.execute(f"ALTER TABLE exports ADD COLUMN {column} TEXT")
            if "excluded" not in {row[1] for row in db.execute("PRAGMA table_info(frames)")}:
                db.execute("ALTER TABLE frames ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
            db.execute("INSERT OR IGNORE INTO kv VALUES ('settings', ?)", (Settings().model_dump_json(),))
            db.execute("INSERT OR IGNORE INTO kv VALUES ('runtime', ?)", (json.dumps({
                "running": False, "started_at": None, "ends_at": None,
                "next_capture_at": None, "last_error": None,
            }),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            return json.loads(db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])

    def put(self, key, value):
        self.put_many({key: value})

    def put_many(self, values):
        with self.connect() as db:
            db.executemany("UPDATE kv SET value=? WHERE key=?", [(json.dumps(value), key) for key, value in values.items()])

    def event(self, level, message):
        import time
        with self.connect() as db:
            db.execute("INSERT INTO events(created_at,level,message) VALUES (?,?,?)", (time.time(), level, message))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 200)")

    def rows(self, query, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params)]
