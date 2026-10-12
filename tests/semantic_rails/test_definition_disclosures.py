"""Say when a row mixes facts on different clocks.

Orders (dated by order time) and storefront sessions (dated by session start) grouped by
customer, with no time block, carry ``MIXED_TIME_ROLES``. Each period is read on its own
role's clock; filters can bound those periods. The warning never changes the numbers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import duckdb
import pytest

from semantic_rails.db import DuckDBAdapter
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts import answer_warnings

CUSTOMER = "dimension.jaffle_customer_id"
ORDER_TIME = "temporal_role.jaffle_order_time"
SESSION_TIME = "temporal_role.jaffle_session_started_at"
DELIVERED_TIME = "temporal_role.jaffle_lifecycle_delivered_at"
# A governed metric over two clocks: the package defined it, so it is one clock.
ORDERS_PER_SESSION = "metric.test.orders_per_session"
_ORDERS_PER_SESSION_YAML = """\
metrics:
  test.orders_per_session:
    as: metric.test.orders_per_session
    label: Orders per session
    description: Orders over storefront sessions, each on its own clock.
    kind: ratio
    numerator: order_count
    denominator: session_starts
    value_type: number
"""


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from tests.semantic_rails.conftest import copy_package_config, opened

    package_dir = copy_package_config(
        tmp_path_factory.mktemp("disclosures"), "jaffle_shop", preseed_db=True
    )
    (package_dir / "metrics" / "extensions" / "orders_per_session.yml").write_text(
        _ORDERS_PER_SESSION_YAML, encoding="utf-8"
    )
    runtime = Runtime.from_path(str(package_dir))
    yield opened(runtime)
    runtime.close()


def _agg(measure: str) -> dict[str, Any]:
    return {"kind": "aggregate", "measure": f"measure.jaffle.{measure}"}


def _query(*selects: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "select": [{"expression": expr, "as": f"v{index}"} for index, expr in enumerate(selects)],
        "group_by": [CUSTOMER],
        **extra,
    }


def _mixed(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in out.get("warnings", []) if w["code"] == "MIXED_TIME_ROLES"]


ORDERS, SESSIONS = _agg("order_count"), _agg("session_starts")
RATIO = {"kind": "ratio", "numerator": ORDERS, "denominator": SESSIONS}
SESSION_MONTHS = {"temporal_role": SESSION_TIME, "grain": "month"}
# An aggregate_if has no time role of its own.
SESSIONS_IF = {
    "kind": "aggregate_if",
    "aggregation": "count",
    "condition": {
        "kind": "comparison",
        "op": "=",
        "left": {
            "kind": "column",
            "column": "store_id",
            "entity": "entity.jaffle_storefront_session",
        },
        "right": {"kind": "literal", "value": "1"},
    },
}
ORDERS_IF = {
    **SESSIONS_IF,
    "condition": {
        **SESSIONS_IF["condition"],
        "left": {"kind": "column", "column": "customer_id", "entity": "entity.jaffle_order"},
    },
}


METHODS = ["validate", "compile", "query"]
TWO_CLOCKS = {
    "clocks": [
        {"subject": "measure.jaffle.order_count", "temporal_roles": [ORDER_TIME]},
        {"subject": "measure.jaffle.session_starts", "temporal_roles": [SESSION_TIME]},
    ]
}


# The documented message: it names the roles and how each period is read.
TWO_CLOCKS_MESSAGE = (
    "These measures are dated by different time roles: measure.jaffle.order_count by "
    f"{ORDER_TIME}; measure.jaffle.session_starts by {SESSION_TIME}. "
    "Each period is read on its own role's clock."
)


@pytest.mark.parametrize("method", METHODS)
def test_two_facts_on_different_clocks_with_no_window_get_one_warning(runtime, method):
    [warning] = _mixed(getattr(runtime, method)(_query(ORDERS, SESSIONS)))
    assert warning["severity"] == "warning"
    assert warning["message"] == TWO_CLOCKS_MESSAGE
    assert warning["details"] == TWO_CLOCKS
    assert warning["object_ids"] == ["measure.jaffle.order_count", "measure.jaffle.session_starts"]


@pytest.mark.parametrize(
    "query",
    [
        # Every time block suppresses the warning, including bounds without a grain.
        _query(
            RATIO,
            time={"temporal_role": SESSION_TIME, "start": "2016-09-01", "end": "2016-10-01"},
        ),
        _query(RATIO, time={"temporal_role": SESSION_TIME, "last": {"unit": "month", "value": 1}}),
        # With a time grain each row is one period, not all of history.
        _query(ORDERS, SESSIONS, time=SESSION_MONTHS),
        # Items and orders share the order's clock.
        _query(_agg("item_revenue_usd"), ORDERS),
        # One fact.
        _query(ORDERS, _agg("revenue_usd")),
        # A governed metric over two clocks, alone, is the package's own definition.
        _query(ORDERS_PER_SESSION),
        # Undated measures are not clocks, even beside a dated measure.
        _query(ORDERS, SESSIONS_IF),
        _query(SESSIONS_IF, ORDERS_IF),
    ],
    ids=[
        "window",
        "last",
        "grain",
        "shared-role",
        "one-fact",
        "governed-metric",
        "dated-and-undated",
        "two-undated",
    ],
)
def test_no_mixed_clock_warning(runtime, query):
    assert _mixed(runtime.query(query, now="2016-10-01T00:00:00Z")) == []


@pytest.mark.parametrize(
    ("selects", "clocks"),
    [
        # Inside one select expression: a ratio, arithmetic or a case.
        (
            [{"kind": "ratio", "numerator": ORDERS, "denominator": SESSIONS}],
            [f"measure.jaffle.order_count by {ORDER_TIME}", "measure.jaffle.session_starts by"],
        ),
        (
            [{"kind": "arithmetic", "op": "subtract", "left": ORDERS, "right": SESSIONS}],
            [f"measure.jaffle.order_count by {ORDER_TIME}", "measure.jaffle.session_starts by"],
        ),
        (
            [
                {
                    "kind": "case",
                    "whens": [
                        {
                            "when": {
                                "kind": "comparison",
                                "op": ">",
                                "left": SESSIONS,
                                "right": {"kind": "literal", "value": 0},
                            },
                            "then": ORDERS,
                        }
                    ],
                }
            ],
            [f"measure.jaffle.order_count by {ORDER_TIME}", "measure.jaffle.session_starts by"],
        ),
        # A governed metric is one clock, with every role it combines.
        (
            [ORDERS_PER_SESSION, _agg("delivered_orders")],
            [
                f"{ORDERS_PER_SESSION} by {ORDER_TIME} and {SESSION_TIME}",
                f"measure.jaffle.delivered_orders by {DELIVERED_TIME}",
            ],
        ),
    ],
    ids=["ratio", "arithmetic", "case", "governed-metric"],
)
def test_measures_inside_a_select_expression_are_disclosed_too(runtime, selects, clocks):
    [warning] = _mixed(runtime.query(_query(*selects)))

    assert all(clock in warning["message"] for clock in clocks)


@pytest.mark.parametrize("method", METHODS)
def test_package_without_time_never_warns_about_mixed_clocks(runtime, method):
    config = replace(
        runtime.config,
        temporal_roles=[],
        measures=[
            replace(measure, default_temporal_role="", compatible_temporal_roles=[])
            for measure in runtime.config.measures
            if measure.id in {ORDERS["measure"], SESSIONS["measure"]}
        ],
        metric_recipes=[],
    )
    isolated = Runtime.from_config(config, source_path=runtime.source_path)
    try:
        out = getattr(isolated, method)(_query(ORDERS, SESSIONS))
        assert out["ok"], out
        assert _mixed(out) == []
    finally:
        isolated.close()


def _one_customer(runtime: Runtime, tmp_path, orders: list[str], sessions: list[str]) -> Runtime:
    """The package's semantics over one customer with orders and sessions at these times."""
    isolated = Runtime.from_config(runtime.config, source_path=runtime.source_path)
    db_path = str(tmp_path / "one_customer.duckdb")
    with duckdb.connect(db_path) as connection:
        connection.execute("CREATE TABLE jaffle_customer AS SELECT 'c1' AS customer_id")
        for table, key, column, times in [
            ("jaffle_order", "order_id", "ordered_at", orders),
            ("jaffle_storefront_session", "session_id", "started_at", sessions),
        ]:
            rows = ", ".join(f"('{key}{n}', 'c1', TIMESTAMP '{at}')" for n, at in enumerate(times))
            connection.execute(
                f"CREATE TABLE {table} AS SELECT * FROM (VALUES {rows}) "
                f"AS t({key}, customer_id, {column})"
            )
    isolated.set_adapter(DuckDBAdapter(db_path))
    return isolated


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("kind", ["aggregate", "scoped_aggregate"])
def test_date_filtered_ratio_discloses_roles_without_claiming_all_history(
    runtime, tmp_path, method, kind
):
    isolated = _one_customer(
        runtime,
        tmp_path,
        orders=["2016-08-01", "2016-09-01", "2016-09-02"],
        sessions=["2016-08-01", "2016-08-02", "2016-09-01"],
    )

    def bounded(measure, dimension):
        bounds = [
            {"field": dimension, "op": "gte", "value": "2016-09-01"},
            {"field": dimension, "op": "lt", "value": "2016-10-01"},
        ]
        return {
            **measure,
            "kind": kind,
            **({"filter": {"all": bounds}} if kind == "aggregate" else {"where": bounds}),
        }

    try:
        query = _query(
            {
                "kind": "ratio",
                "numerator": bounded(ORDERS, "dimension.jaffle_order_ordered_at"),
                "denominator": bounded(SESSIONS, "dimension.jaffle_session_started_at"),
            }
        )
        # The bounded September ratio differs from the all-history ratio.
        assert [row["v0"] for row in isolated.query(query)["rows"]] == [2.0]
        assert [row["v0"] for row in isolated.query(_query(RATIO))["rows"]] == [1.0]
        out = getattr(isolated, method)(query)
        assert out["ok"], out
        [warning] = _mixed(out)
        assert warning["message"] == TWO_CLOCKS_MESSAGE
        assert warning["details"] == TWO_CLOCKS
        assert "history" not in warning["message"]
    finally:
        isolated.close()


