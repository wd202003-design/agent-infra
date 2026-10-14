import os
import re
import secrets
import uuid
from fastapi import FastAPI, Request, Depends, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contracts import v3 as C
from ..service import Fault
from ..v2.security import Keys, sha, unb64
from .service import Images
from .artifacts import Artifacts, ImageStorage


def configured(db):
    storage = None
    if os.environ.get("AGENT_S3_BUCKET"):
        import boto3

        storage = ImageStorage(
            boto3.client("s3", endpoint_url=os.environ.get("AGENT_S3_ENDPOINT")),
            os.environ["AGENT_S3_BUCKET"],
        )
    return Images(db, Keys(unb64(os.environ["AGENT_V3_KEY"])), storage=storage)


def create_api(p, users, workers):
    for w in workers:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", w.get("id", ""))
            or not 1 <= w.get("slots", 0) <= 32
            or not w.get("pools")
            or not re.fullmatch(r"[a-f0-9]{64}", w.get("token_sha256", ""))
        ):
            raise ValueError("invalid_image_worker")
    if len({w["id"] for w in workers}) != len(workers) or len(
        {w["token_sha256"] for w in workers}
    ) != len(workers):
        raise ValueError("duplicate_image_worker")
    app = FastAPI(
        title="Image Agent v3",
        version="image-agent/1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    artifacts = Artifacts(p)

    @app.middleware("http")
    async def headers(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def result(req, value):
        return {"data": value, "request_id": req.state.request_id, "next_cursor": None}

    @app.exception_handler(Fault)
    async def fault(req, exc):
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.code,
                    "retryable": False,
                    "details": {},
                },
                "request_id": req.state.request_id,
            },
            exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(req, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Invalid request",
                    "retryable": False,
                    "details": {},
                },
                "request_id": req.state.request_id,
            },
            422,
        )

    @app.exception_handler(Exception)
    async def unavailable(req, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Service unavailable",
                    "retryable": False,
                    "details": {},
                },
                "request_id": req.state.request_id,
            },
            503,
        )

    def token(authorization: str = Header(default="")):
        if (
            not authorization.startswith("Bearer ")
            or not 24 <= len(authorization[7:]) <= 256
        ):
            raise Fault("UNAUTHORIZED", 401)
        return authorization[7:]

    def identity(items, value):
        for u in items:
            if secrets.compare_digest(u["token_sha256"], sha(value)):
                if u.get("revoked") or not u.get(
                    "issued_at", float("inf")
                ) <= p.clock() < u.get("expires_at", 0):
                    raise Fault("CREDENTIAL_EXPIRED", 401)
                return u
        raise Fault("UNAUTHORIZED", 401)

    def user(value=Depends(token)):
        return identity(users, value)

    def worker(value=Depends(token)):
        return identity(workers, value)

    def admin(u=Depends(user)):
        if u["role"] != "admin":
            raise Fault("ADMIN_REQUIRED", 403)
        return u

    def key(idempotency_key: str = Header(default="")):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise Fault("IDEMPOTENCY_KEY_REQUIRED", 422)
        return idempotency_key

    @app.get("/openapi.json")
    def schema(u=Depends(user)):
        return app.openapi()

    @app.get("/image-apps", response_model=C.Envelope)
    def apps(request: Request, u=Depends(user)):
        return result(
            request,
            [
                {
                    "id": "image-designer",
                    "version": "1",
                    "runtime": "pi-image/1.0",
                    "max_children": 8,
                    "max_depth": 2,
                    "budget_mode": "soft",
                    "provider_api": "openai-images-sync",
                }
            ],
        )

    @app.get("/model-bindings", response_model=C.Envelope)
    def bindings(request: Request, u=Depends(user)):
        return result(request, p.list_bindings(u))

    @app.post("/model-bindings/{id}", response_model=C.Envelope)
    def publish(
        id: C.Name, body: C.Binding, request: Request, u=Depends(admin), k=Depends(key)
    ):
        return result(request, p.publish(u, id, body, k))

    @app.post("/model-bindings/{id}/revoke", response_model=C.Envelope)
    def revoke(id: str, request: Request, u=Depends(admin), k=Depends(key)):
        return result(request, p.revoke_binding(id))

    @app.post("/image-tasks", response_model=C.Envelope, status_code=202)
    def submit(body: C.Submit, request: Request, u=Depends(user), k=Depends(key)):
        return result(request, p.submit(u, body, k))

    @app.get("/image-tasks", response_model=C.Envelope)
    def tasks(
        request: Request,
        after: str = "",
        limit: int = Query(default=50, ge=1, le=100),
        u=Depends(user),
    ):
        with p.tx() as tx:
            p.refresh(tx)
            rows = [
                r
                for r in p.rows(tx, "root")
                if (u["role"] == "admin" or r["owner"] == u["id"]) and r["id"] > after
            ]
            rows = sorted(rows, key=lambda r: r["id"])[:limit]
        out = result(request, rows)
        out["next_cursor"] = rows[-1]["id"] if len(rows) == limit else None
        return out

    @app.get("/image-tasks/{id}", response_model=C.Envelope)
    @app.get("/image-tasks/{id}/result", response_model=C.Envelope)
    def inspect(id: C.ID, request: Request, u=Depends(user)):
        return result(request, p.inspect(u, id))

    @app.get("/image-tasks/{id}/children", response_model=C.Envelope)
    def children(id: C.ID, request: Request, u=Depends(user)):
        return result(request, p.inspect(u, id)["children"])

    @app.post("/image-tasks/{id}/plans", response_model=C.Envelope)
    def plan(id: C.ID, body: C.Plan, request: Request, u=Depends(user), k=Depends(key)):
        return result(request, p.plan(u, id, body, k))

    @app.post("/image-tasks/{id}/contexts", response_model=C.Envelope)
    def context(
        id: C.ID, body: C.Context, request: Request, u=Depends(user), k=Depends(key)
    ):
        return result(request, p.context(u, id, body, k))

    @app.get("/contexts/{id}/versions/{version}", response_model=C.Envelope)
    def read_context(id: C.ID, version: int, request: Request, u=Depends(user)):
        with p.tx() as tx:
            p.owned(tx, id, u)
            value = p.get(tx, "context", id + ":" + str(version))
        return result(request, value)

    @app.post("/image-tasks/{id}/cancel", response_model=C.Envelope)
    def cancel(
        id: C.ID, body: C.CAS, request: Request, u=Depends(user), k=Depends(key)
    ):
        return result(request, p.cancel(u, id, body, k))

    @app.get("/image-tasks/{id}/events", response_model=C.Envelope)
    def events(
        id: C.ID, request: Request, after: int = Query(default=0, ge=0), u=Depends(user)
    ):
        with p.tx() as tx:
            p.owned(tx, id, u)
            items = [e for e in p.rows(tx, "event", id) if e["sequence"] > after]
            items = sorted(items, key=lambda e: e["sequence"])[:100]
        out = result(request, items)
        out["next_cursor"] = str(items[-1]["sequence"]) if len(items) == 100 else None
        return out

    @app.post("/runner/claims", response_model=C.Envelope)
    def claim(body: C.Claim, request: Request, w=Depends(worker), k=Depends(key)):
        if k != body.claim_id:
            raise Fault("CLAIM_KEY_MISMATCH", 422)
        return result(request, p.claim(w, body, k))

    @app.post("/runner/attempts/{id}/renew", response_model=C.Envelope)
    def renew(
        id: C.ID, body: C.Fence, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, p.renew(w, id, body, k))

    @app.post("/runner/attempts/{id}/abandon", response_model=C.Envelope)
    def abandon(
        id: C.ID, body: C.Fence, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, p.abandon(w, id, body))

    @app.post("/runner/attempts/{id}/checkpoints", response_model=C.Envelope)
    def checkpoint(
        id: C.ID,
        body: C.Checkpoint,
        request: Request,
        w=Depends(worker),
        k=Depends(key),
    ):
        return result(request, p.checkpoint(w, id, body, k))

    @app.post("/runner/attempts/{id}/execution-config", response_model=C.Envelope)
    def config(
        id: C.ID, body: C.Fence, request: Request, w=Depends(worker), k=Depends(key)
    ):
        with p.tx() as tx:
            r, c, a = p.active(tx, w, id, body)
            value = {
                purpose: p.public_binding(p.binding(tx, r[purpose + "_binding"]))
                for purpose in ("agent", "image")
            }
        return result(
            request, {"models": value, "runtime": "pi-image/1.0", "max_turns": 8}
        )

    @app.post("/runner/attempts/{id}/credentials:issue", response_model=C.Envelope)
    def credential(
        id: C.ID,
        body: C.Credential,
        request: Request,
        w=Depends(worker),
        k=Depends(key),
    ):
        return result(request, p.credentials(w, id, body))

    @app.post("/runner/attempts/{id}/image-operations", response_model=C.Envelope)
    def reserve(
        id: C.ID, body: C.Operation, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, p.reserve(w, id, body, k))

    @app.post("/runner/image-operations/{id}/dispatch", response_model=C.Envelope)
    def dispatch(
        id: C.ID, body: C.Fence, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, p.dispatch(w, id, body, k))

    @app.post("/runner/image-operations/{id}/report", response_model=C.Envelope)
    def report(
        id: C.ID,
        body: C.OperationReport,
        request: Request,
        w=Depends(worker),
        k=Depends(key),
    ):
        return result(request, p.report(w, id, body, k))

    @app.get("/runner/image-operations/{id}", response_model=C.Envelope)
    def operation(id: C.ID, request: Request, w=Depends(worker)):
        with p.tx() as tx:
            op = p.get(tx, "operation", id)
            a = p.get(tx, "attempt", op["attempt_id"])
            if a["worker_id"] != w["id"]:
                raise Fault("NOT_FOUND", 404)
        return result(request, op)

    @app.post("/runner/attempts/{id}/events", response_model=C.Envelope)
    def report_events(
        id: C.ID, body: C.Events, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, p.events(w, id, body, k))

    @app.post("/runner/attempts/{id}/complete", response_model=C.Envelope)
    def complete(
        id: C.ID, body: C.Complete, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, p.complete(w, id, body, k))

    @app.post("/image-operations/{id}/review", response_model=C.Envelope)
    def review(
        id: C.ID, body: C.Review, request: Request, u=Depends(admin), k=Depends(key)
    ):
        return result(request, p.review(u, id, body, k))

    @app.post("/image-children/{id}/close-review", response_model=C.Envelope)
    def close_review(
        id: C.ID,
        body: C.CloseReview,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, p.close_review(u, id, body, k))

    @app.post("/reference-uploads", response_model=C.Envelope)
    def input_upload(
        body: C.InputUpload, request: Request, u=Depends(user), k=Depends(key)
    ):
        return result(request, artifacts.create(u, body, k))

    @app.post("/reference-artifacts/{id}/commit", response_model=C.Envelope)
    def input_commit(
        id: C.ID, body: C.InputCommit, request: Request, u=Depends(user), k=Depends(key)
    ):
        return result(request, artifacts.commit(u, id, body))

    @app.post("/runner/attempts/{id}/uploads", response_model=C.Envelope)
    def upload(
        id: C.ID, body: C.Upload, request: Request, w=Depends(worker), k=Depends(key)
    ):
        return result(request, artifacts.create(w, body, k, id))

    @app.post("/runner/attempts/{id}/artifacts/{aid}/commit", response_model=C.Envelope)
    def commit(
        id: C.ID,
        aid: C.ID,
        body: C.Commit,
        request: Request,
        w=Depends(worker),
        k=Depends(key),
    ):
        return result(request, artifacts.commit(w, aid, body, id))

    @app.post("/artifacts/{id}/download-url", response_model=C.Envelope)
    def download(id: C.ID, request: Request, u=Depends(user), k=Depends(key)):
        return result(request, artifacts.download(u, id))

    @app.post(
        "/runner/attempts/{id}/artifacts/{aid}/download-url", response_model=C.Envelope
    )
    def runner_download(
        id: C.ID,
        aid: C.ID,
        body: C.Fence,
        request: Request,
        w=Depends(worker),
        k=Depends(key),
    ):
        return result(request, artifacts.download(w, aid, id, body))

    return app
