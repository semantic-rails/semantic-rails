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

from . import operations
from .api_keys import api_key_auth_result
from .diagnostics import (
    enrich_expression_ast_error,
    enrich_object_not_found,
    enrich_path_not_found,
    exception_issue,
)
from .dialects import dialect_for_warehouse
from .errors import UNEXPECTED_ERROR_MESSAGE, SemanticLayerError, query_execution_error
from .http_request import (
    HTTPInputError,
    clean_request_id,
    coerce_bool,
    coerce_int,
    coerce_string_list,
    header_value,
    issue,
    normalize_route,
    public_api_route,
    request_id_from_parts,
    status_label,
)
from .metadata_parts.capabilities import capabilities_payload
from .query_call import EXPRESSION_SHAPES
from .request_context import (
    RequestContext,
    get_policy_context_resolver,
    request_context_payload,
)
from .request_payload import identity_claim_refusal, refuse_identity_claims
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
    "public_api_route",
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


# Options a REST call may set beside its query (``{"query": ..., "options": {...}}``).
CALL_OPTIONS = ("now", "verbosity", "sql_profile")
# Query parameters that would say who is asking; identity comes only from credentials.
IDENTITY_PARAMS = ("environment", "audience")


def call_options(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ``options`` beside a REST call (``now``, ``verbosity``, ``sql_profile``)."""
    options = payload.get("options") or {}
    if not isinstance(options, Mapping):
        raise SemanticLayerError(
            "INVALID_QUERY", "options must be an object", details={"path": "options"}
        )
    unknown = sorted(set(options) - set(CALL_OPTIONS))
    if unknown:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"options.{unknown[0]} is not a call option",
            details={"path": f"options.{unknown[0]}", "supported_keys": list(CALL_OPTIONS)},
        )
    return {key: options[key] for key in CALL_OPTIONS if key in options}


def call_arguments(payload: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The call in a REST body and the options beside it.

    The body is ``{"query": <call>, "options": {...}}``, or the call itself.
    """
    if "query" not in payload:
        return {key: value for key, value in payload.items() if key != "request_id"}, {}
    if not isinstance(payload["query"], Mapping):
        raise HTTPInputError("Field 'query' must be a JSON object.")
    extra = sorted(set(payload) - {"query", "options", "request_id"})
    if extra:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"{extra[0]} belongs in options or in the query",
            details={"path": extra[0], "supported_keys": ["query", "options"]},
        )
    return payload["query"], call_options(payload)


