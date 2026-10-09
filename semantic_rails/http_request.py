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
* public route acceptance for ``/api/v1/*`` and the public ``/health`` probe
  (:func:`public_api_route`) and route-path normalization that maps
  ``/api/v1/foo`` to ``/foo`` (:func:`normalize_route`)
* envelope-error helpers (:func:`issue`, :func:`status_label`)

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

from .request_payload import (
    clean_request_id as clean_request_id,
)
from .request_payload import (
    coerce_bool as coerce_bool,
)
from .request_payload import parse_string_list


class HTTPInputError(ValueError):
    """Client request shape error that should be returned as a 400 envelope."""


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
    try:
        return parse_string_list(value)
    except ValueError as exc:
        raise HTTPInputError(f"Field '{field}' {exc}.") from exc


PUBLIC_API_PREFIX = "/api/v1"


def public_api_route(path: str) -> str | None:
    route = str(path or "/")
    if route.rstrip("/") == "/health":
        return "/health"
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
