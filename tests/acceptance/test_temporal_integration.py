"""Opt-in real Temporal SDK test-server execution and deterministic history replay."""

import os
import unittest
import uuid


@unittest.skipUnless(
    os.environ.get("TEST_TEMPORAL") == "1", "Real Temporal test server is opt-in"
)
class TemporalIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_continues_after_activity_retry_and_replays(self):
        from temporalio import activity
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker, Replayer
        from infra_control.v2.coordination import TaskWorkflow

        attempts = []

        @activity.defn(name="coordinate")
        async def controlled_activity(task_id: str) -> dict:
            attempts.append(task_id)
            if len(attempts) == 1:
                raise RuntimeError("simulated dependency interruption")
            return {"terminal": len(attempts) >= 3}

        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = "integration-" + uuid.uuid4().hex
            async with Worker(
                env.client,
                task_queue=queue,
                workflows=[TaskWorkflow],
                activities=[controlled_activity],
            ):
                handle = await env.client.start_workflow(
                    TaskWorkflow.run,
                    "task-reference-only",
                    id="task/" + uuid.uuid4().hex,
                    task_queue=queue,
                )
                await handle.result()
                history = await handle.fetch_history()
            self.assertEqual(attempts, ["task-reference-only"] * 3)
            await Replayer(workflows=[TaskWorkflow]).replay_workflow(history)

    async def test_outbox_to_real_http_runner_and_workflow_completion(self):
        import asyncio
        import socket
        import threading
        from unittest.mock import patch
        import uvicorn
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker
        from infra_control.v2.coordination import (
            TaskWorkflow,
            coordinate,
            dispatch_once,
        )
        from runner.v2 import Client, Runner
        from runner.wal import WAL
        from test_v2 import V2Tests

        fixture = V2Tests(methodName="test_strict_models_do_not_echo_input")
        fixture.setUp()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(fixture.app, log_level="error", lifespan="off")
        )
        thread = threading.Thread(
            target=lambda: server.run(sockets=[sock]), daemon=True
        )
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(server.started)
            url = "http://127.0.0.1:" + str(port)
            with patch.dict(
                os.environ,
                {
                    "AGENT_CONTROL_URL": url,
                    "AGENT_INTERNAL_DEVELOPMENT": "1",
                    "AGENT_INTERNAL_TOKEN": "internal" + "x" * 32,
                    "NO_PROXY": "127.0.0.1,localhost",
                },
            ):
                async with await WorkflowEnvironment.start_time_skipping() as env:
                    queue = "http-integration-" + uuid.uuid4().hex
                    async with Worker(
                        env.client,
                        task_queue=queue,
                        workflows=[TaskWorkflow],
                        activities=[coordinate],
                    ):
                        task = fixture.submit()
                        await dispatch_once(fixture.db, env.client, queue)
                        wal = WAL(fixture.tmp.name + "/http-runner")
                        client = Client(url, fixture.token)
                        try:
                            runner = Runner(client, wal)
                            completed = False
                            for _ in range(100):
                                if await runner.tick():
                                    completed = True
                                    break
                                await asyncio.sleep(0.1)
                            self.assertTrue(completed)
                        finally:
                            await client.close()
                            wal.close()
                        self.assertEqual(
                            fixture.p.task(fixture.alice, task["id"])["state"],
                            "succeeded",
                        )
                        await env.client.get_workflow_handle(
                            "task/" + task["id"]
                        ).result()
        finally:
            server.should_exit = True
            thread.join(5)
            sock.close()
            fixture.tearDown()
        self.assertFalse(thread.is_alive())
