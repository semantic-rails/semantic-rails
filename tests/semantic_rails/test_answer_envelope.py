"""The answer envelope (``semantic_rails/answer.py``): which fields an answer carries and when.

``observation_scope`` always; ``window`` and ``clock`` when they apply; ``routes``,
``assumptions`` and ``warnings`` only when they have entries; no empty fields. Verbosity trims
only afterwards and never removes a warning, an assumption or a route. The echoed query is the
call, never the engine form with its identity and clock.
"""

from __future__ import annotations

from typing import Any

import pytest

import semantic_rails.runtime_parts.answer_warnings as answer_warnings_module
from semantic_rails.issues import ASSUMPTION, CODES
from semantic_rails.mcp import SemanticLayerMCPAdapter

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
PRODUCT = "dimension.jaffle_item_product_name"
NOW = "2017-08-15"
VERBOSITIES = ("minimal", "compact", "full")
ENTRIES = ("query", "validate", "compile", "mcp")

# Item revenue by product: one route, nothing assumed, nothing to warn about.
PLAIN = {
    "select": ["measure.jaffle.item_revenue_usd"],
    "group_by": [PRODUCT],
    "time": {
        "temporal_role": ORDER_TIME,
        "grain": "year",
        "start": "2017-01-01",
        "end": "2018-01-01",
    },
    "order_by": [{"field": "item_revenue_usd", "direction": "desc"}],
}
# Revenue by store this quarter, as one total: an order reaches its store by its own key.
STORES_THIS_QUARTER = {
    "select": ["measure.jaffle.revenue_usd"],
    "group_by": [STORE],
    "time": {"temporal_role": ORDER_TIME, "this": "quarter"},
}
OWN_KEY_ROUTE = {
    "from": "entity.jaffle_order",
    "to": "entity.jaffle_store",
    "via": ["relationship.orders_store"],
}


