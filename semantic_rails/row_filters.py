"""Tenant-filter every supported base scan; deny unsupported shapes.

One ordinary scan plus engine-tagged observation/coverage scans may read the same
physical relation. Every scan gets the same bound predicate; joins remain denied.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, cast

from .errors import SemanticLayerError
from .policy_rules import policy_action
from .schema import PackageConfig, SemanticPolicyConfig
from .sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCast,
    SqlCte,
    SqlExpr,
    SqlIdentifier,
    SqlLiteral,
    SqlParameter,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
)
from .sql_preparation import ParameterSlot

ROW_FILTER = "row_filter"
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

    policy_action(policy)

    def invalid(problem: str) -> SemanticLayerError:
        return SemanticLayerError("INVALID_CONFIG", f"row_filter policy '{policy.id}': {problem}")

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
    # A relation-pipeline entity is read through a CTE of the same name, never directly.
    if (
        entity is None
        or not entity.table
        or entity.relation_id
        or not _COLUMN.fullmatch(dimension.column)
    ):
        raise invalid(f"dimension '{dimension.id}' must be a plain column of its entity's relation")
    try:
        slot = ParameterSlot(str(policy.config.get("attribute", "")), cast(Any, slot_type))
    except ValueError as exc:
        raise invalid(str(exc)) from None
    return RowFilter(policy.id, entity.table, dimension.column, slot)


def is_row_filter(policy: SemanticPolicyConfig) -> bool:
    """Whether ``policy`` is a row filter; a near miss is an error, never an ignored policy."""
    if policy.kind == ROW_FILTER:
        return True
    kind = re.sub(r"[^a-z]", "", str(policy.kind).lower()).rstrip("s")
    if kind == "rowfilter" or "attribute" in policy.config:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"policy '{policy.id}' of kind {policy.kind!r} looks like a row filter; "
            f"use kind '{ROW_FILTER}' exactly",
        )
    return False


def validate_row_filters(config: PackageConfig) -> None:
    for policy in config.semantic_policies:
        if is_row_filter(policy):
            row_filter(config, policy)


def apply_row_filters(
    sql: SqlSelect, filters: Sequence[RowFilter]
) -> tuple[SqlSelect, tuple[ParameterSlot, ...]]:
    """Filter every qualified scan, or refuse the entire statement."""
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
    selects = [node for node in nodes if isinstance(node, SqlSelect)]
    scan_of = {id(node.from_table): node for node in selects if node.from_table is not None}
    scans = [read for read in reads if isinstance(read, SqlTableRef) and id(read) in scan_of]
    returning = [read for read in scans if not _observes(scan_of[id(read)])]
    read = (returning or scans)[0] if scans and len(scans) == len(reads) else None
    applied = [row for row in filters if read is not None and row.table == read.name]
    if (
        read is None
        or len(returning) > 1
        or len({id(scan) for scan in scans}) != len(scans)
        or any(scan.name != read.name for scan in scans)
        or not applied
        or any(row.table in ctes for row in filters)
    ):
        raise _unsupported(filters)
    filtered = sql
    for ref in scans:
        scan = next(
            (n for n in _walk(filtered) if isinstance(n, SqlSelect) and n.from_table is ref), None
        )
        if scan is None:
            raise _unsupported(filters)
        qualifier = (ref.alias or ref.name).split(".")
        condition = _conjunction(
            [
                SqlBinary(SqlIdentifier([*qualifier, row.column]), "=", SqlParameter(row.slot))
                for row in applied
            ]
        )
        if scan.where:
            # One AND node keeps the filter outside any OR in the existing conditions.
            condition = SqlBinary(condition, "AND", _conjunction(scan.where))
        filtered = _rewrite(filtered, scan, replace(scan, where=[condition]))
    if sum(isinstance(node, SqlParameter) for node in _walk(filtered)) != len(applied) * len(scans):
        raise _unsupported(filters)  # a rewrite missed a scan: never run it unfiltered
    return filtered, tuple(row.slot for row in applied) * len(scans)


_OBSERVING = frozenset({"MIN", "MAX", "COUNT"})


def _aggregate_only(expr: Any) -> bool:
    if isinstance(expr, SqlBinary):
        return _aggregate_only(expr.left) and _aggregate_only(expr.right)
    if isinstance(expr, SqlCast):
        return _aggregate_only(expr.expr)
    return isinstance(expr, SqlLiteral) or (
        isinstance(expr, SqlCall) and expr.name.upper() in _OBSERVING
    )


def _observes(scan: SqlSelect) -> bool:
    """Only engine-tagged, join-free constant/aggregate scans may be repeated."""
    return (
        scan.observation_scan
        and not (scan.joins or scan.ctes or scan.group_by)
        and all(_aggregate_only(item.expression) for item in scan.select)
    )


def _unsupported(filters: Sequence[RowFilter]) -> SemanticLayerError:
    return SemanticLayerError(
        "POLICY_DENIED",
        "A row filter applies to this request, and only a query that reads the filtered "
        "relation with one ordinary scan and filtered observation scans, without joins or "
        "other relations, can be answered under it.",
        details={
            "reason": "row_filter_unsupported_query",
            "policy_ids": sorted({row.policy_id for row in filters}),
        },
    )


def _conjunction(items: Sequence[SqlExpr]) -> SqlExpr:
    """``AND`` of ``items``, balanced so a long condition list can't exhaust the recursion limit."""
    if len(items) == 1:
        return items[0]
    middle = len(items) // 2
    return SqlBinary(_conjunction(items[:middle]), "AND", _conjunction(items[middle:]))


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
    if isinstance(node, list | tuple):
        items = [_rewrite(item, target, new) for item in node]
        changed = any(a is not b for a, b in zip(items, node, strict=True))
        return type(node)(items) if changed else node
    if is_dataclass(node) and not isinstance(node, type):
        changes = {}
        for item in fields(node):
            value = getattr(node, item.name)
            rewritten = _rewrite(value, target, new)
            if rewritten is not value:
                changes[item.name] = rewritten
        return replace(node, **changes) if changes else node
    return node
