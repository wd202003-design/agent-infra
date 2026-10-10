"""OpenSandbox execution probe; no LLM, arbitrary user code, or production data."""

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import signal
import time
import uuid
from datetime import timedelta
from pathlib import Path

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException
from opensandbox.models import NetworkPolicy
from opensandbox.models.execd import RunCommandOpts
from opensandbox.transport import RetryPolicy


BROWSER = r"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.set_content('<title>Agent Infra</title><button onclick="this.textContent=\'done\'">run</button>')
            await page.get_by_role('button', name='run').click()
            assert await page.get_by_role('button').inner_text() == 'done'
            assert await page.title() == 'Agent Infra'
            await page.screenshot(path='/home/sandbox/browser.png')
        finally:
            await browser.close()

asyncio.run(main())
"""


def python_command(source):
    return "python -c " + shlex.quote(source)


async def command(sandbox, text, seconds=30):
    result = await asyncio.wait_for(
        sandbox.commands.run(text, opts=RunCommandOpts(timeout=timedelta(seconds=seconds))),
        timeout=seconds + 15,
    )
    # Missing status is unknown, never success.
    if result.error or result.exit_code != 0:
        raise RuntimeError("command_failed_or_exit_status_unknown")
    return result


async def stopped(sandbox, execution_id, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = await sandbox.commands.get_command_status(execution_id)
        if status.running is False:
            return status
        await asyncio.sleep(0.2)
    raise RuntimeError("stop_not_confirmed")


async def destroy(sandbox):
    try:
        await sandbox.kill()
    except SandboxApiException as exc:
        if exc.status_code != 404:
            raise
    # An acknowledged DELETE is insufficient: require management API absence.
    for _ in range(20):
        try:
            await sandbox.get_info()
        except SandboxApiException as exc:
            if exc.status_code == 404:
                return
            raise
        await asyncio.sleep(0.25)
    raise RuntimeError("sandbox_deletion_not_confirmed")


async def exercise(sandbox, output, report):
    await command(sandbox, "printf 'shell-ok' > /home/sandbox/shell.txt")
    if await sandbox.files.read_bytes('/home/sandbox/shell.txt') != b'shell-ok':
        raise RuntimeError('shell_artifact_mismatch')
    report['checks'].append('shell')

    await command(sandbox, python_command(
        "from pathlib import Path; import json; "
        "Path('/home/sandbox/result.json').write_text(json.dumps({'sum': sum(range(10))}))"
    ))
    data = await sandbox.files.read_bytes('/home/sandbox/result.json')
    if json.loads(data) != {'sum': 45}:
        raise RuntimeError('python_artifact_mismatch')
    (output / 'result.json').write_bytes(data)
    report['checks'].append('python')

    await command(sandbox, python_command(BROWSER), seconds=45)
    png = await sandbox.files.read_bytes('/home/sandbox/browser.png')
    if not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) > 5_000_000:
        raise RuntimeError('invalid_screenshot')
    (output / 'browser.png').write_bytes(png)
    report['checks'].append('browser_local_page')
    report['artifacts'] = {
        'result.json': hashlib.sha256(data).hexdigest(),
        'browser.png': hashlib.sha256(png).hexdigest(),
    }

    # Background commands permit explicit status verification after interruption.
    for label, timeout in [('command_timeout', 2), ('command_interrupt', 60)]:
        execution = await sandbox.commands.run(
            python_command('import time; time.sleep(60)'),
            opts=RunCommandOpts(background=True, timeout=timedelta(seconds=timeout)),
        )
        if not execution.id:
            raise RuntimeError('missing_execution_id')
        if label == 'command_interrupt':
            await sandbox.commands.interrupt(execution.id)
        status = await asyncio.wait_for(stopped(sandbox, execution.id), timeout=15)
        if status.exit_code is None or status.exit_code == 0:
            raise RuntimeError('expected_interrupted_command')
        report['checks'].append(label)


async def run(factory, output, report, wall_seconds=180):
    sandbox = None
    try:
        async with asyncio.timeout(wall_seconds):
            sandbox = await factory()
            report['sandbox_id'] = sandbox.id
            # Persist ownership immediately, before executing a command.
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            await exercise(sandbox, output, report)
            report['status'] = 'passed'
    except asyncio.CancelledError:
        report['status'] = 'cancelled'
    except Exception as exc:
        # Do not write exception bodies: SDK errors may contain headers or payloads.
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
    finally:
        if sandbox is not None:
            try:
                await asyncio.wait_for(destroy(sandbox), timeout=30)
                report['cleanup'] = 'management_api_absent'
            except Exception as exc:
                report['cleanup'] = 'unknown'
                report['cleanup_error_type'] = type(exc).__name__
                report['status'] = 'needs_review'
            finally:
                try:
                    await asyncio.wait_for(sandbox.close(), timeout=5)
                except Exception:
                    report['client_close'] = 'failed'
        else:
            # Creation may have succeeded remotely even without a response.
            report['cleanup'] = 'creation_outcome_unknown'
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['status'] == 'passed' else 1


async def main(args):
    run_id = uuid.uuid4().hex
    output = Path(args.output) / run_id
    output.mkdir(parents=True, mode=0o700)
    report = {
        'run_id': run_id, 'status': 'running', 'checks': [],
        'sandbox_id': None, 'image': args.image, 'sdk_version': '1.1.1',
        'ttl_seconds': 300, 'cleanup': 'not_started',
    }
    config = ConnectionConfig(
        domain=args.domain, api_key=os.environ.get('OPEN_SANDBOX_API_KEY'),
        use_server_proxy=True, request_timeout=timedelta(seconds=15),
        retry_policy=RetryPolicy.disabled(), disable_metrics=True,
    )

    async def factory():
        return await Sandbox.create(
            args.image, connection_config=config,
            timeout=timedelta(seconds=300), ready_timeout=timedelta(seconds=60),
            resource={'cpu': '1', 'memory': '2Gi'},
            network_policy=NetworkPolicy(default_action='deny', egress=[]),
            metadata={'project': 'agent-infra-poc', 'run_id': run_id},
        )

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        code = await run(factory, output, report)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
    print(json.dumps({'status': report['status'], 'report': str(output / 'report.json')}))
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', default='http://127.0.0.1:8080')
    parser.add_argument('--image', required=True, help='Image built on the sandbox host')
    parser.add_argument('--output', default='.local/sandbox-poc')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
