"""Common call mistakes are refused at their path with a hint that names the fix.

An unknown top-level key (``filters``, ``dimensions``) used to validate and then be ignored;
absolute bounds inside the retired ``time.range``, and a dimension in ``select``, got a
generic error. Each is refused at its path, in a full call and in a draft alike, and a
relative window after the data echoes where it landed.
"""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call

AOV = [{"expression": "metric.sales.aov_usd", "as": "v"}]
FILTERS = [{"field": "dimension.jaffle_store_name", "op": "is", "value": "x"}]
RANGE = {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "range": {"start": "2017-01-01", "end": "2017-04-01"},
}
STORE_IN_SELECT = [{"expression": {"dimension": "dimension.jaffle_store_name"}}]


@pytest.mark.parametrize(
    ("call", "partial", "path", "named"),
    [
        ({"select": AOV, "filters": FILTERS}, False, "filters", ["where"]),
        ({"select": AOV, "filters": FILTERS}, True, "filters", ["where"]),
        ({"select": AOV, "dimensions": ["dimension.jaffle_store_name"]}, False, "dimensions", ["group_by"]),
        ({"select": AOV, "time": RANGE}, False, "time.range", ['"start"', '"end"']),
        ({"select": STORE_IN_SELECT}, False, "select[0].expression", ["group_by", "dimension.jaffle_store_name"]),
    ],
    ids=["filters", "filters-in-a-draft", "dimensions", "time-range-bounds", "dimension-in-select"],
)  # fmt: skip
def test_a_common_mistake_is_refused_at_its_path_naming_the_fix(call, partial, path, named):
    with pytest.raises(SemanticLayerError) as excinfo:
        read_call(call, partial=partial)
    err = excinfo.value
    assert (err.code, err.details["path"]) == ("INVALID_QUERY", path)
    (hint,) = err.details["recovery_hints"]
    assert all(word in hint["message"] for word in named), hint


def test_a_relative_window_after_the_data_echoes_its_bounds(runtime_factory):
    """`time.last` against historical fixture data: the window still answers its month, NULL
    with NO_DATA_YET, and the answer echoes the resolved absolute bounds and the data's edge,
    so the agent can tell where the window landed."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "last": {"unit": "month", "value": 1},
                },
            },
            now="2026-10-09T12:00:00Z",
            sql_profile="off",
        )
        # Data ends in 2017; the window is the month before `now`.
        assert [(row["period"], row["rev"]) for row in result["rows"]] == [("2026-09-01", None)]
        assert result["window"]["start"] == "2026-09-01"
        assert result["window"]["end"] == "2026-10-01"
        assert result["window"]["data_through"].startswith("2017-")
        codes = [w.get("code") for w in result.get("warnings", [])]
        assert "NO_DATA_YET" in codes and "EMPTY_RESULT_WINDOW" not in codes
    finally:
        runtime.close()
