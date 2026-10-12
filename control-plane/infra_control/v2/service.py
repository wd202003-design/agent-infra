import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
import jsonschema
from ..db import encode
from ..service import Fault
from .security import sha
from .store import (
    lock,
    object_get,
    object_put,
    get_task,
    save_task,
    get_attempt,
    save_attempt,
)

TERMINAL = {"succeeded", "failed", "cancelled"}


def uid():
    return str(uuid.uuid4())


def iso(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class Platform:
    def __init__(self, db, keys, *, clock=time.time, storage=None, verifier_keys=None):
        self.db, self.keys, self.clock = db, keys, clock
        self.storage, self.verifier_keys = storage, verifier_keys

    def audit(self, tx, actor, action, target):
        value = {
            "id": uid(),
            "actor": actor,
            "action": action,
            "target": target,
            "occurred_at": iso(self.clock()),
        }
        tx.execute(
            "INSERT INTO v2_audit(id,actor,action,created,body) VALUES(?,?,?,?,?)",
            (value["id"], actor, action, self.clock(), encode(value)),
        )

    def idem(self, tx, scope, key, payload, operation, *, secret=False):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise Fault("invalid_idempotency_key", 422)
        lock(tx, "idem:" + scope + ":" + key)
        checksum = sha(encode(payload))
        row = tx.execute(
            "SELECT hash,response FROM v2_idempotency WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row:
            if row[0] != checksum:
                raise Fault("idempotency_conflict")
            value = json.loads(row[1])
            if secret:
                value = self.keys.unseal("response", value["capsule"])
                if value["expires"] <= self.clock():
                    raise Fault("credential_response_expired", 410)
            return value
        value = operation()
        stored = {"capsule": self.keys.seal("response", value)} if secret else value
        tx.execute(
            "INSERT INTO v2_idempotency(scope,key,hash,response,created) VALUES(?,?,?,?,?)",
            (scope, key, checksum, encode(stored), self.clock()),
        )
        return value

    def event(self, tx, task, kind, payload=None):
        task["sequence"] += 1
        task["state_version"] += 1
        value = {
            "event_id": uid(),
            "task_id": task["id"],
            "task_seq": task["sequence"],
            "epoch": task["epoch"],
            "type": kind,
            "occurred_at": iso(self.clock()),
            "received_at": iso(self.clock()),
            "payload": payload or {},
        }
        tx.execute(
            "INSERT INTO v2_events(task_id,seq,created,body) VALUES(?,?,?,?)",
            (task["id"], task["sequence"], self.clock(), encode(value)),
        )
        save_task(tx, task)

    @staticmethod
    def cas(task, version):
        if task["state_version"] != version:
            raise Fault("version_conflict")

    def publish(self, actor, kind, name, payload, key):
        data = payload.model_dump()
        schema = data["input_schema"] if kind == "agent" else data["upload_schema"]
        try:
            jsonschema.Draft202012Validator.check_schema(schema)

            # External schema fetches are forbidden, including nested refs.
            def check(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if k in {"$ref", "$dynamicRef"} and not str(v).startswith("#"):
                            raise ValueError()
                        check(v)
                elif isinstance(value, list):
                    for v in value:
                        check(v)

            check(schema)
        except Exception:
            raise Fault("invalid_schema", 422) from None
        if kind == "agent" and data["agent_id"] != name:
            raise Fault("agent_id_mismatch", 422)
        with self.db.transaction(serialized=False) as tx:

            def apply():
                ref = name + ":" + data["version"]
                lock(tx, kind + ":" + ref)
                old = object_get(tx, kind, ref)
                value = {**data, "id": name, "digest": sha(encode(data))}
                if old and old != value:
                    raise Fault("immutable_version")
                if kind == "agent":
                    policy = object_get(
                        tx,
                        "policy",
                        data["policy_ref"]["id"] + ":" + data["policy_ref"]["version"],
                    )
                    if (
                        not policy
                        or not set(data["tools"]) <= set(policy["tool_kinds"])
                        or data["tool_timeout"] > policy["limits"]["tool_timeout"]
                        or data["max_output_tokens"]
                        > policy["limits"]["max_output_tokens"]
                    ):
                        raise Fault("policy_mismatch", 422)
                object_put(tx, kind, ref, value)
                self.audit(tx, actor["id"], "publish_" + kind, ref)
                return value

            return self.idem(
                tx, actor["id"] + ":publish:" + kind + ":" + name, key, data, apply
            )

    def versions(self, kind):
        with self.db.transaction(serialized=False) as tx:
            return [
                json.loads(r[0])
                for r in tx.execute(
                    "SELECT body FROM v2_objects WHERE kind=? ORDER BY id LIMIT 100",
                    (kind,),
                ).fetchall()
            ]

    def submit(self, actor, payload, key, parent=None):
        data = payload.model_dump()
        with self.db.transaction(serialized=False) as tx:

            def apply():
                if data["pool"] not in actor["pools"]:
                    raise Fault("pool_forbidden", 403)
                agent = object_get(
                    tx,
                    "agent",
                    data["agent_ref"]["id"] + ":" + data["agent_ref"]["version"],
                )
                policy = object_get(
                    tx,
                    "policy",
                    data["policy_ref"]["id"] + ":" + data["policy_ref"]["version"],
                )
                if not agent or not policy:
                    raise Fault("version_not_found", 404)
                if (
                    agent["policy_ref"] != data["policy_ref"]
                    or data["pool"] not in policy["allowed_pools"]
                    or data["data_class"] not in policy["data_classes"]
                ):
                    raise Fault("policy_forbidden", 403)
                if object_get(tx, "revoked_policy", policy["digest"]):
                    raise Fault("policy_revoked", 403)
                # Workspace/edge input never silently falls through to central model/logs.
                if data["workspace_ref"] is not None or data["data_class"] != "public":
                    raise Fault("data_path_not_certified", 422)
                if data["budget"]["mode"] == "hard":
                    raise Fault("hard_budget_requires_proven_bound", 422)
                try:
                    jsonschema.Draft202012Validator(agent["input_schema"]).validate(
                        data["input"]
                    )
                except Exception:
                    raise Fault("input_schema_mismatch", 422) from None
                lock(tx, "capacity")
                active = tx.execute(
                    "SELECT COUNT(*) FROM v2_tasks WHERE state NOT IN ('succeeded','failed','cancelled')"
                ).fetchone()[0]
                if active >= 1000:
                    raise Fault("active_task_limit", 429)
                now = self.clock()
                task = {
                    **data,
                    "id": uid(),
                    "owner": actor["id"],
                    "agent": agent,
                    "policy": policy,
                    "state": "queued",
                    "state_version": 1,
                    "sequence": 0,
                    "epoch": 0,
                    "created": now,
                    "created_at": iso(now),
                    "deadline": now + data["timeout_seconds"],
                    "dispatch_ready": False,
                    "coordinator": "temporal",
                    "active_attempt": None,
                    "cancel_requested_at": None,
                    "waiting": None,
                    "result": None,
                    "deleted": False,
                    "generation": 1,
                    "parent_task_id": parent,
                    "reserved_tokens": 0,
                    "actual_tokens": 0,
                    "reserved_cost": 0,
                    "actual_cost": 0,
                }
                save_task(tx, task)
                self.event(tx, task, "submitted")
                tx.execute(
                    "INSERT INTO v2_outbox(id,status,created) VALUES(?,?,?)",
                    (task["id"], "pending", now),
                )
                self.audit(tx, actor["id"], "submit", task["id"])
                return task

            return self.idem(
                tx,
                actor["id"] + ":submit",
                key,
                {"submit": data, "parent": parent},
                apply,
            )

    def expire(self, tx, task):
        if task["state"] in TERMINAL or task["state"] == "needs_review":
            return
        now = self.clock()
        if not task["active_attempt"]:
            if task["deadline"] <= now:
                task["state"] = "cancelled" if task["cancel_requested_at"] else "failed"
                self.event(tx, task, "deadline_expired")
        else:
            attempt = get_attempt(tx, task["active_attempt"])
            if attempt["lease_until"] <= now or task["deadline"] <= now:
                task["state"] = "needs_review"
                self.event(tx, task, "lease_or_deadline_expired")

    def task(self, actor, task_id):
        with self.db.transaction(serialized=False) as tx:
            task = get_task(tx, task_id, actor)
            self.expire(tx, task)
            return task

    def tasks(self, actor, cursor=None, limit=50):
        after = (
            self.keys.verify("cursor", cursor, self.clock())
            if cursor
            else {"last": "", "owner": actor["id"], "kind": "tasks"}
        )
        if after.get("owner") != actor["id"] or after.get("kind") != "tasks":
            raise Fault("invalid_cursor", 422)
        with self.db.transaction(serialized=False) as tx:
            rows = tx.execute(
                "SELECT body FROM v2_tasks WHERE id>? AND (owner=? OR ?=1) ORDER BY id LIMIT ?",
                (after["last"], actor["id"], int(actor["role"] == "admin"), limit + 1),
            ).fetchall()
            items = [json.loads(r[0]) for r in rows[:limit]]
        next_cursor = (
            self.keys.sign(
                "cursor",
                {
                    "owner": actor["id"],
                    "kind": "tasks",
                    "last": items[-1]["id"],
                    "expires": self.clock() + 86400,
                },
            )
            if len(rows) > limit
            else None
        )
        return items, next_cursor

    def events(self, actor, task_id, cursor=None, limit=50):
        with self.db.transaction(serialized=False) as tx:
            task = get_task(tx, task_id, actor, locked=False)
            if task["deleted"]:
                raise Fault("content_deleted", 410)
            after = (
                self.keys.verify("cursor", cursor, self.clock())
                if cursor
                else {"task": task_id, "generation": task["generation"], "last": 0}
            )
            if after.get("task") != task_id:
                raise Fault("invalid_cursor", 422)
            if after.get("generation") != task["generation"]:
                raise Fault("cursor_expired", 410)
            rows = tx.execute(
                "SELECT body FROM v2_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, after["last"], limit + 1),
            ).fetchall()
            items = [json.loads(r[0]) for r in rows[:limit]]
            after.update(
                last=items[-1]["task_seq"] if items else after["last"],
                expires=self.clock() + 86400,
            )
            return items, self.keys.sign("cursor", after)

    def cancel(self, actor, task_id, payload, key):
        with self.db.transaction(serialized=False) as tx:

            def apply():
                task = get_task(tx, task_id, actor)
                if task["state"] in TERMINAL or task["cancel_requested_at"]:
                    return task
                self.cas(task, payload.expected_version)
                task["cancel_requested_at"] = self.clock()
                if not task["active_attempt"]:
                    task["state"] = "cancelled"
                elif task["state"] != "needs_review":
                    task["state"] = "cancel_requested"
                self.event(tx, task, "cancel_requested")
                self.audit(tx, actor["id"], "cancel", task_id)
                return task

            return self.idem(
                tx, actor["id"] + ":cancel:" + task_id, key, payload.model_dump(), apply
            )

    def ticket(self, actor, payload, key):
        if payload.pool not in actor["pools"]:
            raise Fault("pool_forbidden", 403)
        with self.db.transaction(serialized=False) as tx:

            def apply():
                secret = secrets.token_urlsafe(32)
                value = {
                    "id": uid(),
                    "policy": payload.model_dump(),
                    "secret_hash": sha(secret),
                    "expires": self.clock() + 600,
                    "used": None,
                }
                object_put(tx, "ticket", value["id"], value)
                self.audit(tx, actor["id"], "issue_ticket", value["id"])
                return {
                    "ticket_id": value["id"],
                    "ticket_secret": secret,
                    "expires": value["expires"],
                    "expires_at": iso(value["expires"]),
                }

            return self.idem(
                tx,
                actor["id"] + ":ticket",
                key,
                payload.model_dump(),
                apply,
                secret=True,
            )

    def credential(self, tx, node_id):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        tx.execute(
            "INSERT INTO v2_credentials(hash,node_id,expires,created) VALUES(?,?,?,?)",
            (sha(token), node_id, now + 86400, now),
        )
        return {
            "node_id": node_id,
            "node_token": token,
            "expires": now + 86400,
            "expires_at": iso(now + 86400),
            "rotate_after": iso(now + 43200),
        }

    def register(self, payload, key):
        with self.db.transaction(serialized=False) as tx:
            lock(tx, "ticket:" + payload.ticket_id)
            ticket = object_get(tx, "ticket", payload.ticket_id)
            if not ticket or not secrets.compare_digest(
                ticket["secret_hash"], sha(payload.ticket_secret)
            ):
                raise Fault("invalid_ticket", 401)
            if ticket["expires"] <= self.clock():
                raise Fault("ticket_expired", 401)

            def apply():
                if ticket["used"] and ticket["used"] != payload.registration_id:
                    raise Fault("ticket_consumed")
                if ticket["used"]:
                    raise Fault("registration_key_changed")
                node = {
                    "id": uid(),
                    **ticket["policy"],
                    "state_version": 1,
                    "draining": False,
                    "revoked": False,
                    "created_at": iso(self.clock()),
                }
                object_put(tx, "node", node["id"], node)
                ticket["used"] = payload.registration_id
                object_put(tx, "ticket", ticket["id"], ticket)
                self.audit(tx, node["id"], "register", node["id"])
                return {**self.credential(tx, node["id"]), "policy": ticket["policy"]}

            return self.idem(
                tx,
                "register:" + payload.ticket_id,
                payload.registration_id,
                payload.model_dump(),
                apply,
                secret=True,
            )

    def authenticate(self, token):
        with self.db.transaction(serialized=False) as tx:
            row = tx.execute(
                "SELECT node_id,expires FROM v2_credentials WHERE hash=?", (sha(token),)
            ).fetchone()
            if not row or row[1] <= self.clock():
                raise Fault("unauthorized", 401)
            node = object_get(tx, "node", row[0])
            if not node or node["revoked"]:
                raise Fault("node_revoked", 401)
            return node["id"]

    def node(self, tx, node_id):
        lock(tx, "node:" + node_id)
        node = object_get(tx, "node", node_id)
        if not node or node["revoked"]:
            raise Fault("node_revoked", 401)
        return node

    def rotate(self, node_id, payload, key):
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)

            def apply():
                tx.execute(
                    "UPDATE v2_credentials SET expires=? WHERE node_id=? AND expires>?",
                    (self.clock() + 60, node_id, self.clock() + 60),
                )
                self.audit(tx, node_id, "rotate", node_id)
                return self.credential(tx, node_id)

            return self.idem(
                tx,
                node_id + ":rotate",
                payload.rotation_id,
                payload.model_dump(),
                apply,
                secret=True,
            )

    def nodes(self):
        return self.versions("node")

    def patch_node(self, actor, node_id, payload, key):
        with self.db.transaction(serialized=False) as tx:

            def apply():
                node = self.node(tx, node_id)
                self.cas(node, payload.expected_version)
                if payload.slots_limit is not None:
                    row = tx.execute(
                        "SELECT MAX(slot) FROM v2_attempts WHERE node_id=? AND active=1",
                        (node_id,),
                    ).fetchone()
                    if row[0] is not None and row[0] >= payload.slots_limit:
                        raise Fault("slots_in_use")
                    node["slots_limit"] = payload.slots_limit
                if payload.draining is not None:
                    node["draining"] = payload.draining
                node["state_version"] += 1
                object_put(tx, "node", node_id, node)
                self.audit(tx, actor["id"], "patch_node", node_id)
                return node

            return self.idem(
                tx, actor["id"] + ":node:" + node_id, key, payload.model_dump(), apply
            )

    def revoke(self, actor, node_id, payload, key):
        with self.db.transaction(serialized=False) as tx:

            def apply():
                lock(tx, "node:" + node_id)
                node = object_get(tx, "node", node_id)
                if not node:
                    raise Fault("not_found", 404)
                node["revoked"] = True
                node["draining"] = True
                node["state_version"] += 1
                object_put(tx, "node", node_id, node)
                tx.execute("DELETE FROM v2_credentials WHERE node_id=?", (node_id,))
                self.audit(tx, actor["id"], "revoke_node", node_id)
                return node

            return self.idem(
                tx, actor["id"] + ":revoke:" + node_id, key, payload.model_dump(), apply
            )

    def claim(self, node_id, payload, key):
        with self.db.transaction(serialized=False) as tx:
            node = self.node(tx, node_id)

            def apply():
                if node["draining"]:
                    return None
                slots = min(node["slots_limit"], payload.advertised_slots)
                occupied = {
                    r[0]
                    for r in tx.execute(
                        "SELECT slot FROM v2_attempts WHERE node_id=? AND active=1",
                        (node_id,),
                    ).fetchall()
                }
                free = next((i for i in range(slots) if i not in occupied), None)
                if free is None:
                    return None
                suffix = " FOR UPDATE SKIP LOCKED" if not tx.sqlite else ""
                rows = tx.execute(
                    "SELECT body FROM v2_tasks WHERE pool=? AND state='queued' AND ready=1 AND deadline>? ORDER BY created,id LIMIT 100"
                    + suffix,
                    (node["pool"], self.clock()),
                ).fetchall()
                for row in rows:
                    task = json.loads(row[0])
                    if not set(task["agent"]["tools"]) <= set(node["capabilities"]):
                        continue
                    if (
                        node["location"] in {"local", "edge"}
                        and task["owner"] != node["owner"]
                    ):
                        continue
                    if object_get(tx, "revoked_policy", task["policy"]["digest"]):
                        continue
                    task["epoch"] += 1
                    attempt = {
                        "id": uid(),
                        "task_id": task["id"],
                        "node_id": node_id,
                        "slot": free,
                        "active": True,
                        "epoch": task["epoch"],
                        "lease_until": min(self.clock() + 30, task["deadline"]),
                        "stop_status": "pending",
                        "cleanup_status": "pending",
                        "last_ack": 0,
                        "simulated": node["backend"] == "demo",
                        "backend": node["backend"],
                        "unknown_effects": True,
                        "sandbox_ref": "",
                    }
                    task["active_attempt"] = attempt["id"]
                    task["state"] = "running"
                    task["waiting"] = None
                    save_attempt(tx, attempt)
                    self.event(
                        tx,
                        task,
                        "claimed",
                        {
                            "attempt_id": attempt["id"],
                            "simulated": attempt["simulated"],
                        },
                    )
                    return {"task": task, "attempt": attempt, "policy": task["policy"]}
                return None

            return self.idem(
                tx, node_id + ":claim", payload.claim_id, payload.model_dump(), apply
            )

    def active(self, tx, node_id, attempt_id, epoch, *, allow_report=False):
        attempt = get_attempt(tx, attempt_id)
        if attempt["node_id"] != node_id:
            raise Fault("not_found", 404)
        task = get_task(tx, attempt["task_id"])
        attempt = get_attempt(tx, attempt_id)
        if task["active_attempt"] != attempt_id or attempt["epoch"] != epoch:
            raise Fault("stale_epoch")
        if not allow_report and (
            task["state"] != "running"
            or attempt["lease_until"] <= self.clock()
            or task["deadline"] <= self.clock()
            or not attempt["active"]
        ):
            raise Fault("execution_not_authorized")
        return task, attempt

    def renew(self, node_id, attempt_id, payload):
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)
            try:
                task, attempt = self.active(tx, node_id, attempt_id, payload.epoch)
                if task["id"] != payload.task_id:
                    raise Fault("task_mismatch")
                if payload.health != "healthy":
                    raise Fault("node_unhealthy")
            except Fault as exc:
                return {
                    "authorized": False,
                    "remaining_seconds": 0,
                    "stop_reason": exc.code,
                }
            attempt["lease_until"] = min(self.clock() + 30, task["deadline"])
            save_attempt(tx, attempt)
            return {
                "authorized": True,
                "remaining_seconds": max(0, attempt["lease_until"] - self.clock()),
            }

    def authorize(self, node_id, attempt_id, payload, key):
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)
            task, attempt = self.active(tx, node_id, attempt_id, payload.epoch)
            if payload.policy_digest != task["policy"]["digest"] or object_get(
                tx, "revoked_policy", payload.policy_digest
            ):
                raise Fault("policy_revoked", 403)
            if not attempt["simulated"] and not attempt.get("isolation_verified"):
                raise Fault("isolation_not_verified", 403)
            if (
                payload.tool not in task["agent"]["tools"]
                or payload.tool not in task["policy"]["tool_kinds"]
            ):
                raise Fault("tool_forbidden", 403)

            def apply():
                value = {
                    "node": node_id,
                    "task": task["id"],
                    "attempt": attempt_id,
                    "epoch": payload.epoch,
                    "step": payload.step_id,
                    "operation": payload.operation_id,
                    "tool": payload.tool,
                    "args_hash": payload.args_hash,
                    "policy": payload.policy_digest,
                    "expires": min(
                        self.clock() + 30, attempt["lease_until"], task["deadline"]
                    ),
                }
                return {
                    "capability": self.keys.sign("capability", value),
                    "expires": value["expires"],
                }

            return self.idem(
                tx,
                attempt_id + ":authorize",
                payload.operation_id,
                payload.model_dump(),
                apply,
            )

    def verify_capability(self, node_id, payload):
        value = self.keys.verify("capability", payload.capability, self.clock())
        if (
            value["node"] != node_id
            or value["tool"] != payload.tool
            or value["args_hash"] != payload.args_hash
        ):
            raise Fault("capability_mismatch", 403)
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)
            task, attempt = self.active(tx, node_id, value["attempt"], value["epoch"])
            if task["policy"]["digest"] != value["policy"] or object_get(
                tx, "revoked_policy", value["policy"]
            ):
                raise Fault("policy_revoked", 403)
        return {
            "authorized": True,
            "remaining_seconds": min(value["expires"], attempt["lease_until"])
            - self.clock(),
        }

    def runner_events(self, node_id, attempt_id, payload, key):
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)
            task, attempt = self.active(
                tx, node_id, attempt_id, payload.epoch, allow_report=True
            )
            for item in payload.events:
                data = item.model_dump()
                checksum = sha(encode(data))
                old = tx.execute(
                    "SELECT hash FROM v2_runner_events WHERE attempt_id=? AND seq=?",
                    (attempt_id, item.runner_seq),
                ).fetchone()
                if old:
                    if old[0] != checksum:
                        raise Fault("event_sequence_conflict")
                    continue
                if item.runner_seq > attempt["last_ack"] + 1000:
                    raise Fault("event_gap_too_large", 422)
                used = tx.execute(
                    "SELECT COALESCE(SUM(LENGTH(body)),0) FROM v2_runner_events WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()[0]
                if used + len(encode(data).encode()) > 10485760:
                    raise Fault("task_log_limit", 413)
                tx.execute(
                    "INSERT INTO v2_runner_events(attempt_id,seq,hash,body) VALUES(?,?,?,?)",
                    (attempt_id, item.runner_seq, checksum, encode(data)),
                )
                if item.type == "sandbox_created":
                    ref = item.payload.get("sandbox_ref")
                    if not isinstance(ref, str) or not 1 <= len(ref) <= 256:
                        raise Fault("invalid_sandbox_ref", 422)
                    if attempt["sandbox_ref"] and attempt["sandbox_ref"] != ref:
                        raise Fault("sandbox_ref_conflict")
                    attempt["sandbox_ref"] = ref
                self.event(
                    tx,
                    task,
                    "runner." + item.type,
                    {
                        "attempt_id": attempt_id,
                        "runner_seq": item.runner_seq,
                        "payload": item.payload,
                    },
                )
            seqs = {
                r[0]
                for r in tx.execute(
                    "SELECT seq FROM v2_runner_events WHERE attempt_id=? AND seq>?",
                    (attempt_id, attempt["last_ack"]),
                ).fetchall()
            }
            while attempt["last_ack"] + 1 in seqs:
                attempt["last_ack"] += 1
            save_attempt(tx, attempt)
            return {
                "contiguous_ack": attempt["last_ack"],
                "gaps": [
                    i
                    for i in range(
                        attempt["last_ack"] + 1,
                        max(seqs, default=attempt["last_ack"]) + 1,
                    )
                    if i not in seqs
                ],
            }

    def evidence(self, attempt, token):
        if attempt["simulated"]:
            return token == "SIMULATED"
        if not self.verifier_keys or not attempt["sandbox_ref"]:
            return False
        try:
            proof = self.verifier_keys.verify("cleanup", token, self.clock())
            return (
                proof["attempt"] == attempt["id"]
                and proof["epoch"] == attempt["epoch"]
                and proof["sandbox"] == attempt["sandbox_ref"]
                and proof["management_absent"] is True
                and proof["host_absent"] is True
                and 0 <= self.clock() - proof["observed"] <= 30
                and proof["expires"] <= proof["observed"] + 30
            )
        except (Fault, KeyError, TypeError):
            return False

    def unresolved(self, tx, task_id):
        return (
            tx.execute(
                "SELECT COUNT(*) FROM v2_usage WHERE task_id=? AND status IN ('pending','unknown')",
                (task_id,),
            ).fetchone()[0]
            > 0
        )

    def report(self, node_id, attempt_id, payload, key):
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)

            def apply():
                task, attempt = self.active(
                    tx, node_id, attempt_id, payload.epoch, allow_report=True
                )
                if task["state"] in TERMINAL:
                    raise Fault("already_terminal")
                if payload.sandbox_ref != attempt["sandbox_ref"]:
                    raise Fault("sandbox_ref_mismatch")
                clean = self.evidence(
                    attempt, payload.cleanup_evidence_ref
                ) and self.evidence(attempt, payload.stop_evidence_ref)
                attempt["stop_status"] = attempt["cleanup_status"] = (
                    "confirmed" if clean else "unknown"
                )
                attempt["unknown_effects"] = payload.unknown_effects
                if payload.result_ref:
                    artifact = tx.execute(
                        "SELECT task_id,status FROM v2_artifacts WHERE id=?",
                        (payload.result_ref,),
                    ).fetchone()
                    if (
                        not artifact
                        or artifact[0] != task["id"]
                        or artifact[1] != "committed"
                    ):
                        raise Fault("result_not_committed")
                task["result"] = {
                    "text": payload.result,
                    "artifact_id": payload.result_ref,
                }
                safe = (
                    clean
                    and not payload.unknown_effects
                    and not self.unresolved(tx, task["id"])
                )
                task["state"] = (
                    ("cancelled" if task["cancel_requested_at"] else payload.outcome)
                    if safe
                    else "needs_review"
                )
                attempt["active"] = task["state"] not in TERMINAL
                save_attempt(tx, attempt)
                self.event(
                    tx, task, "reported", {"state": task["state"], "cleanup": clean}
                )
                return task

            return self.idem(
                tx, attempt_id + ":report", key, payload.model_dump(), apply
            )

    def review(self, actor, task_id, payload, key):
        with self.db.transaction(serialized=False) as tx:

            def apply():
                task = get_task(tx, task_id, actor)
                self.cas(task, payload.expected_version)
                if task["state"] != "needs_review" or self.unresolved(tx, task_id):
                    raise Fault("review_not_ready")
                if payload.outcome == "succeeded" and (
                    task["cancel_requested_at"] or task["result"] is None
                ):
                    raise Fault("invalid_success_review")
                attempt = get_attempt(tx, task["active_attempt"])
                attempt.update(
                    active=False,
                    stop_status="confirmed",
                    cleanup_status="confirmed",
                    unknown_effects=False,
                )
                save_attempt(tx, attempt)
                task["state"] = (
                    "cancelled" if task["cancel_requested_at"] else payload.outcome
                )
                self.event(
                    tx, task, "admin_review", {"evidence_refs": payload.evidence_refs}
                )
                self.audit(tx, actor["id"], "review", task_id)
                return task

            return self.idem(
                tx, actor["id"] + ":review:" + task_id, key, payload.model_dump(), apply
            )

    def command(self, payload):
        with self.db.transaction(serialized=False) as tx:

            def apply():
                task = get_task(tx, payload.task_id)
                self.cas(task, payload.expected_version)
                if (
                    payload.expected_epoch is not None
                    and payload.expected_epoch != task["epoch"]
                ):
                    raise Fault("stale_epoch")
                self.expire(tx, task)
                if payload.type == "make_ready":
                    if task["state"] == "queued":
                        task["dispatch_ready"] = True
                elif payload.type == "request_stop":
                    if task["state"] not in TERMINAL:
                        task["cancel_requested_at"] = (
                            task["cancel_requested_at"] or self.clock()
                        )
                        task["state"] = (
                            "cancel_requested"
                            if task["active_attempt"]
                            else "cancelled"
                        )
                elif payload.type == "schedule_safe_retry":
                    attempt = (
                        get_attempt(tx, task["active_attempt"])
                        if task["active_attempt"]
                        else None
                    )
                    if (
                        task["state"] != "failed"
                        or not attempt
                        or attempt["active"]
                        or attempt["stop_status"] != "confirmed"
                        or attempt["cleanup_status"] != "confirmed"
                        or attempt["unknown_effects"]
                        or task["agent"]["side_effect_class"]
                        not in {"pure", "read_only"}
                        or self.unresolved(tx, task["id"])
                        or task["epoch"] >= 2
                        or task["cancel_requested_at"]
                        or task["deadline"] <= self.clock()
                        or task["actual_tokens"] + task["reserved_tokens"]
                        >= task["budget"]["token_limit"]
                        or task["actual_cost"] + task["reserved_cost"]
                        >= task["budget"]["cost_limit"]
                    ):
                        raise Fault("retry_not_safe")
                    task["state"] = "queued"
                    task["active_attempt"] = None
                    task["dispatch_ready"] = True
                elif payload.type == "mark_waiting":
                    if task["state"] != "queued":
                        raise Fault("invalid_waiting_state")
                    task["waiting"] = {
                        "reason": "dependency",
                        "until": iso(min(self.clock() + 10, task["deadline"])),
                        "owns_slot": False,
                    }
                    task["dispatch_ready"] = False
                if payload.type != "reconcile":
                    self.event(tx, task, "command." + payload.type)
                return {
                    "task_id": task["id"],
                    "state": task["state"],
                    "state_version": task["state_version"],
                    "epoch": task["epoch"],
                    "deadline": task["deadline"],
                }

            return self.idem(
                tx, "command", payload.command_id, payload.model_dump(), apply
            )

    def verify_isolation(self, node_id, attempt_id, payload):
        if not self.verifier_keys:
            raise Fault("verifier_not_configured", 503)
        proof = self.verifier_keys.verify("isolation", payload.proof, self.clock())
        with self.db.transaction(serialized=False) as tx:
            self.node(tx, node_id)
            task, attempt = self.active(tx, node_id, attempt_id, payload.epoch)
            if not (
                proof.get("attempt") == attempt_id
                and proof.get("task") == task["id"]
                and proof.get("sandbox") == attempt["sandbox_ref"]
                and proof.get("epoch") == payload.epoch
                and proof.get("isolated") is True
                and proof.get("policy") == "offline-v2"
                and 0 <= self.clock() - proof.get("observed", 0) <= 30
                and proof["expires"] <= proof["observed"] + 30
            ):
                raise Fault("isolation_proof_mismatch", 403)
            attempt["isolation_verified"] = True
            save_attempt(tx, attempt)
            return {"authorized": True}
