"""Pattern: ``inline_comparison``.

Detects "X vs Y" / "X versus Y" intents and emits a single Query IR
with two select columns — one per side. Examples:

* "food vs drink revenue"
* "revenue versus order count by store"

Most of these intents historically went through a multi-query
``comparison_bundle``. For the plan surface we prefer a single-IR shape
because the agent gets a named pattern hint and a ready-to-validate IR
instead of merging two side queries.

"""

from __future__ import annotations

import re
from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _add_order,
    _aggregation_from_text,
    _maybe_group_by,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _runtime_composition_terms,
    _semantic_token,
    _time_spec,
)
from ._protocol import IntentPattern

_VS_RE = re.compile(r"^(?P<left>.+?)\s+(?:vs|versus)\s+(?P<right>.+?)$", re.IGNORECASE)


_SHARE_HINT_TOKENS = frozenset({"share", "pct", "percent", "percentage", "ratio", "%"})


def _resolve_side(
    config: Any, side_text: str, *, prefer_share_metric: bool
) -> tuple[Any, bool] | None:
    """Resolve one side of the comparison to a catalog object.

    Default: measure first (cleanest scope semantics), then metric.

    When ``prefer_share_metric=True`` (intent mentions share / % /
    ratio), we look for a metric recipe whose id contains a
    distinguishing token from the side text BEFORE falling back to
    ``_preferred_measure``. This fixes the blind-agent failure on
    "food vs drink revenue share" where ``_preferred_measure``'s
    hardcoded ``revenue_usd`` preference outranked
    ``food_revenue_share`` / ``drink_revenue_share`` even though the
    IR's own subject ranker had scored both shares at 1.0.

    Returns ``(row, is_measure)`` or ``None``.
    """

    side_terms = _runtime_composition_terms(side_text)
    if not side_terms:
        return None
    # Content tokens — strip the share/ratio hint words so the
    # "distinguishing" search uses just food / drink / email / etc.
    content = side_terms - _SHARE_HINT_TOKENS
    if prefer_share_metric and content:
        metric = _share_metric_with_tokens(config, content)
        if metric is not None:
            return metric, False
    measure = _preferred_measure(config, side_terms)
    if measure is not None:
        return measure, True
    metric = _preferred_metric(config, side_terms)
    if metric is not None:
        return metric, False
    return None


def _share_metric_with_tokens(config: Any, content_tokens: set[str]) -> Any | None:
    """Find a share/ratio metric whose id matches the side's content
    tokens.

    Earlier revision required ALL content tokens to appear in the
    metric's haystack, which was too strict for natural English
    phrasing — "what share of revenue comes from food" carries the
    distinguishing token ("food") plus a lot of common context
    ("what", "share", "comes", "from"). Round-2 blind-agent test
    surfaced this: ``subject_ids_used`` correctly showed the left
    side fell back to ``revenue_usd`` instead of
    ``food_revenue_share``.

    Updated heuristic:
      1. Metric id must contain at least one share-style marker.
      2. Score by number of side tokens that appear in the id
         (label-only matches are ignored — too noisy; metric ids
         are explicit by convention).
      3. Drop candidates with zero id-token matches.
      4. Pick the highest scorer; ties broken by id length.
    """

    share_id_markers = ("_share", "_pct", "_ratio", "_percent")
    candidates: list[tuple[int, int, str, Any]] = []
    for row in config.metric_recipes:
        row_id = getattr(row, "id", "").lower()
        if not any(marker in row_id for marker in share_id_markers):
            continue
        match_count = sum(1 for token in content_tokens if token in row_id)
        if match_count == 0:
            continue
        # Sort key: highest match_count, then shortest id (canonical),
        # then label tiebreaker.
        candidates.append((-match_count, len(row_id), getattr(row, "label", ""), row))
    candidates.sort()
    return candidates[0][3] if candidates else None


