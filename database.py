"""PostgreSQL queue persistence for deployments with shared media storage."""
import hashlib
import os
from contextlib import closing


def lock_key(value):
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


class Postgres:
    def __init__(self, tenant):
        name = os.getenv("FRAMECUT_DB_NAMESPACE", "framecut")
        digest = hashlib.sha256(f"{name}:{tenant}".encode()).hexdigest()
        self.schema = "framecut_" + digest[:40]
        self.dsn = os.environ["FRAMECUT_DATABASE_URL"]

    def open(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10)

    def initialize(self):
        with closing(self.open()) as db, db.transaction():
            db.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key(self.schema),))
            db.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")

    def connect(self):
        return Connection(self.open(), self.schema)

    def try_lock(self, job_id):
        db = self.open()
        try:
            row = db.execute("SELECT pg_try_advisory_lock(%s) AS acquired",
                             (lock_key(f"{self.schema}:{job_id}"),)).fetchone()
            db.commit()
            if row["acquired"]:
                return db
        except BaseException:
            db.close()
            raise
        db.close()
        return None


class Connection:
    def __init__(self, db, schema):
        self.db = db
        try:
            self.db.execute(f"SET search_path TO {schema}")
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def execute(self, statement, params=()):
        if statement == "BEGIN IMMEDIATE":
            statement = "LOCK TABLE jobs IN SHARE ROW EXCLUSIVE MODE"
        return self.db.execute(statement.replace("?", "%s"), params)

    def close(self):
        self.db.close()

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, *exc):
        self.db.__exit__(*exc)
