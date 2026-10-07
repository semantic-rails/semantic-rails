"""Faithfulness: the draft's window and span match the question's."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..ast import _relative_range_bounds
from ..compiler import bind_query
from ..compiler_parts.sql_lowering import _snapshot_series_columns
from ..errors import SemanticLayerError
from ..expressions import expr_to_dict
from ._base import _object_by_id, _tokens
from .coverage import (
    CoverageGap,
    _projected_subject_ids,
    _query_contains_prior_period,
    _referenced_ids,
    _time_block,
)
from .time_phrases import _BOUNDARY_BEFORE_RE, _FISCAL_RE, _QUANTITY_AFTER_RE
from .time_reference import time_policy_context, time_timezone
from .time_windows import (
    _FISCAL_BUCKET_RE,
    _MAX_TIME_TEXT,
    _TO_DATE_OR_ROLLING_RE,
    _fiscal_calendar,
    _time_window,
)
from .visibility import visible_dimensions, visible_object_ids


def _is_prior_period_offset(
    runtime: Any, query: dict[str, Any], windows: list[dict[str, Any]]
) -> bool:
    """Whether the question's one window is the offset of a prior-period comparison.

    "alongside the previous month's revenue" names the comparison's offset, not a window, when
    the draft carries one. Both window checks (the window plan reads, and the caller's) share it.
    """

    if len(windows) != 1:
        return False
    last = (windows[0].get("range") or {}).get("last") or {}
    return (
        set(windows[0]) == {"range"}
        and last.get("value") == 1
        and _query_contains_prior_period(runtime, query)
    )


def _time_window_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """The draft doesn't carry the window the question names, or carries another one."""

    expected = _time_window(text, policy_context=query.get("policy_context")).bounds
    if not expected:
        return []
    if _is_prior_period_offset(runtime, query, [expected]):
        return []
    time = _time_block(query)
    carried = {key: time[key] for key in ("start", "end", "range") if time.get(key)}
    differs = [key for key in carried if carried[key] != expected.get(key)]
    missing = [key for key in expected if key not in carried]
    # A lookback metric can't take a start; plan says so itself
    # (TIME_WINDOW_START_DROPPED), keeping the end.
    if carried and not differs and missing in ([], ["start"]):
        return []
    return [
        CoverageGap(
            kind="time_window_unrealized",
            clause=", ".join(f"{key}={value}" for key, value in expected.items()),
            message=(
                "The question names a time window, but the draft's window is a different one."
                if carried
                else "The question names a time window, but the draft is not bounded by it."
            ),
            expected={"time": expected},
            actual={"time": time or None},
            recovery_hint={
                "kind": "provide_time_window",
                "message": (
                    "Add the window to Query IR time (start inclusive, end exclusive) with a grain "
                    "that yields the buckets the question asks for, then validate."
                ),
            },
        )
    ]


# A span a label, name or question states: "(14 days)", "14-day", "_14d", "4 weeks", "1 year";
# not the upper end of a range such as "31-60 days".
_SPAN_RE = re.compile(
    r"(?<![\w.\-\u2013])(\d+)\s*-?\s*(d|days?|w|wks?|weeks?|mo|months?|q|quarters?|y|yrs?|years?)\b"
)
_SPAN_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "q": 91, "y": 365}
_GRAIN_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}


def _stated_spans(text: str) -> set[int]:
    """Every span, in days, that ``text`` states (identifiers read with spaces for ``_``)."""
    lowered = str(text or "").lower().replace("_", " ")
    return {int(n) * _SPAN_UNIT_DAYS[unit[0]] for n, unit in _SPAN_RE.findall(lowered)}


