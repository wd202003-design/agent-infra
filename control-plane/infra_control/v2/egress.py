"""Non-public tasks emit controlled metadata and approved numeric aggregates only."""

import math
import re
from ..service import Fault
from ..db import encode


def validate_summary(value):
    if (
        not isinstance(value, dict)
        or not 1 <= len(value) <= 16
        or len(encode(value).encode()) > 8192
    ):
        raise Fault("invalid_summary", 422)
    for name, item in value.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) or set(item) != {
            "count",
            "min",
            "max",
            "mean",
        }:
            raise Fault("invalid_summary", 422)
        if type(item["count"]) is not int or not 10 <= item["count"] <= 10000000:
            raise Fault("invalid_summary", 422)
        if (
            any(
                type(item[k]) not in (int, float) or not math.isfinite(item[k])
                for k in ("min", "max", "mean")
            )
            or not item["min"] <= item["mean"] <= item["max"]
        ):
            raise Fault("invalid_summary", 422)


def event_metadata(kind, payload):
    allowed = {
        "step_started": {"step_id", "tool"},
        "step_finished": {"step_id", "exit_code"},
        "sandbox_created": {"sandbox_ref"},
        "stop_observed": set(),
    }
    if kind not in allowed or set(payload) != allowed[kind]:
        raise Fault("data_egress_forbidden", 403)
    if "step_id" in payload and not re.fullmatch(
        r"[a-f0-9-]{36}", str(payload["step_id"])
    ):
        raise Fault("invalid_step_id", 422)
    if "tool" in payload and payload["tool"] not in {"python", "shell", "browser"}:
        raise Fault("invalid_tool", 422)
    if "sandbox_ref" in payload and not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", str(payload["sandbox_ref"])
    ):
        raise Fault("invalid_sandbox_ref", 422)
    if "exit_code" in payload and (
        type(payload["exit_code"]) is not int or not -255 <= payload["exit_code"] <= 255
    ):
        raise Fault("invalid_exit_code", 422)
