"""HTTP request parsing and coercion.

This module owns the **stateless** logic that turns a raw HTTP
request (headers, query string, JSON body) into the structured
shapes the runtime needs:

* type coercion (:func:`coerce_bool`, :func:`coerce_int`,
  :func:`coerce_string_list`) and the :class:`HTTPInputError`
  raised when a client sends the wrong shape
* request-id derivation from header / query / payload precedence
  (:func:`request_id_from_parts`, :func:`clean_request_id`)
* case-insensitive header lookup (:func:`header_value`)
* public route acceptance for the canonical ``/api/v1/*`` HTTP surface
  (:func:`public_api_route`) and route-path normalization that maps
  ``/api/v1/foo`` to ``/foo`` (:func:`normalize_route`)
* envelope-error helpers (:func:`issue`, :func:`status_label`)
* policy-context and query-body payload builders
  (:func:`policy_context_payload`, :func:`query_payload`)

Extracted from :mod:`semantic_rails.http_core` to keep that module
focused on the stateful :class:`SemanticHTTPService` (routing,
runtime calls, JSON envelope assembly). The public API is unchanged
— every name here is re-exported from :mod:`semantic_rails.http_core`
so existing callers do not need to change their imports.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .request_context import RequestContext


class HTTPInputError(ValueError):
    """Client request shape error that should be returned as a 400 envelope."""


def clean_request_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
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


def coerce_int(value: Any, default: int, *, field: str, minimum: int | None = None) -> int:
    if value is None or value == "":
        parsed = default
    elif isinstance(value, bool):
        raise HTTPInputError(f"Field '{field}' must be an integer.")
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPInputError(f"Field '{field}' must be an integer.") from exc
    if minimum is not None and parsed < minimum:
        raise HTTPInputError(f"Field '{field}' must be greater than or equal to {minimum}.")
    return parsed


def coerce_string_list(value: Any, *, field: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    raise HTTPInputError(f"Field '{field}' must be a string or array of strings.")


PUBLIC_API_PREFIX = "/api/v1"


def public_api_route(path: str) -> str | None:
    route = str(path or "/")
    if route == PUBLIC_API_PREFIX or route.startswith(f"{PUBLIC_API_PREFIX}/"):
        return normalize_route(route)
    return None


def normalize_route(path: str) -> str:
    route = str(path or "/")
    if route == PUBLIC_API_PREFIX:
        route = "/"
    elif route.startswith(f"{PUBLIC_API_PREFIX}/"):
        route = route[len(PUBLIC_API_PREFIX) :]
    return route.rstrip("/") if len(route) > 1 else route


def header_value(headers: Mapping[str, Any] | None, name: str) -> str:
    target = name.lower()
    for key, value in dict(headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def issue(code: str, message: str, *, status: str = "error") -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": status,
        "stage": "http",
        "details": {},
        "object_ids": [],
        "path": "",
        "recovery_hints": [],
    }


def status_label(http_status: int, payload: Mapping[str, Any]) -> str:
    if "status" in payload:
        return str(payload["status"])
    if bool(payload.get("ok", 200 <= http_status < 400)):
        return "ok"
    return "error"


def request_id_from_parts(
    headers: Mapping[str, Any] | None,
    query_params: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    return (
        clean_request_id(header_value(headers, "X-Request-ID"))
        or clean_request_id(header_value(headers, "X-Correlation-ID"))
        or clean_request_id((query_params or {}).get("request_id"))
        or clean_request_id((payload or {}).get("request_id"))
        or uuid.uuid4().hex
    )


def _object_payload(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise HTTPInputError(f"Field '{field}' must be a JSON object.")
    return dict(value)


def _safe_request_context_payload(
    payload: Mapping[str, Any] | None,
    query_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect caller context only for the configured resolver to interpret.

    The default local resolver intentionally accepts body/query-parameter
    context. Hosted resolvers may ignore it and derive identity elsewhere.
    Nested Query IR context is collected first, then the outer envelope (the
    same precedence used by the local query builder), followed by the two GET
    catalog context parameters.
    """

    raw_payload = dict(payload or {}) if isinstance(payload, Mapping) else {}
    policy_context: dict[str, Any] = {}
    raw_query = raw_payload.get("query")
    if isinstance(raw_query, Mapping):
        nested = raw_query.get("policy_context")
        if isinstance(nested, Mapping):
            policy_context.update(dict(nested))
    outer = raw_payload.get("policy_context")
    if isinstance(outer, Mapping):
        policy_context.update(dict(outer))
    for key in ("environment", "audience"):
        value = dict(query_params or {}).get(key)
        if value not in (None, ""):
            policy_context[key] = value
    return {"policy_context": policy_context} if policy_context else {}


def authoritative_request_payload(
    payload: Mapping[str, Any], context: RequestContext | None
) -> dict[str, Any]:
    """Strip caller policy claims when a transport resolved trusted context.

    Direct/local callers pass ``context=None`` and retain the trusted-local
    body behavior. Remote transports always pass a resolved RequestContext;
    in that mode *all* caller-controlled context locations are removed before
    the resolver result is injected. Omitted resolver fields stay omitted.
    """

    out = dict(payload or {})
    if context is None:
        return out

    if "policy_context" in out:
        _object_payload(out.get("policy_context"), field="policy_context")
    out.pop("policy_context", None)
    raw_query = out.get("query")
    if isinstance(raw_query, Mapping):
        query = dict(raw_query)
        if "policy_context" in query:
            _object_payload(query.get("policy_context"), field="query.policy_context")
        query.pop("policy_context", None)
        out["query"] = query
    trusted = context.to_policy_context()
    if trusted:
        out["policy_context"] = trusted
    return out


def policy_context_payload(
    payload: Mapping[str, Any] | None = None, context: RequestContext | None = None
) -> dict[str, Any]:
    if context is not None:
        return context.to_policy_context()
    raw_payload = dict(payload or {})
    policy_context = _object_payload(raw_payload.get("policy_context"), field="policy_context")
    return policy_context


def query_payload(
    payload: Mapping[str, Any], context: RequestContext | None = None
) -> dict[str, Any]:
    raw_query = payload.get("query", payload)
    query = _object_payload(raw_query, field="query")
    query.pop("request_id", None)
    # Hoist response-shape knobs from the outer HTTP envelope into the
    # query payload so runtime methods see them via `resolve_verbosity` /
    # `resolve_sql_profile`. Mirrors the MCP `_query_payload` helper.
    raw_payload = dict(payload or {})
    if "verbosity" not in query and raw_payload.get("verbosity") not in (None, ""):
        query["verbosity"] = raw_payload["verbosity"]
    if "sql_profile" not in query and raw_payload.get("sql_profile") not in (None, ""):
        query["sql_profile"] = raw_payload["sql_profile"]
    nested_context = _object_payload(query.get("policy_context"), field="query.policy_context")
    policy_context = policy_context_payload(payload, context)
    if context is not None:
        query.pop("policy_context", None)
        if policy_context:
            query["policy_context"] = policy_context
    elif "policy_context" in query or policy_context:
        nested_context.update(policy_context)
        query["policy_context"] = nested_context
    return query
