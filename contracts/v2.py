"""D1 protocol 2.0. Independent from frozen v1 contracts."""

from typing import Annotated, Any, Literal
from pydantic import Field
from .models import Contract, Agent, Message

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ID = Annotated[str, Field(pattern=r"^[a-f0-9-]{36}$")]
SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Version = Annotated[str, Field(pattern=r"^[a-zA-Z0-9._-]{1,40}$")]


class Ref(Contract):
    id: Name
    version: Version


class Limits(Contract):
    cpu: Literal[1] = 1
    memory_mib: Literal[2048] = 2048
    pids: Literal[256] = 256
    tool_timeout: int = Field(default=30, ge=1, le=60)
    max_output_tokens: int = Field(default=1024, ge=64, le=4096)


class PolicyVersion(Contract):
    version: Version
    allowed_pools: list[Name] = Field(min_length=1, max_length=32)
    workspace_refs: list[Name] = Field(default_factory=list, max_length=32)
    tool_kinds: list[Literal["python", "shell", "browser"]] = Field(
        min_length=1, max_length=3
    )
    egress_rules: list[str] = Field(default_factory=list, max_length=0)
    data_classes: list[Literal["public", "private", "edge_summary"]] = Field(
        default_factory=lambda: ["public"], min_length=1, max_length=3
    )
    upload_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    limits: Limits = Field(default_factory=Limits)


class AgentVersion(Agent):
    runtime_version: Literal["json-loop-2.0"] = "json-loop-2.0"
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    policy_ref: Ref
    side_effect_class: Literal["pure", "read_only", "artifact_only"] = "pure"


class Budget(Contract):
    token_limit: int = Field(default=16000, ge=1024, le=100000)
    cost_limit: int = Field(default=1000000, ge=0, le=1000000000)
    currency: Literal["USD", "CNY"] = "USD"
    mode: Literal["soft", "hard"] = "soft"


class Submit(Contract):
    agent_ref: Ref
    policy_ref: Ref
    input: dict[str, Any] = Field(default_factory=dict)
    pool: Name
    workspace_ref: Name | None = None
    data_class: Literal["public", "private", "edge_summary"] = "public"
    budget: Budget = Field(default_factory=Budget)
    timeout_seconds: int = Field(default=180, ge=10, le=600)


class CAS(Contract):
    expected_version: int = Field(ge=1)


class Cancel(CAS):
    reason: str = Field(default="user_request", max_length=256)


class Review(CAS):
    outcome: Literal["succeeded", "failed", "cancelled"]
    evidence_refs: list[str] = Field(min_length=1, max_length=10)
    stop_confirmed: Literal[True]
    cleanup_confirmed: Literal[True]


class NodePolicy(Contract):
    workspace_refs: list[Name] = Field(default_factory=list, max_length=32)
    pool: Name
    owner: str = Field(min_length=1, max_length=64)
    location: Literal["server", "local", "edge"] = "server"
    backend: Literal["demo", "opensandbox"] = "demo"
    slots_limit: int = Field(default=1, ge=1, le=32)
    capabilities: list[Literal["python", "shell", "browser"]] = Field(
        default_factory=lambda: ["python"], min_length=1, max_length=3
    )


class Register(Contract):
    ticket_id: ID
    ticket_secret: str = Field(min_length=24, max_length=128)
    registration_id: ID
    protocol: Literal["2.0"]
    instance_public_info: str = Field(max_length=512)


class Rotate(Contract):
    rotation_id: ID
    protocol: Literal["2.0"]


class NodePatch(CAS):
    draining: bool | None = None
    slots_limit: int | None = Field(default=None, ge=1, le=32)


class Revoke(Contract):
    reason: str = Field(min_length=1, max_length=256)


class Claim(Contract):
    claim_id: ID
    protocol: Literal["2.0"]
    advertised_slots: int = Field(ge=1, le=32)


class Epoch(Contract):
    epoch: int = Field(ge=1)


class Renew(Epoch):
    task_id: ID
    health: Literal["healthy", "unhealthy"] = "healthy"
    last_ack_seq: int = Field(default=0, ge=0)


class Authorize(Epoch):
    step_id: ID
    operation_id: ID
    tool: Literal["python", "shell", "browser"]
    args_hash: SHA
    policy_digest: SHA


class VerifyCapability(Contract):
    capability: str = Field(max_length=4096)
    tool: Literal["python", "shell", "browser"]
    args_hash: SHA


class RunnerEvent(Contract):
    runner_seq: int = Field(ge=1)
    event_id: ID
    type: Literal[
        "log", "step_started", "step_finished", "sandbox_created", "stop_observed"
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class Events(Epoch):
    events: list[RunnerEvent] = Field(min_length=1, max_length=100)


class Manifest(Contract):
    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,120}$")
    media_type: Literal[
        "application/json", "text/plain", "image/png", "application/octet-stream"
    ]
    size: int = Field(ge=0, le=20971520)
    sha256: SHA


class Upload(Epoch):
    manifest: Manifest


class Complete(Epoch):
    sha256: SHA
    size: int = Field(ge=0, le=20971520)


class Download(Contract):
    disposition: Literal["attachment"] = "attachment"


class Model(Epoch):
    step_id: ID
    request_id: ID
    messages: list[Message] = Field(min_length=1, max_length=32)
    budget_ref: ID


class ReconcileUsage(CAS):
    actual_tokens: int = Field(ge=0, le=1000000)
    cost: int = Field(ge=0, le=1000000000)
    currency: Literal["USD", "CNY"]
    evidence_ref: str = Field(min_length=10, max_length=512)


class Report(Epoch):
    outcome: Literal["succeeded", "failed", "cancelled", "needs_review"]
    result_ref: ID | None = None
    result: str = Field(default="", max_length=32768)
    sandbox_ref: str = Field(default="", max_length=256)
    stop_evidence_ref: str = Field(default="", max_length=4096)
    cleanup_evidence_ref: str = Field(default="", max_length=4096)
    unknown_effects: bool = True


class Command(Contract):
    command_id: ID
    task_id: ID
    expected_version: int = Field(ge=1)
    expected_epoch: int | None = Field(default=None, ge=0)
    type: Literal[
        "make_ready", "schedule_safe_retry", "mark_waiting", "request_stop", "reconcile"
    ]
    payload_ref: str = Field(default="", max_length=256)


class Isolation(Epoch):
    proof: str = Field(max_length=4096)


class Aggregate(Contract):
    count: int = Field(ge=10, le=10000000)
    min: float
    max: float
    mean: float


class Prepared(Epoch):
    summary: dict[Name, Aggregate] = Field(min_length=1, max_length=16)


class Empty(Contract):
    pass


class LocalDeletion(Contract):
    attempt_id: ID
