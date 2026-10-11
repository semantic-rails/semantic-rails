"""What a call's answer assumed, one helper per code.

Each helper reads the compiled plan (and, for an executed query, the hidden columns the SQL
carried for it) and returns the ``assumptions`` entries of its code. An entry changes what the
numbers mean, so it survives every verbosity.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..compiler_parts.bind import aggregation_overrides, select_measure_reads
from ..compiler_parts.paths import display_key, keyed_entity
from ..config_parts.measure_governance import governed_out_of_view, governing_metrics
from ..db_parts.base import QueryRows
from ..errors import SemanticLayerError
from ..schema import PackageConfig, base_of


def hidden_columns(compiled: dict[str, Any]) -> list[str]:
    """The columns the SQL carries for the answer's assumptions, never for its rows."""
    return [
        *dict(compiled.get("override_rows") or {}).values(),
        *dict(compiled.get("share_groups") or {}).values(),
    ]


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


def _governors(config: PackageConfig, select: Any) -> list[tuple[str, list[str], bool]]:
    """For each measure the call reads raw: the governed metrics (``governing_metrics`` of the
    whole package) the caller sees and doesn't select, and whether one is hidden from them."""
    shown = {row.id for row in config.metric_recipes}
    selected = {
        str(node.get("metric"))
        for node in _nodes([row.get("expression") for row in select or []])
        if node.get("kind") == "metric"
    }
    out = []
    for measure in dict.fromkeys(str(node["measure"]) for node in select_measure_reads(select)):
        governors = [row.id for row in governing_metrics(base_of(config), measure)]
        visible = [metric for metric in governors if metric in shown and metric not in selected]
        out.append((measure, visible, governed_out_of_view(config, measure)))
    return out


def _nodes(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _nodes(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)


def _measure_ids(node: Any) -> Iterable[str]:
    """Every measure id a caller's expressions name, at any depth; a metric's own definition is
    the package's and is never expanded."""
    if isinstance(node, list):
        for item in node:
            yield from _measure_ids(item)
    elif isinstance(node, dict):
        measure = node.get("measure")
        if isinstance(measure, str) and measure.startswith("measure."):
            yield measure
        for value in node.values():
            yield from _measure_ids(value)


def refuse_hidden_governors(config: PackageConfig, query: dict[str, Any]) -> None:
    """A raw measure governed by a metric the caller can't see is never answered as a number,
    nor probed by a threshold: every measure the caller's select, having, entity filters and
    nested predicates read is checked. The refusal names neither the metric nor what it
    filters."""
    parts = {
        "select": query.get("select"),
        "having": query.get("metric_filters"),
        "where": query.get("where"),
    }
    for path, part in parts.items():
        for index, item in enumerate(part or []):
            for measure in dict.fromkeys(_measure_ids(item)):
                if not governed_out_of_view(config, measure):
                    continue
                at = f"select[{index}]" if path == "select" else path
                raise SemanticLayerError(
                    "POLICY_DENIED",
                    f"{at}: '{measure}' can't be answered as a raw number for you.",
                    details={
                        "path": at,
                        "measure": measure,
                        "recovery_hints": [
                            {
                                "kind": "ask_admin",
                                "message": "Ask a package admin which metric answers this for you.",
                            }
                        ],
                    },
                )


def governed_metric_exists(config: PackageConfig, compiled: dict[str, Any]) -> list[dict[str, Any]]:
    """``GOVERNED_METRIC_EXISTS`` for each governed metric that filters a measure the call reads
    raw: the package's definition of that number is the metric."""
    select = compiled["logical_plan"].query.get("select")
    return [
        {"code": "GOVERNED_METRIC_EXISTS", "measure": measure, "metric": metric}
        for measure, visible, _ in _governors(config, select)
        for metric in visible
    ]


def share_of_total(
    compiled: dict[str, Any], hidden: dict[str, list[Any]] | None = None
) -> list[dict[str, Any]]:
    """``SHARE_OF_TOTAL`` for each share column: a fraction of its input's total over the
    ``over`` columns, among the rows the query keeps, and how many rows that total spans when
    every returned row's total spans the same number."""
    columns = dict(compiled.get("share_groups") or {})
    out: list[dict[str, Any]] = []
    for row in compiled["logical_plan"].query.get("select") or []:
        expression = row.get("expression") or {}
        if expression.get("kind") != "share":
            continue
        source = expression.get("input") or {}
        entry: dict[str, Any] = {
            "code": "SHARE_OF_TOTAL",
            "column": row.get("as"),
            "input": source.get("measure") or source.get("metric"),
            "over": list(expression.get("over") or []),
            "unit": "fraction",
        }
        spans = set((hidden or {}).get(columns.get(row.get("as"), ""), []))
        if len(spans) == 1:
            entry["rows"] = int(spans.pop())
        out.append(entry)
    return out


def grouped_by_key(
    config: PackageConfig, compiled: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """``GROUPED_BY_KEY`` for each entity key the engine grouped by beside the name the call
    asked for, so same-named entities stay apart; ``SHOWN_BY_KEY`` for each grouped entity key
    whose entity declares no name (``display``), so its rows show only the key."""
    asked = list(payload.get("group_by") or [])
    grouped = list(compiled["logical_plan"].group_by)
    out: list[dict[str, Any]] = [
        {"code": "GROUPED_BY_KEY", "dimension": dimension, "key": key}
        for dimension in asked
        if (key := display_key(config, dimension)) and key in grouped and key not in asked
    ]
    for dimension in grouped:
        entity = keyed_entity(config, dimension)
        if entity is not None and not entity.display:
            out.append({"code": "SHOWN_BY_KEY", "entity": entity.id, "dimension": dimension})
    return out


def call_assumptions(
    config: PackageConfig,
    compiled: dict[str, Any],
    payload: dict[str, Any],
    hidden: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every assumption a call's answer states."""
    return [
        *aggregation_override(config, compiled, hidden),
        *governed_metric_exists(config, compiled),
        *share_of_total(compiled, hidden),
        *grouped_by_key(config, compiled, payload),
    ]
