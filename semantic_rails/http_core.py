"""HTTP routing and response shaping for the semantic layer.

:class:`SemanticHTTPService` owns the v1 routing table, the runtime
calls behind each route, policy-context resolution, and JSON envelope
assembly. Both :mod:`semantic_rails.api` (threaded ``http.server``)
and :mod:`semantic_rails.asgi` (ASGI) wrap this service — they only
differ in transport. This module also owns the CORS allow-list config
that hosted deployments configure via env vars.

The stateless request-parsing primitives (type coercion,
request-id derivation, policy/query-body builders, route
normalization) live in :mod:`semantic_rails.http_request`. They are
re-exported here so external callers (``api``, ``asgi``,
``mcp_server``, tests) keep importing from ``semantic_rails.http_core``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

from .catalog_service import resolve_catalog
from .diagnostics import (
    enrich_expression_ast_error,
    enrich_object_not_found,
    enrich_path_not_found,
    exception_issue,
)
from .dialects import dialect_for_warehouse
from .errors import SemanticLayerError
from .http_request import (
    HTTPInputError,
    _safe_request_context_payload,
    authoritative_request_payload,
    clean_request_id,
    coerce_bool,
    coerce_int,
    coerce_string_list,
    header_value,
    issue,
    normalize_route,
    policy_context_payload,
    public_api_route,
    query_payload,
    request_id_from_parts,
    status_label,
)
from .metadata import (
    build_options_payload,
    discover_payload,
    inspect_payload,
    valid_values_payload,
)
from .metadata_parts.capabilities import _EXPRESSION_SHAPES
from .planner import plan_payload
from .request_context import (
    RequestContext,
    api_key_auth_result,
    get_policy_context_resolver,
    request_context_payload,
)
from .runtime import Runtime

__all__ = [
    "API_VERSION",
    "CORS_ALLOW_HEADERS",
    "HTTPInputError",
    "PUBLIC_V1_ROUTES",
    "SemanticHTTPService",
    "clean_request_id",
    "coerce_bool",
    "coerce_int",
    "coerce_string_list",
    "cors_allowed_origins",
    "cors_origin_header",
    "header_value",
    "issue",
    "normalize_route",
    "policy_context_payload",
    "public_api_route",
    "query_payload",
    "request_id_from_parts",
    "status_label",
]


API_VERSION = "v1"
# Shared request-body ceiling for every HTTP transport (ASGI, MCP
# streamable HTTP, the stdlib servers). Query IR payloads are small;
# 64 KiB leaves generous headroom while bounding what an unauthenticated
# caller can make the server buffer.
MAX_REQUEST_BODY_BYTES = 64 * 1024
CORS_ALLOW_HEADERS = (
    "Authorization, Content-Type, X-API-Key, X-Semantic-API-Key, X-Request-ID, "
    "X-Correlation-ID, X-Semantic-Actor, X-Semantic-Tenant, X-Semantic-Project, "
    "X-Semantic-Roles, X-Semantic-Environment, X-Semantic-Audience, "
    "Accept, MCP-Protocol-Version"
)

PUBLIC_V1_ROUTES = [
    {"method": "GET", "path": "/api/v1/health"},
    {"method": "GET", "path": "/api/v1/ready"},
    {"method": "GET", "path": "/api/v1/capabilities"},
    {"method": "GET", "path": "/api/v1/catalog"},
    {"method": "POST", "path": "/api/v1/catalog"},
    {"method": "POST", "path": "/api/v1/discover"},
    {"method": "POST", "path": "/api/v1/inspect"},
    {"method": "POST", "path": "/api/v1/build-options"},
    {"method": "POST", "path": "/api/v1/valid-values"},
    {"method": "POST", "path": "/api/v1/plan"},
    {"method": "POST", "path": "/api/v1/validate"},
    {"method": "POST", "path": "/api/v1/compile"},
    {"method": "POST", "path": "/api/v1/query"},
    {"method": "POST", "path": "/api/v1/segment-validate"},
    {"method": "POST", "path": "/api/v1/segment-explain"},
    {"method": "POST", "path": "/api/v1/segment-preview"},
]


def cors_allowed_origins() -> tuple[str, ...]:
    """Parse `SEMANTIC_RAILS_CORS_ORIGINS` into a tuple of allowed origins.

    Empty / unset means no cross-origin browser access. A comma-separated
    list opts specific origins in; the single value `*` opts every origin
    in explicitly.
    """
    raw = os.environ.get("SEMANTIC_RAILS_CORS_ORIGINS", "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def cors_origin_header(request_origin: str | None) -> str:
    """Resolve the value to send in `Access-Control-Allow-Origin` for a
    request whose `Origin` header was `request_origin`.

    Logic:
      - If no allow-list is configured, return an empty string. Binding to
        loopback is NOT a defence here: a page in the operator's own
        browser can reach 127.0.0.1, so defaulting to `*` handed any
        website the ability to drive the server as the operator — read the
        catalog, compile, and run queries — with auth off by default.
        Cross-origin access is now opt-in.
      - If `*` is in the allow-list, return `*` — an explicit, auditable
        choice rather than a default.
      - If the request's Origin matches the allow-list, echo it.
      - Otherwise return an empty string — the caller MUST NOT send the
        CORS header at all in that case (browsers reject the response).
    """
    allowed = cors_allowed_origins()
    if not allowed:
        return ""
    if "*" in allowed:
        return "*"
    origin = (request_origin or "").strip()
    if origin and origin in allowed:
        return origin
    return ""


class SemanticHTTPService:
    def __init__(self, runtime: Runtime, package_id: str | None = None) -> None:
        self.runtime = runtime
        self.package_id = package_id or runtime.package_id

    def request_context(
        self,
        headers: Mapping[str, Any] | None,
        *,
        payload: Mapping[str, Any] | None = None,
        query_params: Mapping[str, Any] | None = None,
        request_id: str,
    ) -> RequestContext:
        # Goes through the pluggable PolicyContextResolver so hosted
        # deployments can replace header-trusting with identity-derived
        # policy context without forking. See
        # `semantic_rails.request_context.PolicyContextResolver`.
        resolver = get_policy_context_resolver()
        return resolver.resolve(
            headers,
            payload=_safe_request_context_payload(payload, query_params),
            request_id=request_id,
        )

    def auth_ok(self, route: str, headers: Mapping[str, Any] | None) -> bool:
        deep_ready_check = route == "/ready" and coerce_bool(
            header_value(headers, "X-Semantic-Check-Warehouse"), False
        )
        if route == "/health" or (route == "/ready" and not deep_ready_check):
            return True
        ok, _ = api_key_auth_result(headers)
        return ok

    def unauthorized_payload(self) -> dict[str, Any]:
        auth_issue = issue("UNAUTHORIZED", "Missing or invalid bearer API key.", status="error")
        return {"ok": False, "status": "error", "error": auth_issue, "errors": [auth_issue]}

    def not_found_payload(self) -> dict[str, Any]:
        not_found = issue("NOT_FOUND", "No Semantic Rails HTTP route matches this path.")
        return {"ok": False, "status": "error", "error": not_found, "errors": [not_found]}

    def invalid_json_payload(self, message: str) -> dict[str, Any]:
        invalid_issue = issue("INVALID_JSON", f"Invalid JSON request body: {message}")
        return {"ok": False, "error": invalid_issue, "errors": [invalid_issue]}

    def invalid_request_payload(self, message: str) -> dict[str, Any]:
        invalid_issue = issue("INVALID_REQUEST", message)
        return {"ok": False, "error": invalid_issue, "errors": [invalid_issue]}

    def exception_payload(self, exc: Exception, *, stage: str) -> tuple[dict[str, Any], int]:
        if isinstance(exc, HTTPInputError):
            return self.invalid_request_payload(str(exc)), 400
        if isinstance(exc, SemanticLayerError):
            enriched = enrich_object_not_found(exc, self.runtime.config)
            enriched = enrich_expression_ast_error(enriched, self.runtime.config)
            enriched = enrich_path_not_found(enriched, self.runtime.config)
            error_issue = exception_issue(enriched, stage=stage)
            return {
                "ok": False,
                "status": "error",
                "error": error_issue,
                "errors": [error_issue],
                "recovery_hints": list(error_issue.get("recovery_hints", [])),
            }, 400
        # Defensive INTERNAL_ERROR envelope — bare exceptions (KeyError,
        # AttributeError, TypeError, etc.) must never escape as a raw
        # traceback. Log the trace for ops and emit a public hint pointing
        # the caller at the bug tracker so the envelope is actually
        # recoverable.
        import logging

        logging.getLogger(__name__).exception(
            "unhandled exception in %s stage: %s",
            stage,
            exc,
        )
        error_issue = {
            "code": "INTERNAL_ERROR",
            "message": f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
            "severity": "error",
            "stage": stage,
            "details": {
                "exception_type": type(exc).__name__,
            },
            "object_ids": [],
            "path": "",
            "recovery_hints": [
                {
                    "kind": "file_bug_report",
                    "message": (
                        "An unexpected error reached the HTTP boundary. "
                        "Retry once; if it recurs, please file a bug at "
                        "https://github.com/semantic-rails/semantic-rails/issues "
                        "with the request body and the request_id from "
                        "this response."
                    ),
                }
            ],
        }
        return {
            "ok": False,
            "status": "error",
            "error": error_issue,
            "errors": [error_issue],
            "recovery_hints": list(error_issue["recovery_hints"]),
        }, 500

    def envelope(
        self,
        http_status: int,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        started: float | None = None,
        request_context: RequestContext | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        out = dict(payload or {})
        out.setdefault("ok", 200 <= http_status < 400)
        out["status"] = status_label(http_status, out)
        out.setdefault("api_version", API_VERSION)
        out.setdefault("request_id", request_id)
        out.setdefault("package_id", self.package_id)
        out.setdefault("warnings", [])
        if "errors" not in out:
            error = out.get("error")
            if isinstance(error, dict):
                out["errors"] = [error]
            elif error:
                out["errors"] = [issue(str(error).upper(), str(error))]
            else:
                out["errors"] = []
        if "recovery_hints" not in out:
            recovery_hints: list[Any] = []
            for error in list(out.get("errors", []) or []):
                if isinstance(error, dict):
                    recovery_hints.extend(list(error.get("recovery_hints", []) or []))
            out["recovery_hints"] = recovery_hints
        if started is not None:
            out.setdefault("timing_ms", round((time.perf_counter() - started) * 1000, 3))
        if request_context is not None:
            if isinstance(request_context, RequestContext):
                out.setdefault("request_context", request_context_payload(request_context))
            else:
                out.setdefault("request_context", dict(request_context))
        return out

    def health_payload(self) -> dict[str, Any]:
        with self.runtime.request_scope():
            config = self.runtime.config
            return {
                "ok": True,
                "service": "semantic-rails",
                "package": {
                    "id": config.package.package_id,
                    "name": config.package.name,
                },
                "schema_version": config.version,
                "warehouse": self.runtime.warehouse,
            }

    def ready_payload(self, headers: Mapping[str, Any] | None) -> dict[str, Any]:
        with self.runtime.request_scope():
            checks: list[dict[str, Any]] = []
            config = self.runtime.config
            checks.append(
                {"name": "package_loaded", "ok": True, "package_id": config.package.package_id}
            )
            if coerce_bool(header_value(headers, "X-Semantic-Check-Warehouse"), False):
                try:
                    with self.runtime._query_lock:
                        adapter = self.runtime._get_adapter()
                        adapter.query("SELECT 1 AS semantic_rails_ready")
                    checks.append(
                        {"name": "warehouse", "ok": True, "warehouse": self.runtime.warehouse}
                    )
                except Exception as exc:
                    checks.append(
                        {
                            "name": "warehouse",
                            "ok": False,
                            "warehouse": self.runtime.warehouse,
                            "error": str(exc),
                        }
                    )
            ok = all(bool(check.get("ok")) for check in checks)
            return {
                "ok": ok,
                "service": "semantic-rails",
                "package": {"id": config.package.package_id, "name": config.package.name},
                "schema_version": config.version,
                "warehouse": self.runtime.warehouse,
                "checks": checks,
            }

    def capabilities_payload(self) -> dict[str, Any]:
        with self.runtime.request_scope():
            catalog = resolve_catalog(self.runtime, view="summary", verbosity="compact")
            dialect = dialect_for_warehouse(self.runtime.warehouse)
            package = dict(catalog["meta"]["package"])
            package.setdefault("id", str(package.get("package_id", "")))
            return {
                "ok": True,
                "supported_api_versions": [API_VERSION],
                "route_prefix": "/api/v1",
                "routes": list(PUBLIC_V1_ROUTES),
                "schema_version": self.runtime.config.version,
                "package": package,
                "warehouse": self.runtime.warehouse,
                "dialect": dialect.name,
                "warehouse_capabilities": dialect.capabilities(),
                "capabilities": list(catalog["capabilities"]),
                "unsupported_capabilities": list(catalog["unsupported_capabilities"]),
                # Documented in docs/QUERY_API.md: the accepted
                # ``select[*].expression`` shapes as {name, description, example}
                # dicts. Sourced from the same registry the MCP capabilities tool
                # ships (metadata_parts/capabilities.py), so the HTTP and MCP
                # surfaces cannot drift apart — the examples there are
                # parse-validated by tests/semantic_rails/test_mcp_tool_descriptions.py.
                "expression_shapes": [dict(shape) for shape in _EXPRESSION_SHAPES],
            }

    def handle(
        self,
        method: str,
        route: str,
        payload: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, Any] | None = None,
        query_params: Mapping[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> tuple[dict[str, Any], int]:
        method = method.upper()
        payload = dict(payload or {})
        query_params = dict(query_params or {})
        if method == "GET":
            return self._handle_get(
                route, query_params=query_params, headers=headers, context=context
            )
        if method == "POST":
            # A transport-resolved RequestContext is the entire authority:
            # strip caller claims first, then inject only the resolver result.
            # Direct/local calls with context=None retain body context support.
            payload = authoritative_request_payload(payload, context)
            policy_context = policy_context_payload(payload, context)
            return self._handle_post(route, payload, policy_context=policy_context)
        return self.not_found_payload(), 404

    def _handle_get(
        self,
        route: str,
        *,
        query_params: Mapping[str, Any],
        headers: Mapping[str, Any] | None,
        context: RequestContext | None,
    ) -> tuple[dict[str, Any], int]:
        if route == "/health":
            return self.health_payload(), 200
        if route == "/ready":
            payload = self.ready_payload(headers)
            return payload, 200 if payload["ok"] else 503
        if route == "/capabilities":
            return self.capabilities_payload(), 200
        if route == "/catalog":
            policy_context = {
                "environment": str(query_params.get("environment", "")),
                "audience": str(query_params.get("audience", "")),
            }
            policy_context = policy_context_payload({"policy_context": policy_context}, context)
            return {
                "ok": True,
                "package_id": self.package_id,
                "catalog": resolve_catalog(
                    self.runtime,
                    view=str(query_params.get("view", "summary")),
                    verbosity=str(query_params.get("verbosity", "compact")),
                    kind=str(query_params.get("kind", "")),
                    search=str(query_params.get("search", "")),
                    entity=str(query_params.get("entity", "")),
                    policy_context=policy_context,
                ),
            }, 200
        return self.not_found_payload(), 404

    def _handle_post(
        self,
        route: str,
        payload: Mapping[str, Any],
        *,
        policy_context: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        if route == "/catalog":
            return {
                "ok": True,
                "package_id": self.package_id,
                "catalog": resolve_catalog(
                    self.runtime,
                    view=str(payload.get("view", "summary")),
                    verbosity=str(payload.get("verbosity", "compact")),
                    kind=str(payload.get("kind", "")),
                    search=str(payload.get("search", "")),
                    entity=str(payload.get("entity", "")),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/discover":
            return {
                "ok": True,
                **discover_payload(
                    self.runtime,
                    terms=str(payload.get("terms", "")),
                    kinds=coerce_string_list(payload.get("kinds"), field="kinds"),
                    partial_query=query_payload(payload)
                    if payload.get("query") or payload.get("policy_context")
                    else None,
                    stage=str(payload.get("stage", "")),
                    verbosity=str(payload.get("verbosity", "compact")),
                    limit=coerce_int(payload.get("limit"), 10, field="limit", minimum=1),
                    enforce_scope=True,
                ),
            }, 200
        if route == "/inspect":
            return {
                "ok": True,
                **inspect_payload(
                    self.runtime,
                    object_id=str(payload.get("object_id", "")),
                    partial_query=query_payload(payload)
                    if payload.get("query") or payload.get("policy_context")
                    else None,
                    verbosity=str(payload.get("verbosity", "compact")),
                ),
            }, 200
        if route == "/build-options":
            return {
                "ok": True,
                **build_options_payload(
                    self.runtime,
                    partial_query=query_payload(payload),
                    focus_terms=str(payload.get("focus_terms", "")),
                    focus_object_id=str(payload.get("focus_object_id", "")),
                    step=str(payload.get("step", "")),
                    stage=str(payload.get("stage", "")),
                    verbosity=str(payload.get("verbosity", "compact")),
                    include_blocked=coerce_bool(payload.get("include_blocked"), True),
                    limit=coerce_int(payload.get("limit"), 10, field="limit", minimum=1),
                ),
            }, 200
        if route == "/plan":
            return {
                "ok": True,
                **plan_payload(
                    self.runtime,
                    intent=payload.get("intent"),
                    partial_query=query_payload(payload)
                    if payload.get("query") or payload.get("policy_context")
                    else None,
                    detail=str(payload.get("detail", "best") or "best"),
                    limit=coerce_int(payload.get("limit"), 3, field="limit", minimum=1),
                ),
            }, 200
        if route == "/validate":
            return {"ok": True, **self.runtime.validate(query_payload(payload))}, 200
        if route == "/compile":
            return {"ok": True, **self.runtime.compile(query_payload(payload))}, 200
        if route == "/query":
            return {"ok": True, **self.runtime.query(query_payload(payload))}, 200
        if route == "/segment-validate":
            return {
                "ok": True,
                **self.runtime.segment_validate(
                    str(payload.get("segment_id", "")),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/segment-explain":
            return {
                "ok": True,
                **self.runtime.segment_explain(
                    str(payload.get("segment_id", "")),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/segment-preview":
            return {
                "ok": True,
                **self.runtime.segment_preview(
                    str(payload.get("segment_id", "")),
                    limit=int(payload.get("limit", 50)),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/valid-values":
            return {
                "ok": True,
                **valid_values_payload(
                    self.runtime,
                    dimension_id=str(payload.get("dimension_id", "")),
                    query=query_payload(payload)
                    if payload.get("query") or payload.get("policy_context")
                    else None,
                    search=str(payload.get("search", "")),
                    limit=coerce_int(payload.get("limit"), 100, field="limit", minimum=1),
                    offset=coerce_int(payload.get("offset"), 0, field="offset", minimum=0),
                    include_counts=coerce_bool(payload.get("include_counts"), False),
                    allow_live_query=coerce_bool(payload.get("allow_live_query"), False),
                ),
            }, 200
        return self.not_found_payload(), 404
