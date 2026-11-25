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
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
PRODUCT = "dimension.jaffle_item_product_name"
NOW = "2017-08-15"
VERBOSITIES = ("minimal", "compact", "full")
# Each SDK entry once, and MCP, which trims its own response, at every verbosity.
ENTRY_VERBOSITIES = [
    ("query", "minimal"),
    ("validate", "compact"),
    ("compile", "full"),
    *(("mcp", verbosity) for verbosity in VERBOSITIES),
]

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
def runtime(jaffle_snapshot):
    runtime = Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop")
    yield runtime
    runtime.close()


def _answer(runtime, entry: str, query: dict[str, Any], verbosity: str) -> dict[str, Any]:
    if entry == "mcp":
        adapter = SemanticLayerMCPAdapter(runtime)
        return adapter.call_tool("execute", {"query": query, "now": NOW, "verbosity": verbosity})
    return getattr(runtime, entry)(query, now=NOW, verbosity=verbosity)


@pytest.mark.parametrize(("entry", "verbosity"), ENTRY_VERBOSITIES)
def test_an_answer_with_nothing_to_say_carries_no_empty_fields(runtime, entry, verbosity):
    out = _answer(runtime, entry, PLAIN, verbosity)
    assert out["ok"] is True and out["status"] == "ok"
    assert out["observation_scope"] == "dataset"
    assert out["clock"] == ORDER_TIME
    assert out["window"]["start"] == "2017-01-01" and out["window"]["now"].startswith(NOW)
    for absent in ("routes", "assumptions", "warnings", "errors", "recovery_hints"):
        assert absent not in out, absent
    assert not [key for key, value in out.items() if value in (None, "", [], {}) and key != "rows"]


@pytest.mark.parametrize(("entry", "verbosity"), ENTRY_VERBOSITIES)
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


@pytest.mark.parametrize(
    ("period_start", "period_end", "start", "end"),
    [
        ("2017", "2018", "2017-01-01", "2018-01-01"),
        ("2017-06", "2017-07", "2017-06-01", "2017-07-01"),
        ("2024-02", "2024-03", "2024-02-01", "2024-03-01"),
        ("2025-Q4", "2026-Q1", "2025-10-01", "2026-01-01"),
        ("2025-W01", "2025-W02", "2024-12-30", "2025-01-06"),
        ("2025-W05", "2025-W06", "2025-01-27", "2025-02-03"),
        ("2020-W53", "2021-W01", "2020-12-28", "2021-01-04"),
        ("9999-12", "9999-W52", "9999-12-01", "9999-12-27"),
    ],
)
def test_period_literals_echo_explicit_bounds(runtime, period_start, period_end, start, end):
    call = {"select": ["measure.jaffle.order_count"]}
    short = runtime.validate({**call, "time": {"start": period_start, "end": period_end}}, now=NOW)
    explicit = runtime.validate({**call, "time": {"start": start, "end": end}}, now=NOW)
    assert short["ok"] and explicit["ok"]
    assert short["window"] == explicit["window"]
    assert (short["window"]["start"], short["window"]["end"]) == (start, end)


@pytest.mark.parametrize(
    ("bounds", "start", "end"),
    [
        ({"start": "2017-08"}, "2017-08-01", "2017-08-15T00:00:00Z"),
        ({"end": "2017-W30"}, None, "2017-07-24"),
        (
            {"start": "2017-07-31T12:00:00+00:00", "end": "2017-08"},
            "2017-07-31T12:00:00+00:00",
            "2017-08-01",
        ),
    ],
)
def test_period_bounds_work_alone_or_with_an_explicit_bound(runtime, bounds, start, end):
    call = {"select": ["measure.jaffle.order_count"], "time": bounds}
    window = runtime.query(call, now=NOW)["window"]
    assert (window.get("start"), window["end"]) == (start, end)


@pytest.mark.parametrize(
    ("period_end", "end"), [("2017-07", "2017-07-01"), ("2017-06", "2017-06-01")]
)
def test_nonpositive_period_windows_answer_as_the_same_explicit_dates(runtime, period_end, end):
    call = {"select": ["measure.jaffle.order_count"]}
    short = runtime.query({**call, "time": {"start": "2017-07", "end": period_end}}, now=NOW)
    explicit = runtime.query({**call, "time": {"start": "2017-07-01", "end": end}}, now=NOW)
    assert short["rows"] == explicit["rows"]
    assert short["window"] == explicit["window"]


