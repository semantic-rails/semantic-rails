"""Protocol-neutral request shaping shared by HTTP and MCP.

Transport adapters own identity resolution, input errors, and response defaults.
This module owns the common query envelope and policy-claim removal rules so a
new transport cannot accidentally preserve a nested caller claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .errors import SemanticLayerError


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


def parse_string_list(value: Any) -> list[str]:
    """Read a list-of-strings argument however a client encoded it.

    Accepts an array, a comma-separated string, or a JSON-encoded array in a
    string (some agents send ``'["metric"]'``). Anything else raises
    ``ValueError`` so no transport turns a malformed list into an empty filter.
    """

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                decoded = json.loads(text)
            except (ValueError, RecursionError) as exc:
                raise ValueError("looks like JSON but does not parse") from exc
            if not isinstance(decoded, list):
                raise ValueError("must be an array of strings")
            value = decoded
        else:
            return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        if not all(isinstance(part, str) for part in value):
            raise ValueError("must contain only strings")
        return [part.strip() for part in value if part.strip()]
    raise ValueError("must be a string or array of strings")


def checked_string_list(value: Any, *, field: str) -> list[str]:
    """:func:`parse_string_list` for callers outside HTTP and MCP (the CLI).

    A value that does not parse is refused with ``INVALID_MCP_ARGUMENTS`` naming
    ``field``, never read as a literal item.
    """

    try:
        return parse_string_list(value)
    except ValueError as exc:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            f"Argument '{field}' {exc}.",
            details={"field": field, "argument_type": type(value).__name__},
        ) from exc


DISCOVER_RANKED_KINDS: frozenset[str] = frozenset(
    {"measure", "metric", "segment", "dimension", "entity", "dimension_value"}
)


def unknown_discover_kinds_error(
    unknown: Sequence[str], valid: frozenset[str]
) -> SemanticLayerError:
    unknown = list(unknown)
    return SemanticLayerError(
        "INVALID_MCP_ARGUMENTS",
        f"Unknown kinds value(s) {unknown}; valid kinds: {sorted(valid)}.",
        details={"field": "kinds", "unknown_kinds": unknown, "valid_kinds": sorted(valid)},
    )


def checked_discover_kinds(kinds: Sequence[str] | None, valid: frozenset[str]) -> list[str]:
    """Return ``kinds`` unchanged, or refuse when any value is not in ``valid``.

    A ``kinds`` filter that names no real kind would empty every bucket, and an
    empty result reads as "nothing matches". Refusing keeps that text for
    searches that really ran over the requested kinds.
    """

    requested = list(kinds or [])
    unknown = [kind for kind in requested if kind not in valid]
    if unknown:
        raise unknown_discover_kinds_error(unknown, valid)
    return requested


def checked_discover_limit(limit: int) -> int:
    """Return ``limit``, or refuse a value below 1.

    The buckets are cut to ``limit``, so a zero limit would empty them and read
    as "no match" for a search that found something.
    """

    if limit < 1:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            "Argument 'limit' must be at least 1.",
            details={"field": "limit", "argument_type": type(limit).__name__},
        )
    return limit


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
