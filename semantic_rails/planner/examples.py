"""Reuse an author's certified question only on a complete, deterministic text match."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

from ..ast import _SUPPORTED_RELATIVE_UNITS, _floor_period, _shift_period
from ..expressions import collect_object_references
from ..visible_view import pinned_view
from ._base import RuntimeCompositionDraft, _singular
from .consumed_spans import _ranking_count_spans
from .intent_holds import _with_query_clock
from .intent_ir import IntentIR, ResolvedTerm, compose_hints
from .plan_query import _merge_partial_query, _trim_why_errors, _validate_query
from .plan_trace import _slim_best
from .ranking_checks import _ranking_request
from .time_checks import _window_agrees, _window_days
from .time_reference import time_timezone
from .time_windows import _time_window

# A number is one token with its sign, decimal point and separators ("-5", "2.5", "1,000",
# "2026-09-30"), so two different numbers never compare equal. Any other mark separates words.
_TOKEN_RE = re.compile(r"[-+\N{MINUS SIGN}]?[.,]?\d+(?:[^\w\s]\d+)*|[^\W\d]+")
# The only time keys a slot carries over: other keys (a fiscal calendar_id) bucket time in a
# way the default calendar can't prove.
_SLOT_TIME_KEYS = frozenset({"temporal_role", "grain", "range", "start", "end", "fill"})
_TIME_SLOT, _COUNT_SLOT = " examples_time_slot ", " examples_count_slot "


def _text(text: str) -> str:
    return " ".join(_singular(token) for token in _TOKEN_RE.findall(text.lower()))


def _cuts_token(text: str, span: tuple[int, int]) -> bool:
    """Whether masking ``span`` would cut a word or number ("2" in "-2" or "2.5")."""
    return any(
        low < cut < high
        for low, high in (match.span() for match in _TOKEN_RE.finditer(text))
        for cut in span
    )


def _masked(text: str, span: tuple[int, int] | None, mark: str) -> str:
    return text if span is None else text[: span[0]] + mark + text[span[1] :]


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


def _one_bucket(bounds: dict[str, Any], grain: str, timezone: str) -> bool:
    """Whether ``bounds`` read exactly one whole, elapsed ``grain`` bucket of the default
    calendar ("yesterday" or "on 2026-09-30" for a day; not "last 7 days" or "today")."""
    if grain not in _SUPPORTED_RELATIVE_UNITS:
        return False
    start, end = _window_days(bounds, timezone=timezone) or (None, None)
    # Yesterday ends where today starts: a bucket ending later is still in progress.
    yesterday = {"range": {"last": {"unit": "day", "value": 1}}}
    _start, today = _window_days(yesterday, timezone=timezone) or (None, None)
    if start is None or end is None or today is None:
        return False
    return (
        _floor_period(start, grain) == start
        and _shift_period(start, grain, 1) == end
        and end <= today
    )


def _time_slot(text: str, time: dict[str, Any]) -> tuple[tuple[int, int], dict[str, Any]] | None:
    """The text's one time phrase and its bounds, when they read one bucket of the authored
    grain; ``None`` for any other phrase, several phrases, or an authored time block that
    carries anything but a role, a grain, a window and ``fill``."""
    if not set(time) <= _SLOT_TIME_KEYS:
        return None
    zone = time_timezone(str(time.get("temporal_role") or ""))
    window = _time_window(text, timezone=zone)
    if len(window.spans) != 1 or len(window.windows) != 1 or window.unresolved:
        return None
    span, bounds = window.windows[0]
    if _cuts_token(text, span) or not _one_bucket(bounds, str(time.get("grain") or ""), zone):
        return None
    return span, bounds


def _match(question: str, authored: str, query: dict[str, Any]) -> dict[str, Any] | None:
    if _text(question) == _text(authored):
        return deepcopy(query)
    question, authored = question.lower(), authored.lower()
    time = query.get("time")
    time = time if isinstance(time, dict) else {}
    # Try the count alone, then the one time phrase with an optional count. Agreeing with a
    # one-bucket phrase makes the authored window that same bucket.
    attempts: list[tuple[tuple[int, int] | None, tuple[int, int] | None, dict[str, Any] | None]]
    attempts = [(None, None, None)]
    slot = _time_slot(authored, time)
    if slot is not None and _window_agrees(
        [slot], time, timezone=time_timezone(str(time.get("temporal_role") or ""))
    ):
        asked_slot = _time_slot(question, time)
        if asked_slot is not None:
            attempts.append((slot[0], *asked_slot))
    for authored_span, asked_span, bounds in attempts:
        text = _masked(authored, authored_span, _TIME_SLOT)
        asked = _masked(question, asked_span, _TIME_SLOT)
        # Reuse the existing span parser: a threshold equal to limit is not a count.
        count, asked_count = _count_slot(text), _count_slot(asked)
        if count is not None and count[1] != query.get("limit"):
            count = None
        if (count is None) != (asked_count is None) or (bounds is None and count is None):
            continue
        if _text(_masked(text, count and count[0], _COUNT_SLOT)) != _text(
            _masked(asked, asked_count and asked_count[0], _COUNT_SLOT)
        ):
            continue
        candidate = deepcopy(query)
        if bounds is not None:
            # Every authored time key, grain included, stays; only the window changes.
            kept = {
                key: value for key, value in time.items() if key not in {"range", "start", "end"}
            }
            candidate["time"] = {**kept, **bounds}
        if asked_count is not None:
            candidate["limit"] = asked_count[1]
        return candidate
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
