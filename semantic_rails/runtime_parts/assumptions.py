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
from ..expressions import _opaque_expression_data
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
        over = ""
        if hidden is not None and columns.get(alias) in hidden:
            entry["rows"] = _total(hidden[columns[alias]])
            over = f", over {entry['rows']} source rows"
        entry["message"] = (
            f"{measure} is read as {aggregation}, not its default {entry['default']}{over}."
        )
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


def _expression_nodes(node: Any) -> Iterable[dict[str, Any]]:
    """Every node of a caller's expressions, parents first; literal and parameter values are
    data, not nodes."""
    if isinstance(node, list):
        for item in node:
            yield from _expression_nodes(item)
    elif isinstance(node, dict):
        yield node
        for key, value in node.items():
            if not _opaque_expression_data(node, key):
                yield from _expression_nodes(value)


def _measure_ids(node: Any) -> Iterable[str]:
    """Every measure id a caller's expressions name, at any depth; a metric's own definition is
    the package's and is never expanded."""
    for row in _expression_nodes(node):
        measure = row.get("measure")
        if isinstance(measure, str) and measure.startswith("measure."):
            yield measure


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
    """``GOVERNED_METRIC_EXISTS`` once for each measure the call reads raw that governed metrics
    the caller sees filter: the package's definition of that number is one of them. It names
    the first three and counts them all."""
    select = compiled["logical_plan"].query.get("select")
    out = []
    for measure, visible, _ in _governors(config, select):
        if not visible:
            continue
        more = f" and {len(visible) - 3} more" if len(visible) > 3 else ""
        out.append(
            {
                "code": "GOVERNED_METRIC_EXISTS",
                "measure": measure,
                "metrics": visible[:3],
                "count": len(visible),
                "message": (
                    f"{measure} is also defined as a governed metric "
                    f"({', '.join(visible[:3])}{more}). Select "
                    f"{'one' if len(visible) > 1 else 'it'} to use the package's definition."
                ),
            }
        )
    return out


def share_of_total(
    compiled: dict[str, Any], hidden: dict[str, list[Any]] | None = None
) -> list[dict[str, Any]]:
    """``SHARE_OF_TOTAL`` for every share in a select expression: its input's total over the
    ``over`` columns, among the rows the query keeps, and how many rows that total spans when
    every returned row's total spans the same number."""
    columns = dict(compiled.get("share_groups") or {})
    out: list[dict[str, Any]] = []
    for row in compiled["logical_plan"].query.get("select") or []:
        expression = row.get("expression") or {}
        for share in _shares(expression):
            source = share.get("input") or {}
            entry: dict[str, Any] = {
                "code": "SHARE_OF_TOTAL",
                "column": row.get("as"),
                "input": source.get("measure") or source.get("metric"),
                "over": list(share.get("over") or []),
                "unit": "fraction",
            }
            # The hidden row count belongs only to a bare share's denominator.
            spans = (
                set((hidden or {}).get(columns.get(row.get("as"), ""), []))
                if share is expression
                else set()
            )
            if len(spans) == 1:
                entry["rows"] = int(spans.pop())
            rows = f" ({entry['rows']} rows per total)" if "rows" in entry else ""
            subject = entry["column"] if share is expression else f"The share in {entry['column']}"
            entry["message"] = (
                f"{subject} is a fraction of the total of {entry['input']} across "
                f"{' and '.join(entry['over'])} in the rows the call keeps{rows}."
            )
            out.append(entry)
    return out


def _shares(node: Any) -> Iterable[dict[str, Any]]:
    """Select expression shares at any depth."""
    return (row for row in _expression_nodes(node) if row.get("kind") == "share")


def grouped_by_key(
    config: PackageConfig, compiled: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """``GROUPED_BY_KEY`` for each entity key the engine grouped by beside the name the call
    asked for, so same-named entities stay apart; ``SHOWN_BY_KEY`` for each grouped entity key
    whose entity declares no name (``display``), so its rows show only the key."""
    asked = list(payload.get("group_by") or [])
    grouped = list(compiled["logical_plan"].group_by)
    out: list[dict[str, Any]] = [
        {
            "code": "GROUPED_BY_KEY",
            "dimension": dimension,
            "key": key,
            "message": f"Grouped by {key} beside {dimension}, so namesakes stay apart.",
        }
        for dimension in asked
        if (key := display_key(config, dimension)) and key in grouped and key not in asked
    ]
    entities = {row.id: row.entity for row in config.dimensions}
    for dimension in grouped:
        entity = keyed_entity(config, dimension)
        # A call that groups by another of the entity's dimensions shows more than the key.
        if (
            entity is None
            or entity.display
            or any(
                entities.get(other) == entity.id and keyed_entity(config, other) is None
                for other in grouped
            )
        ):
            continue
        out.append(
            {
                "code": "SHOWN_BY_KEY",
                "entity": entity.id,
                "dimension": dimension,
                "message": f"{entity.id} declares no name, so its rows show only {dimension}.",
            }
        )
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
