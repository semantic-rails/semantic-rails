"""Row filters: package policies that limit a relation's rows to a trusted request attribute.

A ``row_filter`` policy names a dimension and a host-supplied attribute. When it applies to
a request, the compiler adds ``<column> = ?`` to the scan of the dimension's relation, and
the runtime binds the attribute to that parameter. Only the minimum query family is
qualified: a statement that reads exactly one physical relation, once, as a ``FROM``, which
an applicable filter covers. Any other statement is denied, never answered unfiltered.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, cast

from .errors import SemanticLayerError
from .schema import PackageConfig, SemanticPolicyConfig
from .sql_ast import (
    SqlBinary,
    SqlCte,
    SqlExpr,
    SqlIdentifier,
    SqlParameter,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
)
from .sql_preparation import ParameterSlot

ROW_FILTER = "row_filter"
_KEYS = frozenset({"dimension", "attribute", "type", "rule"})  # rule: the rationale alias
_SLOT_TYPES = frozenset({"string", "integer", "boolean"})
_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class RowFilter:
    policy_id: str
    table: str
    column: str
    slot: ParameterSlot


def row_filter(config: PackageConfig, policy: SemanticPolicyConfig) -> RowFilter:
    """Resolve one ``row_filter`` policy; anything it can't enforce is a config error."""

    def invalid(problem: str) -> SemanticLayerError:
        return SemanticLayerError("INVALID_CONFIG", f"row_filter policy '{policy.id}': {problem}")

    extra = sorted(set(policy.config) - _KEYS)
    if policy.object_ids:
        extra.append("object_ids")
    if policy.action:
        extra.append("action")
    if extra:
        raise invalid(f"unsupported keys {extra}; a row filter takes a dimension and an attribute")
    dimension_id = policy.config.get("dimension")
    dimension = next((row for row in config.dimensions if row.id == dimension_id), None)
    if dimension is None:
        raise invalid("'dimension' must name a dimension of this package")
    declared = dimension.data_type
    slot_type = str(policy.config.get("type", declared))
    if slot_type not in _SLOT_TYPES or declared not in {slot_type, "id"}:
        raise invalid(
            f"dimension '{dimension.id}' has type {declared!r}; a row filter compares a "
            "string, integer or boolean column (an id dimension needs 'type')"
        )
    entity = next((row for row in config.entities if row.id == dimension.entity), None)
    if entity is None or not entity.table or not _COLUMN.fullmatch(dimension.column):
        raise invalid(f"dimension '{dimension.id}' must be a plain column of its entity's relation")
    try:
        slot = ParameterSlot(str(policy.config.get("attribute", "")), cast(Any, slot_type))
    except ValueError as exc:
        raise invalid(str(exc)) from None
    return RowFilter(policy.id, entity.table, dimension.column, slot)


def validate_row_filters(config: PackageConfig) -> None:
    for policy in config.semantic_policies:
        if policy.kind == ROW_FILTER:
            row_filter(config, policy)


def apply_row_filters(
    sql: SqlSelect, filters: Sequence[RowFilter]
) -> tuple[SqlSelect, tuple[ParameterSlot, ...]]:
    """Filter the one relation ``sql`` reads, or deny the statement."""
    if not filters:
        return sql, ()
    nodes = list(_walk(sql))
    ctes = {node.name for node in nodes if isinstance(node, SqlCte)}
    reads = [
        node
        for node in nodes
        if isinstance(node, SqlTableFunction)
        or (isinstance(node, SqlTableRef) and node.name not in ctes)
    ]
    read = reads[0] if len(reads) == 1 and isinstance(reads[0], SqlTableRef) else None
    scan = next(
        (node for node in nodes if isinstance(node, SqlSelect) and node.from_table is read), None
    )
    applied = [row for row in filters if read is not None and row.table == read.name]
    # A relation named like one of the statement's CTEs would be read through the CTE.
    if read is None or scan is None or not applied or any(row.table in ctes for row in filters):
        raise SemanticLayerError(
            "POLICY_DENIED",
            "A row filter applies to this request, and only a query that reads the filtered "
            "relation once, without joins, rollups or other relations, can be answered under it.",
            details={
                "reason": "row_filter_unsupported_query",
                "policy_ids": sorted({row.policy_id for row in filters}),
            },
        )
    qualifier = (read.alias or read.name).split(".")
    condition = _conjunction(
        [
            SqlBinary(SqlIdentifier([*qualifier, row.column]), "=", SqlParameter(row.slot))
            for row in applied
        ]
    )
    if scan.where:
        # One AND node keeps the filter outside any OR in the existing conditions.
        condition = SqlBinary(condition, "AND", _conjunction(scan.where))
    filtered = _rewrite(sql, scan, replace(scan, where=[condition]))
    return filtered, tuple(row.slot for row in applied)


def _conjunction(items: Sequence[SqlExpr]) -> SqlExpr:
    out = items[0]
    for item in items[1:]:
        out = SqlBinary(out, "AND", item)
    return out


def _walk(node: Any) -> Iterator[Any]:
    """Every AST node, found generically so no node type can hide a read."""
    if isinstance(node, list | tuple):
        for item in node:
            yield from _walk(item)
    elif is_dataclass(node) and not isinstance(node, type):
        yield node
        for item in fields(node):
            yield from _walk(getattr(node, item.name))


def _rewrite(node: Any, target: Any, new: Any) -> Any:
    if node is target:
        return new
    if isinstance(node, list):
        items = [_rewrite(item, target, new) for item in node]
        return items if any(a is not b for a, b in zip(items, node, strict=True)) else node
    if is_dataclass(node) and not isinstance(node, type):
        changes = {}
        for item in fields(node):
            value = getattr(node, item.name)
            rewritten = _rewrite(value, target, new)
            if rewritten is not value:
                changes[item.name] = rewritten
        return replace(node, **changes) if changes else node
    return node
