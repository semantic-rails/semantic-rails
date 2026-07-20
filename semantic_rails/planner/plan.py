"""Intent planning payload builders.

``plan`` is the single public natural-language intent surface. It
replaces the pre-release split between ``formulate`` / ``propose`` /
``parse-intent`` / ``expand`` with one response contract and an explicit
``detail`` knob.

Workflow shape:

::

    plan(intent, detail="best")              -> {intent_ir, best, status, next}
    plan(intent, detail="full")              -> best + alternatives + blocked
    parse_intent(intent)                     -> internal/debug helper only

The planner shares one composition pipeline (``compose`` in
``orchestrator.py``) so the IR the agent sees in ``plan`` is identical
to the IR consumed internally by patterns.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ..runtime import runtime_request_scope
from .faithfulness import intent_faithfulness_why
from .generators import blocked_object_not_found, fallback_drafts
from .intent_ir import IntentIR, compose_hints, parse_intent
from .orchestrator import compose

_VERSION = 1


# ---------------------------------------------------------------------------
@runtime_request_scope
def plan_payload(
    runtime: Any,
    *,
    intent: Any,
    partial_query: dict[str, Any] | None = None,
    detail: str = "best",
    limit: int = 3,
) -> dict[str, Any]:
    """The public intent-planning entry point.

    Returns the minimal payload an agent needs to take the next workflow
    step by default. ``detail="full"`` includes planner alternatives and
    blocked drafts without exposing separate public tools.

    ::

        {
          "intent_ir":   {...},                     # always present
          "status":      "ok|low_confidence|unrealizable|out_of_scope",
          "best":        {...} | None,
          "why":         {...} | None,              # set when status != ok
          "next":        {"validate":{...}, "valid_values":[...], "ready_for":[...]},
        }
    """

    # Local imports keep the planner package from circular-importing
    # the rest of metadata at load time. Gate helpers live in
    # metadata.py / metadata_parts/relevance.py because they belong to
    # the discover/plan/validate surface, not the planner composition
    # pipeline itself.
    from ..metadata_parts.relevance import (  # noqa: WPS433
        _catalog_token_index,
        _intent_passes_grounding_floor,
        _intent_passes_relevance_floor,
        _low_relevance_block,
        _weak_grounding_block,
        _weak_grounding_tokens,
    )
    from ..metadata_parts.scope_gate import scope_block_payload  # noqa: WPS433
    from ..scope import classify_question  # noqa: WPS433

    if not isinstance(intent, str) or not intent.strip():
        raise SemanticLayerError(
            "INVALID_QUERY",
            "plan requires a non-empty string intent",
            details={
                "path": "intent",
                "expected_type": "non_empty_string",
                "recovery_hints": [
                    {
                        "kind": "provide_intent",
                        "message": "Pass a natural-language analysis intent, for example 'revenue by store'.",
                    }
                ],
            },
        )

    intent_str = intent.strip()
    detail_level = str(detail or "best").lower()
    if detail_level not in {"query", "best", "full", "debug"}:
        detail_level = "best"

    if intent_str:
        classification = classify_question(intent_str)
        if classification.category != "data_query":
            payload = _out_of_scope_envelope(
                intent=intent,
                intent_ir=parse_intent(runtime, intent),
                out_of_scope=scope_block_payload(intent_str, classification),
            )
            return _query_detail_payload(payload) if detail_level == "query" else payload
        catalog_tokens = _catalog_token_index(
            runtime.config,
            search_index=runtime._get_catalog_search_index(),
        )
        passes, overlap = _intent_passes_relevance_floor(intent_str, catalog_tokens)
        if not passes:
            sample = sorted(catalog_tokens)[:30]
            payload = _out_of_scope_envelope(
                intent=intent,
                intent_ir=parse_intent(runtime, intent),
                low_relevance=_low_relevance_block(
                    intent_str, overlap_tokens=overlap, catalog_token_sample=sample
                ),
            )
            return _query_detail_payload(payload) if detail_level == "query" else payload
        grounded, _strong = _intent_passes_grounding_floor(
            intent_str,
            catalog_tokens,
            weak_tokens=_weak_grounding_tokens(runtime.config),
        )
        if not grounded:
            sample = sorted(catalog_tokens)[:30]
            payload = _out_of_scope_envelope(
                intent=intent,
                intent_ir=parse_intent(runtime, intent),
                low_relevance=_weak_grounding_block(
                    intent_str, overlap_tokens=overlap, catalog_token_sample=sample
                ),
            )
            return _query_detail_payload(payload) if detail_level == "query" else payload

    result = compose(runtime, intent)
    intent_ir = result.intent_ir
    draft_rows: list[tuple[Any, str]] = []
    blocked: list[dict[str, Any]] = []
    primary_query_keys: set[str] = set()
    if result.draft is not None:
        draft_rows.append((result.draft, result.pattern))
        primary_query_keys.add(_query_key(result.draft.query))
    if result.draft is None or detail_level in {"full", "debug"}:
        # Full/debug explicitly request alternatives, while a missing
        # primary needs catalog discovery to produce any plan at all.
        draft_rows.extend(
            _distinct_fallback_drafts(
                runtime,
                intent=intent,
                partial_query=partial_query,
                limit=limit,
                excluded_query_keys=primary_query_keys,
            )
        )

    if not draft_rows:
        # Truly unrealizable — no pattern AND no catalog fallback candidate.
        # The IR + compose_hints become the contract for the agent.
        payload = {
            "plan_version": _VERSION,
            "intent": intent,
            "intent_ir": intent_ir.to_dict(),
            "status": "unrealizable",
            "best": None,
            "why": {
                "code": "NO_PATTERN_MATCH",
                "message": (
                    "No built-in pattern realized this intent. Use "
                    "compose_hints to author a Query IR directly, "
                    "then call validate."
                ),
            },
            "compose_hints": compose_hints(intent_ir),
            "next": {
                "discover": {"terms": intent_str},
            },
            "blocked": [blocked_object_not_found(intent_str)] if detail_level != "best" else [],
        }
        return _query_detail_payload(payload) if detail_level == "query" else payload

    planned: list[dict[str, Any]] = []
    for draft, pattern in draft_rows:
        planned.append(_planned_row(runtime, draft, pattern, partial_query, blocked))

    if (
        detail_level in {"query", "best"}
        and result.draft is not None
        and not any(bool(row.get("validation", {}).get("ok")) for row in planned)
        and not any(bool(row.get("blocked")) for row in planned)
    ):
        # The compact response should still choose a validating fallback
        # when a named pattern recognized the intent but produced an
        # invalid draft. Explicitly blocked drafts are different: those
        # carry semantic guidance (for example coordinated comparisons)
        # that a generic fallback must not hide. Only pay this extra
        # validation cost after an ordinary primary validation failure;
        # full/debug already validate all rows.
        for draft, pattern in _distinct_fallback_drafts(
            runtime,
            intent=intent,
            partial_query=partial_query,
            limit=limit,
            excluded_query_keys=primary_query_keys,
        ):
            row = _planned_row(runtime, draft, pattern, partial_query, blocked)
            planned.append(row)
            if bool(row.get("validation", {}).get("ok")):
                break

    best = _select_best_plan(planned, intent_ir=intent_ir)
    best_draft = best["draft"]
    best_validation = best["validation"]
    best_ok = bool(best_validation.get("ok"))
    fallback_drift_why = _first_fallback_drift_why(planned, best)
    # Query validation proves executability, not that every high-confidence
    # clause survived natural-language realization.  Keep the valid draft for
    # inspection but fail closed when its observable structure contradicts or
    # omits a requested clause.
    faithfulness_why = (
        intent_faithfulness_why(
            runtime,
            question=intent_str,
            intent_ir=intent_ir,
            query=best_draft.query,
        )
        if best_ok
        else None
    )
    # Honesty gate: when the intent names a time window the planner
    # detected but could not resolve (and nothing else bounded the
    # query), the draft answers a *different* question than the user
    # asked. Downgrade instead of marking it ready to execute.
    time_why = _unresolved_time_why(intent_str, best_draft.query) if best_ok else None
    # Same honesty principle for intent shape: a conversion/funnel ask
    # answered with a non-conversion metric (e.g. AOV) is a confidently
    # wrong answer, not a best effort. Downgrade and point at the
    # conversion surface instead.
    conversion_why = (
        _conversion_intent_why(runtime, intent_str, best_draft.query)
        if best_ok and time_why is None
        else None
    )
    ready = best_ok and faithfulness_why is None and time_why is None and conversion_why is None
    payload = {
        "plan_version": _VERSION,
        "intent": intent,
        "intent_ir": intent_ir.to_dict(),
        "status": "ok" if ready else "low_confidence",
        "best": _slim_best(
            best_draft,
            pattern=best["pattern"],
            validation_ok=best_ok,
            intent_ir=intent_ir,
            fallback=_fallback_trace(best, planned),
        ),
        "next": _next_block(best_draft.query, ready=ready),
    }
    if fallback_drift_why is not None:
        payload["why"] = fallback_drift_why
    elif faithfulness_why is not None:
        payload["why"] = faithfulness_why
    elif time_why is not None:
        payload["why"] = time_why
    elif conversion_why is not None:
        payload["why"] = conversion_why
    elif not best_ok:
        errors = list(best_validation.get("errors") or [])
        payload["why"] = _trim_why_errors(errors)
        payload["tie_break_hints"] = _slim_recovery_hints(
            list(best_validation.get("recovery_hints") or [])
        )
    if detail_level in {"full", "debug"}:
        payload["alternatives"] = [
            _slim_best(
                row["draft"],
                pattern=row["pattern"],
                validation_ok=bool(row["validation"]["ok"]),
                intent_ir=intent_ir,
                fallback=_fallback_trace(row, planned),
            )
            for row in planned
            if row is not best
        ][: max(0, int(limit or 1) - 1)]
        payload["blocked"] = blocked
    if detail_level == "debug":
        payload["compose_hints"] = compose_hints(intent_ir)
    return _query_detail_payload(payload) if detail_level == "query" else payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _distinct_fallback_drafts(
    runtime: Any,
    *,
    intent: str,
    partial_query: dict[str, Any] | None,
    limit: int,
    excluded_query_keys: set[str],
) -> list[tuple[Any, str]]:
    """Discover fallback drafts only when the caller needs them."""

    return [
        row
        for row in fallback_drafts(
            runtime,
            intent=intent,
            partial_query=partial_query,
            limit=max(1, int(limit or 1)),
        )
        if _query_key(row[0].query) not in excluded_query_keys
    ]


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


def _planned_row(
    runtime: Any,
    draft: Any,
    pattern: str,
    partial_query: dict[str, Any] | None,
    blocked: list[dict[str, Any]],
) -> dict[str, Any]:
    merged_draft = replace(draft, query=_merge_partial_query(draft.query, partial_query))
    if merged_draft.blocked_reason:
        why = dict(merged_draft.blocked_reason)
        blocked.append(
            {
                "pattern": pattern,
                "query_ir": merged_draft.query,
                "resolved": merged_draft.resolved,
                "why": why,
                "recovery_hints": list(why.get("recovery_hints", []) or []),
            }
        )
        return {
            "status": "low_confidence",
            "draft": merged_draft,
            "pattern": pattern,
            "validation": {"ok": False, "errors": [why], "recovery_hints": []},
            "blocked": True,
        }
    validation = _validate_query(runtime, merged_draft.query, partial_query)
    return {
        "status": "ok" if validation["ok"] else "low_confidence",
        "draft": merged_draft,
        "pattern": pattern,
        "validation": validation,
        "blocked": False,
    }


def _merge_partial_query(
    draft_query: dict[str, Any],
    partial_query: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a generated draft with caller-provided Query IR.

    Partial-query preservation is a hard planner invariant: no top-level
    Query IR field supplied by the caller may silently disappear. For
    additive list fields we append generated entries after existing
    caller entries. For scalar/dict fields the caller wins, with ``time``
    merged shallowly so generated temporal roles can still fill missing
    fields.
    """

    partial = dict(partial_query or {})
    policy_context = partial.pop("policy_context", None)
    partial.pop("request_context", None)
    partial.pop("request_id", None)
    merged = dict(draft_query or {})
    for key, value in partial.items():
        if value in (None, "", [], {}):
            continue
        if key in {"select", "where", "metric_filters", "order_by"}:
            merged[key] = _append_unique_dicts(list(value or []), list(merged.get(key, []) or []))
        elif key == "group_by":
            merged[key] = list(
                dict.fromkeys([*list(value or []), *list(merged.get(key, []) or [])])
            )
        elif key == "time" and isinstance(value, dict):
            generated = dict(merged.get("time", {}) or {})
            merged[key] = {**generated, **value}
        else:
            merged[key] = value
    if policy_context:
        merged["policy_context"] = policy_context
    return merged


