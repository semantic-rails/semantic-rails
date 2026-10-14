"""Key inference: a key is suggested only once every row confirms it; otherwise the draft is a
row-grain model, and the search says why."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails import architect_introspection
from semantic_rails.architect_introspection import open_duckdb, suggest_model
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.dbt_artifacts import dbt_import_models, load_dbt_artifacts
from tests.semantic_rails.dbt_warehouse import (
    build_dbt_warehouse,
    write_dbt_artifacts,
    write_orders_package,
)


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[Any]:
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE repo_snapshots AS SELECT r AS repo_id,
              DATE '2024-01-01' + d::INTEGER AS snapshot_date,
              'u' || CAST(r * 100 + d AS VARCHAR) AS row_uuid, r * d AS stars
            FROM range(20) t(r), range(30) u(d);
            CREATE TABLE orders AS SELECT i AS order_id, i % 5 AS customer_id,
              TIMESTAMP '2024-01-01' + to_seconds(i) AS ordered_at, i * 1.5 AS amount
            FROM range(100) t(i);
            CREATE TABLE readings AS SELECT * FROM (VALUES
              (1, TIMESTAMP '2024-01-01', 2.0), (1, TIMESTAMP '2024-01-01', 2.0),
              (2, TIMESTAMP '2024-01-02', 3.0)) t(device_id, read_at, kwh);
            CREATE TABLE empty_events (event_id INTEGER, amount INTEGER);
            CREATE TABLE events AS SELECT
              CASE WHEN i = 701 THEN 700 ELSE i END AS event_id,
              i % 40 AS device_id, TIMESTAMP '2024-01-01' + to_seconds(i) AS read_at
            FROM range(1000) t(i);
            """
        )
    with open_duckdb(path) as opened:
        yield opened


@pytest.mark.parametrize(
    ("relation", "key"),
    [
        ("repo_snapshots", ["repo_id", "snapshot_date"]),  # entity + date over a unique row_uuid
        ("orders", ["order_id"]),  # the entity's id over an accidentally unique pair
    ],
)
def test_inference_prefers_identity_and_clock(warehouse: Any, relation: str, key: list) -> None:
    suggested = suggest_model(warehouse, relation)
    assert suggested["key_search"]["status"] == "confirmed"
    assert suggested["primary_key"]["columns"] == key
    assert suggested["upsert_model"]["primary_key"] == key


@pytest.mark.parametrize(
    ("relation", "status"), [("readings", "none_found"), ("empty_events", "empty_table")]
)
def test_no_key_drafts_row_grain_with_a_row_count(warehouse: Any, relation: str, status: str):
    suggested = suggest_model(warehouse, relation)
    assert suggested["key_search"]["status"] == status
    assert suggested["primary_key"] is None
    draft = suggested["upsert_model"]
    assert draft["primary_key"] == []
    rows = draft["measures"][f"{suggested['entity']}_rows"]
    assert (rows["expr"], rows["default_agg"]) == ({"kind": "literal", "value": 1}, "sum")


