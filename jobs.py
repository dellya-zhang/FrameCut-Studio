"""Durable local job queue and stage checkpoints for the workstation."""
import fcntl
import hashlib
import json
import logging
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from agent import analyze, edit
from framecut import duration, export, index_video
from visual import scan

LOGGER = logging.getLogger(__name__)


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL,
                    state TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL
                )
            """)

    def connect(self):
        db = sqlite3.connect(self.root / "workspace.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def folder(self, job_id):
        return self.root / job_id

    def submit(self, kind, spec):
        payload = json.dumps(spec, ensure_ascii=False, sort_keys=True)
        job_id = hashlib.sha256(f"{kind}:{payload}".encode()).hexdigest()
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO jobs (id, kind, spec, state, created, updated)"
                       " VALUES (?, ?, ?, 'queued', ?, ?)", (job_id, kind, payload, now, now))
        return job_id

    def ingest(self, upload):
        upload.seek(0)
        digest = hashlib.sha256()
        assets = self.root / "assets"
        assets.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=assets, delete=False) as temp:
            scratch = Path(temp.name)
            try:
                while chunk := upload.read(1024 * 1024):
                    digest.update(chunk)
                    temp.write(chunk)
                temp.flush()
                target = assets / (digest.hexdigest() + Path(upload.name).suffix.lower())
                scratch.replace(target)
            finally:
                scratch.unlink(missing_ok=True)
                upload.seek(0)
        return str(target)

    def list(self, kind=None):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE (? IS NULL OR kind = ?) ORDER BY created DESC",
                (kind, kind),
            )]

    def get(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def retry(self, job_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET state='queued', error='', updated=?"
                       " WHERE id=? AND state='failed'", (time.time(), job_id))

    def result(self, job_id):
        return json.loads((self.folder(job_id) / "result.json").read_text(encoding="utf-8"))

    def update(self, job_id, state, phase, error=""):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET state=?, phase=?, error=?, updated=? WHERE id=?",
                       (state, phase, error, time.time(), job_id))

    def process(self, job):
        job_id, kind = job["id"], job["kind"]
        spec = json.loads(job["spec"])
        folder = self.folder(job_id)
        folder.mkdir(exist_ok=True)

        def checkpoint(name, action, phase=None):
            self.update(job_id, "running", phase or name)
            path = folder / f"{name}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            value = action()
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
            return value

        video = Path(spec["video"])
        if kind == "index":
            length = checkpoint("probe", lambda: duration(video))
            captions = Path(spec["captions"]) if spec["captions"] else None
            text = checkpoint("transcript", lambda: index_video(
                video, captions, spec["model"], skip_speech=spec["skip_speech"],
                speech_cache=folder / "audio-chunks")["cues"])
            if spec["ocr"]:
                text = text + checkpoint("ocr", lambda: index_video(
                    video, ocr=spec["ocr"], skip_speech=True)["cues"])
            frames = checkpoint("visual", lambda: scan(video, length))
            checkpoint("result", lambda: {
                "duration": length, "cues": sorted(text, key=lambda c: c["start"]), "visual": frames,
            })
        elif kind == "plan":
            index = self.result(spec["index"])
            checkpoint("result", lambda: edit(video, index, spec["goal"], spec["count"],
                                               spec["seconds"], spec["vision"]), "planning")
        elif kind == "question":
            index = self.result(spec["index"])
            checkpoint("result", lambda: analyze(index, spec["question"], spec["mode"],
                                                  spec["history"], spec["model"], spec["endpoint"]), "answering")
        elif kind == "export":
            output = folder / "highlights.mp4"
            self.update(job_id, "running", "render")
            if not output.exists():
                export(video, spec["clips"], output, spec["vertical"])
            checkpoint("result", lambda: {"file": str(output)})
        else:
            raise ValueError(f"未知任务类型 {kind}")

    def run_once(self):
        """Consume one task. Call only while holding the workspace worker lock."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if task is None:
                return False
            db.execute("UPDATE jobs SET state='running', attempts=attempts+1, updated=? WHERE id=?",
                       (time.time(), task["id"]))
        try:
            self.process(task)
            self.update(task["id"], "done", "complete")
        except Exception as error:
            LOGGER.exception("Job %s failed", task["id"])
            state = self.get(task["id"])
            self.update(task["id"], "failed", state["phase"], str(error) or type(error).__name__)
        return True

    def start(self):
        def loop():
            with (self.root / "worker.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE jobs SET state='queued', updated=? WHERE state='running'",
                               (time.time(),))
                while True:
                    try:
                        if not self.run_once():
                            time.sleep(0.5)
                    except sqlite3.Error:
                        LOGGER.exception("Unable to poll jobs")
                        time.sleep(2)

        threading.Thread(target=loop, name="framecut-worker", daemon=True).start()
        return self
