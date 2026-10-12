"""A named window states its window, clock and scope, and a balance answers its closing day.

The rows themselves are frozen in ``shop/tests/answers.yml`` (the quiet week, the empty
middle month, the days after the last order, the ratio with no denominator, the balance's
closing day); these tests check what the answer says about them.
"""

from dataclasses import replace

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.dialects import DuckDbDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

from .answer_ledger import load_entries
from .conftest import SESSION_ZONE, _rows, _runtime, _write_variant

ENTRIES = dict(load_entries())

ORDERS = "measure.shop.order_count"
SEATS = "measure.shop.seats"
ORDER_CLOCK = "temporal_role.shop_order_ordered_at"
SEAT_CLOCK = "temporal_role.shop_account_day_snapshot_day"
SEAT_DAY = "dimension.shop_account_day_snapshot_day"


@pytest.fixture
def shop(duckdb_backend) -> Runtime:
    return duckdb_backend.runtimes["utc_authored"]


def test_days_after_the_last_order_say_no_data_yet(shop: Runtime) -> None:
    call = {"select": [ORDERS], "time": {"this": "month", "grain": "day"}}
    result = shop.query(call, now="2024-08-03T12:00:00Z")
    assert [(r["period"], r["period_partial"], r["order_count"]) for r in result["rows"]] == [
        ("2024-08-01", False, 1),
        ("2024-08-02", False, None),
        ("2024-08-03", True, None),
    ]
    assert result["window"] == {
        "start": "2024-08-01",
        "end": "2024-08-03T12:00:00Z",
        "now": "2024-08-03T12:00:00Z",
        "partial": True,
        "data_through": "2024-08-01",
    }
    assert result["clock"] == ORDER_CLOCK
    assert result["observation_scope"] == "dataset"
    assert {"code": "PARTIAL_PERIOD", "message": "August 2024 so far (3 of 31 days)"} in result[
        "assumptions"
    ]
    (warning,) = [w for w in result["warnings"] if w["code"] == "NO_DATA_YET"]
    assert warning["details"]["measures"] == [
        {"id": ORDERS, "edge": "2024-08-01", "edge_source": "last_row"}
    ]
    assert warning["message"] == (
        f"No data yet for {ORDERS}: its last row is dated 2024-08-01. Nothing says this data is "
        "loaded past it, so later days read NULL, not 0. If the load ran and nothing happened, "
        'declare zero_through (PACKAGE_AUTHORING.md, "Empty periods").'
    )
    # Only August 2 is a whole day missing before today, and it is a period of its own.
    assert not [w for w in result["warnings"] if w["code"] == "PARTIAL_DATA"]
    # The output columns list the flag right after the period.
    fields = [column["field"] for column in result["output_columns"]]
    assert fields[:2] == ["period", "period_partial"]


def test_a_quiet_week_inside_the_data_reads_zero_without_a_warning(shop: Runtime) -> None:
    call = {"select": [ORDERS], "time": {"last": {"unit": "week", "value": 1}}}
    result = shop.query(call, now="2024-05-10T00:00:00Z")
    assert result["rows"] == [{"order_count": 0}]
    assert result["window"]["data_through"] == "2024-05-06"
    assert result["window"]["partial"] is False
    assert not [w for w in result.get("warnings", []) if w["code"].startswith("NO_DATA")]


def test_validate_and_compile_state_the_window_without_running(shop: Runtime) -> None:
    call = {"select": [ORDERS], "time": {"last": {"unit": "week", "value": 1}}}
    for answer in (
        shop.validate(call, now="2024-05-10T00:00:00Z"),
        shop.compile(call, now="2024-05-10T00:00:00Z"),
    ):
        assert answer["window"] == {
            "start": "2024-04-29",
            "end": "2024-05-06",
            "now": "2024-05-10T00:00:00Z",
            "partial": False,
        }
        assert answer["clock"] == ORDER_CLOCK
        assert answer["observation_scope"] == "dataset"


def test_an_answer_without_time_still_names_its_clock_and_scope(shop: Runtime) -> None:
    result = shop.query({"select": [ORDERS]})
    assert "window" not in result
    assert result["clock"] == ORDER_CLOCK
    assert result["observation_scope"] == "dataset"


