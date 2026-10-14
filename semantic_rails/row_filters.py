"""Row filters: resolve each policy, then hold every statement to the visible database.

Each occurrence of a restricted relation (filtered directly, or owned through its package route
by a filtered one; ``row_filter_visibility``) gets the relation's visibility predicate where it
enters its scope (``enforce_row_filters``), and ``verify_row_filters`` refuses any statement in
which one doesn't, by value, or which reads a relation the filters can't be proven to hold for.
A declared load log's mark (``zero_through``: a ``MAX`` over its relation, or an EXISTS against
it that caps a measure's rows, joined to nothing) reads how far the whole load has completed, so
it is read as it is, unless a filter restricts that relation: then the statement is refused.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any, cast

from .errors import SemanticLayerError
from .policy_rules import policy_action, role_scope_matches
from .schema import PackageConfig, SemanticPolicyConfig, require_base
from .sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCast,
    SqlCte,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlIn,
    SqlInQuery,
    SqlLiteral,
    SqlParameter,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
)
from .sql_preparation import ParameterSlot, checked_slot_value, parameters_denied

ROW_FILTER = "row_filter"
_SLOT_TYPES = frozenset({"string", "integer", "boolean"})
_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class RowFilter:
    policy_id: str
    table: str
    column: str
    slot: ParameterSlot
    # A list attribute's length: the column matches any item; ``slot`` binds the first.
    size: int | None = None
    unless_roles: tuple[str, ...] = ()
    # The filtered model (its relation is ``table``), and the uncovered models the package
    # declares visible to every caller this filter applies to.
    entity: str = ""
    shared: tuple[str, ...] = ()

    @property
    def slots(self) -> tuple[ParameterSlot, ...]:
        """The slots one filtered scan binds, in placeholder order. The first is ``slot``
        itself: binding identifies an applied filter by it."""
        return (self.slot, *(replace(self.slot, index=i) for i in range(1, self.size or 1)))

    def exempts(self, roles: Iterable[str]) -> bool:
        """Whether one of ``roles``, the roles the host verified, is in ``unless_roles``."""
        return bool(self.unless_roles) and role_scope_matches(self.unless_roles, roles)

    def for_value(self, value: Any) -> RowFilter:
        """This filter for one request's attribute value, or a denial naming the attribute.

        A list of strings or integers matches any of its items, each checked against the
        slot's type; an empty list leaves no row to read, so it is denied, never bound."""
        if type(value) is tuple and not value:
            raise parameters_denied("empty_attribute_list", attribute=self.slot.attribute)
        row = self
        if type(value) is tuple and self.slot.type != "boolean":
            row = replace(self, slot=replace(self.slot, index=0), size=len(value))
        for slot in row.slots:
            checked_slot_value(slot, value)
        return row

    def predicate(self, qualifier: Sequence[str]) -> SqlExpr:
        column = SqlIdentifier([*qualifier, self.column])
        if self.size is None:
            return SqlBinary(column, "=", SqlParameter(self.slot))
        return SqlIn(column, [SqlParameter(slot) for slot in self.slots])


# The alias of an owner read inside a visibility predicate. Every column the subquery reads is
# qualified by it, and it names the subquery's only relation, so no outer name is captured.
OWNER = "rf_owner"


def relation_key(name: str) -> str:
    return str(name).casefold()


@dataclass(frozen=True, order=True)
class ParentHop:
    """A covered relation's foreign key into its owner's relation and that owner's key."""

    fk: str
    parent: str
    key: str