def test_joined_answer_echoes_period_bounds_in_each_part(runtime):
    call = {
        "parts": [
            {"select": ["measure.jaffle.order_count"], "group_by": [STORE]},
            {"select": ["measure.jaffle.revenue_usd"], "group_by": [STORE]},
        ],
        "join": {"on": [[STORE, STORE]]},
        "time": {"start": "2017-08", "end": "2017-09"},
    }
    answer = runtime.query(call, now=NOW)
    assert answer["rows"]
    assert [(part["window"]["start"], part["window"]["end"]) for part in answer["parts"]] == [
        ("2017-08-01", "2017-09-01"),
        ("2017-08-01", "2017-09-01"),
    ]


@pytest.mark.parametrize(
    ("stamp", "equivalent"),
    [
        ("2017-08-07 12:00:00 UTC", "2017-08-07T12:00:00Z"),
        ("2017-08-07T24:00:00", "2017-08-08T00:00:00"),
        ("2017-08-07  12:00:00", "2017-08-07T12:00:00"),
        ("2017-8-7 12:00:00", "2017-08-07T12:00:00"),
        ("2017/08/07 12:00:00", "2017-08-07T12:00:00"),
    ],
)
def test_period_bound_support_preserves_explicit_warehouse_timestamps(runtime, stamp, equivalent):
    call = {"group_by": [STORE], "order_by": [{"field": STORE, "direction": "asc"}]}
    time = {"temporal_role": ORDER_TIME, "end": "2017-08-14"}
    out = runtime.query({**call, "time": {**time, "start": stamp}}, now=NOW)
    expected = runtime.query({**call, "time": {**time, "start": equivalent}}, now=NOW)
    assert out["rows"] and out["rows"] == expected["rows"]
    assert out["window"]["start"] == stamp


@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("start", "2025-Q5"),
        ("end", "2025-W53"),
        ("start", "2025-W54"),
        ("end", "2025-W5"),
        ("end", "2017-W32-03"),
        ("start", "2025W05"),
        ("start", "2025-13"),
        ("end", "0000"),
        ("start", "last 3 months"),
        ("end", "2017-08-07T12:00:00+00:60"),
    ],
)
def test_invalid_period_bounds_refuse_with_a_field_and_example(runtime, field, literal):
    report = runtime.validate(
        {"select": ["measure.jaffle.order_count"], "time": {field: literal}}, now=NOW
    )
    assert report["ok"] is False
    (error,) = report["errors"]
    assert error["code"] == "INVALID_QUERY"
    assert (
        error["message"]
        == f"time.{field}: Use a valid ISO date, date-time or period, such as '2025-03'."
    )
    assert error["details"]["path"] == f"time.{field}"


@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("start", "2025-03-31T25:00:00Z"),
        ("end", "2025-03-31T25:00:00Z"),
        ("end", "2025-03-31T23:60:00Z"),
        ("end", "2025-03-31T23:00:60Z"),
        ("end", "2025-03-31T23:00:00Z trailing"),
    ],
)
@pytest.mark.parametrize("mode", ["validate", "run"])
def test_mistyped_timestamps_refuse_at_the_bound(runtime, field, literal, mode):
    call = {
        "select": ["measure.jaffle.order_count"],
        "time": {"start": "2025-03", "end": "2025-04", field: literal},
    }
    adapter = SemanticLayerMCPAdapter(runtime)
    response = adapter.call_tool("execute", {"query": call, "mode": mode, "now": NOW})
    assert response["ok"] is False
    (error,) = response["errors"]
    assert error["code"] == "INVALID_QUERY"
    assert (
        error["message"]
        == f"time.{field}: Use a valid ISO date, date-time or period, such as '2025-03'."
    )
    assert error["details"]["path"] == f"time.{field}"


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


@pytest.mark.parametrize(
    ("call", "verbosity"),
    [*((call, "minimal") for call in REFUSED), ("warned", "minimal"), ("warned", "compact")],
)
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
