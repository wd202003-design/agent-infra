"""Backend adapters. No task source is ever executed on the control-plane host."""
import asyncio
import os
import shlex
from datetime import timedelta


class Demo:
    """State-machine simulator: deliberately does not execute submitted source."""
    id = 'demo-no-real-sandbox'

    async def create(self, task_id):
        self.id = 'demo-' + task_id

    async def execute(self, payload):
        await asyncio.sleep(0.5)
        return {'exit_code': 0, 'stdout': 'SIMULATED: source was NOT executed.', 'stderr': '', 'simulated': True}

    async def destroy(self):
        pass


def command_text(payload):
    # Both Python and browser jobs contain Python; browser templates include Playwright.
    if payload['kind'] == 'shell':
        return payload['source']
    return 'python -c ' + shlex.quote(payload['source'])


class OpenSandboxBackend:
    def __init__(self):
        self.sandbox = None
        self.id = None
        if not os.environ.get('OPEN_SANDBOX_IMAGE') or not os.environ.get('OPEN_SANDBOX_API_KEY'):
            raise ValueError('OpenSandbox image and API key are required')

    async def create(self, task_id, *, workspace=None, deadline=None):
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig
        from opensandbox.models import NetworkPolicy
        from opensandbox.transport import RetryPolicy
        cfg = ConnectionConfig(
            domain=os.environ.get('OPEN_SANDBOX_DOMAIN', 'http://127.0.0.1:8080'),
            api_key=os.environ['OPEN_SANDBOX_API_KEY'], use_server_proxy=True,
            request_timeout=timedelta(seconds=15), retry_policy=RetryPolicy.disabled(),
            disable_metrics=True,
        )
        volumes=None
        if workspace:
            from opensandbox.models.sandboxes import Volume, Host
            volumes=[Volume(name="task-input",host=Host(path=workspace),mount_path="/workspace",read_only=True)]
        import time
        ttl=min(900,max(1,int(deadline-time.time()+120))) if deadline else 300
        self.sandbox = await Sandbox.create(
            os.environ['OPEN_SANDBOX_IMAGE'], connection_config=cfg,
            timeout=timedelta(seconds=ttl), volumes=volumes, ready_timeout=timedelta(seconds=60),
            metadata={'project': 'agent-infra', 'task_id': task_id},
            network_policy=NetworkPolicy(default_action='deny', egress=[]),
            resource={'cpu': '1', 'memory': '2Gi'},
        )
        self.id = self.sandbox.id

    async def execute(self, payload):
        from opensandbox.models.execd import RunCommandOpts, ExecutionHandlers
        chunks = {'stdout': '', 'stderr': ''}
        truncated = False
        def receiver(name):
            async def receive(message):
                nonlocal truncated
                truncated = truncated or len(chunks[name]) + len(message.text) > 16384
                chunks[name] = (chunks[name] + message.text)[:16384]
            return receive
        result = await self.sandbox.commands.run(
            command_text(payload),
            opts=RunCommandOpts(timeout=timedelta(seconds=payload['timeout_seconds'])),
            handlers=ExecutionHandlers(on_stdout=receiver('stdout'), on_stderr=receiver('stderr'), skip_accumulation=True),
        )
        return {**chunks, 'exit_code': result.exit_code, 'execution_error': bool(result.error),
                'output_truncated': truncated, 'simulated': False}

    async def destroy(self):
        from opensandbox.exceptions import SandboxApiException
        try:
            try:
                await self.sandbox.kill()
            except SandboxApiException as exc:
                if exc.status_code != 404:
                    raise
            for _ in range(10):
                try:
                    await self.sandbox.get_info()
                except SandboxApiException as exc:
                    if exc.status_code == 404:
                        return
                    raise
                await asyncio.sleep(.2)
            raise RuntimeError('deletion_not_confirmed')
        finally:
            await self.sandbox.close()


# Cube remains a PoC adapter until bounded output reception is proven.
FACTORIES = {'demo': Demo, 'opensandbox': OpenSandboxBackend}
