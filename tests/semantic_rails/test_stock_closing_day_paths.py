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
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.dialects import DuckDbDialect
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

CLOSING = "BALANCE_CLOSING_DAY"
MISSING = "BALANCE_SERIES_MISSING_ON_CLOSING_DAY"
BALANCE = "measure.x.balance"
ROWS = "('X', TIMESTAMP '2020-01-05', 100), ('Y', TIMESTAMP '2020-01-20', 200)"
FEE = {"kind": "scoped_aggregate", "measure": BALANCE, "aggregation": "sum"}
OVER_50 = {"entity": "entity.x_account", "input": BALANCE, "op": "gt", "value": 50}


def _package(root: Path, rows: str = ROWS, clock: str = "") -> Path:
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
        "model:\n  id: snap\n  relation: snap\n  entities: {snap: {}, account: {}}\n"
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
        con.execute(f"CREATE TABLE snap (id VARCHAR, ts TIMESTAMP, value INTEGER); "
                    f"INSERT INTO snap VALUES {rows}; "
                    "CREATE TABLE accounts AS SELECT DISTINCT id FROM snap")  # fmt: skip
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
