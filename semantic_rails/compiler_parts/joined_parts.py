"""A joined call: parts, each answered as a call of its own, joined on their shared key columns
in one statement.

Each part is bound as its own request (the caller's view, row filters, policies, display keys)
and lowered with its groups read beside every key of the call (``part_domain``), so a key only
another part has is settled by the part's own guard: 0 where its rows are in scope and its
bucket is loaded, NULL otherwise. This module checks the join (``join_keys``: which columns
the parts join on, and ``join_allowed``: whether they may be joined) and writes the one
statement (``joined_statement``): each part's CTEs, the keys of every part, each part's rows,
then the parts joined on plain equality. A NULL key never matches: each part's NULL-key row is
its own row, NULL in every other part. ``keep`` keeps a row only where every named part has
its own row there. The counts the answer states (``PARTS_JOINED``) are window aggregates over
the joined rows before ``keep``, ``order_by`` and ``limit``, so a page states the whole
result's counts, read from the caller's own rows only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..errors import SemanticLayerError
from ..path_gap import _DATED, _IDENTIFIER_WORDS, _identifier, _words
from ..query_call import refusal
from ..renderer import _flatten_ctes
from ..schema import DimensionConfig, PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlOrderTerm,
    SqlSelect,
    SqlSetQuery,
    SqlTableRef,
    SqlWindow,
)
from .namespacing import _namespace_sql_select
from .part_domain import KEYS, PRESENT
from .paths import display_key

# The joined call's own CTEs and hidden columns.
KEYS_CTE = "joined_keys"
JOINED_CTE = "joined_rows"
COUNTED_CTE = "joined_counted"
KEPT_CTE = "joined_kept"
# Whether a row is a part's own: true, false (a key only another part has), or NULL (another
# part's NULL-key row, which this part has no row for).
IN = "joined_in_{part}"
ROWS = "joined_rows_{part}"
NULL_KEYS = "joined_null_keys_{part}"
UNMATCHED = "joined_unmatched_{part}_{other}"
KEPT_ROWS = "joined_kept_rows"
# Marks on the joined rows the counts read: a part's row once (a coarser part's row repeats on
# each of the finer part's rows, an untimed part's on each period's), its NULL key, and whether
# any row of it holds another part's row.
FIRST = "joined_first_{part}"
NULL_KEY = "joined_null_key_{part}"
HAS = "joined_has_{part}_{other}"


@dataclass(frozen=True)
class JoinKey:
    """One key column of the joined call: its name, and the column and dimension it is in each
    part (``period`` and ``""`` for the time axis). ``chosen`` names the dimensions the package
    doesn't say hold the first part's values (``undeclared``): the caller joined key columns of
    one name, and the answer says that join is the caller's choice."""

    name: str
    columns: tuple[str, ...]
    dimensions: tuple[str, ...]
    chosen: tuple[str, ...] = ()


@dataclass(frozen=True)
class JoinShape:
    """The joined call's key columns and its finer part, if any: the one part grouped by more
    than the join key (``extra``: its further key columns), on whose rows every other part's
    values repeat. A part with no clock beside a timed call holds every key but ``period``
    (its column for it is ``""``): its values repeat on each period's row."""

    keys: list[JoinKey]
    finer: int | None = None
    extra: tuple[str, ...] = ()

    def holds(self, part: int) -> list[JoinKey]:
        """The keys ``part`` has a column for."""
        return [key for key in self.keys if key.columns[part]]

    def untimed(self, part: int) -> bool:
        return any(not key.columns[part] for key in self.keys)


@dataclass(frozen=True)
class PartStatement:
    """A part's compiled statement and its columns: the keys (as the part's lowering named
    them), the selected outputs and the hidden columns the runtime reads."""

    sql_ast: SqlSelect
    keys: tuple[str, ...]
    outputs: tuple[str, ...]
    hidden: tuple[str, ...]


def hidden_name(part: int, column: str) -> str:
    """A part's hidden column, as the joined statement names it."""
    return f"joined_{part}__{column}"


def _part(index: int) -> str:
    return f"parts[{index}]"


