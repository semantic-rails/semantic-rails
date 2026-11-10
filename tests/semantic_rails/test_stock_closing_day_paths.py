"""Every path that reads a balance reads the same snapshots and says so.

One balance, ``measure.x.balance`` on the as-of clock ``ts`` (key ``[id, ts]``): X has a
snapshot on January 5 (100) and Y on January 20 (200), so the closing day, January 20, holds Y
only. An entity-set ratio, a balance read inside an entity filter or a metric predicate, two
stocks read side by side and a metric granted without its measure each read that day and
disclose it. The clock's day is its own zone's, and a row with no clock is never a series'
latest snapshot. Each path runs both lowerings (an aggregate joined back, and QUALIFY).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.compiler_parts.bind import _parse_public_expr
from semantic_rails.dialects import DuckDbDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig, SemanticPolicyConfig
from tests.semantic_rails.conftest import opened

CLOSING = "BALANCE_CLOSING_DAY"
MISSING = "BALANCE_SERIES_MISSING_ON_CLOSING_DAY"
BALANCE = "measure.x.balance"
ROWS = "('X', TIMESTAMP '2020-01-05', 100), ('Y', TIMESTAMP '2020-01-20', 200)"
FEE = {"kind": "scoped_aggregate", "measure": BALANCE, "aggregation": "sum"}
OVER_50 = {"entity": "entity.x_account", "input": BALANCE, "op": "gt", "value": 50}


def _package(
    root: Path,
    rows: str = ROWS,
    clock: str = "",
    *,
    relation: str = "snap",
    ts: str = "TIMESTAMP",
    extra: str = "",
) -> Path:
    (root / "models").mkdir(parents=True)
    (root / "metrics").mkdir()
    (root / "package.yml").write_text(
        "schema_version: 1\n"
        "package: {id: x, namespace: x, name: x, warehouse: duckdb, default_db: x.duckdb,\n"
        "  seed: {kind: external}, environments: [development]}\n"
    )
    (root / "graph.yml").write_text(
        "graph:\n  entities:\n"
        "    snap: {key: [id, ts], model: snap}\n"
        "    account: {key: [id], model: accounts}\n"
    )
    stock = "accumulation: {kind: stock, snapshot: end_of_period}, value_type: number"
    (root / "models" / "snap.yml").write_text(
        f"model:\n  id: snap\n  relation: {relation}\n  entities: {{snap: {{}}, account: {{}}}}\n"
        "  dimensions: {id: {kind: categorical}}\n"
        f"  times:\n    ts: {{column: ts, kind: {'date' if ts == 'DATE' else 'timestamp'}, "
        f"class: as_of_time, default: true{clock}}}\n"
        "  measures:\n"
        f"    balance: {{kind: aggregate, expr: value, {stock}}}\n"
        f'    balance2: {{kind: aggregate, expr: "value * 2", {stock}}}\n'
        "    flow: {kind: aggregate, expr: value, default_agg: sum,\n"
        "      accumulation: {kind: flow}, value_type: number}\n" + extra
    )
    (root / "models" / "accounts.yml").write_text(
        "model:\n  id: accounts\n  relation: accounts\n  entities: {account: {}}\n"
    )
    (root / "metrics" / "metrics.yml").write_text(
        "metrics:\n  report: {kind: aggregate, measure: balance, value_type: number}\n"
    )
    with duckdb.connect(str(root / "x.duckdb")) as con:
        con.execute(f"CREATE TABLE {relation} (id VARCHAR, ts {ts}, value INTEGER); "
                    f"INSERT INTO {relation} VALUES {rows}; "
                    f"CREATE TABLE accounts AS SELECT DISTINCT id FROM {relation}")  # fmt: skip
    return root


@pytest.fixture(params=["aggregate_join", "qualify"])
def lowering(request, monkeypatch) -> str:
    # DuckDB lowers the snapshot choice as an aggregate joined back; warehouses with QUALIFY
    # (Snowflake, BigQuery, Databricks) use ROW_NUMBER. DuckDB runs both.
    if request.param == "qualify":
        capabilities = DuckDbDialect.capabilities
        monkeypatch.setattr(
            DuckDbDialect, "capabilities", lambda self: {**capabilities(self), "qualify": True}
        )
    return request.param


def _runtime(root: Path) -> Runtime:
    return opened(Runtime.from_path(str(root)))


@pytest.fixture
def x(lowering, tmp_path: Path) -> Iterator[Runtime]:
    runtime = _runtime(_package(tmp_path / "x"))
    yield runtime
    runtime.close()


def _values(result: dict, alias: str = "v") -> list[Any]:
    return [None if row[alias] is None else float(row[alias]) for row in result["rows"]]


def _by_code(result: dict, key: str, code: str) -> list[dict]:
    return [row for row in result.get(key) or [] if row["code"] == code]


def _disclosed(result: dict) -> list[tuple[str, str, int]]:
    """Each closing-day read: its measure, day, and how many series it left out."""
    missing = {
        (row["details"]["measure"], row["details"]["closing_day"]): row["details"]["series_missing"]
        for row in _by_code(result, "warnings", MISSING)
    }
    return sorted(
        (row["measure"], row["closing_day"], missing.get((row["measure"], row["closing_day"]), 0))
        for row in _by_code(result, "assumptions", CLOSING)
    )


def _refused(runtime: Runtime, call: dict, code: str, reason: str) -> None:
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(call)
    assert (caught.value.code, caught.value.details.get("reason")) == (code, reason)


def _share_refused(runtime: Runtime, ratio: dict) -> None:
    """A share of a balance that closes on a day, filtered by entity sets: refused, never a
    number its operands read alone wouldn't give."""
    call = {"select": [{"expression": ratio, "as": "v"}]}
    _refused(runtime, call, "REWRITE_NOT_SUPPORTED", "entity_set_ratio_closing_day")


