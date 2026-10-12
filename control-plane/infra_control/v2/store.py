"""Narrow SQL operations. Callers own transaction and lock ordering."""

import hashlib
import json
from ..db import encode
from ..service import Fault


def lock(tx, key):
    if not tx.sqlite:
        number = int.from_bytes(
            hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
        )
        tx.execute("SELECT pg_advisory_xact_lock(?)", (number,))


def object_get(tx, kind, key):
    row = tx.execute(
        "SELECT body FROM v2_objects WHERE kind=? AND id=?", (kind, key)
    ).fetchone()
    return json.loads(row[0]) if row else None


def object_put(tx, kind, key, value):
    tx.execute(
        "INSERT INTO v2_objects(kind,id,body) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
        (kind, key, encode(value)),
    )


def get_task(tx, task_id, actor=None, locked=True):
    suffix = " FOR UPDATE" if locked and not tx.sqlite else ""
    row = tx.execute(
        "SELECT body FROM v2_tasks WHERE id=?" + suffix, (task_id,)
    ).fetchone()
    if not row:
        raise Fault("not_found", 404)
    task = json.loads(row[0])
    if actor and actor["role"] != "admin" and actor["id"] != task["owner"]:
        raise Fault("not_found", 404)
    return task


def save_task(tx, task):
    tx.execute(
        "INSERT INTO v2_tasks(id,owner,pool,state,ready,created,deadline,version,body,archived) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,ready=excluded.ready,version=excluded.version,body=excluded.body,archived=excluded.archived",
        (
            task["id"],
            task["owner"],
            task["pool"],
            task["state"],
            int(task["dispatch_ready"]),
            task["created"],
            task["deadline"],
            task["state_version"],
            encode(task),
            int(task.get("tombstone", False)),
        ),
    )


def get_attempt(tx, attempt_id):
    row = tx.execute(
        "SELECT body FROM v2_attempts WHERE id=?", (attempt_id,)
    ).fetchone()
    if not row:
        raise Fault("not_found", 404)
    return json.loads(row[0])


def save_attempt(tx, attempt):
    tx.execute(
        "INSERT INTO v2_attempts(id,task_id,node_id,slot,active,epoch,body,local_content) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET active=excluded.active,body=excluded.body",
        (
            attempt["id"],
            attempt["task_id"],
            attempt["node_id"],
            attempt["slot"],
            int(attempt["active"]),
            attempt["epoch"],
            encode(attempt),
            int(attempt.get("local_content", False)),
        ),
    )
