"""Durable local job queue and stage checkpoints for the workstation."""
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager, nullcontext
from pathlib import Path

from agent import analyze, edit
from database import Postgres
from framecut import duration, export, index_video
from visual import scan
from retrieval import build as build_vectors

LOGGER = logging.getLogger(__name__)


class QuotaExceeded(ValueError):
    """The workspace cannot accept the requested work."""


@contextmanager
def exclusive(path):
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend = None
        if os.getenv("FRAMECUT_DATABASE_URL"):
            tenant = self.root.name if self.root.parent.name == "tenants" else "local"
            self.backend = Postgres(tenant)
            self.backend.initialize()
        with closing(self.connect()) as db, db:
            if not self.backend:
                db.execute("PRAGMA journal_mode=WAL")
            else:
                db.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema())::bigint)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL,
                    state TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS model_budget (
                    day TEXT PRIMARY KEY, used INTEGER NOT NULL
                )
            """)
        if self.backend:
            (self.root / ".postgres").touch()

    def connect(self):
        if self.backend:
            return self.backend.connect()
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
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                return job_id
            active = db.execute("SELECT count(*) AS active FROM jobs WHERE state IN ('queued','running')").fetchone()["active"]
            if active >= int(os.getenv("FRAMECUT_MAX_ACTIVE_JOBS", "24")):
                raise QuotaExceeded("当前素材库的排队与运行任务已达上限。")
            self.reserve_budget(db, kind, spec)
            db.execute("INSERT INTO jobs (id, kind, spec, state, created, updated)"
                       " VALUES (?, ?, ?, 'queued', ?, ?) ON CONFLICT(id) DO NOTHING", (job_id, kind, payload, now, now))
        return job_id

    def ingest(self, upload, *, active_reservation=None):
        staging = self.root / ".uploads"
        staging.mkdir(exist_ok=True)
        guard = exclusive(staging / ".lock") if active_reservation is None else nullcontext()
        with guard:
            return self.ingest_locked(upload, staging, active_reservation)

    def ingest_locked(self, upload, staging, active_reservation):
        upload.seek(0)
        digest = hashlib.sha256()
        assets = self.root / "assets"
        assets.mkdir(exist_ok=True)
        with exclusive(assets / ".lock"), tempfile.NamedTemporaryFile(dir=assets, prefix=".tmp-", delete=False) as temp:
            capacity = int(os.getenv("FRAMECUT_MAX_UPLOAD_BYTES", str(20 * 1024**3)))
            occupied = sum(path.stat().st_size for path in assets.iterdir()
                           if path.is_file() and not path.name.startswith(".") and path != Path(temp.name))
            for marker in staging.glob("*.json"):
                if marker.stem == active_reservation or time.time() - marker.stat().st_mtime > 7 * 86400:
                    continue
                reservation = json.loads(marker.read_text(encoding="utf-8"))
                if not reservation.get("job"):
                    occupied += reservation["length"]
            scratch_size = 0
            scratch = Path(temp.name)
            try:
                while chunk := upload.read(1024 * 1024):
                    scratch_size += len(chunk)
                    if scratch_size > capacity:
                        raise QuotaExceeded("文件大小超过素材库容量。")
                    digest.update(chunk)
                    temp.write(chunk)
                temp.flush()
                target = assets / (digest.hexdigest() + Path(upload.name).suffix.lower())
                if not target.exists():
                    if occupied + scratch_size > capacity:
                        raise QuotaExceeded("素材库上传容量已达上限。")
                    scratch.replace(target)
            finally:
                scratch.unlink(missing_ok=True)
                upload.seek(0)
        return str(target)

    def list(self, kind=None):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE (CAST(? AS TEXT) IS NULL OR kind = ?) ORDER BY created DESC",
                (kind, kind),
            )]

    def get(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def reserve_budget(self, db, kind, spec):
        units = 0
        if kind == "plan":
            units = int(bool(spec.get("vision"))) + int(bool(spec.get("review_model"))) + int(bool(spec.get("planner_model")))
        elif kind == "question":
            units = int(bool(spec.get("model")))
        if not units:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        row = db.execute("SELECT used FROM model_budget WHERE day=?", (day,)).fetchone()
        if (row["used"] if row else 0) + units > int(os.getenv("FRAMECUT_MAX_MODEL_JOBS_PER_DAY", "100")):
            raise QuotaExceeded("今日模型分析额度已用完。")
        db.execute("INSERT INTO model_budget (day,used) VALUES (?,?) ON CONFLICT(day) "
                   "DO UPDATE SET used=model_budget.used+excluded.used", (day, units))

    def retry(self, job_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT count(*) AS active FROM jobs WHERE state IN ('queued','running')").fetchone()["active"]
            if active >= int(os.getenv("FRAMECUT_MAX_ACTIVE_JOBS", "24")):
                raise QuotaExceeded("当前素材库的排队与运行任务已达上限。")
            job = db.execute("SELECT kind, spec FROM jobs WHERE id=? AND state='failed'", (job_id,)).fetchone()
            if job:
                self.reserve_budget(db, job["kind"], json.loads(job["spec"]))
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
            cues = sorted(text, key=lambda c: c["start"])
            semantic = checkpoint("semantic", lambda: build_vectors(cues, spec["embedding"])) \
                if spec.get("embedding") else None
            checkpoint("result", lambda: {
                "duration": length, "cues": cues, "visual": frames, "semantic": semantic,
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

    def try_lock(self, job_id):
        folder = self.folder(job_id)
        folder.mkdir(exist_ok=True)
        with ExitStack() as stack:
            handle = stack.enter_context((folder / ".worker.lock").open("a"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            if self.backend:
                lock = self.backend.try_lock(job_id)
                if lock is None:
                    return None
                stack.callback(lock.close)
            return stack.pop_all()

    def run_once(self):
        """Claim one unlocked task, including work abandoned by a stopped process."""
        lock = None
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                pending = db.execute("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY created")
                task = None
                for row in pending:
                    handle = self.try_lock(row["id"])
                    if handle is None:
                        continue
                    lock, task = handle, dict(row)
                    break
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
        finally:
            if lock is not None:
                lock.close()
