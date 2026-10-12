"""A call is read in the caller's visible view: a hidden id is refused exactly like an unknown one.

The jaffle package hides ``measure.jaffle.lifetime_spend_before_tax_usd`` from the ``ops``
audience. Before the call reached the caller's view, an invalid aggregation override on the
hidden measure was refused with the measure's allowed and default aggregations, while an
invented id was refused as unknown: a disclosure and an existence oracle.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

HIDDEN = "measure.jaffle.lifetime_spend_before_tax_usd"
# Unknown, and a near twin of the hidden id, so both get the same name suggestions.
INVENTED = "measure.jaffle.lifetime_spend_before_tax_usx"
OPS = RequestContext(audience="ops")


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(path))
    yield opened(runtime)
    runtime.close()


def _call(measure: str, aggregation: str | None) -> dict[str, Any]:
    expression: dict[str, Any] = {"kind": "measure", "measure": measure}
    if aggregation is not None:
        expression["aggregation"] = aggregation
    return {"select": [{"expression": expression, "as": "v"}]}


def _refusal(runtime, entry: str, call: dict[str, Any]) -> dict[str, Any]:
    if entry == "validate":
        (error,) = runtime.validate(call, context=OPS)["errors"]
        return dict(error)
    if entry in {"compile", "query"}:
        with pytest.raises(SemanticLayerError) as raised:
            getattr(runtime, entry)(call, context=OPS)
        return {"code": raised.value.code, "message": str(raised.value), **raised.value.details}
    if entry.startswith("mcp_"):
        mode = entry.removeprefix("mcp_")
        out = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute", {"query": call, "mode": mode}, request_context=OPS
        )
        (error,) = out["errors"]
        return dict(error)
    route = "/" + entry.removeprefix("http_")
    service = SemanticHTTPService(runtime)
    try:
        response, _status = service.handle("POST", route, {"query": call}, context=OPS)
    except SemanticLayerError as exc:
        response, _status = service.exception_payload(exc, stage="http", context=OPS)
    (error,) = response["errors"]
    return dict(error)


def _shape(refusal: dict[str, Any], measure: str) -> str:
    """The refusal with the id written as a placeholder."""
    return json.dumps(refusal, sort_keys=True, default=str).replace(measure, "<id>")


@pytest.mark.parametrize(
    "entry",
    [
        "validate",
        "compile",
        "query",
        "mcp_validate",
        "mcp_sql",
        "mcp_run",
        "http_validate",
        "http_compile",
        "http_query",
    ],
)
def test_a_hidden_measure_is_refused_like_an_unknown_one(runtime, entry):
    for aggregation in ("bogus", "median", None):
        hidden = _refusal(runtime, entry, _call(HIDDEN, aggregation))
        invented = _refusal(runtime, entry, _call(INVENTED, aggregation))
        assert hidden["code"] == invented["code"] == "OBJECT_NOT_FOUND"
        assert _shape(hidden, HIDDEN) == _shape(invented, INVENTED)
        # Nothing of the hidden measure's definition is disclosed.
        serialized = json.dumps(hidden, default=str)
        for leak in ('"allowed"', '"default"', "percentile", "UNSUPPORTED_AGGREGATION"):
            assert leak not in serialized


# --- Identity claims anywhere in a request ---------------------------------------------------


@pytest.mark.parametrize(
    ("call_extra", "path"),
    [
        ({"_note": {"policy_context": {"audience": "internal"}}}, "_note.policy_context"),
        ({"_trace": [{"policy_context": {}}]}, "_trace[0].policy_context"),
        ({"version": {"policy_context": {}}}, "version.policy_context"),
    ],
)
def test_a_claim_inside_an_annotation_is_refused_at_its_path(runtime, call_extra, path):
    call = {"select": ["measure.jaffle.order_count"], **call_extra}
    # Behind credentials (MCP with a resolved caller): IDENTITY_CLAIM_REFUSED at the path.
    out = SemanticLayerMCPAdapter(runtime).call_tool(
        "execute", {"query": call, "mode": "validate"}, request_context=OPS
    )
    (error,) = out["errors"]
    assert (error["code"], error["details"]["path"]) == ("IDENTITY_CLAIM_REFUSED", f"query.{path}")
    # A trusted local caller: the key is refused like any key that is not part of a call.
    (error,) = runtime.validate(call)["errors"]
    assert error["code"] == "INVALID_QUERY" and error["details"]["path"] == path
