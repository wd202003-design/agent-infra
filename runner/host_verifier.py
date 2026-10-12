"""Independent Docker host observer. Deploy separately; never mount its socket/key into Runner.

Strict certification: read-only root, no host mounts, no network, non-root UID,
1 CPU/2 GiB/256 PIDs, drop ALL, no-new-privileges. Incompatible hosts fail closed.
"""

import json
import os
import secrets
import time
from urllib.parse import quote
import httpx
from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import JSONResponse


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt: str = Field(pattern=r"^[a-f0-9-]{36}$")
    task: str = Field(pattern=r"^[a-f0-9-]{36}$")
    epoch: int = Field(ge=1)
    sandbox: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class Observer:
    def __init__(self, docker, management, sandbox_label, task_label, keys):
        self.docker, self.management = docker, management
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
        if kind != "isolation" or len(rows) != 1:
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
        if (
            not host.get("ReadonlyRootfs")
            or host.get("Privileged")
            or host.get("Binds")
            or detail.get("Mounts")
            or host.get("Devices")
            or host.get("DeviceRequests")
            or host.get("CapAdd")
            or "ALL" not in host.get("CapDrop", [])
            or host.get("NetworkMode") != "none"
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
            base | {"task": payload.task, "isolated": True, "policy": "offline-v2"},
        )


def create_app():
    from infra_control.v2.security import Keys, unb64

    keys = Keys(unb64(os.environ["AGENT_VERIFIER_KEY"]))
    endpoint = os.environ["OPEN_SANDBOX_DOMAIN"]
    if not endpoint.startswith("https://") and not os.environ.get(
        "AGENT_INTERNAL_DEVELOPMENT"
    ):
        raise ValueError("Management API requires TLS")
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