def join_allowed(config: PackageConfig, dimension_ids: Sequence[str]) -> str:
    """The gate on one ``join.on`` tuple (a dimension of each part): ``""`` when the parts may
    be joined on it, else why not.

    Every column is an identifier column (``path_gap._identifier``: a word that says it
    identifies and one saying what) that doesn't only date the rows, all of one data type; and
    the columns are the same dimension in every part, columns the package pairs (one column of
    one relation, or the two columns a declared single-column relationship joins), or columns
    of one name that one of their models declares in its key (``_shared_key``: the caller's
    choice, which ``undeclared`` names). Two models holding a column of one name that
    neither keys are not enough.
    """
    distinct = list(dict.fromkeys(dimension_ids))
    if len(distinct) == 1:
        return ""
    rows = _rows(config, distinct)
    if rows is None:
        return "a dimension it names is not one the parts can read"
    keys = _keys(config)
    for row in rows:
        if not _identifier(row.column):
            if _words(row.column) <= _IDENTIFIER_WORDS:
                return f"'{row.column}' alone doesn't say what it identifies"
            return (
                f"'{row.column}' doesn't say it identifies anything: a join column names an "
                "identifier (a code, key or id) and what it identifies"
            )
    # An ``id`` column holds its key's values, whatever their type: it agrees with any type.
    types = list(dict.fromkeys(row.data_type for row in rows if row.data_type != "id"))
    if len(types) > 1:
        return f"the columns' types differ ({', '.join(types)})"
    linked = _linked(
        rows, lambda one, other: _paired(config, one, other) or _shared_key(keys, one, other)
    )
    if len(linked) == len(rows):
        return ""
    apart = next(row for row in rows if row.id not in linked)
    same = any(row.column.casefold() == apart.column.casefold() for row in rows if row is not apart)
    if not same:
        why = ", and their names differ"
    elif _dated(keys, rows, apart):
        why = f", and '{apart.column}' only dates the rows"
    else:
        model = "neither model" if len(rows) == 2 else "no model of the tuple"
        why = f", and {model} declares '{apart.column}' in its key"
    return (
        f"no declared relationship pairs '{rows[0].column}' of '{rows[0].entity}' with "
        f"'{apart.column}' of '{apart.entity}'{why}"
    )


def undeclared(config: PackageConfig, dimension_ids: Sequence[str]) -> list[str]:
    """The dimensions of an allowed tuple the package itself doesn't say hold the first one's
    values (the same dimension, or columns ``_paired`` links, directly or through another of
    the tuple): those a caller joined on key columns of one name, by their own choice."""
    distinct = list(dict.fromkeys(dimension_ids))
    rows = _rows(config, distinct)
    if len(distinct) == 1 or rows is None:
        return []
    linked = _linked(rows, lambda one, other: _paired(config, one, other))
    return [row.id for row in rows if row.id not in linked]


def _rows(config: PackageConfig, dimension_ids: Sequence[str]) -> list[DimensionConfig] | None:
    dimensions = {row.id: row for row in config.dimensions}
    rows = [
        dimensions[dimension_id] for dimension_id in dimension_ids if dimension_id in dimensions
    ]
    return rows if len(rows) == len(dimension_ids) else None


def _keys(config: PackageConfig) -> dict[str, set[str]]:
    """Each entity's declared key columns, casefolded."""
    return {row.id: {column.casefold() for column in row.key_columns} for row in config.entities}


def _linked(rows: Sequence[DimensionConfig], paired: Any) -> set[str]:
    """The dimensions ``paired`` links to the first, directly or through another of ``rows``."""
    linked = {rows[0].id}
    pending = [rows[0]]
    while pending:
        current = pending.pop()
        for row in rows:
            if row.id not in linked and paired(current, row):
                linked.add(row.id)
                pending.append(row)
    return linked


def _shared_key(
    keys: Mapping[str, set[str]], left: DimensionConfig, right: DimensionConfig
) -> bool:
    """Whether two models hold a column of one name that at least one of them declares in its
    key: the key column ``PATH_NOT_FOUND`` says two models share (``path_gap.shared_key``).
    A column that only dates the rows (a load, snapshot or validity date) identifies nothing
    they share, unless it is one side's whole key, as a calendar's date is."""
    column = left.column.casefold()
    if column != right.column.casefold():
        return False
    if _dated(keys, [left, right], left):
        return False
    return column in keys.get(left.entity, set()) or column in keys.get(right.entity, set())


