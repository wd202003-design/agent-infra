import asyncio
import fcntl
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .backends import FACTORIES
from .store import Conflict, Store
from .worker import worker_loop


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['python', 'shell', 'browser']
    source: str = Field(min_length=1, max_length=16384)
    timeout_seconds: int = Field(default=30, ge=1, le=60)


def create_app(data_dir=None, token=None, provider=None, start_worker=True):
    data_dir = Path(data_dir or os.environ.get('AGENT_INFRA_DATA', '.local/devserver'))
    token = token or os.environ.get('AGENT_INFRA_TOKEN', '')
    provider = provider or os.environ.get('AGENT_INFRA_BACKEND', 'demo')
    if len(token) < 24:
        raise ValueError('AGENT_INFRA_TOKEN must contain at least 24 characters')
    if provider not in FACTORIES:
        raise ValueError('unknown_backend')
    factory = FACTORIES[provider]
    # Validate backend configuration before accepting any tasks.
    factory()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(data_dir / 'tasks.sqlite3')

    @asynccontextmanager
    async def lifespan(app):
        lock = (data_dir / 'service.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError('Only one service process may use this data directory')
        stop = asyncio.Event()
        worker = None
        try:
            store.recover()
            if start_worker:
                worker = asyncio.create_task(worker_loop(store, factory, stop, provider))
            app.state.worker = worker
            yield
        finally:
            stop.set()
            try:
                if worker:
                    await worker  # Drain current SDK call; no unsafe thread cancellation.
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    app = FastAPI(title='Agent Infra development execution loop', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def limits(request: Request, call_next):
        if request.method == 'POST':
            # Bound request body even when chunked; do not trust Content-Length.
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 100_000:
                    return JSONResponse({'detail': 'request_too_large'}, status_code=413)
            request._body = bytes(content)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        return response

    def auth(authorization: str = Header(default='')):
        if not hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()):
            raise HTTPException(401, 'unauthorized')

    def require_task(task_id):
        task = store.get(task_id)
        if task is None:
            raise HTTPException(404, 'task_not_found')
        return task

    def check_worker():
        worker = getattr(app.state, 'worker', None)
        if worker is not None and worker.done():
            raise HTTPException(503, 'worker_stopped')

    @app.get('/', response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / 'static/index.html').read_text()

    @app.get('/ui.js')
    def script():
        return Response((Path(__file__).parent / 'static/ui.js').read_text(), media_type='text/javascript')

    @app.get('/ui.css')
    def stylesheet():
        return Response((Path(__file__).parent / 'static/ui.css').read_text(), media_type='text/css')

    @app.get('/api/info', dependencies=[Depends(auth)])
    def info():
        check_worker()
        return {'backend': provider, 'simulated': provider == 'demo', 'mode': 'single-process-development',
                'llm_connected': False, 'max_running': 1}

    @app.post('/api/tasks', dependencies=[Depends(auth)])
    def submit(body: Submission, idempotency_key: str = Header(min_length=1, max_length=128)):
        check_worker()
        try:
            task, created = store.submit(idempotency_key, body.model_dump(), provider)
        except Conflict as exc:
            code = 429 if str(exc) == 'queue_full' else 409
            raise HTTPException(code, str(exc)) from exc
        return JSONResponse(task, status_code=201 if created else 200)

    @app.get('/api/tasks', dependencies=[Depends(auth)])
    def tasks():
        return store.list()

    @app.get('/api/tasks/{task_id}', dependencies=[Depends(auth)])
    def task(task_id: str):
        return require_task(task_id)

    @app.post('/api/tasks/{task_id}/cancel', dependencies=[Depends(auth)])
    def cancel(task_id: str):
        if not store.cancel(task_id):
            raise HTTPException(404, 'task_not_found')
        return require_task(task_id)

    @app.get('/api/tasks/{task_id}/events', dependencies=[Depends(auth)])
    def events(task_id: str):
        require_task(task_id)
        return store.events(task_id)

    @app.get('/api/tasks/{task_id}/result', dependencies=[Depends(auth)])
    def result(task_id: str):
        task = require_task(task_id)
        if task['result'] is None:
            raise HTTPException(409, 'result_not_available')
        return Response(json.dumps(task, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="result.json"'})

    return app