def test_an_entity_set_ratio_of_a_balance_is_refused(x: Runtime) -> None:
    """The share of balances held by accounts over 50 is refused; its part and whole answer
    on their own."""
    part = {**FEE, "predicates": [{**OVER_50, "over": "all_time"}]}
    _share_refused(x, {"kind": "ratio", "numerator": part, "denominator": FEE})
    assert _values(x.query({"select": [{"expression": part, "as": "v"}]})) == [200.0]


def test_a_balance_read_inside_an_entity_filter_is_disclosed(x: Runtime) -> None:
    """The flow of accounts whose balance is over 50: X's balance has no closing-day snapshot,
    so only Y's 200 counts, and the answer says which day decided it."""
    where = [{"entity": "entity.x_account", "having": [{"field": BALANCE, "op": "gt", "value": 50}],
              "over": "all_time"}]  # fmt: skip
    result = x.query({"select": [{"expression": "measure.x.flow", "as": "v"}], "where": where})
    assert _values(result) == [200.0]
    assert _disclosed(result) == [(BALANCE, "2020-01-20", 1)]
    # A metric predicate reads it the same way.
    flow = {"kind": "scoped_aggregate", "measure": "measure.x.flow", "aggregation": "sum",
            "predicates": [{**OVER_50, "over": "all_time"}]}  # fmt: skip
    result = x.query({"select": [{"expression": flow, "as": "v"}]})
    assert _values(result) == [200.0]
    assert _disclosed(result) == [(BALANCE, "2020-01-20", 1)]


def test_each_stock_read_side_by_side_is_disclosed(x: Runtime) -> None:
    call = {"select": [{"expression": BALANCE, "as": "v"},
                       {"expression": "measure.x.balance2", "as": "w"},
                       {"expression": "measure.x.flow", "as": "f"}]}  # fmt: skip
    result = x.query(call)
    assert (_values(result), _values(result, "w"), _values(result, "f")) == ([200], [400], [300])
    assert _disclosed(result) == [
        (BALANCE, "2020-01-20", 1),
        ("measure.x.balance2", "2020-01-20", 1),
    ]
    # Each names the output that reads it.
    outputs = {row["measure"]: row["outputs"] for row in _by_code(result, "assumptions", CLOSING)}
    assert outputs == {BALANCE: ["v"], "measure.x.balance2": ["w"]}


