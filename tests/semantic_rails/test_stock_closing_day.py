"""Which day a balance is read on, and what the answer says about it.

A stock on an ``as_of_time`` clock (a snapshot table) is read on each period's closing day,
its last day with a snapshot, and a series with no snapshot that day is not counted: a series
that stops is never carried forward. The engine can't tell a series that stopped from one
that takes its snapshots on other days (or a table that records only changes), so the
answer names the closing day it read and warns, with their count, when series seen earlier
in the period had no snapshot that day. A stock on an event- or state-time clock is a
current-state table read as keyed: each series' value in the period, whatever its day.
Every expected number below comes from the hand SQL beside it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import empty_groups
from semantic_rails.dialects import dialect_for_warehouse, supported_warehouses
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_select_for_profile
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts.answer_warnings import (
    closing_day_disclosures,
    granted_closing_day,
)
from semantic_rails.sql_ast import SqlJoin, SqlTableRef
from semantic_rails.sql_preparation import ParameterSlot, parameters_denied
from tests.semantic_rails import shared_seed
from tests.semantic_rails.conftest import opened

CLOSING = "BALANCE_CLOSING_DAY"
MISSING = "BALANCE_SERIES_MISSING_ON_CLOSING_DAY"
YEAR = {"start": "2020-01-01", "end": "2021-01-01"}

SEED = """
CREATE TABLE inv_dense (building VARCHAR, snap_date DATE, area DECIMAL(10, 1));
-- A, B monthly all year; C stops after April; D starts in June.
INSERT INTO inv_dense
SELECT b, d, a FROM (VALUES ('A', 100.0), ('B', 200.0)) AS t(b, a),
  generate_series(DATE '2020-01-01', DATE '2020-12-01', INTERVAL 1 MONTH) AS s(d)
UNION ALL SELECT 'C', d, 50.0 FROM generate_series(DATE '2020-01-01', DATE '2020-04-01',
  INTERVAL 1 MONTH) AS s(d)
UNION ALL SELECT 'D', d, 30.0 FROM generate_series(DATE '2020-06-01', DATE '2020-12-01',
  INTERVAL 1 MONTH) AS s(d);
-- X snapshots on the 5th of each month, Y on the 20th.
CREATE TABLE inv_stagger (building VARCHAR, snap_date DATE, area DECIMAL(10, 1));
INSERT INTO inv_stagger
SELECT 'X', d + INTERVAL 4 DAY, 100.0 FROM generate_series(DATE '2020-01-01', DATE '2020-12-01',
  INTERVAL 1 MONTH) AS s(d)
UNION ALL SELECT 'Y', d + INTERVAL 19 DAY, 200.0 FROM generate_series(DATE '2020-01-01',
  DATE '2020-12-01', INTERVAL 1 MONTH) AS s(d);
-- A row only when a building's area changes.
CREATE TABLE inv_changes (building VARCHAR, snap_date DATE, area DECIMAL(10, 1));
INSERT INTO inv_changes VALUES ('Q', DATE '2019-01-01', 150.0), ('R', DATE '2019-02-01', 35.0),
  ('P', DATE '2019-05-01', 100.0), ('Q', DATE '2020-03-15', 200.0), ('R', DATE '2020-08-10', 40.0);
-- One row per customer: lifetime spend so far, on its first-order clock.
CREATE TABLE customers (customer_id INTEGER, first_order_at TIMESTAMP, spend DECIMAL(10, 2));
INSERT INTO customers VALUES (1, TIMESTAMP '2020-01-03 10:00', 10.50),
  (2, TIMESTAMP '2020-02-11 09:00', 20.25), (3, TIMESTAMP '2020-03-30 17:00', 5.00),
  (4, TIMESTAMP '2020-03-02 08:00', 7.75), (5, TIMESTAMP '2020-04-01 00:00', 99.00);
