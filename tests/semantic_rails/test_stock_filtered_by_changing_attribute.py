"""A stock filtered by an attribute that changes within the period reads its closing snapshot.

An end-of-period stock takes each series' last snapshot in each period, then adds up the
series; grouped by an attribute stored on the snapshot rows, it reads the attribute from
that snapshot. A `where` filter on the same attribute used to run before the snapshot was
chosen: an account on "basic" Monday to Wednesday and "pro" from Thursday counted its
Wednesday fee under `plan = basic`, although the by-plan breakdown puts it under pro.
The filter now reads the chosen snapshot, so `where plan = v` is the `v` row of the
breakdown. Filters on the stock's clock or a calendar dimension apply before the choice;
other date or timestamp attributes refuse because their reading is ambiguous.

A stock summed across its series reads 0 in a period that has snapshots but none that
match (data of nothing), and NULL in a period with no snapshot at all (no data).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import plan_query
from semantic_rails.compiler_parts.sql_lowering import lower_to_sql
from semantic_rails.dialects import DuckDbDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

ROLE = "temporal_role.fees_account_day_date_day"
DAY = "dimension.fees_account_day_date_day"
PLAN = "dimension.fees_account_day_plan"
STATE = "dimension.fees_account_day_state"
SNAPSHOT_WEEK = "dimension.fees_account_day_snapshot_week"
SEGMENT = "dimension.fees_account_segment"
WEEKDAY = "dimension.fees_time_weekday"
REGION = "dimension.fees_owner_region"
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
        "    owner: {key: [owner_id], model: owners}\n"
        "    notice: {key: [notice_id], model: notices}\n"
        "    time: {kind: time, key: [date_day], model: calendar, allowed_as_root: false}\n"
    )
    (package / "models" / "accounts.yml").write_text(
        "model:\n  id: accounts\n  relation: accounts\n  entities: {account: {}}\n"
        "  dimensions:\n    segment: {kind: categorical}\n"
    )
    (package / "models" / "owners.yml").write_text(
        "model:\n  id: owners\n  relation: owners\n  entities: {owner: {}}\n"
        "  dimensions:\n    region: {kind: categorical}\n"
    )
    (package / "models" / "notices.yml").write_text(
        "model:\n  id: notices\n  relation: notices\n"
        "  entities: {notice: {}, account_day: {}}\n"
        "  dimensions:\n    renewal_on: {kind: date}\n    renewal_at: {kind: timestamp}\n"
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
        "  entities: {account_day: {}, account: {}, owner: {}, time: {}}\n"
        "  dimensions:\n"
        "    plan: {kind: categorical}\n"
        "    state: {kind: categorical}\n"
        "    snapshot_week: {kind: date}\n"
        "    renewal_on: {kind: date}\n"
        "    renewal_at: {kind: timestamp}\n"
        "    observed_on: {column: date_day, kind: date}\n"
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
        "alter table account_day add column renewal_on date; "
        "alter table account_day add column renewal_at timestamp; "
        "update account_day set renewal_on = date '2027-01-01', "
        "renewal_at = timestamp '2027-01-01 00:00:00'; "
        "alter table account_day add column owner_id integer; "
        "update account_day set owner_id = case when date_day < date '2026-09-17' "
        "then 1 else 2 end; "
        "create table owners as select * from (values (1, 'west'), (2, 'east')) t(owner_id, region)"
    )
    connection.execute(
        "create table notices (notice_id integer, account_id varchar, date_day date, "
        "renewal_on date, renewal_at timestamp)"
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
    **options: Any,
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
    result = runtime.query({**query, **options})
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
    # A metric predicate chooses the series measured: keeping none, the stock has no data.
    over = {"entity": "entity.fees_account", "measure": "measure.fees.fee", "op": ">"}
    over |= {"value": 1000, "time_alignment": "same_query_period"}
    largest_accounts = {**customers, "kind": "scoped_aggregate", "predicates": [over]}
    del largest_accounts["filter"]
    assert _query(runtime, enterprise, expression=largest_accounts) == [(None,)]


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
    # Judged inside the query's filters, a period with no snapshot passing them has no data.
    assert _query(runtime, enterprise, observation_scope="query") == [(None,)]
    assert _query(runtime, enterprise, time={"grain": "week"}, observation_scope="query") == []
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
        (_is("dimension.fees_time_week_start", "2026-09-14"), "c.week_start = '2026-09-14'", 599),
        # A second dimension on the clock's entity and column is the same clock.
        (
            _is("dimension.fees_account_day_observed_on", "2026-09-16", "<="),
            "date_day <= '2026-09-16'",
            198,
        ),
    ],
)
def test_a_time_bound_applies_before_the_snapshot_is_chosen(
    runtime: Runtime, where: dict[str, Any], before: str, expected: int
) -> None:
    answer = _query(runtime, [where], time={"grain": "week"})
    assert (WEEK, expected) in answer
    assert answer == _reference("true", before=before)


def test_an_entity_set_share_reads_the_same_closing_snapshots(runtime: Runtime) -> None:
    # The share of active accounts' fees from accounts paying over 100 in the period: a ratio
    # over one choice of snapshots, which a's 500 over a's and c's 599 reads, not over b's
    # Tuesday too (698).
    fee = {"kind": "scoped_aggregate", "measure": "measure.fees.fee", "aggregation": "sum"}
    predicate = {
        "entity": "entity.fees_account",
        "measure": "measure.fees.fee",
        "op": ">",
        "value": 100,
        "time_alignment": "same_query_period",
    }
    query = {
        "version": 1,
        "select": [
            {
                "expression": {
                    "kind": "ratio",
                    "numerator": {**fee, "predicates": [predicate]},
                    "denominator": fee,
                },
                "as": "v",
            }
        ],
        "time": {"temporal_role": ROLE, "grain": "week"},
        "where": [_is(STATE, "active")],
    }
    result = runtime.query(query)
    assert "latest_fees_account_day_snapshot" in result["rendered_sql"]
    answer = [(_day(row[f"{ROLE}__week"]), row["v"]) for row in result["rows"]]
    with duckdb.connect() as connection:
        _load(connection)
        reference = connection.execute(
            "select period, coalesce(sum(fee) filter (where fee > 100), 0) * 1.0 / sum(fee) "
            "from (select *, date_trunc('week', date_day)::date as period, row_number() over ("
            "  partition by account_id, date_trunc('week', date_day) order by date_day desc) as rn"
            "  from account_day"
            ") where rn = 1 and state = 'active' group by period order by period"
        ).fetchall()
    assert sorted(answer) == reference
    assert reference[1] == (WEEK, 500 / 599)


def _share(filter_item: dict[str, Any] | None = None) -> dict[str, Any]:
    fee: dict[str, Any] = {
        "kind": "scoped_aggregate",
        "measure": "measure.fees.fee",
        "aggregation": "sum",
    }
    if filter_item:
        fee["where"] = [filter_item]
    predicate = {
        "entity": "entity.fees_account",
        "measure": "measure.fees.fee",
        "op": ">",
        "value": 100,
        "time_alignment": "same_query_period",
    }
    return {"kind": "ratio", "numerator": {**fee, "predicates": [predicate]}, "denominator": fee}


@pytest.mark.parametrize("attribute", ["snapshot_week", "renewal_on", "renewal_at"])
@pytest.mark.parametrize("placement", ["where", "measure_filter", "anchored", "child_group"])
@pytest.mark.parametrize("grain", [None, "week"])
def test_a_date_attribute_filter_refuses(
    runtime: Runtime, attribute: str, placement: str, grain: str | None
) -> None:
    # Child conditions also need the stock reading check, before fanout planning refuses
    # stocks on a child path. The same dimension type must not get a different reading there.
    entity = "notice" if placement == "child_group" else "account_day"
    attribute = "renewal_on" if entity == "notice" and attribute == "snapshot_week" else attribute
    dimension = f"dimension.fees_{entity}_{attribute}"
    item = _is(dimension, "2026-09-14" if attribute == "snapshot_week" else "2027-01-01")
    expression: dict[str, Any] = {"measure": "measure.fees.fee"}
    where = [item]
    if placement == "measure_filter":
        expression = {**expression, "kind": "aggregate", "filter": {"all": [item]}}
        where = []
    elif placement == "anchored":
        expression = _share()
    elif placement == "child_group":
        where = [{"child": "entity.fees_notice", "match": "any", "where": [item]}]
    with pytest.raises(SemanticLayerError) as raised:
        _query(runtime, where, expression=expression, time={"grain": grain} if grain else None)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details == {
        "reason": "stock_filtered_by_date_attribute",
        "dimension": dimension,
    }


def test_a_renewal_date_has_two_distinct_readings(snapshot_lowering, tmp_path: Path) -> None:
    fixture_sql = "update account_day set renewal_on = case when plan = 'basic' "
    fixture_sql += "then date '2027-01-01' else date '2028-01-01' end"
    package = _package(tmp_path)
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        connection.execute(fixture_sql)
    condition = "renewal_on = '2027-01-01'"
    assert (WEEK, 198) in _reference("true", before=condition, fixture_sql=fixture_sql)
    assert (WEEK, 99) in _reference(condition, fixture_sql=fixture_sql)
    runtime = Runtime.from_path(str(package))
    try:
        with pytest.raises(SemanticLayerError) as raised:
            _query(
                runtime,
                [_is("dimension.fees_account_day_renewal_on", "2027-01-01")],
                time={"grain": "week"},
            )
        assert raised.value.details["reason"] == "stock_filtered_by_date_attribute"
    finally:
        runtime.close()


@pytest.mark.parametrize("anchored", [False, True])
def test_lowering_cannot_bypass_the_date_attribute_refusal(
    runtime: Runtime, anchored: bool
) -> None:
    # Force a caller to bypass the planning check: both stock choices must refuse too.
    query = {
        "select": [
            {"expression": _share() if anchored else {"measure": "measure.fees.fee"}, "as": "v"}
        ],
        "time": {"temporal_role": ROLE, "grain": "week"},
    }
    plan = plan_query(runtime.config, runtime.registry, query)
    dimension = "dimension.fees_account_day_renewal_on"
    plan.query["where"] = [_is(dimension, "2027-01-01")]
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(plan, runtime.config)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details == {
        "reason": "stock_filtered_by_date_attribute",
        "dimension": dimension,
    }


def test_an_unknown_filter_dimension_still_has_a_public_error(runtime: Runtime) -> None:
    with pytest.raises(SemanticLayerError) as raised:
        _query(runtime, [_is("dimension.fees_account_day_unknown", "x")])
    assert raised.value.code == "OBJECT_NOT_FOUND"


def test_a_join_through_a_changing_key_reads_the_closing_owner(
    snapshot_lowering, tmp_path: Path
) -> None:
    package = _package(tmp_path)
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        connection.execute("delete from account_day")
        connection.execute(
            "insert into account_day (account_id, date_day, fee, owner_id) values "
            "('a', date '2026-09-14', 20, 2), ('a', date '2026-09-20', 10, 2), "
            "('b', date '2026-09-14', 10, 2), ('b', date '2026-09-20', 20, 1)"
        )
        reference = connection.execute(
            "with chosen as (select *, row_number() over (partition by account_id "
            "order by date_day desc) rn from account_day) "
            "select o.region, sum(d.fee) from chosen d join owners o using (owner_id) "
            "where rn = 1 group by o.region order by o.region"
        ).fetchall()
        # Filtering first also keeps b's former east owner, incorrectly reading 20.
    runtime = Runtime.from_path(str(package))
    try:
        grouped = _query(runtime, group_by=[REGION], time={"grain": "week"})
        assert grouped == [(WEEK, region, fee) for region, fee in reference]
        assert reference == [("east", 10), ("west", 20)]
        for region, fee in reference:
            assert _query(runtime, [_is(REGION, region)], time={"grain": "week"}) == [(WEEK, fee)]
    finally:
        runtime.close()


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_an_unknown_series_key_distinguishes_observed_and_missing_weeks(
    snapshot_lowering, tmp_path: Path, scope: str
) -> None:
    # Use the canonical shop's January gap, without copying a database seed.
    source = Path(__file__).parents[1] / "integration" / "correctness" / "shop"
    package = tmp_path / "shop"
    (package / "models").mkdir(parents=True)
    for source_file in [
        source / "package.yml",
        source / "graph.yml",
        *(source / "models").glob("*.yml"),
    ]:
        target = package / source_file.relative_to(source)
        target.write_text(source_file.read_text())
    config = yaml.safe_load((package / "package.yml").read_text())
    config["package"].update(default_db="data/shop.duckdb", seed={"kind": "external"})
    (package / "package.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    (package / "data").mkdir()
    with duckdb.connect(str(package / "data" / "shop.duckdb")) as connection:
        connection.execute((source / "data" / "seed.sql").read_text())
        reference = connection.execute(
            "with chosen as (select *, date_trunc('week', snapshot_day)::date period, "
            "row_number() over (partition by account_id, date_trunc('week', snapshot_day) "
            "order by snapshot_day desc) rn from account_days) "
            "select g.period, case when count(*) filter (where d.account_id = 999) > 0 "
            "then sum(d.seats) filter (where d.account_id = 999) "
            "when count(d.account_id) = 0 then null "
            f"when '{scope}' = 'dataset' then 0 end "
            "from generate_series(date '2024-01-01', date '2024-01-15', interval 7 day) g(period) "
            "left join chosen d on d.period = g.period and rn = 1 "
            "group by g.period order by g.period"
        ).fetchall()
    runtime = Runtime.from_path(str(package))
    try:
        role = "temporal_role.shop_account_day_snapshot_day"
        result = runtime.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.seats"}, "as": "v"}],
                "time": {
                    "temporal_role": role,
                    "grain": "week",
                    "start": "2024-01-01",
                    "end": "2024-01-22",
                    "fill": True,
                },
                "where": [_is("dimension.shop_account_day_account_id", 999)],
                "observation_scope": scope,
            }
        )
        answer = sorted((_day(row[f"{role}__week"]), row["v"]) for row in result["rows"])
        assert answer == [(_day(period), value) for period, value in reference]
        assert [value for _, value in answer] == (
            [0, None, 0] if scope == "dataset" else [None] * 3
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("placement", ["where", "measure_filter"])
@pytest.mark.parametrize("scope", ["dataset", "query"])
@pytest.mark.parametrize("state", ["active", "enterprise"])
def test_an_anchored_share_reads_its_measure_filter_and_keeps_observed_periods(
    runtime: Runtime, placement: str, scope: str, state: str
) -> None:
    item = _is(STATE, state)
    expression = _share(item if placement == "measure_filter" else None)
    where = [item] if placement == "where" else []
    answer = _query(
        runtime, where, expression=expression, time={"grain": "week"}, observation_scope=scope
    )
    with duckdb.connect() as connection:
        _load(connection)
        reference = connection.execute(
            "with chosen as (select *, date_trunc('week', date_day)::date period, "
            "row_number() over (partition by account_id, date_trunc('week', date_day) "
            "order by date_day desc) rn from account_day) "
            f"select period, case when count(*) filter (where state = '{state}') = 0 then 0 "
            f"else coalesce(sum(fee) filter (where state = '{state}' and fee > 100), 0) * 1.0 "
            f"/ nullif(sum(fee) filter (where state = '{state}'), 0) end "
            f"from chosen where rn = 1 {'and state = ' + repr(state) if scope == 'query' else ''} "
            "group by period order by period"
        ).fetchall()
    assert answer == reference
    assert answer == (
        []
        if state == "enterprise" and scope == "query"
        else [
            (date(2026, 9, 7), 0),
            (WEEK, 500 / 599 if state == "active" else 0),
            (date(2026, 9, 21), 500 / 599 if state == "active" else 0),
        ]
    )


@pytest.mark.parametrize(
    "dimension",
    [
        # The share chooses one snapshot per series and time bucket: grouped by the day or
        # weekday within the week, it read only the week's closing Sunday.
        DAY,
        WEEKDAY,
        "dimension.fees_account_day_observed_on",
        "dimension.fees_time_week_start",
        SNAPSHOT_WEEK,
        "dimension.fees_account_day_renewal_at",
    ],
)
@pytest.mark.parametrize("where", [[], [_is(STATE, "active")]])
@pytest.mark.parametrize("grain", [None, "week"])
def test_an_anchored_share_grouped_by_a_period_refuses(
    runtime: Runtime, dimension: str, where: list[dict[str, Any]], grain: str | None
) -> None:
    with pytest.raises(SemanticLayerError) as raised:
        _query(
            runtime,
            where,
            expression=_share(),
            group_by=[dimension],
            time={"grain": grain} if grain else None,
        )
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details == {
        "reason": "entity_set_ratio_grouped_by_period",
        "dimension": dimension,
    }


def test_an_anchored_share_grouped_by_a_series_attribute_runs(runtime: Runtime) -> None:
    # The account's segment is the same in every snapshot of its series.
    answer = _query(
        runtime,
        [_is(STATE, "active")],
        expression=_share(),
        group_by=[SEGMENT],
        time={"grain": "week"},
    )
    with duckdb.connect() as connection:
        _load(connection)
        reference = connection.execute(
            "with chosen as (select *, date_trunc('week', date_day)::date period, "
            "row_number() over (partition by account_id, date_trunc('week', date_day) "
            "order by date_day desc) rn from account_day) "
            "select period, segment, coalesce(sum(fee) filter (where fee > 100), 0) * 1.0 "
            "/ nullif(sum(fee), 0) from chosen join accounts using (account_id) "
            "where rn = 1 and state = 'active' group by period, segment order by period, segment"
        ).fetchall()
    assert answer == reference
    assert len(answer) == 6
    assert (WEEK, "customer", 1) in answer


def test_lowering_cannot_bypass_the_period_grouping_refusal(runtime: Runtime) -> None:
    # Force a plan past planning: the share's own lowering, under either snapshot choice,
    # refuses a period group that planning never saw.
    query = {
        "select": [{"expression": _share(), "as": "v"}],
        "group_by": [SEGMENT],
        "time": {"temporal_role": ROLE, "grain": "week"},
    }
    plan = plan_query(runtime.config, runtime.registry, query)
    ctes = lower_to_sql(plan, runtime.config).ctes
    assert "latest_fees_account_day_snapshot" in {cte.name for cte in ctes}
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, group_by=[DAY]), runtime.config)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details == {
        "reason": "entity_set_ratio_grouped_by_period",
        "dimension": DAY,
    }


@pytest.mark.parametrize("value", ["0", "null"])
def test_an_anchored_share_keeps_a_matched_zero_or_unknown_denominator_null(
    snapshot_lowering, tmp_path: Path, value: str
) -> None:
    package = _package(tmp_path)
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        connection.execute(f"update account_day set fee = {value} where state = 'active'")
        connection.execute("delete from account_day where date_day >= date '2026-09-21'")
        reference = connection.execute(
            "with chosen as (select *, date_trunc('week', date_day)::date period, "
            "row_number() over (partition by account_id, date_trunc('week', date_day) "
            "order by date_day desc) rn from account_day) "
            "select period, coalesce(sum(fee) filter (where fee > 100), 0) * 1.0 "
            "/ nullif(sum(fee), 0) from chosen where rn = 1 and state = 'active' "
            "group by period order by period"
        ).fetchall()
    runtime = Runtime.from_path(str(package))
    try:
        assert (
            _query(runtime, expression=_share(_is(STATE, "active")), time={"grain": "week"})
            == reference
        )
        assert reference == [(date(2026, 9, 7), None), (WEEK, None)]
    finally:
        runtime.close()