def _dated(
    keys: Mapping[str, set[str]], rows: Sequence[DimensionConfig], row: DimensionConfig
) -> bool:
    """Whether ``row``'s column, among ``rows`` of its name, only dates the rows: typed as a
    date or time on one of them, and the whole key of none."""
    column = row.column.casefold()
    named = [one for one in rows if one.column.casefold() == column]
    return any(one.data_type.casefold() in _DATED for one in named) and not any(
        keys.get(one.entity) == {column} for one in named
    )


def _paired(config: PackageConfig, left: DimensionConfig, right: DimensionConfig) -> bool:
    """Whether the package says ``left`` and ``right`` hold the same values: one column of one
    relation, or the two columns a declared single-column relationship joins."""
    entities = {row.id: row for row in config.entities}
    one, other = entities.get(left.entity), entities.get(right.entity)
    if one is None or other is None:
        return False
    if one.table == other.table and left.column.casefold() == right.column.casefold():
        return True
    for rel in config.relationships:
        if rel.link:
            continue
        source = list(rel.source_columns) or [rel.source_column]
        target = list(rel.target_columns) or [rel.target_column]
        if len(source) != 1 or len(target) != 1:
            continue
        ends = {
            (rel.source_entity, source[0].casefold()),
            (rel.target_entity, target[0].casefold()),
        }
        if ends == {(left.entity, left.column.casefold()), (right.entity, right.column.casefold())}:
            return True
    return False


def join_keys(
    config: PackageConfig,
    on: Sequence[Sequence[str]],
    group_bys: Sequence[Sequence[str]],
    bound_keys: Sequence[Sequence[str]],
    time_key: str,
) -> JoinShape:
    """The joined call's key columns and finer part, checked: ``on`` (one dimension of each
    part per tuple), each part's own ``group_by`` and the keys its binding grouped by (its
    group_by with the display keys it added, then ``time_key`` when the call has a grain and
    the part has a clock).

    Every tuple names a column each part groups by, once, and passes ``join_allowed``. A key
    the binding added beside a name that every part's tuple names joins with that tuple, so
    two same-named entities stay two rows. One part may be grouped by more than the join key
    (the finer part: each list's owners beside its member count), and it holds every key of
    the join, ``period`` included; two such parts would multiply each other's rows, so they
    are refused, as is a finer part with no clock beside a timed call.
    """
    parts = len(group_bys)
    seen: list[dict[str, int]] = [{} for _ in range(parts)]
    tuples: list[tuple[str, ...]] = []
    for index, row in enumerate(on):
        path = f"join.on[{index}]"
        for part, dimension in enumerate(row):
            if dimension not in group_bys[part]:
                raise refusal(
                    path,
                    f"names {dimension}, which {_part(part)} doesn't group by; a part joins on "
                    "its own group_by columns",
                    fix=f"Add {dimension} to {_part(part)}.group_by, or name a dimension it "
                    "groups by.",
                    reason="join_key_not_grouped",
                    part=part,
                )
            if dimension in seen[part]:
                raise refusal(
                    path,
                    f"names {dimension} of {_part(part)} again (join.on[{seen[part][dimension]}])",
                    fix=f"Name each column of {_part(part)} in one tuple only.",
                    reason="join_key_repeated",
                    part=part,
                )
            seen[part][dimension] = index
        reason = join_allowed(config, row)
        if reason:
            raise refusal(
                path,
                f"pairs {' with '.join(dict.fromkeys(row))}: {reason}. Parts join on the same "
                "dimension in every part, on the two columns a declared relationship joins, or "
                "on identifier columns of one name and type that one of the models declares in "
                "its key",
                fix="Group every part by the same dimension, or ask the package author to "
                "declare the relationship between these models.",
                reason="join_key_not_shared",
                dimensions=list(row),
            )
        tuples.append(tuple(row))
    keys: dict[tuple[str, ...], JoinKey] = {}
    for row in tuples:
        keys[row] = JoinKey(row[0], row, row, chosen=tuple(undeclared(config, row)))
        shared = row[0] if len(set(row)) == 1 else ""
        added = display_key(config, shared) if shared else ""
        if added and all(
            added in bound and added not in own
            for bound, own in zip(bound_keys, group_bys, strict=True)
        ):
            # The key the binding grouped by beside the name, in every part: one more column.
            keys[(added,) * parts] = JoinKey(added, (added,) * parts, (added,) * parts)
    if time_key:
        # A part with no clock holds no period: it is matched on the other keys.
        held = tuple(time_key if time_key in bound else "" for bound in bound_keys)
        keys[(time_key,) * parts] = JoinKey(time_key, held, ("",) * parts)
    covered = [{key.columns[part] for key in keys.values()} - {""} for part in range(parts)]
    extras = {
        part: [column for column in bound if column not in covered[part]]
        for part, bound in enumerate(bound_keys)
    }
    finer = [part for part, extra in extras.items() if extra]
    joined = ", ".join(key.dimensions[0] or key.name for key in keys.values())
    if len(finer) > 1:
        one, other = finer[:2]
        raise refusal(
            _part(other),
            f"{_part(one)} groups by {', '.join(extras[one])} and {_part(other)} by "
            f"{', '.join(extras[other])} beyond the join key ({joined}): joined, each row of one "
            "would repeat for every row of the other",
            fix=f"Group {_part(one)} or {_part(other)} by the join key only, or add the extra "
            "column to join.on with a column of every other part.",
            reason="parts_grain_mismatch",
            part=other,
            extra=extras[other],
            finer=finer,
        )
    shape = JoinShape(list(keys.values()))
    if finer:
        (part,) = finer
        shape = JoinShape(shape.keys, part, tuple(extras[part]))
        if shape.untimed(part):
            raise refusal(
                _part(part),
                f"groups by {', '.join(extras[part])} beyond the join key ({joined}) but has no "
                "period: joined, its rows would repeat on each period's row and each period's "
                "rows on its rows",
                fix=f"Group {_part(part)} by the join key only.",
                reason="parts_grain_mismatch",
                part=part,
                extra=extras[part],
            )
    # The order of the first part's own keys (a display key just before its name).
    order = {column: position for position, column in enumerate(bound_keys[0])}
    ordered = sorted(shape.keys, key=lambda key: order.get(key.columns[0], len(order)))
    return replace(shape, keys=ordered)


