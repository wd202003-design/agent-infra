import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


TERMINAL = {'succeeded', 'failed', 'cancelled', 'needs_review'}


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL,
                    input_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    provider TEXT NOT NULL, state TEXT NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 0, sandbox_id TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, result TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL, kind TEXT NOT NULL,
                    detail TEXT NOT NULL, created REAL NOT NULL
                );
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, task_id, kind, detail=''):
        db.execute('INSERT INTO events(task_id,kind,detail,created) VALUES(?,?,?,?)',
                   (task_id, kind, detail, time.time()))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        task = dict(row)
        task.pop('idem')
        task.pop('input_hash')
        task['payload'] = json.loads(task['payload'])
        task['result'] = json.loads(task['result']) if task['result'] else None
        return task

    def submit(self, key, payload, provider, max_pending=100):
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256((provider + encoded).encode()).hexdigest()
        with self.db() as db:
            row = db.execute('SELECT * FROM tasks WHERE idem=?', (key,)).fetchone()
            if row:
                if row['input_hash'] != digest:
                    raise Conflict('idempotency_key_reused_with_different_input')
                return self.decode(row), False
            count = db.execute("SELECT count(*) FROM tasks WHERE state NOT IN ('succeeded','failed','cancelled','needs_review')").fetchone()[0]
            if count >= max_pending:
                raise Conflict('queue_full')
            task_id, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO tasks(id,idem,input_hash,payload,provider,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, key, digest, encoded, provider, 'queued', now, now))
            self.event(db, task_id, 'queued', provider)
            return self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()), True

    def get(self, task_id):
        with self.db() as db:
            return self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def list(self):
        with self.db() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT 100')]

    def events(self, task_id):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq', (task_id,))]

    def claim(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET state='running',epoch=epoch+1,updated=? WHERE id=?", (time.time(), row['id']))
            self.event(db, row['id'], 'running')
            return self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())

    def bind(self, task_id, epoch, sandbox_id):
        with self.db() as db:
            changed = db.execute("UPDATE tasks SET sandbox_id=?,updated=? WHERE id=? AND epoch=? AND state IN ('running','cancel_requested')",
                                 (sandbox_id, time.time(), task_id, epoch)).rowcount
            if changed != 1:
                raise Conflict('stale_attempt')
            self.event(db, task_id, 'sandbox_created', sandbox_id)

    def cancel(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return False
            state = row['state']
            if state in TERMINAL or state == 'cancel_requested':
                return True
            target = 'cancelled' if state == 'queued' else 'cancel_requested'
            db.execute('UPDATE tasks SET state=?,updated=? WHERE id=?', (target, time.time(), task_id))
            self.event(db, task_id, target)
            return True

    def finish(self, task_id, epoch, state, result):
        if state not in TERMINAL:
            raise ValueError('terminal_state_required')
        with self.db() as db:
            row = db.execute('SELECT state,epoch FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None or row['epoch'] != epoch or row['state'] not in {'running', 'cancel_requested'}:
                raise Conflict('stale_attempt')
            # Cancellation racing with completion wins only after confirmed cleanup.
            if row['state'] == 'cancel_requested' and state != 'needs_review':
                state = 'cancelled'
            db.execute('UPDATE tasks SET state=?,result=?,updated=? WHERE id=?',
                       (state, json.dumps(result), time.time(), task_id))
            self.event(db, task_id, state)

    def recover(self):
        with self.db() as db:
            rows = db.execute("SELECT id FROM tasks WHERE state IN ('running','cancel_requested')").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET state='needs_review',epoch=epoch+1,updated=?,result=? WHERE id=?",
                           (time.time(), json.dumps({'reason': 'service_restarted_execution_outcome_unknown'}), row['id']))
                self.event(db, row['id'], 'needs_review', 'restart_no_automatic_replay')
