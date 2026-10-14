"""A time block with only a temporal role used to return one row per distinct timestamp,
where the caller expected one number. It is refused with the fix that adds a grain; a window
without a grain is one total (``test_window_total.py``)."""

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
        assert "time.grain" in hint["message"]
        patched = {**query, "time": {**query["time"], "grain": "month"}}
        assert runtime.validate(patched)["ok"] is True
    finally:
        runtime.close()