def test_a_granted_metric_keeps_the_day_and_counts_without_its_measure(x: Runtime) -> None:
    """Granted only metric.x.report: the answer keeps the closing day and the count, names the
    metric, and never the measure behind it."""
    grant = RequestContext(actor="subject", roles=("analyst",), audience="finance",
                           metric_allowlist=("metric.x.report",), dimension_allowlist=())  # fmt: skip
    result = x.query({"select": [{"expression": "metric.x.report", "as": "v"}]}, context=grant)
    assert _values(result) == [200.0]
    (assumption,) = _by_code(result, "assumptions", CLOSING)
    (warning,) = _by_code(result, "warnings", MISSING)
    assert (assumption["closing_day"], warning["details"]["series_missing"]) == ("2020-01-20", 1)
    assert BALANCE not in str([assumption, warning])
    assert "metric.x.report" in assumption["message"] and "metric.x.report" in warning["message"]
    assert warning["object_ids"] == ["metric.x.report"]


def test_a_row_without_a_clock_is_never_a_latest_snapshot(lowering, tmp_path: Path) -> None:
    """A NULL clock sorts first in a descending order on Postgres and Snowflake: such a row
    must neither displace Y's January 20 snapshot nor hide that X has none that day."""
    rows = f"{ROWS}, ('X', NULL, 999), ('Y', NULL, 5)"
    runtime = _runtime(_package(tmp_path / "x", rows))
    try:
        # Postgres and Snowflake treat NULL as the largest value.
        runtime._get_adapter()._db.conn.execute(
            "SET GLOBAL default_null_order = 'nulls_last_on_asc_first_on_desc'"
        )
        result = runtime.query({"select": [{"expression": BALANCE, "as": "v"}]})
        assert _values(result) == [200.0]
        assert _disclosed(result) == [(BALANCE, "2020-01-20", 1)]
    finally:
        runtime.close()


# Round 3: each operand's own scope, the clock's day bucket, ownership, names, duplicates.
LA = ", timezone: America/Los_Angeles"


def test_an_entity_set_ratio_closes_within_each_operands_scope(x: Runtime) -> None:
    """Both operands keep only accounts whose flow is 100 (X): each closes on X's January 5
    and reads 100. Their share is refused, never read on another scope's closing day."""
    is_100 = {"entity": "entity.x_account", "input": "measure.x.flow", "op": "is", "value": 100,
              "over": "all_time"}  # fmt: skip
    over_50 = {**is_100, "op": "gt", "value": 50}
    for operand in ({**FEE, "predicates": [is_100]}, {**FEE, "predicates": [is_100, over_50]}):
        assert _values(x.query({"select": [{"expression": operand, "as": "v"}]})) == [100.0]
    ratio = {"kind": "ratio", "numerator": {**FEE, "predicates": [is_100, over_50]},
             "denominator": {**FEE, "predicates": [is_100]}}  # fmt: skip
    _share_refused(x, ratio)


@pytest.mark.parametrize(
    "time",
    [
        {},
        {"start": "2020-03-08", "end": "2020-03-09"},
        {"start": "2020-03-08", "end": "2020-03-10", "grain": "day"},
    ],
    ids=["untimed", "window", "by_day"],
)
@pytest.mark.parametrize(
    ("ts", "zone", "rows"),
    [
        # Naive UTC wall times, declared as such.
        ("TIMESTAMP", ", column_timezone: UTC",
         "('X', TIMESTAMP '2020-03-09 06:30', 100), ('Y', TIMESTAMP '2020-03-08 09:30', 200)"),
        # Instants, read in the session's zone.
        ("TIMESTAMPTZ", "",
         "('X', TIMESTAMPTZ '2020-03-09 06:30:00+00', 100), ('Y', TIMESTAMPTZ '2020-03-08 09:30:00+00', 200)"),
    ],
    ids=["naive", "aware"],
)  # fmt: skip
def test_the_closing_day_is_the_clocks_day_bucket_across_dst(
    lowering, tmp_path: Path, time: dict, ts: str, zone: str, rows: str
) -> None:
    """March 8, 2020 is 23 hours long in Los Angeles: Y at 01:30 PST and X at 23:30 PDT are
    both that day, though X is March 9 in UTC. The closing day is the day the clock's own
    day grain puts each snapshot in, so both are read; untimed, the call runs in UTC and is
    refused."""
    runtime = _runtime(_package(tmp_path / "x", rows, LA + zone, ts=ts))
    try:
        call: dict = {"select": [{"expression": BALANCE, "as": "v"}]}
        if time:
            call["time"] = {"temporal_role": "temporal_role.x_snap_ts", **time}
        if not time:
            # Untimed, the call runs in UTC, not the clock's zone: refused.
            _refused(runtime, call, "MIXED_TIME_ROLES", "closing_day_zone")
            return
        result = runtime.query(call)
        assert [v for v in _values(result) if v is not None] == [300.0]
        assert _disclosed(result) == [(BALANCE, "2020-03-08", 0)]
    finally:
        runtime.close()


