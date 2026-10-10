import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opensandbox.exceptions import SandboxApiException
import probe


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_browser_payload_compiles(self):
        compile(probe.BROWSER, 'browser_payload', 'exec')

    def sandbox(self):
        return SimpleNamespace(
            id='test-sandbox', kill=AsyncMock(), close=AsyncMock(),
            get_info=AsyncMock(side_effect=SandboxApiException(status_code=404)),
        )

    async def execute(self, sandbox, effect=None):
        with tempfile.TemporaryDirectory() as root:
            report = {'status': 'running', 'checks': []}
            with patch.object(probe, 'exercise', AsyncMock(side_effect=effect)):
                code = await probe.run(AsyncMock(return_value=sandbox), Path(root), report)
            self.assertEqual(json.loads((Path(root) / 'report.json').read_text()), report)
            return code, report

    async def test_success_requires_remote_cleanup(self):
        sandbox = self.sandbox()
        code, report = await self.execute(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(report['cleanup'], 'management_api_absent')
        sandbox.kill.assert_awaited_once()
        sandbox.close.assert_awaited_once()

    async def test_command_failure_still_cleans_up_without_retry(self):
        sandbox = self.sandbox()
        code, report = await self.execute(sandbox, RuntimeError('sensitive payload'))
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('sensitive payload', json.dumps(report))
        sandbox.kill.assert_awaited_once()

    async def test_cancel_still_cleans_up(self):
        sandbox = self.sandbox()
        code, report = await self.execute(sandbox, asyncio.CancelledError())
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'cancelled')
        sandbox.kill.assert_awaited_once()

    async def test_cleanup_failure_cannot_be_success(self):
        sandbox = self.sandbox()
        sandbox.kill.side_effect = ConnectionError()
        code, report = await self.execute(sandbox)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['cleanup'], 'unknown')
        sandbox.close.assert_awaited_once()

    async def test_unauthorized_lookup_is_not_deletion(self):
        sandbox = self.sandbox()
        sandbox.get_info.side_effect = SandboxApiException(status_code=403)
        code, report = await self.execute(sandbox)
        self.assertEqual(code, 1)
        self.assertEqual(report['cleanup'], 'unknown')

    async def test_creation_timeout_records_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as root:
            factory = AsyncMock(side_effect=TimeoutError())
            report = {'run_id': 'for-reconciliation', 'status': 'running', 'checks': []}
            code = await probe.run(factory, Path(root), report)
            self.assertEqual(code, 1)
            self.assertEqual(report['cleanup'], 'creation_outcome_unknown')
            factory.assert_awaited_once()

    async def test_wall_timeout_cleans_up(self):
        sandbox = self.sandbox()
        async def hang(*_):
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            report = {'status': 'running', 'checks': []}
            with patch.object(probe, 'exercise', hang):
                code = await probe.run(AsyncMock(return_value=sandbox), Path(root), report, .01)
            self.assertEqual(code, 1)
            self.assertEqual(report['error_type'], 'TimeoutError')
            sandbox.kill.assert_awaited_once()

    async def test_unknown_exit_status_is_failure(self):
        sandbox = SimpleNamespace(commands=SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(error=None, exit_code=None))))
        with self.assertRaises(RuntimeError):
            await probe.command(sandbox, 'true')


if __name__ == '__main__':
    unittest.main()
