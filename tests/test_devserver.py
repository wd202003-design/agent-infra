import asyncio
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from devserver.app import create_app
from devserver.backends import Demo, command_text
from devserver.store import Conflict, Store
from devserver.worker import execute_task, worker_loop


PAYLOAD = {'kind': 'python', 'source': 'print(42)', 'timeout_seconds': 30}
TOKEN = 'test-token-for-local-unit-tests-only'
HEADERS = {'Authorization': 'Bearer ' + TOKEN}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tasks.db')

    def test_concurrent_submission_has_one_task(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.store.submit('same-key', PAYLOAD, 'demo'), range(20)))
        self.assertEqual(len({t['id'] for t, _ in rows}), 1)
        self.assertEqual(sum(int(new) for _, new in rows), 1)
        with self.assertRaises(Conflict):
            self.store.submit('same-key', {**PAYLOAD, 'source': 'different'}, 'demo')

    def test_claim_is_exclusive(self):
        self.store.submit('key', PAYLOAD, 'demo')
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(sum(t is not None for t in tasks), 1)

    def test_cancel_queued_prevents_claim(self):
        t, _ = self.store.submit('key', PAYLOAD, 'demo')
        self.store.cancel(t['id'])
        self.assertEqual(self.store.get(t['id'])['state'], 'cancelled')
        self.assertIsNone(self.store.claim())

    def test_restart_preserves_queue_but_never_replays_running(self):
        self.store.submit('a', PAYLOAD, 'demo')
        running = self.store.claim()
        queued, _ = self.store.submit('b', PAYLOAD, 'demo')
        restarted = Store(Path(self.tmp.name) / 'tasks.db')
        restarted.recover()
        self.assertEqual(restarted.get(running['id'])['state'], 'needs_review')
        self.assertEqual(restarted.claim()['id'], queued['id'])
        with self.assertRaises(Conflict):
            restarted.finish(running['id'], running['epoch'], 'succeeded', {})

    def test_late_cancel_wins_after_cleanup(self):
        self.store.submit('a', PAYLOAD, 'demo')
        task = self.store.claim()
        self.store.cancel(task['id'])
        self.store.finish(task['id'], task['epoch'], 'succeeded', {'cleanup': 'confirmed'})
        self.assertEqual(self.store.get(task['id'])['state'], 'cancelled')

    def test_queue_bound_keeps_idempotent_retries_working(self):
        self.store.submit('a', PAYLOAD, 'demo', max_pending=1)
        self.assertFalse(self.store.submit('a', PAYLOAD, 'demo', max_pending=1)[1])
        with self.assertRaises(Conflict):
            self.store.submit('b', PAYLOAD, 'demo', max_pending=1)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.store.submit('a', PAYLOAD, 'demo')
        self.task = self.store.claim()

    def backend(self):
        b = Demo()
        b.create = AsyncMock()
        b.execute = AsyncMock(return_value={'exit_code': 0})
        b.destroy = AsyncMock()
        return b

    async def test_cleanup_failure_never_reports_success(self):
        b = self.backend()
        b.destroy.side_effect = ConnectionError('credential must not appear')
        await execute_task(self.store, self.task, lambda: b)
        t = self.store.get(self.task['id'])
        self.assertEqual(t['state'], 'needs_review')
        self.assertNotIn('credential must not appear', str(t))

    async def test_unknown_create_does_not_retry(self):
        b = self.backend()
        b.create.side_effect = TimeoutError()
        await execute_task(self.store, self.task, lambda: b)
        b.create.assert_awaited_once()
        b.execute.assert_not_awaited()
        self.assertEqual(self.store.get(self.task['id'])['result']['cleanup'], 'creation_outcome_unknown')

    async def test_cancel_before_create_does_not_create(self):
        b = self.backend()
        self.store.cancel(self.task['id'])
        await execute_task(self.store, self.task, lambda: b)
        b.create.assert_not_awaited()
        self.assertEqual(self.store.get(self.task['id'])['state'], 'cancelled')

    async def test_cancel_during_execution_waits_for_cleanup(self):
        b = self.backend()
        async def execute(_):
            self.store.cancel(self.task['id'])
            self.assertEqual(self.store.get(self.task['id'])['state'], 'cancel_requested')
            return {'exit_code': 0}
        b.execute.side_effect = execute
        await execute_task(self.store, self.task, lambda: b)
        b.destroy.assert_awaited_once()
        self.assertEqual(self.store.get(self.task['id'])['state'], 'cancelled')

    async def test_execute_error_still_destroys_without_replay(self):
        b = self.backend()
        b.execute.side_effect = TimeoutError()
        await execute_task(self.store, self.task, lambda: b)
        b.execute.assert_awaited_once()
        b.destroy.assert_awaited_once()
        self.assertEqual(self.store.get(self.task['id'])['state'], 'needs_review')

    async def test_changed_backend_does_not_execute_old_queue(self):
        self.store.finish(self.task['id'], self.task['epoch'], 'succeeded', {})
        pending, _ = self.store.submit('b', PAYLOAD, 'cubesandbox')
        stop = asyncio.Event()
        loop = asyncio.create_task(worker_loop(self.store, self.backend, stop, 'demo'))
        await asyncio.sleep(.05)
        stop.set()
        await loop
        self.assertEqual(self.store.get(pending['id'])['state'], 'needs_review')


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_http_task_loop_and_result(self):
        with TestClient(create_app(self.tmp.name, TOKEN, 'demo')) as client:
            self.assertEqual(client.get('/api/tasks').status_code, 401)
            self.assertEqual(client.get('/').status_code, 200)
            headers = {**HEADERS, 'Idempotency-Key': 'http-task'}
            r = client.post('/api/tasks', headers=headers, json=PAYLOAD)
            self.assertEqual(r.status_code, 201, r.text)
            task_id = r.json()['id']
            self.assertEqual(client.post('/api/tasks', headers=headers, json=PAYLOAD).status_code, 200)
            for _ in range(40):
                task = client.get('/api/tasks/' + task_id, headers=HEADERS).json()
                if task['state'] == 'succeeded':
                    break
                time.sleep(.05)
            self.assertEqual(task['state'], 'succeeded')
            self.assertTrue(task['result']['simulated'])
            self.assertIn('NOT executed', task['result']['stdout'])
            result = client.get('/api/tasks/' + task_id + '/result', headers=HEADERS)
            self.assertEqual(result.status_code, 200)
            self.assertIn('attachment', result.headers['content-disposition'])
            events = client.get('/api/tasks/' + task_id + '/events', headers=HEADERS).json()
            self.assertEqual(events[-1]['kind'], 'succeeded')

    def test_validation_conflict_missing_and_body_limit(self):
        with TestClient(create_app(self.tmp.name, TOKEN, 'demo', start_worker=False)) as c:
            headers = {**HEADERS, 'Idempotency-Key': 'key'}
            self.assertEqual(c.post('/api/tasks', headers=headers, json={**PAYLOAD, 'kind': 'host-shell'}).status_code, 422)
            self.assertEqual(c.post('/api/tasks', headers=headers, json={**PAYLOAD, 'timeout_seconds': 61}).status_code, 422)
            self.assertEqual(c.post('/api/tasks', headers=headers, json=PAYLOAD).status_code, 201)
            self.assertEqual(c.post('/api/tasks', headers=headers, json={**PAYLOAD, 'source': 'x'}).status_code, 409)
            self.assertEqual(c.post('/api/tasks', headers=headers, content='x'*100001).status_code, 413)
            self.assertEqual(c.get('/api/tasks/missing', headers=HEADERS).status_code, 404)
            self.assertEqual(c.post('/api/tasks/missing/cancel', headers=HEADERS).status_code, 404)

    def test_second_process_and_weak_token_rejected(self):
        with self.assertRaises(ValueError):
            create_app(self.tmp.name, 'short', 'demo')
        with TestClient(create_app(self.tmp.name, TOKEN, 'demo', start_worker=False)):
            with self.assertRaises(RuntimeError):
                with TestClient(create_app(self.tmp.name, TOKEN, 'demo', start_worker=False)):
                    pass


if __name__ == '__main__':
    unittest.main()