def _withheld_report(root: Path, lowering: str) -> Runtime:
    """report withheld from role support, beside an average flow per account (a branch)."""
    base = Runtime.from_path(str(_package(root)))
    config = base.config
    distribution = {"kind": "distribution", "function": "avg",
                    "over": {"kind": "entity_value", "entity": "entity.x_account",
                             "input": "measure.x.flow"}}  # fmt: skip
    payload = base.engine_payload({"select": [{"expression": distribution, "as": "d"}]})
    base.close()
    average = MetricConfig(
        id="metric.x.flow_average",
        kind="derived",
        expression=_parse_public_expr(payload["select"][0]["expression"]),
    )
    policy = SemanticPolicyConfig(id="policy.x.hide", kind="object_access", action="withhold_values",
                                  roles=["support"], object_ids=["metric.x.report"],
                                  config={"max_rank": 3})  # fmt: skip
    config = replace(config, metric_recipes=[*config.metric_recipes, average],
                     semantic_policies=[policy])  # fmt: skip
    return opened(Runtime.from_config(config, source_path=str(root)))


RANKED = {
    "select": [{"expression": "metric.x.report", "as": "v"},
               {"expression": "metric.x.flow_average", "as": "d"}],
    "group_by": ["dimension.x_snap_id"],
    "order_by": [{"field": "v", "direction": "desc"},
                 {"field": "dimension.x_snap_id", "direction": "desc"}],
    "limit": 3,
}  # fmt: skip


def test_a_withheld_stock_beside_a_branch_says_nothing_of_its_day(lowering, tmp_path: Path) -> None:
    """The balance is read for the withheld report only, in a branch beside a distribution:
    its closing day and counts are withheld with it, granted or not, and never attributed to
    the flow average or the grouping."""
    runtime = _withheld_report(tmp_path / "x", lowering)
    try:
        plain = runtime.query(RANKED)
        # Unrestricted, the branch's read belongs to the report's column.
        outputs = [row["outputs"] for row in _by_code(plain, "assumptions", CLOSING)]
        assert outputs == [["v"]]
        support = RequestContext(roles=("support",))
        granted = RequestContext(roles=("support",), metric_allowlist=("metric.x.report", "metric.x.flow_average"),
                                 dimension_allowlist=("dimension.x_snap_id",))  # fmt: skip
        for context in (support, granted):
            result = runtime.query(RANKED, context=context)
            assert "v" not in result["rows"][0]
            assert not _by_code(result, "assumptions", CLOSING)
            assert not _by_code(result, "warnings", MISSING)
            assert "2020-01-20" not in str([result.get("assumptions"), result.get("warnings")])
    finally:
        runtime.close()


@pytest.mark.parametrize("relation", ["snapshot_rows", "snapshot_base", "closing_rows"])
def test_a_relation_named_like_an_internal_step_still_reads(
    lowering, tmp_path: Path, relation: str
) -> None:
    runtime = _runtime(_package(tmp_path / "x", relation=relation))
    try:
        result = runtime.query({"select": [{"expression": BALANCE, "as": "v"}]})
        assert _values(result) == [200.0]
        assert _disclosed(result) == [(BALANCE, "2020-01-20", 1)]
    finally:
        runtime.close()