def test_a_balance_right_now_is_its_last_closing_day(shop: Runtime) -> None:
    """'Right now' on a balance is the last complete day: account 1 and 3 on January 15."""
    call = {"select": [SEATS], "time": {"last": {"unit": "day", "value": 1}, "grain": "day"}}
    result = shop.query(call, now="2024-01-16T09:00:00Z")
    assert result["rows"] == [{"period": "2024-01-15", "period_partial": False, "seats": 220}]
    assert result["clock"] == SEAT_CLOCK


def _no_data_yet(result: dict) -> list[dict]:
    return [
        w["details"]["measures"] for w in result.get("warnings", []) if w["code"] == "NO_DATA_YET"
    ]


def test_a_balance_after_its_last_snapshot_says_no_data_yet(shop: Runtime) -> None:
    """Seats are snapshotted through January 15 (rows frozen in the ledger): the months after
    read NULL, said as a flow measure says it."""
    call = {"select": [SEATS], "time": {"start": "2024-01-01", "end": "2024-04-01", "grain": "month"}}  # fmt: skip
    result = shop.query(call, now="2024-03-10T12:00:00Z")
    assert result["window"]["data_through"] == "2024-01-15"
    (warning,) = [w for w in result.get("warnings", []) if w["code"] == "NO_DATA_YET"]
    assert warning["details"]["outputs"] == ["seats"]
    assert warning["details"]["measures"] == [
        {"id": SEATS, "edge": "2024-01-15", "edge_source": "last_row"}
    ]
    # A window total after the data reads NULL; one that reaches the edge reads its closing day.
    after = {"select": [SEATS], "time": {"last": {"unit": "week", "value": 1}}}
    result = shop.query(after, now="2024-03-10T12:00:00Z")
    assert result["rows"] == [{"seats": None}]
    assert _no_data_yet(result) == [
        [{"id": SEATS, "edge": "2024-01-15", "edge_source": "last_row"}]
    ]
    straddle = {"select": [SEATS], "time": {"start": "2024-01-10", "end": "2024-02-10"}}
    result = shop.query(straddle, now="2024-03-10T12:00:00Z")
    assert result["rows"] == [{"seats": 220}]
    assert result["window"]["data_through"] == "2024-01-15"
    assert _no_data_yet(result) == []


def test_a_balance_says_where_its_data_ends_where_only_a_named_window_reads_coverage(
    packages, monkeypatch
) -> None:
    """Snowflake, BigQuery, Databricks and Athena read coverage only for a named window's every
    period; there a balance still records the coverage that says where its data ends."""
    call = {"select": [SEATS], "time": {"start": "2024-01-01", "end": "2024-04-01", "grain": "month"}}  # fmt: skip
    now = "2024-03-10T12:00:00Z"
    base = _runtime(packages["utc_authored"])
    config = base.config
    base.close()
    for warehouse in ("snowflake", "bigquery", "databricks", "athena"):
        on = replace(config, package=replace(config.package, warehouse=warehouse))
        runtime = Runtime.from_config(on, source_path=str(packages["utc_authored"]))
        try:
            compiled = compile_query(on, Registry(on), runtime.engine_payload(call, now=now))
        finally:
            runtime.close()
        assert [edge["measures"] for edge in compiled["balance_edges"]] == [[SEATS]], warehouse
    # Run on DuckDB under the same rule: coverage read only for a named window.
    monkeypatch.setattr(DuckDbDialect, "has_time_coverage", property(lambda self: False))
    runtime = _runtime(packages["utc_authored"])
    try:
        result = runtime.query(call, now=now)
    finally:
        runtime.close()
    assert [row["seats"] for row in result["rows"]] == [220, None, None]
    assert result["window"]["data_through"] == "2024-01-15"
    assert _no_data_yet(result) == [
        [{"id": SEATS, "edge": "2024-01-15", "edge_source": "last_row"}]
    ]


