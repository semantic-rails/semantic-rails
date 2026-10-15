"""Regression tests for the plan ranker — derivative/grain priority.

Blind-agent benchmarks caught two consistent failure modes:

1. For a plain intent like "monthly revenue", the ranker promoted
   ``measure.jaffle.month_over_month_revenue_growth_ratio`` (a
   derivative ratio) or ``metric.sales.month_over_month_revenue_growth``
   to rank 0, because the token "month" appeared in both the intent
   grain slot and the derivative's name — the ranker double-counted
   it.

2. For "revenue by store last month", the ranker returned
   ``metric.sales.drink_revenue_share`` (a share metric whose
   ``comparison_family`` is ``product_mix_share``) instead of the base
   ``revenue_usd`` measure grouped by store.

End-to-end these produced 0-row successful executes with no warning —
the worst failure mode for an agent. These tests pin the ranker so
plain intents resolve to base measures and derivatives only surface
when the intent text explicitly invokes a comparison/derivative
keyword.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.plan_candidate_envelope import plan_candidate_envelope
from tests.semantic_rails.planner_fixtures import jaffle_runtime

_DERIVATIVE_MARKERS = (
    "ratio",
    "share",
    "growth",
    "conversion",
    "mom",
    "yoy",
)


def _top_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = list(payload.get("candidates", []) or [])
    assert candidates, f"expected at least one candidate; payload keys={sorted(payload.keys())}"
    return candidates[0]


def _status_like_dimensions(values: list[str]) -> list[str]:
    return [
        dim
        for dim in values
        if "status" in dim.lower()
        or "state" in dim.lower()
        or "membership" in dim.lower()
        or "segment" in dim.lower()
        or "type" in dim.lower()
    ]


def _candidate_measure_id(candidate: dict[str, Any]) -> str:
    """Return the measure (or metric) id of the first select expression."""
    ir = dict(candidate.get("candidate_ir", {}) or {})
    selects = list(ir.get("select", []) or [])
    if not selects:
        return ""
    return _ref(selects[0])


def _ref(item: Any) -> str:
    """What a select item reads: an id string, or a kind object's measure or metric."""
    if isinstance(item, str):
        return item
    expression = item.get("expression")
    if isinstance(expression, str):
        return expression
    return str((expression or {}).get("measure") or (expression or {}).get("metric") or "")


def _candidate_group_by(candidate: dict[str, Any]) -> list[str]:
    ir = dict(candidate.get("candidate_ir", {}) or {})
    return [str(item) for item in (ir.get("group_by") or [])]


def _has_derivative_marker(object_id: str) -> bool:
    lowered = object_id.lower()
    return any(marker in lowered for marker in _DERIVATIVE_MARKERS)


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


@pytest.mark.parametrize(
    ("intent", "expected_measure"),
    [
        # "month" is consumed by the time-grain extractor and adds nothing to scoring.
        pytest.param("monthly revenue", "measure.jaffle.revenue_usd", id="monthly-revenue"),
        pytest.param(
            "revenue by store last month", "measure.jaffle.revenue_usd", id="revenue-by-store"
        ),
        pytest.param(
            "drink revenue by month", "measure.jaffle.drink_revenue_usd", id="drink-revenue"
        ),
        pytest.param(
            "food revenue last quarter", "measure.jaffle.food_revenue_usd", id="food-revenue"
        ),
    ],
)
def test_revenue_intent_returns_base_measure(runtime, intent, expected_measure) -> None:
    payload = plan_candidate_envelope(runtime, intent=intent, limit=5)
    top_id = _candidate_measure_id(_top_candidate(payload))
    assert top_id == expected_measure, f"expected {expected_measure} at rank 0, got {top_id!r}"
    assert not _has_derivative_marker(top_id)


def test_orders_by_customer_status_groups_or_flags_assumption(runtime) -> None:
    """For "orders by customer status", either:
      (a) the top candidate's ``group_by`` includes a status-like
          dimension, or
      (b) the payload's ``plan.assumptions[]`` (or top candidate's
          ``assumptions[]``) flags that no status dimension could be
          matched.

    This is a softer assertion than the others because there is no
    canonical ``customer_status`` dimension in jaffle_shop — the closest
    is ``membership_status``. The test enforces the graceful-fallback
    contract: the ranker must either surface a sensible status-like
    dimension or signal the gap explicitly.
    """
    payload = plan_candidate_envelope(runtime, intent="orders by customer status", limit=5)
    candidates = list(payload.get("candidates") or [])
    if not candidates:
        blocked = list(payload.get("blocked") or [])
        assert any(
            row.get("code") in {"PLAN_FALLBACK_SEMANTIC_DRIFT", "PLAN_UNCONNECTED"}
            and _status_like_dimensions(
                [str(item) for item in list(row.get("resolved_objects") or [])]
            )
            for row in blocked
        ), (
            "expected a status-like blocked candidate with semantic-drift diagnostics; "
            f"blocked={blocked!r}"
        )
        return
    top = _top_candidate(payload)
    group_by = _candidate_group_by(top)
    status_like = _status_like_dimensions(group_by)
    if status_like:
        return  # branch (a) — sensible grouping picked
    # Branch (b) — assumption flag must mention the missing status dim.
    payload_assumptions = [str(item).lower() for item in (payload.get("assumptions") or [])]
    candidate_assumptions = [str(item).lower() for item in (top.get("assumptions") or [])]
    all_assumptions = payload_assumptions + candidate_assumptions
    assert any("status" in note for note in all_assumptions), (
        "expected either a status-like group_by or an assumption noting "
        f"the missing status dimension; group_by={group_by!r} "
        f"assumptions={all_assumptions!r}"
    )
