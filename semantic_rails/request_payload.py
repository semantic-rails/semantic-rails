"""Protocol-neutral request shaping shared by HTTP and MCP.

Transport adapters own identity resolution, input errors, and response defaults.
This module owns the common query envelope and policy-claim removal rules so a
new transport cannot accidentally preserve a nested caller claim.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def clean_request_id(value: Any) -> str:
    raw = str(value or "").strip()
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n")
    return cleaned[:128]


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def without_policy_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a request and remove every supported caller policy-context location."""

    out = dict(payload)
    out.pop("policy_context", None)
    raw_query = out.get("query")
    if isinstance(raw_query, Mapping):
        query = dict(raw_query)
        query.pop("policy_context", None)
        out["query"] = query
    return out


def build_query_payload(
    payload: Mapping[str, Any],
    *,
    object_payload: Callable[..., dict[str, Any]],
    policy_context: Mapping[str, Any],
    replace_policy_context: bool = False,
) -> dict[str, Any]:
    """Unwrap Query IR, hoist response options, and apply the resolved context.

    The adapter supplies its existing object validator to preserve protocol
    error contracts. An authoritative context replaces caller claims; local
    contexts merge with outer values taking precedence over nested values.
    """

    query = object_payload(payload.get("query", payload), field="query")
    query.pop("request_id", None)
    for name in ("verbosity", "sql_profile"):
        if name not in query and payload.get(name) not in (None, ""):
            query[name] = payload[name]
    nested = object_payload(query.get("policy_context"), field="query.policy_context")
    if replace_policy_context:
        query.pop("policy_context", None)
        if policy_context:
            query["policy_context"] = dict(policy_context)
    elif "policy_context" in query or policy_context:
        query["policy_context"] = {**nested, **policy_context}
    return query
