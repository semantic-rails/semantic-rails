"""Audit events and the pluggable sink they go to.

:func:`emit_audit_event` is the single hook every governed read and write funnels
through. It is off unless ``SEMANTIC_RAILS_AUDIT_LOGS`` is set, drops secret-bearing
keys, and hands the event to the process-wide :class:`AuditSink` (by default
:class:`StderrAuditSink`), which hosts replace with :func:`set_audit_sink`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Protocol, runtime_checkable


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
