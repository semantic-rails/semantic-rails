"""Round-six PR D — blind-agent friction follow-ups.

Five frictions surfaced by the fresh-blind-agent benchmark on the
post-PR-A/B/C MCP:

  F1  validate accepted unknown top-level keys (e.g. `filters: [...]`)
      with status:ok, then execute silently ignored them. Most dangerous
      failure mode — silent semantic drift. Now rejected with
      INVALID_QUERY + USE_CANONICAL_KEY hint.

  F2  `time.range: {start, end}` (misplaced absolute bounds) was
      rejected as "unsupported keys" but the hint did not point at the
      top-level `time.start` / `time.end`. Now ships
      MOVE_BOUNDS_TO_TIME_TOP_LEVEL.

  F3  `select[{expression: {dimension: "..."}}]` (dimension in select)
      raised the generic "Expression requires a 'kind'" with a docs
      pointer. Now detects the dimension key specifically and ships
      MOVE_DIMENSION_TO_GROUP_BY.

  F4  Zero-row execute with `time.range.last` echoed empty start/end
      in `requested_window`. Now resolves the absolute bounds AND echoes
      the original `relative_range` so the agent sees where the filter
      landed.
"""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call

AOV = [{"expression": "metric.sales.aov_usd", "as": "v"}]


def _refusal(call: dict, *, partial: bool = False) -> tuple[str, str]:
    """The refusal's path and its one hint."""
    with pytest.raises(SemanticLayerError) as excinfo:
        read_call(call, partial=partial)
    err = excinfo.value
    assert err.code == "INVALID_QUERY"
    (hint,) = err.details["recovery_hints"]
    return err.details["path"], hint["message"]


def test_unknown_top_level_key_filters_is_rejected():
    """A payload with `filters` (the most common typo for `where`)
    is refused, and the hint names the canonical key `where`."""
    path, hint = _refusal(
        {
            "select": AOV,
            "filters": [{"field": "dimension.jaffle_store_name", "op": "is", "value": "x"}],
        }
    )
    assert path == "filters"
    assert "where" in hint, f"hint message should reference `where`; got {hint!r}"


def test_unknown_top_level_key_dimensions_points_at_group_by():
    """`dimensions: [...]` at the top level is a common LLM mistake.
    Hint should redirect to `group_by[]`."""
    path, hint = _refusal({"select": AOV, "dimensions": ["dimension.jaffle_store_name"]})
    assert path == "dimensions"
    assert "group_by" in hint, f"hint should name group_by; got {hint!r}"


def test_unknown_top_level_underscore_key_is_accepted():
    """Underscore-prefixed keys (e.g. `_note`) are documentation
    metadata — must pass through silently so example fixtures don't
    have to strip them."""
    read_call({"_note": "an inline comment in JSON", "select": AOV})


def test_time_range_with_misplaced_start_end_points_at_top_level():
    """When an agent puts `start`/`end` inside the retired `time.range`
    (the obvious wrong slot for absolute dates), the hint must say
    'move these to time.start / time.end at the top level'."""
    path, hint = _refusal(
        {
            "select": AOV,
            "time": {
                "temporal_role": "temporal_role.jaffle_order_time",
                "grain": "month",
                "range": {"start": "2017-01-01", "end": "2017-04-01"},
            },
        }
    )
    assert path == "time.range"
    assert '"start"' in hint and '"end"' in hint, f"hint should move the bounds; got {hint!r}"


def test_dimension_in_select_points_at_group_by():
    """Putting `{dimension: '...'}` in select[].expression is the
    natural SQL-like mistake. Targeted hint must redirect."""
    path, hint = _refusal(
        {"select": [{"expression": {"dimension": "dimension.jaffle_store_name"}}]}
    )
    assert path == "select[0].expression"
    # The hint should name the dimension id and group_by specifically.
    assert "group_by" in hint and "dimension.jaffle_store_name" in hint, hint


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


def test_unknown_top_level_key_in_partial_query_is_rejected():
    """A plan or builder draft is read by the same rules: `filters` is
    refused there too, with the same hint."""
    path, hint = _refusal(
        {
            "select": AOV,
            "filters": [{"field": "dimension.jaffle_store_name", "op": "is", "value": "x"}],
        },
        partial=True,
    )
    assert path == "filters"
    assert "where" in hint, f"partial calls must reject filters naming `where`; got {hint!r}"
