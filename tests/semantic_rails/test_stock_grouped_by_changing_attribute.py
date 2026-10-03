"""A stock grouped by an attribute that changes within the period counts each series once.

An end-of-period stock takes each series' last snapshot in each period, then adds up
the series. Grouped by an attribute stored on the snapshot rows, it used to take the
last snapshot per series per attribute value: an account on plan "Builder" Monday to
Wednesday and "Pro" Thursday to Sunday was counted under both plans in that week, its
Wednesday seats under Builder and its Sunday seats under Pro, and the grouped rows no
longer added up to the ungrouped total. The snapshot is now chosen per series per
period first and the attribute read from it. The stock's own clock and calendar
dimensions still split a series into periods: grouped by the day, each day keeps its
own closing value.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.dialects import DuckDbDialect
from semantic_rails.runtime import Runtime

ROLE = "temporal_role.seats_account_day_date_day"
PLAN = "dimension.seats_account_day_plan"
DAY = "dimension.seats_account_day_date_day"
CALENDAR_WEEK = "dimension.seats_time_week_start"
MONDAY = date(2026, 9, 21)


def _rows() -> list[tuple[str, date, str, int]]:
    rows = []
    for offset in range(7):
        day = MONDAY + timedelta(days=offset)
        # a: Builder Monday to Wednesday, Pro Thursday to Sunday.
        rows.append(("a", day, "Builder" if offset < 3 else "Pro", 10 + offset))
        # c: Pro Monday and Tuesday, Builder from Wednesday.
        rows.append(("c", day, "Pro" if offset < 2 else "Builder", 1000 + offset))
        if offset < 5:
            # b: its last snapshot of the week is Friday's.
            rows.append(("b", day, "Team", 100))
    # Nothing in the week of 2026-09-28; one snapshot each in the next week.
    rows.append(("a", date(2026, 10, 5), "Pro", 20))
    rows.append(("b", date(2026, 10, 5), "Team", 200))
    return rows


def _package(root: Path) -> Path:
    package = root / "seats"
    (package / "models").mkdir(parents=True)
    (package / "metrics").mkdir()
    (package / "data").mkdir()
    (package / "package.yml").write_text(
        "schema_version: 1\n"
        "package: {id: seats, namespace: seats, name: seats, warehouse: duckdb,\n"
        "  default_db: data/seats.duckdb, seed: {kind: external}, schema_strict: true,\n"
        "  environments: [development]}\n"
    )
    (package / "graph.yml").write_text(
        "graph:\n  entities:\n"
        "    account_day: {key: [account_id, date_day], model: account_days, "
        "allowed_as_root: true}\n"
        "    time: {kind: time, key: [date_day], model: calendar, allowed_as_root: false}\n"
    )
    (package / "models" / "calendar.yml").write_text(
        "model:\n"
        "  id: calendar\n"
        "  relation: calendar\n"
        "  calendar_id: default\n"
        "  entities: {time: {}}\n"
        "  times:\n"
        "    date_day: {column: date_day, kind: date, class: calendar_time}\n"
        "  dimensions:\n"
        "    week_start: {kind: date}\n"
    )
    stock = "kind: aggregate, expr: seats, value_type: count, accumulation: {kind: stock, snapshot"
    (package / "models" / "account_days.yml").write_text(
        "model:\n"
        "  id: account_days\n"
        "  relation: account_day\n"
        "  entities: {account_day: {}, time: {}}\n"
        "  dimensions: {plan: {kind: categorical}}\n"
        "  times:\n"
        "    date_day: {column: date_day, kind: date, class: as_of_time, default: true}\n"
        "  measures:\n"
        f"    seats: {{{stock}: end_of_period}}}}\n"
        f"    seats_sop: {{{stock}: start_of_period}}}}\n"
    )
    (package / "metrics" / "metrics.yml").write_text(
        "metrics:\n"
        + "".join(
            f"  {name}: {{kind: semi_additive, measure: {name}, temporal_role: {ROLE}, "
            "value_type: count}\n"
            for name in ("seats", "seats_sop")
        )
    )
    connection = duckdb.connect(str(package / "data" / "seats.duckdb"))
    _load(connection)
    connection.close()
    return package


def _load(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        "create table account_day (account_id varchar, date_day date, plan varchar, seats integer)"
    )
    connection.executemany("insert into account_day values (?, ?, ?, ?)", _rows())
    connection.execute(
        "create table calendar as select d::date as date_day, "
        "date_trunc('week', d)::date as week_start "
        "from generate_series(date '2026-09-01', date '2026-10-31', interval 1 day) t(d)"
    )


def _reference(measure: str, group_by: list[str], grain: str = "week") -> list[tuple]:
    """Each account's last (first) snapshot per period, under the plan it has on that day."""
    direction = "asc" if measure == "seats_sop" else "desc"
    period = f"date_trunc('{grain}', date_day)::date"
    groups = "".join(f", {column}" for column in group_by)
    connection = duckdb.connect()
    try:
        _load(connection)
        return connection.execute(
            f"select period{groups}, sum(seats) from ("
            f"  select *, {period} as period, row_number() over ("
            f"    partition by account_id, {period} order by date_day {direction}) as rn"
            "  from account_day"
            f") where rn = 1 group by all order by all"
        ).fetchall()
    finally:
        connection.close()


