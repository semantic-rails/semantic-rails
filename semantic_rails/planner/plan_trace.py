from __future__ import annotations

from typing import Any

from .intent_ir import IntentIR
from .plan_query import _append_unique_dicts, _where_filters


def _select_best_plan(
    planned: list[dict[str, Any]], *, intent_ir: IntentIR | None = None
) -> dict[str, Any]:
    """Pick the best validated draft.

    An explicitly blocked primary draft is itself the planner's best
    semantic answer: it explains why a single executable query would be
    misleading. Otherwise prefer the first validating draft that does
    not drift from a failed primary pattern's intent slots. If none
    validate, keep the first low-confidence draft because registry order
    / fallback order already carries the ranking signal.
    """

    if planned and planned[0].get("blocked"):
        return planned[0]
    primary = planned[0] if planned else None
    for row in planned:
        if bool(row.get("validation", {}).get("ok")):
            if (
                primary is not None
                and row is not primary
                and not bool(primary.get("validation", {}).get("ok"))
            ):
                drift = _fallback_semantic_drift(primary, row, intent_ir=intent_ir)
                if drift is not None:
                    row["semantic_drift"] = drift
                    continue
            return row
    return planned[0]


def _slim_best(
    draft: Any,
    *,
    pattern: str,
    validation_ok: bool | None,
    intent_ir: IntentIR | None = None,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one draft with canonical Query IR and unique resolved rows."""

    out: dict[str, Any] = {
        "pattern": pattern,
        "query_ir": draft.query,
        "resolved": _append_unique_dicts([], draft.resolved),
        "rationale": list(dict.fromkeys(draft.rationale)),
        "interpreted_intent": draft.interpreted_intent,
        # Audit trail: catalog ids the pattern actually wove into the
        # IR. Lets the agent compare against intent_ir.subjects and
        # catch silent divergence (the blind-agent usability test
        # surfaced inline_comparison picking revenue_usd over the
        # subjects ranker's preferred food/drink_revenue_share).
        "subject_ids_used": _extract_subject_ids(draft),
    }
    if validation_ok is not None:
        out["validation_ok"] = bool(validation_ok)
    out["trace"] = _plan_trace(
        intent_ir=intent_ir,
        draft=draft,
        validation_ok=validation_ok,
        fallback=fallback,
    )
    return out


def _query_detail_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the opt-in compact plan payload for direct QA execution.

    The planner still runs through the same composition, validation,
    fallback, and honesty gates. Only the response projection changes.
    """

    out: dict[str, Any] = {}
    # compose_hints is present only when no draft exists; its why points at it.
    for key in (
        "plan_version",
        "intent",
        "status",
        "why",
        "tie_break_hints",
        "assumptions",
        "warnings",
        "compose_hints",
    ):
        if key in payload:
            out[key] = payload[key]
    out["best"] = _query_detail_best(payload.get("best"))
    return out


def _query_detail_best(best: Any) -> dict[str, Any] | None:
    if not isinstance(best, dict):
        return None
    return {
        key: best[key]
        for key in ("pattern", "query_ir", "resolved", "validation_ok", "subject_ids_used")
        if key in best
    }


def _extract_subject_ids(draft: Any) -> list[str]:
    """Walk the draft's select expressions and surface every catalog
    id (measure / metric / scoped_aggregate inner measure) that ends
    up in the IR.

    Lets the agent reconcile ``best.query_ir`` against
    ``intent_ir.subjects`` without parsing the IR themselves. Order
    matches first-seen-in-select.
    """

    ids: list[str] = []
    seen: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("measure", "metric"):
                value = node.get(key)
                if isinstance(value, str) and value and value not in seen:
                    seen.add(value)
                    ids.append(value)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(getattr(draft, "query", {}))
    return ids


def _projected_subject_ids(query: dict[str, Any]) -> list[str]:
    """Return the top-level semantic objects projected by a query.

    Unlike :func:`_extract_subject_ids`, this intentionally does not
    include predicate inputs nested inside scoped aggregates. The target
    slot is the answer's subject; qualification/cohort objects are
    tracked separately.
    """

    ids: list[str] = []
    seen: set[str] = set()
    for item in list((query or {}).get("select") or []):
        if not isinstance(item, dict):
            continue
        expr = item.get("expression")
        if not isinstance(expr, dict):
            continue
        object_id = ""
        if expr.get("metric"):
            object_id = str(expr.get("metric") or "")
        elif expr.get("measure"):
            object_id = str(expr.get("measure") or "")
        if object_id and object_id not in seen:
            seen.add(object_id)
            ids.append(object_id)
    return ids


def _object_ids_in_node(node: Any) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("measure", "metric", "entity", "dimension", "field"):
                item = value.get(key)
                if isinstance(item, str) and "." in item and item not in seen:
                    seen.add(item)
                    ids.append(item)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    _walk(child)
        elif isinstance(value, list):
            for child in value:
                _walk(child)

    _walk(node)
    return ids


def _filter_keys(filters: list[dict[str, Any]]) -> set[str]:
    import json

    return {json.dumps(row, sort_keys=True, default=str) for row in filters}


def _query_qualification(query: dict[str, Any]) -> list[str]:
    """Return generic qualification/cohort signals present in Query IR."""

    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    for item in list((query or {}).get("metric_filters") or []):
        if isinstance(item, dict):
            _add("metric_filter")
            for object_id in _object_ids_in_node(item.get("expression", item)):
                _add(object_id)
    for select in list((query or {}).get("select") or []):
        expr = select.get("expression") if isinstance(select, dict) else None
        for predicate in _scoped_predicates(expr):
            _add("scoped_aggregate_predicates")
            for object_id in _object_ids_in_node(predicate):
                _add(object_id)
    return out


def _scoped_predicates(node: Any) -> list[Any]:
    """Return every scoped-aggregate predicate in an expression, including inside a ratio."""

    found: list[Any] = []
    if isinstance(node, dict):
        found.extend(list(node.get("predicates") or []))
        for key, child in node.items():
            if key != "predicates":
                found.extend(_scoped_predicates(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_scoped_predicates(child))
    return found


def _time_scope(query: dict[str, Any]) -> dict[str, Any]:
    time_spec = (query or {}).get("time")
    if not isinstance(time_spec, dict):
        return {}
    return {
        key: time_spec[key]
        for key in ("temporal_role", "grain", "start", "end", "range")
        if time_spec.get(key) not in (None, "", [], {})
    }


def _intent_slots(intent_ir: IntentIR | None, draft: Any | None = None) -> dict[str, Any]:
    """Build a domain-neutral slot summary for an intent/draft pair."""

    query = dict(getattr(draft, "query", {}) or {}) if draft is not None else {}
    ir = intent_ir.to_dict() if intent_ir is not None else {}
    target = _projected_subject_ids(query)
    if not target:
        target = [
            str(row.get("id", ""))
            for row in list(ir.get("subjects") or [])
            if isinstance(row, dict) and row.get("id")
        ][:5]
    grouping = [str(item) for item in list(query.get("group_by") or []) if item]
    if not grouping:
        grouping = [
            str(row.get("id", ""))
            for row in list(ir.get("grouping") or [])
            if isinstance(row, dict) and row.get("id")
        ]
    qualification = _query_qualification(query)
    for group in list(ir.get("qualification_token_groups") or []):
        if isinstance(group, list):
            qualification.extend(str(item) for item in group if item)
    if ir.get("qualification_phrase"):
        qualification.append(str(ir["qualification_phrase"]))
    if ir.get("threshold"):
        qualification.append("threshold")
    return {
        "target": list(dict.fromkeys(target)),
        "grouping": list(dict.fromkeys(grouping)),
        "qualification": list(dict.fromkeys(qualification)),
        "filters": _where_filters(query),
        "time": _time_scope(query) or dict(ir.get("time", {}) or {}),
    }


def _fallback_semantic_drift(
    primary: dict[str, Any], candidate: dict[str, Any], *, intent_ir: IntentIR | None = None
) -> dict[str, Any] | None:
    """Return a why envelope if a fallback answers different intent slots."""

    primary_draft = primary.get("draft")
    candidate_draft = candidate.get("draft")
    primary_slots = _intent_slots(intent_ir, primary_draft)
    candidate_slots = _intent_slots(None, candidate_draft)
    reasons: list[dict[str, Any]] = []

    primary_target = set(primary_slots["target"])
    candidate_target = set(candidate_slots["target"])
    if primary_target and candidate_target and primary_target.isdisjoint(candidate_target):
        reasons.append(
            {
                "kind": "target_changed",
                "expected": sorted(primary_target),
                "actual": sorted(candidate_target),
            }
        )

    primary_grouping = set(primary_slots["grouping"])
    candidate_grouping = set(candidate_slots["grouping"])
    if primary_grouping and not primary_grouping.issubset(candidate_grouping):
        reasons.append(
            {
                "kind": "grouping_dropped",
                "expected": sorted(primary_grouping),
                "actual": sorted(candidate_grouping),
            }
        )

    if primary_slots["qualification"] and not candidate_slots["qualification"]:
        reasons.append(
            {
                "kind": "qualification_dropped",
                "expected": list(primary_slots["qualification"]),
                "actual": [],
            }
        )

    primary_filters = _filter_keys(list(primary_slots["filters"]))
    candidate_filters = _filter_keys(list(candidate_slots["filters"]))
    if primary_filters and not primary_filters.issubset(candidate_filters):
        reasons.append(
            {
                "kind": "filters_dropped",
                "expected": list(primary_slots["filters"]),
                "actual": list(candidate_slots["filters"]),
            }
        )

    primary_time = dict(primary_slots.get("time") or {})
    candidate_time = dict(candidate_slots.get("time") or {})
    if primary_time and not candidate_time:
        reasons.append({"kind": "time_scope_dropped", "expected": primary_time, "actual": {}})

    if not reasons:
        return None
    return {
        "code": "PLAN_FALLBACK_SEMANTIC_DRIFT",
        "message": (
            "The named planner pattern matched the intent but failed validation; "
            "a validating fallback was available, but it changed or dropped one "
            "or more requested intent slots. Review best.query_ir and validate "
            "diagnostics before executing."
        ),
        "details": {
            "primary_pattern": primary.get("pattern", ""),
            "fallback_pattern": candidate.get("pattern", ""),
            "primary_slots": primary_slots,
            "fallback_slots": candidate_slots,
            "reasons": reasons,
        },
        "recovery_hints": [
            {
                "kind": "inspect_primary_failure",
                "message": (
                    "Validate best.query_ir (over MCP, execute with mode 'validate') to see "
                    "why the semantically closest draft failed."
                ),
            },
            {
                "kind": "use_explicit_filter_or_segment",
                "message": (
                    "If this is a cohort/qualification ask, express it as a reachable "
                    "dimension filter, governed segment, or package-authored metric."
                ),
            },
        ],
    }


def _first_fallback_drift_why(
    planned: list[dict[str, Any]], best: dict[str, Any]
) -> dict[str, Any] | None:
    if not planned or best is not planned[0]:
        return None
    if bool(best.get("validation", {}).get("ok")):
        return None
    for row in planned[1:]:
        drift = row.get("semantic_drift")
        if isinstance(drift, dict):
            return drift
    return None


def _fallback_trace(row: dict[str, Any], planned: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        index = planned.index(row)
    except ValueError:
        index = 0
    drift = row.get("semantic_drift") if isinstance(row.get("semantic_drift"), dict) else None
    used = index > 0
    reason = ""
    if drift:
        reason = str(drift.get("message", ""))
    elif used:
        reason = "Validating fallback preserved the primary intent slots."
    return {
        "used": used,
        "semantic_drift": bool(drift),
        "reason": reason,
    }


def _plan_trace(
    *,
    intent_ir: IntentIR | None,
    draft: Any,
    validation_ok: bool | None,
    fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    slots = _intent_slots(intent_ir, draft)
    query = dict(getattr(draft, "query", {}) or {})
    subjects = _projected_subject_ids(query) or _extract_subject_ids(draft)
    group_by = [str(item) for item in list(query.get("group_by") or []) if item]
    filters = _where_filters(query)
    selected: dict[str, Any] = {
        "subjects": subjects,
        "group_by": group_by,
        "filters": filters,
        "paths": [],
    }
    fallback_block = dict(fallback or {"used": False, "semantic_drift": False, "reason": ""})
    status = "validated" if validation_ok else "needs validation"
    slot_targets = [str(item) for item in list(slots.get("target") or [])]
    target_label = ", ".join(subjects or slot_targets or ["unresolved target"])
    grouping_label = ", ".join(group_by or ["no grouping"])
    filter_count = len(filters)
    markdown = (
        "### Semantic Trace\n"
        f"- Target: {target_label}\n"
        f"- Grouping: {grouping_label}\n"
        f"- Filters: {filter_count}\n"
        f"- Status: {status}"
    )
    if fallback_block.get("used"):
        drift_text = " with semantic drift" if fallback_block.get("semantic_drift") else ""
        markdown += f"\n- Fallback: used{drift_text}"
    return {
        "intent_slots": slots,
        "selected": selected,
        "fallback": fallback_block,
        "markdown": markdown,
    }
