"""Guards on what a call asks for: column names, statistic overrides, governed measures,
shares of a total, rank by key."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime

REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Runtime]:
    rt = runtime_factory("jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


def _refusal(call: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc:
        read_call(call)
    return exc.value


def test_a_reference_is_named_by_its_last_segment_and_a_kind_by_the_engine() -> None:
    select = read_call(
        {
            "select": [
                REVENUE,
                {"expression": REVENUE, "as": "total"},
                {"expression": {"kind": "measure", "measure": REVENUE, "aggregation": "avg"}},
            ]
        }
    )[0]["select"]
    assert [row["as"] for row in select] == ["revenue_usd", "total", "expr_3"]


@pytest.mark.parametrize(
    ("call", "path", "fix"),
    [
        (
            {"select": [REVENUE, {"expression": REVENUE}]},
            "select[1]",
            '{"expression": "measure.jaffle.revenue_usd", "as": "revenue_usd_2"}',
        ),
        (
            {"select": [{"expression": REVENUE, "as": "period"}], "time": {"grain": "month"}},
            "select[0]",
            '"as": "period_2"',
        ),
        (
            {"select": [{"expression": REVENUE, "as": STORE}], "group_by": [STORE]},
            "select[0]",
            f'"as": "{STORE}_2"',
        ),
        ({"select": [REVENUE], "group_by": [STORE, STORE]}, "group_by[1]", "Remove group_by[1]"),
    ],
)
def test_a_repeated_column_name_is_refused_with_the_as_that_fixes_it(
    call: dict[str, Any], path: str, fix: str
) -> None:
    error = _refusal(call)
    assert error.code == "DUPLICATE_OUTPUT_ALIAS"
    assert error.details["path"] == path
    [hint] = error.details["recovery_hints"]
    assert fix in hint["message"]


def test_period_is_a_free_name_without_a_grain() -> None:
    select = read_call({"select": [{"expression": REVENUE, "as": "period"}]})[0]["select"]
    assert select[0]["as"] == "period"
