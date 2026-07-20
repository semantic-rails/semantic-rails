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

from tests.plan_candidate_envelope import plan_candidate_envelope

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
    expression = dict(selects[0].get("expression", {}) or {})
    return str(expression.get("measure") or expression.get("metric") or "")


def _candidate_group_by(candidate: dict[str, Any]) -> list[str]:
    ir = dict(candidate.get("candidate_ir", {}) or {})
    return [str(item) for item in (ir.get("group_by") or [])]


def _has_derivative_marker(object_id: str) -> bool:
    lowered = object_id.lower()
    return any(marker in lowered for marker in _DERIVATIVE_MARKERS)


def test_monthly_revenue_returns_base_revenue_measure(runtime_factory) -> None:
    """Plain "monthly revenue" should resolve to ``revenue_usd``, not
    a MoM/growth/ratio derivative. Token "month" is consumed by the
    time-grain extractor and must not contribute to candidate scoring.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent="monthly revenue", limit=5)
        top = _top_candidate(payload)
        top_id = _candidate_measure_id(top)
        assert top_id == "measure.jaffle.revenue_usd", (
            f"expected revenue_usd at rank 0, got {top_id!r}"
        )
        assert not _has_derivative_marker(top_id), (
            f"top candidate {top_id!r} matches a derivative marker"
        )
    finally:
        runtime.close()


def test_revenue_by_store_last_month_returns_revenue_usd(runtime_factory) -> None:
    """ "revenue by store last month" must resolve to ``revenue_usd``
    grouped by store — not the ``drink_revenue_share`` mix metric whose
    ``comparison_family`` is ``product_mix_share``.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent="revenue by store last month", limit=5)
        top = _top_candidate(payload)
        top_id = _candidate_measure_id(top)
        assert top_id == "measure.jaffle.revenue_usd", (
            f"expected revenue_usd at rank 0, got {top_id!r}"
        )
        # Share/ratio derivatives must not surface at top for an unqualified
        # "revenue" intent.
        assert "share" not in top_id.lower()
        assert "ratio" not in top_id.lower()
    finally:
        runtime.close()


def test_drink_revenue_by_month_returns_drink_revenue_usd(runtime_factory) -> None:
    """A qualified "drink revenue" intent should resolve to the
    ``drink_revenue_usd`` measure, not a share / ratio."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent="drink revenue by month", limit=5)
        top = _top_candidate(payload)
        top_id = _candidate_measure_id(top)
        assert top_id == "measure.jaffle.drink_revenue_usd", (
            f"expected drink_revenue_usd at rank 0, got {top_id!r}"
        )
        assert "share" not in top_id.lower()
        assert "ratio" not in top_id.lower()
    finally:
        runtime.close()


def test_food_revenue_last_quarter_returns_food_revenue_usd(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent="food revenue last quarter", limit=5)
        top = _top_candidate(payload)
        top_id = _candidate_measure_id(top)
        assert top_id == "measure.jaffle.food_revenue_usd", (
            f"expected food_revenue_usd at rank 0, got {top_id!r}"
        )
        assert "share" not in top_id.lower()
        assert "ratio" not in top_id.lower()
    finally:
        runtime.close()


def test_orders_by_customer_status_groups_or_flags_assumption(runtime_factory) -> None:
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
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent="orders by customer status", limit=5)
        candidates = list(payload.get("candidates") or [])
        if not candidates:
            blocked = list(payload.get("blocked") or [])
            assert any(
                row.get("code") == "PLAN_FALLBACK_SEMANTIC_DRIFT"
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
    finally:
        runtime.close()


def test_revenue_this_month_vs_last_month_uses_revenue_usd(runtime_factory) -> None:
    """A "vs" comparison intent on plain revenue must reference
    ``revenue_usd`` — never the ``drink_revenue_share`` mix metric.

    The plan path may emit the comparison as:
      (a) a ``comparison_bundle`` (categorical comparison branch),
      (b) a same-query top candidate (inline period-shift draft), or
      (c) a blocked entry that names the right measure (when a
          validation rule rejects the windowed/period-shift IR).

    In any of these paths the resolved measure must be the base
    ``revenue_usd`` — never a ``share`` / ``ratio`` derivative.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(
            runtime,
            intent="revenue this month vs last month",
            limit=5,
        )
        bundle = list(payload.get("comparison_bundle") or [])
        candidates = list(payload.get("candidates") or [])
        blocked = list(payload.get("blocked") or [])

        observed_ids: set[str] = set()
        for row in bundle:
            patch = dict(row.get("query_patch") or {})
            for sel in patch.get("select") or []:
                expr = dict(sel.get("expression") or {})
                ident = str(expr.get("measure") or expr.get("metric") or "")
                if ident:
                    observed_ids.add(ident)
        for candidate in candidates[:1]:
            ir = dict(candidate.get("candidate_ir") or {})
            for sel in ir.get("select") or []:
                expr = dict(sel.get("expression") or {})
                ident = str(expr.get("measure") or expr.get("metric") or "")
                if ident:
                    observed_ids.add(ident)
        # The first blocked entry is the canonical draft resolution
        # when validation rejects it — read ``resolved_objects`` since
        # the default ``compact`` verbosity strips ``candidate_ir`` from
        # blocked rows.
        for blocked_row in blocked[:1]:
            for ident in blocked_row.get("resolved_objects") or []:
                if ident:
                    observed_ids.add(str(ident))
            ir = dict(blocked_row.get("candidate_ir") or {})
            for sel in ir.get("select") or []:
                expr = dict(sel.get("expression") or {})
                ident_ = str(expr.get("measure") or expr.get("metric") or "")
                if ident_:
                    observed_ids.add(ident_)

        assert "measure.jaffle.revenue_usd" in observed_ids, (
            f"expected revenue_usd to appear in comparison_bundle, top "
            f"candidate, or first blocked; observed={sorted(observed_ids)!r}"
        )
        # Top candidate must not be ONLY a share/ratio derivative.
        if candidates:
            top_ir = dict(candidates[0].get("candidate_ir") or {})
            top_idents: set[str] = set()
            for sel in top_ir.get("select") or []:
                expr = dict(sel.get("expression") or {})
                ident = str(expr.get("measure") or expr.get("metric") or "")
                if ident:
                    top_idents.add(ident)
            non_derivative_top = {
                ident for ident in top_idents if not _has_derivative_marker(ident)
            }
            # When share/ratio sneak through, we want at least one base
            # measure ALSO present in the top candidate or surrounding
            # context (candidates/bundle/blocked combined).
            if not non_derivative_top:
                non_derivative_any = {
                    ident for ident in observed_ids if not _has_derivative_marker(ident)
                }
                assert non_derivative_any, (
                    f"no base-measure reference anywhere; observed={sorted(observed_ids)!r}"
                )
    finally:
        runtime.close()
