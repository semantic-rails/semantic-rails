"""Bounded, advisory request history owned by one query MCP session."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

_RESPONSE_OPTIONS = frozenset({"verbosity", "request_id"})


def _fingerprint(payload: Mapping[str, Any]) -> str | None:
    # Strip response options only at the argument/query envelope, never from
    # filter values or expressions where these names can be meaningful data.
    clean = {key: value for key, value in payload.items() if key not in _RESPONSE_OPTIONS}
    if isinstance(clean.get("query"), Mapping):
        clean["query"] = {
            key: value for key, value in clean["query"].items() if key not in _RESPONSE_OPTIONS
        }
    try:
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        # Non-JSON in-process inputs cannot safely match a previous request.
        return None
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass
class _Request:
    reference: str | dict[str, Any]
    ran: tuple[str, dict[str, Any]] | None = None


def _run_summary(response: Mapping[str, Any]) -> dict[str, Any] | None:
    row_count = response.get("row_count")
    if type(row_count) is not int or row_count < 0:
        return None
    summary = {"request_id": response["request_id"], "row_count": row_count}
    if response.get("truncated") is True:
        # The warning reports the effective cap, including query-authored limits.
        for warning in response.get("warnings") or []:
            if not isinstance(warning, Mapping) or warning.get("code") != "EXECUTE_ROWS_TRUNCATED":
                continue
            details = warning.get("details")
            cap = details.get("max_rows") if isinstance(details, Mapping) else None
            if type(cap) is int and cap > 0:
                summary.update(truncated=True, max_rows=cap)
                break
        else:
            # A custom handler without cap metadata cannot provide a safe hint.
            return None
    return summary


class MCPQuerySession:
    """Pass one instance through calls belonging to a single MCP session.

    Keeps 64 request fingerprints, not query arguments or answers. Every call
    still executes normally; hints are attached only after its final envelope.
    """

    def __init__(self) -> None:
        self._requests: OrderedDict[tuple[object, str, str], _Request] = OrderedDict()
        self._lock = Lock()

    def annotate(
        self,
        adapter: object,
        tool: str,
        arguments: Mapping[str, Any],
        response: dict[str, Any],
        *,
        query: Mapping[str, Any] | None = None,
    ) -> None:
        fingerprint = _fingerprint(arguments)
        request_id = response.get("request_id")
        if fingerprint is None or not isinstance(request_id, str) or not request_id:
            return
        key = (adapter, tool, fingerprint)
        query_key = _fingerprint(query) if query is not None else None
        mode = str(arguments.get("mode") or "run").strip().lower()
        summary = (
            _run_summary(response)
            if tool == "execute" and mode == "run" and response.get("ok") is True
            else None
        )
        if response.get("truncated") is True and summary is None:
            return
        with self._lock:
            previous = self._requests.get(key)
            if previous is not None:
                reference = previous.reference
                response["same_as"] = dict(reference) if isinstance(reference, dict) else reference
                self._requests.move_to_end(key)
            else:
                previous = _Request(summary if summary and summary.get("truncated") else request_id)
                self._requests[key] = previous
                if len(self._requests) > 64:
                    self._requests.popitem(last=False)
            if tool != "execute" or query_key is None:
                return
            if mode in {"validate", "sql"}:
                for (owner, _, _), entry in self._requests.items():
                    if owner is adapter and entry.ran is not None and entry.ran[0] == query_key:
                        response["already_ran"] = dict(entry.ran[1])
                        break
            elif summary is not None:
                # Exactly one retained success per query; LRU touches and failed
                # runs must never make an older answer look like the latest one.
                for (owner, _, _), entry in self._requests.items():
                    if owner is adapter and entry.ran is not None and entry.ran[0] == query_key:
                        entry.ran = None
                previous.ran = (query_key, summary)
