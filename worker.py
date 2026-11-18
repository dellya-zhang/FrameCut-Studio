"""Run one processing slot that fairly visits active tenant workspaces."""
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from jobs import Workspace

try:
    from psycopg import Error as PostgresError
except ImportError:
    PostgresError = sqlite3.Error

LOGGER = logging.getLogger(__name__)


def serve(root):
    root = Path(root).expanduser().resolve()
    tenants = root / "tenants"
    known = {}
    while True:
        did_work = False
        folders = [root, *sorted(tenants.glob("*/"))]
        for folder in folders:
            if not any((folder / marker).exists() for marker in ("workspace.sqlite3", ".postgres")):
                continue
            try:
                if folder not in known:
                    known[folder] = Workspace(folder)
                did_work = known[folder].run_once() or did_work
            except (sqlite3.Error, PostgresError, OSError):
                LOGGER.exception("Could not poll workspace %s", folder)
        if not did_work:
            time.sleep(0.5)


def start(root, concurrency):
    for number in range(concurrency):
        threading.Thread(target=serve, args=(root,), daemon=True,
                         name=f"framecut-worker-{number}").start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve(os.getenv("FRAMECUT_WORKSPACE", ".framecut"))
