"""Reuse an author's certified question only on a complete, deterministic text match."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

from ..visible_view import pinned_view
from ._base import RuntimeCompositionDraft, _singular
from .consumed_spans import _ranking_count_spans
from .groupings import _time_spec
from .intent_holds import _with_query_clock
from .intent_ir import IntentIR, ResolvedTerm, compose_hints
from .plan_query import _merge_partial_query, _trim_why_errors, _validate_query
from .plan_trace import _slim_best
from .ranking_checks import _ranking_request
from .time_checks import _window_agrees
from .time_reference import time_timezone
from .time_windows import _time_window


def _text(text: str) -> str:
    return " ".join(_singular(word) for word in re.sub(r"[^\w\s]", "", text.lower()).split())


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _count_slot(text: str) -> tuple[int, int, int] | None:
    request = _ranking_request(text)
    if request is None or not request["limit"] or request["limit"] <= 0:
        return None
    spans = set(_ranking_count_spans(text, request["limit"], frozenset()))
    if len(spans) != 1:
        return None
    start, end = next(iter(spans))
    if text[max(0, start - 1) : start] == "." or text[end : end + 1] == ".":
        return None
    return start, end, request["limit"]


def _slots(question: str, query: dict[str, Any], *, clock: bool) -> tuple[str, dict[str, Any]]:
    """Mask only one proven time span and one numeral equal to the authored limit."""
    text = question
    changes: dict[str, Any] = {}
    time = query.get("time")
    time = time if isinstance(time, dict) else {}
    window = _time_window(text, timezone=time_timezone(str(time.get("temporal_role") or "")))
    if (
        clock
        and len(window.spans) == len(window.windows) == 1
        and not window.unresolved
        and _window_agrees(
            list(window.windows), time, timezone=time_timezone(str(time.get("temporal_role") or ""))
        )
    ):
        start, end = window.spans[0]
        text = text[:start] + " examples_time_slot " + text[end:]
        changes["time"] = window.bounds
    # Reuse the existing span parser: a threshold equal to limit is not a count.
    count = _count_slot(text)
    if count is not None and count[2] == query.get("limit"):
        start, end, value = count
        text = text[:start] + " examples_count_slot " + text[end:]
        changes["limit"] = value
    return _text(text), changes


def _match(question: str, authored: str, query: dict[str, Any]) -> dict[str, Any] | None:
    if _text(question) == _text(authored):
        return deepcopy(query)
    # Try count alone, then one resolved clock phrase with an optional count.
    for clock in (False, True):
        signature, slots = _slots(authored, query, clock=clock)
        candidate_query = deepcopy(query)
        if "time" in slots:
            role = str(query["time"].get("temporal_role") or "")
            window = _time_window(question, timezone=time_timezone(role))
            if len(window.spans) != 1 or len(window.windows) != 1 or window.unresolved:
                continue
            # Resolve the incoming clock before masking it by the same rule.
            candidate_query["time"] = {
                **{
                    key: value
                    for key, value in query["time"].items()
                    if key not in {"range", "start", "end"}
                },
                **_time_spec(role, question),
            }
        if "limit" in slots:
            count = _count_slot(question)
            if count is None:
                continue
            candidate_query["limit"] = count[2]
        candidate_signature, candidate_slots = _slots(question, candidate_query, clock=clock)
        if slots and slots.keys() == candidate_slots.keys() and signature == candidate_signature:
            return candidate_query
    return None


def example_plan(
    runtime: Any,
    question: str,
    partial: dict[str, Any] | None,
    *,
    normalize: Callable[[str], str],
    planned_row: Callable[..., dict[str, Any]],
    detail: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Match before heuristic planning; validation and caller visibility still govern."""
    view = pinned_view(runtime)
    hidden = view.entry.hidden if view is not None else frozenset()
    matches = []
    invalid = []
    for example_id, entry in runtime._get_package_examples():
        authored, query = entry.get("question"), entry.get("query")
        if not isinstance(authored, str) or not isinstance(query, dict):
            continue
        values = set(_strings(query))
        if any(
            value == object_id or value.startswith(object_id + "__")
            for value in values
            for object_id in hidden
        ):
            continue
        matched = _match(question, normalize(authored), query)
        if matched is None:
            continue
        # Validate the original before any substitution or planner repair can rescue it.
        if not _validate_query(runtime, query, partial)["ok"]:
            invalid.append(example_id)
            continue
        matches.append((example_id, matched))
    if not matches:
        return None, invalid
    intent_ir = IntentIR(intent=question, terms=frozenset())
    payload: dict[str, Any] = {
        "plan_version": 1,
        "intent": question,
        "intent_ir": intent_ir.to_dict(),
    }
    if len(matches) > 1:
        return {
            **payload,
            "status": "needs_clarification",
            "best": None,
            "why": {
                "code": "PLAN_AMBIGUOUS_EXAMPLE",
                "message": "Several package examples define this question. Choose an example.",
                "details": {"example_ids": [example_id for example_id, _query in matches]},
            },
            "next": {"action": "clarify"},
        }, invalid
    example_id, query = matches[0]
    query = _merge_partial_query(runtime._config, query, partial)
    # The authored fields count as caller-supplied: no fiscal rewrite or lookback repair.
    caller = {**(partial or {}), **query}
    ids = set(_strings(query))
    resolved = [
        {"id": row.id, "object_type": row.kind, "label": row.display_name}
        for row in runtime.registry.list_objects()
        if row.id in ids
    ]
    intent_ir = IntentIR(
        intent=question,
        terms=frozenset(),
        subjects=tuple(
            ResolvedTerm(**item, score=1.0)
            for item in resolved
            if item["object_type"] in {"measure", "metric"}
        ),
        grouping=tuple(
            ResolvedTerm(**item, score=1.0)
            for item in resolved
            if item["id"] in query.get("group_by", [])
        ),
        time=query.get("time"),
    )
    payload["intent_ir"] = intent_ir.to_dict()
    draft = RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=[f"Matched package example '{example_id}'."],
        interpreted_intent={"example_id": example_id, "consumed_spans": [[0, len(question)]]},
    )
    row = planned_row(runtime, draft, "package_example", caller, [], question)
    validation = row["validation"]
    clocked = _with_query_clock(runtime._config, row["draft"].query, partial)
    ready = bool(validation["ok"]) and clocked is not None
    best = _slim_best(
        row["draft"],
        pattern="package_example",
        validation_ok=bool(validation["ok"]),
        intent_ir=intent_ir,
        warnings=validation.get("warnings", []),
    )
    if clocked is None:
        best.pop("query_ir", None)
    else:
        best["query_ir"] = clocked
    payload.update(
        status="ok" if ready else "low_confidence",
        best=best,
        next={"ready_for": ["execute"] if ready else []},
    )
    if detail in {"full", "debug"}:
        payload.update(alternatives=[], blocked=[])
    if detail == "debug":
        payload["compose_hints"] = compose_hints(intent_ir)
    if not ready:
        payload["why"] = (
            _trim_why_errors(validation.get("errors", []))
            if not validation["ok"]
            else {
                "code": "TIME_WINDOW_UNRESOLVED",
                "message": "The example's window cannot resolve with the caller's clock.",
            }
        )
    return payload, invalid
