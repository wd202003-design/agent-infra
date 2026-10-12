"""Retention and offline restore fencing. No automatic task replay after restore."""

import argparse
import json
import os
from ..db import Database, encode
from .store import get_task, save_task, object_put, lock
from .artifacts import Artifacts
from .service import TERMINAL


def expire_capsules(platform, tx=None):
    if tx is None:
        with platform.db.transaction(serialized=False) as transaction:
            return expire_capsules(platform, transaction)
    for scope, key in tx.execute(
        "SELECT scope,key FROM v2_idempotency WHERE secret_until<=?",
        (platform.clock(),),
    ).fetchall():
        tx.execute(
            "UPDATE v2_idempotency SET response=?,secret_until=NULL WHERE scope=? AND key=?",
            (encode({"expired": True}), scope, key),
        )


def retention(platform):
    p = platform
    now = p.clock()
    with p.db.transaction(serialized=False) as tx:
        # Event retention invalidates old cursors even when task metadata survives.
        ids = tx.execute(
            "SELECT DISTINCT task_id FROM v2_events WHERE created<? LIMIT 100",
            (now - 30 * 86400,),
        ).fetchall()
        for (task_id,) in ids:
            task = get_task(tx, task_id)
            if task["state"] not in TERMINAL:
                continue
            task["generation"] += 1
            tx.execute(
                "DELETE FROM v2_events WHERE task_id=? AND created<?",
                (task_id, now - 30 * 86400),
            )
            tx.execute(
                "DELETE FROM v2_runner_events WHERE attempt_id IN (SELECT id FROM v2_attempts WHERE task_id=?)",
                (task_id,),
            )
            save_task(tx, task)
        tx.execute("DELETE FROM v2_audit WHERE created<?", (now - 365 * 86400,))
        tx.execute("DELETE FROM v2_idempotency WHERE created<?", (now - 90 * 86400,))
        tx.execute("DELETE FROM v2_credentials WHERE expires<?", (now,))
        tx.execute("DELETE FROM v2_http_keys WHERE created<?", (now - 90 * 86400,))
        expire_capsules(p, tx)
        # Keep unknown money and active resources regardless of age.
        for request_id, task_id in tx.execute(
            "SELECT u.id,u.task_id FROM v2_usage u JOIN v2_tasks t ON t.id=u.task_id WHERE u.created<? AND u.status IN ('complete','unexecuted','reconciled') AND t.state IN ('succeeded','failed','cancelled')",
            (now - 365 * 86400,),
        ).fetchall():
            get_task(tx, task_id)
            tx.execute("DELETE FROM v2_ledger WHERE request_id=?", (request_id,))
            tx.execute("DELETE FROM v2_usage WHERE id=?", (request_id,))
        for (task_id,) in tx.execute(
            "SELECT id FROM v2_tasks WHERE archived=0 AND created<? AND state IN ('succeeded','failed','cancelled') ORDER BY created LIMIT 100",
            (now - 90 * 86400,),
        ).fetchall():
            task = get_task(tx, task_id)
            if task.get("tombstone") or p.unresolved(tx, task_id):
                continue
            Artifacts(p).redact(tx, task)
            keep = {
                key: task[key]
                for key in (
                    "id",
                    "owner",
                    "pool",
                    "state",
                    "state_version",
                    "epoch",
                    "sequence",
                    "created",
                    "created_at",
                    "deadline",
                    "generation",
                )
            }
            keep.update(
                tombstone=True, deleted=True, dispatch_ready=False, active_attempt=None
            )
            save_task(tx, keep)
            tx.execute(
                "INSERT INTO v2_deletions(task_id,requested,body) VALUES(?,?,?) ON CONFLICT(task_id) DO NOTHING",
                (task_id, now, encode({"deadline": now + 86400})),
            )

        stale = [
            json.loads(row[0])
            for row in tx.execute(
                "SELECT body FROM v2_artifacts WHERE (created<? OR (created<? AND status IN ('staging','verifying'))) AND status!='deleted' LIMIT 100",
                (now - 30 * 86400, now - 86400),
            ).fetchall()
        ]
    for artifact in stale:
        # Do not delete from under an active execution or unresolved review.
        with p.db.transaction(serialized=False) as tx:
            task = get_task(tx, artifact["task_id"])
            if task["state"] not in TERMINAL:
                continue
            artifact["status"] = "deleting"
            tx.execute(
                "UPDATE v2_artifacts SET status=?,body=? WHERE id=?",
                ("deleting", encode(artifact), artifact["id"]),
            )
        if not p.storage:
            raise RuntimeError("Storage unavailable during retention")
        p.storage.delete(artifact)
        artifact["status"] = "deleted"
        with p.db.transaction(serialized=False) as tx:
            tx.execute(
                "UPDATE v2_artifacts SET status=?,body=? WHERE id=?",
                ("deleted", encode(artifact), artifact["id"]),
            )
    return len(ids)


def restore_fence(platform):
    """Run with ingress and all workers stopped, AFTER replaying the external deletion ledger."""
    p = platform
    now = p.clock()
    with p.db.transaction(serialized=False) as tx:
        lock(tx, "restore-fence")
        object_put(tx, "system", "auth_not_before", {"time": now})
        tx.execute("DELETE FROM v2_credentials")
        tx.execute("DELETE FROM v2_objects WHERE kind='ticket'")
        tx.execute(
            "DELETE FROM v2_idempotency WHERE scope LIKE ? OR scope LIKE ? OR scope LIKE ?",
            ("register:%", "%:rotate", "%:ticket"),
        )
        for row in tx.execute(
            "SELECT id,body FROM v2_objects WHERE kind='node'"
        ).fetchall():
            node = json.loads(row[1])
            node["revoked"] = True
            node["draining"] = True
            node["state_version"] += 1
            object_put(tx, "node", row[0], node)
        ids = tx.execute(
            "SELECT id FROM v2_tasks WHERE state NOT IN ('succeeded','failed','cancelled')"
        ).fetchall()
        for (task_id,) in ids:
            task = get_task(tx, task_id)
            task["dispatch_ready"] = False
            task["state"] = "needs_review" if task["active_attempt"] else "cancelled"
            p.event(tx, task, "restore_fenced")
        # Rebuild pending deletions before exposing any restored object references.
        for (task_id,) in tx.execute("SELECT task_id FROM v2_deletions").fetchall():
            task = get_task(tx, task_id)
            Artifacts(p).redact(tx, task)
            save_task(tx, task)
            tx.execute(
                "UPDATE v2_deletions SET completed=NULL WHERE task_id=?", (task_id,)
            )
        p.audit(tx, "restore-operator", "restore_fence", "all")
    return now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["retention", "restore-fence", "delete-sweep"]
    )
    args = parser.parse_args()
    from .api import configured_platform

    platform = configured_platform(Database(os.environ["AGENT_DATABASE_URL"]))
    if args.action == "restore-fence":
        print(
            "New user credentials must be issued after epoch:", restore_fence(platform)
        )
    elif args.action == "retention":
        print("Retention batches:", retention(platform))
    else:
        print("Deletion batches:", Artifacts(platform).sweep())


if __name__ == "__main__":
    main()
