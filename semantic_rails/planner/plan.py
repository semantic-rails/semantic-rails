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

import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from ..ast import every_filter, is_child_group, rewrite_select_shorthand
from ..errors import SemanticLayerError
from ..runtime import runtime_request_scope
from ..temporal_support import validate_temporal_support
from ._base import (
    _TIME_UNITS,
    _grouping_matches,
    _is_temporal_grouping_term,
    _listed_grouping_terms,
    _names_time_axis,
    _names_whole_entity,
    _object_by_id,
    _requested_grouping_terms,
    _runtime_composition_terms,
    _term_matches_value_domain,
    _time_window,
    _with_fiscal_calendar,
)
from .faithfulness import (
    _dimension_nouns,
    _ranking_request,
    intent_faithfulness_why,
    intent_subject_why,
    unconsumed_catalog_words,
    unconsumed_terms,
    unconsumed_unknown_words,
    unmatched_intent_terms,
)
from .generators import blocked_object_not_found, fallback_drafts
from .intent_ir import IntentIR, compose_hints, parse_intent
from .orchestrator import compose
from .visibility import (
    require_visible_dimensions,
    visible_dimensions,
    visible_value_domains,
    with_dimension_visibility,
)

_VERSION = 1


# ---------------------------------------------------------------------------
@runtime_request_scope
@with_dimension_visibility
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
          "next":        {"valid_values":[...], "ready_for":["execute"]},
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

    partial_query = _checked_partial_query(partial_query)
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
        dimensions = visible_dimensions(runtime._config)
        catalog_config = runtime._config
        search_index = None
        if len(dimensions) == len(catalog_config.dimensions):
            search_index = runtime._get_catalog_search_index()
        else:
            catalog_config = replace(
                catalog_config,
                dimensions=dimensions,
                value_domains=visible_value_domains(catalog_config),
            )
        catalog_tokens = _catalog_token_index(catalog_config, search_index=search_index)
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
            weak_tokens=_weak_grounding_tokens(runtime._config),
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

    validate_temporal_support(runtime._config, partial_query or {})
    result = compose(runtime, intent)
    if result.draft is not None:
        validate_temporal_support(runtime._config, result.draft.query)
    intent_ir = result.intent_ir
    require_visible_dimensions(runtime._config, {}, intent_ir.to_dict().get("grouping", []))
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
                    "then validate it (over MCP, execute with mode 'validate')."
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
        planned.append(_planned_row(runtime, draft, pattern, partial_query, blocked, intent_str))

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
            row = _planned_row(runtime, draft, pattern, partial_query, blocked, intent_str)
            planned.append(row)
            if bool(row.get("validation", {}).get("ok")):
                break

    best = _select_best_plan(planned, intent_ir=intent_ir)
    best_draft = best["draft"]
    best_validation = best["validation"]
    best_ok = bool(best_validation.get("ok"))
    # No natural-language draft is ready on a package without a time axis.
    # Preserve the Query IR and use one warning independent of question wording.
    atemporal_why = (
        {
            "code": "INVALID_TEMPORAL_ROLE",
            "message": (
                "This package has no time; check the question doesn't ask for a time breakdown or window."
            ),
        }
        if not runtime._config.temporal_roles
        else None
    )
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
            partial_query=partial_query,
        )
        if best_ok
        else None
    )
    # Honesty gate: when the intent names a time window the planner
    # detected but could not resolve (and nothing else bounded the
    # query), the draft answers a *different* question than the user
    # asked. Downgrade instead of marking it ready to execute.
    time_why = (
        atemporal_why
        or _unresolved_time_why(intent_str, partial_query)
        or _start_dropped_why(
            best.get("start_dropped")
            or _pattern_dropped_start(intent_str, best_draft.query, partial_query)
        )
        if best_ok
        else None
    )
    # Same honesty principle for intent shape: a conversion/funnel ask
    # answered with a non-conversion metric (e.g. AOV) is a confidently
    # wrong answer, not a best effort. Downgrade and point at the
    # conversion surface instead.
    conversion_why = (
        _conversion_intent_why(runtime, intent_str, best_draft.query)
        if best_ok and time_why is None
        else None
    )
    subject_why = (
        intent_subject_why(
            runtime,
            question=intent_str,
            intent_ir=intent_ir,
            query=best_draft.query,
            partial_query=partial_query,
        )
        if best_ok and not (faithfulness_why or time_why or conversion_why)
        else None
    )
    unmatched = unmatched_intent_terms(runtime, intent_str, best_draft.query) if best_ok else []
    # The readiness invariants: every numeral and clock word in the question, and every word
    # that names a catalog object, is consumed by something the draft carries. Otherwise an
    # hour, a range, a threshold, a grouping or the asked-for subject was dropped. Last, every
    # grouping the question lists, apart from clock terms and declared values, has its own
    # group_by dimension, and every grouping the draft adds traces to the question; those
    # checks only hold a draft, they never change one.
    value_why = (
        (
            _unconsumed_terms_why(unconsumed_terms(runtime, intent_str, best_draft.query))
            or _unconsumed_catalog_why(
                intent_str, unconsumed_catalog_words(runtime, intent_str, best_draft.query)
            )
            or _dropped_value_why(
                unconsumed_unknown_words(runtime, intent_str, best_draft.query),
                set(intent_ir.unresolved),
            )
            or _dropped_grouping_why(runtime, intent_str, best_draft.query, partial_query)
            or _unasked_grouping_why(runtime, intent_str, best_draft.query, partial_query)
        )
        if best_ok and not (faithfulness_why or time_why or conversion_why or subject_why)
        else None
    )
    ready = best_ok and not (
        faithfulness_why or time_why or conversion_why or subject_why or value_why
    )
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
        if detail_level == "best":
            details = dict(fallback_drift_why["details"])
            details.pop("primary_slots")
            details["reasons"] = [dict(reason) for reason in details["reasons"]]
            for reason in details["reasons"]:
                slot = reason["kind"].rsplit("_", 1)[0].removesuffix("_scope")
                reason["expected"] = f"best.trace.intent_slots.{slot}"
                reason["actual"] = f"why.details.fallback_slots.{slot}"
            payload["why"] = {**fallback_drift_why, "details": details}
    elif faithfulness_why is not None:
        # One why, but an unresolved or shortened window stays visible.
        payload["why"] = _with_time_gap(faithfulness_why, time_why)
    elif time_why is not None:
        payload["why"] = time_why
    elif conversion_why is not None:
        payload["why"] = conversion_why
    elif subject_why is not None:
        payload["why"] = subject_why
    elif value_why is not None:
        payload["why"] = value_why
    elif not best_ok:
        errors = list(best_validation.get("errors") or [])
        payload["why"] = _trim_why_errors(errors)
        payload["tie_break_hints"] = _slim_recovery_hints(
            list(best_validation.get("recovery_hints") or [])
        )
    assumptions = _time_assumptions(intent_str, best_draft.query) if best_ok else []
    if assumptions:
        payload["assumptions"] = assumptions
    if unmatched:
        payload["warnings"] = [
            {
                "code": "PLAN_UNMATCHED_TERMS",
                "severity": "warning",
                "message": (
                    "The draft doesn't use these words from the question: "
                    f"{', '.join(unmatched)}. Check that best.query_ir answers what was "
                    "asked before executing it."
                ),
                "details": {"terms": unmatched},
            }
        ]
    if atemporal_why is not None:
        payload.setdefault("warnings", []).append({**atemporal_why, "severity": "warning"})
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
    if time_why is not None and time_why["code"] == "TIME_WINDOW_UNRESOLVED":
        # Offer no runnable draft, as ask and the REPL refuse to run one: without
        # the question's window it answers a different question.
        for row in [payload["best"], *payload.get("alternatives", []), *blocked]:
            row.pop("query_ir")
    if detail_level == "debug":
        payload["compose_hints"] = compose_hints(intent_ir)
    if detail_level == "best":
        resolved = payload["best"]["resolved"]

        def catalog_refs(value: Any) -> Any:
            if isinstance(value, dict):
                if value in resolved:
                    return {"id": value["id"], "$ref": f"best.resolved.{resolved.index(value)}"}
                return {key: catalog_refs(child) for key, child in value.items()}
            return [catalog_refs(child) for child in value] if isinstance(value, list) else value

        payload["intent_ir"] = catalog_refs(payload["intent_ir"])
        for gap in payload.get("why", {}).get("details", {}).get("gaps", []):
            if isinstance(gap.get("actual"), dict):
                gap["actual"] = {
                    key: {"$ref": f"best.query_ir.{key}"}
                    if value and value == payload["best"].get("query_ir", {}).get(key)
                    else value
                    for key, value in gap["actual"].items()
                }
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
    intent: str,
) -> dict[str, Any]:
    # A caller's time block wins the merge, so it would replace what the fiscal step
    # chose; the draft stays Gregorian and the fiscal gap reports it instead.
    fiscal_query = (
        draft.query
        if (partial_query or {}).get("time")
        else _with_fiscal_calendar(runtime._config, intent, draft.query)
    )
    merged_draft = replace(
        draft, query=_merge_partial_query(runtime._config, fiscal_query, partial_query)
    )
    require_visible_dimensions(
        runtime._config,
        merged_draft.query,
        merged_draft.resolved,
    )
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
    start_dropped = ""
    time_spec = merged_draft.query.get("time")
    caller_time = (partial_query or {}).get("time")
    if (
        not validation["ok"]
        and _codes(validation) & _LOOKBACK_TIME_CODES
        and isinstance(time_spec, dict)
        and time_spec.get("start")
        and not (isinstance(caller_time, dict) and caller_time.get("start"))
    ):
        # The metric looks back over earlier periods, so the engine can't
        # bound time.start. Keep the end: the draft runs, returns every
        # period up to it, and plan says so.
        unbounded = {**time_spec}
        start_dropped = str(unbounded.pop("start"))
        retry = replace(merged_draft, query={**merged_draft.query, "time": unbounded})
        retry_validation = _validate_query(runtime, retry.query, partial_query)
        if retry_validation["ok"]:
            merged_draft, validation = retry, retry_validation
        else:
            start_dropped = ""
    return {
        "status": "ok" if validation["ok"] else "low_confidence",
        "draft": merged_draft,
        "pattern": pattern,
        "validation": validation,
        "blocked": False,
        "start_dropped": start_dropped,
    }


