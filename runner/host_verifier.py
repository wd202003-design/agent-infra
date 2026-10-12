"""Independent Docker host observer. Deploy separately; never mount its socket/key into Runner.

Strict certification: read-only root, no host mounts, no network, non-root UID,
1 CPU/2 GiB/256 PIDs, drop ALL, no-new-privileges. Incompatible hosts fail closed.
"""

import json
import os
import secrets
import time
from urllib.parse import quote
from pathlib import Path
import hashlib
from typing import Literal
import httpx
from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import JSONResponse


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt: str = Field(pattern=r"^[a-f0-9-]{36}$")
    task: str = Field(pattern=r"^[a-f0-9-]{36}$")
    epoch: int = Field(ge=1)
    workspace_ref: str | None = None
    data_class: Literal["public", "private", "edge_summary"] = "public"
    egress: bool = False
    sandbox: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class Observer:
    def __init__(
        self,
        docker,
        management,
        sandbox_label,
        task_label,
        keys,
        snapshot_root=None,
        network_guard=None,
    ):
        self.docker, self.management = docker, management
        self.snapshot_root = Path(snapshot_root) if snapshot_root else None
        self.network_guard = network_guard
        self.sandbox_label, self.task_label, self.keys = sandbox_label, task_label, keys

    async def containers(self, ref):
        response = await self.docker.get(
            "/containers/json",
            params={
                "all": "true",
                "filters": json.dumps({"label": [self.sandbox_label + "=" + ref]}),
            },
        )
        response.raise_for_status()
        return response.json()

    async def inspect(self, kind, payload):
        rows = await self.containers(payload.sandbox)
        now = time.time()
        base = {
            "attempt": payload.attempt,
            "epoch": payload.epoch,
            "sandbox": payload.sandbox,
            "observed": now,
            "expires": now + 30,
        }
        if kind == "cleanup":
            response = await self.management.get(
                "/sandboxes/" + quote(payload.sandbox, safe="")
            )
            if response.status_code != 404 or rows:
                raise ValueError("absence_not_proven")
            volumes = await self.docker.get(
                "/volumes",
                params={
                    "filters": json.dumps(
                        {"label": [self.sandbox_label + "=" + payload.sandbox]}
                    )
                },
            )
            volumes.raise_for_status()
            if volumes.json().get("Volumes"):
                raise ValueError("volumes_remain")
            return self.keys.sign(
                "cleanup", base | {"management_absent": True, "host_absent": True}
            )
        if kind not in {"isolation", "network"} or len(rows) != 1:
            raise ValueError("unique_container_required")
        container_id = rows[0]["Id"]
        response = await self.docker.get(
            "/containers/" + quote(container_id, safe="") + "/json"
        )
        response.raise_for_status()
        detail = response.json()
        host = detail["HostConfig"]
        config = detail["Config"]
        labels = config.get("Labels") or {}
        if labels.get(self.task_label) != payload.task + ":" + payload.attempt:
            raise ValueError("task_binding_missing")
        user = config.get("User", "").split(":")[0]
        if not user.isdecimal() or int(user) == 0:
            raise ValueError("non_root_uid_required")
        mounts = detail.get("Mounts", [])
        data_proof = {}
        if payload.workspace_ref:
            if not self.snapshot_root or len(mounts) != 1:
                raise ValueError("workspace_mount_required")
            expected = self.snapshot_root / payload.attempt
            mount = mounts[0]
            if (
                mount.get("Type") != "bind"
                or mount.get("RW") is not False
                or mount.get("Destination") != "/workspace"
                or Path(mount.get("Source", "")) != expected
                or expected.resolve() != expected
                or mount.get("Propagation") not in {"rprivate", "private"}
            ):
                raise ValueError("workspace_mount_not_readonly_or_bound")
            data_proof = {
                "workspace_ref": payload.workspace_ref,
                "data_class": payload.data_class,
            }
            if payload.data_class == "edge_summary":
                from .workspaces import walk_files, canonical

                files = list(walk_files(expected))
                if (
                    len(files) != 1
                    or files[0][0] != "summary.json"
                    or len(files[0][1]) > 8192
                ):
                    raise ValueError("summary_only_mount_required")
                value = json.loads(files[0][1])
                from infra_control.v2.egress import validate_summary

                validate_summary(value)
                data_proof["summary_digest"] = hashlib.sha256(
                    canonical(value).encode()
                ).hexdigest()
        elif mounts or host.get("Binds"):
            raise ValueError("unexpected_host_mount")
        if payload.egress:
            if (
                payload.data_class != "public"
                or payload.workspace_ref
                or not self.network_guard
            ):
                raise ValueError("egress_not_configured")
            if kind == "network":
                await self.network_guard.install(detail)
            await self.network_guard.verify(detail)
            # Container replacement/PID changes during inspection invalidate proof.
            again = await self.docker.get(
                "/containers/" + quote(container_id, safe="") + "/json"
            )
            again.raise_for_status()
            if again.json() != detail:
                raise ValueError("container_changed_during_inspection")
        elif kind == "network":
            raise ValueError("egress_not_requested")
        if (
            not host.get("ReadonlyRootfs")
            or host.get("Privileged")
            or host.get("Devices")
            or host.get("DeviceRequests")
            or host.get("CapAdd")
            or "ALL" not in host.get("CapDrop", [])
            or (not payload.egress and host.get("NetworkMode") != "none")
            or host.get("PidMode") == "host"
            or host.get("IpcMode") == "host"
            or host.get("Memory", 0) <= 0
            or host["Memory"] > 2147483648
            or not 0 < host.get("PidsLimit", 0) <= 256
            or not 0 < host.get("NanoCpus", 0) <= 1000000000
            or not any(
                s in ("no-new-privileges", "no-new-privileges:true")
                for s in host.get("SecurityOpt", [])
            )
        ):
            raise ValueError("host_policy_not_enforced")
        return self.keys.sign(
            "isolation",
            base
            | {
                "task": payload.task,
                "isolated": True,
                "policy": "https-egress-v1" if payload.egress else "offline-v2",
            }
            | data_proof,
        )


