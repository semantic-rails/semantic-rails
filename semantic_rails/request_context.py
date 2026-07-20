"""Request context, API-key auth, and the audit sink protocol.

Defines :class:`RequestContext` (actor, tenant, project, roles,
environment, audience, request_id) and the helpers that resolve it
from headers, JSON payloads, or a pluggable
:class:`PolicyContextResolver`. Also owns the constant-time API-key
check (:func:`api_key_auth_result`, ``hmac.compare_digest`` for free)
and :func:`emit_audit_event` — the single hook every governed
write/read funnels through so hosts can plug in an :class:`AuditSink`.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

CONTEXT_FIELDS = ("actor", "tenant", "project", "roles", "environment", "audience")
MISSING_API_KEY_FILE_SENTINEL = "__missing_api_key_file__"


@dataclass(frozen=True)
class RequestContext:
    request_id: str = ""
    actor: str = ""
    tenant: str = ""
    project: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)
    environment: str = ""
    audience: str = ""

    def to_policy_context(self) -> dict[str, Any]:
        payload = {
            "actor": self.actor,
            "tenant": self.tenant,
            "project": self.project,
            "roles": list(self.roles),
            "environment": self.environment,
            "audience": self.audience,
        }
        return {key: value for key, value in payload.items() if value not in ("", [], None)}

    def to_public_dict(self) -> dict[str, Any]:
        payload = {"request_id": self.request_id, **self.to_policy_context()}
        return {key: value for key, value in payload.items() if value not in ("", [], None)}


def _split_roles(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        roles = [str(item).strip() for item in value]
    else:
        roles = [part.strip() for part in str(value or "").replace(";", ",").split(",")]
    return tuple(dict.fromkeys(role for role in roles if role))


def _header(headers: Mapping[str, Any] | None, *names: str) -> str:
    if headers is None:
        return ""
    for name in names:
        try:
            value = headers.get(name)
        except AttributeError:
            value = None
        if value:
            return str(value).strip()
    lower = {str(key).lower(): value for key, value in dict(headers or {}).items()}
    for name in names:
        value = lower.get(name.lower())
        if value:
            return str(value).strip()
    return ""


def context_from_policy_context(
    policy_context: Mapping[str, Any] | None, *, request_id: str = ""
) -> RequestContext:
    raw = dict(policy_context or {})
    return RequestContext(
        request_id=str(request_id or raw.get("request_id", "") or "").strip(),
        actor=str(raw.get("actor", raw.get("user", "")) or "").strip(),
        tenant=str(raw.get("tenant", raw.get("tenant_id", "")) or "").strip(),
        project=str(raw.get("project", raw.get("project_id", "")) or "").strip(),
        roles=_split_roles(raw.get("roles", raw.get("role", ""))),
        environment=str(raw.get("environment", "") or "").strip(),
        audience=str(raw.get("audience", "") or "").strip(),
    )


def context_from_headers(
    headers: Mapping[str, Any] | None,
    *,
    payload: Mapping[str, Any] | None = None,
    request_id: str = "",
) -> RequestContext:
    body_context = context_from_policy_context(
        dict((payload or {}).get("policy_context", {}) or {}), request_id=request_id
    )
    return RequestContext(
        request_id=request_id,
        actor=_header(headers, "X-Semantic-Actor", "X-Actor", "X-User", "X-User-ID")
        or body_context.actor,
        tenant=_header(headers, "X-Semantic-Tenant", "X-Tenant", "X-Tenant-ID")
        or body_context.tenant,
        project=_header(headers, "X-Semantic-Project", "X-Project", "X-Project-ID")
        or body_context.project,
        roles=_split_roles(_header(headers, "X-Semantic-Roles", "X-Roles", "X-Role"))
        or body_context.roles,
        environment=_header(headers, "X-Semantic-Environment", "X-Environment")
        or body_context.environment,
        audience=_header(headers, "X-Semantic-Audience", "X-Audience") or body_context.audience,
    )


@runtime_checkable
class PolicyContextResolver(Protocol):
    """Pluggable contract for deriving the trusted policy context for a request.

    The semantic runtime treats `policy_context.audience`, `policy_context.environment`,
    `policy_context.roles`, and `policy_context.tenant` as **trusted upstream context**.
    A policy declared with `audiences: [finance]` only matches when the resolved
    context carries `audience=finance`. If a caller can self-assert these fields,
    they can bypass visibility, redaction, and MNPI-style policy gating.

    The default `HeaderPolicyContextResolver` reads these fields from request
    headers (and falls back to the body's `policy_context`). That is safe for
    local development and single-tenant trusted-network deployments where the
    upstream caller is the operator.

    **Hosted deployments MUST replace this resolver** with one that derives
    audience / environment / tenant from an authenticated identity (JWT,
    mTLS cert, signed session, etc.) — not from arbitrary client headers
    or request payload. The replacement is the integration seam: swap the
    resolver instance, leave the policy engine and Query IR untouched.

    See `docs/QUERY_API.md` "Trusted Upstream Context" for the contract.
    """

    def resolve(
        self,
        headers: Mapping[str, Any] | None,
        *,
        payload: Mapping[str, Any] | None = None,
        request_id: str = "",
    ) -> RequestContext: ...


class HeaderPolicyContextResolver:
    """Default resolver. Reads policy context from request headers + payload.

    Suitable for local dev, single-tenant on-prem, and any deployment where
    the caller is trusted. **Not safe** as-is for multi-tenant hosted use —
    callers can self-assert any audience or environment value.
    """

    def resolve(
        self,
        headers: Mapping[str, Any] | None,
        *,
        payload: Mapping[str, Any] | None = None,
        request_id: str = "",
    ) -> RequestContext:
        return context_from_headers(headers, payload=payload, request_id=request_id)


_default_resolver: PolicyContextResolver = HeaderPolicyContextResolver()


def get_policy_context_resolver() -> PolicyContextResolver:
    """Return the active `PolicyContextResolver`. Hosted deployments override
    via `set_policy_context_resolver(...)` at startup."""
    return _default_resolver


def set_policy_context_resolver(resolver: PolicyContextResolver) -> None:
    """Install a `PolicyContextResolver` for the current process.

    Hosted deployments call this once at startup with an
    identity-aware resolver. The default reads from headers and is
    insecure in multi-tenant contexts (see `PolicyContextResolver`).
    """
    global _default_resolver
    if not hasattr(resolver, "resolve"):
        raise TypeError(
            "PolicyContextResolver must implement .resolve(headers, *, payload, request_id)"
        )
    _default_resolver = resolver


def loopback_host(host: str) -> bool:
    text = str(host or "").strip().lower().strip("[]")
    return text in {"localhost", "::1"} or text.startswith("127.")


def warn_if_default_policy_resolver_exposed(host: str, *, transport: str) -> bool:
    """Print a hard warning when binding beyond loopback with the default resolver.

    The default ``HeaderPolicyContextResolver`` trusts caller-supplied
    ``X-Semantic-Audience`` / ``X-Semantic-Environment`` headers and body
    ``policy_context``, so any remote caller can self-assert the context
    that object visibility and access policies key on. Binding to a
    non-loopback interface without replacing the resolver is the footgun
    this warning exists for. Returns True when the warning fired.
    """
    if loopback_host(host):
        return False
    if type(get_policy_context_resolver()) is not HeaderPolicyContextResolver:
        return False
    auth_note = (
        " API-key auth is enabled, but key holders can still self-assert policy context."
        if configured_api_keys()
        else " No API keys are configured, so this applies to any remote caller."
    )
    print(
        f"WARNING: {transport} is binding to {host or 'all interfaces'!r} with the default "
        "header-trusting policy-context resolver — callers can self-assert policy_context "
        f"fields (audience, environment, roles, tenant) and bypass object policies.{auth_note} "
        "For hosted or multi-tenant deployments, install an identity-aware resolver via "
        "set_policy_context_resolver(...) before serving traffic. "
        "See docs/QUERY_API.md (trusted upstream context).",
        file=sys.stderr,
        flush=True,
    )
    return True


def merge_policy_context(
    payload: Mapping[str, Any] | None, context: RequestContext | None
) -> dict[str, Any]:
    policy_context = dict((payload or {}).get("policy_context", {}) or {})
    if context is None:
        return policy_context
    for key, value in context.to_policy_context().items():
        policy_context[key] = value
    return policy_context


def configured_api_keys() -> tuple[str, ...]:
    values: list[str] = []
    raw = os.environ.get("SEMANTIC_RAILS_API_KEYS", "")
    values.extend(part.strip() for part in raw.replace("\n", ",").split(","))
    filename = os.environ.get("SEMANTIC_RAILS_API_KEY_FILE", "")
    if filename:
        try:
            with open(filename, encoding="utf-8") as handle:
                values.extend(part.strip() for part in handle.read().replace("\n", ",").split(","))
        except FileNotFoundError:
            values.append(MISSING_API_KEY_FILE_SENTINEL)
    return tuple(dict.fromkeys(value for value in values if value))


def extract_bearer_or_api_key(headers: Mapping[str, Any] | None) -> str:
    auth = _header(headers, "Authorization")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return _header(headers, "X-API-Key", "X-Semantic-API-Key")


def api_key_auth_result(headers: Mapping[str, Any] | None) -> tuple[bool, str]:
    keys = configured_api_keys()
    if not keys:
        return True, "disabled"
    supplied = extract_bearer_or_api_key(headers)
    if not supplied or supplied == MISSING_API_KEY_FILE_SENTINEL:
        return False, "missing_or_invalid"
    # Timing-safe comparison: walk the full key list and OR-accumulate
    # `hmac.compare_digest` results so the lookup cost does not depend on
    # the supplied prefix matching an early key. Always compare against the
    # supplied value (not the configured one) so length differences don't
    # short-circuit on bytes-level equality. This matters once the runtime
    # is fronted by a hosted API where remote attackers can time requests.
    supplied_bytes = supplied.encode("utf-8")
    matched = False
    for key in keys:
        if key == MISSING_API_KEY_FILE_SENTINEL:
            continue
        if hmac.compare_digest(supplied_bytes, key.encode("utf-8")):
            matched = True
    if matched:
        return True, "matched"
    return False, "missing_or_invalid"


def audit_logging_enabled() -> bool:
    return os.environ.get("SEMANTIC_RAILS_AUDIT_LOGS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_AUDIT_SECRET_KEYS = frozenset(
    {"authorization", "api_key", "password", "token", "secret", "private_key"}
)


@runtime_checkable
class AuditSink(Protocol):
    """Pluggable destination for audit events.

    The default `StderrAuditSink` writes JSON lines to stderr, suitable
    for local dev and container deployments that ship stderr to a log
    aggregator. Hosted deployments will typically want a structured sink
    (JSON over Kafka, OpenTelemetry, an HTTP collector, etc.). The
    protocol is the integration seam — implementations receive an
    already-scrubbed payload (no `authorization`, `api_key`, etc.).
    """

    def emit(self, payload: dict[str, Any]) -> None: ...


class StderrAuditSink:
    """Default `AuditSink`: write JSON-encoded events to stderr.

    Suitable for the OSS standalone experience and for container
    deployments where stderr is shipped to a log aggregator. Hosted
    operators replace this with a structured sink via
    `set_audit_sink(...)`.
    """

    def emit(self, payload: dict[str, Any]) -> None:
        print(json.dumps(payload, sort_keys=True, default=str), file=sys.stderr, flush=True)


_audit_sink: AuditSink = StderrAuditSink()


def get_audit_sink() -> AuditSink:
    """Return the active `AuditSink`."""
    return _audit_sink


def set_audit_sink(sink: AuditSink) -> None:
    """Install a process-wide `AuditSink`. Hosted deployments call this
    once at startup to route audit events into a structured pipeline.
    """
    global _audit_sink
    if not hasattr(sink, "emit"):
        raise TypeError("AuditSink must implement .emit(payload)")
    _audit_sink = sink


def emit_audit_event(event: str, **payload: Any) -> None:
    if not audit_logging_enabled():
        return
    safe_payload = {
        "event": event,
        "ts": round(time.time(), 3),
        **{key: value for key, value in payload.items() if key not in _AUDIT_SECRET_KEYS},
    }
    try:
        _audit_sink.emit(safe_payload)
    except Exception:  # noqa: BLE001 — never block a request on audit failure
        # Fall back to stderr so a misconfigured sink can't silence audit.
        print(
            json.dumps(safe_payload, sort_keys=True, default=str),
            file=sys.stderr,
            flush=True,
        )


def request_context_payload(context: RequestContext | Mapping[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    if isinstance(context, RequestContext):
        return context.to_public_dict()
    return context_from_policy_context(context).to_public_dict()


def dataclass_payload(context: RequestContext) -> dict[str, Any]:
    payload = asdict(context)
    payload["roles"] = list(context.roles)
    return payload
