"""Parse MetricFlow's Jinja-templated filter strings into Semantic
Rails filter ASTs.

MetricFlow filters live in YAML as Python strings that mix Jinja calls
with SQL fragments, for example::

    "{{ Dimension('booking__is_instant') }}"
    "{{ Dimension('user__home_state_latest') }} IN ('CA', 'HI', 'WA')"
    "{{ Dimension('user__home_state_latest') }} NOT IN ('CA')"
    "{{ Dimension('listing__country') }} = 'US'"
    "NOT {{ Dimension('booking__is_instant') }}"
    "{{ Entity('listing') }} IS NOT NULL"
    "{{ Metric('bookings', group_by=['listing']) }} > 2"
    "{{ Dimension('order__total_cents') }} BETWEEN 100 AND 500"
    "{{ Dimension('order__placed_at') }} NOT BETWEEN '2024-01-01' AND '2024-12-31'"

We don't run a real Jinja parser. Instead we recognize the common
shapes by regex and emit the equivalent Semantic Rails filter dict.
Anything we cannot match is returned as ``None`` and the caller is
expected to log a warning and skip the metric when the filter is unsupported.

``parse_filter`` returns one expression AST per recognized shape::

    {kind: comparison, op: "=", left: {kind: column, column: ...}, right: {kind: literal, value: true}}
    {kind: in,         expr: {kind: column, column: ...}, values: [{kind: literal, value: ...}, ...]}
    {kind: not_in,     expr: {kind: column, column: ...}, values: [{kind: literal, value: ...}, ...]}
    {kind: comparison, op: "=", left: {kind: column, column: ...}, right: {kind: literal, value: ...}}
    {kind: comparison, op: "!=", left: {kind: column, column: ...}, right: {kind: literal, value: null}}
    {kind: metric_predicate, input: {...}, op: ">", value: 2}
    {kind: between,    expr: {kind: column, column: ...}, low: {...}, high: {...}}
    {kind: not_between, expr: {kind: column, column: ...}, low: {...}, high: {...}}

A measure's filter is applied only in the ``{all: [{field, op, value}]}``
form, with each field named by dimension id, so the translator writes
what ``filter_clauses`` returns rather than these ASTs.
"""

from __future__ import annotations

import re
from typing import Any

_JINJA_RE = re.compile(r"\{\{\s*(\w+)\(([^)]*)\)\s*\}\}")
_DIM_NAME_RE = re.compile(r"['\"]([\w.]+?__)?(\w+)['\"]")
# One SQL literal: a quoted string (a doubled quote escapes itself) or a number.
_LITERAL = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|-?\d+(?:\.\d+)?"
_LITERAL_LIST_RE = re.compile(rf"\s*(?:{_LITERAL})\s*(?:,\s*(?:{_LITERAL})\s*)*")


def _parse_dim_arg(arg: str) -> str:
    """`'booking__is_instant'` -> column name `is_instant`.

    MetricFlow names dimensions as `entity__dim`. We drop the entity
    prefix because the Semantic Rails planner re-binds the dimension
    via the model's `entities:` block. The bare column name is what
    lives on the warehouse table.
    """
    m = _DIM_NAME_RE.search(arg)
    if not m:
        return arg.strip(" '\"")
    return m.group(2)


def _parse_value_list(literal: str) -> list[Any] | None:
    """Parse a comma-separated SQL value list like ``'CA', 'HI', 'WA'``
    into a Python list of strings/numbers, or ``None`` when it is
    anything but literals separated by commas (a quoted comma stays in
    its value)."""
    if not _LITERAL_LIST_RE.fullmatch(literal):
        return None
    return [_coerce_scalar(token) for token in re.findall(_LITERAL, literal)]


def _coerce_scalar(token: str) -> Any:
    """Coerce a single SQL literal token (``'CA'``, ``100``, ``2.5``,
    ``'2024-01-01'``) to its Python equivalent. Quoted tokens become
    strings; bare numeric tokens become int/float; anything else
    passes through unchanged so callers can decide what to do with
    unparseable values."""
    part = token.strip()
    if (part.startswith("'") and part.endswith("'")) or (
        part.startswith('"') and part.endswith('"')
    ):
        return part[1:-1].replace(part[0] * 2, part[0])
    if part.upper() in {"TRUE", "FALSE"}:
        return part.upper() == "TRUE"
    try:
        if "." in part:
            return float(part)
        return int(part)
    except ValueError:
        return part