# Validation codes for a bounded time.start that a lookback metric can't take.
_LOOKBACK_TIME_CODES = frozenset(
    {"WINDOWED_TIME_FILTER_UNSUPPORTED", "CUMULATIVE_TIME_FILTER_UNSUPPORTED"}
)


def _codes(validation: dict[str, Any]) -> set[str]:
    return {
        str(issue.get("code", ""))
        for issue in list(validation.get("errors") or [])
        if isinstance(issue, dict)
    }


def _pattern_dropped_start(
    intent: str, query: dict[str, Any], partial_query: dict[str, Any] | None
) -> str:
    """The question's window start a draft left out while keeping its end.

    The lookback retry records the start it drops; a pattern that bounds a
    period comparison drops it itself. Either way plan says so.
    """

    from ._base import _time_bounds_from_text  # noqa: WPS433

    caller_time = (partial_query or {}).get("time")
    if isinstance(caller_time, dict) and any(
        caller_time.get(key) for key in ("start", "end", "range")
    ):
        return ""
    expected = _time_bounds_from_text(intent)
    raw_time = query.get("time")
    time: dict[str, Any] = raw_time if isinstance(raw_time, dict) else {}
    if expected.get("start") and not time.get("start") and time.get("end") == expected.get("end"):
        return str(expected["start"])
    return ""


