"""Missing calendar periods refuse; independent spines preserve dated NULLs."""

import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload
from semantic_rails.request_context import TrustedAttributes
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.result_helpers import typed_rows

from .conftest import _rows
from .test_time_coverage import changed_runtime

calendar_runtime = changed_runtime

ROLE = "temporal_role.shop_order_ordered_at"
KEY = f"{ROLE}__week"
QUESTION = "How many orders did we get last week compared with the week before?"
CALENDAR = """
DELETE FROM dim_date;
INSERT INTO dim_date
SELECT CAST(d AS DATE), CAST(date_trunc('week', d) AS DATE),
  CAST(date_trunc('month', d) AS DATE), CAST(date_trunc('quarter', d) AS DATE),
  CAST(date_trunc('year', d) AS DATE)
FROM generate_series(TIMESTAMP '2018-08-20', TIMESTAMP '2018-09-02', INTERVAL '1 day') g(d);
"""
REFERENCE = """
WITH periods(bucket) AS (VALUES (DATE '2018-08-20'), (DATE '2018-08-27')),
coverage AS (SELECT date_trunc('week', MIN(ordered_at)) lo,
  date_trunc('week', MAX(CASE WHEN ordered_at <= CURRENT_TIMESTAMP THEN ordered_at END)) hi
  FROM orders)
SELECT p.bucket, CASE WHEN p.bucket BETWEEN c.lo AND c.hi
  THEN COUNT(DISTINCT o.order_id) END
FROM periods p CROSS JOIN coverage c LEFT JOIN orders o
  ON o.ordered_at >= p.bucket AND o.ordered_at < p.bucket + INTERVAL '1 week'
GROUP BY p.bucket, c.lo, c.hi ORDER BY p.bucket
"""


def _planned(runtime):
    planned = plan_payload(
        runtime, intent=QUESTION, partial_query={"policy_context": {"now": "2018-09-03"}}
    )
    assert planned["status"] == "ok", planned
    assert planned["next"]["ready_for"] == ["execute"]
    return planned["best"]["query_ir"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    "edge,missing",
    [
        ("2018-08-27", ["2018-08-27"]),
        ("2018-08-20", ["2018-08-20", "2018-08-27"]),
        ("2018-09-03", []),
    ],
)
def test_pair_checks_calendar_presence_against_independent_spine(calendar_runtime, edge, missing):
    runtime = calendar_runtime(
        "utc_authored", CALENDAR + f"DELETE FROM dim_date WHERE date_day >= DATE '{edge}'"
    )
    query = _planned(runtime)
    reference = [(str(bucket)[:10], count) for bucket, count in _rows(runtime, REFERENCE)]
    assert reference == [("2018-08-20", None), ("2018-08-27", None)]
    result = SemanticLayerMCPAdapter(runtime).call_tool("execute", {"query": query, "mode": "run"})
    if missing:
        assert result["status"] == "error" and not result["ok"], result
        assert not result.get("rows"), result
        assert result["errors"][0]["code"] == "FILL_INCOMPLETE"
        assert result["errors"][0]["details"]["missing_periods"] == missing
        assert "EMPTY_RESULT_WINDOW" not in str(result)
        with pytest.raises(SemanticLayerError, match="extend the authored calendar through") as exc:
            runtime.query(query)
        assert exc.value.details["missing_periods"] == missing
    else:
        result = runtime.query(query)
        alias = query["select"][0]["as"]
        assert [(str(r[KEY])[:10], r[alias]) for r in typed_rows(result)] == reference
        assert "NO_DATA_IN_SCOPE" in {w["code"] for w in result["warnings"]}


def _weekly(start: str, end: str) -> dict:
    return {
        "select": [{"as": "orders", "expression": {"measure": "measure.shop.order_count"}}],
        "time": {"temporal_role": ROLE, "grain": "week", "start": start, "end": end, "fill": True},
        "order_by": [{"field": "time", "direction": "ASC"}],
    }


