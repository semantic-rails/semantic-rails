"""Protocol-neutral request shaping shared by HTTP and MCP.

Transport adapters own identity resolution, input errors, and response defaults.
This module owns the rules every transport shares, such as refusing a request that
claims an identity, so a new transport cannot accept a caller's claim.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
    {"measure", "metric", "segment", "dimension", "entity", "dimension_value", "relationship"}
)
# A kind may also be named by its results' bucket: ``measures`` means ``measure``.
_KIND_PLURALS = {
    "measures": "measure",
    "metrics": "metric",
    "segments": "segment",
    "dimensions": "dimension",
    "entities": "entity",
    "dimension_values": "dimension_value",
    "relationships": "relationship",
    "temporal_roles": "temporal_role",
    "value_domains": "value_domain",
}


def singular_kinds(kinds: Sequence[str] | None) -> list[str]:
    """``kinds`` with each plural read as its singular, in order."""
    return [_KIND_PLURALS.get(kind, kind) for kind in kinds or []]


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
    """Return ``kinds`` with each plural read as its singular (``measures`` is
    ``measure``), or refuse when any value is not in ``valid``.

    A ``kinds`` filter that names no real kind would empty every bucket, and an
    empty result reads as "nothing matches". Refusing keeps that text for
    searches that really ran over the requested kinds.
    """

    requested = singular_kinds(kinds)
    unknown = [kind for kind in requested if kind not in valid]
    if unknown:
        raise unknown_discover_kinds_error(unknown, valid)
    return requested


# Where a trusted local caller's identity comes from, by transport.
_LOCAL_IDENTITY = {
    "sdk": "pass the caller's identity as the `context` argument",
    "mcp": "this server reads the caller's identity from its configured credentials or session",
    "cli": "pass identity with --environment and --audience",
}


def identity_claim_refusal(
    path: str, *, remote: bool, transport: str = "sdk"
) -> SemanticLayerError:
    """The refusal of a request that says who is asking: identity comes only from credentials.

    Behind a door that resolved the caller's credentials (``remote``) it is
    ``IDENTITY_CLAIM_REFUSED``; for a trusted local caller the key is just unknown, and the
    hint says where that ``transport`` reads identity from.
    """
    from .query_call import refusal

    if remote:
        fix = f"Remove {path}; the server reads the caller's identity from the credentials."
        return SemanticLayerError(
            "IDENTITY_CLAIM_REFUSED",
            f"{path}: a request may not say who is asking; that comes from its credentials",
            details={"path": path, "recovery_hints": [{"code": "FIX_QUERY_CALL", "message": fix}]},
        )
    return refusal(
        path,
        "is not part of a request; identity never travels in it",
        fix=f"Remove {path}; {_LOCAL_IDENTITY[transport]}.",
    )


def refuse_identity_claims(
    request: Mapping[str, Any], *, remote: bool, transport: str = "sdk"
) -> None:
    """Refuse an identity key anywhere in a request, at its full path; it is never stripped."""
    from .query_call import identity_claim_path

    path = identity_claim_path(request)
    if path is not None:
        raise identity_claim_refusal(path, remote=remote, transport=transport)
