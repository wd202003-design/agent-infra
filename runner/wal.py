"""Multi-slot fsync journal. Unacknowledged critical records never expire."""

import fcntl
import json
from pathlib import Path
import sqlite3
import time


class WAL:
    LIMIT = 64 * 1024 * 1024
    RESERVE = 1024 * 1024

    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = (self.root / "v2.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise RuntimeError("Runner WAL already owned")
        self.conn = sqlite3.connect(self.root / "v2.sqlite3")
        (self.root / "v2.sqlite3").chmod(0o600)
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA max_page_count=16384")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, critical INTEGER NOT NULL, created REAL NOT NULL, body TEXT NOT NULL)"
        )
        self.conn.commit()

    def load(self, key):
        row = self.conn.execute(
            "SELECT body FROM records WHERE id=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, value, *, critical=True):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode()) > 262144:
            raise RuntimeError("WAL record too large")
        used = self.conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM records"
        ).fetchone()[0]
        old = self.conn.execute(
            "SELECT LENGTH(CAST(body AS BLOB)) FROM records WHERE id=?", (key,)
        ).fetchone()
        limit = self.LIMIT if critical else self.LIMIT - self.RESERVE
        if used - (old[0] if old else 0) + len(body.encode()) > limit:
            raise RuntimeError("WAL capacity reached; execution must stop")
        with self.conn:
            self.conn.execute(
                "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET critical=excluded.critical,created=excluded.created,body=excluded.body",
                (key, int(critical), time.time(), body),
            )

    def remove(self, key):
        with self.conn:
            self.conn.execute("DELETE FROM records WHERE id=?", (key,))

    def prune(self, now=None):
        with self.conn:
            self.conn.execute(
                "DELETE FROM records WHERE critical=0 AND created<?",
                ((now or time.time()) - 86400,),
            )

    def close(self):
        self.conn.close()
        self.lock.close()
