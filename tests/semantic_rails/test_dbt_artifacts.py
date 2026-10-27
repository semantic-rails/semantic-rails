"""dbt manifest.json and catalog.json, read for package authoring (dbt never runs)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails import architect_mcp
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.dbt_artifacts import (
    dbt_import_models,
    load_dbt_artifacts,
    suggest_models_from_dbt,
)
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.dbt_warehouse import (
    build_dbt_warehouse,
    write_dbt_artifacts,
)


@pytest.fixture()
def target(tmp_path: Path) -> Path:
    db_path = build_dbt_warehouse(tmp_path / "warehouse.duckdb")
    return write_dbt_artifacts(db_path, tmp_path / "target")


def _by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): row for row in rows}


def test_relations_keep_their_schema_and_alias(target: Path) -> None:
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for index in range(20):  # more sources in another database than models in the project's
        manifest["sources"][f"source.shop_dbt.lake.feed_{index}"] = {
            "resource_type": "source",
            "database": "lake",
            "schema": "main",
            "identifier": f"feed_{index}",
        }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    project = load_dbt_artifacts(target)

    assert project.project_name == "shop_dbt" and project.adapter_type == "duckdb"
    assert project.find("fct_orders").relation == "main_marts.fct_orders"
    assert project.find("stg_orders").relation == "main_staging.stg_orders"
    # dbt-duckdb's default schema is left out, as introspection and packages spell it.
    assert project.find("seed.shop_dbt.raw_orders").relation == "raw_orders"
    assert project.find("source.shop_dbt.lake.feed_0").relation == "lake.main.feed_0"
    assert project.find("fct_orders").materialized == "table"


def test_tests_and_contracts_become_keys_links_and_value_sets(target: Path) -> None:
    project = load_dbt_artifacts(target)

    orders = project.find("fct_orders")
    assert (orders.primary_key, orders.primary_key_source) == (
        ["order_id"],
        "unique and not_null tests",
    )
    assert project.find("dim_customers").primary_key_source == "contract"
    lines = project.find("fct_order_lines")
    assert lines.primary_key == ["order_id", "line_number"]
    assert {(fk["columns"][0], fk["to_relation"]) for fk in lines.foreign_keys} == {
        ("order_id", "main_marts.fct_orders"),
        ("product_id", "main_marts.dim_products"),
    }
    assert orders.columns["status"].accepted_values == [
        "placed",
        "shipped",
        "delivered",
        "returned",
    ]
    assert orders.columns["order_total"].description == "Order value after discounts."
    assert orders.columns["order_total"].data_type.startswith("DECIMAL")


def test_singular_tests_over_several_models_are_ignored(target: Path) -> None:
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["nodes"]["test.shop_dbt.order_totals_match_lines"] = {
        "resource_type": "test",
        "depends_on": {"nodes": ["model.shop_dbt.fct_orders", "model.shop_dbt.fct_order_lines"]},
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert load_dbt_artifacts(target).warnings == []


@pytest.mark.parametrize(
    ("data_type", "kind"), [("DATE", "date"), ("DATETIME", "timestamp"), ("TIMESTAMP", "timestamp")]
)
def test_time_kind_follows_the_catalog_type(target: Path, data_type: str, kind: str) -> None:
    path = target / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["nodes"]["model.shop_dbt.fct_orders"]["columns"]["ordered_at"]["type"] = data_type
    path.write_text(json.dumps(catalog), encoding="utf-8")

    (orders,) = suggest_models_from_dbt(load_dbt_artifacts(target), ["fct_orders"])
    assert orders["upsert_model"]["times"]["ordered_at"]["kind"] == kind


@pytest.mark.parametrize("location", ["config", "unrendered_config", "kwargs_config", "kwargs"])
def test_scoped_key_tests_do_not_prove_a_full_relation_key(target: Path, location: str) -> None:
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    test_ids = [f"test.shop_dbt.{kind}_fct_orders_order_id" for kind in ("unique", "not_null")]
    for test_id in test_ids:
        node = manifest["nodes"][test_id]
        if location == "kwargs_config":
            node["test_metadata"]["kwargs"]["config"] = {"where": "is_current = true"}
        elif location == "kwargs":
            node["test_metadata"]["kwargs"]["where"] = "is_current = true"
        else:
            node[location] = {"where": "is_current = true"}
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)
    orders = project.find("fct_orders")
    (suggestion,) = suggest_models_from_dbt(project, ["fct_orders"])
    items, skipped, _ = dbt_import_models(project, ["fct_orders"])

    assert orders.primary_key == []
    assert suggestion["primary_key"] is None
    assert suggestion["upsert_model"]["primary_key"] == []
    assert items == [] and len(skipped) == 1
    assert {warning["test"] for warning in project.warnings} == set(test_ids)
    assert all("row filter" in warning["reason"] for warning in project.warnings)
    # Unfiltered tests on other models and a declared contract remain authoritative.
    assert project.find("dim_products").primary_key == ["product_id"]
    assert project.find("dim_customers").primary_key_source == "contract"


@pytest.mark.parametrize(
    ("test_id", "expected"),
    [
        ("test.shop_dbt.unique_combination_of_columns_fct_order_lines_rows", "composite_key"),
        ("test.shop_dbt.relationships_fct_orders_customer_id", "relationship"),
        ("test.shop_dbt.accepted_values_fct_orders_status", "domain"),
        ("test.shop_dbt.not_null_fct_orders_ordered_at", "non_null_confidence"),
    ],
)
def test_scoped_tests_do_not_promote_composite_keys_links_or_column_facts(
    target: Path, test_id: str, expected: str
) -> None:
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["nodes"][test_id]["config"] = {"where": "is_current = true"}
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)
    (orders,) = suggest_models_from_dbt(project, ["fct_orders"])
    (lines,) = suggest_models_from_dbt(project, ["fct_order_lines"])
    assert project.warnings == [
        {
            "test": test_id,
            "reason": "dbt test has a row filter; its scoped result cannot describe the full relation",
        }
    ]
    if expected == "composite_key":
        assert lines["primary_key"] is None
    elif expected == "relationship":
        assert "customer_id" not in _by(orders["foreign_keys"], "column")
        assert "store_id" in _by(orders["foreign_keys"], "column")
    elif expected == "domain":
        assert "values" not in _by(orders["dimensions"], "column")["status"]
        assert "domain" not in orders["upsert_model"]["dimensions"]["status"]
    else:
        assert _by(orders["times"], "column")["ordered_at"]["confidence"] == "medium"
    assert orders["primary_key"]["columns"] == ["order_id"]


def _relationship_manifest(target: Path) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    test_id = "test.shop_dbt.relationships_fct_orders_customer_id"
    return path, manifest, test_id, manifest["nodes"][test_id]


@pytest.mark.parametrize("attachment", ["missing", "null"])
@pytest.mark.parametrize("reverse", [False, True])
def test_unattached_relationship_uses_manifest_target_identity_not_dependency_order(
    target: Path, attachment: str, reverse: bool
) -> None:
    path, manifest, _, test = _relationship_manifest(target)
    if attachment == "missing":
        test.pop("attached_node")
    else:
        test["attached_node"] = None
    depends = ["model.shop_dbt.fct_orders", "model.shop_dbt.dim_customers"]
    test["depends_on"]["nodes"] = list(reversed(depends)) if reverse else depends
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)
    orders = project.find("fct_orders")
    customers = project.find("dim_customers")
    assert project.warnings == []
    assert [fk for fk in orders.foreign_keys if fk["to"] == customers.unique_id] == [
        {
            "columns": ["customer_id"],
            "to": customers.unique_id,
            "to_relation": customers.relation,
            "to_columns": ["customer_id"],
            "source": "relationships test",
        }
    ]
    assert customers.foreign_keys == []
    (suggestion,) = suggest_models_from_dbt(project, ["fct_orders"])
    assert suggestion["foreign_keys"][0]["references"]["dbt_unique_id"] == customers.unique_id


CUSTOMERS = "model.shop_dbt.dim_customers"
ORDERS = "model.shop_dbt.fct_orders"
FEED = "source.shop_dbt.raw.customer_feed"


def _to_source(manifest: dict[str, Any], test: dict[str, Any]) -> None:
    manifest["sources"][FEED] = {
        "unique_id": FEED,
        "resource_type": "source",
        "name": "customer_feed",
        "database": "warehouse",
        "schema": "main",
        "identifier": "raw_customers",
        "columns": {"customer_id": {"name": "customer_id"}},
    }
    test["test_metadata"]["kwargs"]["to"] = "source('raw', 'customer_feed')"
    test["depends_on"]["nodes"] = [FEED, ORDERS]


def _to_qualified_ref(manifest: dict[str, Any], test: dict[str, Any]) -> None:
    manifest["nodes"]["model.other.dim_customers"] = {
        **manifest["nodes"][CUSTOMERS],
        "unique_id": "model.other.dim_customers",
    }
    test["test_metadata"]["kwargs"]["to"] = "ref('shop_dbt', 'dim_customers')"
    test["depends_on"]["nodes"] = [CUSTOMERS, ORDERS]


def _depends(*nodes: str):
    def mutate(manifest: dict[str, Any], test: dict[str, Any]) -> None:
        test["depends_on"]["nodes"] = list(nodes)

    return mutate


def _attached_beside_other_dependencies(manifest: dict[str, Any], test: dict[str, Any]) -> None:
    test["attached_node"] = ORDERS
    test["depends_on"]["nodes"] = [CUSTOMERS, "model.shop_dbt.dim_stores"]


def _repeated(manifest: dict[str, Any], test: dict[str, Any]) -> None:
    test["depends_on"]["nodes"] = [ORDERS, CUSTOMERS, ORDERS]
    manifest["nodes"]["test.shop_dbt.relationships_duplicate"] = json.loads(json.dumps(test))


@pytest.mark.parametrize(
    ("mutate", "to", "quiet"),
    [
        (_depends(CUSTOMERS, ORDERS), CUSTOMERS, CUSTOMERS),
        (_to_source, FEED, FEED),
        (_to_qualified_ref, CUSTOMERS, "model.other.dim_customers"),
        (_depends(ORDERS), CUSTOMERS, CUSTOMERS),
        (_attached_beside_other_dependencies, CUSTOMERS, CUSTOMERS),
        # A repeated dependency and a repeated test still make one link.
        (_repeated, CUSTOMERS, CUSTOMERS),
    ],
    ids=[
        "model",
        "source",
        "qualified-ref",
        "single-dependency",
        "explicit-attachment",
        "repeated",
    ],
)
def test_a_relationship_test_links_its_child_once(
    target: Path, mutate, to: str, quiet: str
) -> None:
    path, manifest, _, test = _relationship_manifest(target)
    test.pop("attached_node")
    mutate(manifest, test)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)

    assert project.warnings == []
    assert [fk["to"] for fk in project.find("fct_orders").foreign_keys].count(to) == 1
    assert project.relations[quiet].foreign_keys == []


def test_unresolved_explicit_attachment_is_reported_without_dependency_fallback(
    target: Path,
) -> None:
    path, manifest, test_id, test = _relationship_manifest(target)
    test["attached_node"] = "model.shop_dbt.not_in_manifest"
    test["depends_on"]["nodes"] = ["model.shop_dbt.fct_orders"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)
    assert [warning["test"] for warning in project.warnings] == [test_id]
    assert not any(
        fk["to"] == "model.shop_dbt.dim_customers" for fk in project.find("fct_orders").foreign_keys
    )


@pytest.mark.parametrize(
    "case", ["missing_target", "ambiguous_target", "extra_dependency", "missing_child"]
)
def test_unattached_relationship_reports_unresolved_identity_without_a_false_fk(
    target: Path, case: str
) -> None:
    path, manifest, test_id, test = _relationship_manifest(target)
    test["attached_node"] = None
    test["depends_on"]["nodes"] = [
        "model.shop_dbt.fct_orders",
        "model.shop_dbt.dim_customers",
    ]
    if case == "missing_target":
        test["test_metadata"]["kwargs"]["to"] = "ref('missing_customers')"
    elif case == "ambiguous_target":
        duplicate = dict(manifest["nodes"]["model.shop_dbt.dim_customers"])
        duplicate["unique_id"] = "model.other.dim_customers"
        manifest["nodes"][duplicate["unique_id"]] = duplicate
    elif case == "extra_dependency":
        test["depends_on"]["nodes"].append("model.shop_dbt.dim_stores")
    else:
        test["depends_on"]["nodes"] = ["model.shop_dbt.dim_customers"] * 2 + [
            "model.shop_dbt.unknown"
        ]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)
    assert [warning["test"] for warning in project.warnings] == [test_id]
    assert "missing or ambiguous" in project.warnings[0]["reason"]
    assert not any(
        fk["to"] in {"model.shop_dbt.dim_customers", "model.other.dim_customers"}
        for relation in project.relations.values()
        for fk in relation.foreign_keys
    )


def test_suggestions_prefer_dbt_facts_over_guesses(target: Path) -> None:
    (orders,) = suggest_models_from_dbt(load_dbt_artifacts(target), ["fct_orders"])

    assert orders["primary_key"]["reason"] == "dbt unique and not_null tests"
    status = _by(orders["dimensions"], "column")["status"]
    assert status["confidence"] == "high" and status["values"][0] == "placed"
    assert _by(orders["foreign_keys"], "column")["customer_id"]["reason"] == (
        "dbt relationships test"
    )
    assert _by(orders["times"], "column")["ordered_at"]["confidence"] == "high"  # not_null test
    draft = orders["upsert_model"]
    assert draft["relation"] == "main_marts.fct_orders"
    assert draft["dimensions"]["status"]["domain"] == ["placed", "shipped", "delivered", "returned"]
    assert draft["measures"]["order_total"]["description"] == "Order value after discounts."
    assert draft["description"] == "One row per order."


@pytest.mark.parametrize(
    ("types", "untyped", "dimensions"),
    [
        (
            {"weights": "INTEGER[]", "attrs": "STRUCT(score INTEGER)", "lookup": "MAP(VARCHAR,INTEGER)"},
            {"weights", "attrs", "lookup"},
            set(),
        ),
        # Container words and brackets inside enum labels keep an enum a dimension.
        (
            {
                "category": "ENUM('MAP', 'ARRAY', 'ok')",
                "label": "ENUM('x[]', 'it''s MAP[] DATE INTEGER', 'ok')",
                "labels": "ENUM('MAP', 'ok')[]",
            },
            {"labels"},
            {"category", "label"},
        ),
    ],
    ids=["containers", "enum-labels"],
)  # fmt: skip
def test_dbt_catalog_container_types_stay_unmodeled_in_suggestion_and_import(
    target: Path, types: dict[str, str], untyped: set[str], dimensions: set[str]
) -> None:
    catalog_path = target / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    columns = catalog["nodes"]["model.shop_dbt.fct_orders"]["columns"]
    for index, (name, data_type) in enumerate(types.items(), start=100):
        columns[name] = {"name": name, "type": data_type, "index": index}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    project = load_dbt_artifacts(target)
    (suggestion,) = suggest_models_from_dbt(project, ["fct_orders"])
    items, skipped, _ = dbt_import_models(project, ["fct_orders"])
    server = create_architect_mcp_server(workspace_root=target.parent)
    mcp = _call(
        server, "suggest_models_from_dbt", {"target_dir": "target", "select": ["fct_orders"]}
    )

    assert mcp["ok"] is True and skipped == [] and len(items) == 1
    for result in (suggestion, mcp["models"][0]):
        assert untyped <= set(result["untyped_columns"])
        assert dimensions <= {row["column"] for row in result["dimensions"]}
        assert "order_total" in result["upsert_model"]["measures"]
    for draft in (suggestion["upsert_model"], mcp["models"][0]["upsert_model"], items[0]):
        assert dimensions <= set(draft["dimensions"])
        assert untyped.isdisjoint(draft["dimensions"]) and untyped.isdisjoint(draft["measures"])


def test_by_default_every_model_and_no_seed_is_suggested(target: Path) -> None:
    suggestions = suggest_models_from_dbt(load_dbt_artifacts(target))

    assert len(suggestions) == 10
    assert all(row["dbt_unique_id"].startswith("model.") for row in suggestions)


def test_ephemeral_model_is_described_but_has_no_physical_import_draft(target: Path) -> None:
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["nodes"]["model.shop_dbt.fct_orders"]["config"]["materialized"] = "ephemeral"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    project = load_dbt_artifacts(target)
    (suggestion,) = suggest_models_from_dbt(project, ["fct_orders"])
    items, skipped, _ = dbt_import_models(project, ["dim_customers", "fct_orders"])

    assert suggestion["primary_key"]["columns"] == ["order_id"]
    assert suggestion["materialized"] == "ephemeral"
    assert suggestion["physical_relation"] is False
    assert "CTEs without a warehouse relation" in suggestion["nonphysical_reason"]
    assert suggestion["upsert_model"] is None
    assert [item["model_id"] for item in items] == ["customers"]
    assert [row["dbt_model"] for row in skipped] == ["model.shop_dbt.fct_orders"]
    assert "ephemeral" in skipped[0]["reason"]


def test_a_manifest_without_a_catalog_still_loads(target: Path) -> None:
    (target / "catalog.json").unlink()

    project = load_dbt_artifacts(target)
    (orders,) = suggest_models_from_dbt(project, ["fct_orders"])

    assert project.find("fct_orders").primary_key == ["order_id"]
    assert "ordered_at" in orders["untyped_columns"]  # no types without catalog.json


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda target: (target / "manifest.json").unlink(), "run `dbt build`"),
        (lambda target: (target / "manifest.json").write_text("{", encoding="utf-8"), "JSON"),
    ],
    ids=["missing", "corrupt"],
)
def test_missing_or_corrupt_artifacts_are_reported(target: Path, setup: Any, message: str) -> None:
    setup(target)

    with pytest.raises(SemanticLayerError, match=message):
        load_dbt_artifacts(target)


@pytest.mark.parametrize(
    ("file_name", "field", "value"),
    [
        ("manifest.json", "nodes", []),
        ("manifest.json", "metadata", None),
        ("catalog.json", "sources", "broken"),
    ],
)
def test_malformed_dbt_sections_report_config_errors(
    target: Path, file_name: str, field: str, value: Any
) -> None:
    path = target / file_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SemanticLayerError) as excinfo:
        load_dbt_artifacts(target)
    assert excinfo.value.code == "INVALID_CONFIG"
    assert field in str(excinfo.value)


def _contract_target(target: Path, *, to: str, column_level: bool = False, edit=None) -> None:
    """fct_orders's customer_id link as a contract foreign key to ``to`` instead of a test,
    after ``edit`` changes the manifest."""
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if edit is not None:
        edit(manifest)
    nodes = manifest["nodes"]
    for unique_id, node in list(nodes.items()):
        if (
            node.get("resource_type") == "test"
            and node.get("attached_node") == "model.shop_dbt.fct_orders"
            and node.get("column_name") == "customer_id"
            and node.get("test_metadata", {}).get("name") == "relationships"
        ):
            del nodes[unique_id]
    constraint = {"type": "foreign_key", "to": to, "to_columns": ["customer_id"]}
    entry = nodes["model.shop_dbt.fct_orders"]
    if column_level:
        entry.setdefault("columns", {}).setdefault("customer_id", {"name": "customer_id"})
        entry["columns"]["customer_id"].setdefault("constraints", []).append(constraint)
    else:
        entry.setdefault("constraints", []).append({**constraint, "columns": ["customer_id"]})
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _same_named_model(manifest: dict[str, Any]) -> None:
    other = dict(manifest["nodes"][CUSTOMERS])
    other.update(
        {"unique_id": "model.other.dim_customers", "schema": "other", "alias": "other_customers"}
    )
    manifest["nodes"][other["unique_id"]] = other


def _served_alias(manifest: dict[str, Any]) -> None:
    alias = {"database": "analytics", "schema": "served", "alias": "customers_v2"}
    manifest["nodes"][CUSTOMERS].update(alias)


def _keyed_source(manifest: dict[str, Any]) -> None:
    manifest["sources"][FEED] = {
        "unique_id": FEED,
        "resource_type": "source",
        "name": "customer_feed",
        "database": "warehouse",
        "schema": "main",
        "identifier": "raw_customers",
        "columns": {
            "customer_id": {"name": "customer_id", "constraints": [{"type": "primary_key"}]}
        },
    }


SERVED = "analytics.served.customers_v2"


@pytest.mark.parametrize(
    ("edit", "to", "column_level", "target_id", "relation"),
    [
        (None, "ref('dim_customers')", False, CUSTOMERS, "main_marts.dim_customers"),
        (None, "ref('dim_customers')", True, CUSTOMERS, "main_marts.dim_customers"),
        (None, "ref('shop_dbt', 'dim_customers')", False, CUSTOMERS, "main_marts.dim_customers"),
        (None, "ref('shop_dbt', 'dim_customers')", True, CUSTOMERS, "main_marts.dim_customers"),
        # A package-qualified ref picks its package's model over a same-named one.
        (_same_named_model, "ref('shop_dbt', 'dim_customers')", False, CUSTOMERS,
         "main_marts.dim_customers"),
        # The target relation is the manifest's database, schema and alias.
        (_served_alias, "ref('dim_customers')", False, CUSTOMERS, SERVED),
        (_served_alias, SERVED, False, CUSTOMERS, SERVED),
        # A source target is the source's manifest identity.
        (_keyed_source, "source('raw', 'customer_feed')", False, FEED, "raw_customers"),
    ],
    ids=["ref-model", "ref-column", "qualified-model", "qualified-column", "same-named",
         "alias-ref", "alias-relation", "source"],
)  # fmt: skip
def test_contract_foreign_keys_resolve_to_the_manifest_identity(
    target: Path, edit, to: str, column_level: bool, target_id: str, relation: str
) -> None:
    _contract_target(target, to=to, column_level=column_level, edit=edit)

    project = load_dbt_artifacts(target)
    foreign_key = next(
        fk for fk in project.find("fct_orders").foreign_keys if fk["source"] == "contract"
    )
    assert (foreign_key["to"], foreign_key["to_relation"]) == (target_id, relation)
    assert foreign_key["to_columns"] == ["customer_id"]
    items, skipped, _ = dbt_import_models(project, [target_id, "fct_orders"])
    assert skipped == []
    orders = next(item for item in items if item["model_id"] == "orders")
    (reference,) = [row for row in orders["references"] if row["relation"] == relation]
    assert reference == {
        "relation": relation,
        "columns": ["customer_id"],
        "to_columns": ["customer_id"],
        "target_dbt_unique_id": target_id,
        # An aliased target's entity is named after its alias.
        "entity": reference["entity"] if edit is _served_alias else "customer",
    }


def test_an_unknown_or_ambiguous_model_is_reported(target: Path) -> None:
    project = load_dbt_artifacts(target)

    with pytest.raises(SemanticLayerError, match="not in the manifest"):
        project.find("fct_refunds")


def _call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools[name].annotations is not None
            assert tools[name].annotations.readOnlyHint is True
            result = await session.call_tool(name, arguments)
            return dict(result.structuredContent or {})

    return asyncio.run(run())


def test_mcp_session_suggests_models_from_the_dbt_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb")
    write_dbt_artifacts(db_path, tmp_path / "dbt" / "target")
    server = create_architect_mcp_server(workspace_root=tmp_path)
    monkeypatch.setattr(architect_mcp, "MAX_UNSELECTED_DBT_SUGGESTIONS", 2)

    result = _call(
        server,
        "suggest_models_from_dbt",
        {"target_dir": "dbt/target", "select": ["fct_orders", "fct_order_lines", "dim_stores"]},
    )
    unselected = _call(server, "suggest_models_from_dbt", {"target_dir": "dbt/target"})

    assert len(unselected["models"]) == 2 and unselected["truncated"] is True
    assert unselected["hint"] == "narrow with select"
    assert result["ok"] is True and result["truncated"] is False, result
    assert result["dbt_project"] == "shop_dbt"
    models = _by(result["models"], "relation")
    assert models["main_marts.fct_order_lines"]["primary_key"]["columns"] == [
        "order_id",
        "line_number",
    ]


def test_mcp_dbt_paths_stay_inside_the_workspace(tmp_path: Path) -> None:
    db_path = build_dbt_warehouse(tmp_path / "outside" / "warehouse.duckdb")
    write_dbt_artifacts(db_path, tmp_path / "outside" / "target")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = create_architect_mcp_server(workspace_root=workspace)

    result = _call(
        server, "suggest_models_from_dbt", {"target_dir": str(tmp_path / "outside" / "target")}
    )

    assert result["ok"] is False and "workspace root" in result["error"]["message"]
    json.dumps(result)  # the error payload is serializable
