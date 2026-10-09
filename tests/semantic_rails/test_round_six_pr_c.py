"""Round-six PR C contracts — diagnostic warnings.

B7 — A query with ``time.temporal_role`` set but no ``time.grain``,
     no ``group_by``, and no inline window expression used to silently
     return one row per distinct timestamp. The agent expected a
     scalar; the blind-agent benchmark saw 5,480 rows for a single
     revenue number. Now we emit ``UNGRAINED_TIME_PROJECTION`` so the
     agent knows to set a grain.
"""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError


def test_time_with_only_a_temporal_role_is_refused_with_the_grain_fix(runtime_factory):
    """The exact shape that surprised the blind agent: select a measure,
    set temporal_role to bound the time window, but forget to set grain.
    The call is refused before any row is read, and its fix adds a grain."""
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "select": [
                {
                    "expression": {
                        "kind": "measure",
                        "measure": "measure.jaffle.revenue_usd",
                        "aggregation": "sum",
                    },
                    "as": "revenue",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time"},
        }
        with pytest.raises(SemanticLayerError) as exc:
            runtime.compile(query)
        assert exc.value.code == "INVALID_QUERY"
        assert exc.value.details["path"] == "time"
        (hint,) = exc.value.details["recovery_hints"]
        assert '"grain"' in hint["message"]
        patched = {**query, "time": {**query["time"], "grain": "month"}}
        assert runtime.validate(patched)["ok"] is True
    finally:
        runtime.close()


def test_ungrained_warning_suppressed_when_grain_is_set(runtime_factory):
    """Setting time.grain — the agent's first option — must clear the warning."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "revenue",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "start": "2017-01-01",
                    "end": "2017-04-01",
                },
            }
        )
        warnings = result.get("warnings") or []
        codes = [w.get("code") for w in warnings]
        assert "UNGRAINED_TIME_PROJECTION" not in codes, (
            f"warning should NOT fire when grain is set; got codes={codes!r}"
        )
    finally:
        runtime.close()


def test_ungrained_warning_suppressed_when_group_by_present(runtime_factory):
    """When the agent groups by a dimension, the row-stream is intentional
    (per-dim×timestamp). Don't fire on legitimate detail queries."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.compile(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "revenue",
                    }
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "start": "2017-01-01",
                    "end": "2017-02-01",
                },
            }
        )
        warnings = result.get("warnings") or []
        codes = [w.get("code") for w in warnings]
        assert "UNGRAINED_TIME_PROJECTION" not in codes, (
            f"warning should NOT fire when group_by present; got codes={codes!r}"
        )
    finally:
        runtime.close()


def test_ungrained_warning_suppressed_for_inline_window_expressions(runtime_factory):
    """Inline window kinds (prior_period / rolling / cumulative /
    period_to_date / conversion / offset_window) carry their own time
    semantics — the warning must be suppressed even WITHOUT an outer
    time.grain. The earlier version of this test set grain=month and
    so never exercised the inline-window branch — the grain check
    short-circuited first. This version drops grain so the suppression
    walker is the only thing keeping the warning quiet.
    """
    from semantic_rails.runtime import _ungrained_time_projection_warnings

    # Direct unit test on the warning emitter — bypasses the live
    # compile path (which would reject a bounded start with a
    # WINDOWED_TIME_FILTER_UNSUPPORTED error before we get to check
    # warnings). Pins the suppression contract precisely.
    payload = {
        "select": [
            {
                "expression": {
                    "kind": "prior_period",
                    "input": "measure.jaffle.revenue_usd",
                    "offset": {"unit": "month", "value": 1},
                },
                "as": "prior_month_revenue",
            }
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            # grain intentionally absent — the inline window kind
            # must suppress the warning on its own.
        },
    }
    warnings = _ungrained_time_projection_warnings(payload)
    assert warnings == [], (
        f"inline prior_period must suppress UNGRAINED_TIME_PROJECTION; got {warnings!r}"
    )

    # Confirm the negative: same payload with a non-window expression
    # DOES fire (sanity check that the test isn't passing for the
    # wrong reason).
    payload_negative = dict(payload)
    payload_negative["select"] = [
        {
            "expression": {
                "aggregation": "sum",
                "measure": "measure.jaffle.revenue_usd",
            },
            "as": "rev",
        }
    ]
    negative_warnings = _ungrained_time_projection_warnings(payload_negative)
    assert any(w.get("code") == "UNGRAINED_TIME_PROJECTION" for w in negative_warnings), (
        f"plain aggregate without grain MUST fire the warning; got {negative_warnings!r}"
    )


def test_ungrained_warning_suppressed_for_nested_inline_window():
    """Suppression must walk nested expressions — a `ratio` whose
    numerator is a `rolling` window is still semantically an inline-
    windowed query and grain isn't required on the outer query. The
    earlier shallow check only looked at the top-level expression kind
    and would fire spuriously here."""
    from semantic_rails.runtime import _ungrained_time_projection_warnings

    payload = {
        "select": [
            {
                "expression": {
                    "kind": "ratio",
                    "numerator": {
                        "kind": "rolling",
                        "input": "measure.jaffle.revenue_usd",
                        "window": {"unit": "day", "value": 7},
                    },
                    "denominator": {
                        "kind": "measure",
                        "aggregation": "sum",
                        "measure": "measure.jaffle.revenue_usd",
                    },
                },
                "as": "rolling_share",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time"},
    }
    warnings = _ungrained_time_projection_warnings(payload)
    codes = [w.get("code") for w in warnings]
    assert "UNGRAINED_TIME_PROJECTION" not in codes, (
        f"nested rolling inside ratio must suppress; got {codes!r}"
    )
