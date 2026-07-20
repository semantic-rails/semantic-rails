"""Threaded ``http.server``-based HTTP front end for the v1 API.

Wraps :class:`semantic_rails.http_core.SemanticHTTPService` in a
``ThreadingHTTPServer`` request handler so concurrent agent calls do
not serialise on a single socket. The ASGI app
(:mod:`semantic_rails.asgi`) is the recommended deployment surface;
this module is the stdlib-only ``serve()`` used by the CLI and local
quickstart.
"""

from __future__ import annotations

import json
import time

# Use ThreadingHTTPServer so concurrent agent calls do not serialize
# through a single socket. Runtime caches are already thread-safe.
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .http_core import (
    API_VERSION,
    CORS_ALLOW_HEADERS,
    MAX_REQUEST_BODY_BYTES,
    PUBLIC_V1_ROUTES,
    SemanticHTTPService,
    clean_request_id,
    cors_origin_header,
    header_value,
    public_api_route,
    request_id_from_parts,
)
from .request_context import emit_audit_event, warn_if_default_policy_resolver_exposed
from .runtime import Runtime

__all__ = ["API_VERSION", "CORS_ALLOW_HEADERS", "PUBLIC_V1_ROUTES", "AppState", "Handler", "serve"]


class AppState:
    def __init__(self, package_id: str, *, path: str = ""):
        self.runtime = Runtime.from_path(path) if path else Runtime(package_id)
        self.package_id = self.runtime.package_id


class Handler(BaseHTTPRequestHandler):
    state: AppState

    def _service(self) -> SemanticHTTPService:
        return SemanticHTTPService(self.state.runtime, self.state.package_id)

    def _public_route(self) -> str | None:
        return public_api_route(urlparse(self.path).path)

    def _query_params(self) -> dict[str, Any]:
        parsed = parse_qs(urlparse(self.path).query)
        return {key: values[-1] for key, values in parsed.items() if values}

    def _request_headers(self) -> dict[str, str]:
        return {str(key): str(value) for key, value in self.headers.items()}

    def _prepare_request(self, payload: dict[str, Any] | None = None) -> None:
        if not hasattr(self, "_request_started_at"):
            self._request_started_at = time.perf_counter()
        self._api_version = API_VERSION
        headers = self._request_headers()
        query_params = self._query_params()
        incoming = (
            clean_request_id(header_value(headers, "X-Request-ID"))
            or clean_request_id(header_value(headers, "X-Correlation-ID"))
            or clean_request_id(query_params.get("request_id"))
        )
        if payload is not None:
            incoming = incoming or clean_request_id(payload.get("request_id"))
        if incoming:
            self._request_id = incoming
            self._request_id_source = "client"
        elif not hasattr(self, "_request_id"):
            self._request_id = request_id_from_parts(headers, query_params)
            self._request_id_source = "generated"
        self._request_context = self._service().request_context(
            headers,
            payload=payload or {},
            query_params=query_params,
            request_id=self._request_id,
        )

    def _auth_ok(self, route: str) -> bool:
        return self._service().auth_ok(route, self._request_headers())

    def _auth_error(self) -> None:
        _json(self, 401, self._service().unauthorized_payload())

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._prepare_request()
        if self._public_route() is None:
            return _json(self, 404, self._service().not_found_payload())
        self.send_response(204)
        allow_origin = cors_origin_header(self.headers.get("Origin"))
        if allow_origin:
            self.send_header("Access-Control-Allow-Origin", allow_origin)
        self.send_header("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS)
        self.send_header("Access-Control-Expose-Headers", "X-Request-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-Request-ID", str(getattr(self, "_request_id", "")))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._prepare_request()
        route = self._public_route()
        if route is None:
            return _json(self, 404, self._service().not_found_payload())
        if not self._auth_ok(route):
            return self._auth_error()
        service = self._service()
        try:
            payload, status = service.handle(
                "GET",
                route,
                headers=self._request_headers(),
                query_params=self._query_params(),
                context=getattr(self, "_request_context", None),
            )
        except Exception as exc:  # pragma: no cover - defensive HTTP guardrail
            payload, status = service.exception_payload(exc, stage="http")
        return _json(self, status, payload)

    def do_POST(self) -> None:  # noqa: N802
        self._prepare_request()
        route = self._public_route()
        if route is None:
            return _json(self, 404, self._service().not_found_payload())
        if not self._auth_ok(route):
            return self._auth_error()
        service = self._service()
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return _json(
                self, 400, service.invalid_request_payload("Invalid Content-Length header.")
            )
        if length > MAX_REQUEST_BODY_BYTES:
            return _json(
                self,
                413,
                service.invalid_request_payload(
                    f"Request body exceeds the {MAX_REQUEST_BODY_BYTES // 1024} KiB limit."
                ),
            )
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            decoded = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return _json(self, 400, service.invalid_json_payload(exc.msg))
        if not isinstance(decoded, dict):
            return _json(
                self, 400, service.invalid_request_payload("Request body must be a JSON object.")
            )
        payload = dict(decoded)
        self._prepare_request(payload)
        try:
            result, status = service.handle(
                "POST",
                route,
                payload,
                headers=self._request_headers(),
                query_params=self._query_params(),
                context=getattr(self, "_request_context", None),
            )
        except Exception as exc:  # pragma: no cover - defensive HTTP guardrail
            result, status = service.exception_payload(exc, stage="http")
        return _json(self, status, result)


def _json(handler: Handler, status: int, payload: dict[str, Any]) -> None:
    service = handler._service()
    envelope = service.envelope(
        status,
        payload,
        request_id=str(getattr(handler, "_request_id", "")),
        started=getattr(handler, "_request_started_at", None),
        request_context=getattr(handler, "_request_context", None),
    )
    # Pretty-printing is opt-in via `?pretty=true`. The default is
    # compact JSON to halve bandwidth on large catalog and discover
    # payloads in a hosted deployment.
    pretty = str(handler._query_params().get("pretty", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if pretty:
        body = json.dumps(envelope, indent=2, sort_keys=True, default=str).encode("utf-8")
    else:
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str).encode(
            "utf-8"
        )
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    allow_origin = cors_origin_header(handler.headers.get("Origin"))
    if allow_origin:
        handler.send_header("Access-Control-Allow-Origin", allow_origin)
    handler.send_header("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS)
    handler.send_header("Access-Control-Expose-Headers", "X-Request-ID")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("X-Request-ID", str(envelope.get("request_id", "")))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    emit_audit_event(
        "http_request",
        method=getattr(handler, "command", ""),
        path=urlparse(getattr(handler, "path", "")).path,
        status_code=status,
        request_id=str(envelope.get("request_id", "")),
        package_id=str(envelope.get("package_id", "")),
        request_context=dict(envelope.get("request_context", {}) or {}),
        error_codes=[
            str(error.get("code", ""))
            for error in list(envelope.get("errors", []) or [])
            if isinstance(error, dict)
        ],
        timing_ms=envelope.get("timing_ms"),
    )


def serve(package_id: str, host: str = "127.0.0.1", port: int = 8090, *, path: str = "") -> None:
    state = AppState(package_id, path=path)

    class _H(Handler):
        pass

    _H.state = state
    httpd = HTTPServer((host, port), _H)
    warn_if_default_policy_resolver_exposed(host, transport="semantic-rails HTTP API")
    source = path or package_id
    print(
        f"semantic-rails server running on http://{host}:{port} package={state.package_id} source={source}"
    )
    httpd.serve_forever()
