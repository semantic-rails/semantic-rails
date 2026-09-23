"""Plain-language restatement of what a planned Query IR computes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..catalog_service import resolve_catalog
from ..errors import SemanticLayerError
from ..runtime import Runtime

_AGGREGATION_WORDS = {"avg": "average", "count_distinct": "count distinct"}


_ARITHMETIC_SYMBOLS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}


_MEASURE_KINDS = {
    "",
    "measure",
    "measure_ref",
    "aggregate",
    "semi_additive",
    "scoped_aggregate",
    "prior_period",
}


_MATCHING_MODES = {
    "first_converted_after_base": "the first conversion after each base event",
    "closest_converted_after_base": "the closest conversion after each base event",
}


# Query keys the restatement spells out, and keys that only shape the response.
# Any other key changes what runs, so it is listed as ``[with ...]``.
_DESCRIBED_QUERY_KEYS = frozenset(
    {"select", "group_by", "time", "where", "metric_filters", "limit", "order_by"}
)
_RESPONSE_QUERY_KEYS = frozenset(
    {"version", "request_id", "verbosity", "sql_profile", "explain", "debug"}
)


_EMPTY: tuple[Any, ...] = (None, "", [], {})


def describe_query(query: dict[str, Any], labels: dict[str, str] | None = None) -> str:
    """Restate in words what a Query IR computes.

    The text comes from the query itself, never from the question, so it
    shows what will actually run. Shapes it doesn't know fall back to their
    ids or kinds instead of guessing.
    """

    names = dict(labels or {})

    def label(object_id: Any) -> str:
        text = str(object_id or "")
        return names.get(text, text)

    subjects: list[str] = []
    aliases: dict[str, str] = {}
    for item in list(query.get("select", []) or []):
        if not isinstance(item, dict):
            continue
        alias = str(item.get("as", "") or "")
        expression = item.get("expression")
        described = _describe_expression(expression, label) if isinstance(expression, dict) else ""
        subject = (described or alias or "an expression") + _with(item, {"as", "expression"})
        subjects.append(subject)
        if alias:
            aliases[alias] = subject
    text = " and ".join(filter(None, [", ".join(subjects[:-1]), *subjects[-1:]])) or "no measures"

    group_by = [label(dimension) for dimension in list(query.get("group_by", []) or [])]
    if group_by:
        text += " by " + ", ".join(group_by)
    time_text = _describe_time(dict(query.get("time", {}) or {}), label)
    if time_text:
        text += ", " + time_text
    filters = [_describe_filter(item, label) for item in list(query.get("where", []) or [])]
    filters += [
        _describe_metric_filter(item, label) for item in list(query.get("metric_filters", []) or [])
    ]
    if filters:
        text += ", where " + " and ".join(filters)
    limit = query.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        ordering = [
            f"{aliases.get(str(order.get('field', '')), label(order.get('field')))} "
            f"{'descending' if str(order.get('direction', '')).upper() == 'DESC' else 'ascending'}"
            for order in list(query.get("order_by", []) or [])
            if isinstance(order, dict) and order.get("field")
        ]
        text += f", first {limit} rows" + (f" by {', '.join(ordering)}" if ordering else "")
    return text + _with(query, _DESCRIBED_QUERY_KEYS | _RESPONSE_QUERY_KEYS)


def _with(item: dict[str, Any], used: set[str] | frozenset[str], prefix: str = "") -> str:
    """`` [with a, b]`` for the keys of ``item`` that were not rendered, or ``""``."""

    extras = sorted(
        prefix + key for key, value in item.items() if key not in used and value not in _EMPTY
    )
    return f" [with {', '.join(extras)}]" if extras else ""


def _describe_expression(
    expression: dict[str, Any], label: Callable[[Any], str], depth: int = 0
) -> str:
    """Describe one select expression; keys it can't render are listed, never dropped."""

    if depth > 4:
        return "..."
    kind = str(expression.get("kind", "") or "")
    used = {"kind"}

    def inner(key: str) -> str:
        used.add(key)
        value = expression.get(key)
        if not isinstance(value, dict):
            return "?"
        text = _describe_expression(value, label, depth + 1)
        # Parenthesize compound operands, so (a / b) / c never reads as a / (b / c).
        return f"({text})" if _is_compound(value) else text

    if expression.get("metric") and kind in {"", "metric"}:
        used.add("metric")
        text = label(expression["metric"])
    elif expression.get("measure") and kind in _MEASURE_KINDS:
        # Measure references, aggregates and the prior_period shorthand.
        used.update({"measure", "aggregation", "temporal_role"})
        text = label(expression["measure"])
        aggregation = str(expression.get("aggregation", "") or "")
        if aggregation:
            text += f" ({_AGGREGATION_WORDS.get(aggregation, aggregation.replace('_', ' '))})"
        if expression.get("temporal_role"):
            text += f" on {label(expression['temporal_role'])}"
        if kind == "prior_period":
            used.add("offset")
            if isinstance(expression.get("offset"), int):
                used.add("grain")  # the shorthand counts steps at `grain`
            text += f" {_describe_offset(expression)} earlier"
        if isinstance(expression.get("where"), list) and expression["where"]:
            used.add("where")
            text += " where " + " and ".join(
                _describe_filter(item, label) for item in expression["where"]
            )
    elif kind == "ratio":
        text = f"{inner('numerator')} / {inner('denominator')}"
    elif kind in {"arithmetic", "binary"}:
        used.add("op")
        op = str(expression.get("op", "") or "")
        text = f"({inner('left')} {_ARITHMETIC_SYMBOLS.get(op, op)} {inner('right')})"
    elif kind == "prior_period":
        used.add("offset")
        text = f"{inner('input')} {_describe_offset(expression)} earlier"
    elif kind == "rolling":
        used.add("window")
        text = f"{inner('input')} over a rolling {_describe_span(expression.get('window'))}"
    elif kind == "cumulative":
        text = f"cumulative {inner('input')}"
    elif kind == "period_to_date":
        used.add("period")
        text = f"{expression.get('period') or 'period'}-to-date {inner('input')}"
    elif kind == "conversion":
        text = f"conversion from {inner('base')} to {inner('converted')}"
        if expression.get("entity"):
            used.add("entity")
            text += f" per {label(expression['entity'])}"
        if isinstance(expression.get("window"), dict):
            used.add("window")
            text += f" within {_describe_span(expression['window'])}"
        mode_key = "matching_mode" if expression.get("matching_mode") else "matching"
        mode = str(expression.get(mode_key, "") or "")
        if mode:
            used.add(mode_key)
            text += f", matching {_MATCHING_MODES.get(mode, mode.replace('_', ' '))}"
    elif kind == "literal":
        used.add("value")
        text = _describe_value(expression.get("value"))
    else:
        text = kind.replace("_", " ") or "an expression"
    return text + _with(expression, used)


