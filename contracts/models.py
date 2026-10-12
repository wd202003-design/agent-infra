from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL = "1.0"
TERMINAL = {"succeeded", "failed", "cancelled"}
Kind = Literal["python", "shell", "browser"]
Name = str
State = Literal["queued", "running", "waiting", "cancel_requested", "needs_review", "succeeded", "failed", "cancelled"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Agent(Contract):
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,40}$")
    description: str = Field(min_length=1, max_length=500)
    mode: Literal["program", "agent"] = "program"
    kind: Kind = "python"
    source: str = Field(default="", max_length=16384)
    instruction: str = Field(default="", max_length=4096)
    tools: list[Kind] = Field(default_factory=lambda: ["python"], min_length=1, max_length=3)
    max_steps: int = Field(default=4, ge=1, le=16)
    tool_timeout: int = Field(default=30, ge=1, le=60)
    max_output_tokens: int = Field(default=1024, ge=64, le=4096)

    @model_validator(mode="after")
    def valid_program(self):
        if self.mode == "program" and (not self.source or self.kind not in self.tools):
            raise ValueError("program requires source and an allowed kind")
        if self.mode == "agent" and not self.instruction:
            raise ValueError("agent requires instruction")
        return self


class Submit(Contract):
    agent_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=40)
    pool: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    prompt: str = Field(default="", max_length=4096)
    timeout_seconds: int = Field(default=180, ge=10, le=600)
    token_budget: int = Field(default=16000, ge=1024, le=100000)


class NodeSpec(Contract):
    name: str = Field(min_length=1, max_length=64)
    pool: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    location: Literal["server", "local", "edge"] = "server"
    backend: Literal["demo", "opensandbox"] = "demo"
    capabilities: list[Kind] = Field(default_factory=lambda: ["python", "shell", "browser"], min_length=1, max_length=3)


class NodePatch(Contract):
    draining: bool | None = None
    revoked: bool | None = None


class UsageReview(Contract):
    actual_tokens: int = Field(ge=0, le=1000000)
    evidence: str = Field(min_length=10, max_length=2000)


class Claim(Contract):
    protocol: Literal["1.0"] = PROTOCOL
    claim_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


class AttemptRequest(Contract):
    attempt_id: str = Field(min_length=1, max_length=64)
    epoch: int = Field(ge=1)


class Report(AttemptRequest):
    outcome: Literal["succeeded", "failed", "cancelled", "needs_review"]
    stopped: bool
    cleanup_confirmed: bool
    simulated: bool
    evidence: str = Field(min_length=1, max_length=2000)
    result: str = Field(default="", max_length=32768)


class Review(Contract):
    outcome: Literal["succeeded", "failed", "cancelled"]
    stopped: Literal[True]
    cleanup_confirmed: Literal[True]
    evidence: str = Field(min_length=10, max_length=2000)


class Message(Contract):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=16384)


class ModelRequest(AttemptRequest):
    request_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    messages: list[Message] = Field(min_length=1, max_length=32)


class Action(Contract):
    action: Literal["python", "shell", "browser", "final"]
    content: str = Field(min_length=1, max_length=16384)


class AgentView(Agent):
    id: str
    digest: str


class TaskView(Submit):
    id: str
    owner: str
    agent: AgentView
    state: State
    epoch: int
    attempt_id: str | None
    node_id: str | None
    sequence: int
    state_version: int
    created_at: float
    deadline: float
    lease_deadline: float | None
    cancel_requested_at: float | None
    result: str | None
    report_hash: str | None
    cleanup_confirmed: bool
    stopped: bool
    simulated: bool | None
    model_reserved: int
    model_actual: int
    evidence: str | None = None
    backend: Literal["demo", "opensandbox"] | None = None


class ClaimReply(Contract):
    task: TaskView | None