def create_app():
    from infra_control.v2.security import Keys, unb64

    keys = Keys(unb64(os.environ["AGENT_VERIFIER_KEY"]))
    endpoint = os.environ["OPEN_SANDBOX_DOMAIN"]
    if not endpoint.startswith("https://") and not os.environ.get(
        "AGENT_INTERNAL_DEVELOPMENT"
    ):
        raise ValueError("Management API requires TLS")
    guard = None
    if os.environ.get("AGENT_EGRESS_PROXY_IP"):
        from .network_guard import NetworkGuard

        guard = NetworkGuard(
            os.environ["AGENT_EGRESS_PROXY_IP"],
            int(os.environ.get("AGENT_EGRESS_PROXY_PORT", "3128")),
            os.environ["AGENT_EGRESS_MANAGEMENT_CIDR"],
        )
    observer = Observer(
        httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                uds=os.environ.get("AGENT_DOCKER_SOCKET", "/var/run/docker.sock")
            ),
            base_url="http://docker",
            timeout=5,
        ),
        httpx.AsyncClient(
            base_url=endpoint,
            headers={"OPEN-SANDBOX-API-KEY": os.environ["OPEN_SANDBOX_API_KEY"]},
            timeout=5,
            follow_redirects=False,
        ),
        os.environ["AGENT_DOCKER_SANDBOX_LABEL"],
        os.environ["AGENT_DOCKER_TASK_LABEL"],
        keys,
        snapshot_root=os.environ.get("AGENT_SNAPSHOT_ROOT"),
        network_guard=guard,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/{kind}")
    async def inspect(
        kind: str, payload: Inspection, authorization: str = Header(default="")
    ):
        if not secrets.compare_digest(
            authorization, "Bearer " + os.environ["AGENT_VERIFIER_TOKEN"]
        ):
            return JSONResponse({"code": "unauthorized"}, 401)
        try:
            return {"proof": await observer.inspect(kind, payload)}
        except Exception:
            return JSONResponse({"code": "proof_unavailable"}, 409)

    return app
