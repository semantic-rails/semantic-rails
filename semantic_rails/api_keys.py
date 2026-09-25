"""API-key auth for the HTTP, ASGI, and MCP transports.

Reads the configured keys (:func:`configured_api_keys`), pulls the
supplied key from the request headers (:func:`extract_bearer_or_api_key`),
and checks it in constant time (:func:`api_key_auth_result`,
``hmac.compare_digest`` for free). Also holds the case-insensitive header
lookup that :mod:`semantic_rails.request_context` shares.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Any

MISSING_API_KEY_FILE_SENTINEL = "__missing_api_key_file__"


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