@dataclass(frozen=True)
class Visibility:
    """What a statement may read under ``filters``: relations are keyed by ``relation_key``."""

    filters: tuple[RowFilter, ...]
    # Each covered relation's parent hops, under these filters.
    parents: Mapping[str, tuple[ParentHop, ...]] = field(default_factory=dict)
    # Relations no statement may read under these filters, with why.
    refused: Mapping[str, str] = field(default_factory=dict)
    # Relations a model reads (unrestricted ones may be read as they are).
    modeled: frozenset[str] = frozenset()
    # Load logs whose mark (zero_through) a measure reads.
    load_logs: frozenset[str] = frozenset()
    # Relation-pipeline CTE names: their bodies may not read a restricted relation.
    pipelines: frozenset[str] = frozenset()
    # Whether the warehouse accepts a subquery in a join's ON (``SqlDialect``); unproven, no.
    subquery_in_join_on: bool = False

    @property
    def restricted(self) -> frozenset[str]:
        return frozenset(relation_key(row.table) for row in self.filters) | frozenset(self.parents)

    def predicate(self, table: str, qualifier: Sequence[str]) -> SqlExpr:
        """``V_T`` for an occurrence of ``table`` named by ``qualifier``."""
        return self._predicate(relation_key(table), list(qualifier), ())

    def _predicate(self, table: str, qualifier: list[str], seen: tuple[str, ...]) -> SqlExpr:
        if table in seen or len(seen) > 16:
            raise unsupported(self.filters)  # owners that own each other define nothing
        parts: list[SqlExpr] = [
            row.predicate(qualifier) for row in self.filters if relation_key(row.table) == table
        ]
        for hop in self.parents.get(table, ()):
            owners = SqlSelect(
                [SqlField(SqlIdentifier([OWNER, hop.key]), hop.key)],
                SqlTableRef(hop.parent, OWNER),
                where=[self._predicate(relation_key(hop.parent), [OWNER], (*seen, table))],
            )
            parts.append(SqlInQuery(SqlIdentifier([*qualifier, hop.fk]), owners))
        if not parts:
            raise unsupported(self.filters)
        return conjunction(parts)


def row_filter(config: PackageConfig, policy: SemanticPolicyConfig) -> RowFilter:
    """Resolve one ``row_filter`` policy; anything it can't enforce is a config error."""

    require_base(config)
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
    entities = {row.id: row for row in config.entities}
    entity = entities.get(dimension.entity)
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
    unless = policy.config.get("unless_roles", ())
    if "unless_roles" in policy.config and (
        not isinstance(unless, list | tuple)
        or not unless
        or not all(isinstance(role, str) and role.strip() for role in unless)
    ):
        raise invalid("'unless_roles' must be a non-empty list of role names")
    names = {role.strip().lower() for role in unless}
    if both := sorted(names & {str(role).strip().lower() for role in policy.roles}):
        raise invalid(f"role {both[0]!r} is in both 'roles' and 'unless_roles'")
    shared = policy.config.get("shared", ())
    if "shared" in policy.config and (
        not isinstance(shared, list | tuple)
        or not shared
        or not all(isinstance(model, str) and model in entities for model in shared)
    ):
        raise invalid("'shared' must be a non-empty list of entity ids of this package")
    return RowFilter(
        policy.id,
        entity.table,
        dimension.column,
        slot,
        unless_roles=tuple(unless),
        entity=entity.id,
        shared=tuple(shared),
    )


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


_RIGHT_OR_FULL = frozenset({"RIGHT", "RIGHT OUTER", "FULL", "FULL OUTER"})
_LEFT = frozenset({"LEFT", "LEFT OUTER"})


def enforce_row_filters(sql: SqlSelect, visibility: Visibility) -> SqlSelect:
    """Give every occurrence of a restricted relation its visibility predicate ``V_T`` where it
    enters its scope: the scope's WHERE for a FROM, INNER or CROSS occurrence, the join's ON
    for a LEFT one. A scope holding one may not RIGHT or FULL join. Idempotent; the predicates
    it adds are never rewritten again. ``verify_row_filters`` is the check, not this."""
    if not visibility.filters:
        return sql
    ctes = {node.name.casefold() for node in _walk(sql) if isinstance(node, SqlCte)}

    def rewrite(node: Any) -> Any:
        if isinstance(node, list | tuple):
            items = [rewrite(item) for item in node]
            changed = any(a is not b for a, b in zip(items, node, strict=True))
            return type(node)(items) if changed else node
        if not is_dataclass(node) or isinstance(node, type):
            return node
        changes = {}
        for item in fields(node):
            value = getattr(node, item.name)
            if (rewritten := rewrite(value)) is not value:
                changes[item.name] = rewritten
        node = replace(node, **changes) if changes else node
        return _add_predicates(node, visibility, ctes) if isinstance(node, SqlSelect) else node

    return cast(SqlSelect, rewrite(sql))


