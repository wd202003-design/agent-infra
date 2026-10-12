"""Single authoritative state writer. No network/SDK calls inside transactions."""
import hashlib
import secrets
import time
import uuid

from contracts.models import TERMINAL
from .db import encode


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_id():
    return str(uuid.uuid4())


class Fault(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class Service:
    LEASE = 30

    def __init__(self, db, *, clock=time.time, quota=None):
        self.db, self.clock = db, clock
        self.quota = quota or {"parallel": 2, "rpm": 20, "tpm": 200000}

    def audit(self, tx, actor, action, target):
        tx.save("audit", {"id": new_id(), "actor": actor, "action": action,
                          "target": target, "at": self.clock()})

    def event(self, tx, task, kind, details=None):
        task["sequence"] += 1
        task["state_version"] += 1
        row = {"sequence": task["sequence"], "at": self.clock(), "type": kind,
               "state": task["state"], "epoch": task["epoch"], "details": details or {}}
        tx.execute("INSERT INTO events(task_id,sequence,body) VALUES(?,?,?)",
                   (task["id"], task["sequence"], encode(row)))
        tx.save("tasks", task)

    @staticmethod
    def visible(task, actor):
        if not task or (actor["role"] != "admin" and task["owner"] != actor["id"]):
            raise Fault("not_found", 404)
        return task

    @staticmethod
    def public(task):
        return {k: v for k, v in task.items() if k not in {"idem_key", "input_hash"}}

    def expire(self, tx):
        now = self.clock()
        for task in tx.all("tasks"):
            if task["state"] == "queued" and task["deadline"] <= now:
                task["state"] = "failed"
                self.event(tx, task, "queue_deadline")
            elif task["state"] == "running" and task["lease_deadline"] <= now:
                task["state"] = "needs_review"
                self.event(tx, task, "lease_expired_stop_unknown")
            # cancel_requested stays pending: lease expiry is not proof of stopping.

    def add_agent(self, actor, definition):
        data = definition.model_dump()
        item = {**data, "id": data["agent_id"] + ":" + data["version"], "digest": digest(encode(data))}
        with self.db.transaction() as tx:
            old = tx.get("agents", item["id"])
            if old and old != item:
                raise Fault("immutable_agent_version")
            tx.save("agents", item)
            self.audit(tx, actor["id"], "register_agent", item["id"])
        return item

    def agents(self):
        with self.db.transaction() as tx:
            return tx.all("agents")

    def submit(self, actor, payload, key):
        if not key or len(key) > 128 or not key.isascii() or not all(32 < ord(c) < 127 for c in key):
            raise Fault("invalid_idempotency_key", 422)
        data = payload.model_dump()
        if payload.pool not in actor["pools"]:
            raise Fault("pool_forbidden", 403)
        hashed = digest(encode(data))
        with self.db.transaction() as tx:
            self.expire(tx)
            for task in tx.all("tasks"):
                if task["owner"] == actor["id"] and task["idem_key"] == key:
                    if task["input_hash"] != hashed:
                        raise Fault("idempotency_conflict")
                    return self.public(task)
            agent = tx.get("agents", payload.agent_id + ":" + payload.version)
            if not agent:
                raise Fault("agent_not_found", 404)
            if len([t for t in tx.all("tasks") if t["state"] not in TERMINAL]) >= 1000:
                raise Fault("queue_full", 429)
            task = {**data, "id": new_id(), "owner": actor["id"], "idem_key": key,
                    "input_hash": hashed, "agent": agent, "state": "queued", "epoch": 0,
                    "attempt_id": None, "node_id": None, "sequence": 0, "state_version": 0,
                    "created_at": self.clock(), "deadline": self.clock() + payload.timeout_seconds,
                    "lease_deadline": None, "cancel_requested_at": None, "result": None,
                    "report_hash": None, "cleanup_confirmed": False, "stopped": False,
                    "simulated": None, "model_reserved": 0, "model_actual": 0}
            tx.save("tasks", task)
            self.event(tx, task, "accepted")
            return self.public(task)

    def tasks(self, actor):
        with self.db.transaction() as tx:
            self.expire(tx)
            return [self.public(t) for t in sorted(tx.all("tasks"), key=lambda t: t["created_at"], reverse=True)
                    if actor["role"] == "admin" or t["owner"] == actor["id"]][:100]

    def task(self, actor, task_id):
        with self.db.transaction() as tx:
            self.expire(tx)
            return self.public(self.visible(tx.get("tasks", task_id), actor))

    def events(self, actor, task_id, after):
        import json
        with self.db.transaction() as tx:
            self.visible(tx.get("tasks", task_id), actor)
            rows = tx.execute("SELECT body FROM events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT 100",
                              (task_id, after)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def cancel(self, actor, task_id):
        with self.db.transaction() as tx:
            task = self.visible(tx.get("tasks", task_id), actor)
            if task["state"] not in TERMINAL and not task["cancel_requested_at"]:
                task["cancel_requested_at"] = self.clock()
                if task["state"] == "queued":
                    task["state"] = "cancelled"
                elif task["state"] != "needs_review":
                    task["state"] = "cancel_requested"
                self.event(tx, task, "cancel_requested")
            return self.public(task)

    def add_node(self, actor, spec):
        token = secrets.token_urlsafe(32)
        node = {**spec.model_dump(), "id": new_id(), "token_hash": digest(token),
                "draining": False, "revoked": False, "last_seen": None}
        with self.db.transaction() as tx:
            tx.save("nodes", node)
            self.audit(tx, actor["id"], "register_node", node["id"])
        return {"node": self.public_node(node), "token": token}

    @staticmethod
    def public_node(node):
        return {k: v for k, v in node.items() if k != "token_hash"}

    def nodes(self):
        with self.db.transaction() as tx:
            return [self.public_node(n) for n in tx.all("nodes")]

    def authenticate_node(self, token):
        with self.db.transaction() as tx:
            for node in tx.all("nodes"):
                if secrets.compare_digest(node["token_hash"], digest(token)) and not node["revoked"]:
                    return node["id"]
        raise Fault("unauthorized_node", 401)

    def node(self, tx, node_id):
        node = tx.get("nodes", node_id)
        if not node or node["revoked"]:
            raise Fault("revoked_node", 403)
        return node

    def patch_node(self, actor, node_id, patch):
        with self.db.transaction() as tx:
            node = tx.get("nodes", node_id)
            if not node:
                raise Fault("not_found", 404)
            if node["revoked"] and patch.revoked is False:
                raise Fault("credential_revocation_is_permanent")
            node.update(patch.model_dump(exclude_none=True))
            tx.save("nodes", node)
            self.audit(tx, actor["id"], "update_node", node_id)
            return self.public_node(node)

    def claim(self, node_id, request):
        with self.db.transaction() as tx:
            node = self.node(tx, node_id)
            self.expire(tx)
            node["last_seen"] = self.clock()
            tx.save("nodes", node)
            key = node_id + ":" + request.claim_id
            old = tx.get("claims", key)
            if old:
                task = tx.get("tasks", old["task_id"])
                return self.public(task)
            if node["draining"]:
                return None
            tasks = tx.all("tasks")
            if any(t["node_id"] == node_id and t["state"] not in TERMINAL for t in tasks):
                return None
            for task in sorted(tasks, key=lambda t: t["created_at"]):
                if task["state"] != "queued" or task["pool"] != node["pool"]:
                    continue
                if not set(task["agent"]["tools"]).issubset(node["capabilities"]):
                    continue
                task.update(state="running", node_id=node_id, attempt_id=new_id(), epoch=task["epoch"] + 1,
                            lease_deadline=min(self.clock() + self.LEASE, task["deadline"]),
                            simulated=node["backend"] == "demo", backend=node["backend"])
                self.event(tx, task, "claimed")
                tx.save("claims", {"id": key, "task_id": task["id"]})
                return self.public(task)
            return None

    def active(self, tx, node_id, task_id, attempt, *, allow_review=False):
        self.node(tx, node_id)
        task = tx.get("tasks", task_id)
        if not task or task["node_id"] != node_id or task["attempt_id"] != attempt.attempt_id or task["epoch"] != attempt.epoch:
            raise Fault("stale_attempt")
        if not allow_review and (task["state"] != "running" or task["lease_deadline"] <= self.clock()
                                 or task["deadline"] <= self.clock()):
            raise Fault("execution_not_authorized")
        return task

    def renew(self, node_id, task_id, attempt):
        with self.db.transaction() as tx:
            task = self.active(tx, node_id, task_id, attempt, allow_review=True)
            if task["state"] == "running" and task["lease_deadline"] > self.clock() and task["deadline"] > self.clock():
                task["lease_deadline"] = min(self.clock() + self.LEASE, task["deadline"])
                tx.save("tasks", task)
                node = self.node(tx, node_id)
                node["last_seen"] = self.clock()
                tx.save("nodes", node)
                return {"authorized": True, "lease_seconds": task["lease_deadline"] - self.clock()}
            return {"authorized": False, "state": task["state"], "lease_seconds": 0}

    def report(self, node_id, task_id, report):
        with self.db.transaction() as tx:
            task = self.active(tx, node_id, task_id, report, allow_review=True)
            hashed = digest(encode(report.model_dump()))
            if task["report_hash"]:
                if task["report_hash"] != hashed:
                    raise Fault("report_conflict")
                return self.public(task)
            if task["state"] in TERMINAL:
                raise Fault("terminal_immutable")
            if report.simulated != task["simulated"]:
                raise Fault("backend_mismatch")
            task.update(report_hash=hashed, result=report.result, stopped=report.stopped,
                        cleanup_confirmed=report.cleanup_confirmed, evidence=report.evidence)
            unknown_usage = any(c["task_id"] == task_id and c["status"] != "completed" for c in tx.all("model_calls"))
            if (not report.stopped or not report.cleanup_confirmed or unknown_usage
                    or task["state"] == "needs_review" or task["lease_deadline"] <= self.clock()):
                task["state"] = "needs_review"
            elif task["cancel_requested_at"] and report.outcome != "needs_review":
                task["state"] = "cancelled"
            else:
                task["state"] = report.outcome
            self.event(tx, task, "execution_report", {"evidence": report.evidence})
            return self.public(task)

    def review(self, actor, task_id, review):
        with self.db.transaction() as tx:
            task = tx.get("tasks", task_id)
            if not task or task["state"] not in {"needs_review", "cancel_requested"}:
                raise Fault("not_reviewable")
            if review.outcome == "succeeded" and (not task["result"] or task["cancel_requested_at"]):
                raise Fault("success_not_proven")
            if any(c["task_id"] == task_id and c["status"] != "completed" for c in tx.all("model_calls")):
                raise Fault("model_usage_unreconciled")
            task.update(state=review.outcome, stopped=True, cleanup_confirmed=True, evidence=review.evidence)
            self.event(tx, task, "admin_review", {"actor": actor["id"], "evidence": review.evidence})
            self.audit(tx, actor["id"], "review_task", task_id)
            return self.public(task)

    def reserve_model(self, node_id, task_id, request):
        with self.db.transaction() as tx:
            task = self.active(tx, node_id, task_id, request)
            if task["agent"]["mode"] != "agent" or task["simulated"]:
                raise Fault("model_not_enabled")
            key = task_id + ":" + request.request_id
            hashed = digest(encode(request.model_dump()))
            old = tx.get("model_calls", key)
            if old:
                if old["hash"] != hashed:
                    raise Fault("model_idempotency_conflict")
                if old["status"] == "completed":
                    return old, False
                raise Fault("model_outcome_unknown")
            calls = tx.all("model_calls")
            if len([c for c in calls if c["task_id"] == task_id]) >= task["agent"]["max_steps"]:
                raise Fault("model_step_limit")
            messages = [m.model_dump() for m in request.messages]
            # Conservative byte-based estimate, not provider billing or a hard currency guarantee.
            reserved = len(encode(messages).encode()) + len(task["agent"]["instruction"].encode()) + 2048 + task["agent"]["max_output_tokens"]
            if reserved + task["model_reserved"] + task["model_actual"] > task["token_budget"]:
                raise Fault("token_budget_exhausted", 429)
            recent = [c for c in calls if c["created_at"] > self.clock() - 60]
            pending = [c for c in calls if c["status"] != "completed"]
            if len(pending) >= self.quota["parallel"] or len(recent) >= self.quota["rpm"] or sum(max(c["reserved"], c.get("actual", 0)) for c in recent) + reserved > self.quota["tpm"]:
                raise Fault("model_quota_exhausted", 429)
            call = {"id": key, "task_id": task_id, "hash": hashed, "status": "pending",
                    "created_at": self.clock(), "reserved": reserved, "response": None}
            tx.save("model_calls", call)
            task["model_reserved"] += reserved
            self.event(tx, task, "model_reserved", {"request_id": request.request_id, "tokens": reserved})
            return call, True

    def finish_model(self, call_id, response=None, actual=None):
        with self.db.transaction() as tx:
            call = tx.get("model_calls", call_id)
            if call["status"] != "pending":
                raise Fault("model_already_settled")
            task = tx.get("tasks", call["task_id"])
            if response is None or actual is None:
                call["status"] = "unknown"
                task["state"] = "needs_review"
                self.event(tx, task, "model_outcome_unknown")
            else:
                call.update(status="completed", response=response, actual=actual)
                task["model_reserved"] -= call["reserved"]
                task["model_actual"] += actual
                self.event(tx, task, "model_settled", {"tokens": actual})
            tx.save("model_calls", call)
            return call

    def usage(self, actor, task_id):
        with self.db.transaction() as tx:
            self.visible(tx.get("tasks", task_id), actor)
            return [{k: v for k, v in c.items() if k not in {"hash", "response"}}
                    for c in tx.all("model_calls") if c["task_id"] == task_id]

    def reconcile_usage(self, actor, task_id, request_id, review):
        with self.db.transaction() as tx:
            task = self.visible(tx.get("tasks", task_id), actor)
            call = tx.get("model_calls", task_id + ":" + request_id)
            if not call or call["status"] == "completed":
                raise Fault("usage_not_reviewable")
            if call["status"] == "pending" and self.clock() - call["created_at"] < 60:
                raise Fault("model_request_may_be_in_flight")
            call.update(status="completed", actual=review.actual_tokens, evidence=review.evidence,
                        reconciled_by=actor["id"])
            task["model_reserved"] -= call["reserved"]
            task["model_actual"] += review.actual_tokens
            # Reconciliation is not execution authorization or a task retry.
            if task["state"] not in TERMINAL:
                task["state"] = "needs_review"
            self.event(tx, task, "model_usage_reconciled", {"request_id": request_id, "actor": actor["id"]})
            tx.save("model_calls", call)
            self.audit(tx, actor["id"], "reconcile_model_usage", call["id"])
            return {"status": "completed", "actual_tokens": review.actual_tokens}
