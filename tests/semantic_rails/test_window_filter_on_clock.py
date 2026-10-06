"""A ``where`` filter on a date cuts a window's lookback like ``time.start``.

A prior-period, rolling, period-to-date or cumulative window reads periods before the ones it
returns. ``time.start`` was refused with these windows, but the same bound written as a
``where`` filter on the clock's date dimension ran: the leaf WHERE dropped the earlier rows, so
the previous day of 2026-09-14 read NULL instead of 99 per account, and a 7-day rolling sum,
a cumulative sum and a month-to-date sum on 2026-09-15 read 40 instead of 45, 75 and 75. A
snapshot's own date cuts the same way when the query's clock is a calendar the snapshot has no
relationship to. Such a filter is now refused with the same codes and lookback, plus
``details.where_path``, when its dimension is temporal (a time role, a date, timestamp,
datetime or time kind, on the same table column as one of those, or on a column a
relationship pairs with one) or on a calendar, whatever its relationship to the clock. An
upper bound on a date or timestamp alone still runs, as ``time.end`` does, and so does a
filter on any other dimension.
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
SECOND_CALENDAR_DAY = "dimension.fees_time2_date_day"
LINKED_DATE = "dimension.fees_snapshot_label_observed_date"
CALENDAR_ROLE = "temporal_role.fees_time_date_day"
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
        "date_trunc('week', d)::date as week_start, date_trunc('month', d)::date as month_start "
        "from generate_series(date '2026-08-01', date '2026-10-31', interval 1 day) t(d)"
    )
    connection.execute("create table calendar2 as select * from calendar")


def _load_august(connection: duckdb.DuckDBPyConnection) -> None:
    connection.executemany(
        "insert into account_day values (?, ?, 99)",
        [
            (account, date(2026, 8, 1) + timedelta(days=offset))
            for offset in range(31)
            for account in ("a", "b", "c")
        ],
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


def _reference(sql: str, *, august: bool = False) -> list[tuple]:
    connection = duckdb.connect()
    try:
        _load(connection)
        if august:
            _load_august(connection)
        return sorted(connection.execute(sql).fetchall(), key=repr)
    finally:
        connection.close()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    runtime = Runtime.from_path(str(_package(tmp_path)))
    yield runtime
    runtime.close()


SNAPSHOT_LABEL_ENTITY = (
    "    snapshot_label: {key: [account_id, date_day], model: snapshot_labels}\n"
)
OBSERVED_DATE_LINE = "    observed_date: {column: date_day, kind: date}\n"
# The clock is on the target side of this non-calendar, composite-key relationship.
SNAPSHOT_LABELS = (
    "model:\n"
    "  id: snapshot_labels\n"
    "  relation: snapshot_labels\n"
    "  entities: {snapshot_label: {}, account_day: {}}\n"
    "  dimensions:\n" + OBSERVED_DATE_LINE
)
SNAPSHOT_LABELS_TABLE = (
    "create table snapshot_labels as select account_id, date_day from account_day"
)


@pytest.fixture
def related_runtime(tmp_path: Path) -> Iterator[Runtime]:
    package = _package(tmp_path)
    graph = package / "graph.yml"
    graph.write_text(
        graph.read_text()
        + "    time2: {kind: time, key: [date_day], model: calendar2, allowed_as_root: false}\n"
        + SNAPSHOT_LABEL_ENTITY
    )
    calendar = package / "models" / "calendar.yml"
    calendar.write_text(
        calendar.read_text().replace("entities: {time: {}}", "entities: {time: {}, time2: {}}")
    )
    (package / "models" / "calendar2.yml").write_text(
        "model:\n"
        "  id: calendar2\n"
        "  relation: calendar2\n"
        "  calendar_id: secondary\n"
        "  entities: {time2: {}}\n"
        "  times:\n"
        "    date_day: {column: date_day, kind: date, class: calendar_time}\n"
    )
    (package / "models" / "snapshot_labels.yml").write_text(SNAPSHOT_LABELS)
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        connection.execute(SNAPSHOT_LABELS_TABLE)
    runtime = Runtime.from_path(str(package))
    yield runtime
    runtime.close()


@pytest.fixture
def disconnected_runtime(tmp_path: Path) -> Iterator[Runtime]:
    """The snapshot with August history and no relationship to the calendar, which has a
    month grain."""
    package = _package(tmp_path)
    snapshot = package / "models" / "account_days.yml"
    snapshot.write_text(
        snapshot.read_text().replace(
            "entities: {account_day: {}, time: {}}", "entities: {account_day: {}}"
        )
    )
    calendar = package / "models" / "calendar.yml"
    calendar.write_text(calendar.read_text() + "    month_start: {kind: date}\n")
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        _load_august(connection)
    runtime = Runtime.from_path(str(package))
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
    time_alias = f"{query['time']['temporal_role']}__{query['time']['grain']}"
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


@pytest.mark.parametrize(
    ("field", "op"),
    [
        pytest.param(SECOND_CALENDAR_DAY, ">=", id="two-hop-lower-bound"),
        pytest.param(SECOND_CALENDAR_DAY, "=", id="two-hop-pin"),
        pytest.param(LINKED_DATE, ">=", id="reverse-non-calendar"),
    ],
)
def test_a_related_clock_cut_refuses(related_runtime: Runtime, field: str, op: str) -> None:
    cut = _cut(field, op, "2026-09-14")
    with pytest.raises(SemanticLayerError) as caught:
        related_runtime.query(_query(PRIOR_DAY, [cut], grouped=True))
    assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert caught.value.details["where_path"] == "where[0]"
    assert caught.value.details["where"] == cut


def test_a_two_hop_calendar_upper_bound_matches_reference(related_runtime: Runtime) -> None:
    query = _query(PRIOR_DAY, [_cut(SECOND_CALENDAR_DAY, "<=", "2026-09-14")], grouped=True)
    reference = _reference(
        "select a.account_id, a.date_day, p.fee from account_day a "
        "left join account_day p on p.account_id = a.account_id and p.date_day = a.date_day - 1 "
        "join calendar c on c.date_day = a.date_day "
        "join calendar2 c2 on c2.date_day = c.date_day where c2.date_day <= '2026-09-14'"
    )
    assert _rows(related_runtime, query) == reference
    assert [(account, value) for account, day, value in reference if day == date(2026, 9, 14)] == [
        ("a", 99),
        ("b", 99),
        ("c", 99),
    ]


PRIOR_MONTH = {"kind": "prior_period", "input": FEE, "offset": {"unit": "month", "value": 1}}


def _monthly(where: list[dict]) -> dict[str, Any]:
    query = _query(PRIOR_MONTH, where, grouped=True)
    query["time"] = {"temporal_role": CALENDAR_ROLE, "grain": "month"}
    return query


def test_monthly_snapshot_own_clock_cut_refuses(runtime: Runtime) -> None:
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(_monthly([_cut(DAY, ">=", "2026-09-01")]))
    assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert caught.value.details["where_path"] == "where[0]"


def test_a_date_cut_of_a_snapshot_with_no_calendar_relationship_refuses(
    disconnected_runtime: Runtime,
) -> None:
    """The cut is not on the query clock, but on the snapshot's own date, which the leaf filters
    before its window: September read NULL instead of each account's last August snapshot."""
    cut = _cut(DAY, ">=", "2026-09-01")
    with pytest.raises(SemanticLayerError) as caught:
        disconnected_runtime.query(_monthly([cut]))
    assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert caught.value.details["where_path"] == "where[0]"
    assert caught.value.details["where"] == cut