@pytest.fixture
def per_day_balances(packages) -> Runtime:
    """The shop with a policy: seats are only read per snapshot day."""
    base = _runtime(packages["utc_authored"])
    config, source_path = base.config, base.source_path
    base.close()
    policy = SemanticPolicyConfig(
        id="policy.shop.seats_per_day",
        kind="metric_constraint",
        object_ids=[SEATS],
        config={"required_group_by": [SEAT_DAY]},
    )
    runtime = Runtime.from_config(
        replace(config, semantic_policies=[*config.semantic_policies, policy]),
        source_path=source_path,
    )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("time", "group_by", "allowed"),
    [
        ({"last": {"unit": "day", "value": 1}, "grain": "day"}, [], True),
        ({"start": "2024-01-01", "end": "2024-01-08", "grain": "day"}, [], True),
        ({}, [SEAT_DAY], True),
        ({"start": "2024-01-01", "end": "2024-01-22", "grain": "week"}, [], False),
        ({"start": "2024-01-01", "end": "2024-01-22"}, [], False),
    ],
    ids=["right-now", "daily-series", "grouped-by-the-day", "weekly", "window-total"],
)
def test_a_day_grain_meets_a_required_snapshot_day(per_day_balances, time, group_by, allowed):
    """The clock's own date is the period at day grain; a coarser grain is not that day."""
    call = {"select": [SEATS], "group_by": group_by}
    if time:
        call["time"] = {"temporal_role": SEAT_CLOCK, **time}
    if allowed:
        assert per_day_balances.query(call, now="2024-01-16T09:00:00Z")["rows"]
        return
    with pytest.raises(SemanticLayerError) as refused:
        per_day_balances.query(call, now="2024-01-16T09:00:00Z")
    assert refused.value.code == "POLICY_DENIED"


@pytest.mark.parametrize(
    ("window", "path"),
    [
        ({"last": {"unit": "week", "value": 2}}, "time.last"),
        ({"this": "month"}, "time.this"),
        ({"start": "2024-04-01", "end": "2024-05-06"}, "time.start"),
    ],
)
def test_a_comparison_beside_a_named_window_asks_for_the_series(shop: Runtime, window, path):
    prior = {
        "kind": "prior_period",
        "input": "measure.shop.revenue",
        "offset": {"unit": "week", "value": 1},
    }
    call = {
        "select": [{"expression": prior, "as": "week_before"}],
        "time": {"temporal_role": ORDER_CLOCK, "grain": "week", **window},
    }
    (error,) = shop.validate(call, now="2024-05-10T00:00:00Z")["errors"]
    assert error["code"] == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert error["details"]["path"] == path
    assert "comparison beside a named window is not supported yet" in error["message"]
    (hint,) = error["recovery_hints"]
    assert hint["kind"] == "ask_for_a_series" and "time.start" not in hint["message"]
    series = {**call, "time": hint["patch"]["replace"]["time"]}
    assert series["time"] == {"temporal_role": ORDER_CLOCK, "grain": "week"}
    assert shop.validate(series, now="2024-05-10T00:00:00Z")["ok"] is True


ORDER_TIME = "dimension.shop_order_ordered_at"


def test_a_window_grouped_by_the_order_time_keeps_only_its_observed_rows(shop: Runtime) -> None:
    # Rows are frozen in the ledger; here the answer says why the empty months are absent.
    call = {
        "select": [ORDERS],
        "group_by": [ORDER_TIME],
        "time": {"start": "2024-01-01", "end": "2024-04-01", "grain": "month"},
    }
    result = shop.query(call, now="2024-07-05T06:00:00Z")
    assert result["row_count"] == 3
    (held,) = [a for a in result["assumptions"] if a["code"] == "OBSERVED_PERIODS_ONLY"]
    assert held["group_by"] == [ORDER_TIME]
    # Grouped by a dimension that does not move with time, every month is still answered.
    by_status = {**call, "group_by": ["dimension.shop_customer_channel"]}
    answer = shop.query(by_status, now="2024-07-05T06:00:00Z")
    assert not [a for a in answer.get("assumptions", []) if a["code"] == "OBSERVED_PERIODS_ONLY"]
    assert {row["period"] for row in answer["rows"]} == {"2024-01-01", "2024-02-01", "2024-03-01"}