def test_duplicate_snapshots_on_the_closing_day_are_all_read(lowering, tmp_path: Path) -> None:
    """Two identical rows for X on its closing day are two rows of data: both lowerings read
    them both (100 + 100 + 200)."""
    rows = "('X', TIMESTAMP '2020-01-20', 100), ('X', TIMESTAMP '2020-01-20', 100), ('Y', TIMESTAMP '2020-01-20', 200)"  # fmt: skip
    runtime = _runtime(_package(tmp_path / "x", rows))
    try:
        assert _values(runtime.query({"select": [{"expression": BALANCE, "as": "v"}]})) == [400.0]
    finally:
        runtime.close()


def _with_hits(root: Path) -> Path:
    """Add hits by account on a UTC event clock."""
    (root / "models" / "hits.yml").write_text(
        "model:\n  id: hits\n  relation: hits\n  entities: {hit: {}, account: {}}\n  dimensions: {hit_id: {kind: categorical}}\n"
        "  times:\n    at: {column: at, kind: timestamp, class: event_time, default: true}\n"
        '  measures:\n    hits: {kind: aggregate, expr: "1", default_agg: sum,\n'
        "      accumulation: {kind: flow}, value_type: count}\n"
    )
    graph = (root / "graph.yml").read_text() + "    hit: {key: [hit_id], model: hits}\n"
    (root / "graph.yml").write_text(graph)
    with duckdb.connect(str(root / "x.duckdb")) as con:
        con.execute(
            "CREATE TABLE hits AS SELECT 1 AS hit_id, 'Y' AS id, TIMESTAMP '2020-01-21' AS at"
        )
    return root


HITS_OVER_50 = {
    "select": [{"expression": "measure.x.hits", "as": "v"}],
    "where": [{"entity": "entity.x_account", "having": [{"field": BALANCE, "op": "gt", "value": 50}],
               "over": "all_time"}],
}  # fmt: skip


def test_a_balance_closing_in_another_zone_than_the_query_is_refused(tmp_path: Path) -> None:
    """Accounts filtered by a Los Angeles balance on a timestamp clock, in a call that runs
    in UTC (a hits clock): a zone-aware clock would close on a UTC day, so it is refused."""
    runtime = _runtime(_with_hits(_package(tmp_path / "x", clock=LA)))
    try:
        where = [{"entity": "entity.x_account", "having": [{"field": BALANCE, "op": "gt", "value": 50}],
                  "over": "all_time"}]  # fmt: skip
        call = {"select": [{"expression": "measure.x.hits", "as": "v"}], "where": where}
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(call)
        assert caught.value.code == "MIXED_TIME_ROLES"
        assert caught.value.details["reason"] == "closing_day_zone"
        # Read on its own clock, the balance answers.
        own = {
            "temporal_role": "temporal_role.x_snap_ts",
            "start": "2020-01-01",
            "end": "2020-02-01",
        }
        assert _values(
            runtime.query({"select": [{"expression": BALANCE, "as": "v"}], "time": own})
        ) == [200.0]
    finally:
        runtime.close()


def test_a_date_clock_in_another_zone_closes_on_its_own_day(tmp_path: Path) -> None:
    """A DATE clock never depends on the session's zone: the same filter on a Los Angeles
    DATE balance runs in UTC and keeps Y (1 hit), closing January 20 with X missing."""
    rows = "('X', DATE '2020-01-05', 100), ('Y', DATE '2020-01-20', 200)"
    runtime = _runtime(_with_hits(_package(tmp_path / "x", rows, LA, ts="DATE")))
    try:
        result = runtime.query(HITS_OVER_50)
        assert _values(result) == [1.0]
        assert _disclosed(result) == [(BALANCE, "2020-01-20", 1)]
    finally:
        runtime.close()


LOCAL_HOUR = (
    "    local: {kind: aggregate, default_agg: sum, accumulation: {kind: flow}, "
    "value_type: number,\n      expr: {kind: case, whens: [{when: {kind: comparison, op: '<', "
    "left: {kind: call, name: DATE_PART, args: [{kind: literal, value: hour}, "
    "{kind: column, column: ts}]}, right: {kind: literal, value: 12}}, "
    "then: {kind: column, column: value}}], else: {kind: literal, value: 0}}}\n"
)