"""


def _snapshot_model(name: str) -> str:
    return (
        f"model:\n  id: {name}\n  relation: {name}\n  entities: {{{name}: {{}}}}\n"
        "  dimensions: {building: {kind: categorical}}\n"
        "  times:\n    snap_date: {column: snap_date, kind: date, class: as_of_time, "
        "default: true}\n"
        f"  measures:\n    {name}_area: {{kind: aggregate, expr: area, value_type: number,\n"
        "      accumulation: {kind: stock, snapshot: end_of_period}}\n"
    )


CUSTOMERS = """model:
  id: customers
  relation: customers
  entities: {customer: {}}
  times:
    first_order_at: {column: first_order_at, kind: timestamp, class: event_time, default: true}
  measures:
    spend: {kind: aggregate, expr: spend, value_type: number,
      accumulation: {kind: stock, snapshot: end_of_period}}
"""


def _write_package(root: Path, policies: str = "") -> Path:
    (root / "models").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "package.yml").write_text(
        "schema_version: 1\n"
        "package: {id: inv, namespace: inv, name: inv, warehouse: duckdb,\n"
        "  default_db: data/inv.duckdb, seed: {kind: external}, environments: [development]}\n"
        "defaults:\n  time: {timezone: UTC, supported_grains: [day, week, month, quarter, year]}\n"
    )
    entities = "".join(
        f"    {name}: {{key: [building, snap_date], model: {name}}}\n"
        for name in ("inv_dense", "inv_stagger", "inv_changes")
    )
    (root / "graph.yml").write_text(
        f"graph:\n  entities:\n{entities}    customer: {{key: [customer_id], model: customers}}\n"
    )
    for name in ("inv_dense", "inv_stagger", "inv_changes"):
        (root / "models" / f"{name}.yml").write_text(_snapshot_model(name))
    (root / "models" / "customers.yml").write_text(CUSTOMERS)
    if policies:
        (root / "policies.yml").write_text(policies)
    with duckdb.connect(str(root / "data" / "inv.duckdb")) as con:
        con.execute(SEED)
    return root


@pytest.fixture(scope="module")
def stocks(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    root = _write_package(tmp_path_factory.mktemp("closing") / "inv")
    # The hand SQL runs on its own copy of the seed, never the runtime's connection.
    hand = duckdb.connect()
    hand.execute(SEED)
    runtime = opened(Runtime.from_path(str(root)))
    runtime.hand_sql = lambda sql: hand.execute(sql).fetchall()  # type: ignore[attr-defined]
    yield runtime
    runtime.close()
    hand.close()


def _call(table: str, **time) -> dict:
    measure = f"measure.inv.{table}_area" if table != "customers" else "measure.inv.spend"
    clock = (
        f"temporal_role.inv_{table}_snap_date"
        if table != "customers"
        else "temporal_role.inv_customer_first_order_at"
    )
    call: dict = {"select": [{"expression": {"kind": "measure", "measure": measure}, "as": "v"}]}
    if time:
        call["time"] = {"temporal_role": clock, **time}
    return call


def _closing_read(runtime: Runtime, table: str, window: str = "") -> tuple:
    """Hand SQL: the closing day of ``window`` (all history when empty), the sum of the
    snapshots that day, the series seen in it and those whose last snapshot is earlier."""
    where = f"WHERE {window}" if window else ""
    ((day, total, seen, missing),) = runtime.hand_sql(  # type: ignore[attr-defined]
        f"""
        WITH r AS (SELECT * FROM {table} {where}),
        c AS (SELECT MAX(snap_date) AS day FROM r),
        s AS (SELECT building, MAX(snap_date) AS last FROM r GROUP BY building)
        SELECT c.day, (SELECT SUM(area) FROM r WHERE snap_date = c.day),
          (SELECT COUNT(*) FROM s), (SELECT COUNT(*) FROM s WHERE last < c.day)
        FROM c
        """
    )
    return str(day), total, seen, missing


def _values(result: dict) -> list[float | None]:
    """The answer's numbers (a DECIMAL comes back as text, to keep its digits)."""
    return [None if row["v"] is None else float(row["v"]) for row in result["rows"]]


def _codes(result: dict, key: str) -> list[str]:
    return [row["code"] for row in result.get(key) or []]


def _one(result: dict, key: str, code: str) -> dict:
    (row,) = [row for row in result.get(key) or [] if row["code"] == code]
    return row


IN_2020 = "snap_date >= DATE '2020-01-01' AND snap_date < DATE '2021-01-01'"


