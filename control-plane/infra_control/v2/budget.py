"""Hard budget admission against an operator-reviewed, versioned billing contract.

The byte bound is valid ONLY for the approved byte-fallback tokenizer/template
contract. It is not assumed for arbitrary OpenAI-compatible endpoints.
"""

from typing import Literal
from pydantic import Field
from contracts.models import Contract
from ..db import encode
from ..service import Fault
from .security import sha


class BillingBound(Contract):
    algorithm: Literal["utf8-byte-upper-bound-v1"]
    model: str = Field(min_length=1)
    price_version: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=10, max_length=512)
    approved_by: str = Field(min_length=1, max_length=64)
    valid_until: float
    per_message_overhead: int = Field(ge=0, le=4096)
    request_overhead: int = Field(ge=0, le=4096)
    output_parameter: Literal["max_tokens", "max_completion_tokens"]
    output_limit_includes_all_billable_tokens: Literal[True]
    no_other_billable_items: Literal[True]


def route_snapshot(config, now, hard=False):
    if not config:
        raise Fault("model_not_configured", 503)
    fields = (
        "url",
        "model",
        "price_version",
        "quota_pool_id",
        "currency",
        "input_micro_per_million",
        "output_micro_per_million",
    )
    route = {k: config[k] for k in fields}
    proof = config.get("billing_bound")
    if hard:
        if not proof:
            raise Fault("hard_budget_requires_proven_bound", 422)
        try:
            proof = BillingBound.model_validate(proof).model_dump()
        except Exception:
            raise Fault("invalid_billing_bound", 422) from None
        if (
            proof["model"] != config["model"]
            or proof["price_version"] != config["price_version"]
            or proof["valid_until"] <= now
        ):
            raise Fault("billing_bound_expired_or_mismatched", 422)
        route["billing_bound"] = proof
    route["digest"] = sha(encode(route))
    return route


def request_body(task, payload, config):
    system = (
        'Return exactly one JSON object {"action":"python|shell|browser|final","content":"..."}. Allowed tools: '
        + ",".join(task["agent"]["tools"])
        + ".\n"
        + task["agent"]["instruction"]
    )
    field = (
        (task.get("model_route") or {})
        .get("billing_bound", {})
        .get("output_parameter", "max_tokens")
    )
    return {
        "model": config["model"],
        "messages": [{"role": "system", "content": system}]
        + [m.model_dump() for m in payload.messages],
        field: task["agent"]["max_output_tokens"],
        "stream": False,
    }


def reserve_bound(task, payload, config, now):
    hard = task["budget"]["mode"] == "hard"
    route = route_snapshot(config, now, hard)
    pinned = task.get("model_route")
    if pinned and pinned["digest"] != route["digest"]:
        raise Fault("model_route_changed", 409)
    request = request_body(task, payload, config)
    output = task["agent"]["max_output_tokens"]
    if hard:
        proof = route["billing_bound"]
        input_bound = (
            sum(
                len(m["content"].encode("utf-8")) + proof["per_message_overhead"]
                for m in request["messages"]
            )
            + proof["request_overhead"]
        )
        cost = (
            input_bound * route["input_micro_per_million"]
            + output * route["output_micro_per_million"]
            + 999999
        ) // 1000000
    else:
        input_bound = len(encode(request["messages"]).encode()) + 2048
        cost = (
            (input_bound + output)
            * max(route["input_micro_per_million"], route["output_micro_per_million"])
            + 999999
        ) // 1000000
    return input_bound, output, cost, route