def test_filling_a_series_grouped_by_the_order_time_is_refused(shop: Runtime) -> None:
    call = {"select": [ORDERS], "group_by": [ORDER_TIME], "time": {"grain": "month", "fill": True}}
    (error,) = shop.validate(call, now="2024-07-05T06:00:00Z")["errors"]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "time.fill"


def test_a_window_with_only_a_start_runs_to_now(shop: Runtime) -> None:
    # Order 11 (2024-08-01) is after now: the window stops at the request instant.
    call = {"select": [ORDERS], "time": {"start": "2024-07-01"}}
    result = shop.query(call, now="2024-07-15T00:00:00Z")
    assert result["rows"] == [{"order_count": 1}]
    assert result["window"]["end"] == "2024-07-15T00:00:00Z"
    assert result["window"]["partial"] is True
    assert result["normalized_query"]["time"] == {
        "temporal_role": ORDER_CLOCK,
        "start": "2024-07-01",
    }
    # Still an open window: the months it answers are the ones with rows, up to now.
    series = {**call, "time": {"start": "2024-06-01", "grain": "month"}}
    rows = shop.query(series, now="2024-07-15T00:00:00Z")["rows"]
    assert [(r["period"], r["period_partial"], r["order_count"]) for r in rows] == [
        ("2024-06-01", False, 1),
        ("2024-07-01", True, 1),
    ]


def test_this_keeps_the_request_instant_to_the_microsecond(shop: Runtime) -> None:
    # Order 10 is at 03:30:00, half a second before now.
    call = {"select": [ORDERS], "time": {"this": "month", "grain": "day"}}
    result = shop.query(call, now="2024-07-01T03:30:00.500000Z")
    assert result["rows"] == [{"period": "2024-07-01", "period_partial": True, "order_count": 1}]
    assert result["window"]["end"] == "2024-07-01T03:30:00.500000Z"


@pytest.fixture(params=["converted", "aware"])
def fall_back(tmp_path, request) -> Runtime:
    """A New York order clock with two orders in the hour New York repeats on 2024-11-03:
    05:15 UTC (01:15 EDT) and 06:15 UTC (01:15 EST). The clock converts the stored UTC
    timestamp, or reads the zone-aware column in New York."""
    package = _write_variant(
        tmp_path, "ny_authored" if request.param == "converted" else "tz_authored"
    )
    if request.param == "aware":
        orders = package / "models" / "orders.yml"
        model = yaml.safe_load(orders.read_text(encoding="utf-8"))
        model["model"]["times"]["ordered_at"]["timezone"] = "America/New_York"
        orders.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    seed = package / "data" / "seed.sql"
    last = "(11, 108, 'a', TIMESTAMP '2024-08-01 02:00:00', DATE '2024-08-01', 2.00)"
    text = seed.read_text(encoding="utf-8")
    assert text.count(last) == 1
    seed.write_text(
        text.replace(
            last,
            last
            + ",\n  (12, 101, 'a', TIMESTAMP '2024-11-03 05:15:00', DATE '2024-11-03', 1.00)"
            + ",\n  (13, 101, 'a', TIMESTAMP '2024-11-03 06:15:00', DATE '2024-11-03', 1.00)",
        ),
        encoding="utf-8",
    )
    runtime = _runtime(package)
    try:
        _rows(runtime, f"SET GLOBAL TimeZone = '{SESSION_ZONE}'")
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize("now", ["2024-11-03T05:30:00Z", "2024-11-03T06:30:00Z"])
@pytest.mark.parametrize(
    "time",
    [{"this": "day", "grain": "day"}, {"this": "day"}, {"start": "2024-11-03", "grain": "day"}],
)
def test_now_cuts_at_its_instant_in_the_repeated_hour(fall_back: Runtime, now: str, time) -> None:
    # Both orders read 01:15 in New York; only those before now in UTC count.
    reference = _rows(
        fall_back,
        "SELECT COUNT(order_id) FROM orders WHERE ordered_at >= TIMESTAMP '2024-11-03 04:00:00' "
        f"AND ordered_at < TIMESTAMP '{now[:-1].replace('T', ' ')}'",
    )
    rows = fall_back.query({"select": [ORDERS], "time": time}, now=now)["rows"]
    assert [row["order_count"] for row in rows] == [reference[0][0]]
    assert reference[0][0] == (1 if now.startswith("2024-11-03T05") else 2)


