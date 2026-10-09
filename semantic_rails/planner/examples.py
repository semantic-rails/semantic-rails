"""Reuse an author's certified query: an example answers only its own question (case and
whitespace aside)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

from ..expressions import collect_object_references
from ..visible_view import pinned_view
from ._base import RuntimeCompositionDraft
from .intent_holds import _with_query_clock
from .intent_ir import IntentIR, ResolvedTerm, compose_hints
from .plan_query import _merge_partial_query, _trim_why_errors, _validate_query
from .plan_trace import _slim_best


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _references(query: dict[str, Any]) -> set[str] | None:
    """Every string in the query and every id it uses as a mapping key; None if unreadable.

    No config, so an order_by on a select alias doesn't make field resolution raise.
    """
    try:
        keys = collect_object_references(query)
    except Exception:  # noqa: BLE001 — fail closed: an unreadable query is skipped unnamed
        return None
    return set(_strings(query)) | set(keys)


def _match(question: str, authored: str, query: dict[str, Any]) -> dict[str, Any] | None:
    """The authored query, unedited, when the question is the authored one (case and whitespace
    aside); anything else gets normal planning."""
    if " ".join(question.casefold().split()) != " ".join(authored.casefold().split()):
        return None
    return deepcopy(query)


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
        values = _references(query)
        if values is None:
            continue
        matched = _match(question, normalize(authored), query)
        if matched is None:
            continue
        # A hidden reference answers like the absent object: the example fails validation.
        # Validate the original before any planner repair can rescue it.
        if (
            any(
                value == object_id or value.startswith(object_id + "__")
                for value in values
                for object_id in hidden
            )
            or not _validate_query(runtime, query, partial)["ok"]
        ):
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
    ids = _references(query)
    if ids is None:
        return None, invalid
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