def test_daily_ratio_covers_each_day_not_all_history(runtime, tmp_path):
    isolated = _one_customer(
        runtime,
        tmp_path,
        orders=["2016-01-01", "2016-01-02", "2016-01-02"],
        sessions=["2016-01-01", "2016-01-02"],
    )
    try:
        timed_query = _query(RATIO, time={"temporal_role": SESSION_TIME, "grain": "day"})
        timed = isolated.query(timed_query)
        total = isolated.query(_query(RATIO))

        assert sorted(row["v0"] for row in timed["rows"]) == [1.0, 2.0]
        assert [row["v0"] for row in total["rows"]] == [1.5]
        assert len(_mixed(total)) == 1
        assert _mixed(timed) == []
        assert _mixed(isolated.validate(timed_query)) == []
        assert _mixed(isolated.compile(timed_query)) == []
    finally:
        isolated.close()


def test_the_warning_never_changes_the_answer(runtime, monkeypatch):
    query = _query(ORDERS, SESSIONS)
    before = runtime.query(query)
    assert len(_mixed(before)) == 1
    monkeypatch.setattr(answer_warnings, "mixed_time_role_warnings", lambda *_: [])

    after = runtime.query(query)

    assert _mixed(after) == []
    assert after["rendered_sql"] == before["rendered_sql"]
    assert _by_customer(after["rows"]) == _by_customer(before["rows"])


def _by_customer(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row[CUSTOMER]))
