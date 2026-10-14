"""image-agent/1.0. Separate from frozen v1/v2 contracts."""

from typing import Any, Literal
from pydantic import Field, model_validator
from .models import Contract
from .v2 import ID, Name, SHA


class Ref(Contract):
    id: Name
    version: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,40}$")


class Binding(Contract):
    version: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,40}$")
    purpose: Literal["agent", "image"]
    provider: Name
    api: Literal["openai-completions", "openai-images"]
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=128)
    secret: str = Field(min_length=16, max_length=4096, repr=False)
    allowed_pools: list[Name] = Field(min_length=1, max_length=32)
    quota_pool_id: Name
    max_inflight: int = Field(default=2, ge=1, le=32)
    currency: Literal["USD", "CNY"] = "USD"
    # Operator-approved fixed billed upper bound per image/per complete Pi session.
    reserve_micro: int = Field(ge=1, le=1000000000)
    input_micro_per_million: int = Field(default=0, ge=0, le=1000000000)
    output_micro_per_million: int = Field(default=0, ge=0, le=1000000000)
    cache_read_micro_per_million: int = Field(default=0, ge=0, le=1000000000)
    cache_write_micro_per_million: int = Field(default=0, ge=0, le=1000000000)
    price_version: str = Field(min_length=1, max_length=64)
    sizes: list[str] = Field(
        default_factory=lambda: ["1024x1024"], min_length=1, max_length=8
    )
    qualities: list[str] = Field(
        default_factory=lambda: ["standard"], min_length=1, max_length=8
    )

    @model_validator(mode="after")
    def validate_route(self):
        from urllib.parse import urlsplit

        u = urlsplit(self.base_url)
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("approved_https_route_required")
        if (self.purpose == "agent") != (self.api == "openai-completions"):
            raise ValueError("purpose_api_mismatch")
        return self


class Context(Contract):
    expected_version: int = Field(ge=1)
    brief: str = Field(min_length=1, max_length=12000)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    reference_artifact_ids: list[ID] = Field(default_factory=list, max_length=4)


class Policy(Contract):
    max_parallel_children: int = Field(default=2, ge=1, le=8)
    max_children: int = Field(default=8, ge=1, le=8)
    failure_policy: Literal["fail_fast", "continue_independent"] = (
        "continue_independent"
    )
    allow_partial: bool = False


class Submit(Contract):
    instruction: str = Field(min_length=1, max_length=12000)
    pool: Name
    agent_binding: Ref
    image_binding: Ref
    policy: Policy = Field(default_factory=Policy)
    currency: Literal["USD", "CNY"] = "USD"
    cost_limit_micro: int = Field(ge=1, le=1000000000)
    timeout_seconds: int = Field(default=600, ge=30, le=3600)


class Child(Contract):
    child_key: Name
    depends_on: list[Name] = Field(default_factory=list, max_length=8)
    instruction: str = Field(min_length=1, max_length=4000)
    context_version: int = Field(ge=1)
    budget_limit_micro: int = Field(ge=1, le=1000000000)
    kind: Literal["generate", "edit", "selection"] = "generate"
    count: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    quality: str = "standard"


class Plan(Contract):
    expected_version: int = Field(ge=1)
    children: list[Child] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def dag(self):
        graph = {c.child_key: c.depends_on for c in self.children}
        if len(graph) != len(self.children):
            raise ValueError("duplicate_child")
        ready = set()
        while len(ready) < len(graph):
            nxt = {k for k, v in graph.items() if set(v) <= ready} - ready
            if not nxt:
                raise ValueError("cyclic_or_missing_dependency")
            ready |= nxt
        return self


class Claim(Contract):
    claim_id: ID
    protocol: Literal["image-agent/1.0"] = "image-agent/1.0"
    available_slots: int = Field(default=1, ge=1, le=32)


class Fence(Contract):
    task_id: ID
    epoch: int = Field(ge=1)


class Checkpoint(Fence):
    expected_revision: int = Field(ge=0)
    # Structured recovery references only, never raw Pi transcript/credentials.
    completed_tool_calls: list[ID] = Field(default_factory=list, max_length=32)
    artifact_ids: list[ID] = Field(default_factory=list, max_length=16)
    phase: Literal["planning", "generating", "waiting", "finalizing"]


class Operation(Fence):
    operation_id: ID
    tool_call_id: str = Field(min_length=1, max_length=128)
    kind: Literal["agent", "generate", "edit"]
    prompt: str = Field(default="", max_length=16000)
    count: int = Field(default=1, ge=1, le=4)
    reference_artifact_ids: list[ID] = Field(default_factory=list, max_length=4)


class OperationReport(Fence):
    state: Literal["succeeded", "failed", "unknown", "accepted"]
    provider_job_id: str | None = Field(default=None, max_length=256)
    actual_cost_micro: int | None = Field(default=None, ge=0, le=1000000000000)
    provider_request_id: str | None = Field(default=None, max_length=256)


class Credential(Fence):
    operation_id: ID


class Complete(Fence):
    outcome: Literal["succeeded", "failed", "cancelled"]
    artifact_ids: list[ID] = Field(default_factory=list, max_length=16)
    selected_artifact_ids: list[ID] = Field(default_factory=list, max_length=16)
    summary: str = Field(default="", max_length=4000)


class Event(Contract):
    event_id: ID
    seq: int = Field(ge=1)
    type: Literal[
        "task.started",
        "agent.phase_changed",
        "image.operation.accepted",
        "artifact.committed",
    ]
    operation_id: ID | None = None


class Events(Fence):
    events: list[Event] = Field(min_length=1, max_length=32)


class Upload(Fence):
    operation_id: ID
    size_bytes: int = Field(ge=1, le=20971520)
    sha256: SHA
    media_type: Literal["image/png", "image/jpeg", "image/webp"]


class Commit(Fence):
    version_id: str = Field(min_length=1, max_length=1024)


class Review(Contract):
    actual_cost_micro: int = Field(ge=0, le=1000000000000)
    outcome: Literal["succeeded", "failed"]
    evidence: str = Field(min_length=10, max_length=2000)
    provider_stopped: Literal[True]


class Envelope(Contract):
    data: Any
    request_id: ID
    next_cursor: str | None = None


class InputUpload(Contract):
    size_bytes: int = Field(ge=1, le=20971520)
    sha256: SHA
    media_type: Literal["image/png", "image/jpeg", "image/webp"]


class InputCommit(Contract):
    version_id: str = Field(min_length=1, max_length=1024)


class CloseReview(Contract):
    evidence: str = Field(min_length=10, max_length=2000)
    provider_stopped: Literal[True]
