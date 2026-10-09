"""What a call's answer assumed, one helper per code.

Each helper reads the compiled plan (and, for an executed query, the hidden columns the SQL
carried for it) and returns the ``assumptions`` entries of its code. An entry changes what the
numbers mean, so it survives every verbosity.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..compiler_parts.bind import aggregation_overrides
from ..db_parts.base import QueryRows
from ..schema import PackageConfig


def hidden_columns(compiled: dict[str, Any]) -> list[str]:
    """The columns the SQL carries for the answer's assumptions, never for its rows."""
    return list(dict(compiled.get("override_rows") or {}).values())


def split_hidden(
    rows: list[dict[str, Any]], compiled: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
    """``rows`` without the hidden columns, and each hidden column's values by name. A warehouse
    that folds unquoted aliases returns them in its own case."""
    names = hidden_columns(compiled)
    if not names or not rows:
        return rows, {name: [] for name in names}
    returned = {key.casefold(): key for key in rows[0]}
    found = {name: returned[name.casefold()] for name in names if name.casefold() in returned}
    values = {name: [row.get(key) for row in rows] for name, key in found.items()}
    dropped = set(found.values())
    kept = [{key: value for key, value in row.items() if key not in dropped} for row in rows]
    return QueryRows(kept, truncated=bool(getattr(rows, "truncated", False))), values


def aggregation_override(
    config: PackageConfig, compiled: dict[str, Any], hidden: dict[str, list[Any]] | None = None
) -> list[dict[str, Any]]:
    """``AGGREGATION_OVERRIDE`` for each measure the call reads with a statistic other than its
    default, with the source rows it aggregated in the returned rows when the query ran."""
    plan = compiled["logical_plan"]
    defaults = {row.id: row.default_aggregation for row in config.measures}
    columns = dict(compiled.get("override_rows") or {})
    out: list[dict[str, Any]] = []
    for alias, (measure, aggregation) in aggregation_overrides(
        plan.query.get("select"), config
    ).items():
        entry: dict[str, Any] = {
            "code": "AGGREGATION_OVERRIDE",
            "measure": measure,
            "aggregation": aggregation,
            "default": defaults.get(measure, ""),
        }
        if hidden is not None and columns.get(alias) in hidden:
            entry["rows"] = _total(hidden[columns[alias]])
        out.append(entry)
    return out


def _total(values: Iterable[Any]) -> int:
    return sum(int(value) for value in values if value is not None)


def call_assumptions(
    config: PackageConfig, compiled: dict[str, Any], hidden: dict[str, list[Any]] | None = None
) -> list[dict[str, Any]]:
    """Every assumption a call's answer states."""
    return [*aggregation_override(config, compiled, hidden)]
