import asyncio
import json
import time
import uuid

from contracts.models import Action, Report
from .backends import FACTORIES


def identity(task):
    return {"attempt_id": task["attempt_id"], "epoch": task["epoch"]}


def report(task, outcome, evidence, *, result="", stopped=False, cleanup=False):
    return Report(**identity(task), outcome=outcome, stopped=stopped, cleanup_confirmed=cleanup,
                  simulated=task["simulated"], evidence=evidence, result=result[:32768]).model_dump()


class Runner:
    def __init__(self, client, journal, backend="demo", *, factory=None):
        if backend not in FACTORIES:
            raise ValueError("Unsupported formal backend; Cube requires output-bound validation")
        self.client, self.journal, self.backend = client, journal, backend
        self.factory = factory or FACTORIES[backend]

    async def renew(self, task):
        return await self.client.post(f"/v1/runner/tasks/{task['id']}/renew", identity(task))

    async def authorized(self, task):
        status = await self.renew(task)
        if not status["authorized"]:
            raise RuntimeError("execution_not_authorized")

    async def heartbeat(self, task, stop):
        while not stop.is_set():
            try:
                await self.authorized(task)
            except Exception:
                stop.set()
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def body(self, task, backend):
        await self.authorized(task)
        await backend.create(task["id"] + ":" + task["attempt_id"])
        agent = task["agent"]
        if agent["mode"] == "program":
            await self.authorized(task)
            result = await backend.execute({"kind": agent["kind"], "source": agent["source"],
                                            "timeout_seconds": agent["tool_timeout"]})
            if result.get("exit_code") != 0 or result.get("execution_error"):
                return "failed", json.dumps(result)
            return "succeeded", json.dumps(result)
        if task["simulated"]:
            return "succeeded", "SIMULATED: no model or submitted code was executed."
        messages = [{"role": "user", "content": task["prompt"] or "Execute the registered agent objective."}]
        outputs = []
        for _ in range(agent["max_steps"]):
            await self.authorized(task)
            answer = await self.client.post(f"/v1/runner/tasks/{task['id']}/model",
                                            {**identity(task), "request_id": str(uuid.uuid4()), "messages": messages})
            action = Action.model_validate_json(answer["content"])
            if action.action == "final":
                return "succeeded", json.dumps({"final": action.content, "steps": outputs})[:32768]
            if action.action not in agent["tools"]:
                raise RuntimeError("tool_not_allowed")
            await self.authorized(task)
            result = await backend.execute({"kind": action.action, "source": action.content,
                                            "timeout_seconds": agent["tool_timeout"]})
            output = json.dumps(result, ensure_ascii=False)[:8000]
            outputs.append({"kind": action.action, "output": output})
            # Bound the context; no unlimited transcripts or raw source persistence in events.
            messages = [messages[0], {"role": "assistant", "content": answer["content"][:16384]},
                        {"role": "user", "content": "Tool result: " + output}]
        return "failed", json.dumps({"reason": "step_limit", "steps": outputs})[:32768]

    async def execute(self, task):
        if task.get("backend") != self.backend:
            return report(task, "needs_review", "Runner backend differs from registered node")
        stop = asyncio.Event()
        backend = self.factory()
        heartbeat = asyncio.create_task(self.heartbeat(task, stop))
        job = asyncio.create_task(self.body(task, backend))
        stopped = asyncio.create_task(stop.wait())
        outcome, output = "needs_review", ""
        evidence = "execution_outcome_unknown"
        try:
            remaining = max(0, task["deadline"] - time.time())
            done, _ = await asyncio.wait({job, stopped}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                outcome, output = job.result()
                evidence = "execution_returned"
            else:
                outcome = "failed" if not done else "needs_review"
                evidence = "execution_interrupted_or_authorization_lost"
        except Exception as exc:
            evidence = "execution_error:" + type(exc).__name__
        finally:
            stop.set()
            job.cancel()
            stopped.cancel()
            heartbeat.cancel()
            await asyncio.gather(job, stopped, heartbeat, return_exceptions=True)
            cleanup = False
            try:
                if getattr(backend, "id", None):
                    await asyncio.wait_for(backend.destroy(), timeout=25)
                    cleanup = task["simulated"]
                    evidence += ";simulated_cleanup" if cleanup else ";management_deletion_observed_host_check_required"
            except Exception as exc:
                evidence += ";cleanup_unknown:" + type(exc).__name__
        # Real SDK management deletion alone is insufficient stop/host-cleanup evidence.
        return report(task, outcome if cleanup else "needs_review", evidence, result=output,
                      stopped=cleanup, cleanup=cleanup)

    async def tick(self, *, recovering=False):
        state = self.journal.load()
        if not state:
            state = {"claim_id": str(uuid.uuid4()), "task": None, "started": False, "report": None}
            self.journal.save(state)
        if not state["task"]:
            claimed = await self.client.post("/v1/runner/claim", {"protocol": "1.0", "claim_id": state["claim_id"]})
            if not claimed["task"]:
                self.journal.clear()
                return False
            state["task"] = claimed["task"]
            self.journal.save(state)
        task = state["task"]
        if not state["report"]:
            if state["started"]:
                state["report"] = report(task, "needs_review", "Runner restarted after execution intent; no tool replay")
            elif task["state"] != "running":
                # Replayed claim after an already settled task: never execute again.
                self.journal.clear()
                return False
            else:
                state["started"] = True
                self.journal.save(state)
                state["report"] = await self.execute(task)
            self.journal.save(state)
        await self.client.post(f"/v1/runner/tasks/{task['id']}/report", state["report"])
        self.journal.clear()
        return True
