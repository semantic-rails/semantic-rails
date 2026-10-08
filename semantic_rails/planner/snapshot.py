"""Shape a balance draft to the one day it reads.

A balance is a stock whose key holds a series column besides its clock: it answers with each
series' last snapshot in the period read, so a series that stopped reporting (a closed account)
keeps its last value unless each answer row reads one day. The invariant: a draft whose subject
is a balance reads exactly one as-of day, the last complete day before now when the question
names none ("What's our MRR?", "MRR right now"), or the closing day of the period it names ("MRR
at the end of last month", "MRR last month", "MRR as of 2026-09-30"; a stated period's opening
day for a start-of-period stock), and it groups by that day when a metric constraint requires it.

``shape_snapshot`` is the one place a draft gets that shape: ``_planned_row`` calls it for every
draft, and once more for a policy denial that asks for the day grouping. Readiness reads the same
``_expected_read``: a draft consumes the question's as-of words or window only when it reads the
day they name (``snapshot_read``), and ``time_checks._stock_as_of_gaps`` still holds a balance
draft without a day grain. A day not yet complete is never drafted, and no earlier day stands in
for one: a last complete day with no rows returns no rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..expressions import AggregateExpr
from ._base import _object_by_id, _semantic_token
from .coverage import CoverageGap, _coverage_why, _query_contains_prior_period, _time_block
from .generators import _target_focus_text
from .grouping_checks import _declared_name_spans
from .patterns.metric_by_dimension_rollup import _governed_target
from .time_checks import _multi_series_stocks, _window_days
from .time_reference import time_timezone
from .time_windows import _RANGE_END_UNITS, _time_window
from .unasked_groupings import _names_grain

_DAY = timedelta(days=1)
_LAST_DAY = {"range": {"last": {"unit": "day", "value": 1}}}


@dataclass(frozen=True)
class _Balance:
    """The stocks a draft's selects read, on one clock with one snapshot kind."""

    clock: str
    snapshot: str
    stocks: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class _Read:
    """The day the question asks a balance draft to read, and the question spans naming it."""

    balance: _Balance
    day: date
    time: dict[str, Any]
    spans: tuple[tuple[int, int], ...]
    # "latest" (no time words, or "now"), "as_of" ("end of", "as of") or "window" (a stated
    # period or day).
    source: str
    reading: str


@dataclass(frozen=True)
class _Ask:
    """The question asks for more than one read of a balance: "compare" or "series"."""

    balance: _Balance
    kind: str


def _balance(config: Any, query: dict[str, Any]) -> _Balance | None:
    """The balance every select of the draft reads directly, or None.

    A closed list: each select is a stock measure, or a metric whose whole expression is one
    aggregate of a stock measure (filtered or not, with no window), at the stock's own
    aggregation. They share one clock, an ``as_of_time`` snapshot clock, and one snapshot kind;
    at least one is a balance, and none is read through a predicate. Anything else (arithmetic,
    a ratio, a flow beside the stock, a daily rollup alone, a stock on an event clock) is not
    shaped, and readiness decides it as before.
    """

    measures = {row.id: row for row in config.measures}
    recipes = {row.id: row for row in config.metric_recipes}
    clocks: set[str] = set()
    snapshots: set[str] = set()
    stocks: list[str] = []
    labels: list[str] = []
    select = list(query.get("select") or [])
    for item in select:
        expression = item.get("expression") if isinstance(item, dict) else None
        if not isinstance(expression, dict) or set(expression) - {
            "measure",
            "metric",
            "aggregation",
        }:
            return None
        recipe = recipes.get(str(expression.get("metric") or ""))
        body = recipe.expression if recipe is not None else None
        if recipe is not None and not (isinstance(body, AggregateExpr) and not body.window):
            return None
        measure = measures.get(
            body.measure
            if isinstance(body, AggregateExpr)
            else str(expression.get("measure") or "")
        )
        aggregation = body.aggregation if isinstance(body, AggregateExpr) else ""
        aggregation = aggregation or str(expression.get("aggregation") or "")
        if (
            measure is None
            or getattr(measure, "measure_class", "") != "semi_additive"
            or aggregation not in ("", measure.default_aggregation)
        ):
            return None
        clocks.add(
            str(getattr(recipe, "temporal_role", "") or "")
            or str(getattr(body, "temporal_role", "") or "")
            or measure.default_temporal_role
            or next(iter(measure.compatible_temporal_roles or []), "")
        )
        snapshots.add(measure.accumulation.snapshot or "end_of_period")
        stocks.append(measure.id)
        labels.append(str(getattr(recipe or measure, "label", "") or ""))
    try:
        series = _multi_series_stocks(config, query)
    except Exception:  # noqa: BLE001 — an unreadable stock is not shaped; readiness holds it
        return None
    if not select or len(clocks) != 1 or len(snapshots) != 1:
        return None
    # The as-of day is a day of the snapshot clock; any other clock (a first-order time) isn't.
    role = _object_by_id(getattr(config, "temporal_roles", []), next(iter(clocks)))
    if getattr(role, "temporal_class", "") != "as_of_time" or not series or any(series.values()):
        return None
    label = labels[0] if len(labels) == 1 and labels[0] else "the balance"
    return _Balance(clocks.pop(), snapshots.pop(), tuple(dict.fromkeys(stocks)), label)


