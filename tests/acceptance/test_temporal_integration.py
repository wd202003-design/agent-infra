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
