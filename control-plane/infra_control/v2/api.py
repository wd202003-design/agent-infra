import json
import os
import re
import secrets
import uuid
from fastapi import FastAPI, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contracts import v2 as C
from contracts import v2_views as V
from ..service import Fault
from .security import Keys, sha, unb64
from .service import Platform
from .artifacts import Artifacts, S3Storage
from .model import ModelGateway
from .store import object_put


def configured_platform(db):
    keys = Keys(unb64(os.environ["AGENT_V2_KEY"]))
    storage = None
    if os.environ.get("AGENT_S3_BUCKET"):
        import boto3

        client = boto3.client("s3", endpoint_url=os.environ.get("AGENT_S3_ENDPOINT"))
        storage = S3Storage(client, os.environ["AGENT_S3_BUCKET"])
        storage.ready()
    verifier = (
        Keys(unb64(os.environ["AGENT_VERIFIER_KEY"]))
        if os.environ.get("AGENT_VERIFIER_KEY")
        else None
    )
    return Platform(db, keys, storage=storage, verifier_keys=verifier)


def create_api(
    platform, users, *, gateway=None, transport=None, internal_token_hash=None
):
    p = platform
    artifacts = Artifacts(p)
    model = ModelGateway(p, gateway, transport=transport)
    app = FastAPI(
        responses={
            code: {"model": V.Error}
            for code in (401, 403, 404, 409, 410, 413, 422, 429, 503)
        },
        title="Agent Infra v2",
        version="2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    internal = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.platform = p
    app.state.model = model

    def bearer(authorization: str = Header(default="")):
        if (
            not authorization.startswith("Bearer ")
            or not 24 <= len(authorization[7:]) <= 256
        ):
            raise Fault("unauthorized", 401)
        return authorization[7:]

    def actor(request: Request, token=Depends(bearer)):
        for u in users:
            if secrets.compare_digest(u["token_sha256"], sha(token)):
                now = p.clock()
                issued = u.get("issued_at")
                expires = u.get("expires_at")
                if (
                    type(issued) not in (int, float)
                    or type(expires) not in (int, float)
                    or not issued <= now < expires
                    or expires - issued > 2592000
                ):
                    raise Fault("user_token_expired", 401)
                from .store import object_get

                with p.db.transaction(serialized=False) as tx:
                    fence = object_get(tx, "system", "auth_not_before")
                    if fence and issued <= fence["time"]:
                        raise Fault("credential_invalidated_by_restore", 401)
                request.state.principal = "user:" + u["id"]
                return u
        raise Fault("unauthorized", 401)

    def admin(u=Depends(actor)):
        if u["role"] != "admin":
            raise Fault("admin_required", 403)
        return u

    def node(request: Request, token=Depends(bearer)):
        node_id = p.authenticate(token)
        request.state.principal = "node:" + node_id
        return node_id

    async def key(request: Request, idempotency_key: str = Header(default="")):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise Fault("invalid_idempotency_key", 422)
        if request.url.path.endswith("/runner/register"):
            # Ticket validation/consumption owns this anonymous route's durable key.
            return idempotency_key
        from ..db import encode
        from .store import lock

        try:
            checksum = sha(encode(await request.json()))
        except Exception:
            raise Fault("invalid_request", 422) from None
        scope = request.state.principal + ":" + request.method + ":" + request.url.path
        with p.db.transaction(serialized=False) as tx:
            lock(tx, "http-key:" + scope + ":" + idempotency_key)
            row = tx.execute(
                "SELECT hash FROM v2_http_keys WHERE scope=? AND key=?",
                (scope, idempotency_key),
            ).fetchone()
            if row and row[0] != checksum:
                raise Fault("idempotency_conflict")
            if not row:
                tx.execute(
                    "INSERT INTO v2_http_keys(scope,key,hash,created) VALUES(?,?,?,?)",
                    (scope, idempotency_key, checksum, p.clock()),
                )
        return idempotency_key

    def service(token=Depends(bearer)):
        if not internal_token_hash or not secrets.compare_digest(
            sha(token), internal_token_hash
        ):
            raise Fault("service_identity_required", 401)

    def result(request, value, cursor=False):
        from contracts.wire import convert

        envelope = {"data": convert(value), "request_id": request.state.request_id}
        if cursor is not False:
            envelope["next_cursor"] = cursor
        return envelope

    for target in (app, internal):

        @target.middleware("http")
        async def request_id(request, call_next):
            request.state.request_id = str(uuid.uuid4())
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["Cache-Control"] = "no-store"
            return response

        @target.exception_handler(Fault)
        async def fault(request, exc):
            return JSONResponse(
                {
                    "code": exc.code,
                    "message": exc.code,
                    "request_id": request.state.request_id,
                },
                exc.status,
            )

        @target.exception_handler(RequestValidationError)
        async def invalid(request, exc):
            return JSONResponse(
                {
                    "code": "invalid_request",
                    "message": "invalid_request",
                    "request_id": request.state.request_id,
                    "details": {"fields": [list(e["loc"]) for e in exc.errors()]},
                },
                422,
            )

        @target.exception_handler(Exception)
        async def unavailable(request, exc):
            return JSONResponse(
                {
                    "code": "service_unavailable",
                    "message": "service_unavailable",
                    "request_id": getattr(request.state, "request_id", ""),
                },
                503,
            )

    @app.get("/me", response_model=V.Envelope[V.Me])
    def me(request: Request, u=Depends(actor)):
        return result(
            request,
            {
                "id": u["id"],
                "role": u["role"],
                "pools": u["pools"],
                "workspace_refs": u.get("workspace_refs", []),
                "protocol": "2.0",
                "model_configured": bool(gateway),
                "storage_configured": bool(p.storage),
                "capabilities": {
                    "private_fixed_program": True,
                    "edge_numeric_summary": True,
                    "hard_budget": bool(gateway and gateway.get("billing_bound")),
                },
            },
        )

    @app.get("/openapi.json")
    def schema(u=Depends(actor)):
        return app.openapi()

    @app.get(
        "/agents",
        response_model=V.Envelope[list[V.Agent]],
    )
    def agents(request: Request, u=Depends(actor)):
        return result(request, p.versions("agent"), None)

    @app.get(
        "/policies",
        response_model=V.Envelope[list[V.Policy]],
    )
    def policies(request: Request, u=Depends(actor)):
        return result(request, p.versions("policy"), None)

    @app.post(
        "/agents/{agent_id}/versions",
        response_model=V.Envelope[V.Agent],
    )
    def publish_agent(
        agent_id: C.Name,
        payload: C.AgentVersion,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, p.publish(u, "agent", agent_id, payload, k))

    @app.post(
        "/policies/{policy_id}/versions",
        response_model=V.Envelope[V.Policy],
    )
    def publish_policy(
        policy_id: C.Name,
        payload: C.PolicyVersion,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, p.publish(u, "policy", policy_id, payload, k))

    @app.post(
        "/policies/{policy_id}/versions/{version}/revoke",
        response_model=V.Envelope[V.PolicyRevoked],
    )
    def revoke_policy(
        policy_id: C.Name,
        version: C.Version,
        payload: C.Revoke,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        from .store import object_get

        with p.db.transaction(serialized=False) as tx:

            def apply():
                policy = object_get(tx, "policy", policy_id + ":" + version)
                if not policy:
                    raise Fault("not_found", 404)
                object_put(tx, "revoked_policy", policy["digest"], {"revoked": True})
                p.audit(tx, u["id"], "revoke_policy", policy["digest"])
                return {"revoked": True, "digest": policy["digest"]}

            return result(
                request,
                p.idem(
                    tx,
                    u["id"] + ":revoke_policy:" + policy_id + ":" + version,
                    k,
                    payload.model_dump(),
                    apply,
                ),
            )

    @app.post(
        "/tasks",
        status_code=201,
        response_model=V.Envelope[V.TaskResult],
    )
    def submit(payload: C.Submit, request: Request, u=Depends(actor), k=Depends(key)):
        return result(request, p.submit(u, payload, k))

    @app.get(
        "/tasks",
        response_model=V.Envelope[list[V.TaskResult]],
    )
    def tasks(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        u=Depends(actor),
    ):
        items, next_cursor = p.tasks(u, cursor, limit)
        return result(request, items, next_cursor)

    @app.get(
        "/tasks/{task_id}",
        response_model=V.Envelope[V.TaskResult],
    )
    def task(task_id: C.ID, request: Request, u=Depends(actor)):
        return result(request, p.task(u, task_id))

    @app.get(
        "/tasks/{task_id}/events",
        response_model=V.Envelope[list[V.Event]],
    )
    def events(
        task_id: C.ID,
        request: Request,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        u=Depends(actor),
    ):
        items, next_cursor = p.events(u, task_id, cursor, limit)
        return result(request, items, next_cursor)

    @app.post(
        "/tasks/{task_id}/cancel",
        response_model=V.Envelope[V.TaskResult],
    )
    def cancel(
        task_id: C.ID,
        payload: C.Cancel,
        request: Request,
        u=Depends(actor),
        k=Depends(key),
    ):
        return result(request, p.cancel(u, task_id, payload, k))

    @app.post(
        "/tasks/{task_id}/review",
        response_model=V.Envelope[V.TaskResult],
    )
    def review(
        task_id: C.ID,
        payload: C.Review,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, p.review(u, task_id, payload, k))

    @app.post(
        "/tasks/{task_id}/rerun",
        status_code=201,
        response_model=V.Envelope[V.TaskResult],
    )
    def rerun(
        task_id: C.ID,
        payload: C.Submit,
        request: Request,
        u=Depends(actor),
        k=Depends(key),
    ):
        old = p.task(u, task_id)
        if old["state"] not in {"succeeded", "failed", "cancelled"}:
            raise Fault("rerun_not_safe")
        return result(request, p.submit(u, payload, k, parent=task_id))

    @app.delete(
        "/tasks/{task_id}/content",
        response_model=V.Envelope[V.Delete],
    )
    def delete(
        task_id: C.ID,
        payload: C.CAS,
        request: Request,
        u=Depends(actor),
        k=Depends(key),
    ):
        return result(request, artifacts.delete_content(u, task_id, payload, k))

    @app.get(
        "/tasks/{task_id}/artifacts",
        response_model=V.Envelope[list[V.Artifact]],
    )
    def artifact_list(task_id: C.ID, request: Request, u=Depends(actor)):
        return result(request, artifacts.list(u, task_id), None)

    @app.post(
        "/artifacts/{artifact_id}/download",
        response_model=V.Envelope[V.Download],
    )
    def download(
        artifact_id: C.ID,
        payload: C.Download,
        request: Request,
        u=Depends(actor),
        k=Depends(key),
    ):
        return result(request, artifacts.download(u, artifact_id, payload, k))

    @app.get(
        "/tasks/{task_id}/usage",
        response_model=V.Envelope[V.Usage],
    )
    def usage(task_id: C.ID, request: Request, u=Depends(actor)):
        return result(request, model.usage(u, task_id))

    @app.post(
        "/tasks/{task_id}/usage/{request_id}/reconcile",
        response_model=V.Envelope[V.UsageRequest],
    )
    def usage_review(
        task_id: C.ID,
        request_id: C.ID,
        payload: C.ReconcileUsage,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, model.reconcile(u, task_id, request_id, payload, k))

    @app.get(
        "/metrics",
        response_model=V.Envelope[V.Metrics],
    )
    def metrics(request: Request, u=Depends(admin)):
        with p.db.transaction(serialized=False) as tx:
            states = {
                r[0]: r[1]
                for r in tx.execute(
                    "SELECT state,COUNT(*) FROM v2_tasks GROUP BY state"
                ).fetchall()
            }
            attempts = [
                json.loads(r[0])
                for r in tx.execute(
                    "SELECT body FROM v2_attempts WHERE active=1"
                ).fetchall()
            ]
            usage = {
                r[0]: r[1]
                for r in tx.execute(
                    "SELECT status,COUNT(*) FROM v2_usage GROUP BY status"
                ).fetchall()
            }
            outbox = tx.execute(
                "SELECT COUNT(*) FROM v2_outbox WHERE status='pending'"
            ).fetchone()[0]
        return result(
            request,
            {
                "tasks_by_state": states,
                "active_real_attempts": sum(not a["simulated"] for a in attempts),
                "active_simulated_attempts": sum(a["simulated"] for a in attempts),
                "model_requests_by_state": usage,
                "pending_workflow_starts": outbox,
            },
        )

    @app.get(
        "/nodes",
        response_model=V.Envelope[list[V.Node]],
    )
    def nodes(request: Request, u=Depends(admin)):
        return result(request, p.nodes(), None)

    @app.post(
        "/nodes/tickets",
        response_model=V.Envelope[V.Ticket],
    )
    def ticket(
        payload: C.NodePolicy, request: Request, u=Depends(admin), k=Depends(key)
    ):
        return result(request, p.ticket(u, payload, k))

    @app.patch(
        "/nodes/{node_id}",
        response_model=V.Envelope[V.Node],
    )
    def patch(
        node_id: C.ID,
        payload: C.NodePatch,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, p.patch_node(u, node_id, payload, k))

    @app.post(
        "/nodes/{node_id}/revoke",
        response_model=V.Envelope[V.Node],
    )
    def revoke(
        node_id: C.ID,
        payload: C.Revoke,
        request: Request,
        u=Depends(admin),
        k=Depends(key),
    ):
        return result(request, p.revoke(u, node_id, payload, k))

    @app.get(
        "/audit",
        response_model=V.Envelope[list[V.Audit]],
    )
    def audit(
        request: Request,
        after: str = "",
        limit: int = Query(50, ge=1, le=100),
        u=Depends(admin),
    ):
        with p.db.transaction(serialized=False) as tx:
            items = [
                json.loads(r[0])
                for r in tx.execute(
                    "SELECT body FROM v2_audit WHERE id>? ORDER BY id LIMIT ?",
                    (after, limit),
                ).fetchall()
            ]
        return result(request, items, items[-1]["id"] if len(items) == limit else None)

    @app.post(
        "/runner/register",
        response_model=V.Envelope[V.Registration],
    )
    def register(payload: C.Register, request: Request, k=Depends(key)):
        if k != payload.registration_id:
            raise Fault("registration_key_mismatch", 422)
        return result(request, p.register(payload, k))

    @app.post(
        "/runner/deletions",
        response_model=V.Envelope[list[C.LocalDeletion]],
    )
    def local_deletions(
        payload: C.Empty, request: Request, n=Depends(node), k=Depends(key)
    ):
        return result(request, p.pending_local_deletions(n))

    @app.post(
        "/runner/deletions/ack",
        response_model=V.Envelope[V.LocalAck],
    )
    def local_deletion_ack(
        payload: C.LocalDeletion, request: Request, n=Depends(node), k=Depends(key)
    ):
        return result(request, p.acknowledge_local_deletion(n, payload.attempt_id))

    @app.post(
        "/runner/rotate",
        response_model=V.Envelope[V.Credential],
    )
    def rotate(payload: C.Rotate, request: Request, n=Depends(node), k=Depends(key)):
        return result(request, p.rotate(n, payload, k))

    @app.post(
        "/runner/claim",
        response_model=V.Envelope[V.Claim | None],
    )
    def claim(payload: C.Claim, request: Request, n=Depends(node), k=Depends(key)):
        return result(request, p.claim(n, payload, k))

    @app.post(
        "/runner/attempts/{attempt_id}/renew",
        response_model=V.Envelope[V.Renew],
    )
    def renew(
        attempt_id: C.ID,
        payload: C.Renew,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, p.renew(n, attempt_id, payload, k))

    @app.post(
        "/runner/attempts/{attempt_id}/authorize-step",
        response_model=V.Envelope[V.Capability],
    )
    def authorize(
        attempt_id: C.ID,
        payload: C.Authorize,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, p.authorize(n, attempt_id, payload, k))

    @app.post(
        "/runner/attempts/{attempt_id}/prepared",
        response_model=V.Envelope[V.Prepared],
    )
    def prepared(
        attempt_id: C.ID,
        payload: C.Prepared,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, p.prepared(n, attempt_id, payload, k))

    @app.post(
        "/runner/attempts/{attempt_id}/verify-isolation",
        response_model=V.Envelope[V.Authorization],
    )
    def isolation(
        attempt_id: C.ID,
        payload: C.Isolation,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, p.verify_isolation(n, attempt_id, payload))

    @app.post(
        "/runner/verify-capability",
        response_model=V.Envelope[V.Authorization],
    )
    def verify(
        payload: C.VerifyCapability, request: Request, n=Depends(node), k=Depends(key)
    ):
        return result(request, p.verify_capability(n, payload))

    @app.post(
        "/runner/attempts/{attempt_id}/events",
        response_model=V.Envelope[V.Acknowledgement],
    )
    def runner_events(
        attempt_id: C.ID,
        payload: C.Events,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, p.runner_events(n, attempt_id, payload, k))

    @app.post(
        "/runner/attempts/{attempt_id}/artifacts",
        response_model=V.Envelope[V.Upload],
    )
    def upload(
        attempt_id: C.ID,
        payload: C.Upload,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, artifacts.create(n, attempt_id, payload, k))

    @app.post(
        "/runner/uploads/{upload_id}/complete",
        response_model=V.Envelope[V.Artifact],
    )
    def complete(
        upload_id: C.ID,
        payload: C.Complete,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, artifacts.complete(n, upload_id, payload, k))

    @app.post(
        "/runner/attempts/{attempt_id}/model",
        response_model=V.Envelope[V.Model],
    )
    async def invoke(
        attempt_id: C.ID,
        payload: C.Model,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, await model.invoke(n, attempt_id, payload))

    @app.post(
        "/runner/attempts/{attempt_id}/report",
        response_model=V.Envelope[V.TaskResult],
    )
    def report(
        attempt_id: C.ID,
        payload: C.Report,
        request: Request,
        n=Depends(node),
        k=Depends(key),
    ):
        return result(request, p.report(n, attempt_id, payload, k))

    @internal.post("/task-commands", response_model=V.Envelope[V.Command])
    def command(payload: C.Command, request: Request, s=Depends(service)):
        return result(request, p.command(payload))

    @internal.get("/tasks/{task_id}", response_model=V.Envelope[V.Inspection])
    def inspect(task_id: C.ID, request: Request, s=Depends(service)):
        task = p.task(None, task_id)
        return result(
            request,
            {
                k: task[k]
                for k in (
                    "id",
                    "state",
                    "state_version",
                    "epoch",
                    "deadline",
                    "dispatch_ready",
                )
            },
        )

    return app, internal