def _query(runtime: Runtime, measure: str, group_by: list[str], time: dict | None) -> list[tuple]:
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"expression": {"measure": f"measure.seats.{measure}"}, "as": "v"}],
        "group_by": group_by,
    }
    if time:
        query["time"] = time
    result = runtime.query(query)
    assert result["ok"], result.get("errors")
    time_alias = f"{ROLE}__{time['grain']}" if time else None
    rows = []
    for row in result["rows"]:
        period = [_day(row[time_alias])] if time_alias else []
        groups = [_day(row[dim]) if dim in (DAY, CALENDAR_WEEK) else row[dim] for dim in group_by]
        rows.append((*period, *groups, row["v"]))
    return sorted(rows, key=lambda row: tuple(str(cell) for cell in row))


def _day(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


@pytest.fixture(params=["aggregate_join", "qualify"])
def runtime(request, tmp_path: Path, monkeypatch) -> Iterator[Runtime]:
    # DuckDB lowers the snapshot choice as an aggregate joined back; warehouses with
    # QUALIFY (Snowflake, BigQuery, Databricks) use ROW_NUMBER. DuckDB runs both.
    if request.param == "qualify":
        capabilities = DuckDbDialect.capabilities
        monkeypatch.setattr(
            DuckDbDialect, "capabilities", lambda self: {**capabilities(self), "qualify": True}
        )
    runtime = Runtime.from_path(str(_package(tmp_path)))
    yield runtime
    runtime.close()


def test_each_series_counts_once_under_its_closing_plan(runtime: Runtime) -> None:
    week = {"temporal_role": ROLE, "grain": "week"}
    grouped = _query(runtime, "seats", [PLAN], week)
    # a closes the week on Pro (16), c on Builder (1006); b's last snapshot, Friday's,
    # stands for its week (100). Each account is counted once.
    assert grouped == [
        (MONDAY, "Builder", 1006),
        (MONDAY, "Pro", 16),
        (MONDAY, "Team", 100),
        (date(2026, 10, 5), "Pro", 20),
        (date(2026, 10, 5), "Team", 200),
    ]
    assert grouped == _reference("seats", ["plan"])
    # The grouped rows add up to the ungrouped total of each week.
    total = _query(runtime, "seats", [], week)
    assert total == _reference("seats", []) == [(MONDAY, 1122), (date(2026, 10, 5), 220)]
    for period, value in total:
        assert sum(row[2] for row in grouped if row[0] == period) == value
    # Start of period reads the first snapshot, under the plan it has on that day.
    assert _query(runtime, "seats_sop", [PLAN], week) == _reference("seats_sop", ["plan"])
    assert _query(runtime, "seats_sop", [PLAN], week)[:3] == [
        (MONDAY, "Builder", 10),
        (MONDAY, "Pro", 1000),
        (MONDAY, "Team", 100),
    ]


def test_without_a_time_block_each_series_counts_once(runtime: Runtime) -> None:
    # Untimed, the period is everything: each account's last snapshot, under its plan.
    assert _query(runtime, "seats", [PLAN], None) == [
        ("Builder", 1006),
        ("Pro", 20),
        ("Team", 200),
    ]
    assert _query(runtime, "seats", [], None) == [(1226,)]


def test_an_unobserved_week_reads_null(runtime: Runtime) -> None:
    # A stock has no value for a week nobody observed: filled, it reads NULL, not 0.
    filled = {"temporal_role": ROLE, "grain": "week", "fill": True}
    assert _query(runtime, "seats", [], filled) == [
        (MONDAY, 1122),
        (date(2026, 9, 28), None),
        (date(2026, 10, 5), 220),
    ]
    grouped = _query(runtime, "seats", [PLAN], filled)
    assert [row for row in grouped if row[0] == date(2026, 9, 28) and row[2] is not None] == []
    assert [row for row in grouped if row[2] is not None] == _reference("seats", ["plan"])


@pytest.mark.parametrize(
    ("group_by", "grain"),
    [
        # The stock's own clock: each day is its own period.
        ([DAY], "day"),
        # A calendar dimension: each calendar week is its own period.
        ([CALENDAR_WEEK], "week"),
    ],
)
def test_the_clock_and_calendar_still_split_periods(
    runtime: Runtime, group_by: list[str], grain: str
) -> None:
    by_period = _query(runtime, "seats", [], {"temporal_role": ROLE, "grain": grain})
    assert by_period == _reference("seats", [], grain)
    assert _query(runtime, "seats", group_by, None) == by_period
    # Grouped by the day and the plan at week grain, each day still keeps its own
    # snapshot, under that day's plan.
    if group_by == [DAY]:
        daily = _query(runtime, "seats", [DAY, PLAN], {"temporal_role": ROLE, "grain": "week"})
        assert [row[1:] for row in daily] == _reference("seats", ["plan"], "day")