def _with_time_gap(why: dict[str, Any], time_why: dict[str, Any] | None) -> dict[str, Any]:
    """Add a time why (unresolved window, dropped start) to a coverage-gap why."""

    if time_why is None:
        return why
    details = dict(time_why.get("details") or {})
    clause = ", ".join(details.get("unresolved_phrases") or []) or str(
        details.get("requested_start", "")
    )
    gaps = [
        *list((why.get("details") or {}).get("gaps") or []),
        {
            "kind": str(time_why.get("code", "")).lower(),
            "clause": clause,
            "message": str(time_why.get("message", "")),
        },
    ]
    hints = list(why.get("recovery_hints") or [])
    kinds = {str(hint.get("kind", "")) for hint in hints if isinstance(hint, dict)}
    hints += [
        hint
        for hint in list(time_why.get("recovery_hints") or [])
        if isinstance(hint, dict) and str(hint.get("kind", "")) not in kinds
    ]
    return {
        **why,
        "details": {**dict(why.get("details") or {}), "gap_count": len(gaps), "gaps": gaps},
        "recovery_hints": hints,
    }


def _unconsumed_terms_why(terms: list[str]) -> dict[str, Any] | None:
    """Explain a draft that leaves out a number or a clock or zone word of the question."""

    if not terms:
        return None
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": (
            f"The question has numbers or time words the draft doesn't use: {', '.join(terms)}. "
            "It may have dropped an hour, a range or a threshold, so plan doesn't call it ready."
        ),
        "details": {"terms": terms},
        "recovery_hints": [
            {
                "kind": "state_missing_condition",
                "message": (
                    "Add the filter or limit to best.query_ir, or (plan resolves days and "
                    "coarser windows only) state an hour range as query.time start and end "
                    "ISO timestamps, or ask again without those words, then validate."
                ),
            }
        ],
    }


def _unconsumed_catalog_why(question: str, words: list[str]) -> dict[str, Any] | None:
    """Explain a draft that leaves out a question word naming a catalog object.

    A word inside a grouping the question asks for ("by store, customer type and product type")
    means the draft dropped that grouping, and the message says so. A comma in the list reads as
    "and" here: the grouping parse stops at a comma, which is how the draft lost the rest.
    """

    if not words:
        return None
    listed = _requested_grouping_terms(re.sub(r"\s*,\s*(?:and\s+)?", " and ", question))
    dropped = [term for term in listed if set(words) & set(re.findall(r"[^\W_]+", term))]
    terms = words[:8]  # as many as the warning names
    message = (
        f"The draft drops the grouping by {', '.join(dropped)} that the question asks for: "
        if dropped
        else "The draft may answer a different question: "
    ) + (
        f"it doesn't use these words, which name catalog objects: {', '.join(terms)}. "
        "So plan doesn't call it ready."
    )
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": message,
        "details": {"terms": terms, **({"dropped_groupings": dropped} if dropped else {})},
        "recovery_hints": [
            {
                "kind": "use_named_objects",
                "message": (
                    "Find what these words name with discover, add it to best.query_ir (a "
                    "group_by for a grouping, the select for a measure), then validate; or ask "
                    "again without those words."
                ),
            }
        ],
    }


def _entity_grouping_dimensions(config: Any, term: str) -> set[str] | None:
    """The dimensions that may stand for a listed grouping naming an entity, or None when the
    term names no entity.

    Only an entity the term names by its whole label, with a one-column key, has any: its key
    dimension, and its one declared dimension whose own words name the term when no other
    does. A clock the entity declares is not one of them. Another entity's dimension never
    stands in, and a composite key has none, so the grouping stays unmatched.
    """

    entities = [row for row in config.entities if _grouping_matches(term, row, entity=True)]
    if not entities:
        return None
    clocks = {row.dimension for row in config.temporal_roles}
    allowed: set[str] = set()
    for entity in entities:
        if len(entity.key) != 1 or not _names_whole_entity(term, entity):
            continue
        owned = [row for row in config.dimensions if row.entity == entity.id]
        allowed |= {row.id for row in owned if row.column == entity.key[0]}
        named = [
            row.id
            for row in owned
            if row.column != entity.key[0] and row.id not in clocks and _grouping_matches(term, row)
        ]
        if len(named) == 1:
            allowed |= set(named)
    return allowed


def _query_clocks(config: Any, query: dict[str, Any]) -> list[str]:
    """The labels of the time block's clock: its temporal role, and the calendar it buckets on."""

    time = _time_of(query)
    return [
        str(row.label or "")
        for row in [
            _object_by_id(config.temporal_roles, str(time.get("temporal_role") or "")),
            *(
                row
                for row in config.entities
                if row.calendar_id and row.calendar_id == time.get("calendar_id")
            ),
        ]
        if row is not None
    ]


