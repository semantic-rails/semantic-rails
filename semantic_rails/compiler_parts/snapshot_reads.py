"""Which snapshots a stock reads: one selection for every path that reads one.

A stock reads one snapshot per series per period: the last (its first, for a start-of-period
stock). On an ``as_of_time`` clock (a snapshot table) an end-of-period read also closes on
the period's closing day, the last day with a snapshot in the clock's own zone (with no
period, the last such day of everything read): a series with no snapshot that day is not
counted, never carried forward. A row with no clock is never a snapshot. A measure's own leaf
and an entity-set ratio both choose through :func:`select_snapshots`, on either lowering
(QUALIFY, or an aggregate joined back), and each closing-day read records the probe the
runtime runs to name its day and count the series it left out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ..schema import PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlOrderTerm,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)
from .dependencies import current_cut_owners, holding, plan_is_root
from .lowering_common import _and_conditions, _apply_role_timezone


@dataclass(frozen=True)
class SnapshotRead:
    """A stock's rows and how one snapshot is chosen from them.

    ``fields`` are carried on each chosen row; ``periods`` and ``series`` name the ones that
    split time and identify a series. ``day`` maps the clock to its local date when the read
    closes on a day (:func:`closing_day`), else None.
    """

    fields: list[SqlField]
    periods: list[str]
    series: list[str]
    order: Any
    day: Callable[[Any], Any] | None
    first: bool
    source: SqlTableRef
    joins: list[SqlJoin] = field(default_factory=list)
    where: list[Any] = field(default_factory=list)
    ctes: list[SqlCte] = field(default_factory=list)


def closing_day(measure: Any, role: Any, config: PackageConfig) -> Callable[[Any], Any] | None:
    """The date in the clock's own zone a stock on ``role`` closes on; None for anything but a
    stock, and for a clock that isn't as-of: a current-state table, read as keyed."""
    if measure.measure_class != "semi_additive" or role.temporal_class != "as_of_time":
        return None
    return lambda clock: SqlCast(_apply_role_timezone(clock, role, config), "DATE")


def select_snapshots(
    read: SnapshotRead, *, name: str, prefix: str, dialect: Any, measure_id: str
) -> list[SqlCte]:
    """The CTEs whose last, ``name``, holds the chosen rows with ``read.fields``."""
    exprs = {item.alias: item.expression for item in read.fields}
    where = [*read.where, SqlBinary(read.order, "IS NOT", SqlLiteral(None))]
    if read.day is not None:
        record_closing_read(measure_id, _closing_day_probe(read, exprs, where))
    if dialect.capabilities().get("qualify"):
        rank = SqlWindow(
            SqlCall("ROW_NUMBER", []),
            partition_by=[exprs[alias] for alias in [*read.periods, *read.series]],
            order_by=[SqlOrderTerm(read.order, "ASC" if read.first else "DESC")],
        )
        chosen: list[Any] = [SqlBinary(rank, "=", SqlLiteral(1))]
        if read.day is not None:
            last = SqlWindow(
                SqlCall("MAX", [read.order]),
                partition_by=[exprs[alias] for alias in read.periods],
            )
            chosen.append(SqlBinary(read.day(read.order), "=", read.day(last)))
        rows = SqlSelect(
            select=read.fields,
            from_table=read.source,
            joins=read.joins,
            where=where,
            qualify=[_and_conditions(chosen)],
        )
        return [*read.ctes, SqlCte(name=name, query=rows)]

    base, complete, closing = f"{prefix}_base", f"{prefix}_complete", f"{prefix}_closing"

    def column(relation: str, alias: str) -> SqlIdentifier:
        return SqlIdentifier(parts=[relation, alias])

    def grouped(aliases: list[str], value: Any, alias: str) -> SqlSelect:
        return SqlSelect(
            select=[*(SqlField(column(base, a), a) for a in aliases), SqlField(value, alias)],
            from_table=SqlTableRef(name=base),
            group_by=[column(base, a) for a in aliases],
        )

    def same(aliases: list[str], other: str) -> list[Any]:
        return [dialect.null_safe_eq(column(base, a), column(other, a)) for a in aliases]

    order = column(base, "__snapshot_order")
    partition = [*read.periods, *read.series]
    ctes = [
        *read.ctes,
        SqlCte(
            name=base,
            query=SqlSelect(
                select=[*read.fields, SqlField(read.order, "__snapshot_order")],
                from_table=read.source,
                joins=read.joins,
                where=where,
            ),
        ),
        SqlCte(
            name=complete,
            query=grouped(
                partition,
                SqlCall("MIN" if read.first else "MAX", [order]),
                "__snapshot_order",
            ),
        ),
    ]
    joins = [
        SqlJoin(
            join_type="INNER",
            table=SqlTableRef(name=complete),
            on=_and_conditions(
                [
                    *same(partition, complete),
                    SqlBinary(order, "=", column(complete, "__snapshot_order")),
                ]
            ),
        )
    ]
    if read.day is not None:
        ctes.append(
            SqlCte(name=closing, query=grouped(read.periods, SqlCall("MAX", [order]), "__closing"))
        )
        on_close = SqlBinary(read.day(order), "=", read.day(column(closing, "__closing")))
        joins.append(
            SqlJoin(
                join_type="INNER",
                table=SqlTableRef(name=closing),
                on=_and_conditions([*same(read.periods, closing), on_close]),
            )
        )
    rows = SqlSelect(
        select=[SqlField(column(base, item.alias), item.alias) for item in read.fields],
        from_table=SqlTableRef(name=base),
        joins=joins,
    )
    return [*ctes, SqlCte(name=name, query=rows)]