def test_an_upper_bound_on_a_snapshot_with_no_calendar_relationship_matches_reference(
    disconnected_runtime: Runtime,
) -> None:
    rows = _rows(disconnected_runtime, _monthly([_cut(DAY, "<=", "2026-09-30")]))
    september = [(account, value) for account, month, value in rows if month == date(2026, 9, 1)]
    reference = _reference(
        "select account_id, fee from account_day a where date_day = (select max(date_day) "
        "from account_day p where p.account_id = a.account_id and p.date_day < '2026-09-01')",
        august=True,
    )
    assert september == reference == [("a", 99), ("b", 99), ("c", 99)]


SHIPPED = "dimension.fees_event_shipped_on"
CHANNEL_LINE = "    channel: {kind: categorical}\n"
ACCOUNT_LINE = "    account_id: {kind: categorical}\n"
SNAPSHOT_DATE_LINE = "    snapshot_date: {column: date_day, kind: date}\n"


def _edited_runtime(tmp_path: Path, edits: dict[str, dict[str, str] | str]) -> Runtime:
    """The package with the graph's or each model's text replaced, or a new model given whole,
    a ship date one day after each event, and the tables the new models read."""
    package = _package(tmp_path)
    for name, edit in edits.items():
        path = package / ("graph.yml" if name == "graph" else f"models/{name}.yml")
        if isinstance(edit, str):
            path.write_text(edit)
            continue
        text = path.read_text()
        for old, new in edit.items():
            assert old in text
            text = text.replace(old, new)
        path.write_text(text)
    with duckdb.connect(str(package / "data" / "fees.duckdb")) as connection:
        connection.execute("alter table events add column shipped_on date")
        connection.execute("update events set shipped_on = occurred_at::date + 1")
        connection.execute(SNAPSHOT_LABELS_TABLE)
        connection.execute("create table day_infos as select distinct date_day from account_day")
    return Runtime.from_path(str(package))