def _refused(runtime, query) -> dict:
    result = SemanticLayerMCPAdapter(runtime).call_tool("execute", {"query": query, "mode": "run"})
    assert result["status"] == "error" and not result["ok"], result
    assert not result.get("rows"), result
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)
    assert exc.value.code == "FILL_INCOMPLETE"
    return exc.value.details


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("edge", ["2018-08-27", "2018-09-03"], ids=["short", "covered"])
def test_sub_microsecond_end_never_answers(calendar_runtime, edge):
    runtime = calendar_runtime(
        "utc_authored", CALENDAR + f"DELETE FROM dim_date WHERE date_day >= DATE '{edge}'"
    )
    # The end keeps a tenth of a microsecond of August 27, so both weeks are required.
    reference = [(str(bucket)[:10], count) for bucket, count in _rows(runtime, REFERENCE)]
    assert reference == [("2018-08-20", None), ("2018-08-27", None)]
    details = _refused(runtime, _weekly("2018-08-20", "2018-08-27T00:00:00.0000001"))
    assert details["reason"] == "unverifiable" and details["bounds"] == ["end"]


SUNDAY_CALENDAR = """
DELETE FROM dim_date;
INSERT INTO dim_date
SELECT CAST(d AS DATE),
  CAST(date_trunc('week', d + INTERVAL '1 day') - INTERVAL '1 day' AS DATE),
  CAST(date_trunc('month', d) AS DATE), CAST(date_trunc('quarter', d) AS DATE),
  CAST(date_trunc('year', d) AS DATE)
FROM generate_series(TIMESTAMP '2018-08-19', TIMESTAMP '2018-09-08', INTERVAL '1 day') g(d);
"""
SUNDAY_REFERENCE = """
WITH periods(bucket) AS (
  VALUES (DATE '2018-08-19'), (DATE '2018-08-26'), (DATE '2018-09-02')),
coverage AS (SELECT
  date_trunc('week', MIN(ordered_at) + INTERVAL '1 day') - INTERVAL '1 day' lo,
  date_trunc('week', MAX(CASE WHEN ordered_at <= CURRENT_TIMESTAMP THEN ordered_at END)
    + INTERVAL '1 day') - INTERVAL '1 day' hi
  FROM orders)
SELECT p.bucket, CASE WHEN p.bucket BETWEEN c.lo AND c.hi
  THEN COUNT(DISTINCT o.order_id) END
FROM periods p CROSS JOIN coverage c LEFT JOIN orders o
  ON o.ordered_at >= p.bucket AND o.ordered_at < p.bucket + INTERVAL '1 week'
  AND o.ordered_at >= TIMESTAMP '2018-08-20' AND o.ordered_at < TIMESTAMP '2018-09-03'
GROUP BY p.bucket, c.lo, c.hi ORDER BY p.bucket
"""


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    "edge,returned", [("2018-09-02", 2), ("2018-09-09", 3)], ids=["short", "complete"]
)
def test_authored_sunday_weeks_are_unverifiable(calendar_runtime, edge, returned):
    runtime = calendar_runtime(
        "utc_authored", SUNDAY_CALENDAR + f"DELETE FROM dim_date WHERE date_day >= DATE '{edge}'"
    )
    reference = [(str(bucket)[:10], count) for bucket, count in _rows(runtime, SUNDAY_REFERENCE)]
    assert reference == [("2018-08-19", None), ("2018-08-26", None), ("2018-09-02", None)]
    # Even the complete three-week answer refuses: Monday buckets cannot prove Sunday ones.
    details = _refused(runtime, _weekly("2018-08-20", "2018-09-03"))
    assert details["reason"] == "unverifiable"
    assert (details["expected_periods"], details["returned_periods"]) == (2, returned)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    "addition",
    [
        {
            "metric_filters": [
                {"expression": {"measure": "measure.shop.order_count"}, "op": ">", "value": 0}
            ]
        },
        {"limit": 1},
        {"group_by": ["dimension.shop_order_store_id"]},
    ],
)
def test_short_fills_with_row_removing_shapes_are_exempt(calendar_runtime, addition):
    runtime = calendar_runtime(
        "utc_authored", CALENDAR + "DELETE FROM dim_date WHERE date_day >= DATE '2018-08-27'"
    )
    query = {**_planned(runtime), **addition}
    result = runtime.query(query)
    assert result["ok"]
    assert result["row_count"] < 2


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_resource_truncation_is_exempt(calendar_runtime):
    runtime = calendar_runtime("utc_authored", CALENDAR)
    result = runtime.query({**_planned(runtime), "limits": {"max_rows": 1}})
    assert result["ok"] and result["truncated"] and result["row_count"] == 1


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_one_row_injected_beneath_valid_plan_never_answers_ok(calendar_runtime, monkeypatch):
    runtime = calendar_runtime("utc_authored", CALENDAR)
    query = _planned(runtime)
    original = runtime_module._adapter_query

    def lose_row(*args, **kwargs):
        return original(*args, **kwargs)[:1]

    monkeypatch.setattr(runtime_module, "_adapter_query", lose_row)
    result = SemanticLayerMCPAdapter(runtime).call_tool("execute", {"mode": "run", "query": query})
    assert not result["ok"] and result["status"] == "error"
    assert result["errors"][0]["code"] == "FILL_INCOMPLETE"
    assert result["errors"][0]["details"]["missing_periods"] == ["2018-08-27"]
    assert not result.get("rows")


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_fill_check_preserves_policy_filters_and_withholding(calendar_runtime, monkeypatch):
    runtime = calendar_runtime(
        "utc_authored",
        CALENDAR
        + """
      INSERT INTO orders (order_id, ordered_at, amount, store_id) VALUES
        (991, TIMESTAMP '2018-08-20', 10, 'a'), (992, TIMESTAMP '2018-08-27', 100, 'b');
    """,
    )
    query = _planned(runtime)
    runtime._config.semantic_policies.extend(
        [
            SemanticPolicyConfig(
                "policy.test.store",
                "row_filter",
                config={"dimension": "dimension.shop_order_store_id", "attribute": "store"},
            ),
            SemanticPolicyConfig(
                "policy.test.hidden",
                "object_access",
                action="withhold_values",
                object_ids=["measure.shop.revenue"],
                roles=["sales"],
            ),
        ]
    )
    query["policy_context"] = {"roles": ["sales"], "attributes": TrustedAttributes({"store": "a"})}
    # The existing row-policy guard refuses calendar joins before any value can escape.
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)
    assert exc.value.code == "POLICY_DENIED"
    assert exc.value.details["reason"] == "row_filter_unsupported_query"
    guarded = SemanticLayerMCPAdapter(runtime).call_tool("execute", {"query": query, "mode": "run"})
    assert not guarded["ok"] and not guarded.get("rows")
    filtered = runtime.query({**query, "time": {**query["time"], "fill": False}})
    alias = query["select"][0]["as"]
    gold = _rows(
        runtime,
        "SELECT date_trunc('week', ordered_at), COUNT(DISTINCT order_id) "
        "FROM orders WHERE store_id = 'a' AND ordered_at >= TIMESTAMP '2018-08-20' "
        "AND ordered_at < TIMESTAMP '2018-09-03' GROUP BY 1",
    )
    assert [(str(row[KEY])[:10], row[alias]) for row in typed_rows(filtered)] == [
        (str(bucket)[:10], count) for bucket, count in gold
    ]
    runtime._config.semantic_policies.pop(0)
    # Existing rank-only withholding remains enforced for a bounded filled series.
    ranked = {
        **query,
        "select": [{"as": "revenue", "expression": {"measure": "measure.shop.revenue"}}],
        "order_by": [
            {"field": "revenue", "direction": "DESC"},
            {"field": "time", "direction": "DESC"},
        ],
        "limit": 2,
    }
    hidden = runtime.query(ranked)
    assert hidden["withheld"] == ["measure.shop.revenue"]
    assert all("revenue" not in row for row in hidden["rows"])
    original = runtime_module._adapter_query

    def lose_row(*args, **kwargs):
        return original(*args, **kwargs)[:1]

    monkeypatch.setattr(runtime_module, "_adapter_query", lose_row)
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)
    assert exc.value.code == "FILL_INCOMPLETE"
    assert exc.value.details["missing_periods"] == ["2018-08-27"]