def test_an_untimed_call_without_a_balance_keeps_running_in_utc(tmp_path: Path) -> None:
    """No balance read, no time block: the call runs in UTC as it always has. 01:00Z is
    before noon there and 13:00Z after, so only 100 counts (in Los Angeles it would be 200)."""
    rows = "('X', TIMESTAMPTZ '2020-01-02 01:00:00+00', 100), ('Y', TIMESTAMPTZ '2020-01-02 13:00:00+00', 200)"  # fmt: skip
    runtime = _runtime(_package(tmp_path / "x", rows, LA, ts="TIMESTAMPTZ", extra=LOCAL_HOUR))
    try:
        assert _values(
            runtime.query({"select": [{"expression": "measure.x.local", "as": "v"}]})
        ) == [100.0]
    finally:
        runtime.close()


# Every statistic of a balance with no time window reads the closing day, as last_value does.
# Y's January 20 snapshot is the only one that day; W, Z and X last reported earlier.
SPREAD = (
    "('W', TIMESTAMP '2020-01-02', 350), ('Z', TIMESTAMP '2020-01-03', 300), "
    "('X', TIMESTAMP '2020-01-05', 100), ('Y', TIMESTAMP '2020-01-20', 200)"
)
STATISTICS = {
    "avg": "AVG(value)",
    "median": "MEDIAN(value)",
    "percentile": "QUANTILE_CONT(value, 0.9)",
    "min": "MIN(value)",
    "max": "MAX(value)",
    "sum": "SUM(value)",
    "last_value": "SUM(value)",
}


def _statistic(aggregation: str) -> dict:
    expression: dict = {"kind": "measure", "measure": BALANCE, "aggregation": aggregation}
    if aggregation == "percentile":
        expression["parameters"] = {"p": 0.9}
    return expression


@pytest.fixture
def spread(lowering, tmp_path: Path) -> Iterator[tuple[Runtime, Any]]:
    """The runtime, and hand SQL on its own copy of the rows."""
    runtime = _runtime(_package(tmp_path / "x", SPREAD))
    hand = duckdb.connect()
    hand.execute(f"CREATE TABLE snap (id VARCHAR, ts TIMESTAMP, value INTEGER); "
                 f"INSERT INTO snap VALUES {SPREAD}")  # fmt: skip
    yield runtime, hand
    runtime.close()
    hand.close()


@pytest.mark.parametrize("aggregation", list(STATISTICS))
def test_every_statistic_of_an_untimed_balance_reads_the_closing_day(
    spread: tuple[Runtime, Any], aggregation: str
) -> None:
    """With no time window, avg, median, percentile, min and max read the closing day's
    snapshots, as last_value and sum do: Y's 200. Each account's own last snapshot (W's from
    January 2 beside Y's) is never read; it gave 237.5, 250, 335, 100 and 350. The answer
    names the day and the three accounts it left out."""
    runtime, hand = spread
    statistic = STATISTICS[aggregation]
    ((closing, latest),) = hand.execute(
        f"SELECT (SELECT {statistic} FROM snap WHERE CAST(ts AS DATE) = "
        "(SELECT CAST(MAX(ts) AS DATE) FROM snap)), "
        f"(SELECT {statistic} FROM snap WHERE (id, ts) IN (SELECT id, MAX(ts) FROM snap GROUP BY id))"
    ).fetchall()
    assert closing != latest
    result = runtime.query({"select": [{"expression": _statistic(aggregation), "as": "v"}]})
    assert _values(result) == [float(closing)] == [200.0]
    assert _by_code(result, "assumptions", CLOSING) == [
        {
            "code": CLOSING,
            "message": f"{BALANCE} is read on its closing day, 2020-01-20, the last day with a "
            "snapshot: a series with no snapshot that day is not counted, never carried forward "
            "from an earlier day.",
            "measure": BALANCE,
            "outputs": ["v"],
            "nested": False,
            "closing_day": "2020-01-20",
            "periods": 1,
        }
    ]
    assert _disclosed(result) == [(BALANCE, "2020-01-20", 3)]