def test_staggered_snapshot_days_are_never_dropped_silently(stocks: Runtime) -> None:
    """X snapshots on the 5th and Y on the 20th: the closing day holds Y only."""
    day, total, seen, missing = _closing_read(stocks, "inv_stagger", IN_2020)
    assert (day, total, seen, missing) == ("2020-12-20", 200, 2, 1)
    result = stocks.query(_call("inv_stagger", **YEAR))
    assert _values(result) == [float(total)]
    warning = _one(result, "warnings", MISSING)
    assert warning["severity"] == "warning"
    assert warning["details"] == {
        "measure": "measure.inv.inv_stagger_area",
        "outputs": ["v"],
        "nested": False,
        "closing_day": day,
        "periods": 1,
        "checked": True,
        "series": seen,
        "series_missing": missing,
        "periods_missing": 1,
    }
    assert "2020-12-20" in warning["message"] and "1 of 2 series" in warning["message"]
    # By quarter, each quarter's closing day misses X.
    result = stocks.query(_call("inv_stagger", grain="quarter", **YEAR))
    assert _values(result) == [200, 200, 200, 200]
    assert _one(result, "warnings", MISSING)["details"] == {
        "measure": "measure.inv.inv_stagger_area",
        "outputs": ["v"],
        "nested": False,
        "closing_day": "2020-12-20",
        "periods": 4,
        "checked": True,
        "series": 8,
        "series_missing": 4,
        "periods_missing": 4,
    }


def test_sparse_change_records_warn_and_point_to_state_as_of(stocks: Runtime) -> None:
    """Q changed in March and R in August; the closing day (Aug 10) holds R only."""
    day, total, seen, missing = _closing_read(stocks, "inv_changes", IN_2020)
    assert (day, total, seen, missing) == ("2020-08-10", 40, 2, 1)
    result = stocks.query(_call("inv_changes", **YEAR))
    assert _values(result) == [float(total)]
    warning = _one(result, "warnings", MISSING)
    assert warning["details"]["closing_day"] == day
    assert warning["details"]["series_missing"] == missing
    assert "state_as_of" in warning["message"]


def test_a_current_state_stock_on_an_event_clock_reads_each_series(stocks: Runtime) -> None:
    """One row per customer: Q1 holds customers 1-4, whatever day each first ordered."""
    ((total,),) = stocks.hand_sql(  # type: ignore[attr-defined]
        "SELECT SUM(spend) FROM customers WHERE first_order_at >= TIMESTAMP '2020-01-01' "
        "AND first_order_at < TIMESTAMP '2020-04-01'"
    )
    result = stocks.query(_call("customers", start="2020-01-01", end="2020-04-01"))
    assert _values(result) == [float(total)] == [43.5]
    assert CLOSING not in _codes(result, "assumptions")
    assert MISSING not in _codes(result, "warnings")
    by_month = stocks.query(_call("customers", start="2020-01-01", end="2020-04-01", grain="month"))
    assert _values(by_month) == [10.5, 20.25, 12.75]


def test_an_untimed_stock_reads_its_last_closing_day(stocks: Runtime) -> None:
    """With no period, a balance is read on the last day with a snapshot: C, which stopped in
    April, and P, last changed in 2019, are not carried forward."""
    for table, expected in (
        ("inv_dense", ("2020-12-01", 330, 4, 1)),
        ("inv_stagger", ("2020-12-20", 200, 2, 1)),
        ("inv_changes", ("2020-08-10", 40, 3, 2)),
    ):
        day, total, seen, missing = _closing_read(stocks, table)
        assert (day, total, seen, missing) == expected, table
        result = stocks.query(_call(table))
        assert _values(result) == [float(total)], table
        warning = _one(result, "warnings", MISSING)
        assert (warning["details"]["closing_day"], warning["details"]["series_missing"]) == (
            day,
            missing,
        ), table
        assert _one(result, "assumptions", CLOSING)["closing_day"] == day, table


