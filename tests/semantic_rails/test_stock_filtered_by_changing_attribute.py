"""A stock filtered by an attribute that changes within the period reads its closing snapshot.

An end-of-period stock takes each series' last snapshot in each period, then adds up the
series; grouped by an attribute stored on the snapshot rows, it reads the attribute from
that snapshot. A `where` filter on the same attribute used to run before the snapshot was
chosen: an account on "basic" Monday to Wednesday and "pro" from Thursday counted its
Wednesday fee under `plan = basic`, although the by-plan breakdown puts it under pro.
The filter now reads the chosen snapshot, so `where plan = v` is the `v` row of the
breakdown. Filters that bound time (the stock's clock, a calendar dimension, a date) still
apply before the choice.

A stock summed across its series reads 0 in a period that has snapshots but none that
match (data of nothing), and NULL in a period with no snapshot at all (no data).
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

ROLE = "temporal_role.fees_account_day_date_day"
DAY = "dimension.fees_account_day_date_day"
PLAN = "dimension.fees_account_day_plan"
STATE = "dimension.fees_account_day_state"
SNAPSHOT_WEEK = "dimension.fees_account_day_snapshot_week"
SEGMENT = "dimension.fees_account_segment"
WEEKDAY = "dimension.fees_time_weekday"
WEEK = date(2026, 9, 14)
FILTERED = {PLAN: "plan", STATE: "state", SEGMENT: "segment"}


def _rows() -> list[tuple[str, date, str, str, int]]:
    rows = []
    day = date(2026, 9, 7)
    while day <= date(2026, 9, 27):
        # a: basic at 99 through Wednesday 09-16, pro at 500 from Thursday 09-17.
        upgraded = day >= date(2026, 9, 17)
        rows.append(("a", day, "pro" if upgraded else "basic", "active", 500 if upgraded else 99))
        # b: basic and active at 99 through Tuesday 09-15, cancelled at 0 from 09-16.
        cancelled = day >= date(2026, 9, 16)
        rows.append(
            ("b", day, "basic", "cancelled" if cancelled else "active", 0 if cancelled else 99)
        )
        # c: basic and active at 99 every day.
        rows.append(("c", day, "basic", "active", 99))
        day += timedelta(days=1)
    return rows


def _package(root: Path) -> Path:
    package = root / "fees"
    (package / "models").mkdir(parents=True)
    (package / "data").mkdir()
    (package / "package.yml").write_text(
        "schema_version: 1\n"
        "package: {id: fees, namespace: fees, name: fees, warehouse: duckdb,\n"
        "  default_db: data/fees.duckdb, seed: {kind: external}, schema_strict: true,\n"
        "  environments: [development]}\n"
    )
    (package / "graph.yml").write_text(
        "graph:\n  entities:\n"
        "    account_day: {key: [account_id, date_day], model: account_days, "
        "allowed_as_root: true}\n"
        "    account: {key: [account_id], model: accounts}\n"
        "    time: {kind: time, key: [date_day], model: calendar, allowed_as_root: false}\n"
    )
    (package / "models" / "accounts.yml").write_text(
        "model:\n  id: accounts\n  relation: accounts\n  entities: {account: {}}\n"
        "  dimensions:\n    segment: {kind: categorical}\n"
    )
    (package / "models" / "calendar.yml").write_text(
        "model:\n  id: calendar\n  relation: calendar\n  calendar_id: default\n"
        "  entities: {time: {}}\n"
        "  times:\n    date_day: {column: date_day, kind: date, class: calendar_time}\n"
        "  dimensions:\n    weekday: {kind: integer}\n    week_start: {kind: date}\n"
    )
    stock = "kind: aggregate, expr: fee, value_type: currency, accumulation: {kind: stock, snapshot"
    (package / "models" / "account_days.yml").write_text(
        "model:\n  id: account_days\n  relation: account_day\n"
        "  entities: {account_day: {}, account: {}, time: {}}\n"
        "  dimensions:\n"
        "    plan: {kind: categorical}\n"
        "    state: {kind: categorical}\n"
        "    snapshot_week: {kind: date}\n"
        "  times:\n"
        "    date_day: {column: date_day, kind: date, class: as_of_time, default: true}\n"
        "  measures:\n"
        f"    fee: {{{stock}: end_of_period}}}}\n"
        f"    fee_sop: {{{stock}: start_of_period}}}}\n"
    )
    connection = duckdb.connect(str(package / "data" / "fees.duckdb"))
    _load(connection)
    connection.close()
    return package


def _load(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        "create table account_day (account_id varchar, date_day date, plan varchar, "
        "state varchar, fee integer)"
    )
    connection.executemany("insert into account_day values (?, ?, ?, ?, ?)", _rows())
    connection.execute(
        "alter table account_day add column snapshot_week date; "
        "update account_day set snapshot_week = date_trunc('week', date_day)::date"
    )
    connection.execute(
        "create table accounts as select * from (values ('a', 'customer'), ('b', 'customer'), "
        "('c', 'internal')) t(account_id, segment)"
    )
    connection.execute(
        "create table calendar as select d::date as date_day, isodow(d) as weekday, "
        "date_trunc('week', d)::date as week_start "
        "from generate_series(date '2026-09-01', date '2026-10-31', interval 1 day) t(d)"
    )


def _reference(
    condition: str,
    *,
    measure: str = "fee",
    grain: str = "week",
    before: str = "true",
    fixture_sql: str = "",
) -> list[tuple[Any, ...]]:
    """Each account's last (first) snapshot per period among the rows `before` keeps, then
    the sum of those meeting `condition`: 0 in a period whose snapshots all fail it, and
    NULL where those meeting it have no known fee."""
    direction = "asc" if measure == "fee_sop" else "desc"
    period = f"date_trunc('{grain}', d.date_day)::date"
    connection = duckdb.connect()
    try:
        _load(connection)
        if fixture_sql:
            connection.execute(fixture_sql)
        return connection.execute(
            "with chosen as ("
            f"  select d.*, s.segment, c.weekday, {period} as period, row_number() over ("
            f"    partition by d.account_id, {period} order by d.date_day {direction}) as rn"
            "  from account_day d join accounts s using (account_id)"
            "  join calendar c using (date_day)"
            f"  where {before}"
            ") "
            f"select period, case when count(*) filter (where {condition}) = 0 then 0 "
            f"  else sum(fee) filter (where {condition}) end "
            "from chosen where rn = 1 group by period order by period"
        ).fetchall()
    finally:
        connection.close()


def _query(
    runtime: Runtime,
    where: list[dict[str, Any]] | None = None,
    *,
    measure: str = "fee",
    group_by: list[str] | None = None,
    time: dict[str, Any] | None = None,
    expression: dict[str, Any] | None = None,
) -> list[tuple[Any, ...]]:
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"expression": expression or {"measure": f"measure.fees.{measure}"}, "as": "v"}],
        "group_by": list(group_by or []),
    }
    if time is not None:
        query["time"] = {"temporal_role": ROLE, **time}
    if where:
        query["where"] = where
    result = runtime.query(query)
    assert result["ok"], result.get("errors")
    time_alias = f"{ROLE}__{time['grain']}" if time and time.get("grain") else None
    rows = []
    for row in result["rows"]:
        period = [_day(row[time_alias])] if time_alias else []
        groups = [_day(row[dim]) if dim == DAY else row[dim] for dim in group_by or []]
        rows.append((*period, *groups, row["v"]))
    return sorted(rows, key=lambda row: tuple(str(cell) for cell in row))


def _is(field: str, value: Any, op: str = "=") -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


def _day(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


@pytest.fixture(params=["aggregate_join", "qualify"])
def snapshot_lowering(request, monkeypatch) -> None:
    # DuckDB lowers the snapshot choice as an aggregate joined back; warehouses with
    # QUALIFY (Snowflake, BigQuery, Databricks) use ROW_NUMBER. DuckDB runs both.
    if request.param == "qualify":
        capabilities = DuckDbDialect.capabilities
        monkeypatch.setattr(
            DuckDbDialect, "capabilities", lambda self: {**capabilities(self), "qualify": True}
        )


@pytest.fixture
def runtime(snapshot_lowering, tmp_path: Path) -> Iterator[Runtime]:
    runtime = Runtime.from_path(str(_package(tmp_path)))
    yield runtime
    runtime.close()


@pytest.mark.parametrize(
    ("where", "condition", "closing_week"),
    [
        # a closes the week on pro, b at 0 (cancelled): only c's 99, not a's Wednesday too (198).
        (_is(PLAN, "basic"), "plan = 'basic'", 99),
        # b cancelled on Wednesday: a's 500 and c's 99, not b's Tuesday too (698).
        (_is(STATE, "active"), "state = 'active'", 599),
        (_is(PLAN, ["pro", "enterprise"], "IN"), "plan in ('pro', 'enterprise')", 500),
        # Reached through the account: the same for every snapshot of a series.
        (_is(SEGMENT, "customer"), "segment = 'customer'", 500),
    ],
)
def test_a_filter_reads_each_series_closing_snapshot(
    runtime: Runtime, where: dict[str, Any], condition: str, closing_week: int
) -> None:
    answer = _query(runtime, [where], time={"grain": "week"})
    assert answer == _reference(condition)
    assert (WEEK, closing_week) in answer


def test_start_of_period_reads_each_series_opening_snapshot(runtime: Runtime) -> None:
    week = {"grain": "week"}
    # a opens the week of 09-14 on basic and the next on pro.
    pro = _query(runtime, [_is(PLAN, "pro")], measure="fee_sop", time=week)
    assert pro == _reference("plan = 'pro'", measure="fee_sop")
    assert pro == [(date(2026, 9, 7), 0), (WEEK, 0), (date(2026, 9, 21), 500)]
    basic = _query(runtime, [_is(PLAN, "basic")], measure="fee_sop", time=week)
    assert basic == _reference("plan = 'basic'", measure="fee_sop")
    assert (WEEK, 297) in basic


@pytest.mark.parametrize("measure", ["fee", "fee_sop"])
@pytest.mark.parametrize("grain", ["day", "week", "month"])
@pytest.mark.parametrize("field", [PLAN, STATE, SEGMENT])
def test_a_filter_equals_its_row_of_the_grouped_breakdown(
    runtime: Runtime, measure: str, grain: str, field: str
) -> None:
    time = {"grain": grain}
    grouped = {
        (period, value): fee
        for period, value, fee in _query(runtime, measure=measure, group_by=[field], time=time)
    }
    periods = sorted({period for period, _ in grouped})
    for value in sorted({value for _, value in grouped}):
        filtered = _query(runtime, [_is(field, value)], measure=measure, time=time)
        # A period whose closing snapshots all hold another value reads 0.
        assert filtered == [(period, grouped.get((period, value), 0)) for period in periods]
        assert filtered == _reference(
            f"{FILTERED[field]} = '{value}'", measure=measure, grain=grain
        )


def test_no_snapshot_reads_null_and_no_match_reads_zero(runtime: Runtime) -> None:
    enterprise = [_is(PLAN, "enterprise")]
    filled = {"grain": "week", "start": "2026-09-07", "end": "2026-10-12", "fill": True}
    # Every account has snapshots through the week of 09-21; none in the two after it.
    assert _query(runtime, enterprise, time=filled) == [
        (date(2026, 9, 7), 0),
        (WEEK, 0),
        (date(2026, 9, 21), 0),
        (date(2026, 9, 28), None),
        (date(2026, 10, 5), None),
    ]
    assert _query(runtime, enterprise, time={"grain": "week"}) == _reference("plan = 'enterprise'")
    # Untimed, the period is everything; a date with no snapshot has no data.
    assert _query(runtime, enterprise) == [(0,)]
    assert _query(runtime, [*enterprise, _is(DAY, "2026-09-20")]) == [(0,)]
    assert _query(runtime, [_is(PLAN, "basic"), _is(DAY, "2026-09-30")]) == [(None,)]
    # A filter on the account, and one authored on the measure, settle the same way.
    assert _query(runtime, [_is(SEGMENT, "partner")]) == [(0,)]
    customers = {
        "kind": "aggregate",
        "measure": "measure.fees.fee",
        "filter": {"all": [_is(SEGMENT, "customer")]},
    }
    internal = [_is("dimension.fees_account_day_account_id", "c")]
    assert _query(runtime, internal, expression=customers) == [(0,)]
    assert _query(runtime, internal, expression=customers, time={"grain": "week"}) == [
        (date(2026, 9, 7), 0),
        (WEEK, 0),
        (date(2026, 9, 21), 0),
    ]


def test_a_matching_snapshot_of_unknown_value_stays_null(snapshot_lowering, tmp_path) -> None:
    fixture_sql = "update account_day set fee = null where account_id = 'c'"
    package = _package(tmp_path)
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        connection.execute(fixture_sql)
    runtime = Runtime.from_path(str(package))
    try:
        week = {"grain": "week"}
        internal = _query(runtime, [_is(SEGMENT, "internal")], time=week)
        # c's closing snapshot is kept each week, with no known fee: unknown, not 0.
        assert [value for _, value in internal] == [None, None, None]
        assert internal == _reference("segment = 'internal'", fixture_sql=fixture_sql)
        assert _query(runtime, [_is(PLAN, "enterprise")]) == [(0,)]
    finally:
        runtime.close()


def test_only_a_balance_summed_across_series_reads_zero(runtime: Runtime) -> None:
    enterprise = [_is(PLAN, "enterprise")]
    largest = {"measure": "measure.fees.fee", "aggregation": "max"}
    # The largest closing fee of no account is undefined.
    assert _query(runtime, enterprise, expression=largest) == [(None,)]
    assert _query(runtime, enterprise, expression=largest, time={"grain": "week"}) == []
    assert _query(runtime, [_is(PLAN, "basic")], expression=largest, time={"grain": "week"}) == [
        (date(2026, 9, 7), 99),
        (WEEK, 99),
        (date(2026, 9, 21), 99),
    ]
    # Grouped by an attribute, an empty group is absent, as it is for a flow.
    assert _query(runtime, enterprise, group_by=[STATE], time={"grain": "week"}) == []
    assert _query(runtime, [_is(PLAN, "basic")], group_by=[STATE], time={"grain": "week"}) == [
        (date(2026, 9, 7), "active", 297),
        (WEEK, "active", 99),
        (WEEK, "cancelled", 0),
        (date(2026, 9, 21), "active", 99),
        (date(2026, 9, 21), "cancelled", 0),
    ]


@pytest.mark.parametrize(
    ("where", "before", "expected"),
    [
        # The stock's clock: as of Wednesday, a is still on basic and b is cancelled.
        (_is(DAY, "2026-09-16", "<="), "date_day <= '2026-09-16'", 198),
        # A calendar dimension: the last Monday or Tuesday, when b was still active.
        (_is(WEEKDAY, 2, "<="), "weekday <= 2", 297),
        # A date attribute bounds time like the clock: the last snapshot of that week.
        (_is(SNAPSHOT_WEEK, "2026-09-14"), "snapshot_week = '2026-09-14'", 599),
    ],
)
def test_a_time_bound_applies_before_the_snapshot_is_chosen(
    runtime: Runtime, where: dict[str, Any], before: str, expected: int
) -> None:
    answer = _query(runtime, [where], time={"grain": "week"})
    assert (WEEK, expected) in answer
    assert answer == _reference("true", before=before)
