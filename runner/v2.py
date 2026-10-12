"""D1 Runner. Durable intent precedes every effect; restart never replays a tool."""

import asyncio
import hashlib
import json
import os
import time
import uuid
import httpx
from contracts.models import Action
from .backends import FACTORIES
from .wal import WAL


def uid():
    return str(uuid.uuid4())


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def checksum(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Client:
    def __init__(self, url, token="", *, transport=None):
        parsed = httpx.URL(url)
        if parsed.scheme != "https" and parsed.host not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "control",
        }:
            raise ValueError("Remote control requires TLS")
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=65,
            follow_redirects=False,
            transport=transport,
        )
        self.token = token

    async def post(self, path, payload, key=None):
        response = await self.http.post(
            "/v2" + path,
            json=payload,
            headers={
                "Authorization": "Bearer " + self.token,
                "Idempotency-Key": key or uid(),
            },
        )
        response.raise_for_status()
        from contracts.wire import convert

        return convert(response.json()["data"], decode=True)

    async def close(self):
        await self.http.aclose()


class Runner:
    def __init__(
        self,
        client,
        wal,
        *,
        backend="demo",
        slots=1,
        factory=None,
        verifier=None,
        workspaces=None,
    ):
        if backend not in FACTORIES or not 1 <= slots <= 32:
            raise ValueError("Unsupported backend/slots")
        self.client, self.wal, self.backend, self.slots = client, wal, backend, slots
        self.factory = factory or FACTORIES[backend]
        self.verifier = verifier
        self.workspaces = workspaces

    async def credentials(self):
        credential = self.wal.load("credentials")
        if credential:
            self.client.token = credential["node_token"]
            if time.time() > credential["expires"] - 43200:
                intent = self.wal.load("rotation") or {
                    "rotation_id": uid(),
                    "protocol": "2.0",
                }
                self.wal.save("rotation", intent)
                renewed = await self.client.post(
                    "/runner/rotate", intent, intent["rotation_id"]
                )
                self.wal.save("credentials", renewed)
                self.client.token = renewed["node_token"]
                self.wal.remove("rotation")
            return
        if self.client.token:
            return  # Explicit short-lived preprovisioned token.
        intent = self.wal.load("registration")
        if not intent:
            intent = {
                "ticket_id": os.environ["AGENT_TICKET_ID"],
                "ticket_secret": os.environ["AGENT_TICKET_SECRET"],
                "registration_id": uid(),
                "protocol": "2.0",
                "instance_public_info": self.backend,
            }
            self.wal.save("registration", intent)
        credential = await self.client.post(
            "/runner/register", intent, intent["registration_id"]
        )
        if (
            credential["policy"]["backend"] != self.backend
            or self.slots > credential["policy"]["slots_limit"]
        ):
            raise RuntimeError("Node policy mismatch")
        self.wal.save("credentials", credential)
        self.client.token = credential["node_token"]
        self.wal.remove("registration")

    async def delete_local_content(self):
        if self.workspaces is None:
            return
        jobs = await self.client.post("/runner/deletions", {})
        for item in jobs:
            await asyncio.to_thread(
                self.workspaces.delete_local_content, item["attempt_id"]
            )
            await self.client.post("/runner/deletions/ack", item, item["attempt_id"])

    async def renew(self, job):
        a = job["attempt"]
        t = job["task"]
        status = await self.client.post(
            "/runner/attempts/" + a["id"] + "/renew",
            {
                "task_id": t["id"],
                "epoch": a["epoch"],
                "health": "healthy",
                "last_ack_seq": job.get("ack", 0),
            },
        )
        if not status["authorized"] or status["remaining_seconds"] <= 0:
            raise RuntimeError("Execution authority lost")
        job["local_lease"] = time.monotonic() + status["remaining_seconds"]

    async def event(self, job, kind, payload):
        # A lost ACK is recovered by resending the exact persisted event.
        pending = job.get("pending_event")
        if not pending:
            pending = {
                "runner_seq": job.get("ack", 0) + 1,
                "event_id": uid(),
                "type": kind,
                "payload": payload,
            }
            job["pending_event"] = pending
            self.wal.save(job["journal_key"], job)
        a = job["attempt"]
        answer = await self.client.post(
            "/runner/attempts/" + a["id"] + "/events",
            {"epoch": a["epoch"], "events": [pending]},
            pending["event_id"],
        )
        if answer["contiguous_ack"] < pending["runner_seq"]:
            raise RuntimeError("Event sequence gap")
        job["ack"] = answer["contiguous_ack"]
        job.pop("pending_event", None)
        self.wal.save(job["journal_key"], job)

    async def tool(self, job, backend, kind, source, timeout):
        await self.renew(job)
        args = {"kind": kind, "source": source, "timeout_seconds": timeout}
        intent = {
            "epoch": job["attempt"]["epoch"],
            "step_id": uid(),
            "operation_id": uid(),
            "tool": kind,
            "args_hash": checksum(args),
            "policy_digest": job["policy"]["digest"],
        }
        job["step_intent"] = intent
        self.wal.save(job["journal_key"], job)
        path = "/runner/attempts/" + job["attempt"]["id"]
        auth = await self.client.post(
            path + "/authorize-step", intent, intent["operation_id"]
        )
        # Trusted host process consumes the capability; guest only receives source.
        await self.client.post(
            "/runner/verify-capability",
            {
                "capability": auth["capability"],
                "tool": kind,
                "args_hash": checksum(args),
            },
        )
        await self.event(
            job, "step_started", {"step_id": intent["step_id"], "tool": kind}
        )
        answer = await asyncio.wait_for(
            backend.execute(args),
            timeout=min(timeout, max(0.001, job["local_lease"] - time.monotonic())),
        )
        await self.event(
            job,
            "step_finished",
            {"step_id": intent["step_id"], "exit_code": answer.get("exit_code")},
        )
        job.pop("step_intent", None)
        self.wal.save(job["journal_key"], job)
        return answer

    async def body(self, job, backend):
        task = job["task"]
        agent = task["agent"]
        workspace = None
        summary = None
        if task["workspace_ref"]:
            if self.workspaces is None or self.backend == "demo":
                raise RuntimeError("Workspace provider required")
            prepared = await asyncio.to_thread(
                self.workspaces.prepare, task, job["attempt"]["id"]
            )
            workspace = prepared["path"]
            summary = prepared["summary"]
            job["workspace_prepared"] = True
            self.wal.save(job["journal_key"], job)
            if summary is not None:
                await self.client.post(
                    "/runner/attempts/" + job["attempt"]["id"] + "/prepared",
                    {"epoch": job["attempt"]["epoch"], "summary": summary},
                )
        await self.renew(job)
        if self.backend != "demo" and self.verifier is None:
            raise RuntimeError("Independent host verifier required")
        if workspace:
            await backend.create(
                task["id"] + ":" + job["attempt"]["id"],
                workspace=workspace,
                deadline=task["deadline"],
            )
        else:
            await backend.create(task["id"] + ":" + job["attempt"]["id"])
        job["sandbox_ref"] = backend.id
        self.wal.save(job["journal_key"], job)
        await self.event(job, "sandbox_created", {"sandbox_ref": backend.id})
        if self.backend != "demo":
            proof = await self.verifier("isolation", job)
            await self.client.post(
                "/runner/attempts/" + job["attempt"]["id"] + "/verify-isolation",
                {"epoch": job["attempt"]["epoch"], "proof": proof},
            )
        if agent["mode"] == "program":
            result = await self.tool(
                job,
                backend,
                agent["kind"],
                agent["source"],
                min(agent["tool_timeout"], task["policy"]["limits"]["tool_timeout"]),
            )
            if task["data_class"] == "private":
                self.workspaces.save_private_result(job["attempt"]["id"], result)
                return (
                    "succeeded"
                    if result.get("exit_code") == 0
                    and not result.get("execution_error")
                    else "failed"
                ), "PRIVATE_RESULT_RETAINED_LOCALLY"
            return (
                "succeeded"
                if result.get("exit_code") == 0 and not result.get("execution_error")
                else "failed"
            ), canonical(result)
        if self.backend == "demo":
            return "succeeded", "SIMULATED: no code or model executed."
        messages = [
            {
                "role": "user",
                "content": canonical(summary if summary is not None else task["input"]),
            }
        ]
        for _ in range(agent["max_steps"]):
            await self.renew(job)
            request = {
                "epoch": job["attempt"]["epoch"],
                "step_id": uid(),
                "request_id": uid(),
                "messages": messages,
                "budget_ref": task["id"],
            }
            job["model_intent"] = request["request_id"]
            self.wal.save(job["journal_key"], job)
            answer = await self.client.post(
                "/runner/attempts/" + job["attempt"]["id"] + "/model",
                request,
                request["request_id"],
            )
            action = Action.model_validate_json(answer["content"])
            job.pop("model_intent", None)
            self.wal.save(job["journal_key"], job)
            if action.action == "final":
                return "succeeded", action.content
            if action.action not in agent["tools"]:
                raise RuntimeError("Tool forbidden")
            result = await self.tool(
                job,
                backend,
                action.action,
                action.content,
                min(agent["tool_timeout"], task["policy"]["limits"]["tool_timeout"]),
            )
            messages = [
                messages[0],
                {"role": "assistant", "content": answer["content"][:16384]},
                {"role": "user", "content": canonical(result)[:8000]},
            ]
        return "failed", "step_limit"

    async def execute(self, job):
        backend = self.factory()
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                try:
                    await self.renew(job)
                except Exception:
                    stop.set()
                    return
                try:
                    await asyncio.wait_for(stop.wait(), 5)
                except TimeoutError:
                    pass

        pulse = asyncio.create_task(heartbeat())
        work = asyncio.create_task(self.body(job, backend))
        cancelled = asyncio.create_task(stop.wait())
        outcome, result = "needs_review", "execution_outcome_unknown"
        proof = ""
        unknown = True
        try:
            done, _ = await asyncio.wait(
                {work, cancelled},
                timeout=max(0, job["task"]["deadline"] - time.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work in done:
                outcome, result = work.result()
                unknown = False
        except Exception as exc:
            result = "execution_error:" + type(exc).__name__
        finally:
            stop.set()
            for task in (pulse, work, cancelled):
                task.cancel()
            await asyncio.gather(pulse, work, cancelled, return_exceptions=True)
            try:
                if backend.id:
                    await asyncio.wait_for(backend.destroy(), 25)
                    proof = (
                        "SIMULATED"
                        if self.backend == "demo"
                        else await self.verifier("cleanup", job)
                    )
            except Exception:
                proof = ""
        if job["task"]["data_class"] == "private":
            result = "PRIVATE_RESULT_RETAINED_LOCALLY"
        if proof and job.get("workspace_prepared") and self.workspaces:
            self.workspaces.remove(job["attempt"]["id"])
        return {
            "epoch": job["attempt"]["epoch"],
            "outcome": outcome,
            "result": result[:32768],
            "sandbox_ref": job.get("sandbox_ref", ""),
            "stop_evidence_ref": proof,
            "cleanup_evidence_ref": proof,
            "unknown_effects": unknown,
        }

    async def tick(self, slot=0):
        journal_key = "slot:" + str(slot)
        job = self.wal.load(journal_key)
        if not job:
            job = {"journal_key": journal_key, "claim_id": uid(), "started": False}
            self.wal.save(journal_key, job)
        if "attempt" not in job:
            claimed = await self.client.post(
                "/runner/claim",
                {
                    "claim_id": job["claim_id"],
                    "protocol": "2.0",
                    "advertised_slots": self.slots,
                },
                job["claim_id"],
            )
            if not claimed:
                self.wal.remove(journal_key)
                return False
            job.update(claimed)
            self.wal.save(journal_key, job)
        if not job.get("report"):
            if job["started"]:
                if job.get("pending_event"):
                    await self.event(
                        job,
                        job["pending_event"]["type"],
                        job["pending_event"]["payload"],
                    )
                job["report"] = {
                    "epoch": job["attempt"]["epoch"],
                    "outcome": "needs_review",
                    "result": ""
                    if job["task"]["data_class"] == "private"
                    else "Runner restarted after execution intent; no replay",
                    "sandbox_ref": job.get("sandbox_ref", ""),
                    "stop_evidence_ref": "",
                    "cleanup_evidence_ref": "",
                    "unknown_effects": True,
                }
            else:
                job["started"] = True
                self.wal.save(journal_key, job)
                job["report"] = await self.execute(job)
            job["report_key"] = uid()
            self.wal.save(journal_key, job)
        await self.client.post(
            "/runner/attempts/" + job["attempt"]["id"] + "/report",
            job["report"],
            job["report_key"],
        )
        self.wal.remove(journal_key)
        return True


async def main():
    wal = WAL(os.environ.get("AGENT_RUNNER_STATE", ".local/runner-v2"))
    client = Client(
        os.environ["AGENT_CONTROL_URL"], os.environ.get("AGENT_NODE_TOKEN", "")
    )
    verifier = None
    if os.environ.get("AGENT_VERIFIER_URL"):
        endpoint = os.environ["AGENT_VERIFIER_URL"]
        if not endpoint.startswith("https://"):
            raise ValueError("Independent verifier requires TLS")

        async def verifier(kind, job):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
                response = await http.post(
                    endpoint.rstrip("/") + "/" + kind,
                    headers={
                        "Authorization": "Bearer " + os.environ["AGENT_VERIFIER_TOKEN"]
                    },
                    json={
                        "attempt": job["attempt"]["id"],
                        "task": job["task"]["id"],
                        "epoch": job["attempt"]["epoch"],
                        "sandbox": job["sandbox_ref"],
                        "workspace_ref": job["task"]["workspace_ref"],
                        "data_class": job["task"]["data_class"],
                    },
                )
                response.raise_for_status()
                return response.json()["proof"]

    workspaces = None
    if os.environ.get("AGENT_WORKSPACES_JSON"):
        from .workspaces import Workspaces

        workspaces = Workspaces(
            json.loads(os.environ["AGENT_WORKSPACES_JSON"]),
            os.environ["AGENT_SNAPSHOT_ROOT"],
        )
    runner = Runner(
        client,
        wal,
        backend=os.environ.get("AGENT_BACKEND", "demo"),
        slots=int(os.environ.get("AGENT_RUNNER_SLOTS", "1")),
        verifier=verifier,
        workspaces=workspaces,
    )
    try:
        await runner.credentials()

        async def loop(slot):
            while True:
                try:
                    await runner.tick(slot)
                except Exception as exc:
                    print("runner_retry:" + type(exc).__name__, flush=True)
                await asyncio.sleep(1)

        async def rotation():
            while True:
                await asyncio.sleep(30)
                try:
                    await runner.credentials()
                    await runner.delete_local_content()
                    wal.prune()
                except Exception as exc:
                    print("credential_retry:" + type(exc).__name__, flush=True)

        await asyncio.gather(*(loop(slot) for slot in range(runner.slots)), rotation())
    finally:
        await client.close()
        wal.close()


if __name__ == "__main__":
    asyncio.run(main())
