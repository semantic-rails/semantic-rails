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
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ..ast import every_filter
from ..errors import SemanticLayerError
from ..runtime import runtime_request_scope
from ..temporal_support import validate_temporal_support
from .answer_shape import _answer_shape_why
from .consumed_spans import unconsumed_terms
from .faithfulness import intent_faithfulness_why, intent_subject_why, named_subject_why
from .generators import blocked_object_not_found, fallback_drafts
from .grouping_checks import _dropped_grouping_why
from .intent_holds import (
    _conversion_intent_why,
    _dropped_value_why,
    _pattern_dropped_start,
    _qualifying_entity_why,
    _start_dropped_why,
    _time_assumptions,
    _unclocked_window_why,
    _unconsumed_catalog_why,
    _unconsumed_terms_why,
    _unresolved_time_why,
    _with_query_clock,
    _with_time_gap,
)
from .intent_ir import IntentIR, compose_hints, parse_intent
from .orchestrator import compose
from .plan_query import (
    _checked_partial_query,
    _merge_partial_query,
    _slim_recovery_hints,
    _trim_why_errors,
    _validate_query,
)
from .plan_trace import (
    _fallback_trace,
    _first_fallback_drift_why,
    _query_detail_payload,
    _select_best_plan,
    _slim_best,
)
from .time_reference import with_time_reference
from .time_windows import _with_fiscal_calendar
from .unasked_groupings import _unasked_grouping_why
from .unmatched_words import (
    unconsumed_catalog_words,
    unconsumed_unknown_words,
    unmatched_intent_terms,
)
from .visibility import caller_hidden_ids, require_visible_objects, with_dimension_visibility

_VERSION = 1


# ---------------------------------------------------------------------------
_QUOTED = r"\"[^\"]*\"|“[^”]*”|(?<!\w)['‘].*?['’](?!\w)"
_CONTRACTION = r"\b(?P<word>[^\W_]+?)(?P<suffix>n['’]t|['’](?:s|re|ve|ll|d))\b"
_IS = frozenset({"what", "who", "where", "when", "how", "it", "that", "there", "here"})
_EXPANDED = {"'s": " is", "'re": " are", "'ve": " have", "'ll": " will", "'d": " would"}


def _normalize_question(text: str, declared: Iterable[str] = ()) -> str:
    """Expand grammatical contractions once, before every planning gate.

    Quoted text and a declared phrase spelled with an apostrophe stay as typed. Only
    a wh-word or pronoun reads ``'s`` as "is"; any other ``'s`` is left alone.
    """

    phrases = sorted(declared, key=len, reverse=True)
    kept = "|".join(re.sub(r"['’]", "['’]", re.escape(phrase)) for phrase in phrases) or "(?!)"

    def expand(match: re.Match[str]) -> str:
        word, suffix = match["word"], (match["suffix"] or "").lower().replace("’", "'")
        if match["kept"] is not None or (suffix == "'s" and word.lower() not in _IS):
            return match[0]
        if suffix == "n't":
            return {"ca": "can", "wo": "will", "sha": "shall"}.get(word.lower(), word) + " not"
        return word + _EXPANDED[suffix]

    pattern = rf"(?P<kept>{_QUOTED}|(?<!\w)(?:{kept})(?!\w))|{_CONTRACTION}"
    return re.sub(pattern, expand, text, flags=re.IGNORECASE)