def _occurrences(
    select: SqlSelect, visibility: Visibility, ctes: Collection[str]
) -> list[tuple[int | None, SqlTableRef]]:
    """The restricted relations ``select`` reads directly: (join index or None, reference)."""
    tables = [(None, select.from_table), *enumerate(join.table for join in select.joins)]
    return [
        (position, table)
        for position, table in tables
        if isinstance(table, SqlTableRef)
        and table.name.casefold() not in ctes
        and table.name.casefold() in visibility.restricted
        and not _is_mark(select, position, table, visibility)
    ]


def _is_mark(
    select: SqlSelect, position: int | None, table: SqlTableRef, visibility: Visibility
) -> bool:
    return position is None and table.name.casefold() in visibility.load_logs and _observes(select)


def _qualifier(table: SqlTableRef) -> list[str]:
    return (table.alias or table.name).split(".")


def _add_predicates(select: SqlSelect, visibility: Visibility, ctes: Collection[str]) -> SqlSelect:
    occurrences = _occurrences(select, visibility, ctes)
    if not occurrences:
        return select
    if any(join.join_type in _RIGHT_OR_FULL for join in select.joins):
        raise unsupported(visibility.filters)
    where, joins = list(select.where), list(select.joins)
    added: list[SqlExpr] = []
    for position, table in occurrences:
        predicate = visibility.predicate(table.name, _qualifier(table))
        if position is None or joins[position].join_type not in _LEFT:
            if predicate not in where and predicate not in added:
                added.append(predicate)
            continue
        join = joins[position]
        if not visibility.subquery_in_join_on and any(
            isinstance(node, SqlInQuery) for node in _walk(predicate)
        ):
            raise SemanticLayerError(
                "POLICY_DENIED",
                "A row filter applies to this request, and its warehouse can't yet read the "
                "visibility of a relation joined optionally here.",
                details={
                    "reason": "row_filter_shape_unsupported_by_warehouse",
                    "policy_ids": sorted({row.policy_id for row in visibility.filters}),
                },
            )
        if not _leads(join.on, predicate):
            on = predicate if join.on is None else SqlBinary(predicate, "AND", join.on)
            joins[position] = replace(join, on=on)
    return replace(select, where=[*added, *where], joins=joins)


def _leads(on: SqlExpr | None, predicate: SqlExpr) -> bool:
    """Whether a LEFT join's ON is ``predicate``, or ``predicate AND …``."""
    return on == predicate or (
        isinstance(on, SqlBinary) and on.op == "AND" and on.left == predicate
    )


def verify_row_filters(sql: SqlSelect, visibility: Visibility) -> None:
    """Refuse ``sql`` unless every occurrence of a restricted relation carries ``V_T`` where it
    enters its scope (by value: the builder's own output), and nothing else reads a relation
    the filters could restrict: no rollup or unmodeled relation, no table function but the
    day series, no restricted read inside a relation pipeline, no load-log mark of a
    restricted relation, no duplicate CTE name and no CTE named like a restricted relation."""
    if not visibility.filters:
        return
    names = [node.name.casefold() for node in _walk(sql) if isinstance(node, SqlCte)]
    ctes = set(names)
    if len(ctes) != len(names) or ctes & visibility.restricted:
        raise unsupported(visibility.filters)
    _verify(sql, visibility, ctes, in_pipeline=False)


