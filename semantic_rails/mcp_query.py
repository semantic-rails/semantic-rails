"""The one place a tool's arguments are read: the call in ``query``, never an identity."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .errors import SemanticLayerError
from .query_call import FIELDS, refusal
from .request_payload import refuse_identity_claims


def coerce_arguments(arguments: Mapping[str, Any], *, tool: str, remote: bool) -> dict[str, Any]:
    """A tool's arguments with ``query`` as an object.

    A ``query`` sent as a JSON string is parsed. An identity claim is refused (``remote``: the
    transport resolved the caller from credentials), and so is a call ``execute`` received
    as its own arguments: the call travels in ``query``, the options beside it.
    """
    out = dict(arguments)
    query = out.get("query")
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except (ValueError, RecursionError):
            query = None
        if not isinstance(query, dict):
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "query must encode a JSON object.",
                details={"field": "query", "argument_type": "str"},
            )
        out["query"] = query
    refuse_identity_claims(out, remote=remote)
    if tool == "execute" and "query" not in out:
        flat = next((key for key in out if key in FIELDS), None)
        if flat is not None:
            raise refusal(
                flat,
                "is a key of the query call, not an argument of execute",
                fix=f'Send the call as the query argument: {{"query": {{"{flat}": ...}}}}.',
            )
    return out