def test_every_closing_day_read_names_its_day(stocks: Runtime) -> None:
    """Monthly, every series seen in a month has a snapshot on its closing day: the answer
    names the day and doesn't warn. Quarterly, C's last snapshot (April 1) misses Q2's
    closing day (June 1)."""
    result = stocks.query(_call("inv_dense", grain="month", **YEAR))
    assumption = _one(result, "assumptions", CLOSING)
    assert assumption["closing_day"] == "2020-12-01"
    assert assumption["periods"] == 12
    assert "2020-12-01" in assumption["message"]
    assert MISSING not in _codes(result, "warnings")
    result = stocks.query(_call("inv_dense", grain="quarter", **YEAR))
    assert _values(result) == [350, 330, 330, 330]
    assert _one(result, "warnings", MISSING)["details"] == {
        "measure": "measure.inv.inv_dense_area",
        "outputs": ["v"],
        "nested": False,
        "closing_day": "2020-06-01",
        "periods": 4,
        "checked": True,
        "series": 13,
        "series_missing": 1,
        "periods_missing": 1,
    }


def test_jaffle_lifetime_spend_reads_every_customer_in_the_quarter(runtime_factory) -> None:
    """The customers table holds one row per customer, on the event clock first_order_at:
    Q1 2017 adds up every customer who first ordered in it, not the customers of one day."""
    seed = shared_seed.seed_for("jaffle_shop")
    assert seed is not None
    with duckdb.connect(str(seed), read_only=True) as con:
        ((customers, total),) = con.execute(
            "SELECT COUNT(*), ROUND(SUM(lifetime_spend_cents) / 100.0, 2) FROM jaffle_customer "
            "WHERE first_order_at >= TIMESTAMP '2017-01-01' "
            "AND first_order_at < TIMESTAMP '2017-04-01'"
        ).fetchall()
    assert (customers, float(total)) == (341, 267324.55)
    runtime = runtime_factory("jaffle_shop")
    call = {
        "select": [
            {
                "expression": {"kind": "measure", "measure": "measure.jaffle.lifetime_spend_usd"},
                "as": "v",
            }
        ],  # fmt: skip
        "time": {
            "temporal_role": "temporal_role.jaffle_customer_first_order_at",
            "start": "2017-01-01",
            "end": "2017-04-01",
        },
    }
    result = runtime.query(call)
    assert [round(value, 2) for value in _values(result)] == [267324.55]
    assert CLOSING not in _codes(result, "assumptions")


@pytest.mark.parametrize("warehouse", supported_warehouses())
def test_closing_day_reads_lower_on_every_dialect(stocks: Runtime, warehouse: str) -> None:
    """The read and its probe compile on every warehouse: by period, over a window, and with
    no period at all (the last closing day over every row)."""
    on = replace(stocks.config, package=replace(stocks.config.package, warehouse=warehouse))
    runtime = Runtime.from_config(on, source_path=stocks.source_path)
    dialect = dialect_for_warehouse(warehouse)
    try:
        for time in ({}, YEAR, {"grain": "quarter", **YEAR}):
            payload = runtime.engine_payload(_call("inv_stagger", **time))
            compiled = compile_query(on, Registry(on), payload)
            (read,) = compiled["closing_reads"]
            assert read["measure"] == "measure.inv.inv_stagger_area"
            probe = render_select_for_profile(read["probe"], dialect=dialect)
            assert "ROW_NUMBER() OVER" in probe and "closing_rows" in probe
            # The answer's own SQL chooses the closing day; with no period, over every row.
            qualify = dialect.capabilities().get("qualify")
            closing = "MAX(inv_stagger.snap_date) OVER (" + ("" if time else ")")
            assert (closing if qualify else "__closing") in compiled["sql"]
            # An event-clock stock is read as keyed, with nothing to probe.
            payload = runtime.engine_payload(_call("customers", **time))
            assert compile_query(on, Registry(on), payload)["closing_reads"] == []
    finally:
        runtime.close()


ONE_BUILDING = """semantic_policies:
  - id: policy.inv.one_building
    kind: row_filter
    dimension: dimension.inv_inv_stagger_building
    attribute: building
    type: string
    audiences: [tenant]
"""


