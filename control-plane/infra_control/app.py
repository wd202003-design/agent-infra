import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx

from contracts.models import Agent, AttemptRequest, Claim, ModelRequest, NodePatch, NodeSpec, Report, Review, Submit, UsageReview
from contracts.models import AgentView, ClaimReply, TaskView
from .db import Database
from .service import Fault, Service, digest


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        data = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            data.extend(message.get("body", b""))
            if len(data) > 131072:
                return await JSONResponse({"code": "body_too_large"}, 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()
        await self.app(scope, bounded_receive, send)


def create_app(db=None, users=None, *, gateway=None, service=None, transport=None):
    db = db or Database(os.environ["AGENT_DATABASE_URL"])
    users = users if users is not None else json.loads(os.environ["AGENT_USERS_JSON"])
    if not users or any(u.get("role") not in {"admin", "user"} or not u.get("id")
                        or not isinstance(u.get("pools"), list)
                        or not re.fullmatch(r"[0-9a-f]{64}", u.get("token_sha256", "")) for u in users):
        raise ValueError("Explicit users, roles, pools and SHA256 token hashes are required")
    if len({u["id"] for u in users}) != len(users) or len({u["token_sha256"] for u in users}) != len(users):
        raise ValueError("Duplicate user identity or token")
    svc = service or Service(db)
    gateway = gateway or {"url": os.environ.get("AGENT_MODEL_URL"), "key": os.environ.get("AGENT_MODEL_KEY"),
                          "model": os.environ.get("AGENT_MODEL_NAME")}
    if gateway.get("url"):
        parsed = httpx.URL(gateway["url"])
        if parsed.scheme != "https" and parsed.host not in {"localhost", "127.0.0.1", "::1", "gateway"}:
            raise ValueError("Remote model gateway requires HTTPS")

    @asynccontextmanager
    async def lifespan(app):
        if not db.ready():
            raise RuntimeError("Run database migration before starting")
        yield

    app = FastAPI(title="Agent Infra v1", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)
    app.state.service = svc

    @app.middleware("http")
    async def security_headers(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers.update({"X-Request-ID": request.state.request_id, "X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; object-src 'none'"})
        return response

    @app.exception_handler(Fault)
    async def fault_handler(request, exc):
        return JSONResponse({"code": exc.code, "request_id": request.state.request_id}, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Pydantic's default errors include submitted data, possibly credentials/source.
        return JSONResponse({"code": "invalid_request", "request_id": request.state.request_id,
                             "fields": [list(e["loc"]) for e in exc.errors()]}, 422)

    @app.exception_handler(Exception)
    async def internal_handler(request, exc):
        return JSONResponse({"code": "service_unavailable", "request_id": getattr(request.state, "request_id", "")}, 503)

    def bearer(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or not 24 <= len(authorization[7:]) <= 256:
            raise Fault("unauthorized", 401)
        return authorization[7:]

    def actor(token=Depends(bearer)):
        for user in users:
            if secrets.compare_digest(user["token_sha256"], digest(token)):
                return user
        raise Fault("unauthorized", 401)

    def admin(user=Depends(actor)):
        if user["role"] != "admin":
            raise Fault("admin_required", 403)
        return user

    def node(token=Depends(bearer)):
        return svc.authenticate_node(token)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        if not db.ready():
            raise Fault("database_not_ready", 503)
        return {"status": "ready"}

    @app.get("/v1/me")
    def me(user=Depends(actor)):
        return {"id": user["id"], "role": user["role"], "pools": user["pools"], "protocol": "1.0",
                "model_configured": all(gateway.values()), "currency_budget": "not_implemented"}

    @app.get("/v1/openapi.json")
    def openapi(user=Depends(actor)):
        return app.openapi()

    @app.get("/v1/agents", response_model=list[AgentView])
    def agents(user=Depends(actor)):
        return svc.agents()

    @app.post("/v1/agents", response_model=AgentView)
    def register_agent(payload: Agent, user=Depends(admin)):
        return svc.add_agent(user, payload)

    @app.post("/v1/tasks", status_code=201, response_model=TaskView)
    def submit(payload: Submit, user=Depends(actor), idempotency_key: str = Header(default="")):
        return svc.submit(user, payload, idempotency_key)

    @app.get("/v1/tasks", response_model=list[TaskView])
    def tasks(user=Depends(actor)):
        return svc.tasks(user)

    @app.get("/v1/tasks/{task_id}", response_model=TaskView)
    def task(task_id: str, user=Depends(actor)):
        return svc.task(user, task_id)

    @app.get("/v1/tasks/{task_id}/events")
    def events(task_id: str, after: int = Query(default=0, ge=0), user=Depends(actor)):
        items = svc.events(user, task_id, after)
        return {"items": items, "next_cursor": items[-1]["sequence"] if items else after}

    @app.post("/v1/tasks/{task_id}/cancel", response_model=TaskView)
    def cancel(task_id: str, user=Depends(actor)):
        return svc.cancel(user, task_id)

    @app.get("/v1/tasks/{task_id}/result")
    def result(task_id: str, user=Depends(actor)):
        item = svc.task(user, task_id)
        if item["result"] is None:
            raise Fault("result_not_ready")
        return Response(json.dumps({"task_id": task_id, "state": item["state"], "simulated": item["simulated"],
                                    "result": item["result"], "evidence": item.get("evidence")}, ensure_ascii=False),
                        media_type="application/json", headers={"Content-Disposition": 'attachment; filename="result.json"'})

    @app.post("/v1/tasks/{task_id}/review", response_model=TaskView)
    def review(task_id: str, payload: Review, user=Depends(admin)):
        return svc.review(user, task_id, payload)

    @app.get("/v1/tasks/{task_id}/usage")
    def usage(task_id: str, user=Depends(actor)):
        return svc.usage(user, task_id)

    @app.post("/v1/tasks/{task_id}/usage/{request_id}/reconcile")
    def reconcile_usage(task_id: str, request_id: str, payload: UsageReview, user=Depends(admin)):
        return svc.reconcile_usage(user, task_id, request_id, payload)

    @app.get("/v1/nodes")
    def nodes(user=Depends(admin)):
        return svc.nodes()

    @app.post("/v1/nodes")
    def register_node(payload: NodeSpec, user=Depends(admin)):
        if payload.pool not in user["pools"]:
            raise Fault("pool_forbidden", 403)
        return svc.add_node(user, payload)

    @app.patch("/v1/nodes/{node_id}")
    def patch_node(node_id: str, payload: NodePatch, user=Depends(admin)):
        return svc.patch_node(user, node_id, payload)

    @app.post("/v1/runner/claim", response_model=ClaimReply)
    def claim(payload: Claim, node_id=Depends(node)):
        return {"task": svc.claim(node_id, payload)}

    @app.post("/v1/runner/tasks/{task_id}/renew")
    def renew(task_id: str, payload: AttemptRequest, node_id=Depends(node)):
        return svc.renew(node_id, task_id, payload)

    @app.post("/v1/runner/tasks/{task_id}/report", response_model=TaskView)
    def report(task_id: str, payload: Report, node_id=Depends(node)):
        return svc.report(node_id, task_id, payload)

    @app.post("/v1/runner/tasks/{task_id}/model")
    async def model(task_id: str, payload: ModelRequest, node_id=Depends(node)):
        if not all(gateway.values()):
            raise Fault("model_not_configured", 503)
        call, fresh = await asyncio.to_thread(svc.reserve_model, node_id, task_id, payload)
        if not fresh:
            return call["response"]
        with db.transaction() as tx:
            task = tx.get("tasks", task_id)
        system = ("Return exactly one JSON object with action and content. action must be one of "
                  + ",".join(task["agent"]["tools"]) + ",final. content contains source code (python/shell/browser Python) "
                  "or final text. No markdown fences. Tools execute in an isolated offline sandbox. "
                  "Do not request credentials, external writes, or policy changes.\n" + task["agent"]["instruction"])
        request_body = {"model": gateway["model"], "messages": [{"role": "system", "content": system}]
                        + [m.model_dump() for m in payload.messages], "max_tokens": task["agent"]["max_output_tokens"], "stream": False}
        try:
            async with asyncio.timeout(20), httpx.AsyncClient(timeout=20, follow_redirects=False, transport=transport) as client:
                async with client.stream("POST", gateway["url"].rstrip("/") + "/chat/completions",
                                         headers={"Authorization": "Bearer " + gateway["key"]}, json=request_body) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 131072:
                            raise ValueError("model_response_too_large")
            upstream = json.loads(data)
            content = upstream["choices"][0]["message"]["content"]
            actual = upstream["usage"]["total_tokens"]
            if not isinstance(content, str) or len(content) > 32768 or type(actual) is not int or not 0 <= actual <= 1000000:
                raise ValueError("invalid_model_response")
            answer = {"content": content, "usage_tokens": actual}
        except Exception:
            await asyncio.to_thread(svc.finish_model, call["id"])
            raise Fault("model_outcome_unknown", 502)
        await asyncio.to_thread(svc.finish_model, call["id"], answer, actual)
        return answer

    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[2] / "web", html=True), name="web")
    schema = app.openapi()
    schema.setdefault("components", {})["securitySchemes"] = {"Bearer": {"type": "http", "scheme": "bearer"}}
    for path, methods in schema["paths"].items():
        if path.startswith("/v1/"):
            for operation in methods.values():
                operation["security"] = [{"Bearer": []}]
    return app