@runtime_request_scope
@with_dimension_visibility
@with_time_reference
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
        _apostrophe_names,
        _catalog_token_index,
        _intent_passes_grounding_floor,
        _intent_passes_relevance_floor,
        _low_relevance_block,
        _visible_catalog,
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
    catalog_config = _visible_catalog(runtime._config, caller_hidden_ids(runtime._config))
    intent = intent_str = _normalize_question(intent.strip(), _apostrophe_names(catalog_config))
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
            catalog_config,
            search_index=(
                runtime._get_catalog_search_index() if catalog_config is runtime._config else None
            ),
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

    collision_why = named_subject_why(runtime, intent_str, partial_query)
    if collision_why is not None:
        payload = {
            "plan_version": _VERSION,
            "intent": intent,
            "intent_ir": parse_intent(runtime, intent).to_dict(),
            "status": "needs_clarification",
            "best": None,
            "why": collision_why,
            "next": {"action": "clarify"},
        }
        return _query_detail_payload(payload) if detail_level == "query" else payload

    validate_temporal_support(runtime._config, partial_query or {})
    # compose and every fallback helper inherit the request's time reference.
    result = compose(runtime, intent)
    if result.draft is not None:
        validate_temporal_support(runtime._config, result.draft.query)
    intent_ir = result.intent_ir
    require_visible_objects(runtime._config, {}, intent_ir.to_dict().get("grouping", []))
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
        or _unclocked_window_why(runtime._config, intent_str, best_draft.query, partial_query)
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
    grouping_why = (
        _dropped_grouping_why(runtime, intent_str, best_draft.query, partial_query)
        if best_ok
        else None
    )
    value_why = (
        (
            _unconsumed_terms_why(unconsumed_terms(runtime, intent_str, best_draft.query))
            # A shared grouping needs options even when fallback discovery consumed
            # words the primary parser did not (for example "their districts").
            or (
                grouping_why
                if (grouping_why or {}).get("details", {}).get("clarification")
                else None
            )
            or _unconsumed_catalog_why(
                intent_str, unconsumed_catalog_words(runtime, intent_str, best_draft.query)
            )
            or _dropped_value_why(
                intent_str,
                unconsumed_unknown_words(runtime, intent_str, best_draft.query),
                set(intent_ir.unresolved),
            )
            or grouping_why
            or _unasked_grouping_why(runtime, intent_str, best_draft.query, partial_query)
            or _qualifying_entity_why(runtime, intent_ir, best_draft.query)
        )
        if best_ok and not (faithfulness_why or time_why or conversion_why or subject_why)
        else None
    )
    # Last, the draft's result holds what the question's shape asks for: the listed entity's
    # rows for "who" or "which", a row per item for "each", a value to compare with for a
    # comparison, and a select of its own for each of several questions. It runs only when
    # nothing else holds the draft.
    shape_why = (
        _answer_shape_why(runtime, intent_str, best_draft.query, partial_query)
        if best_ok
        and not (faithfulness_why or time_why or conversion_why or subject_why or value_why)
        else None
    )
    ready = best_ok and not (
        faithfulness_why or time_why or conversion_why or subject_why or value_why or shape_why
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
            warnings=best_validation.get("warnings", []),
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
        # One why, but an unresolved or shortened window stays visible. A hold that returns
        # no query leaves no rows to filter from a shortened window.
        unrunnable = faithfulness_why["code"] == "TIME_WINDOW_UNRESOLVED"
        dropped = (time_why or {}).get("code") == "TIME_WINDOW_START_DROPPED"
        payload["why"] = _with_time_gap(
            faithfulness_why, None if unrunnable and dropped else time_why
        )
    elif time_why is not None:
        payload["why"] = time_why
    elif conversion_why is not None:
        payload["why"] = conversion_why
    elif subject_why is not None:
        payload["why"] = subject_why
    elif value_why is not None:
        payload["why"] = value_why
    elif shape_why is not None:
        payload["why"] = shape_why
    elif best_draft.blocked_reason.get("code") == "PLAN_INTENT_COVERAGE_GAP":
        payload["why"] = best_draft.blocked_reason
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
                warnings=row["validation"].get("warnings", []),
                intent_ir=intent_ir,
                fallback=_fallback_trace(row, planned),
            )
            for row in planned
            if row is not best
        ][: max(0, int(limit or 1) - 1)]
        payload["blocked"] = blocked
    for draft in [payload["best"], *payload.get("alternatives", [])]:
        for warning in draft.get("warnings", []):
            if warning not in payload.setdefault("warnings", []):
                payload["warnings"].append(warning)
    # Every returned query runs the window plan checked, without the caller's clock.
    for row in [payload["best"], *payload.get("alternatives", []), *blocked]:
        if "query_ir" in row:
            clocked = _with_query_clock(runtime._config, row["query_ir"], partial_query)
            if clocked is None:
                row.pop("query_ir")
            else:
                row["query_ir"] = clocked
    if any(
        (why or {}).get("code") == "TIME_WINDOW_UNRESOLVED" for why in (time_why, faithfulness_why)
    ):
        # Offer no runnable draft, as ask and the REPL refuse to run one: without
        # the question's window it answers a different question.
        for row in [payload["best"], *payload.get("alternatives", []), *blocked]:
            row.pop("query_ir", None)
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
    require_visible_objects(
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


def _query_key(query: dict[str, Any]) -> str:
    import json

    return json.dumps(query, sort_keys=True, default=str)


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
