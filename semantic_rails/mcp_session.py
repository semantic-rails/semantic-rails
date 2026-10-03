"""Bounded, advisory request history owned by one query MCP session."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

_RESPONSE_OPTIONS = frozenset({"max_rows", "verbosity", "request_id"})


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
    request_id: str
    ran: tuple[str, str, int] | None = None


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
        with self._lock:
            previous = self._requests.get(key)
            if previous is not None:
                response["same_as"] = previous.request_id
                self._requests.move_to_end(key)
            else:
                previous = _Request(request_id)
                self._requests[key] = previous
                if len(self._requests) > 64:
                    self._requests.popitem(last=False)
            if tool != "execute" or query_key is None:
                return
            if mode in {"validate", "sql"}:
                for (owner, _, _), entry in self._requests.items():
                    if owner is adapter and entry.ran is not None and entry.ran[0] == query_key:
                        response["already_ran"] = {
                            "request_id": entry.ran[1],
                            "row_count": entry.ran[2],
                        }
                        break
            elif mode == "run" and response.get("ok") is True and previous.ran is None:
                row_count = response.get("row_count")
                if type(row_count) is int and row_count >= 0:
                    previous.ran = (query_key, request_id, row_count)