def test_a_statistic_of_a_balance_inside_a_predicate_or_a_ratio_reads_the_closing_day(
    spread: tuple[Runtime, Any],
) -> None:
    """Accounts whose average balance is over 150: only Y has a closing-day snapshot, so only
    Y's flow counts (200; on each account's last snapshot W and Z counted too: 850), and the
    answer says the read was nested. A ratio and a scoped aggregate of its statistics read
    that day too."""
    runtime, _ = spread
    flow = {"kind": "scoped_aggregate", "measure": "measure.x.flow", "aggregation": "sum",
            "predicates": [{**OVER_50, "input": _statistic("avg"), "value": 150,
                            "over": "all_time"}]}  # fmt: skip
    result = runtime.query({"select": [{"expression": flow, "as": "v"}]})
    assert _values(result) == [200.0]
    assert [
        (row["nested"], row["closing_day"]) for row in _by_code(result, "assumptions", CLOSING)
    ] == [(True, "2020-01-20")]
    assert _disclosed(result) == [(BALANCE, "2020-01-20", 3)]
    ratio = {"kind": "ratio", "numerator": _statistic("max"), "denominator": _statistic("min")}
    scoped = {"kind": "scoped_aggregate", "measure": BALANCE, "aggregation": "avg"}
    for expression, expected in ((ratio, 1.0), (scoped, 200.0)):
        result = runtime.query({"select": [{"expression": expression, "as": "v"}]})
        assert _values(result) == [expected]
        assert _disclosed(result) == [(BALANCE, "2020-01-20", 3)]


# Y reports again on February 10, the window's closing day; W, Z and X last reported in January.
WINDOWED = f"{SPREAD}, ('Y', TIMESTAMP '2020-02-10', 220)"
JAN_FEB = {"temporal_role": "temporal_role.x_snap_ts", "start": "2020-01-01", "end": "2020-03-01"}


@pytest.fixture
def windowed(lowering, tmp_path: Path) -> Iterator[tuple[Runtime, Any]]:
    """The runtime, with a governed average balance, and hand SQL on its own copy of the rows."""
    root = _package(tmp_path / "x", WINDOWED)
    with (root / "metrics" / "metrics.yml").open("a") as metrics:
        metrics.write("  avg_report: {kind: aggregate, measure: balance, aggregation: avg, "
                      "value_type: number}\n")  # fmt: skip
    runtime = _runtime(root)
    hand = duckdb.connect()
    hand.execute(f"CREATE TABLE snap (id VARCHAR, ts TIMESTAMP, value INTEGER); "
                 f"INSERT INTO snap VALUES {WINDOWED}")  # fmt: skip
    yield runtime, hand
    runtime.close()
    hand.close()