def _seed_call(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The partial call a metadata or plan route starts from (its ``query``)."""
    seed = payload.get("query")
    if seed is not None and not isinstance(seed, Mapping):
        raise HTTPInputError("Field 'query' must be a JSON object.")
    return dict(seed or {})


class SemanticHTTPService:
    def __init__(self, runtime: Runtime, package_id: str | None = None) -> None:
        self.runtime = runtime
        self.package_id = package_id or runtime.package_id

    def request_context(
        self, headers: Mapping[str, Any] | None, *, request_id: str
    ) -> RequestContext:
        # Goes through the pluggable PolicyContextResolver so hosted
        # deployments can replace header-trusting with identity-derived
        # policy context without forking. It reads credentials, never the body.
        # See `semantic_rails.request_context.PolicyContextResolver`.
        return get_policy_context_resolver().resolve(headers, request_id=request_id)

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

    def exception_payload(
        self, exc: Exception, *, stage: str, context: RequestContext | None = None
    ) -> tuple[dict[str, Any], int]:
        if isinstance(exc, HTTPInputError):
            return self.invalid_request_payload(str(exc)), 400
        if isinstance(exc, SemanticLayerError):
            enriched = exc
            try:
                # Suggestions come from the caller's view; without one, none are offered.
                config = self.runtime.view_for(context) if context is not None else None
            except Exception:  # noqa: BLE001 — uncertain visibility suggests nothing
                config = None
            if config is not None:
                enriched = enrich_object_not_found(enriched, config)
                enriched = enrich_expression_ast_error(enriched, config)
                enriched = enrich_path_not_found(enriched, config)
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
            "message": UNEXPECTED_ERROR_MESSAGE,
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
        if "errors" not in out:
            error = out.get("error")
            if isinstance(error, dict):
                out["errors"] = [error]
            elif error:
                out["errors"] = [issue(str(error).upper(), str(error))]
        if "recovery_hints" not in out:
            recovery_hints: list[Any] = []
            for error in list(out.get("errors", []) or []):
                if isinstance(error, dict):
                    recovery_hints.extend(list(error.get("recovery_hints", []) or []))
            out["recovery_hints"] = recovery_hints
        # Errors, warnings and hints only when there are some; no empty fields.
        for key in ("errors", "warnings", "recovery_hints"):
            if not out.get(key):
                out.pop(key, None)
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
            config = self.runtime._config
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
            config = self.runtime._config
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
                            "error": str(exc)
                            if isinstance(exc, SemanticLayerError)
                            else str(query_execution_error({"engine": self.runtime.warehouse})),
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

    def capabilities_payload(self, context: RequestContext | None = None) -> dict[str, Any]:
        with self.runtime.request_scope():
            if context is not None and context.metric_allowlist is not None:
                dialect = dialect_for_warehouse(self.runtime.warehouse)
                return {
                    "ok": True,
                    "supported_api_versions": [API_VERSION],
                    "route_prefix": "/api/v1",
                    "routes": [
                        row
                        for row in PUBLIC_V1_ROUTES
                        if "segment-" not in row["path"]
                        and not row["path"].endswith("/valid-values")
                    ],
                    "warehouse": self.runtime.warehouse,
                    "dialect": dialect.name,
                    "warehouse_capabilities": dialect.capabilities(),
                    **capabilities_payload(
                        self.runtime, policy_context=context.to_policy_context()
                    ),
                }
            catalog = operations.run(
                "catalog",
                self.runtime,
                view="summary",
                verbosity="compact",
                policy_context=context.to_policy_context() if context is not None else None,
            )
            dialect = dialect_for_warehouse(self.runtime.warehouse)
            package = dict(catalog["meta"]["package"])
            package.setdefault("id", str(package.get("package_id", "")))
            return {
                "ok": True,
                "supported_api_versions": [API_VERSION],
                "route_prefix": "/api/v1",
                "routes": list(PUBLIC_V1_ROUTES),
                "schema_version": self.runtime._config.version,
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
                "expression_shapes": [dict(shape) for shape in EXPRESSION_SHAPES],
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
        """Answer one request as ``context``: who is asking, resolved from credentials.

        A transport passes the context it resolved; a trusted local caller passes its own or
        none. A request that says who is asking is refused, never stripped.
        """
        method = method.upper()
        payload = dict(payload or {})
        query_params = dict(query_params or {})
        remote = context is not None
        claimed = next((key for key in IDENTITY_PARAMS if key in query_params), None)
        if claimed is not None:
            raise identity_claim_refusal(claimed, remote=remote)
        refuse_identity_claims(payload, remote=remote)
        policy_context = context.to_policy_context() if context is not None else {}
        if method == "GET":
            return self._handle_get(
                route,
                query_params=query_params,
                headers=headers,
                context=context,
                policy_context=policy_context,
            )
        if method == "POST":
            return self._handle_post(route, payload, policy_context=policy_context)
        return self.not_found_payload(), 404

    def _seed(self, payload: Mapping[str, Any], policy_context: dict[str, Any]) -> Any:
        """A metadata route's ``query`` seed in the engine's form, with the caller's identity."""
        seed = _seed_call(payload)
        if not seed and not policy_context:
            return None
        return self.runtime.engine_seed({**seed, "policy_context": policy_context})

    def _handle_get(
        self,
        route: str,
        *,
        query_params: Mapping[str, Any],
        headers: Mapping[str, Any] | None,
        context: RequestContext | None,
        policy_context: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        if route == "/health":
            return self.health_payload(), 200
        if route == "/ready":
            payload = self.ready_payload(headers)
            return payload, 200 if payload["ok"] else 503
        if route == "/capabilities":
            return self.capabilities_payload(context), 200
        if route == "/catalog":
            return {
                "ok": True,
                "package_id": self.package_id,
                "catalog": operations.run(
                    "catalog",
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
                "catalog": operations.run(
                    "catalog",
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
                **operations.run(
                    "discover",
                    self.runtime,
                    terms=str(payload.get("terms", "")),
                    kinds=coerce_string_list(payload.get("kinds"), field="kinds"),
                    partial_query=self._seed(payload, policy_context),
                    stage=str(payload.get("stage", "")),
                    verbosity=str(payload.get("verbosity", "compact")),
                    limit=coerce_int(payload.get("limit"), 10, field="limit", minimum=1),
                    enforce_scope=True,
                ),
            }, 200
        if route == "/inspect":
            return {
                "ok": True,
                **operations.run(
                    "inspect",
                    self.runtime,
                    object_id=str(payload.get("object_id", "")),
                    partial_query=self._seed(payload, policy_context),
                    verbosity=str(payload.get("verbosity", "compact")),
                ),
            }, 200
        if route == "/build-options":
            return {
                "ok": True,
                **operations.run(
                    "build-options",
                    self.runtime,
                    partial_query=self.runtime.engine_seed(
                        {**_seed_call(payload), "policy_context": policy_context}
                    ),
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
                **operations.run(
                    "plan",
                    self.runtime,
                    intent=payload.get("intent"),
                    # Plan is an intent envelope: the seed call is its query.
                    partial_query={**_seed_call(payload), "policy_context": policy_context},
                    now=call_options(payload).get("now"),
                    detail=str(payload.get("detail", "best") or "best"),
                    limit=coerce_int(payload.get("limit"), 3, field="limit", minimum=1),
                ),
            }, 200
        if route in {"/validate", "/compile", "/query"}:
            call, options = call_arguments(payload)
            answer = operations.run(
                route.lstrip("/"), self.runtime, query=call, context=policy_context, **options
            )
            return {"ok": True, **answer}, 200
        if route == "/segment-validate":
            return {
                "ok": True,
                **operations.run(
                    "segment-validate",
                    self.runtime,
                    segment_id=str(payload.get("segment_id", "")),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/segment-explain":
            return {
                "ok": True,
                **operations.run(
                    "segment-explain",
                    self.runtime,
                    segment_id=str(payload.get("segment_id", "")),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/segment-preview":
            return {
                "ok": True,
                **operations.run(
                    "segment-preview",
                    self.runtime,
                    segment_id=str(payload.get("segment_id", "")),
                    limit=int(payload.get("limit", 50)),
                    policy_context=policy_context,
                ),
            }, 200
        if route == "/valid-values":
            return {
                "ok": True,
                **operations.run(
                    "valid-values",
                    self.runtime,
                    dimension_id=str(payload.get("dimension_id", "")),
                    query=self._seed(payload, policy_context),
                    search=str(payload.get("search", "")),
                    limit=coerce_int(payload.get("limit"), 100, field="limit", minimum=1),
                    offset=coerce_int(payload.get("offset"), 0, field="offset", minimum=0),
                    include_counts=coerce_bool(payload.get("include_counts"), False),
                    allow_live_query=coerce_bool(payload.get("allow_live_query"), False),
                ),
            }, 200
        return self.not_found_payload(), 404
