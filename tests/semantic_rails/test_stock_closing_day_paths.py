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
    root: Path, rows: str = ROWS, clock: str = "", *, relation: str = "snap", ts: str = "TIMESTAMP"
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
        f"  times:\n    ts: {{column: ts, kind: timestamp, class: as_of_time, default: true{clock}}}\n"
        "  measures:\n"
        f"    balance: {{kind: aggregate, expr: value, {stock}}}\n"
        f'    balance2: {{kind: aggregate, expr: "value * 2", {stock}}}\n'
        "    flow: {kind: aggregate, expr: value, default_agg: sum,\n"
        "      accumulation: {kind: flow}, value_type: number}\n"
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


def test_an_entity_set_ratio_reads_the_closing_day(x: Runtime) -> None:
    """The share of balances held by accounts over 50: X has no snapshot on the closing day,
    so neither side counts it, and the share is 200 / 200."""
    ratio = {"kind": "ratio", "numerator": {**FEE, "predicates": [{**OVER_50, "over": "all_time"}]},
             "denominator": FEE}  # fmt: skip
    result = x.query({"select": [{"expression": ratio, "as": "v"}]})
    assert _values(result) == [1.0]
    assert (BALANCE, "2020-01-20", 1) in _disclosed(result)


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


@pytest.mark.parametrize(
    "time",
    [
        {},
        {"start": "2020-01-01", "end": "2020-01-02"},
        {"start": "2020-01-01", "end": "2020-01-03", "grain": "day"},
    ],
    ids=["untimed", "window", "by_day"],
)
def test_the_closing_day_is_the_clocks_own_day(lowering, tmp_path: Path, time: dict) -> None:
    """Stored in UTC, read in Los Angeles: X at 07:00Z on January 2 and Y at 20:00Z on
    January 1 are both January 1 there, so both are read."""
    rows = "('X', TIMESTAMP '2020-01-02 07:00', 100), ('Y', TIMESTAMP '2020-01-01 20:00', 200)"
    zone = ", timezone: America/Los_Angeles, column_timezone: UTC"
    runtime = _runtime(_package(tmp_path / "x", rows, zone))
    try:
        call: dict = {"select": [{"expression": BALANCE, "as": "v"}]}
        if time:
            call["time"] = {"temporal_role": "temporal_role.x_snap_ts", **time}
        result = runtime.query(call)
        assert [v for v in _values(result) if v is not None] == [300.0]
        assert _disclosed(result) == [(BALANCE, "2020-01-01", 0)]
    finally:
        runtime.close()


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
    and reads 100, so their ratio is 1.0, not a ratio of two empty closing days."""
    is_100 = {"entity": "entity.x_account", "input": "measure.x.flow", "op": "is", "value": 100,
              "over": "all_time"}  # fmt: skip
    over_50 = {**is_100, "op": "gt", "value": 50}
    for operand in ({**FEE, "predicates": [is_100]}, {**FEE, "predicates": [is_100, over_50]}):
        assert _values(x.query({"select": [{"expression": operand, "as": "v"}]})) == [100.0]
    ratio = {"kind": "ratio", "numerator": {**FEE, "predicates": [is_100, over_50]},
             "denominator": {**FEE, "predicates": [is_100]}}  # fmt: skip
    result = x.query({"select": [{"expression": ratio, "as": "v"}]})
    assert _values(result) == [1.0]
    assert (BALANCE, "2020-01-05", 0) in _disclosed(result)


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
    day grain puts each snapshot in, so both are read, untimed too."""
    runtime = _runtime(_package(tmp_path / "x", rows, LA + zone, ts=ts))
    try:
        call: dict = {"select": [{"expression": BALANCE, "as": "v"}]}
        if time:
            call["time"] = {"temporal_role": "temporal_role.x_snap_ts", **time}
        result = runtime.query(call)
        assert [v for v in _values(result) if v is not None] == [300.0]
        assert _disclosed(result) == [(BALANCE, "2020-03-08", 0)]
    finally:
        runtime.close()


def test_a_zone_aware_clock_declared_in_utc_closes_on_its_day_bucket(
    lowering, tmp_path: Path
) -> None:
    """A TIMESTAMP WITH TIME ZONE column with column_timezone: UTC: X at 20:00Z and Y at
    10:00Z, January 2, close on January 2 and both count."""
    rows = "('X', TIMESTAMPTZ '2020-01-02 20:00:00+00', 100), ('Y', TIMESTAMPTZ '2020-01-02 10:00:00+00', 200)"  # fmt: skip
    runtime = _runtime(
        _package(tmp_path / "x", rows, LA + ", column_timezone: UTC", ts="TIMESTAMPTZ")
    )
    try:
        result = runtime.query({"select": [{"expression": BALANCE, "as": "v"}]})
        assert _values(result) == [300.0]
        assert _disclosed(result) == [(BALANCE, "2020-01-02", 0)]
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