def _append_unique_dicts(
    existing: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    import json

    out = list(existing)
    seen = {json.dumps(row, sort_keys=True, default=str) for row in out}
    for row in additions:
        key = json.dumps(row, sort_keys=True, default=str)
        if key in seen:
            continue
        out.append(row)
        seen.add(key)
    return out


def _query_key(query: dict[str, Any]) -> str:
    import json

    return json.dumps(query, sort_keys=True, default=str)


def _slim_best(
    draft: Any,
    *,
    pattern: str,
    validation_ok: bool | None,
    intent_ir: IntentIR | None = None,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trim the realized draft to just what an agent needs to call
    ``validate`` next.

    Omits older ``query_patch`` and ``closest_legal_alternatives`` fields
    from the default response. ``detail="full"`` surfaces alternatives
    and blocked drafts when asked.
    """

    out: dict[str, Any] = {
        "pattern": pattern,
        "query_ir": draft.query,
        "resolved": draft.resolved,
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
    for key in ("plan_version", "intent", "status", "why", "tie_break_hints"):
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


def _where_filters(query: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list((query or {}).get("where") or []):
        if not isinstance(row, dict):
            continue
        field = row.get("field") or row.get("dimension")
        if not field:
            continue
        out.append(
            {
                "field": str(field),
                "op": str(row.get("op", "") or ""),
                "value": row.get("value"),
            }
        )
    return out


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
        if not isinstance(expr, dict):
            continue
        predicates = list(expr.get("predicates") or [])
        if predicates:
            _add("scoped_aggregate_predicates")
        for predicate in predicates:
            for object_id in _object_ids_in_node(predicate):
                _add(object_id)
    return out


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
                "message": "Call validate on best.query_ir to see why the semantically closest draft failed.",
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


def _validate_query(
    runtime: Any,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None,
) -> dict[str, Any]:
    """Call ``runtime.validate`` against a pattern draft.

    Returns ``{"ok": bool, "errors": [...], "recovery_hints": [...]}``
    — a small projection of the full validate response that plan needs.
    Runs inline so plan's status field is trustworthy. If
    validate raises (an unexpected runtime error rather than a
    structured failure), we return ``ok=False`` with the exception
    rendered as an error so the agent still gets a structured signal.
    """

    # Cheap structural pre-check — skip the full validate (which
    # compiles the IR) when the draft is obviously malformed. Catches
    # the common pattern bugs without paying compile cost. The full
    # validator still runs on draft IRs that pass these gates.
    structural = _structural_precheck(query)
    if structural is not None:
        return {
            "ok": False,
            "errors": [structural],
            "recovery_hints": [],
        }

    payload = dict(query)
    if partial_query and partial_query.get("policy_context"):
        payload["policy_context"] = partial_query["policy_context"]
    try:
        report = runtime.validate(payload)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure as a validation error
        return {
            "ok": False,
            "errors": [{"code": "VALIDATE_EXCEPTION", "message": str(exc)}],
            "recovery_hints": [],
        }
    return {
        "ok": bool(report.get("ok", False)),
        "errors": list(report.get("errors") or []),
        "recovery_hints": list(report.get("recovery_hints") or []),
    }


def _structural_precheck(query: dict[str, Any]) -> dict[str, Any] | None:
    """Return a structured error if the draft IR is obviously bad.

    Catches the cheap failure modes without paying ``runtime.validate``'s
    compile cost — a missing ``select``, empty selects, a select item
    without an ``expression``. Returning ``None`` means the IR is
    plausible and the full validator should run.

    Deliberately narrow: only catches structural shape errors that no
    legitimate pattern would produce. Catalog-membership checks
    (does this measure id exist?) stay in the full validator where
    they have config access.
    """

    if not isinstance(query, dict):
        return {
            "code": "STRUCTURAL_NOT_A_QUERY",
            "message": "Pattern emitted something that is not a Query IR dict.",
        }
    select = query.get("select")
    if not isinstance(select, list) or not select:
        return {
            "code": "STRUCTURAL_EMPTY_SELECT",
            "message": "Pattern emitted a draft with no select columns.",
        }
    for index, item in enumerate(select):
        if not isinstance(item, dict):
            return {
                "code": "STRUCTURAL_BAD_SELECT_ITEM",
                "message": f"select[{index}] is not a dict.",
            }
        if not item.get("expression"):
            return {
                "code": "STRUCTURAL_MISSING_EXPRESSION",
                "message": f"select[{index}] missing expression.",
            }
    return None


def _unresolved_time_why(intent: str, query: dict[str, Any]) -> dict[str, Any] | None:
    """Return a ``why`` envelope when the intent's time scope got dropped.

    Triggers only when (a) the intent contains a temporal phrase the
    window resolver could not turn into bounds, and (b) the realized IR
    carries no time window from anywhere else (resolved phrase, caller's
    ``partial_query``). The shape mirrors the pattern ``blocked_reason``
    envelope ({code, message, details, recovery_hints}).
    """

    from ._base import _SUPPORTED_WINDOW_FORMS, _unresolved_time_phrases  # noqa: WPS433

    phrases = _unresolved_time_phrases(intent)
    if not phrases:
        return None
    time_spec = query.get("time") if isinstance(query, dict) else None
    if isinstance(time_spec, dict) and any(time_spec.get(key) for key in ("start", "end", "range")):
        return None
    return {
        "code": "TIME_WINDOW_UNRESOLVED",
        "message": (
            "The intent names a time window the planner could not resolve; "
            "best.query_ir is NOT time-bounded and would answer a different "
            "(unbounded) question if executed as-is."
        ),
        "details": {
            "path": "time",
            "unresolved_phrases": list(phrases),
        },
        "recovery_hints": [
            {
                "kind": "rephrase_time_window",
                "message": (
                    "Rephrase the window using a supported form: "
                    + "; ".join(_SUPPORTED_WINDOW_FORMS)
                    + "."
                ),
            },
            {
                "kind": "provide_explicit_bounds",
                "message": (
                    "Or pass explicit bounds via partial_query, e.g. "
                    '{"time": {"start": "2025-01-01", "end": "2025-02-01"}} '
                    "(end-exclusive), or the relative form "
                    '{"time": {"range": {"last": {"unit": "month", "value": 1}}}}.'
                ),
            },
        ],
    }


def _query_contains_conversion(runtime: Any, query: dict[str, Any]) -> bool:
    """True when any expression in the draft is conversion-shaped.

    Covers both ad-hoc ``kind: conversion`` IR and references to curated
    metrics whose authored expression is a conversion.
    """

    from ..expressions import ConversionExpr  # noqa: WPS433

    conversion_metric_ids = {
        str(recipe.id)
        for recipe in getattr(runtime.config, "metric_recipes", []) or []
        if isinstance(recipe.expression, ConversionExpr)
    }

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if str(node.get("kind", "")) == "conversion":
                return True
            metric_ref = str(node.get("metric", "") or node.get("metric_recipe", "") or "")
            if metric_ref and metric_ref in conversion_metric_ids:
                return True
            return any(_walk(value) for value in node.values())
        if isinstance(node, list):
            return any(_walk(item) for item in node)
        return False

    return _walk(query if isinstance(query, dict) else {})


# "converted to euros" is currency talk, not funnel talk — currencies
# are a closed-enough set to carve out.
_CURRENCY_WORDS = r"(?:euros?|eur|dollars?|usd|pounds?|gbp|yen|jpy|cad|aud|chf|local\s+currency)"
_CONVERSION_INTENT_RE_PARTS = (
    r"\bconversion\b",
    rf"\bconverts?\b(?!\s+(?:to|into)\s+{_CURRENCY_WORDS})",
    rf"\bconverted\b(?!\s+(?:to|into)\s+{_CURRENCY_WORDS})",
    r"\bfunnel\b",
    r"\b(?:and|who|customers?)\s+then\s+(?:order|orders|ordered|buy|bought|purchase[ds]?"
    r"|place[ds]?|sign(?:ed|s)?(?:\s+up)?|send[s]?|sent|return(?:ed|s)?)\b",
    r"\bfollowed\s+by\b",
)


def _conversion_intent_markers(intent: str) -> list[str]:
    import re  # noqa: WPS433

    lowered = str(intent or "").lower()
    out: list[str] = []
    for part in _CONVERSION_INTENT_RE_PARTS:
        match = re.search(part, lowered)
        if match:
            phrase = match.group(0).strip()
            if phrase not in out:
                out.append(phrase)
    return out


def _custom_conversion_operands_required(intent: str) -> bool:
    """True when the intent asks for a named-event conversion, not a curated funnel."""

    import re  # noqa: WPS433

    lowered = str(intent or "").lower()
    return bool(
        re.search(
            r"\b(?:who|customers?)\s+ordered\b.+\bthen\s+ordered\b",
            lowered,
        )
    )


def _conversion_intent_why(
    runtime: Any, intent: str, query: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a ``why`` envelope when a conversion ask got a non-conversion draft.

    External-agent feedback: the planner confidently returned AOV for
    "conversion rate of customers who ordered A and then ordered B
    within 28 days". The draft validated, so nothing downstream caught
    that it answers a different question. This gate keeps the draft
    available but refuses to call it ready.
    """

    from ..expressions import ConversionExpr  # noqa: WPS433

    markers = _conversion_intent_markers(intent)
    if not markers:
        return None
    query_has_conversion = _query_contains_conversion(runtime, query)
    custom_operands_required = _custom_conversion_operands_required(intent)
    if query_has_conversion and not custom_operands_required:
        return None
    conversion_metrics = [
        {"id": str(recipe.id), "label": str(getattr(recipe, "label", "") or "")}
        for recipe in getattr(runtime.config, "metric_recipes", []) or []
        if isinstance(recipe.expression, ConversionExpr)
    ]
    hints: list[dict[str, Any]] = [
        {
            "kind": "author_conversion_ir",
            "message": (
                "Author the conversion directly: an expression with kind "
                "'conversion', an 'entity' to match base and converted events "
                "on, a 'window' ({unit, value}), and 'base'/'converted' "
                "aggregate operands. Operand-level 'filter' clauses (e.g. "
                "restricting each side to a product) are applied in SQL."
            ),
        },
        {
            "kind": "discover_conversion_metrics",
            "message": "Call discover with terms like 'conversion rate funnel' to rank curated conversion metrics.",
        },
    ]
    if conversion_metrics:
        hints.insert(
            0,
            {
                "kind": "use_curated_conversion_metric",
                "message": "Curated conversion metrics exist in this package; inspect one and adapt it.",
                "conversion_metrics": conversion_metrics[:5],
            },
        )
    return {
        "code": "CONVERSION_INTENT_UNREALIZED",
        "message": (
            "The intent reads as a conversion/funnel question, but best.query_ir "
            "does not encode the requested conversion operands — executing it as-is "
            "would answer a different question."
        ),
        "details": {
            "path": "select",
            "conversion_markers": markers,
            "query_contains_conversion": query_has_conversion,
            "custom_operands_required": custom_operands_required,
            "curated_conversion_metrics": [row["id"] for row in conversion_metrics],
        },
        "recovery_hints": hints,
    }


# Maximum number of validation errors surfaced inline under ``why``.
# Validation can emit many errors per IR (one per offending key, one
# per missing dimension, one per impossible JOIN); a full envelope can
# push the low_confidence response past v1's size. Trim to the top
# few — the agent calls ``validate`` next to get the full set anyway.
_WHY_ERROR_BUDGET = 3


def _trim_why_errors(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Cap ``why.errors`` to ``_WHY_ERROR_BUDGET`` entries.

    Adds a ``truncated`` marker with the dropped count so the agent
    knows there is more detail behind a follow-up ``validate`` call.
    """

    why: dict[str, Any] = {
        "code": "VALIDATION_FAILED",
        "message": "Pattern-realized IR did not pass validate.",
        "errors": [_slim_validation_error(error) for error in errors[:_WHY_ERROR_BUDGET]],
    }
    overflow = len(errors) - _WHY_ERROR_BUDGET
    if overflow > 0:
        why["truncated"] = {
            "dropped": overflow,
            "hint": (
                f"+{overflow} additional validation errors; call "
                "validate on best.query_ir for the full list."
            ),
        }
    return why


def _slim_validation_error(error: dict[str, Any]) -> dict[str, Any]:
    """Keep the inline plan failure compact.

    Full validator errors can contain path analyses, relationship
    payloads, and suggested patches. ``plan`` only needs the branching
    signal; callers can forward ``best.query_ir`` to ``validate`` for
    the complete diagnostic envelope.
    """

    out: dict[str, Any] = {}
    for key in (
        "code",
        "message",
        "severity",
        "stage",
        "path",
        "unsupported_construct",
        "why_invalid",
        "missing_metadata_or_capability",
    ):
        value = error.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    object_ids = error.get("object_ids")
    if object_ids:
        out["object_ids"] = list(object_ids)[:8]
    recovery_hints = _slim_recovery_hints(list(error.get("recovery_hints") or []))
    if recovery_hints:
        out["recovery_hints"] = recovery_hints
    return out or {"code": "VALIDATION_ERROR", "message": str(error)[:500]}


def _slim_recovery_hints(hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for hint in hints[:3]:
        if not isinstance(hint, dict):
            continue
        slim = {
            key: hint[key] for key in ("kind", "message") if hint.get(key) not in (None, "", [], {})
        }
        if slim:
            out.append(slim)
    return out


def _next_block(query: dict[str, Any], *, ready: bool) -> dict[str, Any]:
    """Assemble the ``next`` block with pre-baked arguments for the
    immediately useful next workflow steps.

    Carries ``validate`` with the query inline so the agent can
    forward without restructuring. When the IR has ``where`` filters,
    ``valid_values`` lists one entry per filtered dimension so the
    agent can confirm the literal values exist before executing.

    ``ready_for`` flags downstream calls that take the same ``{query}``
    shape — once validate passes, the agent reuses
    ``next.validate.query`` (or equivalently ``best.query_ir``) for
    those calls instead of paying to duplicate the IR three times in
    every response.
    """

    out: dict[str, Any] = {"validate": {"query": query}}
    if ready:
        # Cheap signal that the same query is ready to forward to these
        # later workflow steps. Avoids triplicating the IR under three
        # separate keys (validate/compile/execute).
        out["ready_for"] = ["compile", "execute"]
    valid_values_calls = _valid_values_next_steps(query)
    if valid_values_calls:
        out["valid_values"] = valid_values_calls
    return out


def _valid_values_next_steps(query: dict[str, Any]) -> list[dict[str, Any]]:
    """Build pre-baked ``valid_values`` calls for every filtered
    dimension in the realized IR so the agent doesn't have to scan
    ``query["where"]`` itself.
    """

    out: list[dict[str, Any]] = []
    for filter_spec in list(query.get("where") or []):
        if not isinstance(filter_spec, dict):
            continue
        dim = filter_spec.get("dimension") or filter_spec.get("field")
        if not dim:
            continue
        out.append({"dimension_id": dim})
    return out


def _out_of_scope_envelope(
    *,
    intent: str,
    intent_ir: IntentIR,
    out_of_scope: dict[str, Any] | None = None,
    low_relevance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Unified return shape for the two scope-gate failures.

    The IR is still attached so the agent has *something* to work with
    when it wants to suggest a related in-scope intent to the user.
    """

    envelope: dict[str, Any] = {
        "plan_version": _VERSION,
        "intent": intent,
        "intent_ir": intent_ir.to_dict(),
        "status": "out_of_scope",
        "best": None,
        "next": {},
    }
    if out_of_scope is not None:
        envelope["why"] = out_of_scope
    elif low_relevance is not None:
        envelope["why"] = low_relevance
    return envelope


__all__ = [
    "plan_payload",
]
