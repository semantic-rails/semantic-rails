"""A ``where`` filter on the query's own clock cuts a window's lookback like ``time.start``.

A prior-period, rolling, period-to-date or cumulative window reads periods before the ones it
returns. ``time.start`` was refused with these windows, but the same bound written as a
``where`` filter on the clock's date dimension ran: the leaf WHERE dropped the earlier rows, so
the previous day of 2026-09-14 read NULL instead of 99 per account, and a 7-day rolling sum,
a cumulative sum and a month-to-date sum on 2026-09-15 read 40 instead of 45, 75 and 75. Such a
filter is now refused with the same codes and lookback, plus ``details.where_path``, whether it
is on the clock's own dimension, another dimension on its column, or a calendar dimension
joined on it. An upper bound alone still runs, as ``time.end`` does, and so does a filter on
any other dimension.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime

DAY_ROLE = "temporal_role.fees_account_day_date_day"
EVENT_ROLE = "temporal_role.fees_event_occurred_at"
ACCOUNT = "dimension.fees_account_day_account_id"
DAY = "dimension.fees_account_day_date_day"
SNAPSHOT_DATE = "dimension.fees_account_day_snapshot_date"
CALENDAR_DAY = "dimension.fees_time_date_day"
CALENDAR_WEEK = "dimension.fees_time_week_start"
OCCURRED_AT = "dimension.fees_event_occurred_at"
CHANNEL = "dimension.fees_event_channel"
FEE = {"measure": "measure.fees.fee"}
AMOUNT = {"measure": "measure.fees.amount"}
PRIOR_DAY = {"kind": "prior_period", "input": FEE, "offset": {"unit": "day", "value": 1}}
ROLLING = {"kind": "rolling", "input": AMOUNT, "window": {"unit": "day", "value": 7}}
CUMULATIVE = {"kind": "cumulative", "input": AMOUNT}
MONTH_TO_DATE = {"kind": "period_to_date", "period": "month", "input": AMOUNT}
EVENTS = [
    (1, "2026-09-01 10:00:00", "web", 30),
    (2, "2026-09-10 10:00:00", "store", 5),
    (3, "2026-09-15 10:00:00", "web", 40),
]


def _load(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("create table account_day (account_id varchar, date_day date, fee integer)")
    connection.executemany(
        "insert into account_day values (?, ?, ?)",
        [
            (account, day, 99 if day <= date(2026, 9, 13) else 120)
            for day in (date(2026, 9, 1) + timedelta(days=offset) for offset in range(20))
            for account in ("a", "b", "c")
        ],
    )
    connection.execute(
        "create table events (event_id integer, occurred_at timestamp, channel varchar, "
        "amount integer)"
    )
    connection.executemany("insert into events values (?, ?, ?, ?)", EVENTS)
    connection.execute(
        "create table calendar as select d::date as date_day, "
        "date_trunc('week', d)::date as week_start "
        "from generate_series(date '2026-08-01', date '2026-10-31', interval 1 day) t(d)"
    )


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
        "    account_day: {key: [account_id, date_day], model: account_days}\n"
        "    event: {key: [event_id], model: events}\n"
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
    # A daily snapshot joined to the calendar on its clock, with a second date dimension on
    # the clock's column.
    (package / "models" / "account_days.yml").write_text(
        "model:\n"
        "  id: account_days\n"
        "  relation: account_day\n"
        "  entities: {account_day: {}, time: {}}\n"
        "  dimensions:\n"
        "    account_id: {kind: categorical}\n"
        "    snapshot_date: {column: date_day, kind: date}\n"
        "  times:\n"
        "    date_day: {column: date_day, kind: date, class: as_of_time, default: true}\n"
        "  measures:\n"
        "    fee: {kind: aggregate, expr: fee, value_type: currency,\n"
        "      accumulation: {kind: stock, snapshot: end_of_period}}\n"
    )
    (package / "models" / "events.yml").write_text(
        "model:\n"
        "  id: events\n"
        "  relation: events\n"
        "  entities: {event: {}}\n"
        "  dimensions:\n"
        "    channel: {kind: categorical}\n"
        "  times:\n"
        "    occurred_at: {column: occurred_at, kind: timestamp, class: event_time, "
        "default: true}\n"
        "  measures:\n"
        "    amount: {kind: aggregate, expr: amount, value_type: count, "
        "accumulation: {kind: flow}}\n"
    )
    connection = duckdb.connect(str(package / "data" / "fees.duckdb"))
    try:
        _load(connection)
    finally:
        connection.close()
    return package


def _reference(sql: str) -> list[tuple]:
    connection = duckdb.connect()
    try:
        _load(connection)
        return sorted(connection.execute(sql).fetchall(), key=repr)
    finally:
        connection.close()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    runtime = Runtime.from_path(str(_package(tmp_path)))
    yield runtime
    runtime.close()


def _query(expression: dict, where: list[dict], *, grouped: bool = False) -> dict[str, Any]:
    role = DAY_ROLE if expression is PRIOR_DAY else EVENT_ROLE
    return {
        "version": 1,
        "select": [{"expression": expression, "as": "v"}],
        "group_by": [ACCOUNT] if grouped else [],
        "time": {"temporal_role": role, "grain": "day"},
        "where": where,
    }


def _rows(runtime: Runtime, query: dict[str, Any]) -> list[tuple]:
    result = runtime.query(query)
    assert result["ok"], result.get("errors")
    time_alias = f"{query['time']['temporal_role']}__day"
    return sorted(
        (
            (
                *(row[dim] for dim in query["group_by"]),
                date.fromisoformat(str(row[time_alias])[:10]),
                row["v"],
            )
            for row in result["rows"]
        ),
        key=repr,
    )


def _cut(field: str, op: str, value: Any) -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


SINCE_14 = _cut(DAY, ">=", "2026-09-14")
SINCE_15 = _cut(OCCURRED_AT, ">=", "2026-09-15")
# id: expression, where, grouped by account, refusal, path of the cut
ON_THE_CLOCK = {
    "lower-bound": (PRIOR_DAY, [SINCE_14], True, "WINDOWED", "where[0]"),
    "strict-lower-bound": (PRIOR_DAY, [_cut(DAY, ">", "2026-09-13")], True, "WINDOWED", "where[0]"),
    "pinned-total": (PRIOR_DAY, [_cut(DAY, "=", "2026-09-14")], False, "WINDOWED", "where[0]"),
    "in-list": (PRIOR_DAY, [_cut(DAY, "in", ["2026-09-14"])], True, "WINDOWED", "where[0]"),
    "excluded-day": (PRIOR_DAY, [_cut(DAY, "!=", "2026-09-13")], True, "WINDOWED", "where[0]"),
    "same-column": (
        PRIOR_DAY,
        [_cut(SNAPSHOT_DATE, ">=", "2026-09-14")],
        True,
        "WINDOWED",
        "where[0]",
    ),
    "calendar-day": (
        PRIOR_DAY,
        [_cut(CALENDAR_DAY, ">=", "2026-09-14")],
        True,
        "WINDOWED",
        "where[0]",
    ),
    "calendar-week": (
        PRIOR_DAY,
        [_cut(CALENDAR_WEEK, "=", "2026-09-14")],
        True,
        "WINDOWED",
        "where[0]",
    ),
    "second-item": (PRIOR_DAY, [_cut(ACCOUNT, "=", "a"), SINCE_14], True, "WINDOWED", "where[1]"),
    # The guard reads a child group's conditions too. (A group on the measure's own rows is
    # refused later in any case; this checks the guard's depth.)
    "child-group": (
        PRIOR_DAY,
        [{"child": "entity.fees_account_day", "match": "any", "where": [SINCE_14]}],
        True,
        "WINDOWED",
        "where[0].where[0]",
    ),
    "rolling": (ROLLING, [SINCE_15], False, "WINDOWED", "where[0]"),
    "cumulative": (CUMULATIVE, [SINCE_15], False, "CUMULATIVE", "where[0]"),
    "period-to-date": (MONTH_TO_DATE, [SINCE_15], False, "WINDOWED", "where[0]"),
}


@pytest.mark.parametrize(
    ("expression", "where", "grouped", "code", "where_path"),
    list(ON_THE_CLOCK.values()),
    ids=list(ON_THE_CLOCK),
)
def test_a_where_cut_of_the_clock_refuses(
    runtime: Runtime,
    expression: dict,
    where: list[dict],
    grouped: bool,
    code: str,
    where_path: str,
) -> None:
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(_query(expression, where, grouped=grouped))
    assert caught.value.code == f"{code}_TIME_FILTER_UNSUPPORTED"
    details = caught.value.details
    assert details["where_path"] == where_path
    cut = where[-1] if "field" in where[-1] else where[-1]["where"][0]
    assert details["where"] == cut
    # A lower bound is where a widened start goes; a pin or an exclusion has none.
    assert details.get("start") == (cut["value"] if cut["op"] in {">=", ">"} else None)
    if expression is ROLLING:
        assert details["lookback"] == {
            "kind": "OffsetWindowExpr[rolling]",
            "unit": "day",
            "value": 7,
        }


def test_the_uncut_series_has_the_reference_values_the_cut_lost(runtime: Runtime) -> None:
    """What the refused cuts read as NULL or 40, from the uncut series and independent SQL."""
    reference = _reference("select account_id, fee from account_day where date_day = '2026-09-13'")
    assert reference == [("a", 99), ("b", 99), ("c", 99)]
    grouped = {
        (account, day): value
        for account, day, value in _rows(runtime, _query(PRIOR_DAY, [], grouped=True))
    }
    assert [(account, grouped[(account, date(2026, 9, 14))]) for account in "abc"] == reference
    assert dict(_rows(runtime, _query(PRIOR_DAY, [])))[date(2026, 9, 14)] == 297
    for expression, since, expected in (
        (ROLLING, "2026-09-09", 45),
        (CUMULATIVE, "2000-01-01", 75),
        (MONTH_TO_DATE, "2026-09-01", 75),
    ):
        assert _reference(
            "select sum(amount) from events "
            f"where occurred_at::date between date '{since}' and date '2026-09-15'"
        ) == [(expected,)]
        assert dict(_rows(runtime, _query(expression, [])))[date(2026, 9, 15)] == expected


def test_time_start_refusal_is_unchanged(runtime: Runtime) -> None:
    query = _query(PRIOR_DAY, [])
    query["time"]["start"] = "2026-09-14"
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(query)
    assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert str(caught.value) == (
        "Rolling, prior_period, and period_to_date expressions do not support a bounded "
        "query.time.start: the leaf-level WHERE truncates the lookback rows the window "
        "function depends on, producing silently wrong values. Remove the start boundary "
        "or widen it by the metric's window/offset/period lookback."
    )
    assert caught.value.details["start"] == "2026-09-14"
    assert "where_path" not in caught.value.details
    query = _query(CUMULATIVE, [])
    query["time"]["start"] = "2026-09-15"
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(query)
    assert caught.value.code == "CUMULATIVE_TIME_FILTER_UNSUPPORTED"
    assert str(caught.value) == (
        "Cumulative expressions do not support a bounded query.time.start; widen the time "
        "window or remove the start boundary"
    )
    assert set(caught.value.details) == {"start", "expression"}


PRIOR_REFERENCE = (
    "select a.account_id, a.date_day, p.fee from account_day a "
    "left join account_day p on p.account_id = a.account_id and p.date_day = a.date_day - 1 "
    "join calendar c on c.date_day = a.date_day where {cut}"
)
# The engine's rolling rows run daily from the first to the last day with rows; cumulative
# and month-to-date rows are the days with rows.
ROLLING_REFERENCE = (
    "with e as (select occurred_at::date as d, amount from events where {cut}), "
    "days as (select unnest(generate_series(min(d), max(d), interval 1 day))::date as d from e) "
    "select days.d, coalesce((select sum(amount) from e where e.d between days.d - 6 and days.d), 0) "
    "from days"
)
RUNNING_REFERENCE = (
    "with e as (select occurred_at::date as d, amount from events where {cut}) "
    "select distinct d, (select sum(amount) from e as p where p.d <= e.d {period}) from e"
)


@pytest.mark.parametrize(
    ("expression", "where", "reference"),
    [
        pytest.param(
            PRIOR_DAY,
            [_cut(DAY, "<=", "2026-09-14")],
            PRIOR_REFERENCE.format(cut="a.date_day <= '2026-09-14'"),
            id="prior-upper",
        ),
        pytest.param(
            PRIOR_DAY,
            [_cut(CALENDAR_WEEK, "<", "2026-09-14")],
            PRIOR_REFERENCE.format(cut="c.week_start < '2026-09-14'"),
            id="calendar-upper",
        ),
        pytest.param(
            PRIOR_DAY,
            [_cut(ACCOUNT, "=", "a")],
            PRIOR_REFERENCE.format(cut="a.account_id = 'a'"),
            id="prior-account",
        ),
        pytest.param(
            ROLLING,
            [_cut(OCCURRED_AT, "<", "2026-09-15")],
            ROLLING_REFERENCE.format(cut="occurred_at < '2026-09-15'"),
            id="rolling-upper",
        ),
        pytest.param(
            ROLLING,
            [_cut(CHANNEL, "=", "web")],
            ROLLING_REFERENCE.format(cut="channel = 'web'"),
            id="rolling-channel",
        ),
        pytest.param(
            CUMULATIVE,
            [_cut(OCCURRED_AT, "<=", "2026-09-15 12:00:00")],
            RUNNING_REFERENCE.format(cut="occurred_at <= '2026-09-15 12:00:00'", period=""),
            id="cumulative-upper",
        ),
        pytest.param(
            CUMULATIVE,
            [_cut(CHANNEL, "=", "web")],
            RUNNING_REFERENCE.format(cut="channel = 'web'", period=""),
            id="cumulative-channel",
        ),
        pytest.param(
            MONTH_TO_DATE,
            [_cut(CHANNEL, "!=", "web")],
            RUNNING_REFERENCE.format(
                cut="channel != 'web'",
                period="and date_trunc('month', p.d) = date_trunc('month', e.d)",
            ),
            id="month-to-date-channel",
        ),
    ],
)
def test_an_upper_bound_or_another_dimension_matches_reference(
    runtime: Runtime, expression: dict, where: list[dict], reference: str
) -> None:
    grouped = expression is PRIOR_DAY
    rows = _rows(runtime, _query(expression, where, grouped=grouped))
    assert rows == _reference(reference)


def test_mcp_execute_returns_the_refusal_with_a_patch_for_the_filter(runtime: Runtime) -> None:
    mcp = SemanticLayerMCPAdapter(runtime)
    query = _query(PRIOR_DAY, [_cut(ACCOUNT, "=", "a"), _cut(DAY, ">=", "2026-09-14")])
    response = mcp.call_tool("execute", {"query": query})
    assert response["ok"] is False
    error = response["errors"][0]
    assert error["code"] == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert error["details"]["where_path"] == "where[1]"
    hints = {hint["kind"]: hint for hint in error["recovery_hints"]}
    assert hints["drop_time_start"]["patch"] == {"remove": ["where[1]"]}
    assert hints["widen_time_window"]["suggested_start"] == "2026-09-13"
