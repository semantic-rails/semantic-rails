"""ZERO_FILLED_GROUPS on every warehouse: the marks the statement carries, and where it carries
none.

Grouped by customer, signups and orders join on the customer, so a customer with no orders
reads 0 orders. The guard marks each group a measure's own rows never reach in a hidden column
(1 or 0); the runtime strips the columns and counts the marked rows that read 0. A series, one
leaf and an ungrouped call can't hold such a group, so their statements carry no mark. The
answers are checked against reference SQL on DuckDB and Postgres in
``tests/integration/correctness/test_zero_filled_groups.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.db_parts.base import QueryRows
from semantic_rails.dialects import supported_warehouses
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts.answer_warnings import zero_filled_groups_notes
from semantic_rails.runtime_parts.assumptions import split_hidden

SHOP = Path(__file__).resolve().parents[1] / "integration" / "correctness" / "shop"
SIGNUPS = "measure.shop.signup_count"
ORDERS = "measure.shop.order_count"
REVENUE = "measure.shop.revenue"
CUSTOMER = "dimension.shop_customer_id"
STORE = "dimension.shop_order_store_id"
BY_CUSTOMER = {"select": [SIGNUPS, ORDERS, REVENUE], "group_by": [CUSTOMER]}
MARKS = {"signup_count": ["m1_absent"], "order_count": ["m2_absent"], "revenue": ["m3_absent"]}


@pytest.fixture(scope="module")
def shop() -> Any:
    return load_package_config(str(SHOP))


def _on(config: Any, warehouse: str) -> Any:
    return replace(config, package=replace(config.package, warehouse=warehouse))


def _compiled(config: Any, query: dict[str, Any]) -> dict[str, Any]:
    """The statement the runtime runs for ``query`` (no warehouse is opened)."""
    runtime = Runtime.from_config(config, source_path=str(SHOP), package_id="shop")
    try:
        payload = runtime.engine_payload(query, now="2026-10-01T00:00:00Z")
        return compile_query(runtime.config, runtime.registry, payload)
    finally:
        runtime.close()


@pytest.mark.parametrize("warehouse", sorted(supported_warehouses()))
def test_every_warehouse_marks_the_groups_a_measure_never_reached(shop, warehouse):
    config = _on(shop, warehouse)
    compiled = _compiled(config, BY_CUSTOMER)

    assert compiled["absent_rows"] == MARKS
    guard = next(cte for cte in compiled["sql_ast"].ctes if cte.name == "guarded_base")
    assert [field.alias for field in guard.query.select][-3:] == [
        "m1_absent",
        "m2_absent",
        "m3_absent",
    ]
    # A count's mark reads its value, a sum's its row count: NULL only where no row reached.
    sql = " ".join(compiled["sql"].split()).casefold()
    for source, mark in (("m1", "m1_absent"), ("m2", "m2_absent"), ("m3_rows", "m3_absent")):
        assert f"when (base.{source} is null) then 1 else 0 end as" in sql, mark

    # A warehouse that folds unquoted names returns the marks in its own case; the answer
    # never shows them, and the note counts the rows they mark.
    returned = QueryRows(
        [
            {CUSTOMER: 101, "signup_count": 1, "order_count": 3, "revenue": 23}
            | {"M1_ABSENT": 0, "M2_ABSENT": 0, "M3_ABSENT": 0},
            {CUSTOMER: 107, "signup_count": 1, "order_count": 0, "revenue": 0}
            | {"M1_ABSENT": 0, "M2_ABSENT": 1, "M3_ABSENT": 1},
            {CUSTOMER: 108, "signup_count": 0, "order_count": 1, "revenue": 2}
            | {"M1_ABSENT": "1", "M2_ABSENT": 0, "M3_ABSENT": 0},
        ],
        truncated=False,
    )
    rows, hidden = split_hidden(returned, compiled)
    assert [set(row) for row in rows] == [{CUSTOMER, "signup_count", "order_count", "revenue"}] * 3
    [note] = zero_filled_groups_notes(config, compiled, rows, hidden)
    assert [(row["output"], row["zero_filled"]) for row in note["details"]["outputs"]] == [
        ("signup_count", 1),
        ("order_count", 1),
        ("revenue", 1),
    ]
    assert note["details"]["groups"] == 3


def test_a_marked_group_that_reads_null_is_no_zero_filled_group(shop):
    """The mark says no row reached the group; a NULL there is no data, not data of nothing."""
    compiled = _compiled(shop, BY_CUSTOMER)
    returned = [
        {CUSTOMER: 107, "signup_count": 1, "order_count": None, "revenue": None}
        | {"m1_absent": 0, "m2_absent": 1, "m3_absent": 1},
    ]
    rows, hidden = split_hidden(returned, compiled)
    assert zero_filled_groups_notes(shop, compiled, rows, hidden) == []


CALLS = {
    "a series": {
        **BY_CUSTOMER,
        "time": {"grain": "month", "temporal_role": "temporal_role.shop_order_ordered_at"},
    },
    "one leaf": {"select": [REVENUE, ORDERS], "group_by": [STORE]},
    "no group": {"select": [SIGNUPS, ORDERS]},
}


@pytest.mark.parametrize("query", CALLS.values(), ids=CALLS.keys())
def test_a_call_that_holds_no_such_group_carries_no_mark(shop, query):
    compiled = _compiled(shop, query)
    assert compiled["absent_rows"] == {}
    assert "_absent" not in compiled["sql"]