def _clock_in(tmp_path, variant: str, zone: str, row: str = ""):
    """The order clock read in ``zone`` (no column_timezone), with one more order at ``row``."""
    package = _write_variant(tmp_path, variant)
    orders = package / "models" / "orders.yml"
    model = yaml.safe_load(orders.read_text(encoding="utf-8"))
    model["model"]["times"]["ordered_at"]["timezone"] = zone
    orders.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    if row:
        seed = package / "data" / "seed.sql"
        last = "(11, 108, 'a', TIMESTAMP '2024-08-01 02:00:00', DATE '2024-08-01', 2.00)"
        text = seed.read_text(encoding="utf-8")
        assert text.count(last) == 1
        extra = f",\n  (12, 101, 'a', TIMESTAMP '{row}', DATE '{row[:10]}', 1.00)"
        seed.write_text(text.replace(last, last + extra), encoding="utf-8")
    return package


@pytest.mark.parametrize(
    ("zone", "wall", "now"),
    [
        # 01:15 and now (01:30 EDT) both fall in the hour New York repeats.
        ("America/New_York", "2024-11-03 01:15:00", "2024-11-03T05:30:00Z"),
        # 02:15 and now (02:30 CEST) both fall in the hour Berlin repeats.
        ("Europe/Berlin", "2024-10-27 02:15:00", "2024-10-27T00:30:00Z"),
    ],
)
@pytest.mark.parametrize("shape", ["this", "this_by_day", "start_by_day"])
def test_a_plain_timestamp_keeps_its_wall_time_in_the_repeated_hour(
    tmp_path, zone, wall, now, shape
) -> None:
    # A plain timestamp is wall time in the clock's zone: 01:15 is before 01:30.
    day = wall[:10]
    time = {
        "this": {"this": "day"},
        "this_by_day": {"this": "day", "grain": "day"},
        "start_by_day": {"start": day, "grain": "day"},
    }[shape]
    runtime = _runtime(_clock_in(tmp_path, "utc_authored", zone, wall))
    try:
        _rows(runtime, f"SET GLOBAL TimeZone = '{SESSION_ZONE}'")
        cutoff = wall[:11] + wall[11:13] + ":30:00"
        reference = _rows(
            runtime,
            f"SELECT COUNT(order_id) FROM orders WHERE ordered_at >= TIMESTAMP '{day}' "
            f"AND ordered_at < TIMESTAMP '{cutoff}'",
        )
        rows = runtime.query({"select": [ORDERS], "time": time}, now=now)["rows"]
    finally:
        runtime.close()
    assert reference == [(1,)]
    assert [row["order_count"] for row in rows] == [1]


NOT_IN_THE_CLOCKS_ZONE = ["snowflake", "bigquery", "clickhouse", "databricks", "athena"]


@pytest.mark.parametrize("warehouse", NOT_IN_THE_CLOCKS_ZONE)
@pytest.mark.parametrize(
    ("time", "path"),
    [({"this": "day", "grain": "day"}, "time.this"), ({"start": "2024-07-01"}, "time.start")],
)
def test_a_window_to_now_is_refused_where_plain_and_aware_columns_cut_differently(
    tmp_path, warehouse, time, path
) -> None:
    # The query doesn't run in the clock's zone, so its wall-time cutoff would read a
    # zone-aware column as another zone's instant.
    runtime = _runtime(_clock_in(tmp_path, "tz_authored", "America/New_York"), warehouse=warehouse)
    try:
        now = "2024-07-01T12:00:00Z"
        (error,) = runtime.validate({"select": [ORDERS], "time": time}, now=now)["errors"]
        assert (error["code"], error["details"]["path"]) == ("INVALID_QUERY", path)
        (hint,) = error["recovery_hints"]
        assert "column_timezone" in hint["message"] and "dated window" in hint["message"]
        # A dated window reads as it did.
        dated = {"start": "2024-07-01", "end": "2024-07-02", "grain": "day"}
        assert runtime.validate({"select": [ORDERS], "time": dated}, now=now)["ok"] is True
    finally:
        runtime.close()