def _is_compound(expression: dict[str, Any]) -> bool:
    """Whether the rendered expression needs parentheses as an operand."""

    kind = str(expression.get("kind", "") or "")
    if kind in {"arithmetic", "binary", "literal"}:
        return False  # arithmetic brings its own parentheses
    if expression.get("metric") and kind in {"", "metric"}:
        return False
    return not (
        expression.get("measure")
        and kind in _MEASURE_KINDS - {"prior_period"}
        and not expression.get("where")
    )


def _describe_offset(expression: dict[str, Any]) -> str:
    offset = expression.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool):
        # Shorthand: a signed step count at `grain`.
        return _describe_span({"unit": expression.get("grain") or "period", "value": abs(offset)})
    return _describe_span(offset)


def _describe_span(span: Any) -> str:
    if not isinstance(span, dict):
        return "one period"
    unit = str(span.get("unit", "") or "period")
    value = span.get("value", 1)
    return f"{value} {unit}" + ("" if value == 1 else "s") + _with(span, {"unit", "value"})


def _describe_time(time: dict[str, Any], label: Callable[[Any], str]) -> str:
    used = {"temporal_role", "grain", "start", "end", "calendar_id"}
    role = str(time.get("temporal_role", "") or "")
    grain = str(time.get("grain", "") or "")
    parts: list[str] = []
    if grain:
        parts.append(f"per {grain}" + (f" of {label(role)}" if role else ""))
    elif role:
        parts.append(f"per {label(role)} value")
    calendar = str(time.get("calendar_id", "") or "")
    if calendar.strip().lower() not in {"", "default"}:
        parts.append(f"on calendar {label(calendar)}")
    if isinstance(time.get("fill"), bool):
        used.add("fill")
        if time["fill"]:
            parts.append("including periods with no data")
    window = time.get("range")
    extras = ""
    if isinstance(window, dict) and isinstance(window.get("last"), dict):
        used.add("range")
        parts.append(f"in the last {_describe_span(window['last'])}")
        extras = _with(window, {"last"}, prefix="time.range.")
    start, end = time.get("start"), time.get("end")
    if start and end:
        parts.append(f"from {start} to before {end}")
    elif start:
        parts.append(f"from {start}")
    elif end:
        parts.append(f"before {end}")
    extras += _with(time, used, prefix="time.")
    return (", ".join(parts) + extras).strip()


def _describe_filter(item: Any, label: Callable[[Any], str]) -> str:
    if not isinstance(item, dict):
        return str(item)
    key = "field" if item.get("field") else "dimension"
    if item.get(key):
        op = str(item.get("op", "") or "=").upper()
        if op in {"IS NULL", "IS NOT NULL"}:
            return f"{label(item[key])} {op.lower()}" + _with(item, {key, "op"})
        text = f"{label(item[key])} {op} {_describe_value(item.get('value'))}"
        return text + _with(item, {key, "op", "value"})
    if item.get("segment"):
        return f"in segment {label(item['segment'])}" + _with(item, {"segment"})
    return json.dumps(item, sort_keys=True, default=str)


def _describe_metric_filter(item: Any, label: Callable[[Any], str]) -> str:
    if not isinstance(item, dict):
        return str(item)
    expression = item.get("expression")
    subject = _describe_expression(expression, label) if isinstance(expression, dict) else ""
    op = str(item.get("op", "") or "")
    if subject and op:
        text = f"{subject} {op} {_describe_value(item.get('value'))}"
        return text + _with(item, {"expression", "op", "value"})
    if subject:
        return subject + _with(item, {"expression"})
    return json.dumps(item, sort_keys=True, default=str)


def _describe_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list | tuple):
        return "(" + ", ".join(_describe_value(item) for item in value) + ")"
    return str(value)


def _object_labels(runtime: Runtime, plan: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for row in list(dict(plan.get("best", {}) or {}).get("resolved", []) or []):
        if isinstance(row, dict) and row.get("id") and row.get("label"):
            labels[str(row["id"])] = str(row["label"])
    try:
        catalog = resolve_catalog(runtime, view="summary", verbosity="compact")
    except SemanticLayerError:
        return labels
    for key in ("entities", "dimensions", "measures", "metrics", "segments", "temporal_roles"):
        for entry in list(catalog.get(key, []) or []):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            text = entry.get("label") or entry.get("display_name")
            if text:
                labels[str(entry["id"])] = str(text)
    return labels