def _closing_day_probe(read: SnapshotRead, exprs: dict[str, Any], where: list[Any]) -> SqlSelect:
    """One row on a closing-day read, over the very rows it chooses from: its latest closing
    day, its periods, the series seen in them, and those whose last snapshot in a period is
    earlier than that period's closing day (how many, in how many periods, the latest such
    day)."""
    assert read.day is not None
    periods = [exprs[alias] for alias in read.periods]
    rows = SqlSelect(
        select=[
            SqlField(
                read.day(SqlWindow(SqlCall("MAX", [read.order]), partition_by=periods)), "closing"
            ),
            SqlField(
                SqlWindow(
                    SqlCall("ROW_NUMBER", []),
                    partition_by=[*periods, *(exprs[alias] for alias in read.series)],
                    order_by=[SqlOrderTerm(read.order, "DESC")],
                ),
                "series_rank",
            ),
            SqlField(read.day(read.order), "day"),
        ],
        from_table=read.source,
        joins=read.joins,
        where=where,
    )

    def column(alias: str) -> SqlIdentifier:
        return SqlIdentifier(["closing_rows", alias])

    last = SqlBinary(column("series_rank"), "=", SqlLiteral(1))
    missed = _and_conditions([last, SqlBinary(column("day"), "<", column("closing"))])

    def count(condition: Any) -> SqlCall:
        return SqlCall("SUM", [SqlCase([SqlCaseWhen(condition, SqlLiteral(1))], SqlLiteral(0))])

    when_missed = SqlCase([SqlCaseWhen(missed, column("closing"))])
    return SqlSelect(
        ctes=[*read.ctes, SqlCte(name="closing_rows", query=rows)],
        select=[
            SqlField(SqlCall("MAX", [column("closing")]), "closing_day"),
            SqlField(SqlCall("COUNT", [column("closing")], distinct=True), "periods"),
            SqlField(count(last), "series"),
            SqlField(count(missed), "series_missing"),
            SqlField(SqlCall("COUNT", [when_missed], distinct=True), "periods_missing"),
            SqlField(SqlCall("MAX", [when_missed]), "missing_day"),
        ],
        from_table=SqlTableRef(name="closing_rows"),
    )


_closing_reads: ContextVar[dict[str, Any] | None] = ContextVar("closing_reads", default=None)


@contextmanager
def recording_closing_reads() -> Iterator[dict[str, Any]]:
    """Collect each closing-day read in this block, nested compiles included (an entity
    filter's or a predicate's source, a branch), and which leaves each output reads."""
    recorded: dict[str, Any] = {"reads": [], "outputs": {}}
    with holding(_closing_reads, recorded):
        yield recorded


def record_closing_read(measure_id: str, probe: SqlSelect) -> None:
    """One read per measure and probe: each stock keeps its own disclosure. ``owners`` are
    the root leaves or outputs the read counts for (None: the whole answer, as an entity
    filter's), the attribution its cuts use; ``nested`` says the read is inside another."""
    recorded = _closing_reads.get()
    if recorded is None:
        return
    owners, nested = current_cut_owners(), not plan_is_root()
    for read in recorded["reads"]:
        if (read["measure"], read["probe"]) == (measure_id, probe):
            mine = read["owners"]
            read["owners"] = None if mine is None or owners is None else mine | owners
            read["nested"] = read["nested"] and nested
            return
    recorded["reads"].append(
        {"measure": measure_id, "probe": probe, "owners": owners, "nested": nested}
    )


def record_output_reads(output: str, leaves: set[str]) -> None:
    """The root leaves an output of the request reads."""
    recorded = _closing_reads.get()
    if recorded is not None and plan_is_root():
        recorded["outputs"][output] = set(leaves)


def closing_reads_by_output(recorded: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Each read with the outputs it counts for; an empty list is the whole answer (an entity
    filter's read, or a lowering that records no outputs)."""
    outputs: dict[str, set[str]] = recorded["outputs"]
    return tuple(
        {
            "measure": read["measure"],
            "probe": read["probe"],
            "nested": read["nested"],
            "outputs": sorted(
                name
                for name, leaves in outputs.items()
                if read["owners"] is not None
                and (name in read["owners"] or leaves & read["owners"])
            ),
        }
        for read in recorded["reads"]
    )