def parse_filter(filter_str: str) -> dict[str, Any] | None:
    """Best-effort parse of a MetricFlow filter string. Returns None
    when the shape isn't one we recognize — the translator surfaces
    that as a warning rather than emit a wrong filter."""
    if not filter_str or not filter_str.strip():
        return None
    raw = filter_str.strip()

    # 1) Bare-truthy boolean dimension: "{{ Dimension('x__is_y') }}"
    m = re.fullmatch(r"\{\{\s*Dimension\(([^)]*)\)\s*\}\}", raw)
    if m:
        col = _parse_dim_arg(m.group(1))
        return {
            "kind": "comparison",
            "op": "=",
            "left": {"kind": "column", "column": col},
            "right": {"kind": "literal", "value": True},
        }

    # 2) Negated boolean dimension: "NOT {{ Dimension('x__y') }}"
    m = re.fullmatch(r"NOT\s+\{\{\s*Dimension\(([^)]*)\)\s*\}\}", raw, re.IGNORECASE)
    if m:
        col = _parse_dim_arg(m.group(1))
        return {
            "kind": "comparison",
            "op": "=",
            "left": {"kind": "column", "column": col},
            "right": {"kind": "literal", "value": False},
        }

    # 3) Dimension IN (...): "{{ Dimension('x__y') }} IN ('A', 'B')"
    m = re.fullmatch(
        r"\{\{\s*Dimension\(([^)]*)\)\s*\}\}\s+IN\s*\(\s*(.*?)\s*\)",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        col = _parse_dim_arg(m.group(1))
        values = _parse_value_list(m.group(2))
        if values is None:
            return None
        return {
            "kind": "in",
            "expr": {"kind": "column", "column": col},
            "values": [{"kind": "literal", "value": v} for v in values],
        }

    # 3b) Dimension NOT IN (...): "{{ Dimension('x__y') }} NOT IN ('A', 'B')"
    m = re.fullmatch(
        r"\{\{\s*Dimension\(([^)]*)\)\s*\}\}\s+NOT\s+IN\s*\(\s*(.*?)\s*\)",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        values = _parse_value_list(m.group(2))
        if values is None:
            return None
        return {
            "kind": "not_in",
            "expr": {"kind": "column", "column": _parse_dim_arg(m.group(1))},
            "values": [{"kind": "literal", "value": v} for v in values],
        }

    # 3c) Dimension compared with a literal: "{{ Dimension('x__y') }} = 'A'"
    m = re.fullmatch(
        rf"\{{\{{\s*Dimension\(([^)]*)\)\s*\}}\}}\s*(<>|!=|<=|>=|=|<|>)\s*({_LITERAL}|TRUE|FALSE)",
        raw,
        re.IGNORECASE,
    )
    if m:
        return {
            "kind": "comparison",
            "op": "!=" if m.group(2) == "<>" else m.group(2),
            "left": {"kind": "column", "column": _parse_dim_arg(m.group(1))},
            "right": {"kind": "literal", "value": _coerce_scalar(m.group(3))},
        }

    # 4) Entity IS NOT NULL: "{{ Entity('x') }} IS NOT NULL"
    m = re.fullmatch(r"\{\{\s*Entity\(([^)]*)\)\s*\}\}\s+IS\s+NOT\s+NULL", raw, re.IGNORECASE)
    if m:
        ent = _parse_dim_arg(m.group(1))
        return {
            "kind": "comparison",
            "op": "!=",
            "left": {"kind": "column", "column": f"{ent}_id"},
            "right": {"kind": "literal", "value": None},
        }

    # 5) Dimension BETWEEN lo AND hi: "{{ Dimension('x__y') }} [NOT] BETWEEN 0 AND 100"
    # Accepts numeric tokens or single-quoted scalars (for date / string
    # ranges). The boundaries are treated as opaque SQL literals — we
    # don't validate type compatibility with the dimension; that's the
    # runtime's job. Mapping to Semantic Rails ``between`` / ``not_between``
    # lets the planner desugar at parse time to the canonical
    # ``expr >= low AND expr <= high`` (or the inverted OR form) pattern.
    m = re.fullmatch(
        r"\{\{\s*Dimension\(([^)]*)\)\s*\}\}\s+(NOT\s+)?BETWEEN\s+"
        r"('[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?)\s+AND\s+"
        r"('[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?)",
        raw,
        re.IGNORECASE,
    )
    if m:
        col = _parse_dim_arg(m.group(1))
        negated = m.group(2) is not None
        low = _coerce_scalar(m.group(3))
        high = _coerce_scalar(m.group(4))
        return {
            "kind": "not_between" if negated else "between",
            "expr": {"kind": "column", "column": col},
            "low": {"kind": "literal", "value": low},
            "high": {"kind": "literal", "value": high},
        }

    # 6) Metric predicate: "{{ Metric('m', group_by=['e']) }} > 2"
    m = re.fullmatch(
        r"\{\{\s*Metric\(\s*['\"](\w+)['\"]\s*(?:,\s*group_by\s*=\s*\[([^\]]*)\])?\s*\)\s*\}\}\s*(>=|<=|=|!=|>|<)\s*(-?\d+(?:\.\d+)?)",
        raw,
    )
    if m:
        metric_name, _gb_raw, op, val_raw = m.groups()
        value: Any
        try:
            value = float(val_raw) if "." in val_raw else int(val_raw)
        except ValueError:
            value = val_raw  # pragma: no cover — regex already constrains
        return {
            "kind": "metric_predicate",
            "scope_mode": "contextual",
            "input": {
                "kind": "metric",
                "metric": metric_name,
            },
            "op": op,
            "value": value,
        }

    return None


def filter_clauses(
    filter_str: str, dimensions: dict[str, str | None]
) -> tuple[list[dict[str, Any]], str]:
    """A MetricFlow filter as the engine's measure filter clauses, or why not.

    The engine applies a metric's filter as ``{all: [{field, op, value}]}``,
    naming each field by dimension id. ``dimensions`` maps MetricFlow's
    ``entity__dimension`` references to those ids; a time dimension maps to
    ``None``, because MetricFlow compares it at its own grain rather than as
    the raw column. Returns ``(clauses, "")``, or ``([], reason)`` when the
    filter can't be written that way.
    """
    node = parse_filter(filter_str)
    if node is None:
        return [], f"could not parse filter `{filter_str!r}`"
    kind = node["kind"]
    if kind == "metric_predicate":
        return [], f"filter `{filter_str}` is a metric predicate, which mf2sr doesn't translate"
    if kind == "not_between":
        return (
            [],
            f"filter `{filter_str}` needs OR (NOT BETWEEN), which a metric filter can't express",
        )
    calls = _JINJA_RE.findall(filter_str)
    if len(calls) != 1:
        return (
            [],
            f"filter `{filter_str}` names {len(calls)} objects; mf2sr translates one dimension",
        )
    function, arguments = calls[0]
    if function != "Dimension":
        return (
            [],
            f"filter `{filter_str}` tests {'an entity' if function == 'Entity' else function}, not a dimension",
        )
    reference = arguments.strip()
    quoted = re.fullmatch(r"(['\"])([\w.]+)\1", reference)
    if quoted is None:
        return (
            [],
            f"filter `{filter_str}`: mf2sr doesn't translate Dimension options such as entity_path",
        )
    reference = quoted.group(2)
    if reference not in dimensions:
        return [], f"filter `{filter_str}`: `{reference}` is not a dimension in this project"
    field = dimensions[reference]
    if field is None:
        return [], (
            f"filter `{filter_str}` compares time dimension `{reference}`, which MetricFlow "
            "truncates to its grain and mf2sr doesn't translate"
        )
    if kind == "comparison":
        return [{"field": field, "op": node["op"], "value": node["right"]["value"]}], ""
    if kind in {"in", "not_in"}:
        values = [value["value"] for value in node["values"]]
        return [{"field": field, "op": "in" if kind == "in" else "not in", "value": values}], ""
    if kind == "between":
        return [
            {"field": field, "op": ">=", "value": node["low"]["value"]},
            {"field": field, "op": "<=", "value": node["high"]["value"]},
        ], ""
    return [], f"filter `{filter_str}` has a shape mf2sr doesn't translate"