def test_the_closing_day_and_its_counts_are_read_under_the_callers_row_filter(
    tmp_path: Path,
) -> None:
    """A caller who sees only X reads X's own closing day, and no series of another caller
    counts as missing."""
    runtime = opened(Runtime.from_path(str(_write_package(tmp_path / "inv", ONE_BUILDING))))
    try:
        for building, day, total in (("X", "2020-12-05", 100), ("Y", "2020-12-20", 200)):
            context = RequestContext(
                actor="u", audience="tenant", attributes={"building": building}
            )
            result = runtime.query(_call("inv_stagger", **YEAR), context=context)
            assert _values(result) == [total], building
            assert _one(result, "assumptions", CLOSING)["closing_day"] == day, building
            assert MISSING not in _codes(result, "warnings"), building
        # Unfiltered, the same call leaves X out and says so.
        assert _one(runtime.query(_call("inv_stagger", **YEAR)), "warnings", MISSING)
    finally:
        runtime.close()


ONE_DENSE_BUILDING = ONE_BUILDING.replace("inv_stagger", "inv_dense")


@pytest.fixture
def dense_by_building(tmp_path: Path) -> Runtime:
    runtime = opened(Runtime.from_path(str(_write_package(tmp_path / "inv", ONE_DENSE_BUILDING))))
    yield runtime
    runtime.close()


def _building(building: str) -> RequestContext:
    return RequestContext(actor="u", audience="tenant", attributes={"building": building})


MONTHLY_DENSE = _call("inv_dense", grain="month", **YEAR)
DENSE_AREA = "measure.inv.inv_dense_area"


def test_balance_coverage_uses_only_the_callers_visible_rows(dense_by_building: Runtime) -> None:
    """C's snapshots stop after April; other buildings' run to December. C's answer says its
    data ends April 1 and its later months have no data yet; A's data ends in December."""
    result = dense_by_building.query(MONTHLY_DENSE, context=_building("C"))
    assert _values(result) == [50.0] * 4 + [None] * 8
    assert result["window"]["data_through"] == "2020-04-01"
    assert _one(result, "warnings", "NO_DATA_YET")["details"]["measures"] == [
        {"id": DENSE_AREA, "edge": "2020-04-01", "edge_source": "last_row"}
    ]
    result = dense_by_building.query(MONTHLY_DENSE, context=_building("A"))
    assert _values(result) == [100.0] * 12
    assert result["window"]["data_through"] == "2020-12-01"
    assert "NO_DATA_YET" not in _codes(result, "warnings")


