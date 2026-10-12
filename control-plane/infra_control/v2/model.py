"""Quota reservations and append-only money ledger, with unknown-outcome fencing."""

import asyncio
import json
import random
import httpx
from ..db import encode
from ..service import Fault
from .security import sha
from .service import uid
from .store import lock, get_task, object_get, object_put
from .budget import request_body, reserve_bound


class ModelGateway:
    def __init__(self, platform, config, *, transport=None):
        self.p, self.config, self.transport = platform, config, transport
        platform.model_config = config
        if config:
            required = {
                "url",
                "key",
                "model",
                "quota_pool_id",
                "price_version",
                "currency",
                "input_micro_per_million",
                "output_micro_per_million",
            }
            if not required <= config.keys():
                raise ValueError(
                    "Complete versioned model pricing and quota pool required"
                )
            if any(
                type(config[k]) is not int or config[k] < 0
                for k in ("input_micro_per_million", "output_micro_per_million")
            ):
                raise ValueError("Integer nonnegative prices required")
            url = httpx.URL(config["url"])
            if url.scheme != "https" and url.host not in {
                "localhost",
                "127.0.0.1",
                "gateway",
            }:
                raise ValueError("Model upstream requires TLS")

    def ledger(self, tx, call, kind, tokens, cost, evidence=None):
        value = {
            "id": uid(),
            "request_id": call["id"],
            "task_id": call["task_id"],
            "kind": kind,
            "tokens": tokens,
            "cost": cost,
            "currency": call["currency"],
            "price_version": call["price_version"],
            "evidence_ref": evidence,
        }
        tx.execute(
            "INSERT INTO v2_ledger(id,request_id,task_id,created,body) VALUES(?,?,?,?,?)",
            (value["id"], call["id"], call["task_id"], self.p.clock(), encode(value)),
        )

    def reserve(self, node, attempt_id, payload):
        p = self.p
        cfg = self.config
        if not cfg:
            raise Fault("model_not_configured", 503)
        checksum = sha(encode(payload.model_dump()))
        with p.db.transaction(serialized=False) as tx:
            p.node(tx, node)
            task, attempt = p.active(tx, node, attempt_id, payload.epoch)
            if not attempt.get("isolation_verified"):
                raise Fault("isolation_not_verified", 403)
            if payload.budget_ref != task["id"]:
                raise Fault("budget_ref_mismatch", 422)
            if (
                task["data_class"] == "private"
                or attempt["simulated"]
                or task["agent"]["mode"] != "agent"
            ):
                raise Fault("model_forbidden", 403)
            if task["data_class"] == "edge_summary":
                summary = attempt.get("approved_summary")
                if (
                    not summary
                    or payload.messages[0].role != "user"
                    or payload.messages[0].content != encode(summary)
                ):
                    raise Fault("summary_context_mismatch", 403)
            lock(tx, "quota:" + cfg["quota_pool_id"])
            old = tx.execute(
                "SELECT body FROM v2_usage WHERE id=?", (payload.request_id,)
            ).fetchone()
            if old:
                call = json.loads(old[0])
                if (
                    call["task_id"] != task["id"]
                    or call["attempt_id"] != attempt_id
                    or call["hash"] != checksum
                ):
                    raise Fault("model_idempotency_conflict")
                if call["status"] != "complete":
                    raise Fault("model_outcome_unknown")
                return call, False, task
            if task["budget"]["currency"] != cfg["currency"]:
                raise Fault("currency_mismatch", 422)
            count = tx.execute(
                "SELECT COUNT(*) FROM v2_usage WHERE task_id=?", (task["id"],)
            ).fetchone()[0]
            if count >= task["agent"]["max_steps"]:
                raise Fault("model_step_limit", 429)
            input_bound, output_bound, cost, route = reserve_bound(
                task, payload, cfg, p.clock()
            )
            if object_get(tx, "blocked_model_route", route["digest"]):
                raise Fault("billing_route_quarantined", 503)
            estimate = input_bound + output_bound
            if (
                task["reserved_tokens"] + task["actual_tokens"] + estimate
                > task["budget"]["token_limit"]
                or task["reserved_cost"] + task["actual_cost"] + cost
                > task["budget"]["cost_limit"]
            ):
                raise Fault("task_budget_exceeded", 429)
            rows = [
                json.loads(r[0])
                for r in tx.execute(
                    "SELECT body FROM v2_usage WHERE pool=? AND (created>? OR status IN ('pending','unknown'))",
                    (cfg["quota_pool_id"], p.clock() - 60),
                ).fetchall()
            ]
            if (
                sum(c["status"] in {"pending", "unknown"} for c in rows)
                >= cfg.get("parallel", 2)
                or sum(c["created"] > p.clock() - 60 for c in rows)
                >= cfg.get("rpm", 20)
                or sum(
                    c["reserved_tokens"]
                    if c["status"] in {"pending", "unknown"}
                    else c.get("actual_tokens", 0)
                    for c in rows
                )
                + estimate
                > cfg.get("tpm", 200000)
            ):
                raise Fault("quota_exceeded", 429)
            call = {
                "id": payload.request_id,
                "task_id": task["id"],
                "attempt_id": attempt_id,
                "step_id": payload.step_id,
                "hash": checksum,
                "quota_pool_id": cfg["quota_pool_id"],
                "price_version": cfg["price_version"],
                "currency": cfg["currency"],
                "reserved_tokens": estimate,
                "input_bound": input_bound,
                "output_bound": output_bound,
                "hard": task["budget"]["mode"] == "hard",
                "route_digest": route["digest"],
                "reserved_cost": cost,
                "status": "pending",
                "created": p.clock(),
                "input_price": cfg["input_micro_per_million"],
                "output_price": cfg["output_micro_per_million"],
                "version": 1,
            }
            tx.execute(
                "INSERT INTO v2_usage(id,task_id,pool,status,created,body) VALUES(?,?,?,?,?,?)",
                (
                    call["id"],
                    task["id"],
                    cfg["quota_pool_id"],
                    "pending",
                    p.clock(),
                    encode(call),
                ),
            )
            task["reserved_tokens"] += estimate
            task["reserved_cost"] += cost
            self.ledger(tx, call, "reserve", estimate, cost)
            p.event(tx, task, "model_reserved", {"request_id": call["id"]})
            return call, True, task

    def finish(self, call, response=None, *, unexecuted=False):
        p = self.p
        with p.db.transaction(serialized=False) as tx:
            task = get_task(tx, call["task_id"])
            lock(tx, "quota:" + call["quota_pool_id"])
            call = json.loads(
                tx.execute(
                    "SELECT body FROM v2_usage WHERE id=?", (call["id"],)
                ).fetchone()[0]
            )
            if call["status"] != "pending":
                return
            if response is None and not unexecuted:
                call["status"] = "unknown"
                task["state"] = "needs_review"
                self.ledger(tx, call, "unknown", 0, 0)
            else:
                actual = response["usage_tokens"] if response else 0
                cost = response["cost"] if response else 0
                call.update(
                    status="complete" if response else "unexecuted",
                    actual_tokens=actual,
                    actual_cost=cost,
                    response=response,
                )
                task["reserved_tokens"] -= call["reserved_tokens"]
                task["reserved_cost"] -= call["reserved_cost"]
                task["actual_tokens"] += actual
                task["actual_cost"] += cost
                self.ledger(
                    tx,
                    call,
                    "release",
                    -call["reserved_tokens"],
                    -call["reserved_cost"],
                )
                self.ledger(tx, call, "charge", actual, cost)
                if call.get("hard") and (
                    response
                    and (
                        response.get("input_tokens", 0) > call["input_bound"]
                        or response.get("output_tokens", 0) > call["output_bound"]
                        or cost > call["reserved_cost"]
                    )
                ):
                    call["bound_violation"] = True
                    task["billing_violation"] = True
                    task["state"] = "needs_review"
                    object_put(
                        tx,
                        "blocked_model_route",
                        call["route_digest"],
                        {"request_id": call["id"]},
                    )
                    self.ledger(tx, call, "bound_violation", 0, 0)

            call["version"] += 1
            tx.execute(
                "UPDATE v2_usage SET status=?,body=? WHERE id=?",
                (call["status"], encode(call), call["id"]),
            )
            p.event(tx, task, "model_" + call["status"], {"request_id": call["id"]})

    async def invoke(self, node, attempt_id, payload):
        call, fresh, task = await asyncio.to_thread(
            self.reserve, node, attempt_id, payload
        )
        if not fresh:
            return call["response"]
        cfg = self.config
        request = request_body(task, payload, cfg)
        proven_unexecuted = False
        try:
            async with (
                asyncio.timeout(60),
                httpx.AsyncClient(
                    timeout=20, follow_redirects=False, transport=self.transport
                ) as client,
            ):
                for attempt_number in range(3):
                    # Recheck authority before every HTTP attempt. Unknown transport outcomes never retry.
                    with self.p.db.transaction(serialized=False) as tx:
                        self.p.node(tx, node)
                        self.p.active(tx, node, attempt_id, payload.epoch)
                    proven_unexecuted = False
                    async with client.stream(
                        "POST",
                        cfg["url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + cfg["key"]},
                        json=request,
                    ) as response:
                        # Upstream contract must explicitly guarantee this marker means no execution/billing.
                        if (
                            response.status_code == 429
                            and cfg.get("trusted_unexecuted_429")
                            and response.headers.get("X-Request-Executed") == "false"
                        ):
                            proven_unexecuted = True
                            if attempt_number == 2:
                                break
                            try:
                                delay = max(
                                    0,
                                    min(
                                        20,
                                        float(response.headers.get("Retry-After", "1")),
                                    ),
                                )
                            except ValueError:
                                delay = 1
                            await asyncio.sleep(delay + random.random())
                            continue
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 131072:
                                raise ValueError("response_limit")
                    raw = json.loads(data)
                    text = raw["choices"][0]["message"]["content"]
                    usage = raw["usage"]
                    i, o = usage["prompt_tokens"], usage["completion_tokens"]
                    if (
                        not isinstance(text, str)
                        or len(text) > 32768
                        or any(
                            type(n) is not int or not 0 <= n <= 1000000 for n in (i, o)
                        )
                    ):
                        raise ValueError("invalid_usage")
                    answer = {
                        "content": text,
                        "usage_tokens": i + o,
                        "input_tokens": i,
                        "output_tokens": o,
                        "cost": (
                            i * call["input_price"] + o * call["output_price"] + 999999
                        )
                        // 1000000,
                        "currency": call["currency"],
                        "request_id": call["id"],
                    }
                    await asyncio.to_thread(self.finish, call, answer)
                    return answer
        except BaseException:
            await asyncio.shield(
                asyncio.to_thread(self.finish, call, unexecuted=proven_unexecuted)
            )
            raise Fault(
                "model_unexecuted" if proven_unexecuted else "model_outcome_unknown",
                502,
            ) from None
        await asyncio.to_thread(self.finish, call, unexecuted=proven_unexecuted)
        raise Fault("upstream_rate_limited", 429)

    def usage(self, actor, task_id):
        with self.p.db.transaction(serialized=False) as tx:
            get_task(tx, task_id, actor, locked=False)
            calls = [
                json.loads(r[0])
                for r in tx.execute(
                    "SELECT body FROM v2_usage WHERE task_id=? ORDER BY created,id",
                    (task_id,),
                ).fetchall()
            ]
            for call in calls:
                call.pop("response", None)
            ledger = [
                json.loads(r[0])
                for r in tx.execute(
                    "SELECT body FROM v2_ledger WHERE task_id=? ORDER BY created,id",
                    (task_id,),
                ).fetchall()
            ]
            return {"requests": calls, "ledger": ledger}

    def reconcile(self, actor, task_id, request_id, payload, key):
        p = self.p
        with p.db.transaction(serialized=False) as tx:

            def apply():
                task = get_task(tx, task_id, actor)
                row = tx.execute(
                    "SELECT body FROM v2_usage WHERE id=? AND task_id=?",
                    (request_id, task_id),
                ).fetchone()
                if not row:
                    raise Fault("not_found", 404)
                call = json.loads(row[0])
                lock(tx, "quota:" + call["quota_pool_id"])
                if call["version"] != payload.expected_version:
                    raise Fault("version_conflict")
                if (
                    call["status"] not in {"unknown", "pending"}
                    or p.clock() - call["created"] < 60
                ):
                    raise Fault("usage_not_reconcilable")
                if payload.currency != call["currency"]:
                    raise Fault("currency_mismatch", 422)
                task["reserved_tokens"] -= call["reserved_tokens"]
                task["reserved_cost"] -= call["reserved_cost"]
                task["actual_tokens"] += payload.actual_tokens
                task["actual_cost"] += payload.cost
                call.update(
                    status="reconciled",
                    actual_tokens=payload.actual_tokens,
                    actual_cost=payload.cost,
                    version=call["version"] + 1,
                )
                self.ledger(
                    tx,
                    call,
                    "release",
                    -call["reserved_tokens"],
                    -call["reserved_cost"],
                    payload.evidence_ref,
                )
                self.ledger(
                    tx,
                    call,
                    "reconcile",
                    payload.actual_tokens,
                    payload.cost,
                    payload.evidence_ref,
                )
                tx.execute(
                    "UPDATE v2_usage SET status=?,body=? WHERE id=?",
                    ("reconciled", encode(call), request_id),
                )
                p.event(tx, task, "usage_reconciled", {"request_id": request_id})
                p.audit(tx, actor["id"], "usage_reconcile", request_id)
                return call

            return p.idem(
                tx,
                actor["id"] + ":usage:" + request_id,
                key,
                payload.model_dump(),
                apply,
            )
