"""Small DAG scheduler. PostgreSQL is authoritative; external effects never run in transactions."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid

from ..db import encode
from ..service import Fault
from ..v2.store import lock

TERMINAL = {"succeeded", "partially_succeeded", "failed", "cancelled", "skipped"}
UNSETTLED = {"reserved", "dispatching", "accepted", "unknown"}


def uid():
    return str(uuid.uuid4())


def sha(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def stamp(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class Images:
    def __init__(self, db, keys, clock=time.time, storage=None):
        self.db, self.keys, self.clock, self.storage = db, keys, clock, storage

    @contextmanager
    def tx(self):
        # Deliberately serialize the bounded first release; no capacity claim.
        with self.db.transaction(serialized=False) as tx:
            lock(tx, "image-agent-v3")
            yield tx

    def get(self, tx, kind, id):
        row = tx.execute(
            "SELECT body FROM v3_records WHERE kind=? AND id=?", (kind, id)
        ).fetchone()
        if not row:
            raise Fault("NOT_FOUND", 404)
        return json.loads(row[0])

    def maybe(self, tx, kind, id):
        try:
            return self.get(tx, kind, id)
        except Fault:
            return None

    def put(self, tx, kind, value, root=None):
        tx.execute(
            "INSERT INTO v3_records(kind,id,root,state,created,body) VALUES(?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET state=excluded.state,body=excluded.body",
            (
                kind,
                value["id"],
                root or value.get("root_id", value["id"]),
                value.get("state", ""),
                value.get("created", self.clock()),
                encode(value),
            ),
        )

    def rows(self, tx, kind, root=None):
        sql = "SELECT body FROM v3_records WHERE kind=?"
        args = [kind]
        if root is not None:
            sql += " AND root=?"
            args.append(root)
        return [
            json.loads(x[0])
            for x in tx.execute(sql + " ORDER BY created,id", tuple(args)).fetchall()
        ]

    def idem(self, tx, scope, key, payload, apply):
        digest = sha(payload)
        row = tx.execute(
            "SELECT hash,response FROM v3_idempotency WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row:
            if row[0] != digest:
                raise Fault("IDEMPOTENCY_CONFLICT", 409)
            return json.loads(row[1])
        result = apply()
        tx.execute(
            "INSERT INTO v3_idempotency(scope,key,hash,response) VALUES(?,?,?,?)",
            (scope, key, digest, encode(result)),
        )
        return result

    def owned(self, tx, id, user):
        root = self.get(tx, "root", id)
        if user["role"] != "admin" and root["owner"] != user["id"]:
            raise Fault("NOT_FOUND", 404)
        return root

    def binding(self, tx, ref):
        return self.get(tx, "binding", ref["id"] + ":" + ref["version"])

    def public_binding(self, b):
        return {
            k: v for k, v in b.items() if k not in {"secret_cipher", "input_digest"}
        }

    def publish(self, user, id, payload, key):
        data = payload.model_dump()
        ref = id + ":" + data["version"]
        with self.tx() as tx:

            def apply():
                old = self.maybe(tx, "binding", ref)
                if old:
                    if old["input_digest"] != sha(data):
                        raise Fault("IMMUTABLE_VERSION", 409)
                    return self.public_binding(old)
                quota = self.maybe(tx, "quota", data["quota_pool_id"])
                if quota and quota["limit"] != data["max_inflight"]:
                    raise Fault("SHARED_QUOTA_MISMATCH", 422)
                self.put(
                    tx,
                    "quota",
                    {"id": data["quota_pool_id"], "limit": data["max_inflight"]},
                )
                b = {k: v for k, v in data.items() if k != "secret"}
                b.update(
                    id=ref,
                    input_digest=sha(data),
                    secret_cipher=self.keys.seal("v3-provider", data["secret"]),
                    revoked=False,
                )
                self.put(tx, "binding", b)
                return self.public_binding(b)

            return self.idem(tx, user["id"] + ":binding:" + id, key, data, apply)

    def list_bindings(self, user):
        with self.tx() as tx:
            return [
                self.public_binding(b)
                for b in self.rows(tx, "binding")
                if set(b["allowed_pools"]) & set(user["pools"]) and not b["revoked"]
            ]

    def revoke_binding(self, id):
        with self.tx() as tx:
            b = self.get(tx, "binding", id)
            b["revoked"] = True
            self.put(tx, "binding", b)
            return {"revoked": True}

    def submit(self, user, payload, key):
        data = payload.model_dump()
        with self.tx() as tx:

            def apply():
                if data["pool"] not in user["pools"]:
                    raise Fault("POOL_FORBIDDEN", 403)
                for purpose in ("agent", "image"):
                    b = self.binding(tx, data[purpose + "_binding"])
                    if (
                        b["purpose"] != purpose
                        or b["revoked"]
                        or data["pool"] not in b["allowed_pools"]
                        or b["currency"] != data["currency"]
                    ):
                        raise Fault("MODEL_NOT_ALLOWED", 403)
                roots = self.rows(tx, "root")
                if (
                    sum(r["state"] not in TERMINAL for r in roots) >= 1000
                    or sum(
                        r["owner"] == user["id"] and r["state"] not in TERMINAL
                        for r in roots
                    )
                    >= 32
                ):
                    raise Fault("ACTIVE_TASK_LIMIT", 429)
                now = self.clock()
                id = uid()
                root = data | {
                    "id": id,
                    "owner": user["id"],
                    "state": "queued",
                    "state_version": 1,
                    "created": now,
                    "created_at": stamp(now),
                    "deadline": now + data["timeout_seconds"],
                    "deadline_at": stamp(now + data["timeout_seconds"]),
                    "actual_micro": 0,
                    "reserved_micro": 0,
                    "context_version": 1,
                    "planned": False,
                    "cancel_requested": False,
                }
                self.put(tx, "root", root)
                self.put(
                    tx,
                    "context",
                    {
                        "id": id + ":1",
                        "root_id": id,
                        "version": 1,
                        "brief": data["instruction"],
                        "constraints": [],
                        "reference_artifact_ids": [],
                        "digest": sha({"brief": data["instruction"]}),
                    },
                )
                return root

            return self.idem(tx, user["id"] + ":submit", key, data, apply)

    def context(self, user, id, payload, key):
        data = payload.model_dump()
        with self.tx() as tx:

            def apply():
                root = self.owned(tx, id, user)
                if root["state"] in TERMINAL or root["cancel_requested"]:
                    raise Fault("TASK_CLOSED", 409)
                if root["state_version"] != data["expected_version"]:
                    raise Fault("VERSION_CONFLICT", 409)
                for ref in data["reference_artifact_ids"]:
                    artifact = self.get(tx, "artifact", ref)
                    if (
                        artifact["owner"] != root["owner"]
                        or artifact["state"] != "committed"
                    ):
                        raise Fault("ARTIFACT_FORBIDDEN", 403)
                version = root["context_version"] + 1
                value = {k: v for k, v in data.items() if k != "expected_version"}
                value.update(
                    id=id + ":" + str(version),
                    root_id=id,
                    version=version,
                    digest=sha(value),
                )
                self.put(tx, "context", value)
                root["context_version"] = version
                root["state_version"] += 1
                self.put(tx, "root", root)
                # Existing children intentionally keep their immutable version.
                return value

            return self.idem(tx, user["id"] + ":context:" + id, key, data, apply)

    def plan(self, user, id, payload, key):
        data = payload.model_dump()
        with self.tx() as tx:

            def apply():
                root = self.owned(tx, id, user)
                if root["planned"] or root["state"] != "queued":
                    raise Fault("PLAN_ALREADY_FIXED", 409)
                if root["state_version"] != data["expected_version"]:
                    raise Fault("VERSION_CONFLICT", 409)
                if len(data["children"]) > root["policy"]["max_children"]:
                    raise Fault("CHILD_LIMIT", 422)
                if (
                    sum(x["budget_limit_micro"] for x in data["children"])
                    > root["cost_limit_micro"]
                ):
                    raise Fault("BUDGET_ALLOCATION_EXCEEDED", 422)
                image = self.binding(tx, root["image_binding"])
                for child in data["children"]:
                    context = self.get(
                        tx, "context", id + ":" + str(child["context_version"])
                    )
                    if (
                        child["size"] not in image["sizes"]
                        or child["quality"] not in image["qualities"]
                    ):
                        raise Fault("UNSUPPORTED_GENERATION_OPTION", 422)
                    if (
                        child["kind"] == "edit"
                        and not context["reference_artifact_ids"]
                    ):
                        raise Fault("REFERENCE_REQUIRED", 422)
                    if child["kind"] == "selection" and not child["depends_on"]:
                        raise Fault("DEPENDENCY_REQUIRED", 422)
                    # Prevent silently merging outputs of different briefs.
                    versions = {
                        c["child_key"]: c["context_version"] for c in data["children"]
                    }
                    if any(
                        versions[d] != child["context_version"]
                        for d in child["depends_on"]
                    ):
                        raise Fault("CONTEXT_VERSION_MISMATCH", 422)
                    self.put(
                        tx,
                        "child",
                        child
                        | {
                            "id": uid(),
                            "root_id": id,
                            "state": "blocked" if child["depends_on"] else "ready",
                            "epoch": 0,
                            "active_attempt": None,
                            "actual_micro": 0,
                            "reserved_micro": 0,
                            "artifact_ids": [],
                            "selected_artifact_ids": [],
                            "created": self.clock(),
                        },
                    )
                root["planned"] = True
                root["state_version"] += 1
                self.put(tx, "root", root)
                return self.rows(tx, "child", id)

            return self.idem(tx, user["id"] + ":plan:" + id, key, data, apply)

    def refresh(self, tx):
        now = self.clock()
        for a in self.rows(tx, "attempt"):
            if a["state"] == "active" and a["lease_until"] <= now:
                a["state"] = "expired"
                self.put(tx, "attempt", a)
                c = self.get(tx, "child", a["task_id"])
                c["state"] = "needs_review"
                self.put(tx, "child", c)
                for op in self.rows(tx, "operation", c["root_id"]):
                    if op["attempt_id"] == a["id"] and op["state"] in {
                        "dispatching",
                        "accepted",
                    }:
                        op["state"] = "unknown"
                        self.put(tx, "operation", op)
                    elif op["attempt_id"] == a["id"] and op["state"] == "reserved":
                        self.settle(tx, op, 0, "failed")
        for root in self.rows(tx, "root"):
            if root["state"] in TERMINAL:
                continue
            if root["deadline"] <= now:
                root["cancel_requested"] = True
            children = self.rows(tx, "child", root["id"])
            bykey = {c["child_key"]: c for c in children}
            failfast = root["policy"]["failure_policy"] == "fail_fast" and any(
                c["state"] in {"failed", "needs_review"} for c in children
            )
            for c in children:
                if (root["cancel_requested"] or failfast) and c["state"] in {
                    "ready",
                    "blocked",
                }:
                    c["state"] = "cancelled"
                    self.put(tx, "child", c)
                elif (root["cancel_requested"] or failfast) and c["state"] == "running":
                    c["state"] = "cancel_requested"
                    self.put(tx, "child", c)
                elif c["state"] == "blocked":
                    deps = [bykey[k] for k in c["depends_on"]]
                    if all(d["state"] == "succeeded" for d in deps):
                        c["state"] = "ready"
                        self.put(tx, "child", c)
                    elif any(d["state"] in TERMINAL - {"succeeded"} for d in deps):
                        c["state"] = "skipped"
                        self.put(tx, "child", c)
            states = [c["state"] for c in children]
            operations = self.rows(tx, "operation", root["id"])
            if (
                any(o["state"] == "unknown" for o in operations)
                or "needs_review" in states
            ):
                state = "needs_review"
            elif (
                children
                and all(s in TERMINAL for s in states)
                and not any(o["state"] in UNSETTLED for o in operations)
            ):
                state = (
                    "cancelled"
                    if root["cancel_requested"]
                    else "succeeded"
                    if all(s == "succeeded" for s in states)
                    else "partially_succeeded"
                    if "succeeded" in states and root["policy"]["allow_partial"]
                    else "failed"
                )
            elif root["cancel_requested"]:
                state = "cancel_requested" if children else "cancelled"
            elif any(s in {"running", "succeeded", "waiting"} for s in states):
                state = "running"
            else:
                state = root["state"]
            # Money may have changed while expiring a reservation; use latest totals.
            latest = self.get(tx, "root", root["id"])
            root["actual_micro"], root["reserved_micro"] = (
                latest["actual_micro"],
                latest["reserved_micro"],
            )
            if state != root["state"]:
                root["state_version"] += 1
            root["state"] = state
            self.put(tx, "root", root)

    def claim(self, worker, payload, key):
        data = payload.model_dump()
        with self.tx() as tx:
            self.refresh(tx)

            def apply():
                attempts = self.rows(tx, "attempt")
                active = [a for a in attempts if a["state"] == "active"]
                occupied = {a["slot"] for a in active if a["worker_id"] == worker["id"]}
                slot = next(
                    (
                        n
                        for n in range(min(worker["slots"], data["available_slots"]))
                        if n not in occupied
                    ),
                    None,
                )
                if slot is None:
                    return {"task": None}
                for c in self.rows(tx, "child"):
                    if c["state"] != "ready":
                        continue
                    root = self.get(tx, "root", c["root_id"])
                    if (
                        root["pool"] not in worker["pools"]
                        or root["cancel_requested"]
                        or root["state"] in TERMINAL | {"needs_review"}
                    ):
                        continue
                    if (
                        sum(a["root_id"] == root["id"] for a in active)
                        >= root["policy"]["max_parallel_children"]
                    ):
                        continue
                    if (
                        sum(
                            self.get(tx, "root", a["root_id"])["owner"] == root["owner"]
                            for a in active
                        )
                        >= 8
                    ):
                        continue
                    c["epoch"] += 1
                    c["state"] = "running"
                    c["active_attempt"] = uid()
                    a = {
                        "id": c["active_attempt"],
                        "root_id": root["id"],
                        "task_id": c["id"],
                        "worker_id": worker["id"],
                        "slot": slot,
                        "epoch": c["epoch"],
                        "state": "active",
                        "lease_until": min(self.clock() + 30, root["deadline"]),
                        "revision": 0,
                        "last_seq": 0,
                    }
                    self.put(tx, "child", c)
                    self.put(tx, "attempt", a)
                    dependencies = [
                        d
                        for d in self.rows(tx, "child", root["id"])
                        if d["child_key"] in c["depends_on"]
                    ]
                    return {
                        "task": c,
                        "attempt": a,
                        "context": self.get(
                            tx, "context", root["id"] + ":" + str(c["context_version"])
                        ),
                        "dependency_results": [
                            {
                                k: d[k]
                                for k in (
                                    "id",
                                    "context_version",
                                    "artifact_ids",
                                    "selected_artifact_ids",
                                )
                            }
                            for d in dependencies
                        ],
                        "deadline_at": root["deadline_at"],
                    }
                return {"task": None}

            # Replay returns the same claim, never an extra slot; subsequent calls must fence.
            return self.idem(tx, worker["id"] + ":claim", key, data, apply)

    def active(self, tx, worker, id, payload, allow_stop=False):
        a = self.get(tx, "attempt", id)
        c = self.get(tx, "child", a["task_id"])
        r = self.get(tx, "root", a["root_id"])
        if a["worker_id"] != worker["id"] or r["pool"] not in worker["pools"]:
            raise Fault("NOT_FOUND", 404)
        if (
            a["epoch"] != payload.epoch
            or c["epoch"] != payload.epoch
            or c["active_attempt"] != id
            or c["id"] != payload.task_id
        ):
            raise Fault("STALE_EPOCH", 409)
        if (
            a["state"] != "active"
            or a["lease_until"] <= self.clock()
            or r["deadline"] <= self.clock()
        ):
            raise Fault("LEASE_EXPIRED", 409)
        revoked = any(
            self.binding(tx, r[k + "_binding"])["revoked"] for k in ("agent", "image")
        )
        if not allow_stop and (
            revoked
            or c["state"] != "running"
            or r["cancel_requested"]
            or r["state"] == "needs_review"
        ):
            raise Fault("EXECUTION_STOPPED", 409)
        return r, c, a

    def renew(self, w, id, p, key):
        with self.tx() as tx:
            self.refresh(tx)
            r, c, a = self.active(tx, w, id, p, True)

            def apply():
                a["lease_until"] = min(self.clock() + 30, r["deadline"])
                self.put(tx, "attempt", a)
                return {"lease_expires_at": stamp(a["lease_until"])}

            result = self.idem(tx, w["id"] + ":renew:" + id, key, p.model_dump(), apply)
            stop = (
                c["state"] != "running"
                or r["cancel_requested"]
                or r["state"] == "needs_review"
                or any(
                    self.binding(tx, r[k + "_binding"])["revoked"]
                    for k in ("agent", "image")
                )
            )
            return result | {"authorized": not stop, "stop_requested": stop}

    def checkpoint(self, w, id, p, key):
        with self.tx() as tx:
            r, c, a = self.active(tx, w, id, p)

            def apply():
                if a["revision"] != p.expected_revision:
                    raise Fault("REVISION_CONFLICT", 409)
                for oid in p.completed_tool_calls:
                    op = self.get(tx, "operation", oid)
                    if op["attempt_id"] != id or op["state"] not in {
                        "succeeded",
                        "failed",
                    }:
                        raise Fault("INVALID_CHECKPOINT", 422)
                for aid in p.artifact_ids:
                    art = self.get(tx, "artifact", aid)
                    if art["attempt_id"] != id or art["state"] != "committed":
                        raise Fault("INVALID_CHECKPOINT", 422)
                a["revision"] += 1
                a["checkpoint"] = p.model_dump()
                self.put(tx, "attempt", a)
                return {"revision": a["revision"]}

            return self.idem(tx, id + ":checkpoint", key, p.model_dump(), apply)

    def reserve(self, w, id, p, key):
        with self.tx() as tx:
            self.refresh(tx)
            r, c, a = self.active(tx, w, id, p)

            def apply():
                existing = self.maybe(tx, "operation", p.operation_id)
                if existing and existing["attempt_id"] != id:
                    raise Fault("OPERATION_CONFLICT", 409)

                for old in self.rows(tx, "operation", r["id"]):
                    if old["id"] == p.operation_id or (
                        old["attempt_id"] == id
                        and old["tool_call_id"] == p.tool_call_id
                    ):
                        if old["digest"] != sha(p.model_dump()):
                            raise Fault("OPERATION_CONFLICT", 409)
                        return old
                purpose = "agent" if p.kind == "agent" else "image"
                b = self.binding(tx, r[purpose + "_binding"])
                if b["revoked"]:
                    raise Fault("MODEL_NOT_ALLOWED", 403)
                if p.kind != "agent" and (p.kind != c["kind"] or p.count != c["count"]):
                    raise Fault("TOOL_FORBIDDEN", 403)
                if any(
                    o["attempt_id"] == id
                    and (o["kind"] == "agent") == (p.kind == "agent")
                    for o in self.rows(tx, "operation", r["id"])
                ):
                    raise Fault("OPERATION_LIMIT", 429)
                refs = self.get(
                    tx, "context", r["id"] + ":" + str(c["context_version"])
                )["reference_artifact_ids"]
                if p.reference_artifact_ids != (refs if p.kind == "edit" else []):
                    raise Fault("REFERENCE_FORBIDDEN", 403)
                operations = self.rows(tx, "operation")
                if (
                    sum(
                        o["quota_pool_id"] == b["quota_pool_id"]
                        and o["state"] in UNSETTLED
                        for o in operations
                    )
                    >= b["max_inflight"]
                ):
                    raise Fault("QUOTA_EXCEEDED", 429)
                amount = b["reserve_micro"] * (p.count if purpose == "image" else 1)
                if (
                    r["actual_micro"] + r["reserved_micro"] + amount
                    > r["cost_limit_micro"]
                    or c["actual_micro"] + c["reserved_micro"] + amount
                    > c["budget_limit_micro"]
                ):
                    raise Fault("BUDGET_EXCEEDED", 429)
                op = p.model_dump() | {
                    "id": p.operation_id,
                    "root_id": r["id"],
                    "attempt_id": id,
                    "state": "reserved",
                    "digest": sha(p.model_dump()),
                    "binding_id": b["id"],
                    "quota_pool_id": b["quota_pool_id"],
                    "reserved_micro": amount,
                    "actual_micro": None,
                    "price_version": b["price_version"],
                    "size": c["size"],
                    "quality": c["quality"],
                }
                r["reserved_micro"] += amount
                c["reserved_micro"] += amount
                self.put(tx, "root", r)
                self.put(tx, "child", c)
                self.put(tx, "operation", op)
                self.ledger(tx, op, "reserve", amount)
                return op

            return self.idem(tx, id + ":reserve", key, p.model_dump(), apply)

    def ledger(self, tx, op, kind, amount):
        self.put(
            tx,
            "ledger",
            {
                "id": uid(),
                "root_id": op["root_id"],
                "operation_id": op["id"],
                "kind": kind,
                "amount_micro": amount,
                "at": stamp(self.clock()),
                "price_version": op["price_version"],
            },
        )

    def dispatch(self, w, oid, p, key):
        with self.tx() as tx:
            op = self.get(tx, "operation", oid)
            r, c, a = self.active(tx, w, op["attempt_id"], p)
            if op["state"] != "reserved":
                raise Fault("DISPATCH_ALREADY_CONSUMED", 409)
            if self.get(tx, "binding", op["binding_id"])["revoked"]:
                raise Fault("MODEL_NOT_ALLOWED", 403)
            op["state"] = "dispatching"
            self.put(tx, "operation", op)
            # Dispatch is intentionally one-shot even for transport retry: lost ACK => unknown.
            return {"dispatch": True, "operation": op}

    def credentials(self, w, id, p):
        with self.tx() as tx:
            r, c, a = self.active(tx, w, id, p)
            op = self.get(tx, "operation", p.operation_id)
            if op["attempt_id"] != id or op["state"] != "reserved":
                raise Fault("CREDENTIAL_FORBIDDEN", 403)
            b = self.get(tx, "binding", op["binding_id"])
            if b["revoked"]:
                raise Fault("MODEL_NOT_ALLOWED", 403)
            return {
                "binding": self.public_binding(b),
                "secret": self.keys.unseal("v3-provider", b["secret_cipher"]),
                "lease_expires_at": stamp(a["lease_until"]),
                "provider_expires_at": None,
            }

    def settle(self, tx, op, cost, state):
        if op["state"] not in UNSETTLED:
            raise Fault("ALREADY_SETTLED", 409)
        r = self.get(tx, "root", op["root_id"])
        c = self.get(tx, "child", op["task_id"])
        r["reserved_micro"] -= op["reserved_micro"]
        c["reserved_micro"] -= op["reserved_micro"]
        r["actual_micro"] += cost
        c["actual_micro"] += cost
        op["state"] = state
        op["actual_micro"] = cost
        if cost > op["reserved_micro"]:
            c["state"] = "needs_review"
            r["state"] = "needs_review"
            r["billing_violation"] = True
            b = self.get(tx, "binding", op["binding_id"])
            b["revoked"] = True
            self.put(tx, "binding", b)
        self.put(tx, "operation", op)
        self.put(tx, "root", r)
        self.put(tx, "child", c)
        self.ledger(tx, op, "settle", cost)

    def report(self, w, oid, p, key):
        with self.tx() as tx:
            op = self.get(tx, "operation", oid)
            self.active(tx, w, op["attempt_id"], p, True)

            def apply():
                if op["state"] not in {"dispatching", "accepted", "unknown"}:
                    raise Fault("OPERATION_NOT_DISPATCHED", 409)
                op["provider_job_id"] = p.provider_job_id
                op["provider_request_id"] = p.provider_request_id
                if p.state in {"succeeded", "failed"}:
                    if p.actual_cost_micro is None:
                        raise Fault("COST_REQUIRED", 422)
                    self.settle(tx, op, p.actual_cost_micro, p.state)
                else:
                    if p.state == "accepted" and not p.provider_job_id:
                        raise Fault("JOB_ID_REQUIRED", 422)
                    op["state"] = p.state
                    self.put(tx, "operation", op)
                    if p.state == "unknown":
                        c = self.get(tx, "child", op["task_id"])
                        c["state"] = "needs_review"
                        self.put(tx, "child", c)
                return self.get(tx, "operation", oid)

            result = self.idem(tx, oid + ":report", key, p.model_dump(), apply)
            self.refresh(tx)
            return result

    def complete(self, w, id, p, key):
        with self.tx() as tx:
            a = self.get(tx, "attempt", id)
            if (
                a["worker_id"] != w["id"]
                or a["task_id"] != p.task_id
                or a["epoch"] != p.epoch
            ):
                raise Fault("STALE_EPOCH", 409)

            def apply():
                r, c, a = self.active(tx, w, id, p, True)
                ops = [
                    o
                    for o in self.rows(tx, "operation", r["id"])
                    if o["attempt_id"] == id
                ]
                if any(o["state"] in UNSETTLED for o in ops) or r.get(
                    "billing_violation"
                ):
                    raise Fault("OPERATION_OUTCOME_UNKNOWN", 409)
                if len(set(p.artifact_ids)) != len(p.artifact_ids) or len(
                    set(p.selected_artifact_ids)
                ) != len(p.selected_artifact_ids):
                    raise Fault("DUPLICATE_ARTIFACT", 422)
                if p.outcome == "succeeded":
                    if r["cancel_requested"] or c["state"] != "running":
                        raise Fault("EXECUTION_STOPPED", 409)
                    if not any(
                        o["kind"] == "agent" and o["state"] == "succeeded" for o in ops
                    ):
                        raise Fault("AGENT_NOT_SETTLED", 409)
                    if c["kind"] != "selection" and (
                        len(p.artifact_ids) != c["count"]
                        or not any(
                            o["kind"] == c["kind"] and o["state"] == "succeeded"
                            for o in ops
                        )
                    ):
                        raise Fault("OUTPUT_COUNT_MISMATCH", 409)
                for aid in p.artifact_ids:
                    art = self.get(tx, "artifact", aid)
                    if art["attempt_id"] != id or art["state"] != "committed":
                        raise Fault("ARTIFACT_NOT_COMMITTED", 409)
                allowed = {
                    aid
                    for d in self.rows(tx, "child", r["id"])
                    if d["child_key"] in c["depends_on"]
                    for aid in d["artifact_ids"]
                }
                if not set(p.selected_artifact_ids) <= allowed or (
                    c["kind"] == "selection"
                    and p.outcome == "succeeded"
                    and not p.selected_artifact_ids
                ):
                    raise Fault("SELECTION_INVALID", 422)
                c.update(
                    state=p.outcome,
                    artifact_ids=p.artifact_ids,
                    selected_artifact_ids=p.selected_artifact_ids,
                    summary=p.summary,
                )
                a["state"] = "finished"
                self.put(tx, "child", c)
                self.put(tx, "attempt", a)
                self.refresh(tx)
                return c

            return self.idem(tx, id + ":complete", key, p.model_dump(), apply)

    def cancel(self, user, id, p, key):
        with self.tx() as tx:

            def apply():
                r = self.owned(tx, id, user)
                if r["state_version"] != p.expected_version:
                    raise Fault("VERSION_CONFLICT", 409)
                if r["state"] in TERMINAL:
                    return r
                r["cancel_requested"] = True
                r["state_version"] += 1
                self.put(tx, "root", r)
                self.refresh(tx)
                return self.get(tx, "root", id)

            return self.idem(
                tx, user["id"] + ":cancel:" + id, key, p.model_dump(), apply
            )

    def inspect(self, user, id):
        with self.tx() as tx:
            self.refresh(tx)
            r = self.owned(tx, id, user)
            return {
                "task": r,
                "children": self.rows(tx, "child", id),
                "artifacts": [
                    self.artifact_view(a)
                    for a in self.rows(tx, "artifact", id)
                    if a["state"] == "committed"
                ],
                "usage": self.rows(tx, "ledger", id),
            }

    def events(self, w, id, p, key):
        with self.tx() as tx:
            r, c, a = self.active(tx, w, id, p, True)

            def apply():
                for event in p.events:
                    value = event.model_dump()
                    eid = id + ":" + str(event.seq)
                    old = self.maybe(tx, "event", eid)
                    if old:
                        if old["payload"] != value:
                            raise Fault("EVENT_CONFLICT", 409)
                        continue
                    if event.seq != a["last_seq"] + 1:
                        raise Fault("EVENT_SEQUENCE_GAP", 409)
                    for e in self.rows(tx, "event", r["id"]):
                        if e["payload"]["event_id"] == event.event_id:
                            raise Fault("EVENT_CONFLICT", 409)
                    if (
                        event.operation_id
                        and self.get(tx, "operation", event.operation_id)["attempt_id"]
                        != id
                    ):
                        raise Fault("NOT_FOUND", 404)
                    r["event_sequence"] = r.get("event_sequence", 0) + 1
                    self.put(tx, "root", r)
                    self.put(
                        tx,
                        "event",
                        {
                            "sequence": r["event_sequence"],
                            "id": eid,
                            "root_id": r["id"],
                            "attempt_id": id,
                            "payload": value,
                            "at": stamp(self.clock()),
                        },
                    )
                    a["last_seq"] = event.seq
                self.put(tx, "attempt", a)
                return {"contiguous_ack": a["last_seq"]}

            return self.idem(tx, id + ":events", key, p.model_dump(), apply)

    def review(self, admin, oid, p, key):
        with self.tx() as tx:

            def apply():
                op = self.get(tx, "operation", oid)
                if op["state"] not in {"unknown", "accepted", "dispatching"}:
                    raise Fault("NOT_REVIEWABLE", 409)
                self.settle(tx, op, p.actual_cost_micro, p.outcome)
                a = self.get(tx, "attempt", op["attempt_id"])
                a["state"] = "finished"
                self.put(tx, "attempt", a)
                c = self.get(tx, "child", op["task_id"])
                c["state"] = "failed"
                self.put(tx, "child", c)
                self.put(
                    tx,
                    "audit",
                    {
                        "id": uid(),
                        "root_id": op["root_id"],
                        "actor": admin["id"],
                        "operation_id": oid,
                        "evidence": p.evidence,
                        "at": stamp(self.clock()),
                    },
                )
                self.refresh(tx)
                return self.get(tx, "operation", oid)

            return self.idem(tx, "review:" + oid, key, p.model_dump(), apply)

    @staticmethod
    def artifact_view(a):
        return {
            k: a[k]
            for k in (
                "id",
                "state",
                "media_type",
                "size_bytes",
                "sha256",
                "width",
                "height",
            )
            if k in a
        }

    def abandon(self, w, id, p):
        with self.tx() as tx:
            a = self.get(tx, "attempt", id)
            if (
                a["worker_id"] != w["id"]
                or a["task_id"] != p.task_id
                or a["epoch"] != p.epoch
            ):
                raise Fault("STALE_EPOCH", 409)
            if a["state"] == "finished":
                return {"state": "finished"}
            c = self.get(tx, "child", a["task_id"])
            for op in self.rows(tx, "operation", a["root_id"]):
                if op["attempt_id"] != id:
                    continue
                if op["state"] == "reserved":
                    self.settle(tx, op, 0, "failed")
                elif op["state"] in {"dispatching", "accepted"}:
                    op["state"] = "unknown"
                    self.put(tx, "operation", op)
            c = self.get(tx, "child", a["task_id"])
            c["state"] = "needs_review"
            a["state"] = "expired"
            self.put(tx, "attempt", a)
            self.put(tx, "child", c)
            self.refresh(tx)
            return {"state": "needs_review"}

    def close_review(self, admin, id, p, key):
        with self.tx() as tx:

            def apply():
                c = self.get(tx, "child", id)
                if c["state"] != "needs_review":
                    raise Fault("NOT_REVIEWABLE", 409)
                if any(
                    o["task_id"] == id and o["state"] in UNSETTLED
                    for o in self.rows(tx, "operation", c["root_id"])
                ):
                    raise Fault("OPERATION_OUTCOME_UNKNOWN", 409)
                if c["active_attempt"]:
                    a = self.get(tx, "attempt", c["active_attempt"])
                    a["state"] = "finished"
                    self.put(tx, "attempt", a)
                c["state"] = "failed"
                self.put(tx, "child", c)
                self.put(
                    tx,
                    "audit",
                    {
                        "id": uid(),
                        "root_id": c["root_id"],
                        "actor": admin["id"],
                        "task_id": id,
                        "evidence": p.evidence,
                        "at": stamp(self.clock()),
                    },
                )
                self.refresh(tx)
                return c

            return self.idem(tx, "close-review:" + id, key, p.model_dump(), apply)
