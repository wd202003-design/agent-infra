"""Temporal durable coordination. History contains IDs/state only, no user content."""

import asyncio
import os
import uuid
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    import httpx


@activity.defn
async def coordinate(task_id: str) -> dict:
    url = os.environ["AGENT_CONTROL_URL"].rstrip("/") + "/internal/v2"
    if not url.startswith("https://") and not os.environ.get(
        "AGENT_INTERNAL_DEVELOPMENT"
    ):
        raise ValueError("Internal command endpoint requires TLS")
    headers = {"Authorization": "Bearer " + os.environ["AGENT_INTERNAL_TOKEN"]}
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        r = await client.get(url + "/tasks/" + task_id, headers=headers)
        r.raise_for_status()
        state = r.json()["data"]
        kind = (
            "make_ready"
            if state["state"] == "queued" and not state["dispatch_ready"]
            else "reconcile"
        )
        if state["state"] == "failed" and state["epoch"] < 2:
            kind = "schedule_safe_retry"
        if (
            state["state"] in {"succeeded", "cancelled"}
            or state["state"] == "failed"
            and kind != "schedule_safe_retry"
        ):
            return {"terminal": True}
        command = {
            "command_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    task_id + ":" + str(state["state_version"]) + ":" + kind,
                )
            ),
            "task_id": task_id,
            "expected_version": state["state_version"],
            "expected_epoch": state["epoch"],
            "type": kind,
            "payload_ref": "",
        }
        r = await client.post(url + "/task-commands", headers=headers, json=command)
        if r.status_code == 409:
            if r.json().get("code") == "retry_not_safe":
                return {"terminal": True}
            return {"terminal": False}
        r.raise_for_status()
        state = r.json()["data"]
        return {"terminal": state["state"] in {"succeeded", "cancelled"}}


@workflow.defn
class TaskWorkflow:
    @workflow.run
    async def run(self, task_id: str) -> None:
        for _ in range(100):
            try:
                state = await workflow.execute_activity(
                    coordinate,
                    task_id,
                    start_to_close_timeout=timedelta(seconds=20),
                    schedule_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
                if state["terminal"]:
                    return
            except Exception:
                # Activity failure doesn't release authority or fabricate success.
                pass
            await workflow.sleep(10)
        workflow.continue_as_new(task_id)


async def dispatch_once(db, client, task_queue):
    from temporalio.exceptions import WorkflowAlreadyStartedError

    with db.transaction(serialized=False) as tx:
        rows = tx.execute(
            "SELECT id FROM v2_outbox WHERE status='pending' ORDER BY created LIMIT 100"
        ).fetchall()
    for (task_id,) in rows:
        try:
            await client.start_workflow(
                TaskWorkflow.run,
                task_id,
                id="task/" + task_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=10),
            )
        except WorkflowAlreadyStartedError:
            pass
        # Timeouts/connection errors propagate; outbox remains pending for same workflow ID.
        with db.transaction(serialized=False) as tx:
            tx.execute("UPDATE v2_outbox SET status='started' WHERE id=?", (task_id,))
    return len(rows)


async def run():
    from temporalio.client import Client, TLSConfig
    from pathlib import Path
    from temporalio.worker import Worker
    from ..db import Database
    from .api import configured_platform
    from .artifacts import Artifacts

    db = Database(os.environ["AGENT_DATABASE_URL"])
    p = configured_platform(db)
    tls = False
    if os.environ.get("AGENT_TEMPORAL_TLS") == "1":
        cert = os.environ.get("AGENT_TEMPORAL_CLIENT_CERT")
        private = os.environ.get("AGENT_TEMPORAL_CLIENT_KEY")
        ca = os.environ.get("AGENT_TEMPORAL_CA")
        if bool(cert) != bool(private):
            raise ValueError(
                "Temporal client certificate and key must be configured together"
            )
        tls = TLSConfig(
            client_cert=Path(cert).read_bytes() if cert else None,
            client_private_key=Path(private).read_bytes() if private else None,
            server_root_ca_cert=Path(ca).read_bytes() if ca else None,
            verification_server_name=os.environ.get("AGENT_TEMPORAL_SERVER_NAME"),
        )
    elif not os.environ.get("AGENT_INTERNAL_DEVELOPMENT"):
        raise ValueError("Production Temporal connection requires TLS")
    client = await Client.connect(
        os.environ["AGENT_TEMPORAL_ADDRESS"],
        namespace=os.environ.get("AGENT_TEMPORAL_NAMESPACE", "default"),
        tls=tls,
        api_key=os.environ.get("AGENT_TEMPORAL_API_KEY"),
    )
    queue = os.environ.get("AGENT_TEMPORAL_QUEUE", "agent-infra-v2")
    async with Worker(
        client, task_queue=queue, workflows=[TaskWorkflow], activities=[coordinate]
    ):

        async def dispatch_loop():
            while True:
                try:
                    await dispatch_once(db, client, queue)
                except Exception as exc:
                    print("coordination_retry:" + type(exc).__name__, flush=True)
                await asyncio.sleep(1)

        async def maintenance_loop():
            # Object deletion continues even if Temporal RPCs are unavailable.
            from .maintenance import retention

            next_retention = 0.0
            while True:
                try:
                    await asyncio.to_thread(Artifacts(p).sweep)
                    if asyncio.get_running_loop().time() >= next_retention:
                        await asyncio.to_thread(retention, p)
                        next_retention = asyncio.get_running_loop().time() + 3600
                except Exception as exc:
                    print("maintenance_retry:" + type(exc).__name__, flush=True)
                await asyncio.sleep(5)

        await asyncio.gather(dispatch_loop(), maintenance_loop())


if __name__ == "__main__":
    asyncio.run(run())
