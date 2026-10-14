"""Key inference: a key is written only once the whole relation confirms it; otherwise the draft
is a row-grain model, and the search says why."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.architect_introspection import open_duckdb, suggest_model
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.dbt_artifacts import dbt_import_models, load_dbt_artifacts
from tests.semantic_rails.dbt_warehouse import (
    build_dbt_warehouse,
    write_dbt_artifacts,
    write_orders_package,
)


def _suggest(tmp_path: Path, sql: str, relation: str, **options) -> dict:
    db_path = tmp_path / "keys.duckdb"
    with duckdb.connect(str(db_path)) as connection:
        connection.execute(sql)
    with open_duckdb(db_path) as warehouse:
        return suggest_model(warehouse, relation, **options)


def test_an_entity_and_date_snapshot_is_keyed_by_both_over_a_unique_row_uuid(tmp_path):
    suggested = _suggest(
        tmp_path,
        "CREATE TABLE repos (repo_id INTEGER PRIMARY KEY); "
        "INSERT INTO repos SELECT range FROM range(5); "
        "CREATE TABLE repo_traffic AS SELECT range % 5 AS repo_id, "
        "DATE '2024-01-01' + CAST(range // 5 AS INTEGER) AS snapshot_date, range * 7 AS views, "
        "CAST(uuid() AS VARCHAR) AS row_uuid FROM range(50)",
        "repo_traffic",
    )
    assert suggested["primary_key"]["columns"] == ["repo_id", "snapshot_date"]
    assert suggested["key_search"] == {
        "status": "confirmed",
        "columns": ["repo_id", "snapshot_date"],
    }
    assert suggested["upsert_model"]["primary_key"] == ["repo_id", "snapshot_date"]


def test_the_entity_named_id_beats_an_accidentally_unique_foreign_key_and_time(tmp_path):
    suggested = _suggest(
        tmp_path,
        "CREATE TABLE orders AS SELECT range AS order_id, range % 3 AS customer_id, "
        "TIMESTAMP '2024-01-01' + INTERVAL (range) HOUR AS ordered_at, 10 AS amount "
        "FROM range(30)",
        "orders",
    )
    assert suggested["primary_key"]["columns"] == ["order_id"]
    assert suggested["primary_key"]["confidence"] == "high"


def test_duplicate_rows_have_no_key(tmp_path):
    suggested = _suggest(
        tmp_path,
        "CREATE TABLE scans AS SELECT * FROM (VALUES (1, TIMESTAMP '2024-01-01', 'pickup'), "
        "(1, TIMESTAMP '2024-01-01', 'pickup'), (2, TIMESTAMP '2024-01-02', 'pickup')) "
        "AS t(order_id, scanned_at, scan_type)",
        "scans",
    )
    assert suggested["primary_key"] is None
    assert suggested["key_search"]["status"] == "none_found"
    assert suggested["key_search"]["checked"] > 0
    draft = suggested["upsert_model"]
    assert draft["primary_key"] == []
    assert draft["measures"]["scan_rows"] == {
        "kind": "aggregate",
        "expr": {"kind": "literal", "value": 1},
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
        "value_type": "count",
    }


def test_a_key_unique_only_in_the_sample_is_rejected_and_the_search_goes_on(tmp_path):
    # event_id repeats once among 1000 rows, outside a 20-row sample; serial never repeats.
    suggested = _suggest(
        tmp_path,
        "CREATE TABLE events AS SELECT CASE WHEN range = 999 THEN 0 ELSE range END AS event_id, "
        "range AS serial FROM range(1000)",
        "events",
        sample_rows=20,
    )
    assert suggested["sampled"] is True
    assert suggested["primary_key"]["columns"] == ["serial"]
    assert suggested["key_search"] == {"status": "confirmed", "columns": ["serial"]}


def test_a_search_out_of_time_writes_no_key(tmp_path):
    suggested = _suggest(
        tmp_path,
        "CREATE TABLE events AS SELECT range AS event_id FROM range(1000)",
        "events",
        sample_rows=20,
        deadline=0,
    )
    assert suggested["primary_key"] is None
    assert suggested["key_search"] == {"status": "timed_out", "unconfirmed": [["event_id"]]}
    assert suggested["upsert_model"]["primary_key"] == []


def test_an_empty_table_is_no_evidence_of_a_key(tmp_path):
    suggested = _suggest(
        tmp_path, "CREATE TABLE events (event_id INTEGER, amount INTEGER)", "events"
    )
    assert suggested["primary_key"] is None
    assert suggested["key_search"] == {"status": "empty_table"}


def test_upsert_model_with_no_key_writes_a_row_grain_model(tmp_path):
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    result = ArchitectProject(project, workspace_root=tmp_path).upsert_model(
        model_id="order_events",
        entity_key="order_event",
        relation="main_marts.fct_orders",
        primary_key=[],
        measures={"event_rows": {"kind": "aggregate", "expr": {"kind": "literal", "value": 1}}},
    )
    assert result.report["ok"] is True, result.report
    config = load_package_config(str(project))
    events = next(entity for entity in config.entities if entity.id == "entity.shop_order_events")
    assert events.is_row_grain
    graph = yaml.safe_load((project / "graph.yml").read_text(encoding="utf-8"))
    assert "order_event" not in graph["graph"]["entities"]


@pytest.mark.xfail(
    strict=True, reason="cut for size: the dbt importer still skips a model without a key"
)
def test_a_dbt_model_without_key_tests_imports_as_row_grain(tmp_path):
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    target = tmp_path / "dbt" / "target"
    write_dbt_artifacts(package / "data" / "warehouse.duckdb", target)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    for name in ("unique_fct_orders_order_id", "not_null_fct_orders_order_id"):
        del manifest["nodes"][f"test.shop_dbt.{name}"]
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    items, skipped_models, skipped_references = dbt_import_models(
        load_dbt_artifacts(target), ["fct_orders", "fct_order_lines"]
    )
    orders = next(item for item in items if item["relation"] == "main_marts.fct_orders")
    assert skipped_models == []
    assert orders["primary_key"] == []
    assert [(row["model"], row["columns"]) for row in skipped_references] == [
        ("order_lines", ["order_id"])
    ]
    assert "no key" in skipped_references[0]["reason"]
