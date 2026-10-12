"""Explicit v2 wire projection: timestamps are RFC3339, arbitrary input is untouched."""

from copy import deepcopy
from datetime import datetime, timezone


def convert(value, decode=False):
    if isinstance(value, list):
        return [convert(v, decode) for v in value]
    if not isinstance(value, dict):
        return value
    data = deepcopy(value)
    fields = []
    if "state_version" in data and "owner" in data and "deadline" in data:
        fields = ["created", "deadline", "cancel_requested_at"]
    elif "lease_until" in data and "node_id" in data:
        fields = ["lease_until"]
    elif "expires" in data and (
        "node_token" in data or "ticket_secret" in data or "capability" in data
    ):
        fields = ["expires"]
    elif "media_type" in data and "sha256" in data:
        fields = ["created"]
    elif "state_version" in data and "deadline" in data:
        fields = ["deadline"]
    elif "quota_pool_id" in data and "reserved_tokens" in data:
        fields = ["created"]
    for key in fields:
        if data.get(key) is None:
            continue
        if decode and isinstance(data[key], str):
            data[key] = datetime.fromisoformat(
                data[key].replace("Z", "+00:00")
            ).timestamp()
        elif not decode and type(data[key]) in (int, float):
            data[key] = (
                datetime.fromtimestamp(data[key], timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
    # Only known envelope containers recurse; do not interpret user input/payload/source.
    for key in ("task", "attempt", "requests", "ledger"):
        if key in data:
            data[key] = convert(data[key], decode)
    return data