def _coordinated_clock_comparison(
    config: Any, intent_tokens: set[str]
) -> RuntimeCompositionDraft | None:
    if not {"delivered", "order", "revenue"} <= intent_tokens or "month" not in intent_tokens:
        return None
    delivered = next(
        (row for row in config.measures if row.id == "measure.jaffle.delivered_revenue_usd"),
        None,
    )
    ordered = next((row for row in config.measures if row.id == "measure.jaffle.revenue_usd"), None)
    if delivered is None or ordered is None:
        return None
    query = {
        "version": 2,
        "select": [
            {
                "as": "delivered_revenue_usd",
                "expression": {
                    "measure": delivered.id,
                    "aggregation": _aggregation_from_text("", set(), delivered),
                },
            },
            {
                "as": "ordered_revenue_usd",
                "expression": {
                    "measure": ordered.id,
                    "aggregation": _aggregation_from_text("", set(), ordered),
                },
            },
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
        },
    }
    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(delivered), _resolved(ordered)],
        rationale=[
            "detected a delivered-vs-ordered clock comparison that needs coordinated queries",
        ],
        interpreted_intent={
            "pattern": "inline_comparison",
            "source": "plan",
            "comparison_mode": "coordinated_queries",
            "left": {"id": delivered.id, "kind": "measure", "alias": "delivered_revenue_usd"},
            "right": {"id": ordered.id, "kind": "measure", "alias": "ordered_revenue_usd"},
        },
        blocked_reason={
            "code": "COMPARISON_COORDINATED",
            "message": (
                "Delivered-month versus ordered-month revenue comparison requires coordinated "
                "queries because each side uses a different temporal role."
            ),
            "details": {
                "left_temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
                "right_temporal_role": "temporal_role.jaffle_order_time",
            },
            "recovery_hints": [
                {
                    "kind": "run_coordinated_queries",
                    "message": (
                        "Run one query on delivered revenue by delivered month and one on "
                        "ordered revenue by order month, then compare the aligned outputs."
                    ),
                }
            ],
        },
    )


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    match = _VS_RE.match(text.strip())
    if not match:
        return None

    config = runtime.config
    left_text = match.group("left").strip()
    right_text = match.group("right").strip()
    # Whole-intent token bag — the share/ratio hint can sit on either
    # side ("food vs drink share" vs "food revenue share vs drink").
    intent_tokens = terms | _runtime_composition_terms(text)
    prefer_share = bool(intent_tokens & _SHARE_HINT_TOKENS)
    coordinated = _coordinated_clock_comparison(config, intent_tokens)
    if coordinated is not None:
        return coordinated

    left = _resolve_side(config, left_text, prefer_share_metric=prefer_share)
    right = _resolve_side(config, right_text, prefer_share_metric=prefer_share)
    if left is None or right is None:
        return None
    if left[0].id == right[0].id:
        return None

    left_row, left_is_measure = left
    right_row, right_is_measure = right

    # Use whichever side carries a temporal role for the time spec.
    role = ""
    for row, is_measure in (left, right):
        candidate = (
            str(getattr(row, "default_temporal_role", "") or "")
            if is_measure
            else str(getattr(row, "temporal_role", "") or "")
        )
        if candidate:
            role = candidate
            break
    time_spec = _time_spec(role, text) if role else None

    def _expression(row: Any, is_measure: bool) -> dict[str, Any]:
        if is_measure:
            return {
                "measure": row.id,
                "aggregation": _aggregation_from_text(text, terms, row),
            }
        return {"metric": row.id}

    left_alias = _semantic_token(left_row.id, fallback="left")
    right_alias = _semantic_token(right_row.id, fallback="right")
    if left_alias == right_alias:
        right_alias = f"{right_alias}_alt"

    query: dict[str, Any] = {
        "version": 2,
        "select": [
            {"as": left_alias, "expression": _expression(left_row, left_is_measure)},
            {"as": right_alias, "expression": _expression(right_row, right_is_measure)},
        ],
    }
    if time_spec is not None:
        query["time"] = time_spec
    group_by = _maybe_group_by(config, text)
    if group_by:
        query["group_by"] = group_by
    _add_order(query)

    resolved = [_resolved(left_row), _resolved(right_row)]
    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=[
            "composed inline comparison: emitted both sides as columns "
            "of a single Query IR for side-by-side review",
        ],
        interpreted_intent={
            "pattern": "inline_comparison",
            "source": "plan",
            "left": {
                "id": left_row.id,
                "kind": "measure" if left_is_measure else "metric",
                "alias": left_alias,
            },
            "right": {
                "id": right_row.id,
                "kind": "measure" if right_is_measure else "metric",
                "alias": right_alias,
            },
        },
        # Catch-all score so a more specific pattern wins when both
        # fire (e.g. qualified_metric_rollup on "top X vs at least
        # Y" — the more specific pattern should claim it).
        score=0.5,
    )


PATTERN = IntentPattern(
    name="inline_comparison",
    match=_match,
)