def test_a_key_unique_only_in_the_sample_is_rejected_and_the_search_goes_on(
    warehouse: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(architect_introspection, "MAX_PROFILE_ROWS", 100)
    queries: list[str] = []
    execute = type(warehouse).execute
    monkeypatch.setattr(
        type(warehouse),
        "execute",
        lambda self, sql, params=None: queries.append(sql) or execute(self, sql, params),
    )
    suggested = suggest_model(warehouse, "events")
    assert suggested["sampled"]
    whole = [sql for sql in queries if '"event_id"' in sql and '"main"."events"' in sql]
    assert whole, "event_id survived the sample and was rejected on the whole relation"
    assert suggested["key_search"] == {"status": "confirmed", "checked": 2}
    assert suggested["primary_key"]["columns"] == ["event_id", "read_at"]  # the next candidate


def test_a_search_out_of_time_writes_no_key(warehouse: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(architect_introspection, "KEY_SEARCH_SECONDS", 0.0)
    suggested = suggest_model(warehouse, "orders")
    assert suggested["key_search"]["status"] == "timed_out"
    assert suggested["key_search"]["unconfirmed"][0] == ["order_id"]
    assert suggested["primary_key"] is None and suggested["upsert_model"]["primary_key"] == []


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
    assert not {"order_event", "order_events"} & set(graph["graph"]["entities"])


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


def test_a_relation_named_like_a_sample_is_confirmed_on_its_own_rows(tmp_path):
    # One duplicate id among the rows the one-thread repeatable sample leaves out.
    path = tmp_path / "shadow.duckdb"
    rows = architect_introspection.MAX_PROFILE_ROWS + 1
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE __semantic_rails_sample AS SELECT CASE WHEN i = 866466 THEN 700 "
            f"ELSE i END AS id, 'x' AS category FROM range({rows}) t(i)"
        )
    with open_duckdb(path) as warehouse:
        warehouse.connection.execute("SET threads = 1")
        suggested = suggest_model(warehouse, "__semantic_rails_sample")
        temporary = warehouse.execute(
            "SELECT system.main.count(*) FROM system.main.duckdb_tables() WHERE temporary"
        )[1][0][0]
    assert suggested["sampled"] is True
    assert suggested["primary_key"] is None, suggested["key_search"]
    assert suggested["upsert_model"]["primary_key"] == []
    assert temporary == 0  # the sample is dropped


def test_a_key_is_confirmed_under_the_equality_joins_use(tmp_path):
    # 'a' and 'A' are one value to a NOCASE column's joins and groups.
    path = tmp_path / "collated.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE accounts (account_id VARCHAR COLLATE NOCASE, category VARCHAR); "
            "INSERT INTO accounts VALUES ('a', 'x'), ('A', 'x')"
        )
    with open_duckdb(path) as warehouse:
        suggested = suggest_model(warehouse, "accounts")
    assert suggested["primary_key"] is None, suggested["key_search"]
    assert suggested["key_search"]["status"] == "none_found"


def test_a_declared_primary_key_is_confirmed_like_any_other(tmp_path):
    # DuckDB keeps 'a' and 'A' under a NOCASE primary key, but joins read them as one value.
    path = tmp_path / "declared.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE accounts (account_id VARCHAR COLLATE NOCASE PRIMARY KEY, "
            "category VARCHAR); INSERT INTO accounts VALUES ('a', 'x'), ('A', 'x')"
        )
    with open_duckdb(path) as warehouse:
        suggested = suggest_model(warehouse, "accounts")
    assert suggested["primary_key"] is None, suggested["key_search"]
    assert suggested["upsert_model"]["primary_key"] == []


def test_one_deadline_covers_sampling_profiling_and_the_search(
    warehouse: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(architect_introspection, "KEY_SEARCH_SECONDS", 0.01)
    monkeypatch.setattr(architect_introspection, "MAX_PROFILE_ROWS", 100)  # a sampled relation
    profile = architect_introspection._profile

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.035)
        return profile(*args, **kwargs)

    monkeypatch.setattr(architect_introspection, "_profile", slow)
    suggested = suggest_model(warehouse, "events")
    temporary = warehouse.execute(
        "SELECT system.main.count(*) FROM system.main.duckdb_tables() WHERE temporary"
    )[1][0][0]
    assert suggested["key_search"]["status"] == "timed_out"
    assert suggested["primary_key"] is None and suggested["upsert_model"]["primary_key"] == []
    assert temporary == 0


@pytest.mark.parametrize(
    ("sql", "key"),
    [  # a key column named like an internal alias reads as itself
        (
            "CREATE TABLE accounts (account_id VARCHAR COLLATE NOCASE, n INTEGER); "
            "INSERT INTO accounts VALUES ('a', 1), ('A', 1)",
            None,
        ),
        ("CREATE TABLE letters (n VARCHAR); INSERT INTO letters VALUES ('a'), ('b')", ["n"]),
    ],
)
def test_key_columns_never_collide_with_internal_names(tmp_path, sql, key):
    path = tmp_path / "names.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(sql)
    relation = sql.split()[2]
    with open_duckdb(path) as warehouse:
        suggested = suggest_model(warehouse, relation)
    assert (suggested["primary_key"] or {}).get("columns") == key, suggested["key_search"]