def _listed_dimension_terms(config: Any, question: str, query: dict[str, Any]) -> list[str]:
    """The listed groupings a dimension answers: not a clock term ("by month", "by order
    date"), which is the time block's, nor a declared value, which is a filter."""

    clocks = _query_clocks(config, query)
    return [
        term
        for term in _listed_grouping_terms(question, config)
        if not (
            _is_temporal_grouping_term(term)
            or any(_names_time_axis(term, clock) for clock in clocks)
            or _term_matches_value_domain(config, term)
        )
    ]


def _reads_grouping(term: str, ids: set[str] | None, row: Any) -> bool:
    """Whether a dimension is a reading of a listed grouping: one of the entity's stand-ins
    (``_entity_grouping_dimensions``) for a term naming an entity, else a dimension whose own
    words name the term."""

    return _grouping_matches(term, row) if ids is None else row.id in ids


def _time_of(query: dict[str, Any]) -> dict[str, Any]:
    raw = (query or {}).get("time")
    return raw if isinstance(raw, dict) else {}


def _dropped_grouping_why(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A listed grouping that names an entity is satisfied only by that entity's own key
    dimension, or by the single declared dimension of that entity whose own words name it.
    An entity with a composite key is never satisfied by the guard, so the plan is not ready.

    Any other listed grouping needs a dimension whose own words name it; a clock term ("by
    month", "by order date") is the time block's and a declared value is a filter, so neither
    needs one. One dimension satisfies one listed grouping.

    A grouping whose dimensions belong to two or more entities, none of them the measure's own
    ("name" for an order count: Customer name, Store name and more), is ambiguous: plan holds
    instead of picking one. Only the caller's ``partial_query`` group_by settles it, when the
    draft adds no other dimension that may be it.
    """

    from ..metadata import _selection_context  # noqa: WPS433 - shared metadata helper

    config = runtime._config
    try:
        root = _selection_context(config, query)["root_entity"]
    except SemanticLayerError:
        root = ""
    terms = _listed_dimension_terms(config, question, query)
    grouped = [
        row
        for item in dict.fromkeys(query.get("group_by") or [])
        if (row := _object_by_id(config.dimensions, item)) is not None
    ]
    stand_ins = [_entity_grouping_dimensions(config, term) for term in terms]
    reads = _reads_grouping
    chosen = set((partial_query or {}).get("group_by") or [])

    def unsettled(term: str, ids: set[str] | None) -> bool:
        """Dimensions of two or more entities, none the measure's own, may be the grouping, and
        the caller's group_by doesn't say which: it names none, or the draft added one."""

        entities = {
            row.entity for row in config.dimensions if row.groupable and reads(term, ids, row)
        }
        picked = {row.id for row in grouped if reads(term, ids, row)}
        return len(entities) > 1 and root not in entities and not (picked and picked <= chosen)

    ambiguous = [term for term, ids in zip(terms, stand_ins, strict=True) if unsettled(term, ids)]
    candidates = [
        [
            index
            for index, dimension in enumerate(grouped)
            if term not in ambiguous and reads(term, ids, dimension)
        ]
        for term, ids in zip(terms, stand_ins, strict=True)
    ]
    assigned: dict[int, int] = {}

    def assign(term_index: int, seen: set[int]) -> bool:
        for dimension_index in candidates[term_index]:
            if dimension_index in seen:
                continue
            seen.add(dimension_index)
            if dimension_index not in assigned or assign(assigned[dimension_index], seen):
                assigned[dimension_index] = term_index
                return True
        return False

    dropped = [term for index, term in enumerate(terms) if not assign(index, set())]
    if not dropped:
        return None
    unclear = [term for term in dropped if term in ambiguous]
    missing = [term for term in dropped if term not in ambiguous]
    messages = [
        *(
            [
                f"The draft drops the grouping by {', '.join(missing)} that the question asks "
                "for: each listed grouping needs its own matching dimension, so plan doesn't "
                "call it ready."
            ]
            if missing
            else []
        ),
        *(
            [
                f"The grouping by {', '.join(unclear)} may be a dimension of any of several "
                "entities, none of them the measure's own, so plan doesn't pick one or call the "
                "draft ready."
            ]
            if unclear
            else []
        ),
    ]
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": " ".join(messages),
        "details": {
            "terms": dropped,
            "dropped_groupings": missing,
            **({"ambiguous_groupings": unclear} if unclear else {}),
        },
        "recovery_hints": [
            {
                "kind": "use_named_objects",
                "message": (
                    "Find a dimension for each grouping with discover, add the missing ones to "
                    "best.query_ir group_by, then validate; or ask again without those groupings."
                    + (
                        ' Name the entity of an ambiguous one ("customer name", not "name").'
                        if unclear
                        else ""
                    )
                ),
            }
        ],
    }


def _grain_bucket(day: date, grain: str) -> Any:
    """The calendar bucket of a day at a grain; weeks start on Monday, as the engine's do."""

    if grain == "week":
        return day - timedelta(days=day.weekday())
    if grain == "month":
        return day.year, day.month
    if grain == "quarter":
        return day.year, (day.month - 1) // 3
    if grain == "year":
        return day.year
    return day


def _grain_splits(time: dict[str, Any]) -> bool:
    """Whether the time block's grain can put the rows in two or more buckets.

    It can't when the block's window fits in one bucket: a calendar window inside one period of
    the grain ("in Q1 2017" at quarter or year), or the last single period of the grain ("last
    month" at month), or of a day. Any other grain, an open or relative window of more periods,
    or a non-Gregorian calendar may split them.
    """

    grain = str(time.get("grain") or "")
    if not grain:
        return False
    if grain not in _TIME_UNITS or str(time.get("calendar_id") or "default") != "default":
        return True
    window = time.get("range")
    if isinstance(window, dict):
        last = window.get("last")
        return not (
            isinstance(last, dict) and last.get("value") == 1 and last.get("unit") in {grain, "day"}
        )
    try:
        start = datetime.fromisoformat(str(time["start"]))
        end = datetime.fromisoformat(str(time["end"]))
    except (KeyError, ValueError):
        return True
    final = max((end - timedelta(microseconds=1)).date(), start.date())
    return _grain_bucket(start.date(), grain) != _grain_bucket(final, grain)


# A series the question asks for in words: it splits the answer by time at plan's grain.
_SERIES_RE = re.compile(r"\b(?:over\s+time|trends?|trending|time\s+series)\b")


def _names_grain(config: Any, question: str, query: dict[str, Any], grain: str) -> bool:
    """Whether the question's own words, outside every time window it states, ask for the
    grain's buckets: its unit or "-ly" form ("by month", "monthly", "month level", "per
    week", "daily"); a series ("over time", "trend", "trending", "time series"), which plan
    buckets at its default grain; or, for days, a listed grouping that names the query's clock
    ("by order date"). A window of whole calendar years ("in 2016 and 2017") names each of its
    years, so it names the year grain plan reads it at: one total per year."""

    time = _time_of(query)
    if grain == "year" and all(
        re.fullmatch(r"\d{4}-01-01(?:T00:00(?::00)?)?", str(time.get(key) or ""))
        for key in ("start", "end")
    ):
        return True
    lowered = str(question or "").lower()
    for start, end in _time_window(question).spans:
        lowered = lowered[:start] + " " * (end - start) + lowered[end:]
    forms = {grain, f"{grain}s", "daily" if grain == "day" else f"{grain}ly"}
    if forms & set(re.findall(r"[^\W\d_]+", lowered)) or _SERIES_RE.search(lowered):
        return True
    clocks = _query_clocks(config, query)
    return grain == "day" and any(
        _names_time_axis(term, clock)
        for term in _listed_grouping_terms(question, config)
        for clock in clocks
    )


# "revenue per store" and "revenue for each store" are "revenue by store": the words after
# "per", "each" or "every", up to a clause.
_PER_GROUPING_RE = re.compile(
    r"\b(?:per|each|every)\s+([a-z _-]+?)"
    r"(?=\s+(?:by|and|where|for|from|in|with|during|over|having|who|that)\b|\s*[.?!,;]|\s*$)"
)


def _asked_grouping_terms(config: Any, question: str) -> list[str]:
    """What the question asks to group by: each grouping it lists (``_listed_grouping_terms``),
    the noun a ranking ranks ("which 5 stores had the most orders"), and the words after
    "per", "each" or "every" ("revenue per store"). Windows are not part of any of them."""

    lowered = str(question or "").lower()
    for start, end in _time_window(question).spans:
        lowered = lowered[:start] + " " * (end - start) + lowered[end:]
    request = _ranking_request(question, _dimension_nouns(config))
    return [
        *_listed_grouping_terms(question, config),
        *([str(request["noun"])] if request else []),
        *(match.group(1).strip() for match in _PER_GROUPING_RE.finditer(lowered)),
    ]


def _unasked_grouping_why(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Every grouping the draft adds traces to the question, or the plan is not ready.

    A group_by dimension traces when a grouping the question asks for reads it
    (``_asked_grouping_terms``, read as ``_dropped_grouping_why`` reads a listed one), when the
    caller's ``partial_query`` group_by has it, or when the draft's own ``=`` or ``IN`` filter
    keeps only values of it the question names. The time block's grain
    traces when the question's words outside its windows name it (``_names_grain``), when the
    caller's ``partial_query`` time has it, or when it can't split the rows because the window
    fits in one bucket (``_grain_splits``). The package declares no default grain, so a grain
    plan picks for a comparison, a trend or a window of several periods doesn't trace.

    A ranking whose rows are split by a traced grain is a clarification instead: the draft
    would keep the top N of (entity x period). ``why.details.clarification`` offers the top N
    overall and the top N in each period. The check only holds a plan; it never changes the
    draft.
    """

    config = runtime._config
    caller = partial_query or {}
    time = _time_of(query)
    grain = str(time.get("grain") or "")
    splits = _grain_splits(time)
    grain_traced = not splits or (
        _time_of(caller).get("grain") == grain or _names_grain(config, question, query, grain)
    )
    terms = _asked_grouping_terms(config, question)
    stand_ins = [_entity_grouping_dimensions(config, term) for term in terms]
    # A dimension the draft filters to the values the question names splits the rows into
    # those values only; the filter-value check holds a filter that keeps any other.
    pinned = {
        str(row["field"])
        for row in _where_filters(query)
        if (row.get("op") == "=" and not isinstance(row.get("value"), (list, tuple, dict)))
        or (str(row.get("op")).lower() == "in" and isinstance(row.get("value"), list))
    }
    chosen = set(caller.get("group_by") or [])
    grouped = [
        row
        for item in dict.fromkeys(query.get("group_by") or [])
        if (row := _object_by_id(config.dimensions, item)) is not None
    ]
    unasked = [
        row
        for row in grouped
        if row.id not in chosen
        and row.id not in pinned
        and not any(
            _reads_grouping(term, ids, row) for term, ids in zip(terms, stand_ins, strict=True)
        )
    ]
    if unasked or not grain_traced:
        names = [str(row.label or row.id) for row in unasked] + ([] if grain_traced else [grain])
        return {
            "code": "PLAN_UNASKED_GROUPING",
            "message": (
                f"The draft groups by {', '.join(names)}, which the question never asks for: "
                "that splits the answer into rows the question doesn't ask for, so plan doesn't "
                "call it ready."
            ),
            "details": {
                "unasked_groupings": names,
                **({"dimensions": [row.id for row in unasked]} if unasked else {}),
                **({} if grain_traced else {"grain": grain}),
            },
            "recovery_hints": [
                {
                    "kind": "remove_unasked_grouping",
                    "message": (
                        "Remove them from best.query_ir (a dimension from group_by; the grain "
                        "from time, or the whole time block and its order_by entry when it "
                        "holds no start, end or range), then validate; or ask again naming "
                        'the grouping you want ("by month", "monthly").'
                    ),
                }
            ],
        }
    order_by = [row for row in query.get("order_by") or [] if isinstance(row, dict)]
    aliases = {row.get("as") for row in query.get("select") or [] if isinstance(row, dict)}
    if not (
        splits
        and grouped
        and query.get("limit") is not None
        and order_by
        and order_by[0].get("field") in aliases
    ):
        return None
    request = _ranking_request(question, _dimension_nouns(config))
    entity = str(request["noun"]) if request else " and ".join(str(row.label) for row in grouped)
    return _ranking_period_why(query, [row.id for row in grouped], entity, grain, order_by[0])


def _ranking_period_why(
    query: dict[str, Any], keys: list[str], entity: str, grain: str, ranked_by: dict[str, Any]
) -> dict[str, Any]:
    """The clarification for a ranking split by a period: top N overall, or in each period.

    Each option carries a Query IR that runs as is. Query IR ranks only over all rows, so
    ``top_overall`` returns the N ``entity`` rows on their total and its ``breakdown`` runs
    the draft for them, and ``top_per_period`` returns every row with each period's highest
    first, to keep each period's first N.
    """

    limit = int(query["limit"])
    time = _time_of(query)
    window = {key: time[key] for key in ("start", "end", "range") if key in time}
    overall: dict[str, Any] = {
        key: value for key, value in query.items() if key not in {"time", "order_by"}
    }
    if window:
        overall["time"] = {"temporal_role": time["temporal_role"], **window}
    overall["order_by"] = [ranked_by, *({"field": key, "direction": "ASC"} for key in keys)]
    every_row = {key: value for key, value in query.items() if key not in {"limit", "order_by"}}
    breakdown = {
        **every_row,
        "order_by": [
            *({"field": key, "direction": "ASC"} for key in keys),
            {"field": "time", "direction": "ASC"},
        ],
    }
    per_period = {
        **every_row,
        "order_by": [
            {"field": "time", "direction": "ASC"},
            ranked_by,
            *({"field": key, "direction": "ASC"} for key in keys),
        ],
    }
    question = (
        f"The top {limit} {entity} over the whole window, or the top {limit} {entity} in each "
        f"{grain}?"
    )
    return {
        "code": "PLAN_RANKING_PERIOD_AMBIGUOUS",
        "message": (
            f"The question ranks {entity} and splits the rows by {grain}, so the draft would "
            f"keep the top {limit} ({entity}, {grain}) rows. {question}"
        ),
        "details": {
            "limit": limit,
            "ranked": keys,
            "grain": grain,
            "clarification": {
                "kind": "ranking_period",
                "apply": ["query"],
                "question": question,
                "options": [
                    {
                        "id": "top_overall",
                        "meaning": (
                            f"The {limit} {entity} with the most over the whole window, on "
                            f"their total. For each one by {grain}, run breakdown.query_ir "
                            f"with a where filter keeping the {entity} this query returns."
                        ),
                        "query_ir": overall,
                        "breakdown": {"query_ir": breakdown, "filter_fields": keys},
                    },
                    {
                        "id": "top_per_period",
                        "meaning": (
                            f"In each {grain}, the {limit} {entity} with the most that {grain}. "
                            f"Query IR can't rank within a {grain}: this query returns every "
                            f"row with each {grain}'s highest first, so keep each {grain}'s "
                            f"first {limit} rows."
                        ),
                        "query_ir": per_period,
                        "keep_first_per_period": limit,
                    },
                ],
            },
        },
        "recovery_hints": [
            {
                "kind": "choose_ranking_period",
                "message": (
                    "Ask which option the question means, then run that option's query_ir as "
                    "its meaning says."
                ),
            }
        ],
    }


def _dropped_value_why(unconsumed: list[str], unresolved: set[str]) -> dict[str, Any] | None:
    """Unknown words left unresolved by the intent parse make the draft not ready.

    The parse records a word as it normalizes it ("messages" as "message", "sent" as
    "received"), so membership compares that form; the message names the question's spelling.
    """

    unknown = [term for term in unconsumed if _runtime_composition_terms(term) & unresolved]
    if not unknown:
        return None
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": (
            f"The question has words that match nothing in the catalog and the draft "
            f"doesn't consume: {', '.join(unknown)}. So plan doesn't call it ready."
        ),
        "details": {"terms": unknown, "kind": "filter_values_unrealized"},
        "recovery_hints": [
            {
                "kind": "add_missing_condition",
                "message": (
                    "Find the values with valid_values, add the filter to best.query_ir, "
                    "then validate; or ask again without those words."
                ),
            }
        ],
    }


def _time_assumptions(intent: str, query: dict[str, Any]) -> list[str]:
    """The reading plan took of an open end in the window the draft carries."""

    window = _time_window(intent)
    time = query.get("time")
    if not window.assumptions or not isinstance(time, dict):
        return []
    if any(time.get(key) != window.bounds.get(key) for key in ("start", "end")):
        return []
    return list(window.assumptions)


def _start_dropped_why(start: Any) -> dict[str, Any] | None:
    """Explain a window whose start a lookback metric couldn't take."""

    if not start:
        return None
    return {
        "code": "TIME_WINDOW_START_DROPPED",
        "message": (
            f"The question's window starts {start}, but this metric looks back over earlier "
            "periods, so the query can't bound time.start. best.query_ir returns every period "
            f"up to time.end; keep only the rows from {start} on."
        ),
        "details": {"path": "time.start", "requested_start": start},
        "recovery_hints": [
            {
                "kind": "filter_rows_after_execution",
                "message": f"Execute best.query_ir and keep the rows dated {start} or later.",
            }
        ],
    }


def _merge_partial_query(
    config: Any,
    draft_query: dict[str, Any],
    partial_query: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a generated draft with caller-provided Query IR.

    Partial-query preservation is a hard planner invariant: no top-level
    Query IR field supplied by the caller may silently disappear. For
    additive list fields we append generated entries after existing
    caller entries. For scalar/dict fields the caller wins, with ``time``
    merged shallowly so generated temporal roles can still fill missing
    fields. The question's values on one field form one generated filter
    for one total or combined ranking, without adding grouping. Caller
    rows stay as written, with only string predicate fields stripped.
    """

    partial = dict(partial_query or {})
    # Context is validation authority, not portable Query IR. _validate_query
    # receives it separately so every draft remains governed without asking
    # callers to replay trusted claims in a later compile/execute request.
    partial.pop("policy_context", None)
    partial.pop("request_context", None)
    partial.pop("request_id", None)
    merged = dict(draft_query or {})
    if partial.get("select"):
        merged, partial["select"] = _without_caller_selects(config, merged, partial["select"])
    for key, value in partial.items():
        if value in (None, "", [], {}):
            continue
        if key == "where":
            value = [
                {**row, "field": row["field"].strip()}
                if isinstance(row, dict) and isinstance(row.get("field"), str)
                else row
                for row in list(value or [])
            ]
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
    return merged


def _checked_partial_query(partial_query: dict[str, Any] | None) -> dict[str, Any] | None:
    """The caller's partial query with ``group_by`` as dimension ids.

    The merge reads its list fields as lists, so a shape it can't read
    (``group_by: [["dimension.x"]]``) fails here as the validation error the
    engine gives, not as an internal error.
    """

    if not partial_query:
        return partial_query
    for key in ("select", "where", "metric_filters", "order_by", "group_by"):
        value = partial_query.get(key)
        if value in (None, "", [], {}) or isinstance(value, list):
            continue
        raise _invalid_partial(
            f"query.{key}",
            type(value).__name__,
            f"query.{key} must be a list; got {type(value).__name__}.",
            f"Pass query.{key} as a list.",
        )
    group_by: list[str] = []
    for index, item in enumerate(partial_query.get("group_by") or []):
        dimension = item.get("dimension", item.get("field")) if isinstance(item, dict) else item
        if not isinstance(dimension, str):
            raise _invalid_partial(
                f"query.group_by[{index}]",
                type(item).__name__,
                f"query.group_by[{index}] must be a dimension id string; "
                f"got {type(item).__name__}.",
                "Pass group_by as a flat list of dimension ids, e.g. "
                '["dimension.store_name"], not [["dimension.store_name"]].',
            )
        group_by.append(dimension)
    checked = {**partial_query, "group_by": group_by} if group_by else partial_query
    # The same rewrite validate, compile and execute apply, so plan accepts what they accept.
    return rewrite_select_shorthand(checked, partial=True)[0]


def _invalid_partial(path: str, received: str, message: str, hint: str) -> SemanticLayerError:
    return SemanticLayerError(
        "INVALID_QUERY",
        message,
        details={
            "path": path,
            "received_type": received,
            "recovery_hints": [{"kind": "fix_query_shape", "message": hint}],
        },
    )


def _without_caller_selects(
    config: Any, query: dict[str, Any], caller_select: list[Any]
) -> tuple[dict[str, Any], list[Any]]:
    """Drop generated select items that compute one of the caller's.

    The caller's alias names the column: the generated ``order_by`` follows
    it, and a caller item without an alias takes the generated one.
    """

    caller = list(caller_select)
    positions = {_select_key(config, item): index for index, item in enumerate(caller)}
    kept: list[Any] = []
    renamed: dict[str, str] = {}
    for item in query.get("select") or []:
        index = positions.get(_select_key(config, item))
        if index is None:
            kept.append(item)
            continue
        alias, mine = item.get("as") if isinstance(item, dict) else None, caller[index]
        if alias and isinstance(mine, dict) and mine.get("as"):
            renamed[str(alias)] = str(mine["as"])
        elif alias and isinstance(mine, dict):
            caller[index] = {**mine, "as": alias}
    out = {**query, "select": kept}
    if renamed and query.get("order_by"):
        out["order_by"] = [
            {**row, "field": renamed.get(str(row.get("field")), row.get("field"))}
            if isinstance(row, dict)
            else row
            for row in query["order_by"]
        ]
    return out, caller


def _select_key(config: Any, item: Any) -> str:
    """What a select item computes: its expression, with a measure's default
    aggregation spelled out or left implicit alike."""

    import json

    expression = item
    if isinstance(item, dict):
        expression = item.get("expression", {k: v for k, v in item.items() if k != "as"})
    if isinstance(expression, dict) and set(expression) <= {"measure", "aggregation"}:
        measure = next(
            (row for row in config.measures if row.id == expression.get("measure")), None
        )
        if measure is not None:
            aggregation = expression.get("aggregation") or measure.default_aggregation
            expression = {"measure": measure.id, "aggregation": aggregation}
    return json.dumps(expression, sort_keys=True, default=str)


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


def _where_filters(query: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list((query or {}).get("where") or []):
        if not isinstance(row, dict):
            continue
        if is_child_group(row):
            out.append(
                {
                    "child": str(row.get("child", "")),
                    "match": str(row.get("match", "")),
                    "where": _where_filters({"where": list(row.get("where") or [])}),
                }
            )
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


def _unresolved_time_why(
    intent: str, partial_query: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Return a ``why`` envelope when the intent's time scope wasn't resolved.

    Triggers when the intent contains a time phrase the window resolver
    didn't turn into bounds, whatever window the draft happens to carry:
    only an explicit window in the caller's ``partial_query`` settles it.
    The shape mirrors the pattern ``blocked_reason`` envelope ({code,
    message, details, recovery_hints}).
    """

    from ._base import (  # noqa: WPS433
        _FISCAL_RE,
        _MAX_TIME_TEXT,
        _SUPPORTED_WINDOW_FORMS,
    )

    window = _time_window(intent)
    phrases = list(window.unresolved)
    too_long = len(intent) > _MAX_TIME_TEXT
    if not phrases and not too_long:
        return None
    caller_time = (partial_query or {}).get("time")
    if isinstance(caller_time, dict):
        # An unread suffix may supply either missing endpoint. Only a
        # complete caller window can settle an overlong question's scope.
        complete = caller_time.get("range") or (caller_time.get("start") and caller_time.get("end"))
        if complete or (not too_long and any(caller_time.get(key) for key in ("start", "end"))):
            return None
    hour_hint = (
        [
            {
                "kind": "state_hour_range",
                "message": (
                    "State the window yourself in the plan tool's query argument: set "
                    "query.time.start and query.time.end as ISO timestamps (end-exclusive, in "
                    "the temporal role's time zone; the role must be a timestamp), with the "
                    "selected temporal_role and grain."
                ),
            }
        ]
        if window.sub_day and not too_long
        else []
    )
    return {
        "code": "TIME_WINDOW_UNRESOLVED",
        "message": (
            f"The question exceeds the {_MAX_TIME_TEXT}-character time-resolution limit; "
            "its complete time scope could not be checked."
            if too_long
            else "The question names a window shorter than a day "
            f"({'; '.join(window.sub_day)}). plan resolves days and coarser windows only, so "
            "it returns no query: one over all time would answer a different question."
            if window.sub_day
            else "The question states windows that differ from one another "
            f"({'; '.join(window.conflicts)}), so plan returns no query: picking one would "
            "answer a different question."
            if window.conflicts
            else "The intent names a time window the planner could not resolve, so "
            "plan returns no query: one without that window would answer a "
            "different question."
        ),
        "details": {
            "path": "time",
            "unresolved_phrases": list(phrases),
            **({"conflicting_phrases": list(window.conflicts)} if window.conflicts else {}),
            **({"sub_day_phrases": list(window.sub_day)} if window.sub_day else {}),
            **({"max_intent_chars": _MAX_TIME_TEXT} if too_long else {}),
        },
        "recovery_hints": [
            *hour_hint,
            {
                "kind": "rephrase_time_window",
                "message": (
                    f"Shorten the question to at most {_MAX_TIME_TEXT} characters."
                    if too_long
                    else "A fiscal question's window resolves only from exact days: name the "
                    "period's first and last day and its fiscal bucket (e.g. 'by fiscal year "
                    "from 2017-02-01 to 2018-01-31')."
                    if _FISCAL_RE.search(intent.lower())
                    else "Rephrase the window using a supported form: "
                    + "; ".join(_SUPPORTED_WINDOW_FORMS)
                    + "."
                ),
            },
            {
                "kind": "provide_explicit_bounds",
                "message": (
                    "Or pass a complete window in the plan tool's query argument: "
                    "set query.time.start and query.time.end (end-exclusive), or "
                    "query.time.range.last with unit and value. Include the selected "
                    "temporal_role and grain in query.time; inspect the measure to choose them."
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
        for recipe in getattr(runtime._config, "metric_recipes", []) or []
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
        for recipe in getattr(runtime._config, "metric_recipes", []) or []
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
# bloat the low_confidence response. Trim to the top few; validating the
# draft (over MCP, ``execute`` with mode ``validate``) returns the full set.
_WHY_ERROR_BUDGET = 3


def _trim_why_errors(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Cap ``why.errors`` to ``_WHY_ERROR_BUDGET`` entries.

    Adds a ``truncated`` marker with the dropped count so the agent
    knows validating the draft returns more detail.
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
                f"+{overflow} additional validation errors; validate best.query_ir "
                "(over MCP, execute with mode 'validate') for the full list."
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
    """Offer next steps for the canonical ``best.query_ir``."""

    out: dict[str, Any] = {"ready_for": ["execute"]} if ready else {}
    if valid_values_calls := _valid_values_next_steps(query):
        out["valid_values"] = valid_values_calls
    return out


def _valid_values_next_steps(query: dict[str, Any]) -> list[dict[str, Any]]:
    """Offer ``valid_values`` arguments for each filtered dimension."""

    out: list[dict[str, Any]] = []
    for filter_spec in every_filter(query.get("where")):  # a child group's conditions too
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