@pytest.fixture
def runtime(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


def _answer(runtime, entry: str, query: dict[str, Any], verbosity: str) -> dict[str, Any]:
    if entry == "mcp":
        adapter = SemanticLayerMCPAdapter(runtime)
        return adapter.call_tool("execute", {"query": query, "now": NOW, "verbosity": verbosity})
    return getattr(runtime, entry)(query, now=NOW, verbosity=verbosity)


@pytest.mark.parametrize("verbosity", VERBOSITIES)
@pytest.mark.parametrize("entry", ENTRIES)
def test_an_answer_with_nothing_to_say_carries_no_empty_fields(runtime, entry, verbosity):
    out = _answer(runtime, entry, PLAIN, verbosity)
    assert out["ok"] is True and out["status"] == "ok"
    assert out["observation_scope"] == "dataset"
    assert out["clock"] == ORDER_TIME
    assert out["window"]["start"] == "2017-01-01" and out["window"]["now"].startswith(NOW)
    for absent in ("routes", "assumptions", "warnings", "errors", "recovery_hints"):
        assert absent not in out, absent
    assert not [key for key, value in out.items() if value in (None, "", [], {}) and key != "rows"]


@pytest.mark.parametrize("verbosity", VERBOSITIES)
@pytest.mark.parametrize("entry", ENTRIES)
def test_routes_and_assumptions_survive_every_verbosity(runtime, entry, verbosity):
    out = _answer(runtime, entry, STORES_THIS_QUARTER, verbosity)
    assert out["ok"] is True
    (route,) = out["routes"]
    assert {key: route[key] for key in ("from", "to", "via")} == OWN_KEY_ROUTE
    # How the route was chosen is detail, trimmed from the minimal response.
    assert ("basis" in route) is (verbosity != "minimal")
    codes = [row["code"] for row in out["assumptions"]]
    assert codes == ["WINDOW_TOTAL", "PARTIAL_PERIOD"]
    assert out["assumptions"][1]["message"] == "Q3 2017 so far (46 of 92 days)"


@pytest.mark.parametrize("verbosity", VERBOSITIES)
def test_a_disclosure_registered_as_an_assumption_is_stated_under_assumptions(
    runtime, monkeypatch, verbosity
):
    """One registry entry is all a new disclosure needs: written as a warning anywhere, it is
    stated under ``assumptions`` as {code, message, **details}, at every verbosity."""
    monkeypatch.setitem(CODES, "TEST_DISCLOSURE", ASSUMPTION)
    disclosure = {
        "code": "TEST_DISCLOSURE",
        "message": "median over 1,234 orders",
        "severity": "info",
        "details": {"measure": "measure.jaffle.revenue_usd", "rows": 1234},
    }
    monkeypatch.setattr(
        answer_warnings_module, "_compiled_warnings", lambda *args, **kwargs: [disclosure]
    )
    out = runtime.query(PLAIN, now=NOW, verbosity=verbosity)
    assert out["assumptions"] == [
        {
            "code": "TEST_DISCLOSURE",
            "message": "median over 1,234 orders",
            "measure": "measure.jaffle.revenue_usd",
            "rows": 1234,
        }
    ]
    assert "warnings" not in out


@pytest.mark.parametrize("entry", ("query", "validate", "compile"))
def test_the_echoed_query_is_the_call(runtime, entry):
    call = {**PLAIN, "order_by": [{"field": "item_revenue_usd", "direction": "desc"}]}
    out = getattr(runtime, entry)(call, now=NOW, verbosity="compact")
    for echo in (out["query"], out["normalized_query"]):
        assert "policy_context" not in echo and "now" not in echo
        assert echo["select"] == ["measure.jaffle.item_revenue_usd"]
        assert echo["order_by"] == [{"field": "item_revenue_usd", "direction": "desc"}]
    assert "query" not in runtime.query(call, now=NOW, verbosity="minimal")


# --- Error and warning objects ------------------------------------------------------------

REFUSED = {
    "unknown id": {"select": ["measure.jaffle.zz_unknown"]},
    "filter word": {
        "select": ["measure.jaffle.order_count"],
        "where": [{"field": STORE, "op": "IS NULL", "value": True}],
    },
    "unknown grouping": {"select": ["measure.jaffle.order_count"], "group_by": ["dimension.zz"]},
}
# Revenue by store by month: a caveat and a no-data-yet warning.
WARNED = {
    "select": ["measure.jaffle.revenue_usd"],
    "group_by": [STORE],
    "time": {
        "temporal_role": ORDER_TIME,
        "grain": "month",
        "start": "2017-01-01",
        "end": "2018-01-01",
    },
}


def _issues(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Every error and warning object in a response, and each one's recovery hints."""
    found = [
        *(response.get("errors") or []),
        *(response.get("warnings") or []),
        *([response["error"]] if isinstance(response.get("error"), dict) else []),
    ]
    return [*found, *(hint for issue in found for hint in issue.get("recovery_hints") or [])]


def _responses(runtime, call: dict[str, Any], verbosity: str) -> dict[str, dict[str, Any]]:
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.http_core import SemanticHTTPService

    service = SemanticHTTPService(runtime)
    adapter = SemanticLayerMCPAdapter(runtime)
    out = {
        "sdk": runtime.validate(call, now=NOW, verbosity=verbosity),
        "mcp_validate": adapter.call_tool(
            "execute", {"query": call, "now": NOW, "mode": "validate", "verbosity": verbosity}
        ),
        "mcp_run": adapter.call_tool(
            "execute", {"query": call, "now": NOW, "verbosity": verbosity}
        ),
    }
    for route in ("/validate", "/query"):
        body = {"query": call, "options": {"now": NOW, "verbosity": verbosity}}
        try:
            payload, status = service.handle("POST", route, body)
        except SemanticLayerError as exc:
            payload, status = service.exception_payload(exc, stage="http")
        out[f"http{route}"] = service.envelope(status, payload, request_id="r")
    return out


@pytest.mark.parametrize("verbosity", ("minimal", "compact"))
@pytest.mark.parametrize("call", [*REFUSED, "warned"])
def test_error_and_warning_objects_carry_no_empty_fields(runtime, call, verbosity):
    """Like the answer, each error and warning object leaves out empty fields, on every
    transport and at every verbosity; its code and message always stay."""
    for transport, response in _responses(runtime, REFUSED.get(call, WARNED), verbosity).items():
        issues = _issues(response)
        assert issues, (transport, response)
        for issue in issues:
            empty = [key for key, value in issue.items() if value in (None, "", [], {})]
            assert not empty, (transport, issue.get("code"), empty)
            if "kind" not in issue:  # a recovery hint is named by its kind
                assert issue.get("code") and issue.get("message"), (transport, issue)