def _verify(node: Any, visibility: Visibility, ctes: set[str], *, in_pipeline: bool) -> None:
    if isinstance(node, list | tuple):
        for item in node:
            _verify(item, visibility, ctes, in_pipeline=in_pipeline)
        return
    if isinstance(node, SqlTableRef | SqlTableFunction):
        raise unsupported(visibility.filters)  # a relation read outside FROM or JOIN
    if isinstance(node, SqlCte):
        inside = in_pipeline or node.name.casefold() in visibility.pipelines
        _verify(node.query, visibility, ctes, in_pipeline=inside)
        return
    if not is_dataclass(node) or isinstance(node, type):
        return
    if not isinstance(node, SqlSelect):
        for item in fields(node):
            _verify(getattr(node, item.name), visibility, ctes, in_pipeline=in_pipeline)
        return
    proven: set[int] = set()
    on_rest: dict[int, SqlExpr | None] = {}
    tables = [(None, node.from_table), *enumerate(join.table for join in node.joins)]
    for position, table in tables:
        if table is None:
            continue
        if isinstance(table, SqlTableFunction):
            if not _reads_nothing(table):
                raise unsupported(visibility.filters)
            continue
        name = table.name.casefold()
        if name in ctes:
            continue
        if name in visibility.refused:
            raise SemanticLayerError(
                "POLICY_DENIED",
                "A row filter applies to this request, and it reads a relation whose rows the "
                "filter can't be traced to, so it can't be answered under it.",
                details={
                    "reason": "row_filter_coverage_unsupported",
                    "policy_ids": sorted({row.policy_id for row in visibility.filters}),
                },
            )
        if _is_mark(node, position, table, visibility):
            if name in visibility.restricted:
                raise _on_load_log()
            continue
        if name not in visibility.restricted:
            if name in visibility.modeled or name in visibility.load_logs:
                continue
            raise unsupported(visibility.filters)  # a relation no model reads
        if in_pipeline or any(join.join_type in _RIGHT_OR_FULL for join in node.joins):
            raise unsupported(visibility.filters)
        predicate = visibility.predicate(table.name, _qualifier(table))
        join = None if position is None else node.joins[position]
        if join is None or join.join_type not in _LEFT:
            index = next((i for i, item in enumerate(node.where) if item == predicate), None)
            if index is None:
                raise unsupported(visibility.filters)
            proven.add(index)
        elif _leads(join.on, predicate) and position is not None:
            on = join.on
            on_rest[position] = on.right if isinstance(on, SqlBinary) and on != predicate else None
        else:
            raise unsupported(visibility.filters)
    for item in fields(node):
        value = getattr(node, item.name)
        if item.name == "from_table":
            continue
        if item.name == "joins":
            for position, join in enumerate(value):
                on = on_rest.get(position, join.on)
                _verify(on, visibility, ctes, in_pipeline=in_pipeline)
            continue
        if item.name == "where":
            value = [each for index, each in enumerate(value) if index not in proven]
        _verify(value, visibility, ctes, in_pipeline=in_pipeline)


def apply_row_filters(sql: SqlSelect, visibility: Visibility) -> SqlSelect:
    """Enforce, then verify: the one call every statement a filtered request runs takes."""
    enforced = enforce_row_filters(sql, visibility)
    verify_row_filters(enforced, visibility)
    return enforced


def _on_load_log() -> SemanticLayerError:
    # The load log may be hidden from this caller, so the refusal names no policy.
    return SemanticLayerError(
        "POLICY_DENIED",
        "A row filter applies to the load log this request reads to know how far its data "
        "is loaded (zero_through), so that date can't be read for this caller.",
        details={"reason": "row_filter_on_load_log"},
    )


_OBSERVING = frozenset({"MIN", "MAX", "COUNT"})


def _reads_nothing(function: SqlTableFunction) -> bool:
    """The engine's day series (a named window's periods): a generator of values from its
    arguments, which reach no relation, so it holds no row a filter could have removed."""
    return function.name == "GENERATE_SERIES" and not any(
        isinstance(node, SqlTableRef | SqlTableFunction | SqlSelect)
        for node in _walk([function.args, function.named_args])
    )


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


def unsupported(filters: Sequence[RowFilter]) -> SemanticLayerError:
    return SemanticLayerError(
        "POLICY_DENIED",
        "A row filter applies to this request, and this query reads a relation in a way the "
        "filter can't be proven to hold for, so it can't be answered under it.",
        details={
            "reason": "row_filter_unsupported_query",
            "policy_ids": sorted({row.policy_id for row in filters}),
        },
    )


def conjunction(items: Sequence[SqlExpr]) -> SqlExpr:
    """``AND`` of ``items``, balanced so a long condition list can't exhaust the recursion limit."""
    if len(items) == 1:
        return items[0]
    middle = len(items) // 2
    return SqlBinary(conjunction(items[:middle]), "AND", conjunction(items[middle:]))


def _walk(node: Any) -> Iterator[Any]:
    """Every AST node, found generically so no node type can hide a read."""
    if isinstance(node, list | tuple):
        for item in node:
            yield from _walk(item)
    elif is_dataclass(node) and not isinstance(node, type):
        yield node
        for item in fields(node):
            yield from _walk(getattr(node, item.name))
