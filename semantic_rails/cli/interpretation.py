"""Plain-language restatement of what a planned query call computes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..catalog_service import resolve_catalog
from ..errors import SemanticLayerError
from ..runtime import Runtime

_AGGREGATION_WORDS = {"avg": "average", "count_distinct": "count distinct"}


_ARITHMETIC_SYMBOLS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}


_MEASURE_KINDS = {"measure", "aggregate", "semi_additive", "scoped_aggregate"}


_MATCHING_MODES = {
    "first_converted_after_base": "the first conversion after each base event",
    "closest_converted_after_base": "the closest conversion after each base event",
}


# How each filter word reads; ``missing`` reads by its value.
_OP_WORDS = {
    "is": "is",
    "not": "is not",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "contains": "contains",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "not_contains": "does not contain",
    "not_starts_with": "does not start with",
    "not_ends_with": "does not end with",
}


# How an entity filter's aggregate is taken.
_OVER_WORDS = {
    "all_time": "over all time",
    "window": "in the window",
    "period": "in each period",
}


# Call keys the restatement spells out; ``version`` is ignored by the engine. Any other key
# changes what runs, so it is listed as ``[with ...]``.
_DESCRIBED_QUERY_KEYS = frozenset(
    {"select", "group_by", "time", "where", "having", "limit", "order_by", "version"}
)


_EMPTY: tuple[Any, ...] = (None, "", [], {})


def describe_query(query: dict[str, Any], labels: dict[str, str] | None = None) -> str:
    """Restate in words what a query call computes.

    The text comes from the call itself, never from the question, so it shows what will
    actually run. Shapes it doesn't know fall back to their ids or kinds instead of guessing.
    """

    names = dict(labels or {})

    def label(object_id: Any) -> str:
        text = str(object_id or "")
        return names.get(text, text)

    subjects: list[str] = []
    aliases: dict[str, str] = {}
    for item in list(query.get("select", []) or []):
        if isinstance(item, str):
            subject = label(item)
            aliases[item.split(".")[-1]] = subject
        elif isinstance(item, dict):
            alias = str(item.get("as", "") or "")
            described = _describe_expression(item.get("expression"), label)
            subject = (described or alias or "an expression") + _with(item, {"as", "expression"})
            if alias:
                aliases[alias] = subject
        else:
            continue
        subjects.append(subject)
    text = " and ".join(filter(None, [", ".join(subjects[:-1]), *subjects[-1:]])) or "no measures"

    group_by = [label(dimension) for dimension in list(query.get("group_by", []) or [])]
    if group_by:
        text += " by " + ", ".join(group_by)
    time_text = _describe_time(dict(query.get("time", {}) or {}), label)
    if time_text:
        text += ", " + time_text
    filters = [_describe_filter(item, label) for item in list(query.get("where", []) or [])]
    filters += [
        _describe_condition(item, lambda field: aliases.get(str(field), label(field)))
        for item in list(query.get("having", []) or [])
    ]
    if filters:
        text += ", where " + " and ".join(filters)
    limit = query.get("limit")
    order_by = list(query.get("order_by", []) or [])
    ordering = [
        f"{aliases.get(str(order.get('field', '')), label(order.get('field')))} "
        f"{'descending' if str(order.get('direction', '')).lower() == 'desc' else 'ascending'}"
        for order in order_by
        if isinstance(order, dict) and order.get("field")
    ]
    has_limit = isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0
    if has_limit:
        text += f", first {limit} rows" + (f" by {', '.join(ordering)}" if ordering else "")
    elif ordering:
        text += f", ordered by {', '.join(ordering)}"
    described_keys = set(_DESCRIBED_QUERY_KEYS)
    if len(ordering) != len(order_by):
        described_keys.remove("order_by")
    if limit is not None and not has_limit:
        described_keys.remove("limit")
    return text + _with(query, described_keys)


def _with(item: dict[str, Any], used: set[str] | frozenset[str], prefix: str = "") -> str:
    """`` [with a, b]`` for the keys of ``item`` that were not rendered, or ``""``."""

    extras = sorted(
        prefix + key for key, value in item.items() if key not in used and value not in _EMPTY
    )
    return f" [with {', '.join(extras)}]" if extras else ""


def _describe_expression(expression: Any, label: Callable[[Any], str], depth: int = 0) -> str:
    """Describe one select expression; keys it can't render are listed, never dropped."""

    if isinstance(expression, str):
        return label(expression)
    if not isinstance(expression, dict):
        return ""
    if depth > 4:
        return "..."
    kind = str(expression.get("kind", "") or "")
    used = {"kind"}

    def inner(key: str) -> str:
        used.add(key)
        value = expression.get(key)
        if not isinstance(value, (dict, str)):
            return "?"
        text = _describe_expression(value, label, depth + 1)
        # Parenthesize compound operands, so (a / b) / c never reads as a / (b / c).
        return f"({text})" if _is_compound(value) else text

    if kind in _MEASURE_KINDS and expression.get("measure"):
        used.update({"measure", "aggregation", "temporal_role"})
        text = label(expression["measure"])
        aggregation = str(expression.get("aggregation", "") or "")
        if aggregation:
            text += f" ({_AGGREGATION_WORDS.get(aggregation, aggregation.replace('_', ' '))})"
        if expression.get("temporal_role"):
            text += f" on {label(expression['temporal_role'])}"
        if isinstance(expression.get("where"), list) and expression["where"]:
            used.add("where")
            text += " where " + " and ".join(
                _describe_filter(item, label) for item in expression["where"]
            )
    elif kind == "ratio":
        text = f"{inner('numerator')} / {inner('denominator')}"
    elif kind == "arithmetic":
        used.add("op")
        op = str(expression.get("op", "") or "")
        text = f"({inner('left')} {_ARITHMETIC_SYMBOLS.get(op, op)} {inner('right')})"
    elif kind == "prior_period":
        used.add("offset")
        text = f"{inner('input')} {_describe_span(expression.get('offset'))} earlier"
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
        mode = str(expression.get("matching_mode", "") or "")
        if mode:
            used.add("matching_mode")
            text += f", matching {_MATCHING_MODES.get(mode, mode.replace('_', ' '))}"
    elif kind == "literal":
        used.add("value")
        text = _describe_value(expression.get("value"))
    else:
        text = kind.replace("_", " ") or "an expression"
    return text + _with(expression, used)