def refuse_shared_outputs(shape: JoinShape, outputs: Sequence[Sequence[str]]) -> None:
    """Every column of the joined answer is named once: a key, the finer part's further key
    or a select alias of one part that another part's select alias repeats is refused."""
    taken = {key.name: "the join key" for key in shape.keys}
    taken.update({column: f"{_part(shape.finer)}'s group_by" for column in shape.extra})
    for part, names in enumerate(outputs):
        for name in names:
            if name in taken:
                raise refusal(
                    _part(part),
                    f"selects '{name}' and so does {taken[name]}",
                    fix='Name one of them with "as".',
                    code="DUPLICATE_OUTPUT_ALIAS",
                    alias=name,
                    part=part,
                )
            taken[name] = _part(part)


def _keys_cte(name: str) -> str:
    return f"{name}__{KEYS}"


def joined_statement(
    parts: Sequence[PartStatement],
    shape: JoinShape,
    *,
    keep: Sequence[int] | None,
    order_by: Sequence[Mapping[str, Any]],
    limit: int | None,
) -> SqlSelect:
    """The one statement of a joined call (see the module docstring); ``keep`` None keeps
    every key of every part.

    A part that holds every key reads every key of the call, as its own groups or settled by
    its guard. The keys of the call are those parts' own keys, the finer part's included, so
    a key only the finer part has is settled in the others. An untimed part reads every key of
    the call without its period, and its own keys besides: a key no timed part has is its own
    row, with no period. The finer part reads its own keys only: on a key it lacks, its
    columns are NULL, one row for the key."""
    keys = shape.keys
    ctes_before: list[SqlCte] = []
    ctes_after: list[SqlCte] = []
    sources: list[str] = []
    every_own: list[SqlCte] = []
    roots: list[SqlCte] = []
    for index, part in enumerate(parts):
        prefix = f"joined_part_{index}"
        flat = _flatten_ctes(
            _namespace_sql_select(replace(part.sql_ast, order_by=[], limit=None), f"{prefix}__")
        )
        names = [cte.name for cte in flat.ctes]
        at = names.index(_keys_cte(prefix)) if _keys_cte(prefix) in names else -1
        if at < 0 or names.count(_keys_cte(prefix)) != 1:
            raise SemanticLayerError(
                "REWRITE_NOT_SUPPORTED",
                f"{_part(index)}: this call can't be a part of a joined call yet",
                details={"path": _part(index), "part": index, "reason": "part_not_joinable"},
            )
        own = flat.ctes[at]
        assert isinstance(own.query, SqlSelect)
        held = shape.holds(index)
        # The part's own keys with no NULL join key, under the joined call's key names.
        named = dict(zip(part.keys, own.query.select, strict=True))
        own_keys = SqlCte(
            name=f"{prefix}__own_keys",
            query=replace(
                own.query,
                select=[SqlField(named[key.columns[index]].expression, key.name) for key in held],
                where=[
                    SqlBinary(named[key.columns[index]].expression, "IS NOT", SqlLiteral(None))
                    for key in held
                ],
                distinct=True,
            ),
        )
        if index == shape.finer:
            # The finer part reads its own keys only.
            replaced = own
            sources.append(own_keys.name)
        else:
            source = KEYS_CTE
            if shape.untimed(index):
                source = f"{prefix}__every_keys"
                every_own.append(_every_and_own(source, own_keys.name, [key.name for key in held]))
            else:
                sources.append(own_keys.name)
            replaced = SqlCte(
                name=own.name,
                query=SqlSelect(
                    select=[
                        SqlField(
                            SqlIdentifier(parts=["every", key.name]),
                            named[key.columns[index]].alias,
                        )
                        for key in held
                    ],
                    from_table=SqlTableRef(name=source, alias="every"),
                ),
            )
        ctes_before.extend([*flat.ctes[:at], own_keys])
        ctes_after.extend([replaced, *flat.ctes[at + 1 :]])
        roots.append(SqlCte(name=prefix, query=replace(flat, ctes=[])))
    # Every part with a clock holds every key: some part does, whenever the call is timed.
    assert sources, "a joined call has a part holding every key"
    every_key = _union_of_keys(sources, [key.name for key in keys])
    joined = _joined_rows(parts, shape)
    marks = {field.alias for field in joined.select if field.alias.startswith(_MARKS)}
    carried = [field.alias for field in joined.select if field.alias not in marks]
    counted = SqlSelect(
        select=[
            *(SqlField(SqlIdentifier(parts=["joined", name]), name) for name in carried),
            *_counts(len(parts)),
        ],
        from_table=SqlTableRef(name=JOINED_CTE, alias="joined"),
    )
    kept = SqlSelect(
        select=[
            SqlField(SqlIdentifier(parts=["counted", field.alias]), field.alias)
            for field in counted.select
        ]
        + [SqlField(SqlWindow(function=SqlCall("COUNT", [SqlLiteral(1)])), KEPT_ROWS)],
        from_table=SqlTableRef(name=COUNTED_CTE, alias="counted"),
        where=[
            SqlBinary(SqlIdentifier(parts=["counted", IN.format(part=part)]), "=", SqlLiteral(True))
            for part in (keep or ())
        ],
    )
    outputs = [
        *(key.name for key in keys),
        *shape.extra,
        *(name for part in parts for name in part.outputs),
    ]
    hidden = [field.alias for field in kept.select if field.alias not in outputs]
    final = SqlSelect(
        ctes=[
            *ctes_before,
            *every_key,
            *every_own,
            *ctes_after,
            *roots,
            SqlCte(name=JOINED_CTE, query=joined),
            SqlCte(name=COUNTED_CTE, query=counted),
            SqlCte(name=KEPT_CTE, query=kept),
        ],
        select=[
            SqlField(SqlIdentifier(parts=["kept", name]), name) for name in [*outputs, *hidden]
        ],
        from_table=SqlTableRef(name=KEPT_CTE, alias="kept"),
        order_by=_order(order_by, shape),
        limit=limit,
    )
    return final


