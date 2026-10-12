"""``zero_through``: what a package may declare, and what the declared edge compiles to.

Answers are frozen in the correctness ledger (``shop/tests/answers.yml``) and their disclosures
checked in ``tests/integration/correctness/test_zero_through.py``.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.compiler_parts.empty_groups import (
    DeclaredEdge,
    LeafScope,
    guard_empty_groups,
)
from semantic_rails.config import load_package_config
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ZeroThrough
from semantic_rails.sql_ast import (
    SqlCall,
    SqlField,
    SqlIdentifier,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
)
from tests.integration.correctness.conftest import _write_variant

NOW = "2024-10-07T06:00:00Z"
WEEKS = {"select": ["measure.shop.tickets_opened"], "time": {"last": {"unit": "week", "value": 4}, "grain": "week"}}  # fmt: skip


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    return _write_variant(tmp_path, "utc_authored")


def _edit(package: Path, name: str, change: Any) -> None:
    path = package / name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _tickets(package: Path, value: Any) -> None:
    _edit(package, "models/tickets.yml", lambda doc: doc["model"].update(zero_through=value))


def _edges(package: Path) -> dict[str, ZeroThrough | None]:
    config = load_package_config(str(package))
    return {row.id: row.zero_through for row in config.measures}


def _default(package: Path, value: Any) -> None:
    (package / "defaults.yml").write_text(yaml.safe_dump({"defaults": {"zero_through": value}}))


def test_a_model_declares_its_edge_and_the_project_default_covers_the_rest(shop):
    _default(shop, "now")
    edges = _edges(shop)
    assert edges["measure.shop.tickets_opened"] == ZeroThrough(
        "loaded", "ticket_loads.through_day", "ticket_loads", "through_day", "date", "UTC"
    )
    assert edges["measure.shop.audited_tickets"].relation == "ticket_audits"
    assert edges["measure.shop.tickets_always_loaded"] == ZeroThrough("now")
    # A model with no zero_through takes the project's; the engine default is the last row.
    assert edges["measure.shop.revenue"] == ZeroThrough("now")
    assert edges["measure.shop.tickets_by_last_row"] == ZeroThrough("now")
    _tickets(shop, "data")
    assert _edges(shop)["measure.shop.tickets_opened"] is None


def test_without_a_declaration_every_measure_reads_its_last_row(shop):
    edges = _edges(shop)
    assert edges["measure.shop.revenue"] is None
    assert edges["measure.shop.seats"] is None


def test_a_mark_is_found_by_its_column_and_a_timestamp_keeps_its_stored_zone(shop):
    def logged_at(doc: dict[str, Any]) -> None:
        doc["model"]["times"] = {
            "logged": {
                "column": "logged_at",
                "kind": "timestamp",
                "column_timezone": "Europe/Paris",
            }
        }

    _edit(shop, "models/ticket_loads.yml", logged_at)
    _tickets(shop, {"loaded": "ticket_loads.logged_at"})
    edge = _edges(shop)["measure.shop.tickets_opened"]
    assert (edge.column, edge.data_type, edge.timezone) == (
        "logged_at",
        "timestamp",
        "Europe/Paris",
    )


@pytest.mark.parametrize(
    "value,problem",
    [
        ("yes", "must be data, now or {loaded: <model>.<field>}"),
        ({"loaded": "ticket_loads"}, "must be data, now or {loaded: <model>.<field>}"),
        ({"loaded": 3}, "must be data, now or {loaded: <model>.<field>}"),
        ({"loaded": "ticket_loads.through_day", "now": True}, "must be data, now"),
        ({"loaded": "export_runs.through_day"}, "there is no model 'export_runs'"),
        ({"loaded": "ticket_loads.run_id"}, "no date or timestamp field of model 'ticket_loads'"),
        ({"loaded": "ticket_loads.missing"}, "no date or timestamp field of model 'ticket_loads'"),
    ],
)
def test_an_edge_that_cant_be_read_is_refused_at_load_with_its_path(shop, value, problem):
    _tickets(shop, value)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(shop))
    assert raised.value.code == "INVALID_CONFIG"
    assert "models.tickets.zero_through" in str(raised.value)
    assert problem in str(raised.value)
    assert "Write zero_through: data | now | {loaded: <model>.<date or timestamp field>}" in str(
        raised.value
    )
    assert raised.value.details["path"] == "models.tickets.zero_through"


def test_a_project_default_is_checked_where_it_is_written(shop):
    _default(shop, "soon")
    with pytest.raises(SemanticLayerError, match="defaults.zero_through must be data, now"):
        load_package_config(str(shop))


def test_a_load_log_read_through_a_relation_pipeline_is_refused(shop):
    spine = {"start": "2024-09-01", "end": "2024-09-29", "column": "through_day"}
    (shop / "relations").mkdir()
    (shop / "relations" / "loads_view.yml").write_text(
        yaml.safe_dump({"relation": {"id": "loads_view", "date_spine": spine}})
    )
    _edit(shop, "models/ticket_loads.yml", lambda doc: doc["model"].update(relation="loads_view"))
    with pytest.raises(SemanticLayerError, match="reads a relation pipeline"):
        load_package_config(str(shop))


def test_a_warehouse_without_loaded_coverage_refuses_a_declared_edge(shop):
    _edit(shop, "package.yml", lambda doc: doc["package"].update(warehouse="clickhouse"))
    with pytest.raises(SemanticLayerError, match="can only be data on ClickHouse"):
        load_package_config(str(shop))


def test_a_compile_on_such_a_warehouse_refuses_rather_than_ignores_it(shop):
    config = load_package_config(str(shop))
    config = replace(config, package=replace(config.package, warehouse="clickhouse"))
    runtime = Runtime.from_config(config, source_path=str(shop))
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.compile(WEEKS, now=NOW)
        assert raised.value.code == "INVALID_CONFIG"
        assert raised.value.details["measures"] == ["measure.shop.tickets_opened"]
        # A measure that reads its last row compiles as before.
        runtime.compile({**WEEKS, "select": ["measure.shop.tickets_by_last_row"]}, now=NOW)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "warehouse",
    [
        "duckdb",
        "motherduck",
        "ducklake",
        "postgres",
        "snowflake",
        "bigquery",
        "databricks",
        "athena",
    ],
)
def test_the_load_log_mark_compiles_beside_coverage_on_every_warehouse_that_reads_it(
    shop, warehouse
):
    config = load_package_config(str(shop))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    runtime = Runtime.from_config(config, source_path=str(shop))
    try:
        sql = runtime.compile(WEEKS, now=NOW)["rendered_sql"]
    finally:
        runtime.close()
    assert "edge_1_rows" in sql and "ticket_loads" in sql and "loaded_before" in sql


def test_the_declared_edge_never_moves_routing(shop):
    """An open monthly window reads the rollup with or without an edge on its measures; a
    named window reads the base either way."""
    base = load_package_config(str(shop))
    declared = replace(
        base,
        measures=[
            replace(row, zero_through=ZeroThrough("now"))
            if row.id == "measure.shop.revenue"
            else row
            for row in base.measures
        ],
    )
    sql: dict[str, list[str]] = {}
    for name, config in (("last_row", base), ("now", declared)):
        runtime = Runtime.from_config(config, source_path=str(shop))
        try:
            sql[name] = [
                runtime.compile({"select": ["measure.shop.revenue"], "time": time}, now=NOW)[
                    "rendered_sql"
                ]
                for time in (
                    {"grain": "month", "end": "2023-12-01"},
                    {"grain": "month", "start": "2023-10-01", "end": "2024-09-01"},
                )
            ]
        finally:
            runtime.close()
    assert sql["now"][0] == sql["last_row"][0]
    assert "FROM orders_monthly" in sql["now"][0]
    assert ["FROM orders_monthly" in text for text in sql["now"]] == [True, False]
    assert ["FROM orders_monthly" in text for text in sql["last_row"]] == [True, False]
    assert "edge_1" in sql["now"][1] and "edge_1" not in sql["last_row"][1]


def _scope(edge: DeclaredEdge) -> LeafScope:
    opened = SqlIdentifier(parts=["tickets", "opened_at"])
    return LeafScope(
        from_table=SqlTableRef(name="tickets"),
        joins=(),
        where=(),
        value=SqlCall("COUNT", [SqlIdentifier(parts=["tickets", "ticket_id"])]),
        bucket=opened,
        raw_time=opened,
        edge=edge,
    )


def _edge(relation: str) -> DeclaredEdge:
    rows = SqlSelect(
        select=[
            SqlField(SqlCall("MAX", [SqlIdentifier(parts=[relation, "through_day"])]), "watermark")
        ],
        from_table=SqlTableRef(name=relation),
        observation_scan=True,
    )
    bound = SqlSelect(select=[SqlField(SqlLiteral(1), "loaded_before")])
    return DeclaredEdge(rows=rows, bound=bound)


@pytest.mark.parametrize("second", ["ticket_audits", "ticket_loads"])
def test_two_bindings_over_one_table_never_share_an_edge(second):
    """Coverage over one table is read once; each binding has its own edge, and each measure's
    guard reads only its own."""
    scopes = {"a": _scope(_edge("ticket_loads")), "b": _scope(_edge(second))}
    ctes = guard_empty_groups(
        "series_base",
        ["t"],
        ["a", "b"],
        {"a": "count", "b": "count"},
        scopes,
        time_key="t",
        dialect=dialect_for_warehouse("duckdb"),
    )
    names = [cte.name for cte in ctes]
    shared = second == "ticket_loads"
    assert names.count("coverage_1") == 1 and "coverage_2" not in names
    assert ("edge_2" in names) is not shared
    guard = ctes[-1].query
    reads = {
        field.alias: {
            node.parts[0]
            for node in _identifiers(field.expression)
            if node.parts[0].startswith("edge_")
        }
        for field in guard.select
        if field.alias in {"a", "b"}
    }
    assert reads == {"a": {"edge_1"}, "b": {"edge_1" if shared else "edge_2"}}


def _identifiers(expr: Any) -> list[SqlIdentifier]:
    from semantic_rails.sql_ast import sql_nodes

    return [node for node in sql_nodes(expr) if isinstance(node, SqlIdentifier)]


def test_a_written_package_keeps_its_models_edges(tmp_path):
    """The package writer writes each model's edge back on the model, never on a measure."""
    from semantic_rails.config import load_package_snapshot
    from semantic_rails.interop.package_writer import write_package

    starter = (
        Path(__file__).resolve().parents[2] / "configs/examples/semantic_rails_package_starter.yml"
    )
    snapshot = load_package_snapshot(starter)
    config = snapshot.config
    first = config.measures[0]
    declared = replace(
        config,
        measures=[
            replace(row, zero_through=ZeroThrough("now")) if row.entity == first.entity else row
            for row in config.measures
        ],
    )
    directory = write_package(
        declared, tmp_path / "written", namespace=snapshot.normalized["package"]["namespace"]
    )
    loaded = load_package_snapshot(directory).config
    assert {row.id: row.zero_through for row in loaded.measures} == {
        row.id: row.zero_through for row in declared.measures
    }