def _is_compound(expression: Any) -> bool:
    """Whether the rendered expression needs parentheses as an operand."""

    if isinstance(expression, str):
        return False
    kind = str(expression.get("kind", "") or "")
    if kind in {"arithmetic", "literal"}:
        return False  # arithmetic brings its own parentheses
    return not (
        expression.get("measure") and kind in _MEASURE_KINDS and not expression.get("where")
    )


def _describe_span(span: Any) -> str:
    if not isinstance(span, dict):
        return "one period"
    unit = str(span.get("unit", "") or "period")
    value = span.get("value", 1)
    return f"{value} {unit}" + ("" if value == 1 else "s") + _with(span, {"unit", "value"})


def _describe_time(time: dict[str, Any], label: Callable[[Any], str]) -> str:
    used = {"temporal_role", "grain", "start", "end", "last", "this"}
    role = str(time.get("temporal_role", "") or "")
    grain = str(time.get("grain", "") or "")
    parts: list[str] = []
    if grain:
        parts.append(f"per {grain}" + (f" of {label(role)}" if role else ""))
    elif role:
        parts.append(f"on {label(role)}")
    if isinstance(time.get("fill"), bool):
        used.add("fill")
        if time["fill"]:
            parts.append("including periods with no data")
    extras = ""
    if isinstance(time.get("last"), dict):
        parts.append(f"in the last {_describe_span(time['last'])}")
    if time.get("this"):
        parts.append(f"this {time['this']} so far")
    start, end = time.get("start"), time.get("end")
    if start and end:
        parts.append(f"from {start} to before {end}")
    elif start:
        parts.append(f"from {start}")
    elif end:
        parts.append(f"before {end}")
    extras += _with(time, used, prefix="time.")
    return (", ".join(parts) + extras).strip()


def _describe_condition(item: Any, name: Callable[[Any], str]) -> str:
    """One ``{field, op, value}`` condition in words."""

    if not isinstance(item, dict) or not item.get("field"):
        return json.dumps(item, sort_keys=True, default=str)
    op = str(item.get("op", "") or "")
    subject = name(item["field"])
    if op == "missing":
        text = f"{subject} has {'no' if item.get('value') else 'a'} value"
    else:
        text = f"{subject} {_OP_WORDS.get(op, op)} {_describe_value(item.get('value'))}"
    return text + _with(item, {"field", "op", "value"})


def _describe_filter(item: Any, label: Callable[[Any], str]) -> str:
    """A ``where`` item: a row condition, or an entity filter."""

    if not isinstance(item, dict):
        return str(item)
    if not item.get("entity"):
        return _describe_condition(item, label)
    entity = label(item["entity"])
    for part, quantifier in (("any", "some"), ("none", "no")):
        if isinstance(item.get(part), list):
            conditions = " and ".join(_describe_condition(row, label) for row in item[part])
            return f"{quantifier} {entity} has {conditions}" + _with(item, {"entity", part})
    if isinstance(item.get("having"), list):
        conditions = " and ".join(_describe_condition(row, label) for row in item["having"])
        over = _OVER_WORDS.get(str(item.get("over", "")), str(item.get("over", "")))
        return (
            f"the {entity}'s {conditions}"
            + (f" {over}" if over else "")
            + _with(item, {"entity", "having", "over"})
        )
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
