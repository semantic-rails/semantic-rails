"""Allocate CTE and relation aliases, then rewrite their bound references."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass, replace
from itertools import count
from typing import Any

from ..sql_ast import (
    ROW_FILTER_OWNER,
    SqlCte,
    SqlIdentifier,
    SqlLiteral,
    SqlParameter,
    SqlPipelineCte,
    SqlSelect,
    SqlSourceColumn,
    SqlSourceCte,
    SqlSourceRef,
    SqlTableFunction,
    SqlTableRef,
    sql_nodes,
)


def reserve_name(candidates: Iterable[str], occupied: set[str], *, paired: bool = False) -> str:
    """Claim the first free spelling and, for a probe, its companion rows name."""
    for candidate in candidates:
        names = {candidate.casefold()}
        if paired:
            names.add(f"{candidate.casefold()}_rows")
        if names.isdisjoint(occupied):
            occupied.update(names)
            return candidate
    raise AssertionError("Generated names require an unbounded sequence of candidates")


def _namespace_sql_select(select: SqlSelect, prefix: str) -> SqlSelect:
    ctes = [node for node in sql_nodes(select) if isinstance(node, SqlCte)]
    if not ctes:
        return select
    rename = {cte.name: f"{prefix}{cte.name}" for cte in ctes}
    return _rewrite_names(
        select, rename, {}, {cte.name for cte in ctes if isinstance(cte, SqlSourceCte)}
    )


def avoid_name_collisions(
    select: SqlSelect, reserved: Iterable[str] = ()
) -> tuple[SqlSelect, dict[str, str]]:
    """Separate package relations from generated names before WITH clauses are hoisted."""
    nodes = list(sql_nodes(select))
    sourced = {node.name for node in nodes if isinstance(node, SqlSourceCte)}
    sources = {node.name for node in nodes if isinstance(node, SqlSourceRef)}
    blocked = {
        part.casefold() for name in [*reserved, *sources, *sourced] for part in name.split(".")
    }
    ctes = {
        node.name
        for node in nodes
        if isinstance(node, SqlCte) and not isinstance(node, SqlSourceCte)
    }
    aliases = {node.alias for node in nodes if isinstance(node, SqlTableRef | SqlTableFunction)}
    occupied = blocked | {name.casefold() for name in ctes | aliases}
    paired = {
        name
        for name in ctes
        if f"{name}_rows" in ctes and name.rsplit("__", 1)[-1].startswith(("edge_", "observed_"))
    }
    rename: dict[str, str] = {}

    def available(name: str, *, pair: bool = False) -> str:
        return reserve_name((f"{name}_{index}" for index in count(2)), occupied, paired=pair)

    for name in sorted(ctes - {f"{name}_rows" for name in paired}):
        rows = f"{name}_rows"
        if name.casefold() in blocked or (name in paired and rows.casefold() in blocked):
            rename[name] = available(name, pair=name in paired)
            if name in paired:
                rename[rows] = f"{rename[name]}_rows"
    alias_rename = {
        name: rename.get(name) or available(name)
        for name in sorted(aliases - {"", ROW_FILTER_OWNER})
        if name.casefold() in blocked
    }
    if not (rename or alias_rename):
        return select, rename
    return _rewrite_names(select, rename, alias_rename, set()), rename


def _rewrite_names(
    select: SqlSelect,
    cte_names: Mapping[str, str],
    aliases: Mapping[str, str],
    sourced: set[str],
) -> SqlSelect:
    def table_name(table: SqlTableRef) -> str:
        if isinstance(table, SqlSourceRef) and table.name not in sourced:
            return table.name
        return cte_names.get(table.name, table.name)

    # A caller may already be deep in its own stack. Walk the SQL tree without
    # adding a Python frame for each level of a supported expression.
    Scope = tuple[Mapping[str, str], set[str], Mapping[str, str]]
    stack: list[tuple[Any, Scope, tuple[str, ...] | None]] = [
        (select, (cte_names, set(), {}), None)
    ]
    values: list[Any] = []
    while stack:
        node, scope, keys = stack.pop()
        qualifiers, sources, values_by_table = scope
        if keys is not None:
            length = len(node) if isinstance(node, list | tuple) else len(keys)
            children = values[-length:] if length else []
            if length:
                del values[-length:]
            if isinstance(node, list | tuple):
                values.append(type(node)(children))
            else:
                values.append(replace(node, **dict(zip(keys, children, strict=True))))
            continue

        if isinstance(node, SqlIdentifier):
            if len(node.parts) == 1 and node.parts[0] in values_by_table:
                node = replace(node, parts=[values_by_table[node.parts[0]]])
            if len(node.parts) == 2:
                prefix, column = node.parts
                if not (isinstance(node, SqlSourceColumn) and prefix in sources):
                    name = qualifiers.get(prefix, prefix)
                    if name != prefix:
                        node = replace(node, parts=[name, column])
            values.append(node)
            continue
        if isinstance(node, SqlTableRef):
            name, alias = table_name(node), aliases.get(node.alias, node.alias)
            if isinstance(node, SqlSourceRef) and node.name in sourced:
                node = SqlTableRef(name, alias)
            values.append(replace(node, name=name, alias=alias))
            continue
        if isinstance(node, list | tuple):
            stack.append((node, scope, ()))
            stack.extend((item, scope, None) for item in reversed(node))
            continue
        if (
            isinstance(node, SqlLiteral | SqlParameter)
            or not is_dataclass(node)
            or isinstance(node, type)
        ):
            values.append(node)
            continue

        current = scope
        if isinstance(node, SqlSelect):
            tables = (node.from_table, *(join.table for join in node.joins))
            raw = {
                table.name
                for table in tables
                if isinstance(table, SqlSourceRef) and not table.alias and table.name not in sourced
            }
            local = {**qualifiers, **{name: name for name in raw}}
            for table in tables:
                if table is None:
                    continue
                if table.alias:
                    local[table.alias] = aliases.get(table.alias, table.alias)
                elif isinstance(table, SqlTableRef) and (
                    not isinstance(table, SqlSourceRef) or table.name in sourced
                ):
                    local[table.name] = table_name(table)
            value_names = {
                table.alias: aliases[table.alias]
                for table in tables
                if isinstance(table, SqlTableFunction)
                and not table.columns
                and table.alias in aliases
            }
            current = local, sources | raw, {**values_by_table, **value_names}
        elif isinstance(node, SqlCte):
            name = cte_names.get(node.name, node.name)
            node = (
                SqlPipelineCte(name, node.query)
                if isinstance(node, SqlSourceCte) and node.name in sourced
                else replace(node, name=name)
            )
        elif isinstance(node, SqlTableFunction):
            node = replace(node, alias=aliases.get(node.alias, node.alias))
        keys = tuple(item.name for item in fields(node))
        stack.append((node, scope, keys))
        stack.extend(
            (getattr(node, key), scope if key == "ctes" else current, None)
            for key in reversed(keys)
        )
    result = values.pop()
    assert isinstance(result, SqlSelect)
    return result
