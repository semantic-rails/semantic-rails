"""Regression tests for MIXED_GRAIN_INVALID recovery enrichment.

A blind-agent evaluation showed that MIXED_GRAIN_INVALID was a dead
end: pairing ``measure.jaffle.revenue_usd`` with product dimensions
took five failed calls of brute force before the agent discovered that
``measure.jaffle.item_revenue_usd`` works, and another agent never
recovered from grouping an order measure by
``dimension.jaffle_time_month_start`` instead of using ``time.grain``.

These tests pin the enrichment contract added in
``semantic_rails/compiler_parts/grain_recovery.py``:

1. The error details carry bounded ``compatible_measures`` /
   ``compatible_dimensions`` lists (registry metadata only) and a
   ``closest_compatible_measure``.
2. A ``replace_measure`` recovery hint names the closest compatible
   measure.
3. Calendar-date dimensions additionally carry a ``use_time_grain``
   hint pointing at the ``time`` block, listed first.
"""

from __future__ import annotations

MAX_SUGGESTIONS = 5


def _first_error(report: dict) -> dict:
    assert report["ok"] is False
    errors = list(report.get("errors") or [])
    assert errors, "expected at least one error"
    return errors[0]


def _validate(runtime, *, measure: str, group_by: list[str], time: dict | None = None) -> dict:
    payload: dict = {
        "select": [{"expression": {"measure": measure}, "as": "value"}],
        "group_by": group_by,
    }
    if time is not None:
        payload["time"] = time
    return runtime.validate(payload)


def test_revenue_by_product_name_suggests_item_revenue(runtime_factory) -> None:
    """revenue_usd x jaffle_product_name must name item_revenue_usd as
    a compatible measure and surface it in a replace_measure hint."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(
            runtime,
            measure="measure.jaffle.revenue_usd",
            group_by=["dimension.jaffle_product_name"],
        )
        err = _first_error(report)
        assert err["code"] == "MIXED_GRAIN_INVALID"
        details = err.get("details") or {}

        assert details.get("measures") == ["measure.jaffle.revenue_usd"]
        assert details.get("offending_dimensions") == ["dimension.jaffle_product_name"]

        compatible_measures = list(details.get("compatible_measures") or [])
        assert "measure.jaffle.item_revenue_usd" in compatible_measures
        assert len(compatible_measures) <= MAX_SUGGESTIONS
        # Semantically closest name ranks first and is called out.
        assert details.get("closest_compatible_measure") == "measure.jaffle.item_revenue_usd"
        assert compatible_measures[0] == "measure.jaffle.item_revenue_usd"

        compatible_dimensions = list(details.get("compatible_dimensions") or [])
        assert compatible_dimensions, "compatible_dimensions must be populated"
        assert len(compatible_dimensions) <= MAX_SUGGESTIONS
        # The offending dimension must not be suggested back.
        assert "dimension.jaffle_product_name" not in compatible_dimensions

        hints = list(err.get("recovery_hints") or [])
        replace_measure = [h for h in hints if h.get("kind") == "replace_measure"]
        assert replace_measure, "replace_measure recovery hint must be present"
        assert "measure.jaffle.item_revenue_usd" in replace_measure[0]["message"]
        assert "measure.jaffle.item_revenue_usd" in list(
            replace_measure[0].get("compatible_measures") or []
        )
        closest_query = dict(replace_measure[0].get("closest_valid_query") or {})
        assert err["closest_valid_query"] == closest_query
        assert closest_query["select"][0]["expression"]["measure"] == (
            "measure.jaffle.item_revenue_usd"
        )
        assert closest_query["group_by"] == ["dimension.jaffle_product_name"]
        assert runtime.validate(closest_query)["ok"] is True

        allocation = [h for h in hints if h.get("kind") == "requires_allocation_policy"]
        assert allocation, "fan-out attribution must explicitly call out allocation semantics"
        assert "allocate" in allocation[0]["message"]
    finally:
        runtime.close()


def test_revenue_by_item_product_name_also_suggests_item_revenue(runtime_factory) -> None:
    """The item-grain spelling of the product dimension hits the same
    wall and must carry the same escape route."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(
            runtime,
            measure="measure.jaffle.revenue_usd",
            group_by=["dimension.jaffle_item_product_name"],
        )
        err = _first_error(report)
        assert err["code"] == "MIXED_GRAIN_INVALID"
        details = err.get("details") or {}
        assert "measure.jaffle.item_revenue_usd" in list(details.get("compatible_measures") or [])
    finally:
        runtime.close()