def test_every_balance_coverage_cte_carries_the_row_filter(
    dense_by_building: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coverage read binds the caller's filter: one slot for each filtered scan."""
    statements: list = []
    real = runtime_module._adapter_query

    def recording(adapter, query, **kwargs):
        statements.append(query)
        return real(adapter, query, **kwargs)

    monkeypatch.setattr(runtime_module, "_adapter_query", recording)
    dense_by_building.query(MONTHLY_DENSE, context=_building("C"))
    (coverage,) = [row for row in statements if "balance_coverage_1__data_through" in row.sql]
    assert coverage.sql.count("building = ?") == coverage.sql.count("?") == 1
    assert coverage.parameters == (ParameterSlot("building", "string"),)


@pytest.mark.parametrize("read", ["coverage", "closing_day"])
def test_a_refused_coverage_read_is_not_swallowed(
    dense_by_building: Runtime, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """A policy refusal while reading a balance's coverage or closing day refuses the answer;
    any other failure leaves the answer as it is, without what that read would have said."""
    real = runtime_module._probe_row
    failure: Exception = parameters_denied("parameter_count_mismatch")

    def failing(runtime, compiled, payload, probe):
        names = [cte.name for cte in probe.ctes]
        if any(name.startswith("balance_coverage") for name in names) == (read == "coverage"):
            raise failure
        return real(runtime, compiled, payload, probe)

    monkeypatch.setattr(runtime_module, "_probe_row", failing)
    with pytest.raises(SemanticLayerError) as caught:
        dense_by_building.query(MONTHLY_DENSE, context=_building("C"))
    assert (caught.value.code, caught.value.details) == ("POLICY_DENIED", failure.details)
    failure = RuntimeError("the warehouse went away")
    result = dense_by_building.query(MONTHLY_DENSE, context=_building("C"))
    assert _values(result) == [50.0] * 4 + [None] * 8
    if read == "coverage":
        assert "data_through" not in result["window"]
    else:
        assert _one(result, "assumptions", CLOSING)["closing_day"] == ""


def test_a_balance_coverage_read_that_cannot_be_filtered_refuses_the_request(
    dense_by_building: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage that reads a relation no model reads (here a second spelling of the filtered
    one) is refused under a row filter, never read unfiltered; a caller with no filter still
    reads it."""
    real = empty_groups.coverage_select

    def joined(scope, dialect):
        select = real(scope, dialect)
        return replace(select, joins=[SqlJoin("CROSS", SqlTableRef("main.inv_dense", "again"))])

    monkeypatch.setattr(empty_groups, "coverage_select", joined)
    with pytest.raises(SemanticLayerError) as caught:
        dense_by_building.query(MONTHLY_DENSE, context=_building("C"))
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["reason"] == "row_filter_unsupported_query"
    assert caught.value.details["policy_ids"] == ["policy.inv.one_building"]
    assert dense_by_building.query(MONTHLY_DENSE)["window"]["data_through"] == "2020-12-01"


HIDDEN_REGION_RULE = f"""{ONE_DENSE_BUILDING}  - id: policy.inv.secret_region_rule
    kind: row_filter
    dimension: dimension.inv_inv_dense_region
    attribute: region
    type: string
    audiences: [tenant]
  - id: policy.inv.hide_region
    kind: object_visibility
    action: hidden
    object_ids: [dimension.inv_inv_dense_region]
    audiences: [tenant]
"""
PIPELINE_BALANCE = """model:
  id: dense_pipe
  relation: dense_pipe
  entities: {dense_pipe: {}}
  times:
    snap_date: {column: snap_date, kind: date, class: event_time, default: true}
  measures:
    pipe_area: {kind: aggregate, expr: area, value_type: number,
      accumulation: {kind: stock, snapshot: end_of_period}}
"""


@pytest.fixture
def hidden_region(tmp_path: Path) -> Runtime:
    """A tenant sees only its building; a second filter, on a region it can't see, applies
    too. A pipeline reads the filtered relation."""
    root = _write_package(tmp_path / "inv", HIDDEN_REGION_RULE)
    (root / "relations.yml").write_text(
        "relations:\n  dense_pipe:\n    columns: [building, snap_date, area]\n"
        "    steps:\n      - source: inv_dense\n"
    )
    (root / "models" / "dense_pipe.yml").write_text(PIPELINE_BALANCE)
    (root / "models" / "inv_dense.yml").write_text(
        _snapshot_model("inv_dense").replace(
            "{building:", "{region: {kind: categorical}, building:"
        )
    )
    with (root / "graph.yml").open("a") as graph:
        graph.write("    dense_pipe: {key: [building, snap_date], model: dense_pipe}\n")
    with duckdb.connect(str(root / "data" / "inv.duckdb")) as con:
        con.execute("ALTER TABLE inv_dense ADD COLUMN region VARCHAR DEFAULT 'north'")
    runtime = Runtime.from_path(str(root))
    yield runtime
    runtime.close()


PIPE_AREA = [{"expression": {"kind": "measure", "measure": "measure.inv.pipe_area"}, "as": "v"}]
PIPE_MONTHLY = {
    "select": PIPE_AREA,
    "time": {"temporal_role": "temporal_role.inv_dense_pipe_snap_date", "grain": "month", **YEAR},
}


def _tenant(**attributes: object) -> RequestContext:
    return RequestContext(actor="u", audience="tenant", attributes=attributes)


def test_a_refusal_names_no_row_filter_hidden_from_the_caller(hidden_region: Runtime) -> None:
    """A balance read through a pipeline over a filtered relation is refused, timed or not;
    the refusal names the caller's own filter and only the kind of one hidden from them."""
    context = _tenant(building="C", region="north")
    with pytest.raises(SemanticLayerError) as untimed:
        hidden_region.query({"select": PIPE_AREA}, context=context)
    assert untimed.value.details["policy_ids"] == ["policy.inv.one_building", "row_filter"]
    with pytest.raises(SemanticLayerError) as caught:
        hidden_region.query(PIPE_MONTHLY, context=context)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["policy_ids"] == ["policy.inv.one_building", "row_filter"]
    assert "region" not in f"{caught.value} {caught.value.details}"


def _refusal(runtime: Runtime, surface: str, context: RequestContext) -> tuple[dict, str]:
    """The refusal's details and everything a caller reads about it, on ``surface``."""
    if surface == "validate":
        out = runtime.validate({"select": PIPE_AREA}, context=context)
        (error,) = out["errors"]
        return error["details"], json.dumps([error, out["recovery_hints"], out["query_ir_hints"]])
    with pytest.raises(SemanticLayerError) as caught:
        getattr(runtime, surface)({"select": PIPE_AREA}, context=context)
    assert caught.value.code == "POLICY_DENIED"
    return caught.value.details, f"{caught.value} {json.dumps(caught.value.details)}"


@pytest.mark.parametrize("surface", ["query", "compile", "validate"])
@pytest.mark.parametrize("region", [None, 7])
def test_an_attribute_refusal_names_no_row_filter_hidden_from_the_caller(
    hidden_region: Runtime, surface: str, region: int | None
) -> None:
    """A missing or mistyped attribute of a filter the caller can't see is refused without
    naming it; one of the caller's own filters is named, so they can fix it."""
    attributes: dict = {"building": "C"} if region is None else {"building": "C", "region": 7}
    details, said = _refusal(hidden_region, surface, _tenant(**attributes))
    assert details == {"reason": "row_filter_denied"}
    assert "region" not in said
    details, said = _refusal(hidden_region, surface, _tenant(region="north"))
    assert details == {"reason": "missing_attribute", "attribute": "building"}
    assert "building" in said


def test_a_probe_that_fails_still_discloses_the_rule() -> None:
    """A failed probe can't name the day or count the series: the answer says the rule and
    that it couldn't check, never nothing."""
    read = {"measure": "measure.inv.x", "outputs": ["v"], "nested": False}
    (assumption,), (warning,) = closing_day_disclosures([read])
    assert assumption["code"] == CLOSING and assumption["closing_day"] == ""
    assert warning["code"] == MISSING and "couldn't be checked" in warning["message"]
    # A window with no snapshot read no closing day: nothing to say.
    assert closing_day_disclosures([{**read, "closing_day": None, "periods": 0}]) == (
        [],
        [],
    )


def test_a_read_only_for_withheld_values_says_nothing() -> None:
    """A policy withholding an output's values withholds its closing day and counts too; a
    read for the whole answer (an entity filter's) still says them."""
    read = {"measure": "m", "outputs": ["rank"], "nested": False, "closing_day": "2020-01-20",
            "periods": 1, "series": 2, "series_missing": 1, "periods_missing": 1,
            "missing_day": "2020-01-20"}  # fmt: skip
    assert closing_day_disclosures([read], ["rank"]) == ([], [])
    whole = {**read, "outputs": []}
    assumptions, warnings = closing_day_disclosures([whole], ["rank"])
    assert [row["closing_day"] for row in assumptions] == ["2020-01-20"]
    assert [row["details"]["series_missing"] for row in warnings] == [1]


def test_a_read_whose_owner_is_unknown_never_sits_beside_a_withheld_or_ungranted_value() -> None:
    """outputs None: nobody declared which column the read is for, so it says nothing beside
    a withheld column, and a grant sees it only when it holds the measure and every column."""
    read = {"measure": "m", "outputs": None, "nested": True, "closing_day": "2020-01-20",
            "periods": 1, "series": 2, "series_missing": 1, "periods_missing": 1,
            "missing_day": "2020-01-20"}  # fmt: skip
    assert closing_day_disclosures([read], ["rank"]) == ([], [])
    (assumption,), (warning,) = closing_day_disclosures([read])
    assert assumption["outputs"] is None
    columns = [{"field": "v", "semantic_id": "metric.v"}, {"field": "d", "semantic_id": "dim.d"}]
    for entry in (assumption, warning):
        assert granted_closing_day(entry, columns, {"m", "metric.v", "dim.d"}) == entry
        assert granted_closing_day(entry, columns, {"m", "metric.v"}) is None
        # A measure not granted is never named by a column that may not read it.
        assert granted_closing_day(entry, columns, {"metric.v", "dim.d"}) is None
    # A whole-answer read (an entity filter's) of a measure not granted says nothing either.
    whole, _ = closing_day_disclosures([{**read, "outputs": []}])
    assert granted_closing_day(whole[0], columns, {"metric.v", "dim.d"}) is None