def _same_span(a: int, b: int) -> bool:
    # A month is 28-31 days, a year 360-366.
    return a == b or (a >= 28 and b >= 28 and abs(a - b) <= max(3, max(a, b) // 50))


def _draft_span_days(query: dict[str, Any]) -> int:
    """Days in the period the draft reports each value for: its bucket, else its window."""
    time = _time_block(query)
    if str(time.get("grain") or "") in _GRAIN_DAYS:
        return _GRAIN_DAYS[str(time["grain"])]
    try:
        if time.get("start") and time.get("end"):
            start, end = (date.fromisoformat(str(time[key])[:10]) for key in ("start", "end"))
            return (end - start).days
    except ValueError:
        pass
    last = dict(dict(time.get("range") or {}).get("last") or {})
    if last.get("unit") in _GRAIN_DAYS:
        return _GRAIN_DAYS[last["unit"]] * int(last.get("value") or 1)
    return 0


def _subject_window_gaps(config: Any, query: dict[str, Any]) -> list[CoverageGap]:
    """The subject is a stock over its own trailing window, and each row reports another period.

    "Unique visitors (14 days)" filtered to this week is the 14-day count as of the week's
    last snapshot, not this week's unique visitors. The window is read from the stock's
    label, name or id. Nothing the question says turns the check off: a false match only
    lowers confidence, while a missed one returns a wrong number as the period's.
    """
    asked = _draft_span_days(query)
    if not asked:
        return []
    objects = {row.id: row for row in [*config.measures, *config.metric_recipes]}
    gaps: list[CoverageGap] = []
    for subject_id in _projected_subject_ids(query):
        row = objects.get(subject_id)
        kind = getattr(row, "measure_class", "") or getattr(row, "kind", "")
        if kind != "semi_additive":
            continue
        label = str(getattr(row, "label", "") or subject_id)
        spans = _stated_spans(f"{label} {getattr(row, 'name', '')} {subject_id}")
        if len(spans) != 1:
            continue
        (own,) = spans
        if _same_span(own, asked):
            continue
        gaps.append(
            CoverageGap(
                kind="subject_window_mismatch",
                clause=label,
                message=(
                    f"{label} is a value over its own {own}-day window as of each point in time, "
                    f"not a total for the {asked}-day period the question asks about."
                ),
                expected={"period_days": asked},
                actual={"subject": subject_id, "subject_window_days": own},
                recovery_hint={
                    "kind": "ask_within_the_subject_window",
                    "message": (
                        f"Ask for {label} as of a date (it covers the {own} days before it), or "
                        "choose a measure that covers the period you asked about."
                    ),
                },
            )
        )
    return gaps


def _stock_as_of_gaps(config: Any, query: dict[str, Any]) -> list[CoverageGap]:
    """Hold predicate stocks at any grain, and direct stocks without one as-of day."""
    grain = _time_block(query).get("grain") or None
    try:
        stocks = _multi_series_stocks(config, query)
    except Exception:  # noqa: BLE001 — an unreadable stock cannot make a draft ready
        stocks = None
    if stocks is not None and grain == "day":
        stocks = {stock: True for stock, predicate in stocks.items() if predicate}
    if stocks == {}:
        return []
    shown = visible_object_ids(config, stocks or [])
    period = f"each {grain}" if grain else "the whole history (no time block)"
    selected = (
        visible_object_ids(config, _projected_subject_ids(query)) if stocks is not None else []
    )
    recipes = {row.id: row for row in config.metric_recipes} if selected else {}
    metric = next((recipes[subject] for subject in selected if subject in recipes), None)
    label = str(getattr(metric, "label", "") or "")
    if not label and shown:
        label = str(getattr(_object_by_id(config.measures, shown[0]), "label", "") or "")
    label = label or "the balance"
    predicate_stock = any((stocks or {}).values())
    return [
        CoverageGap(
            kind="stock_as_of_unrealized",
            clause=", ".join(shown) or "stock",
            message=(
                "A balance read through a predicate may have its own time scope; "
                "the outer grain does not prove a one-day read."
                if predicate_stock
                else f"Read a balance on one day; this draft adds each series' last value over {period}."
            ),
            expected={"grain": "day", "stocks": shown},
            actual={"grain": grain},
            recovery_hint={
                "kind": "ask_for_one_day",
                "message": (
                    "Choose a metric without a stock predicate, or select the balance directly."
                    if predicate_stock
                    else f"Ask for '{label} yesterday' or '{label} on <YYYY-MM-DD>', "
                    "or set time.grain: day with that day's start and end."
                ),
            },
        )
    ]


def _multi_series_stocks(config: Any, query: dict[str, Any]) -> dict[str, bool]:
    """Map each stock to whether any path to it crosses a predicate, including recipes."""
    measures = {row.id: row for row in config.measures}
    recipes = {row.id: row for row in config.metric_recipes}
    pending: list[tuple[Any, bool]] = [
        (query.get(key) or [], False) for key in ("select", "metric_filters", "where")
    ]
    seen: set[tuple[str, bool]] = set()
    stocks: dict[str, bool] = {}
    while pending:
        node, predicate = pending.pop()
        if isinstance(node, list):
            pending.extend((child, predicate) for child in node)
            continue
        if not isinstance(node, dict):
            continue
        predicate = predicate or node.get("kind") == "metric_predicate"
        pending.extend(
            (child, predicate or (node.get("kind") == "scoped_aggregate" and key == "predicates"))
            for key, child in node.items()
            if isinstance(child, (dict, list))
        )
        for object_id in (node.get(key) for key in ("measure", "metric", "metric_recipe")):
            if not isinstance(object_id, str) or (object_id, predicate) in seen:
                continue
            seen.add((object_id, predicate))
            if (recipe := recipes.get(object_id)) is not None:
                pending.append((expr_to_dict(recipe.expression), predicate))
            elif (
                row := measures.get(object_id)
            ) is not None and row.measure_class == "semi_additive":
                clock = row.default_temporal_role or next(
                    iter(row.compatible_temporal_roles or []), ""
                )
                if _snapshot_series_columns(row, clock, config):
                    stocks[object_id] = stocks.get(object_id, False) or predicate
    return stocks


def _fiscal_calendar_gaps(config: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """The question counts time in fiscal periods, but the draft counts Gregorian ones.

    A draft honors a fiscal bucket ("by fiscal quarter") by bucketing on a
    non-default calendar (the planner picks only the fiscal one; a caller may name
    another), or by grouping on a dimension whose name says fiscal (a fiscal-period
    column on the fact). A question with no fiscal bucket is honored on days, too.
    Any other fiscal mention ("the first fiscal quarter") also needs the period as
    exact days in the draft's window. A draft with no time buckets honors it by
    filtering on such a dimension. Nothing honors a to-date or rolling value:
    period-to-date resets on Gregorian periods.
    """

    lowered = text.lower()
    fiscal = _FISCAL_RE.search(lowered)
    if fiscal is None:
        return []
    time = _time_block(query)
    rolling = _TO_DATE_OR_ROLLING_RE.search(lowered) is not None
    scoped = not _FISCAL_RE.search(_FISCAL_BUCKET_RE.sub(" ", lowered)) or any(
        time.get(key) for key in ("start", "end", "range")
    )
    named = {
        row.id
        for row in visible_dimensions(config)
        if "fiscal" in _tokens(f"{row.id} {row.name} {row.label}")
    }
    bucketed = (
        str(time.get("calendar_id") or "default").lower() != "default"
        # A day is a day on any calendar, unless the question asks for fiscal buckets.
        or (time.get("grain") == "day" and not _FISCAL_BUCKET_RE.search(lowered))
        or bool(named & {str(item) for item in query.get("group_by") or []})
    )
    filtered = not time.get("grain") and bool(named & set(_referenced_ids(query)))
    if not rolling and ((scoped and bucketed) or filtered):
        return []
    calendar = _fiscal_calendar(config)
    calendars = sorted(
        {row.calendar_id for row in config.entities if row.kind == "time" and row.calendar_id}
    )
    steps = []
    if rolling:
        steps.append(
            "ask for the fiscal buckets alone: plan can't draft a fiscal to-date or rolling value"
        )
    elif not bucketed and calendar is not None:
        steps.append(
            f"set query.time.calendar_id to {calendar.calendar_id!r} and time.fill to true"
            + ("" if time.get("grain") else " with a temporal_role and grain")
        )
    if not scoped:
        steps.append(
            "give any fiscal period as exact query.time.start and end dates (or ask only for "
            "fiscal buckets, as in 'by fiscal quarter')"
        )
    hint = " and ".join(steps)
    return [
        CoverageGap(
            kind="fiscal_calendar_unrealized",
            clause=fiscal.group(0),
            message=(
                "The question asks for a to-date or rolling value in fiscal periods."
                if rolling
                else "The question names a fiscal period, but the draft carries no window for it."
                if bucketed
                else "The question counts time in fiscal periods, but the draft buckets and "
                "bounds time on the Gregorian calendar."
            ),
            expected={"calendar_id": calendar.calendar_id if calendar else "fiscal"},
            actual={"calendar_id": time.get("calendar_id") or "default"},
            recovery_hint={
                "kind": "use_fiscal_calendar",
                "message": (
                    f"{hint[:1].upper()}{hint[1:]}, then validate."
                    if calendar
                    else "plan found no single calendar named fiscal in this package"
                    + (f" (its calendars: {', '.join(calendars)})" if calendars else "")
                    + ": set query.time.calendar_id to the one you mean with time.fill true, "
                    "author a fiscal calendar, or ask in calendar-year terms."
                ),
            },
        )
    ]


_YEAR_WORD_RE = re.compile(r"(?:19|20)\d{2}")


# A year a date phrase states: after "in", "for" or "during", or the word "year". Only the 2000s,
# as when plan reads a short question ("1930" is never a date there).
_YEAR_CUE_RE = re.compile(r"\b(?:in|for|during|year)\s+(20\d{2})\b")
# The rest of a bound after its date, when it is midnight.
_MIDNIGHT_RE = re.compile(r"(?:[t ]00:00(?::00(?:\.0+)?)?(?:z|[+-]00:?00)?)?")


def _question_time(
    lowered: str,
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> tuple[list[tuple[tuple[int, int], dict[str, Any]]], list[tuple[int, int]]]:
    """The windows plan reads from the question, and the other spans it reads as time.

    The other spans are time phrases plan cannot resolve ("last 24 hours", "before 2017"). A
    bare year is one only with the bound word plan reports it by ("before 2017"); on its own it
    may be an hour ("at 2000"), so it is never returned.

    A question too long to read has a window only when its date phrases state one calendar year
    ("in 2017", "for 2017"; never "at 2000" or "in 2000+"). Two different years cannot be told
    from a quantity ("in 2017 ... for 2000 customers") without the resolver, so such a question
    states no window and each year in it is left unconsumed.
    """

    if len(lowered) > _MAX_TIME_TEXT:
        years = [
            (match.span(1), int(match.group(1)))
            for match in _YEAR_CUE_RE.finditer(lowered)
            if _QUANTITY_AFTER_RE.match(lowered[match.end(1) :]) is None
        ]
        if len({year for _span, year in years}) > 1:
            return [], []
        return [
            (span, {"start": f"{year:04d}-01-01", "end": f"{year + 1:04d}-01-01"})
            for span, year in years
        ], []
    read = _time_window(lowered, policy_context=policy_context, timezone=timezone)
    windows = list(read.windows)
    others: list[tuple[int, int]] = []
    for low, high in read.spans:
        if any(cue.span == (low, high) for cue in read.as_of):
            # An interval cannot consume a snapshot request.
            continue
        if any(start <= low and high <= end for (start, end), _bounds in windows):
            continue
        if _YEAR_WORD_RE.fullmatch(lowered[low:high].strip()):
            # A bare year is a date only as the phrase plan reports it: after a bound word
            # ("before 2017", "of 2017"), the span starting at that word. "at 2000" is not one.
            bound = _BOUNDARY_BEFORE_RE.search(lowered[:low])
            if bound is None:
                continue
            low = bound.start()
        others.append((low, high))
    return windows, others


def _window_days(
    bounds: dict[str, Any],
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> tuple[date | None, date | None] | None:
    """The first day a window covers and the first day after it, or None where unreadable.

    A relative range is read in ``timezone``, the planning zone unless one is given. A bound is
    readable only at a whole day, the grain of every window plan reads: a date, or that date at
    midnight. A bound with another time of day is unreadable, as is a window whose start is not
    before its end (empty or reversed); a missing bound comes back None. A bound with a zone
    designator is readable only when its offset is its temporal role's at that instant: then
    its written date is the role-local date.
    """

    role = str(bounds.get("temporal_role") or "")
    if bounds.get("range"):
        try:
            bounds = _relative_range_bounds(
                bounds["range"],
                policy_context=time_policy_context(policy_context),
                timezone=timezone or time_timezone(),
            )
        except (SemanticLayerError, ValueError, OverflowError):
            return None
    days: list[date | None] = []
    for key in ("start", "end"):
        raw = str(bounds.get(key) or "").strip()
        text = raw.lower()
        if not text:
            days.append(None)
            continue
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        tail = text[10:]
        if tail.endswith("z") or "+" in tail or "-" in tail:
            try:
                moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                local = moment.astimezone(ZoneInfo(time_timezone(role))).utcoffset()
            except (ValueError, KeyError, OverflowError):
                return None
            if moment.utcoffset() != local:
                return None
        if not _MIDNIGHT_RE.fullmatch(tail):
            return None
        days.append(day)
    if days[0] is not None and days[1] is not None and days[0] >= days[1]:
        return None
    return days[0], days[1]


def _window_agrees(
    windows: list[tuple[tuple[int, int], dict[str, Any]]],
    time: dict[str, Any],
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> bool:
    """Whether the draft's window is the one the question's date phrases state.

    The one rule for a window in the draft: it carries both bounds, each read only at a whole
    day (see ``_window_days``), and they are the earliest start and the latest end among the
    windows the question states. A missing bound, or a draft that cannot be read, does not
    agree. A question that states no window agrees with any draft.
    """

    if not windows:
        return True
    carried = _window_days(time, policy_context, timezone=timezone)
    starts: list[date] = []
    ends: list[date] = []
    for _span, bounds in windows:
        asked = _window_days(bounds, policy_context, timezone=timezone)
        if asked is None or asked[0] is None or asked[1] is None:
            return False
        starts.append(asked[0])
        ends.append(asked[1])
    return carried == (min(starts), max(ends))


def _caller_window_gaps(
    runtime: Any, text: str, query: dict[str, Any], *, timezone: str | None = None
) -> list[CoverageGap]:
    """The window a caller passed is not the one the question's date phrases state."""

    lowered = text.lower()
    context = query.get("policy_context")
    time = _time_block(query)
    windows, _others = _question_time(lowered, context, timezone=timezone)
    if _window_agrees(windows, time, context, timezone=timezone) or _is_prior_period_offset(
        runtime, query, [bounds for _span, bounds in windows]
    ):
        return []
    return [
        CoverageGap(
            kind="time_window_unrealized",
            clause=", ".join(lowered[low:high].strip() for (low, high), _bounds in windows),
            message="The question names a time window, but the draft's window is a different one.",
            expected={"time": [bounds for _span, bounds in windows]},
            actual={"time": time or None},
            recovery_hint={
                "kind": "provide_time_window",
                "message": (
                    "Pass the window the question states in Query IR time (start inclusive, end "
                    "exclusive), or ask about the window you passed."
                ),
            },
        )
    ]


def _role_window_why(runtime: Any, text: str, query: dict[str, Any]) -> dict[str, Any] | None:
    """Hold a draft whose window reads other days in the zone of a role the query reads it on.

    Plan drafts and checks every window in one planning zone (the package default, else UTC),
    while execution filters it on the query's role and on each leg's own role where the leg
    can't read the query's (``_leaf_time_role``, as binding records it), each in its zone. A
    draft is ``ok`` only when the question's windows, and a relative range the draft carries
    for them, read the same days in every such zone as in the planning zone. If binding can't
    say which roles are read, every role is taken as read, so the draft is held.
    """

    time = _time_block(query)
    role = str(time.get("temporal_role") or "")
    planning = time_timezone(runtime=runtime)
    names = [role, *(row.id for row in runtime._config.temporal_roles)]
    zones = {name: time_timezone(name, runtime=runtime) for name in names if name}
    if set(zones.values()) <= {planning}:
        return None
    context = query.get("policy_context")
    asked = _time_window(text, context, timezone=planning).windows
    if not asked:
        return None
    carried = {"range": time["range"]} if time.get("range") else {}

    def differing(zone: str) -> list[tuple[int, int]]:
        if carried and _window_days(carried, context, timezone=planning) != _window_days(
            carried, context, timezone=zone
        ):
            return [span for span, _bounds in asked]
        try:
            read = _time_window(text, context, timezone=zone).windows
        except (ValueError, OverflowError):
            read = ()
        return [
            span
            for (span, bounds), local in zip(asked, read, strict=False)
            if span != local[0]
            or _window_days(bounds, context, timezone=planning)
            != _window_days(local[1], context, timezone=zone)
        ] + [span for span, _bounds in asked[len(read) :]]

    moved = {zone: spans for zone in set(zones.values()) - {planning} if (spans := differing(zone))}
    if not moved:
        return None
    try:
        bound = bind_query(runtime._config, None, query).temporal_roles.values()
        read_roles = {role}.union(*bound)
    except Exception:  # any failure: every role may be read
        read_roles = set(zones)
    held = {
        name: zones[name]
        for name in sorted(read_roles, key=lambda name: (name != role, name))
        if zones.get(name) in moved
    }
    if not held:
        return None
    zone = next(iter(held.values()))
    differing_spans = sorted({span for held_zone in held.values() for span in moved[held_zone]})
    lowered = text.lower()
    return {
        "code": "TIME_WINDOW_UNRESOLVED",
        "message": (
            "The question's window reads different days in the zone of a temporal role the "
            f"query reads it on ({', '.join(dict.fromkeys(held.values()))}) than in the planning "
            f"zone ({planning}), so plan returns no query: either reading may answer a "
            "different question."
        ),
        "details": {
            "path": "time",
            "temporal_role": next(iter(held)),
            "timezone": zone,
            "temporal_roles": held,
            "planning_timezone": planning,
            "unresolved_phrases": list(
                dict.fromkeys(lowered[low:high].strip() for low, high in differing_spans)
            ),
        },
        "recovery_hints": [
            {
                "kind": "rephrase_time_window",
                "message": (
                    "Name the window's dates as the temporal role's zone reads them (e.g. "
                    "'2017-04-03'), so the window doesn't depend on the zone."
                ),
            }
        ],
    }
