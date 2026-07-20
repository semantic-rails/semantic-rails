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

from semantic_rails.ast import normalize_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression


def test_unknown_top_level_key_filters_is_rejected():
    """A payload with `filters` (the most common typo for `where`)
    must raise INVALID_QUERY with USE_CANONICAL_KEY recovery hint
    pointing at `where[]`."""
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(
            {
                "version": 1,
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
                "filters": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "x"}],
            }
        )
    err = excinfo.value
    assert err.code == "INVALID_QUERY"
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "USE_CANONICAL_KEY" in codes, f"expected USE_CANONICAL_KEY; got {hints!r}"
    # Hint must name the canonical key explicitly.
    assert any("where" in h.get("message", "") for h in hints), (
        f"hint message should reference `where`; got {hints!r}"
    )


def test_unknown_top_level_key_dimensions_points_at_group_by():
    """`dimensions: [...]` at the top level is a common LLM mistake.
    Hint should redirect to `group_by[]`."""
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.aov_usd"},
                        "as": "v",
                    }
                ],
                "dimensions": ["dimension.jaffle_store_name"],
            }
        )
    err = excinfo.value
    assert err.code == "INVALID_QUERY"
    hints = err.details.get("recovery_hints") or []
    assert any("group_by" in h.get("message", "") for h in hints), (
        f"hint should name group_by; got {hints!r}"
    )


def test_unknown_top_level_underscore_key_is_accepted():
    """Underscore-prefixed keys (e.g. `_note`) are documentation
    metadata — must pass through silently so example fixtures don't
    have to strip them."""
    normalize_query(
        {
            "version": 1,
            "_note": "an inline comment in JSON",
            "select": [
                {
                    "expression": {"metric": "metric.sales.aov_usd"},
                    "as": "v",
                }
            ],
        }
    )


def test_time_range_with_misplaced_start_end_points_at_top_level():
    """When an agent puts `start`/`end` inside `time.range` (the
    obvious wrong slot for absolute dates), the hint must say
    'move these to time.start / time.end at the top level'."""
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.aov_usd"},
                        "as": "v",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "range": {"start": "2017-01-01", "end": "2017-04-01"},
                },
            }
        )
    err = excinfo.value
    assert err.code == "INVALID_QUERY"
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "MOVE_BOUNDS_TO_TIME_TOP_LEVEL" in codes, (
        f"expected MOVE_BOUNDS_TO_TIME_TOP_LEVEL hint; got {hints!r}"
    )


def test_dimension_in_select_points_at_group_by():
    """Putting `{dimension: '...'}` in select[].expression is the
    natural SQL-like mistake. Targeted hint must redirect."""
    with pytest.raises(SemanticLayerError) as excinfo:
        parse_semantic_expression(
            {"dimension": "dimension.jaffle_store_name"},
            context="query",
        )
    err = excinfo.value
    assert err.code == "INVALID_EXPRESSION_AST"
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "MOVE_DIMENSION_TO_GROUP_BY" in codes, (
        f"expected MOVE_DIMENSION_TO_GROUP_BY hint; got {hints!r}"
    )
    # The hint should name the dimension id specifically.
    assert any("dimension.jaffle_store_name" in h.get("message", "") for h in hints)


def test_empty_result_with_relative_range_echoes_relative_window(runtime_factory):
    """When `time.range.last` produces a zero-row result (e.g., a
    relative window against historical fixture data), the
    EMPTY_RESULT_WINDOW warning must echo BOTH the resolved absolute
    bounds AND the original `relative_range` — otherwise the agent
    can't tell where the filter actually landed."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(
            {
                "version": 1,
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
                    "range": {"last": {"unit": "month", "value": 1}},
                },
                "sql_profile": "off",
            }
        )
        # Should return zero rows (data ends in 2017; relative window
        # is anchored to now()).
        assert len(result.get("rows", [])) == 0
        warnings = result.get("warnings", [])
        empty = next(
            (w for w in warnings if w.get("code") == "EMPTY_RESULT_WINDOW"),
            None,
        )
        assert empty is not None, f"expected EMPTY_RESULT_WINDOW warning; got {warnings!r}"
        requested = empty.get("details", {}).get("requested_window", {})
        # The bug F4 was actually closing: `start` / `end` came back as
        # empty strings when `range.last` was used (the diagnostic only
        # read `time_block.get("start")`, which is empty for relative
        # windows). The fix reads the resolved bounds from the
        # normalized query. Pin both halves: the absolute bounds AND
        # the original relative_range echo.
        assert requested.get("start"), (
            f"requested_window.start must be the resolved absolute date when "
            f"range.last is used (NOT the empty string); got {requested!r}"
        )
        assert requested.get("end"), (
            f"requested_window.end must be the resolved absolute date when "
            f"range.last is used (NOT the empty string); got {requested!r}"
        )
        assert "relative_range" in requested, (
            f"requested_window must echo relative_range when range was used; got {requested!r}"
        )
        # Cheap sanity: the resolved bounds look like ISO dates.
        assert "-" in str(requested["start"]) and len(str(requested["start"])) >= 10
        assert "-" in str(requested["end"]) and len(str(requested["end"])) >= 10
    finally:
        runtime.close()


def test_unknown_top_level_key_in_partial_query_is_rejected():
    """V2 from independent review: F1 closed the silent-drift hole on
    validate/compile/execute (via normalize_query) but `normalize_partial_query`
    on the build_options / plan paths still had the leak. The fix
    routes both paths through the same _check_unknown_top_level_keys
    helper. Pin it directly.
    """
    from semantic_rails.ast import normalize_partial_query

    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_partial_query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.aov_usd"},
                        "as": "v",
                    }
                ],
                "filters": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "x"}],
            }
        )
    err = excinfo.value
    assert err.code == "INVALID_QUERY"
    hints = err.details.get("recovery_hints") or []
    codes = {h.get("code") for h in hints}
    assert "USE_CANONICAL_KEY" in codes, (
        f"normalize_partial_query must reject filters with USE_CANONICAL_KEY; got {hints!r}"
    )