GRAPH_ENTITIES = "graph:\n  entities:\n"
# A non-calendar entity keyed on the clock's column: account_days is the source this time.
DAY_INFO = {
    "graph": {
        GRAPH_ENTITIES: GRAPH_ENTITIES + "    day_info: {key: [date_day], model: day_infos}\n"
    },
    "account_days": {
        "entities: {account_day: {}, time: {}}": "entities: {account_day: {}, time: {}, "
        "day_info: {}}"
    },
    "day_infos": (
        "model:\n"
        "  id: day_infos\n"
        "  relation: day_infos\n"
        "  entities: {day_info: {}}\n"
        "  dimensions:\n"
        "    label: {column: date_day, kind: categorical}\n"
    ),
}
# id: graph and model edits, expression, field, operator. Each dimension is temporal by its
# own kind, by its column, or by a relationship pairing its column with a temporal one,
# whatever its relationship to the query clock.
BY_TYPE = {
    "categorical-on-the-clock-column": (
        {"account_days": {ACCOUNT_LINE: ACCOUNT_LINE + "    label: {column: date_day}\n"}},
        PRIOR_DAY,
        "dimension.fees_account_day_label",
        ">=",
    ),
    # Not date-typed, so an upper bound refuses too.
    "categorical-upper-bound": (
        {"account_days": {ACCOUNT_LINE: ACCOUNT_LINE + "    label: {column: date_day}\n"}},
        PRIOR_DAY,
        "dimension.fees_account_day_label",
        "<=",
    ),
    "datetime-kind": (
        {
            "account_days": {
                SNAPSHOT_DATE_LINE: "    snapshot_date: {column: date_day, kind: datetime}\n"
            }
        },
        PRIOR_DAY,
        SNAPSHOT_DATE,
        ">=",
    ),
    "clock-of-another-kind": (
        {
            "account_days": {
                SNAPSHOT_DATE_LINE: "",
                "kind: date, class: as_of_time": "kind: categorical, class: as_of_time",
            }
        },
        PRIOR_DAY,
        DAY,
        ">=",
    ),
    # Not the window's clock; still a date, so it may cut the lookback.
    "another-date-of-the-measure": (
        {"events": {CHANNEL_LINE: CHANNEL_LINE + "    shipped_on: {kind: date}\n"}},
        ROLLING,
        SHIPPED,
        ">=",
    ),
    "kind-in-capitals": (
        {"events": {CHANNEL_LINE: CHANNEL_LINE + "    shipped_on: {kind: Date}\n"}},
        ROLLING,
        SHIPPED,
        "=",
    ),
    # Not temporal by kind or by a dimension on its column: by the relationship alone.
    "categorical-related-to-the-clock": (
        {
            "graph": {GRAPH_ENTITIES: GRAPH_ENTITIES + SNAPSHOT_LABEL_ENTITY},
            "snapshot_labels": SNAPSHOT_LABELS.replace(
                OBSERVED_DATE_LINE, "    observed_date: {column: date_day, kind: categorical}\n"
            ),
        },
        PRIOR_DAY,
        LINKED_DATE,
        ">=",
    ),
    "kindless-related-to-the-clock": (
        {
            "graph": {GRAPH_ENTITIES: GRAPH_ENTITIES + SNAPSHOT_LABEL_ENTITY},
            "snapshot_labels": SNAPSHOT_LABELS.replace(
                OBSERVED_DATE_LINE, "    observed_date: {column: date_day}\n"
            ),
        },
        PRIOR_DAY,
        LINKED_DATE,
        ">=",
    ),
    "categorical-relating-the-clock": (DAY_INFO, PRIOR_DAY, "dimension.fees_day_info_label", ">="),
}


@pytest.mark.parametrize(
    ("edits", "expression", "field", "op"), list(BY_TYPE.values()), ids=list(BY_TYPE)
)
def test_a_cut_of_a_temporal_dimension_refuses_by_type(
    tmp_path: Path, edits: dict, expression: dict, field: str, op: str
) -> None:
    runtime = _edited_runtime(tmp_path, edits)
    cut = _cut(field, op, "2026-09-15")
    try:
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(_query(expression, [cut], grouped=expression is PRIOR_DAY))
    finally:
        runtime.close()
    assert caught.value.code == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert caught.value.details["where_path"] == "where[0]"
    assert caught.value.details["where"] == cut


def test_an_upper_bound_on_another_date_matches_reference(tmp_path: Path) -> None:
    runtime = _edited_runtime(
        tmp_path, {"events": {CHANNEL_LINE: CHANNEL_LINE + "    shipped_on: {kind: date}\n"}}
    )
    try:
        rows = _rows(runtime, _query(ROLLING, [_cut(SHIPPED, "<=", "2026-09-11")]))
    finally:
        runtime.close()
    reference = ROLLING_REFERENCE.format(cut="occurred_at::date + 1 <= date '2026-09-11'")
    assert rows == _reference(reference)
