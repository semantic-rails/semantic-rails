"""Reuse an author's certified question only on a complete, deterministic text match."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

from ..expressions import collect_object_references
from ..visible_view import pinned_view
from ._base import RuntimeCompositionDraft, _singular
from .consumed_spans import _ranking_count_spans
from .intent_holds import _with_query_clock
from .intent_ir import IntentIR, ResolvedTerm, compose_hints
from .plan_query import _merge_partial_query, _trim_why_errors, _validate_query
from .plan_trace import _slim_best
from .ranking_checks import _ranking_request

# A number is one token with its sign, decimal point and separators ("-5", "2.5", "1,000",
# "2026-09-30"), so two different numbers never compare equal. Only the sentence separators
# below are dropped; any other mark ("$", "%", "<", "/") is a token of its own.
_TOKEN_RE = re.compile(
    r"[-+\N{MINUS SIGN}]?[.,]?\d+(?:[^\w\s]\d+)*|[^\W\d]+"
    r"|[^\w\s.,;:?!'\"()\-\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    r"\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}]"
)
_COUNT_SLOT = " examples_count_slot "


def _text(text: str) -> str:
    return " ".join(_singular(token) for token in _TOKEN_RE.findall(text.lower()))


def _cuts_token(text: str, span: tuple[int, int]) -> bool:
    """Whether masking ``span`` would cut a word or number ("2" in "-2" or "2.5")."""
    return any(
        low < cut < high
        for low, high in (match.span() for match in _TOKEN_RE.finditer(text))
        for cut in span
    )


def _masked(text: str, span: tuple[int, int], mark: str) -> str:
    return text[: span[0]] + mark + text[span[1] :]


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


def _count_slot(text: str) -> tuple[tuple[int, int], int] | None:
    request = _ranking_request(text)
    if request is None or not request["limit"] or request["limit"] <= 0:
        return None
    spans = set(_ranking_count_spans(text, request["limit"], frozenset()))
    if len(spans) != 1:
        return None
    span = next(iter(spans))
    if _cuts_token(text, span):
        return None
    return span, request["limit"]


def _match(question: str, authored: str, query: dict[str, Any]) -> dict[str, Any] | None:
    """The authored query for its exact question, or with only ``limit`` changed for the same
    question with a different top-N count; the authored ``time`` block is never edited."""
    if _text(question) == _text(authored):
        return deepcopy(query)
    question, authored = question.lower(), authored.lower()
    # Reuse the existing span parser: a threshold equal to limit is not a count.
    count, asked_count = _count_slot(authored), _count_slot(question)
    if count is None or asked_count is None or count[1] != query.get("limit"):
        return None
    if _text(_masked(authored, count[0], _COUNT_SLOT)) != _text(
        _masked(question, asked_count[0], _COUNT_SLOT)
    ):
        return None
    candidate = deepcopy(query)
    candidate["limit"] = asked_count[1]
    return candidate


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
        if values is None or any(
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