def _flow_of_accounts_over(hand: Any, statistic: str, threshold: int) -> list[float]:
    """Hand SQL: each month's flow of the accounts whose statistic of their last snapshot in
    January and February is over ``threshold``."""
    rows = hand.execute(
        "WITH latest AS (SELECT id, value FROM snap WHERE (id, ts) IN "
        "(SELECT id, MAX(ts) FROM snap WHERE ts >= '2020-01-01' AND ts < '2020-03-01' "
        f"GROUP BY id)), kept AS (SELECT id FROM latest GROUP BY id HAVING {statistic} > "
        f"{threshold}) SELECT DATE_TRUNC('month', ts), SUM(value) FROM snap "
        "WHERE id IN (SELECT id FROM kept) GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return [float(value) for _, value in rows]


@pytest.mark.parametrize(
    ("aggregation", "statistic"), [("avg", "AVG(value)"), ("max", "MAX(value)")]
)
def test_a_statistic_of_a_balance_in_a_windowed_predicate_reads_the_window(
    windowed: tuple[Runtime, Any], aggregation: str, statistic: str
) -> None:
    """Flow from accounts whose average (or highest) balance over January and February is over
    150: a predicate read ``over: "window"`` reads each account's last snapshot in the window,
    as the same statistic with that window in the call does, so W, Z and Y count (850 in
    January, 220 in February), not only Y, the one account with a snapshot on February 10. The
    balance itself in the same predicate still closes on that day and says so."""
    runtime, hand = windowed
    flow = {"kind": "scoped_aggregate", "measure": "measure.x.flow", "aggregation": "sum",
            "predicates": [{**OVER_50, "input": _statistic(aggregation), "value": 150,
                            "over": "window"}]}  # fmt: skip
    call = {"select": [{"expression": flow, "as": "v"}], "time": JAN_FEB}
    total = runtime.query(call)
    by_month = runtime.query({**call, "time": {**JAN_FEB, "grain": "month"}})
    expected = _flow_of_accounts_over(hand, statistic, 150)
    assert _values(by_month) == expected == [850.0, 220.0]
    assert _values(total) == [sum(expected)]
    for result in (total, by_month):
        assert _by_code(result, "assumptions", CLOSING) == []
    # The call's own window reads the same snapshots: W, Z, X and Y's February 220.
    own = runtime.query({"select": [{"expression": _statistic(aggregation), "as": "v"}],
                         "time": JAN_FEB})  # fmt: skip
    ((same,),) = hand.execute(
        f"SELECT {statistic} FROM snap WHERE (id, ts) IN (SELECT id, MAX(ts) FROM snap GROUP BY id)"
    ).fetchall()
    assert _values(own) == [float(same)]
    balance = {**flow, "predicates": [{**OVER_50, "value": 150, "over": "window"}]}
    closed = runtime.query({"select": [{"expression": balance, "as": "v"}], "time": JAN_FEB})
    assert _values(closed) == [420.0]  # Y alone: 200 + 220
    assert [
        (row["nested"], row["closing_day"]) for row in _by_code(closed, "assumptions", CLOSING)
    ] == [(True, "2020-02-10")]


def test_a_governed_statistic_in_a_windowed_entity_filter_reads_the_window(
    windowed: tuple[Runtime, Any],
) -> None:
    """The same filter as an entity filter on a governed average balance, by month: W, Z and Y
    count, as hand SQL over each account's last snapshot in the window gives, and no closing
    day is read."""
    runtime, hand = windowed
    having = [{"field": "metric.x.avg_report", "op": "gt", "value": 150}]
    result = runtime.query(
        {
            "select": [{"expression": "measure.x.flow", "as": "v"}],
            "where": [{"entity": "entity.x_account", "having": having, "over": "window"}],
            "time": {**JAN_FEB, "grain": "month"},
        }
    )
    assert _values(result) == _flow_of_accounts_over(hand, "AVG(value)", 150) == [850.0, 220.0]
    assert _by_code(result, "assumptions", CLOSING) == []


@pytest.mark.parametrize(("aggregation", "own_window"), [("avg", 150.0), ("max", 200.0)])
def test_a_statistic_of_a_balance_closing_in_another_zone_is_refused_as_the_balance_is(
    tmp_path: Path, aggregation: str, own_window: float
) -> None:
    """With no window, a statistic of a Los Angeles balance on a timestamp clock closes on
    that clock's day, as the balance does, so a call that runs in UTC refuses it the same way
    and names the zone. Read over a window on its own clock it answers, as before: each
    account's last snapshot in the window."""
    runtime = _runtime(_package(tmp_path / "x", clock=LA))
    try:
        call = {"select": [{"expression": _statistic(aggregation), "as": "v"}]}
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(call)
        assert caught.value.code == "MIXED_TIME_ROLES"
        assert caught.value.details == {
            "reason": "closing_day_zone",
            "measure": BALANCE,
            "clock_zone": "America/Los_Angeles",
            "time_zone": "UTC",
        }
        assert str(caught.value) == (
            f"{BALANCE} is a balance read on its closing day in America/Los_Angeles, its clock's "
            "zone, but this query runs in UTC, so its day could be another. Give the call a time "
            "window on the balance's own clock (time.temporal_role) so it runs in that zone. "
            f"Over a window, {aggregation} reads each series' last snapshot in it, not one "
            "closing day."
        )
        balance = {"select": [{"expression": _statistic("last_value"), "as": "v"}]}
        with pytest.raises(SemanticLayerError) as closing:
            runtime.query(balance)
        assert str(closing.value).endswith("(time.temporal_role) so it runs in that zone.")
        own = {
            "temporal_role": "temporal_role.x_snap_ts",
            "start": "2020-01-01",
            "end": "2020-02-01",
        }
        assert _values(runtime.query({**call, "time": own})) == [own_window]
    finally:
        runtime.close()