def _compares(config: Any, question: str) -> bool:
    """Whether the question compares, as ``_answer_shape_why`` reads a comparison."""

    from .answer_shape import _COMPARISON_PHRASE_RE, _COMPARISON_WORDS  # noqa: WPS433 (cycle)

    lowered = question.lower()
    names = list(_declared_name_spans(config, lowered))
    return _COMPARISON_PHRASE_RE.search(lowered) is not None or any(
        match.group() in _COMPARISON_WORDS
        and not any(low <= match.start() and match.end() <= high for low, high in names)
        for match in re.finditer(r"[^\W_]+", lowered)
    )


def _names_one_period(bounds: dict[str, Any], phrase: str) -> bool:
    """Whether a window phrase names one calendar week (from Monday), month, quarter or year.

    A relative window names one when it is the last one of its unit ("last month"); a calendar
    form when it isn't a range ("September 2026", not "July to September 2026") and its days are
    one period. "Last 3 months" names three, whatever its days.
    """

    last = dict(dict(bounds.get("range") or {}).get("last") or {})
    if last:
        return last.get("value") == 1 and last.get("unit") in {"week", "month", "quarter", "year"}
    if any(pattern.search(phrase) for pattern in _RANGE_END_UNITS):
        return False
    try:
        start, end = (date.fromisoformat(str(bounds[key])[:10]) for key in ("start", "end"))
    except (KeyError, ValueError):
        return False

    def months_later(months: int) -> date:
        index = start.month - 1 + months
        return date(start.year + index // 12, index % 12 + 1, 1)

    return (start.weekday() == 0 and end - start == 7 * _DAY) or (
        start.day == 1
        and any(
            end == months_later(months) and (start.month - 1) % months == 0 for months in (1, 3, 12)
        )
    )


def _expected_read(runtime: Any, question: str, query: dict[str, Any]) -> _Read | _Ask | None:
    """The one day the question asks the draft's balance to be read on, or what it asks instead.

    None when the draft is not a balance read (``_balance``), carries a prior-period select, or
    the question's time words name no single complete day: several phrases, one plan can't
    read, a window of several periods, a day not yet complete. Those keep the existing checks.
    """

    config = runtime._config
    balance = _balance(config, query)
    if balance is None or _query_contains_prior_period(runtime, query):
        return None
    if _compares(config, question):
        return _Ask(balance, "compare")
    grain = str(_time_block(query).get("grain") or "")
    if grain and _names_grain(config, question, query, grain):
        # Daily values already read one day per row; any coarser bucket is a series.
        return None if grain == "day" else _Ask(balance, "series")
    latest = _days(_LAST_DAY, balance.clock)
    window = _time_window(question)
    if latest is None or len(window.spans) > 1:
        return None
    last_complete, today = latest
    if len(window.as_of) > 1 or (window.as_of and window.as_of[0].span != window.spans[0]):
        return None
    cue = window.as_of[0] if window.as_of else None
    phrase = "".join(question.lower()[low:high].strip() for low, high in window.spans)
    if (cue is None and not window.spans) or (
        cue is not None and cue.kind == "latest_complete_day"
    ):
        time = {"temporal_role": balance.clock, "grain": "day", **_LAST_DAY}
        said = f"'{phrase}' is read as" if phrase else "the question names no day, so it is read on"
        reading = f"{balance.label} is a balance: {said} {last_complete}, the last complete day."
        return _Read(balance, last_complete, time, window.spans, "latest", reading)
    if cue is not None:
        bounds, source = cue.bounds, "as_of"
    else:
        bounds, source = (window.bounds if len(window.windows) == 1 else {}), "window"
    days = _days(bounds, balance.clock) if bounds else None
    if days is None:
        return None
    start, end = days
    opening = source == "window" and balance.snapshot == "start_of_period"
    if source == "window" and end - start != _DAY and not _names_one_period(bounds, phrase):
        return None
    day = start if opening else end - _DAY
    if day >= today:
        return None
    time = {
        "temporal_role": balance.clock,
        "grain": "day",
        "start": day.isoformat(),
        "end": (day + _DAY).isoformat(),
    }
    reading = (
        ""  # the question names the day itself
        if end - start == _DAY
        else f"{balance.label} is a balance: '{phrase}' is read as {day}, the period's "
        f"{'opening' if opening else 'closing'} day."
    )
    return _Read(balance, day, time, window.spans, source, reading)


def _days(bounds: dict[str, Any], clock: str) -> tuple[date, date] | None:
    """The first day a window reads and the day after it, in the clock's zone, or None."""

    days = _window_days(bounds, timezone=time_timezone(clock))
    return None if days is None or days[0] is None or days[1] is None else (days[0], days[1])


def _reads(read: _Read, query: dict[str, Any]) -> bool:
    """Whether the draft reads its balance on exactly ``read.day`` at day grain."""

    time = _time_block(query)
    return (
        time.get("temporal_role") == read.balance.clock
        and time.get("grain") == "day"
        and _days(time, read.balance.clock) == (read.day, read.day + _DAY)
    )


def _day_dimension(config: Any, clock: str) -> str | None:
    """The clock's own date dimension: on a day-grain read it takes the day's one value."""

    role = _object_by_id(config.temporal_roles, clock)
    row = _object_by_id(config.dimensions, str(getattr(role, "dimension", "") or ""))
    return str(row.id) if row is not None and row.data_type == "date" else None


def _governed(config: Any, question: str, query: dict[str, Any]) -> dict[str, Any]:
    """A building-block stock measure answers with its governed metric once read on the clock.

    The rule ``metric_by_dimension_rollup`` applies when a draft's time is on the metric's clock;
    a balance read now has that time block.
    """

    metric = _governed_target(config, _target_focus_text(question) or question, query)
    if metric is None or metric.temporal_role != _time_block(query).get("temporal_role"):
        return query
    [item] = query["select"]
    alias = _semantic_token(str(metric.id), fallback="value")
    order_by = [
        {**row, "field": alias}
        if isinstance(row, dict) and row.get("field") == item.get("as")
        else row
        for row in query.get("order_by") or []
    ]
    swapped = {**query, "select": [{"as": alias, "expression": {"metric": metric.id}}]}
    return {**swapped, "order_by": order_by} if order_by else swapped


def _ask_why(ask: _Ask, query: dict[str, Any]) -> dict[str, Any]:
    """Why plan asks instead of drafting more than one read of a balance."""

    label = ask.balance.label
    grain = str(_time_block(query).get("grain") or "") or None
    latest = _days(_LAST_DAY, ask.balance.clock)
    example = f"'{label} on {latest[0] if latest else '<YYYY-MM-DD>'}'"
    compare = ask.kind == "compare"
    gap = CoverageGap(
        kind="stock_as_of_unrealized",
        clause=label,
        message=(
            f"{label} is a balance, read on one day per answer row, and the question compares "
            "it, so plan doesn't draft one query for it."
            if compare
            else f"{label} is a balance the package reads per day, so plan doesn't draft it by "
            f"{grain}."
        ),
        expected={"grain": "day", "stocks": list(ask.balance.stocks)},
        actual={"grain": grain},
        recovery_hint={
            "kind": "ask_for_one_day",
            "message": (
                f"Ask for each value on its own day ({example}), then compare them."
                if compare
                else f"Pick a day ({example}) or ask for daily values ('daily {label}')."
            ),
        },
    )
    why = _coverage_why([gap]) or {}
    question = (
        "Which days should the balance be read on?"
        if compare
        else "A balance is read per day: which day, or daily values?"
    )
    return {**why, "details": {**why.get("details", {}), "clarification": {"question": question}}}


def shape_snapshot(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None,
    *,
    required: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The draft reading its balance on the day the question asks for, or why plan asks.

    Returns the draft unchanged when it is not a balance read, when the question names no single
    complete day for it, or when the caller's ``partial_query`` states the time. A comparison, or
    a series by week, month, quarter or year the package doesn't allow (the clock lacks the
    grain, or ``required`` holds the clock's day), returns the draft with a why that asks.

    ``required`` are the group_by fields a metric constraint found missing. Only the clock's own
    date dimension is added, beside a day grain (it adds no row); any other field leaves the draft
    unchanged, so the denial stands.
    """

    config = runtime._config
    balance = _balance(config, query)
    if balance is None or (partial_query or {}).get("time"):
        return query, None
    expected = _expected_read(runtime, question, query)
    day_dimension = _day_dimension(config, balance.clock)
    per_day = day_dimension is not None and day_dimension in required
    if isinstance(expected, _Ask):
        role = _object_by_id(config.temporal_roles, balance.clock)
        grain = str(_time_block(query).get("grain") or "")
        unsupported = grain not in list(getattr(role, "supported_grains", None) or [grain])
        if expected.kind == "compare" or per_day or unsupported:
            return query, _ask_why(expected, query)
        return query, None
    shaped = query
    if (
        isinstance(expected, _Read)
        and not _reads(expected, query)
        # Daily values over a stated window each read one day already.
        and not (expected.source == "window" and _time_block(query).get("grain") == "day")
    ):
        shaped = _governed(config, question, {**query, "time": expected.time})
    if required:
        added = set(required) - {day_dimension}
        if not per_day or added or _time_block(shaped).get("grain") != "day":
            return query, None
        group_by = list(dict.fromkeys([*(shaped.get("group_by") or []), day_dimension]))
        shaped = {**shaped, "group_by": group_by}
    return shaped, None


def snapshot_read(runtime: Any, question: str, query: dict[str, Any]) -> _Read | None:
    """The read the draft realizes: the day the question names for its balance, or None."""

    read = _expected_read(runtime, question, query)
    return read if isinstance(read, _Read) and _reads(read, query) else None


def snapshot_day_gaps(
    runtime: Any, question: str, query: dict[str, Any], partial_query: dict[str, Any] | None
) -> list[CoverageGap]:
    """A one-day balance draft on another day than the last complete one, for a question that
    names no time and a caller that states none."""

    if (partial_query or {}).get("time"):
        return []
    read = _expected_read(runtime, question, query)
    if not isinstance(read, _Read) or read.spans or _reads(read, query):
        return []
    time = _time_block(query)
    days = _days(time, read.balance.clock)
    if time.get("grain") != "day" or days is None or days[1] - days[0] != _DAY:
        return []
    return [
        CoverageGap(
            kind="stock_as_of_unrealized",
            clause=read.balance.label,
            message=(
                f"The question names no day, so {read.balance.label} is read on the last "
                f"complete day, {read.day}; this draft reads {days[0]}."
            ),
            expected={"grain": "day", "stocks": list(read.balance.stocks)},
            actual={"grain": "day"},
            recovery_hint={
                "kind": "ask_for_one_day",
                "message": f"Read {read.day}, or name the day you mean in the question.",
            },
        )
    ]


def as_of_groupings(config: Any, query: dict[str, Any]) -> set[str]:
    """The balance clock's own date dimension, when the draft reads the balance at day grain on
    that clock: grouped beside the day it adds no row (a metric constraint may require it)."""

    balance = _balance(config, query)
    time = _time_block(query)
    if balance is None or time.get("grain") != "day" or time.get("temporal_role") != balance.clock:
        return set()
    dimension = _day_dimension(config, balance.clock)
    return {dimension} if dimension else set()