@pytest.mark.parametrize("warehouse", NOT_IN_THE_CLOCKS_ZONE)
def test_a_declared_storage_zone_cuts_at_now_there(tmp_path, warehouse) -> None:
    package = _clock_in(tmp_path, "utc_authored", "America/New_York")
    orders = package / "models" / "orders.yml"
    model = yaml.safe_load(orders.read_text(encoding="utf-8"))
    model["model"]["times"]["ordered_at"]["column_timezone"] = "UTC"
    orders.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    runtime = _runtime(package, warehouse=warehouse)
    try:
        call = {"select": [ORDERS], "time": {"start": "2024-07-01", "grain": "day"}}
        sql = runtime.compile(call, now="2024-07-01T12:00:00Z")["rendered_sql"]
    finally:
        runtime.close()
    # The stored UTC value is compared with now in UTC.
    assert "orders.ordered_at < " in sql and "2024-07-01" in sql
    assert "12:00:00" in sql.split("orders.ordered_at < ", 1)[1].splitlines()[0]


def test_a_named_window_answers_the_same_with_its_probe_read_through_ctes(packages, monkeypatch):
    # Snowflake and Spark can't put EXISTS in a select, so their guard reads the
    # outside-the-window probe through plain CTEs. On DuckDB that form answers every frozen
    # named-window series the same.
    from semantic_rails import package_tools
    from semantic_rails.dialects import DuckDbDialect

    monkeypatch.setattr(DuckDbDialect, "exists_in_select", False)
    cases = [
        (case_id, spec)
        for case_id, spec in load_entries()
        if spec["expect"] == "answer"
        and (time := spec["query"].get("time") or {}).get("grain")
        and (time.get("last") or time.get("this") or ("start" in time and "end" in time))
    ]
    assert cases
    runtimes: dict[str, Runtime] = {}
    probed = 0
    try:
        for case_id, spec in cases:
            variant = spec.get("variant", "utc_authored")
            runtime = runtimes.setdefault(variant, _runtime(packages[variant]))
            report = package_tools._run_test(runtime, case_id, spec)
            assert report["ok"], report
            sql = runtime.compile(spec["query"], now=spec.get("now"))["rendered_sql"]
            probed += "observed_1_rows" in sql and "EXISTS" not in sql
    finally:
        for runtime in runtimes.values():
            runtime.close()
    assert probed


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_window_missing_whole_days_after_the_data_says_partial_data(request, backend) -> None:
    """Rows frozen in the ledger; here the answer says which days its total covers."""
    runtime = request.getfixturevalue(f"{backend}_backend").runtimes["utc_authored"]
    spec = ENTRIES["shop/last-week-before-its-last-days-loaded-keeps-its-total"]
    result = runtime.query(spec["query"], now=spec["now"])
    assert result["window"]["partial"] is False
    assert _partial_data(result) == [
        {
            "code": "PARTIAL_DATA",
            "message": "Data runs through 2024-08-01, so this total covers 4 of the window's 7 days.",
            "severity": "warning",
            "stage": "execution",
            "details": {"through": "2024-08-01", "days_covered": 4, "days": 7},
        }
    ]
    # A series names the period that misses the days; the complete week before says nothing.
    series = {**spec["query"], "time": {"last": {"unit": "week", "value": 2}, "grain": "week"}}
    result = runtime.query(series, now=spec["now"])
    assert [w["message"] for w in _partial_data(result)] == [
        "Data runs through 2024-08-01, so the week of 2024-07-29 covers 4 of its 7 days."
    ]
    assert _partial_data(result)[0]["details"]["period"] == "2024-07-29"
    # A nightly load leaves only today unfinished: nothing to say.
    spec = ENTRIES["shop/a-week-so-far-after-a-nightly-load-keeps-its-total"]
    assert _partial_data(runtime.query(spec["query"], now=spec["now"])) == []
    # A window wholly after the data is NO_DATA_YET's, never PARTIAL_DATA's.
    result = runtime.query(spec["query"], now="2024-08-20T06:00:00Z")
    assert _partial_data(result) == []
    assert _no_data_yet(result)


def _partial_data(result: dict) -> list[dict]:
    return [w for w in result.get("warnings", []) if w["code"] == "PARTIAL_DATA"]
