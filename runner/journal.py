"""Single-slot durable intent/result journal. Never replays execution after a crash."""
import json
from pathlib import Path
import sqlite3


class Journal:
    def __init__(self, directory):
        import fcntl
        self.root = Path(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = (self.root / "runner.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise RuntimeError("Another Runner already owns this journal")
        self.conn = sqlite3.connect(self.root / "journal.sqlite3")
        (self.root / "journal.sqlite3").chmod(0o600)
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        self.conn.commit()

    def load(self):
        row = self.conn.execute("SELECT body FROM journal WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state):
        data = json.dumps(state)
        if len(data.encode()) > 262144:
            raise ValueError("journal_size_limit")
        with self.conn:
            self.conn.execute("INSERT INTO journal VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (data,))

    def clear(self):
        with self.conn:
            self.conn.execute("DELETE FROM journal")

    def close(self):
        self.conn.close()
        self.lock.close()
