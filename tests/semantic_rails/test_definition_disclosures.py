"""Say when a row mixes facts on different clocks.

Orders (dated by order time) and storefront sessions (dated by session start) grouped by
customer, with no window, each count all of their own history, so a ratio of the two is not a
rate over one period. The answer is right but reads as one, so it carries ``MIXED_TIME_ROLES``.
The warning never changes the numbers.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.runtime import Runtime

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
"""


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from tests.semantic_rails.conftest import copy_package_config

    package_dir = copy_package_config(
        tmp_path_factory.mktemp("disclosures"), "jaffle_shop", preseed_db=True
    )
    (package_dir / "metrics" / "extensions" / "orders_per_session.yml").write_text(
        _ORDERS_PER_SESSION_YAML, encoding="utf-8"
    )
    runtime = Runtime.from_path(str(package_dir))
    yield runtime
    runtime.close()


def _agg(measure: str) -> dict[str, Any]:
    return {"kind": "aggregate", "measure": f"measure.jaffle.{measure}"}


def _query(*selects: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "select": [{"expression": expr, "as": f"v{index}"} for index, expr in enumerate(selects)],
        "group_by": [CUSTOMER],
        **extra,
    }


def _mixed(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in out["warnings"] if w["code"] == "MIXED_TIME_ROLES"]


ORDERS, SESSIONS = _agg("order_count"), _agg("session_starts")
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


@pytest.mark.parametrize("method", ["validate", "compile", "query"])
def test_two_facts_on_different_clocks_with_no_window_get_one_warning(runtime, method):
    out = getattr(runtime, method)(_query(ORDERS, SESSIONS))

    [warning] = _mixed(out)
    assert warning["severity"] == "warning"
    assert warning["message"] == (
        "These measures are dated by different time roles: measure.jaffle.order_count by "
        f"{ORDER_TIME}; measure.jaffle.session_starts by {SESSION_TIME}. With no window each "
        "covers all of its own history; add a window, or read them separately."
    )
    assert warning["details"] == {
        "clocks": [
            {"subject": "measure.jaffle.order_count", "temporal_roles": [ORDER_TIME]},
            {"subject": "measure.jaffle.session_starts", "temporal_roles": [SESSION_TIME]},
        ]
    }
    assert warning["object_ids"] == ["measure.jaffle.order_count", "measure.jaffle.session_starts"]


@pytest.mark.parametrize(
    "query",
    [
        # A window covers one period on each fact's own clock.
        _query(
            ORDERS, SESSIONS, time={**SESSION_MONTHS, "start": "2016-09-01", "end": "2016-10-01"}
        ),
        # With a time grain each row is one period, not all of history.
        _query(ORDERS, SESSIONS, time=SESSION_MONTHS),
        # Items and orders share the order's clock.
        _query(_agg("item_revenue_usd"), ORDERS),
        # One fact.
        _query(ORDERS, _agg("revenue_usd")),
        # A governed metric over two clocks, alone, is the package's own definition.
        _query({"metric": ORDERS_PER_SESSION}),
    ],
    ids=["window", "grain", "shared-role", "one-fact", "governed-metric"],
)
def test_no_mixed_clock_warning(runtime, query):
    assert _mixed(runtime.query(query)) == []


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
        # An aggregate_if over sessions has no time role, so it is its own clock.
        ([ORDERS, SESSIONS_IF], ["aggregate_if(count, …) has no time role"]),
        # A governed metric is one clock, with every role it combines.
        (
            [{"metric": ORDERS_PER_SESSION}, _agg("delivered_orders")],
            [
                f"{ORDERS_PER_SESSION} by {ORDER_TIME} and {SESSION_TIME}",
                f"measure.jaffle.delivered_orders by {DELIVERED_TIME}",
            ],
        ),
    ],
    ids=["ratio", "arithmetic", "case", "aggregate-if", "governed-metric"],
)
def test_measures_inside_a_select_expression_are_disclosed_too(runtime, selects, clocks):
    [warning] = _mixed(runtime.query(_query(*selects)))

    assert all(clock in warning["message"] for clock in clocks)


def test_the_warning_never_changes_the_answer(runtime, monkeypatch):
    query = _query(ORDERS, SESSIONS)
    before = runtime.query(query)
    assert len(_mixed(before)) == 1
    monkeypatch.setattr(runtime_module, "mixed_time_role_warnings", lambda *_: [])

    after = runtime.query(query)

    assert _mixed(after) == []
    assert after["rendered_sql"] == before["rendered_sql"]
    assert _by_customer(after["rows"]) == _by_customer(before["rows"])


def _by_customer(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row[CUSTOMER]))