_MARKS = ("joined_first_", "joined_null_key_", "joined_has_")


def _every_and_own(name: str, own_keys: str, columns: Sequence[str]) -> SqlCte:
    """An untimed part's keys: every key of the call without its period, and the part's own."""
    return SqlCte(
        name=name,
        query=SqlSetQuery(
            "UNION",
            [
                SqlSelect(
                    select=[
                        SqlField(SqlIdentifier(parts=["every", column]), column)
                        for column in columns
                    ],
                    from_table=SqlTableRef(name=KEYS_CTE, alias="every"),
                ),
                SqlSelect(
                    select=[
                        SqlField(SqlIdentifier(parts=["own_keys", column]), column)
                        for column in columns
                    ],
                    from_table=SqlTableRef(name=own_keys, alias="own_keys"),
                ),
            ],
        ),
    )


def _counts(count: int) -> list[SqlField]:
    """The whole result's counts per part, over the marked joined rows: its rows (each once),
    its NULL-key rows, and its rows with a non-NULL key no row of another part holds."""

    def mark(name: str) -> Any:
        return SqlIdentifier(parts=["joined", name])

    def summed(condition: Any) -> SqlWindow:
        return SqlWindow(
            function=SqlCall(
                "SUM", [SqlCase([SqlCaseWhen(condition, SqlLiteral(1))], else_expr=SqlLiteral(0))]
            )
        )

    def is_one(name: str) -> Any:
        return SqlBinary(mark(name), "=", SqlLiteral(1))

    out: list[SqlField] = []
    for part in range(count):
        first = is_one(FIRST.format(part=part))
        null = is_one(NULL_KEY.format(part=part))
        out.append(SqlField(summed(first), ROWS.format(part=part)))
        out.append(SqlField(summed(SqlBinary(first, "AND", null)), NULL_KEYS.format(part=part)))
        for other in range(count):
            if other == part:
                continue
            unmatched = SqlBinary(
                SqlBinary(
                    first,
                    "AND",
                    SqlBinary(mark(NULL_KEY.format(part=part)), "=", SqlLiteral(0)),
                ),
                "AND",
                SqlBinary(mark(HAS.format(part=part, other=other)), "=", SqlLiteral(0)),
            )
            out.append(SqlField(summed(unmatched), UNMATCHED.format(part=part, other=other)))
    return out