def test_calendar_dimension_group_by_carries_time_grain_hint(runtime_factory) -> None:
    """Grouping an order measure by dimension.jaffle_time_month_start
    must point the agent at time.grain=month instead of brute force."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(
            runtime,
            measure="measure.jaffle.order_count",
            group_by=["dimension.jaffle_time_month_start"],
        )
        err = _first_error(report)
        assert err["code"] == "MIXED_GRAIN_INVALID"
        details = err.get("details") or {}

        time_axis = dict(details.get("time_axis_recovery") or {})
        assert time_axis.get("calendar_dimension") == "dimension.jaffle_time_month_start"
        assert time_axis.get("grain") == "month"
        # No time block in the query — the measure's default role is used.
        assert time_axis.get("temporal_role") == "temporal_role.jaffle_order_time"

        hints = list(err.get("recovery_hints") or [])
        assert hints, "recovery_hints must be populated"
        kinds = [h.get("kind") for h in hints]
        assert "use_time_grain" in kinds
        # The time-axis recovery is the primary escape route — first hint.
        assert kinds[0] == "use_time_grain"
        time_hint = hints[kinds.index("use_time_grain")]
        assert "time.grain" in time_hint["message"]
        assert time_hint.get("time", {}).get("grain") == "month"
        closest_query = dict(time_hint.get("closest_valid_query") or {})
        assert closest_query["time"] == {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
        }
        assert "dimension.jaffle_time_month_start" not in list(closest_query.get("group_by") or [])
        assert runtime.validate(closest_query)["ok"] is True
    finally:
        runtime.close()


def test_calendar_dimension_with_existing_time_block_keeps_query_role(runtime_factory) -> None:
    """When the query already has a time block, the hint reuses its
    temporal role instead of inventing a new one."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(
            runtime,
            measure="measure.jaffle.revenue_usd",
            group_by=["dimension.jaffle_time_month_start"],
            time={"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
        )
        err = _first_error(report)
        assert err["code"] == "MIXED_GRAIN_INVALID"
        time_axis = dict((err.get("details") or {}).get("time_axis_recovery") or {})
        assert time_axis.get("temporal_role") == "temporal_role.jaffle_order_time"
        assert time_axis.get("grain") == "month"
    finally:
        runtime.close()


def test_enrichment_lists_are_bounded_and_registry_only(runtime_factory) -> None:
    """Both suggestion lists stay within the suggestion cap and every
    entry is a real registry id — the enrichment never touches the
    warehouse, so all suggestions must resolve against config metadata."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _validate(
            runtime,
            measure="measure.jaffle.revenue_usd",
            group_by=["dimension.jaffle_product_name"],
        )
        err = _first_error(report)
        details = err.get("details") or {}
        measure_ids = {m.id for m in runtime.config.measures}
        dimension_ids = {d.id for d in runtime.config.dimensions}

        compatible_measures = list(details.get("compatible_measures") or [])
        compatible_dimensions = list(details.get("compatible_dimensions") or [])
        assert len(compatible_measures) <= MAX_SUGGESTIONS
        assert len(compatible_dimensions) <= MAX_SUGGESTIONS
        assert set(compatible_measures) <= measure_ids
        assert set(compatible_dimensions) <= dimension_ids
        # The requested measure is never suggested back to the agent.
        assert "measure.jaffle.revenue_usd" not in compatible_measures
    finally:
        runtime.close()
