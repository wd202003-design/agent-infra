"""Versioned response contracts; opaque user input/schema/payload remains JSON."""

from typing import Any, Generic, Literal, TypeVar, Annotated
from pydantic import Field
from .models import Contract
from . import v2 as C

Timestamp = Annotated[
    str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
]
T = TypeVar("T")


class Envelope(Contract, Generic[T]):
    data: T
    request_id: C.ID
    next_cursor: str | None = None


class Error(Contract):
    code: str
    message: str
    request_id: str
    retry_after_seconds: int | None = None
    details: dict[str, Any] | None = None


class Agent(C.AgentVersion):
    id: C.Name
    digest: C.SHA


class Policy(C.PolicyVersion):
    id: C.Name
    digest: C.SHA


class Task(C.Submit):
    id: C.ID
    owner: str
    agent: Agent
    policy: Policy
    state: Literal[
        "queued",
        "running",
        "waiting",
        "cancel_requested",
        "needs_review",
        "succeeded",
        "failed",
        "cancelled",
    ]
    state_version: int
    sequence: int
    epoch: int
    created: Timestamp
    created_at: Timestamp
    deadline: Timestamp
    dispatch_ready: bool
    coordinator: Literal["temporal"]
    active_attempt: C.ID | None
    cancel_requested_at: Timestamp | None
    waiting: dict[str, Any] | None
    result: dict[str, Any] | None
    deleted: bool
    generation: int
    parent_task_id: C.ID | None
    reserved_tokens: int
    actual_tokens: int
    reserved_cost: int
    actual_cost: int
    model_route: dict[str, Any] | None = None
    billing_violation: bool = False


class Tombstone(Contract):
    id: C.ID
    owner: str
    pool: C.Name
    state: Literal["succeeded", "failed", "cancelled"]
    state_version: int
    epoch: int
    sequence: int
    created: Timestamp
    created_at: Timestamp
    deadline: Timestamp
    generation: int
    tombstone: Literal[True]
    deleted: Literal[True]
    dispatch_ready: Literal[False]
    active_attempt: None


TaskResult = Task | Tombstone


class Attempt(Contract):
    id: C.ID
    task_id: C.ID
    node_id: C.ID
    slot: int
    active: bool
    epoch: int
    lease_until: Timestamp
    stop_status: Literal["pending", "confirmed", "unknown"]
    cleanup_status: Literal["pending", "confirmed", "unknown"]
    last_ack: int
    simulated: bool
    backend: Literal["demo", "opensandbox"]
    unknown_effects: bool
    sandbox_ref: str
    local_content: bool = False
    isolation_verified: bool = False
    summary_digest: C.SHA | None = None
    approved_summary: dict[str, C.Aggregate] | None = None


class Claim(Contract):
    task: Task
    attempt: Attempt
    policy: Policy


class Node(C.NodePolicy):
    id: C.ID
    state_version: int
    draining: bool
    revoked: bool
    created_at: Timestamp


class Ticket(Contract):
    ticket_id: C.ID
    ticket_secret: str
    expires: Timestamp
    expires_at: Timestamp


class Credential(Contract):
    node_id: C.ID
    node_token: str
    expires: Timestamp
    expires_at: Timestamp
    rotate_after: Timestamp


class Registration(Credential):
    policy: C.NodePolicy


class Event(Contract):
    event_id: C.ID
    task_id: C.ID
    task_seq: int
    epoch: int
    type: str
    occurred_at: Timestamp
    received_at: Timestamp
    payload: dict[str, Any]


class Renew(Contract):
    authorized: bool
    remaining_seconds: float
    stop_reason: str | None = None


class Capability(Contract):
    capability: str
    expires: Timestamp


class Authorization(Contract):
    authorized: bool
    remaining_seconds: float | None = None


class Acknowledgement(Contract):
    contiguous_ack: int
    gaps: list[int]


class Prepared(Contract):
    summary_digest: C.SHA


class Upload(Contract):
    upload_session: C.ID
    url: str
    expires_at: Timestamp
    headers: dict[str, str]


class Artifact(C.Manifest):
    id: C.ID
    task_id: C.ID
    attempt_id: C.ID
    epoch: int
    status: Literal["staging", "verifying", "committed", "deleting", "deleted"]
    data_class: Literal["public", "private", "edge_summary"]
    created: Timestamp
    retention_until: Timestamp


class Download(Contract):
    url: str
    expires_at: Timestamp


class Delete(Contract):
    task_id: C.ID
    deleted: Literal[True]
    physical_delete_deadline: Timestamp | None = None


class Model(Contract):
    content: str
    usage_tokens: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: int
    currency: Literal["USD", "CNY"]
    request_id: C.ID


class UsageRequest(Contract):
    id: C.ID
    task_id: C.ID
    attempt_id: C.ID
    step_id: C.ID
    hash: C.SHA
    quota_pool_id: str
    price_version: str
    currency: Literal["USD", "CNY"]
    reserved_tokens: int
    reserved_cost: int
    status: Literal["pending", "complete", "unknown", "unexecuted", "reconciled"]
    created: Timestamp
    input_price: int
    output_price: int
    version: int
    actual_tokens: int | None = None
    actual_cost: int | None = None
    input_bound: int | None = None
    output_bound: int | None = None
    hard: bool = False
    route_digest: C.SHA | None = None
    bound_violation: bool = False


class Ledger(Contract):
    id: C.ID
    request_id: C.ID
    task_id: C.ID
    kind: str
    tokens: int
    cost: int
    currency: Literal["USD", "CNY"]
    price_version: str
    evidence_ref: str | None


class Usage(Contract):
    requests: list[UsageRequest]
    ledger: list[Ledger]


class Audit(Contract):
    id: C.ID
    actor: str
    action: str
    target: str
    occurred_at: Timestamp


class LocalAck(Contract):
    acknowledged: Literal[True]


class PolicyRevoked(Contract):
    revoked: Literal[True]
    digest: C.SHA


class Metrics(Contract):
    tasks_by_state: dict[str, int]
    active_real_attempts: int
    active_simulated_attempts: int
    model_requests_by_state: dict[str, int]
    pending_workflow_starts: int


class Me(Contract):
    id: str
    role: Literal["admin", "user"]
    pools: list[str]
    workspace_refs: list[str]
    protocol: Literal["2.0"]
    model_configured: bool
    storage_configured: bool
    capabilities: dict[str, bool]


class Command(Contract):
    task_id: C.ID
    state: str
    state_version: int
    epoch: int
    deadline: Timestamp


class Inspection(Contract):
    id: C.ID
    state: str
    state_version: int
    epoch: int
    deadline: Timestamp
    dispatch_ready: bool