def _union_of_keys(sources: Sequence[str], names: Sequence[str]) -> list[SqlCte]:
    """Every part's own keys once: the parts' key lists FULL OUTER JOINed on plain equality
    (no key here holds a NULL), the last CTE named ``KEYS_CTE``."""
    out: list[SqlCte] = []
    current = sources[0]
    for index, source in enumerate(sources[1:], start=1):
        name = KEYS_CTE if index == len(sources) - 1 else f"{KEYS_CTE}_{index + 1}"
        on: Any = None
        for column in names:
            equal = SqlBinary(
                SqlIdentifier(parts=["left_keys", column]),
                "=",
                SqlIdentifier(parts=["right_keys", column]),
            )
            on = equal if on is None else SqlBinary(on, "AND", equal)
        out.append(
            SqlCte(
                name=name,
                query=SqlSelect(
                    select=[
                        SqlField(
                            SqlCall(
                                "COALESCE",
                                [
                                    SqlIdentifier(parts=["left_keys", column]),
                                    SqlIdentifier(parts=["right_keys", column]),
                                ],
                            ),
                            column,
                        )
                        for column in names
                    ],
                    from_table=SqlTableRef(name=current, alias="left_keys"),
                    joins=[
                        SqlJoin("FULL OUTER", SqlTableRef(name=source, alias="right_keys"), on=on)
                    ],
                ),
            )
        )
        current = name
    if len(sources) == 1:
        out.append(
            SqlCte(
                name=KEYS_CTE,
                query=SqlSelect(
                    select=[
                        SqlField(SqlIdentifier(parts=["own_keys", column]), column)
                        for column in names
                    ],
                    from_table=SqlTableRef(name=sources[0], alias="own_keys"),
                ),
            )
        )
    return out


def _joined_rows(parts: Sequence[PartStatement], shape: JoinShape) -> SqlSelect:
    """The parts' rows joined on the keys by plain equality, each part's presence beside them
    and the marks the counts read.

    The parts that hold every key join first, then the untimed parts (on every key but the
    period), then the finer part, each on the keys it holds, compared with the value the parts
    before it have there; a key's column is the first value any part has."""
    keys = shape.keys
    aliases = [f"part_{index}" for index in range(len(parts))]
    finer = [] if shape.finer is None else [shape.finer]
    order = [
        *(i for i in range(len(parts)) if i not in finer and not shape.untimed(i)),
        *(i for i in range(len(parts)) if i not in finer and shape.untimed(i)),
        *finer,
    ]

    def column(index: int, key: JoinKey) -> Any:
        return SqlIdentifier(parts=[aliases[index], key.columns[index]])

    def first_of(indexes: Sequence[int], key: JoinKey) -> Any:
        values = [column(index, key) for index in indexes if key.columns[index]]
        return values[0] if len(values) == 1 else SqlCall("COALESCE", values)

    fields: list[SqlField] = [SqlField(first_of(order, key), key.name) for key in keys]
    fields.extend(
        SqlField(SqlIdentifier(parts=[aliases[shape.finer], name]), name)
        for name in shape.extra
        if shape.finer is not None
    )
    for index, (alias, part) in enumerate(zip(aliases, parts, strict=True)):
        fields.extend(SqlField(SqlIdentifier(parts=[alias, name]), name) for name in part.outputs)
        fields.extend(
            SqlField(SqlIdentifier(parts=[alias, name]), hidden_name(index, name))
            for name in part.hidden
        )
    presence = [SqlIdentifier(parts=[alias, PRESENT]) for alias in aliases]
    fields.extend(
        SqlField(present, IN.format(part=index)) for index, present in enumerate(presence)
    )

    def own(index: int) -> Any:
        return SqlBinary(presence[index], "=", SqlLiteral(True))

    def flag(condition: Any) -> SqlCase:
        return SqlCase([SqlCaseWhen(condition, SqlLiteral(1))], else_expr=SqlLiteral(0))

    for index, alias in enumerate(aliases):
        held = [column(index, key) for key in shape.holds(index)]
        extra = (
            [SqlIdentifier(parts=[alias, name]) for name in shape.extra] if index in finer else []
        )
        # A part's row is told apart by its presence and its own keys: it repeats, whole, on
        # each joined row it is matched to.
        row = [presence[index], *held, *extra]
        number = SqlWindow(
            function=SqlCall("ROW_NUMBER", []),
            partition_by=row,
            order_by=[SqlOrderTerm(presence[index])],
        )
        fields.append(
            SqlField(
                flag(SqlBinary(own(index), "AND", SqlBinary(number, "=", SqlLiteral(1)))),
                FIRST.format(part=index),
            )
        )
        any_null: Any = None
        for value in held:
            test = SqlIsNull(value)
            any_null = test if any_null is None else SqlBinary(any_null, "OR", test)
        fields.append(
            SqlField(flag(SqlBinary(own(index), "AND", any_null)), NULL_KEY.format(part=index))
        )
        for other in range(len(parts)):
            if other != index:
                fields.append(
                    SqlField(
                        SqlWindow(function=SqlCall("MAX", [flag(own(other))]), partition_by=row),
                        HAS.format(part=index, other=other),
                    )
                )
    joins = []
    for position, index in enumerate(order[1:], start=1):
        before = order[:position]
        on: Any = None
        for key in shape.holds(index):
            if not any(key.columns[prior] for prior in before):
                continue  # no part before it holds this key (a period beside untimed parts)
            # Plain equality: a NULL key matches nothing, so it stays its part's own row.
            equal = SqlBinary(first_of(before, key), "=", column(index, key))
            on = equal if on is None else SqlBinary(on, "AND", equal)
        joins.append(
            SqlJoin(
                "FULL OUTER",
                SqlTableRef(name=f"joined_part_{index}", alias=aliases[index]),
                on=on,
            )
        )
    return SqlSelect(
        select=fields,
        from_table=SqlTableRef(name=f"joined_part_{order[0]}", alias=aliases[order[0]]),
        joins=joins,
    )


def presence_order(count: int) -> list[SqlOrder]:
    """The last terms of every joined call's order: rows tied on every other term (two parts'
    NULL-key rows) in the order of their parts."""
    return [
        SqlOrder(SqlIdentifier(parts=[IN.format(part=part)]), "DESC", nulls_last=True)
        for part in range(count - 1)
    ]


def _order(order_by: Sequence[Mapping[str, Any]], shape: JoinShape) -> list[SqlOrder]:
    """The call's order, else its time axis, its keys and the finer part's further keys,
    ascending; NULLs last."""
    keys = shape.keys
    if order_by:
        return [
            SqlOrder(
                SqlIdentifier(parts=[str(item["field"])]),
                str(item.get("direction") or "ASC"),
                nulls_last=True,
            )
            for item in order_by
        ]
    names = [
        *(key.name for key in keys if not key.dimensions[0]),
        *(key.name for key in keys if key.dimensions[0]),
        *shape.extra,
    ]
    return [SqlOrder(SqlIdentifier(parts=[name]), "ASC", nulls_last=True) for name in names]
