"""Authoring choices that must be explicit before a draft can change an answer."""

from __future__ import annotations

import asyncio
import json
import shutil

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.dbt_artifacts import dbt_import_models, load_dbt_artifacts
from semantic_rails.errors import SemanticLayerError
from semantic_rails.route_census import resolve_pairs
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_route_census import _files_and_receipts


@pytest.fixture(autouse=True)
def _external_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _package(root, sql, entities=(), models=None):
    package = root / "shop"
    (package / "data").mkdir(parents=True)
    with duckdb.connect(str(package / "data/warehouse.duckdb")) as connection:
        connection.execute(
            "CREATE TABLE probe_items (probe_id VARCHAR PRIMARY KEY); "
            "INSERT INTO probe_items VALUES ('one');"
        )
        connection.execute(sql)
    entities = [*entities, ("probe_item", "probe_items", ["probe_id"])]
    models = {
        **(models or {}),
        "probe_item": {
            "measures": {"probe_count": {"kind": "entity_count", "entity_key": "probe_id"}}
        },
    }
    _write(
        package / "metrics/core.yml",
        {
            "metrics": {
                "probe_count": {
                    "kind": "aggregate",
                    "measure": "measure.shop.probe_count",
                    "value_type": "count",
                    "description": "Count of probe items",
                }
            }
        },
    )
    _write(
        package / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "shop",
                "namespace": "shop",
                "warehouse": "duckdb",
                "default_db": "data/warehouse.duckdb",
                "seed": {"kind": "external"},
            },
            "defaults": {"relationship": {"traversal": ["forward", "reverse"]}},
        },
    )
    _write(
        package / "graph.yml",
        {
            "graph": {
                "entities": {
                    entity: {"model": table, "key": keys} for entity, table, keys in entities
                }
            }
        },
    )
    for entity, table, _ in entities:
        _write(
            package / "models" / f"{table}.yml",
            {
                "model": {
                    "id": table,
                    "relation": table,
                    "entities": {entity: {}},
                    **(models or {}).get(entity, {}),
                }
            },
        )
    return package


def _call(package, tool, **arguments):
    async def run():
        server = create_architect_mcp_server(workspace_root=package.parent)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(tool, {"project_path": str(package), **arguments})
            assert not result.isError, result
            return dict(result.structuredContent or {})

    return asyncio.run(run())


def _gold(package, sql):
    with duckdb.connect(str(package / "data/warehouse.duckdb"), read_only=True) as connection:
        return dict(connection.execute(sql).fetchall())


def _aggregate(column):
    return {
        "kind": "aggregate",
        "expr": column,
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
    }


def _query(package, measure, *, grouping=None, monthly=False, route=None):
    query = {"select": [{"expression": f"measure.shop.{measure}", "as": "v"}]}
    if grouping:
        config = load_package_config(str(package))
        dimension = next(row.id for row in config.dimensions if row.column == grouping)
        query["group_by"] = [dimension]
    if monthly:
        query["time"] = {
            "grain": "month",
            "temporal_role": load_package_config(str(package)).temporal_roles[0].id,
        }
    if route:
        query["routes"] = [route]
    runtime = Runtime.from_path(str(package))
    try:
        rows = runtime.query(query)["rows"]
    finally:
        runtime.close()
    return {row[dimension]: row["v"] for row in rows} if grouping else [row["v"] for row in rows]


@pytest.mark.parametrize(
    ("tool", "role", "reverse_only", "keep", "pinned", "unique"),
    [
        pytest.param(
            "upsert_relationship",
            role,
            reverse_only,
            keep,
            pinned,
            unique,
            id=f"{role_id}-{keep_id}-{pin_id}-{unique_id}",
        )
        for role, reverse_only, role_id in [
            ("", False, "unnamed"),
            ("buyer", False, "named"),
            ("buyer", True, "named-with-reverse-only-recipient"),
        ]
        for keep, keep_id in [(False, "plain"), (True, "keep")]
        for pinned, pin_id in [(True, "held-pin"), (False, "no-pin")]
        for unique, unique_id in [(True, "functional"), (False, "reverse-many")]
    ]
    + [
        pytest.param(tool, "", True, False, True, True, id=tool)
        for tool in ("upsert_model", "upsert_models", "import_dbt_project")
    ],
)
def test_new_lookup_requires_a_choice_between_buyer_and_refund(
    tmp_path, tool, role, reverse_only, keep, pinned, unique
):
    keys = ["customer_id"] if unique else ["customer_id", "store_id"]
    package = _package(
        tmp_path,
        f"""
        CREATE TABLE customers(customer_id INTEGER, store_id INTEGER, buyer VARCHAR,
                               PRIMARY KEY ({", ".join(keys)}));
        CREATE TABLE sales(sale_id INTEGER PRIMARY KEY, customer_id INTEGER,
                           recipient_id INTEGER, store_id INTEGER, amount INTEGER);
        CREATE TABLE refunds(refund_id INTEGER PRIMARY KEY,
                             sale_id INTEGER {"UNIQUE" if unique else ""} NOT NULL,
                             customer_id INTEGER NOT NULL, store_id INTEGER);
        INSERT INTO customers VALUES (1, 1, 'Alice'), (2, 1, 'Bob');
        INSERT INTO sales VALUES (10, 1, 2, 1, 100), (11, 2, 1, 1, 60);
        INSERT INTO refunds VALUES (100, 10, 2, 1);
        """,
        [
            ("customer", "customers", keys),
            ("sale", "sales", ["sale_id"]),
            ("refund", "refunds", ["refund_id"]),
        ],
        {
            "customer": {"dimensions": {"buyer": {"kind": "categorical"}}},
            "sale": {"measures": {"amount": _aggregate("amount")}},
        },
    )
    project = ArchitectProject(package, workspace_root=tmp_path)
    graph_path = package / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["entities"]["sale"]["as"] = "entity.sale_record"
    _write(graph_path, graph)
    project.upsert_relationship(
        from_entity="refund",
        to_entity="sale",
        columns=["sale_id"],
        cardinality="one_to_one" if unique else "many_to_one",
    )
    project.upsert_relationship(from_entity="refund", to_entity="customer", columns=keys)
    pair = ("entity.sale_record", "entity.shop_customer")
    detour = ["relationship.refunds_sale", "relationship.refunds_customer"]
    if pinned:
        project.record_route_decision(
            source_entity=pair[0], target_entity=pair[1], relationship_path=detour
        )
    direct = [f"relationship.{role or 'sales_customer'}"]
    if reverse_only:
        graph = yaml.safe_load(graph_path.read_text())
        graph["graph"].setdefault("relationships", {})["recipient"] = {
            "entities": ["sale", "customer"],
            "cardinality": "many_to_one",
            "allowed_directions": ["reverse"],
            "via": ["recipient_id", *keys[1:]],
        }
        _write(graph_path, graph)
        if role and unique and pinned:
            # Hand-authored YAML can deliberately keep the refund path. Compare it with
            # the same package without the recipient, and with a query choosing the buyer.
            authored = tmp_path / "authored" / package.name
            shutil.copytree(package, authored)
            graph["graph"]["relationships"][role] = {
                "entities": ["sale", "customer"],
                "cardinality": "many_to_one",
                "via": keys,
            }
            for recipient in (True, False):
                if not recipient:
                    del graph["graph"]["relationships"]["recipient"]
                _write(authored / "graph.yml", graph)
                assert resolve_pairs(load_package_config(str(authored)), [pair])[
                    pair
                ].path == tuple(detour)
                assert _query(authored, "amount", grouping="buyer") == {"Bob": 100, None: 60}
                assert _query(
                    authored,
                    "amount",
                    grouping="buyer",
                    route={"from": pair[0], "to": pair[1], "via": direct},
                ) == {"Alice": 100, "Bob": 60}
            authored_project = ArchitectProject(authored, workspace_root=tmp_path)
            repeated = authored_project.upsert_relationship(
                from_entity="sale", to_entity="customer", name=role, columns=keys
            ).report
            assert repeated["ok"], repeated.get("errors")
    revision, files = project.revision(), _files_and_receipts(project)
    model_args = {
        "model_id": "sales",
        "entity_key": "sale",
        "relation": "sales",
        "primary_key": ["sale_id"],
        "entities": {"customer": {}},
    }
    if tool == "import_dbt_project":
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "metadata": {"project_name": "shop", "adapter_type": "duckdb"},
                    "nodes": {
                        "model.shop.sales": {
                            "resource_type": "model",
                            "name": "sales",
                            "schema": "main",
                            "columns": {
                                "sale_id": {"data_type": "INTEGER"},
                                "customer_id": {"data_type": "INTEGER"},
                            },
                            "constraints": [
                                {"type": "primary_key", "columns": ["sale_id"]},
                                {"type": "foreign_key", "columns": keys, "to": "customers"},
                            ],
                        }
                    },
                }
            )
        )
    for dry_run in (True, False):
        mutation = {
            "dry_run": dry_run,
            "expected_revision": revision,
            "idempotency_key": "relate-buyer",
        }
        if tool == "upsert_model":
            reply = _call(package, tool, **model_args, **mutation)
        elif tool == "import_dbt_project":
            reply = _call(package, tool, manifest_path=str(manifest), select=["sales"], **mutation)
        else:
            with pytest.raises(SemanticLayerError) as refusal:
                if tool == "upsert_models":
                    project.upsert_models([model_args], **mutation)
                else:
                    project.upsert_relationship(
                        from_entity=pair[0],
                        to_entity="customer",
                        columns=keys,
                        name=role,
                        keep_existing_routes=keep,
                        **mutation,
                    )
            error = refusal.value
            reply = {
                "ok": False,
                "error": {"code": error.code, "message": str(error), "details": error.details},
            }
        assert not reply["ok"]
        error = reply["error"]
        assert error["code"] == "ROUTE_DECISION_NOT_RECORDED"
        assert error["message"] == (
            "Choose the route from Sale to Customer. The direct relationship uses "
            f"{', '.join(keys)}. The other outcome is "
            + (
                "the Customer of the Sale's Refund"
                if unique
                else "the Customer of any of the Sale's Refunds"
            )
            + ". Save the relationship and the affected graph.path_preferences rows together "
            "with write_project_files."
        )
        assert error["details"]["direct_path"] == direct
        assert error["details"]["previous"] == {"relationship_path": detour}
        assert error["details"]["selected"] == {
            "relationship_path": detour if pinned or keep else direct
        }
        assert (project.revision(), _files_and_receipts(project)) == (revision, files)

    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["path_preferences"] = [
        {"source_entity": pair[0], "target_entity": pair[1], "relationship_path": direct},
        {"source_entity": pair[1], "target_entity": pair[0], "relationship_path": direct},
        {
            "source_entity": pair[1],
            "target_entity": "entity.shop_refund",
            "relationship_path": [detour[1]],
        },
        {
            "source_entity": pair[0],
            "target_entity": "entity.shop_refund",
            "relationship_path": [detour[0]],
        },
    ]
    updates = []
    if role:
        graph["graph"].setdefault("relationships", {})[role] = {
            "entities": ["sale", "customer"],
            "cardinality": "many_to_one",
            "via": keys,
            "target": keys,
        }
    else:
        model = yaml.safe_load((package / "models/sales.yml").read_text())
        model["model"]["entities"]["customer"] = {}
        updates.append({"path": "models/sales.yml", "content": yaml.safe_dump(model)})
    written = project.write_files(
        [*updates, {"path": "graph.yml", "content": yaml.safe_dump(graph)}],
        expected_revision=revision,
        idempotency_key="relate-buyer",
    ).report
    assert written["ok"], written.get("errors")
    assert resolve_pairs(load_package_config(str(package)), [pair])[pair].shape() == {
        "relationship_path": direct
    }
    assert (
        _query(package, "amount", grouping="buyer")
        == _gold(
            package,
            "SELECT c.buyer, sum(s.amount) FROM sales s LEFT JOIN customers c "
            "ON c.customer_id = s.customer_id AND c.store_id = s.store_id GROUP BY 1",
        )
        == {"Alice": 100, "Bob": 60}
    )


@pytest.mark.parametrize(
    ("calendar_key", "via_python", "wrong_key", "authored"),
    [
        ("day", False, False, ""),
        ("day_key", False, False, ""),
        ("day_key", True, False, ""),
        ("day", False, True, ""),
        ("day", False, True, "retained-link"),
        ("day", False, False, "bridge-disabled"),
        ("day", True, False, "edited-measures"),
    ],
    ids=[
        "shorthand",
        "surrogate-and-replay",
        "python-surrogate",
        "wrong-manifest-key",
        "retained-calendar",
        "bridge-disabled",
        "edited-python-measures",
    ],
)
def test_dbt_import_uses_the_package_calendar_key_for_periodic_holds(
    tmp_path, calendar_key, via_python, wrong_key, authored
):
    package = _package(
        tmp_path,
        f"""
        CREATE TABLE calendar(day_key INTEGER, day DATE, legacy_day DATE,
                              PRIMARY KEY({calendar_key}));
        CREATE TABLE inventory_levels(product_id INTEGER,
                                      observed_on {"DATE" if calendar_key == "day" else "INTEGER"},
                                      recorded_date DATE,
                                      quantity INTEGER,
                                      PRIMARY KEY(product_id, observed_on));
        CREATE TABLE sales(sale_id INTEGER PRIMARY KEY, quantity INTEGER);
        CREATE TABLE reserves(reserve_id INTEGER PRIMARY KEY, quantity INTEGER);
        INSERT INTO calendar VALUES (1, '2026-01-03', '2026-01-02'),
                                    (2, '2026-01-27', '2026-01-26');
        INSERT INTO inventory_levels
        SELECT 1, {calendar_key}, day, 7 FROM calendar WHERE day_key = 1 UNION ALL
        SELECT 1, {calendar_key}, day, 17 FROM calendar WHERE day_key = 2;
        INSERT INTO sales VALUES (1, 10), (2, 20);
        INSERT INTO reserves VALUES (1, 30);
        """,
        [("calendar", "calendar", [calendar_key]), ("reserve", "reserves", ["reserve_id"])],
        {
            "calendar": {"times": {"day": {"kind": "date", "default": True}}},
            "reserve": {"measures": {"sale_quantity": _aggregate("quantity")}},
        },
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "metadata": {"project_name": "shop", "adapter_type": "duckdb"},
                "nodes": {
                    "model.shop.inventory_levels": {
                        "resource_type": "model",
                        "name": "inventory_levels",
                        "schema": "main",
                        "columns": {
                            "product_id": {"data_type": "INTEGER"},
                            "observed_on": {
                                "data_type": "DATE" if calendar_key == "day" else "INTEGER"
                            },
                            "recorded_date": {"data_type": "DATE"},
                            "quantity": {"data_type": "INTEGER"},
                        },
                        "constraints": [
                            {"type": "primary_key", "columns": ["product_id", "observed_on"]},
                            {
                                "type": "foreign_key",
                                "columns": ["observed_on"],
                                "to": "calendar",
                                **({"to_columns": ["legacy_day"]} if wrong_key else {}),
                            },
                        ],
                    },
                    "model.shop.sales": {
                        "resource_type": "model",
                        "name": "sales",
                        "schema": "main",
                        "columns": {
                            "sale_id": {"data_type": "INTEGER"},
                            "quantity": {"data_type": "INTEGER"},
                        },
                        "constraints": [{"type": "primary_key", "columns": ["sale_id"]}],
                    },
                    **(
                        {
                            "model.shop.calendar": {
                                "resource_type": "model",
                                "name": "calendar",
                                "schema": "main",
                                "columns": {"legacy_day": {"data_type": "DATE"}},
                                "constraints": [{"type": "primary_key", "columns": ["legacy_day"]}],
                            }
                        }
                        if wrong_key
                        else {}
                    ),
                },
            }
        )
    )
    project = ArchitectProject(package, workspace_root=tmp_path)
    if authored:
        kwargs = {}
        if authored == "edited-measures":
            kwargs["measures"] = {"quantity": {**_aggregate("quantity"), "default_agg": "max"}}
            kwargs["times"] = {
                "recorded_date": {"kind": "date", "column": "recorded_date", "default": True}
            }
        assert project.upsert_model(
            model_id="inventory_levels",
            entity_key="inventory_level",
            relation="inventory_levels",
            primary_key=["product_id", "observed_on"],
            entities={"calendar": {"expr": "observed_on"}} if authored == "retained-link" else {},
            **kwargs,
        ).report["ok"]
        if authored == "bridge-disabled":
            authored_path = package / "models/core/inventory_levels.yml"
            doc = yaml.safe_load(authored_path.read_text())
            doc["model"]["entities"]["bridge"] = False
            _write(authored_path, doc)
    select = ["inventory_levels"]
    if calendar_key == "day_key" and not via_python:
        # The sale draft cannot use sale_quantity, which an existing model already owns.
        select.append("sales")
    mutation = {"expected_revision": project.revision(), "idempotency_key": "import-levels"}
    if via_python:
        items, _, _ = dbt_import_models(load_dbt_artifacts(manifest_path=manifest), select)
        if authored == "edited-measures":
            del items[0]["measures"]["quantity"]
        report = project.upsert_models(items, **mutation).report
    else:
        args = {"manifest_path": str(manifest), "select": select, "full_report": True, **mutation}
        report = _call(package, "import_dbt_project", **args)
        assert report["ok"], report
        replay = _call(package, "import_dbt_project", **args)
        assert replay["ok"] and replay["idempotent_replay"], replay
        assert replay["models"] == report["models"]
    assert report["ok"], report
    model = report["models"][0]
    path = package / model["target_file"]
    spec = yaml.safe_load(path.read_text())["model"]
    if wrong_key:
        assert not report["references"]
        assert report["skipped_references"][0]["reason"] == (
            "it points at ['legacy_day'], not calendar's key ['day']"
        )
    else:
        assert report["references"] == [
            {"model": "inventory_levels", "entity": "calendar", "columns": ["observed_on"]}
        ]
    if (not wrong_key and not authored) or authored == "retained-link":
        assert spec["entities"]["calendar"] == {"expr": "observed_on"}
        assert model["held_measures"] == {
            "quantity": "Choose whether quantity is a total for each period or a balance. For a "
            "balance, declare the snapshot date and the columns that identify the same series "
            "across dates."
        }
        assert "quantity" not in spec["measures"]
        if "sales" not in select:
            with pytest.raises(SemanticLayerError) as refusal:
                _query(package, "quantity", monthly=True)
            assert refusal.value.code == "OBJECT_NOT_FOUND"
            assert str(refusal.value) == "Unknown measure 'measure.shop.quantity'"
        else:
            assert _query(package, "quantity") == [30]
    else:
        assert "held_measures" not in model
        runtime = Runtime.from_path(str(package))
        try:
            months = runtime.query(
                {
                    "select": [{"expression": "measure.shop.quantity", "as": "quantity"}],
                    "time": {
                        "grain": "month",
                        "temporal_role": "temporal_role.shop_inventory_level_recorded_date",
                    },
                }
            )["rows"]
        finally:
            runtime.close()
        assert [row["quantity"] for row in months] == (
            [17] if authored == "edited-measures" else [24]
        )
    assert (
        _gold(
            package,
            "SELECT date_trunc('month', recorded_date), arg_max(quantity, recorded_date) "
            "FROM inventory_levels GROUP BY 1",
        ).popitem()[1]
        == 17
    )


def test_link_suggestions_hold_composite_components_before_matching_values(tmp_path):
    package = _package(
        tmp_path,
        """
        CREATE SCHEMA other;
        CREATE TABLE purchase_lines(order_no INTEGER, line_no INTEGER, item VARCHAR,
                                    PRIMARY KEY(order_no,line_no));
        CREATE TABLE sales_orders(order_id INTEGER PRIMARY KEY, sales_customer VARCHAR);
        CREATE TABLE other.sales_orders(order_id INTEGER PRIMARY KEY);
        CREATE TABLE line_numbers(line_no INTEGER PRIMARY KEY);
        CREATE TABLE employees(employee_id INTEGER PRIMARY KEY);
        CREATE TABLE receipts(
          receipt_id INTEGER PRIMARY KEY, purchase_order_id INTEGER, purchase_line_no INTEGER,
          employee_id INTEGER, amount INTEGER,
          FOREIGN KEY(purchase_order_id,purchase_line_no) REFERENCES purchase_lines(order_no,line_no),
          FOREIGN KEY(purchase_line_no) REFERENCES line_numbers(line_no)
        );
        INSERT INTO purchase_lines VALUES (1, 2, 'Coat'), (2, 1, 'Hat');
        INSERT INTO sales_orders VALUES (1, 'Alice'), (2, 'Bob');
        INSERT INTO other.sales_orders VALUES (1);
        INSERT INTO line_numbers VALUES (1), (2);
        INSERT INTO employees VALUES (1);
        INSERT INTO receipts VALUES (100, 1, 2, 1, 100), (101, 2, 1, 1, 60), (102, NULL, 1, 1, 5);
        """,
        [
            ("receipt", "receipts", ["receipt_id"]),
            ("purchase_line", "purchase_lines", ["order_no", "line_no"]),
            ("sales_order", "sales_orders", ["order_id"]),
            ("other_sale", "other.sales_orders", ["order_id"]),
            ("line_number", "line_numbers", ["line_no"]),
            ("employee", "employees", ["employee_id"]),
        ],
        {
            "purchase_line": {"dimensions": {"item": {"kind": "categorical"}}},
            "receipt": {"measures": {"amount": _aggregate("amount")}},
        },
    )
    declared = {
        "columns": ["purchase_order_id", "purchase_line_no"],
        "references": {"relation": "purchase_lines", "columns": ["order_no", "line_no"]},
    }
    for columns in (None, ["purchase_order_id", "purchase_line_no"]):
        args = {"relation": "receipts", **({"columns": columns} if columns else {})}
        report = _call(package, "suggest_links", **args, full_report=True)
        assert report["ok"], report
        assert report["composite_foreign_keys"] == [declared]
        held = [
            row
            for row in report["not_proposed"]
            if row["target"]["entity"] in {"sales_order", "other_sale"}
        ]
        expected_columns = columns or ["purchase_order_id"]
        assert {(row["column"], row["target"]["relation"]) for row in held} == {
            (column, relation)
            for column in expected_columns
            for relation in ("sales_orders", "other.sales_orders")
        }
        for row in held:
            assert row["declared_foreign_keys"] == [declared]
            assert row["reason"] == (
                f"{row['column']} is part of a declared composite foreign key. Choose the "
                "target and supply all source columns in its key order with upsert_relationship."
            )
            assert {"evidence", "upsert_relationship"}.isdisjoint(row)
        targets = {(row["column"], row["target"]["entity"]) for row in report["proposals"]}
        assert ("purchase_line_no", "line_number") in targets
        if columns is None:
            assert ("employee_id", "employee") in targets
        assert all(
            row["target"]["entity"] not in {"sales_order", "other_sale"}
            or row["column"] == "employee_id"
            for row in report["proposals"] + report["below_threshold"]
        )
        offset = len(report["proposals"]) + len(report["below_threshold"])
        page = _call(package, "suggest_links", **args, offset=offset, limit=1)
        assert page["counts"] == report["counts"]
        assert page["not_proposed"] == report["not_proposed"][:1]
    project = ArchitectProject(package, workspace_root=tmp_path)
    with pytest.raises(SemanticLayerError) as refusal:
        _query(package, "amount", grouping="item")
    assert refusal.value.code == "PATH_NOT_FOUND"
    assert str(refusal.value) == (
        "No path from 'entity.shop_receipt' to 'entity.shop_purchase_line': "
        "'entity.shop_receipt' and 'entity.shop_purchase_line' share no key column."
    )
    assert project.upsert_relationship(
        from_entity="receipt", to_entity="purchase_line", columns=declared["columns"]
    ).report["ok"]
    assert (
        _query(package, "amount", grouping="item")
        == _gold(
            package,
            "SELECT l.item, sum(r.amount) FROM receipts r LEFT JOIN purchase_lines l "
            "ON l.order_no = r.purchase_order_id AND l.line_no = r.purchase_line_no GROUP BY 1",
        )
        == {"Coat": 100, "Hat": 60, None: 5}
    )


@pytest.mark.parametrize("target", ["purchase_slots", '"purchase.slots"'])
@pytest.mark.parametrize("repeated", [False, True], ids=["suggest_model", "suggest_one_per_key"])
def test_model_draft_holds_composite_calendar_component(tmp_path, repeated, target):
    package = _package(
        tmp_path,
        f"""
        CREATE TABLE calendar(day_key INTEGER PRIMARY KEY, day DATE);
        CREATE TABLE {target}(day_key INTEGER, slot_no INTEGER, PRIMARY KEY(day_key, slot_no));
        CREATE TABLE receipts(receipt_id INTEGER, day_key INTEGER, slot_no INTEGER, quantity INTEGER,
          FOREIGN KEY(day_key, slot_no) REFERENCES {target}(day_key, slot_no));
        INSERT INTO calendar VALUES (1, '2026-01-01'), (2, '2026-01-02');
        INSERT INTO {target} VALUES (1, 2), (2, 1);
        INSERT INTO receipts VALUES (101, 1, 2, 50), (102, 2, 1, 60);
        """
        + (
            "INSERT INTO receipts SELECT * FROM receipts;"
            if repeated
            else "ALTER TABLE receipts ADD PRIMARY KEY (receipt_id);"
        ),
        [
            ("day", "calendar", ["day_key"]),
        ],
        {"day": {"times": {"day": {"column": "day", "kind": "date", "default": True}}}},
    )
    if repeated:
        response = _call(package, "suggest_one_per_key", relation="receipts", key=["receipt_id"])
        suggestion = response["proposal"]
    else:
        response = _call(package, "suggest_model", relation="receipts", full_report=True)
        suggestion = response
    assert response["ok"], response
    assert suggestion["held_links"] == [
        {
            "column": "day_key",
            "target": {"entity": "day", "relation": "calendar", "key": "day_key"},
            "declared_foreign_keys": [
                {
                    "columns": ["day_key", "slot_no"],
                    "references": {"relation": target, "columns": ["day_key", "slot_no"]},
                }
            ],
            "reason": "day_key is part of a declared composite foreign key. Choose the target "
            "and supply all source columns in its key order with upsert_relationship.",
        }
    ]
    draft = suggestion["upsert_model"]
    assert "entities" not in draft
    assert not draft["times"]
    assert "times" not in draft["measures"]["quantity"]


@pytest.mark.parametrize("repeated", [False, True], ids=["suggest_model", "suggest_one_per_key"])
def test_periodic_model_holds_quantity_until_the_author_defines_it(tmp_path, repeated):
    package = _package(
        tmp_path,
        """
        CREATE TABLE inventory_snapshots (
          snapshot_id INTEGER, product_id INTEGER, snapshot_date DATE, quantity_on_hand INTEGER
        );
        INSERT INTO inventory_snapshots VALUES
          (1, 1, '2026-01-03', 3), (2, 2, '2026-01-03', 4),
          (3, 1, '2026-01-27', 7), (4, 2, '2026-01-27', 10);
        """
        + (
            "INSERT INTO inventory_snapshots SELECT * FROM inventory_snapshots;"
            if repeated
            else "ALTER TABLE inventory_snapshots ADD PRIMARY KEY (snapshot_id);"
        ),
    )
    arguments = {"relation": "inventory_snapshots"}
    if repeated:
        response = _call(package, "suggest_one_per_key", **arguments, key=["snapshot_id"])
        assert response["status"] == "proposed", response
        suggestion = response["proposal"]
    else:
        response = _call(package, "suggest_model", **arguments, full_report=True)
        suggestion = response
    assert response["ok"], response
    assert suggestion["held_measures"] == {
        "quantity_on_hand": "Choose whether quantity_on_hand is a total for each period or a "
        "balance. For a balance, declare the snapshot date and the columns that identify the "
        "same series across dates."
    }
    draft = suggestion["upsert_model"]
    assert draft["times"]["snapshot_date"]["class"] == "as_of_time"
    assert draft.get("one_per_key", False) is repeated
    (count,) = draft["measures"]
    assert draft["measures"][count]["kind"] == "entity_count"
    project = ArchitectProject(package, workspace_root=tmp_path)
    written = _call(
        package,
        "upsert_model",
        **draft,
        expected_revision=project.revision(),
        idempotency_key="draft-inventory",
    )
    assert written["ok"], written.get("errors")
    assert _query(package, count, monthly=True) == [4]
    with pytest.raises(SemanticLayerError) as refusal:
        _query(package, "quantity_on_hand", monthly=True)
    assert refusal.value.code == "OBJECT_NOT_FOUND"
    assert "measure.shop.quantity_on_hand" in str(refusal.value)

    for balance in (False, True):
        measure = _aggregate("quantity_on_hand")
        if balance:
            measure["accumulation"] = {"kind": "stock", "snapshot": "end_of_period"}
        explicit = project.upsert_model(
            **{
                **draft,
                "primary_key": ["product_id", "snapshot_date"],
                "measures": {"quantity_on_hand": measure},
            },
            replace=True,
        ).report
        assert explicit["ok"], explicit.get("errors")
        actual = _query(package, "quantity_on_hand", monthly=True)
        predicate = (
            " WHERE snapshot_date = (SELECT max(snapshot_date) FROM snapshots)" if balance else ""
        )
        gold = _gold(
            package,
            "WITH snapshots AS (SELECT DISTINCT * FROM inventory_snapshots) "
            "SELECT 'January', sum(quantity_on_hand) FROM snapshots" + predicate,
        )
        assert actual == [gold["January"]] == ([17] if balance else [24])


@pytest.mark.parametrize("repeated", [False, True], ids=["suggest_model", "suggest_one_per_key"])
def test_calendar_key_in_grain_holds_columns_but_an_order_date_does_not(tmp_path, repeated):
    package = _package(
        tmp_path,
        """
        CREATE TABLE calendar(day_key INTEGER PRIMARY KEY, day DATE);
        CREATE TABLE product_levels(product_id INTEGER, period_id INTEGER, quantity INTEGER,
            FOREIGN KEY(period_id) REFERENCES calendar(day_key));
        CREATE TABLE orders(order_id INTEGER PRIMARY KEY, ordered_on DATE, amount INTEGER);
        INSERT INTO calendar VALUES (1, '2026-01-01'), (2, '2026-01-02');
        INSERT INTO product_levels VALUES (1, 1, 2), (1, 2, 3), (2, 1, 5);
        INSERT INTO orders VALUES (10, '2026-01-01', 50), (11, '2026-01-02', 60);
        """
        + (
            "INSERT INTO product_levels SELECT * FROM product_levels;"
            if repeated
            else "ALTER TABLE product_levels ADD PRIMARY KEY (product_id, period_id);"
        ),
        [("day", "calendar", ["day_key"])],
        {"day": {"times": {"day": {"column": "day", "kind": "date", "default": True}}}},
    )
    if repeated:
        result = _call(
            package,
            "suggest_one_per_key",
            relation="product_levels",
            key=["product_id", "period_id"],
        )["proposal"]
    else:
        result = _call(package, "suggest_model", relation="product_levels", full_report=True)
    assert "quantity" in result["held_measures"]
    assert result["upsert_model"]["measures"] == {}
    orders = _call(package, "suggest_model", relation="orders", full_report=True)
    assert "held_measures" not in orders
    assert orders["upsert_model"]["measures"]["amount"]["accumulation"] == {"kind": "flow"}


@pytest.mark.parametrize(
    ("first", "layout", "composite"),
    [
        ("original", "model", True),
        ("replacement", "model", True),
        ("original", "graph-only", True),
        ("replacement", "graph-override", True),
        ("original", "model", False),
    ],
)
def test_unnamed_write_cannot_retarget_an_existing_role(tmp_path, first, layout, composite):
    key = ["order_id", "line_no"] if composite else ["line_no"]
    second = "original" if first == "replacement" else "replacement"

    def columns(role):
        return [f"{role}_{column}" for column in key]

    package = _package(
        tmp_path,
        f"""
        CREATE TABLE sale_lines(order_id INTEGER, line_no INTEGER, product VARCHAR,
                                PRIMARY KEY ({", ".join(key)}));
        CREATE TABLE exchanges (
          exchange_id INTEGER PRIMARY KEY, original_order_id INTEGER, original_line_no INTEGER,
          replacement_order_id INTEGER, replacement_line_no INTEGER, quantity INTEGER,
          FOREIGN KEY ({", ".join(columns("original"))}) REFERENCES sale_lines({", ".join(key)}),
          FOREIGN KEY ({", ".join(columns("replacement"))}) REFERENCES sale_lines({", ".join(key)})
        );
        INSERT INTO sale_lines VALUES (1, 1, 'Coat'), (1, 2, 'Hat');
        INSERT INTO exchanges VALUES (101, 1, 1, 1, 2, 1);
        """,
        [
            ("sale_line", "sale_lines", key),
            ("exchange", "exchanges", ["exchange_id"]),
        ],
        {
            "sale_line": {"dimensions": {"product": {"kind": "categorical"}}},
            "exchange": {"measures": {"quantity": _aggregate("quantity")}},
        },
    )
    project = ArchitectProject(package, workspace_root=tmp_path)
    pair = {"from_entity": "exchange", "to_entity": "sale_line"}
    if layout != "graph-only":
        assert project.upsert_relationship(**pair, columns=columns(first)).report["ok"]
    if layout != "model":
        graph = yaml.safe_load((package / "graph.yml").read_text())
        graph["graph"]["relationships"] = {
            "exchanges_sale_line": {
                "entities": ["exchange", "sale_line"],
                "via": columns(first),
                "target": key,
                "cardinality": "many_to_one",
            }
        }
        assert project.write_files(
            [{"path": "graph.yml", "content": yaml.safe_dump(graph)}]
        ).report["ok"]

    def expected(role):
        comparisons = " AND ".join(f"s.{column} = e.{role}_{column}" for column in key)
        return _gold(
            package,
            "SELECT s.product, sum(e.quantity) FROM exchanges e JOIN sale_lines s ON "
            f"{comparisons} GROUP BY 1",
        )

    assert _query(package, "quantity", grouping="product") == expected(first)
    revision, files = project.revision(), _files_and_receipts(project)
    for keep, dry_run in [(False, False), (True, False), (False, True), (True, True)]:
        with pytest.raises(SemanticLayerError) as refusal:
            project.upsert_relationship(
                **pair,
                columns=columns(second),
                keep_existing_routes=keep,
                dry_run=dry_run,
                expected_revision=revision,
                idempotency_key="new-role",
            )
        assert refusal.value.code == "INVALID_CONFIG"
        assert str(refusal.value) == (
            f"exchange already links to sale_line using {columns(first)} to {key}. "
            f"The requested mapping uses {columns(second)} to {key}. Give the new "
            "relationship a role name, or update the mapping and confirm affected routes "
            "with write_project_files."
        )
        assert refusal.value.details == {
            "existing_mapping": list(zip(columns(first), key, strict=True)),
            "requested_mapping": list(zip(columns(second), key, strict=True)),
        }
        assert (project.revision(), _files_and_receipts(project)) == (revision, files)
    assert _query(package, "quantity", grouping="product") == expected(first)
    assert project.upsert_relationship(**pair, columns=columns(first)).report["ok"]
    report = project.upsert_relationship(
        **pair,
        columns=columns(second),
        name=second,
        keep_existing_routes=True,
        expected_revision=project.revision(),
        idempotency_key="new-role",
    ).report
    assert report["ok"], report.get("errors")
    assert _query(package, "quantity", grouping="product") == expected(first)
    assert _query(
        package,
        "quantity",
        grouping="product",
        route={
            "from": "entity.shop_exchange",
            "to": "entity.shop_sale_line",
            "via": [f"relationship.{second}"],
        },
    ) == expected(second)


@pytest.mark.parametrize("bridge", [True, False], ids=["different-target", "no-edge"])
def test_new_lookup_must_stage_the_exact_ordered_mapping(tmp_path, bridge):
    package = _package(
        tmp_path,
        """
        CREATE TABLE customers(customer_id INT PRIMARY KEY, retired_id INT UNIQUE, buyer VARCHAR);
        CREATE TABLE sales(sale_id INT PRIMARY KEY, customer_id INT, amount INT);
        INSERT INTO customers VALUES (1, 2, 'Alice'), (2, 1, 'Bob');
        INSERT INTO sales VALUES (10, 1, 100);
        """,
        [("customer", "customers", ["customer_id"]), ("sale", "sales", ["sale_id"])],
        {
            "customer": {"dimensions": {"buyer": {"kind": "categorical"}}},
            "sale": {
                "entities": {"sale": {}, "bridge": bridge},
                "measures": {"amount": _aggregate("amount")},
            },
        },
    )
    if bridge:
        path = package / "package.yml"
        config = yaml.safe_load(path.read_text())
        config["defaults"]["relationship"]["target"] = ["retired_id"]
        _write(path, config)
    project = ArchitectProject(package, workspace_root=tmp_path)
    revision, files = project.revision(), _files_and_receipts(project)
    args = {"from_entity": "sale", "to_entity": "customer", "columns": ["customer_id"]}
    outcome = "the Sale's Customer" if bridge else "a refusal (PATH_NOT_FOUND)"
    for dry_run in (True, False):
        with pytest.raises(SemanticLayerError) as refusal:
            project.upsert_relationship(
                **args,
                dry_run=dry_run,
                expected_revision=revision,
                idempotency_key="sale-customer",
                cardinality="one_to_one" if bridge else "many_to_one",
            )
        assert refusal.value.code == "ROUTE_DECISION_NOT_RECORDED"
        assert str(refusal.value) == (
            "Choose the route from Sale to Customer. The staged package could not create the lookup "
            f"on customer_id. Its outcome is {outcome}. "
            "Save the relationship and the affected graph.path_preferences rows together with "
            "write_project_files."
        )
        assert refusal.value.details["direct_path"] == []
        assert (project.revision(), _files_and_receipts(project)) == (revision, files)
    report = project.upsert_relationship(**args, name="buyer", cardinality="one_to_one").report
    assert report["ok"], report.get("errors")
    assert _query(package, "amount", grouping="buyer") == {"Alice": 100}

    # A named graph edge that explicitly overrides an inherited target keeps that mapping on repeat.
    graph = yaml.safe_load((package / "graph.yml").read_text())
    assert project.upsert_relationship(**args, cardinality="one_to_one").report["ok"]
    current = yaml.safe_load((package / "graph.yml").read_text())
    assert (
        current["graph"]["relationships"]["buyer"]["target"]
        == graph["graph"]["relationships"]["buyer"]["target"]
    )
    assert _query(package, "amount", grouping="buyer") == {"Alice": 100}


@pytest.mark.parametrize(
    "standalone", [False, True], ids=["inline-defaults", "standalone-defaults"]
)
def test_the_package_default_can_make_an_own_clock_as_of(tmp_path, standalone):
    package = _package(
        tmp_path,
        """
        CREATE TABLE product_levels(level_id INT PRIMARY KEY, balance_date DATE, quantity INT);
        INSERT INTO product_levels VALUES (1, '2026-01-03', 4), (2, '2026-01-31', 8);
        """,
        [("product_level", "product_levels", ["level_id"])],
        {"product_level": {"times": {"balance_date": {"kind": "date", "default": True}}}},
    )
    path = package / "package.yml"
    config = yaml.safe_load(path.read_text())
    config["defaults"]["time"] = {"class": "as_of_time"}
    if standalone:
        _write(package / "defaults.yml", {"defaults": config.pop("defaults")})
    _write(path, config)
    reply = _call(package, "suggest_model", relation="product_levels", full_report=True)
    assert reply["ok"], reply
    assert "quantity" in reply["held_measures"]
    assert "quantity" not in reply["upsert_model"]["measures"]


def test_repeating_an_unnamed_lookup_keeps_its_explicit_source_over_the_default(tmp_path):
    package = _package(
        tmp_path,
        """
        CREATE TABLE customers(customer_id INT PRIMARY KEY, buyer VARCHAR);
        CREATE TABLE sales(sale_id INT PRIMARY KEY, buyer_id INT, recipient_id INT, amount INT);
        INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob');
        INSERT INTO sales VALUES (10, 1, 2, 100);
        """,
        [("customer", "customers", ["customer_id"]), ("sale", "sales", ["sale_id"])],
        {
            "customer": {"dimensions": {"buyer": {"kind": "categorical"}}},
            "sale": {"measures": {"amount": _aggregate("amount")}},
        },
    )
    path = package / "package.yml"
    config = yaml.safe_load(path.read_text())
    config["defaults"]["relationship"]["via"] = ["recipient_id"]
    _write(path, config)
    graph_path = package / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["relationships"] = {
        "sales_customer": {
            "entities": ["sale", "customer"],
            "cardinality": "many_to_one",
            "via": ["buyer_id"],
        }
    }
    _write(graph_path, graph)
    project = ArchitectProject(package, workspace_root=tmp_path)
    gold = _gold(
        package,
        "SELECT c.buyer, sum(s.amount) FROM sales s JOIN customers c "
        "ON c.customer_id = s.buyer_id GROUP BY 1",
    )
    assert _query(package, "amount", grouping="buyer") == gold == {"Alice": 100}
    report = project.upsert_relationship(
        from_entity="sale", to_entity="customer", columns=["buyer_id"]
    ).report
    assert report["ok"], report
    assert _query(package, "amount", grouping="buyer") == gold
    entry = yaml.safe_load(graph_path.read_text())["graph"]["relationships"]["sales_customer"]
    assert entry["via"] == ["buyer_id"]


def test_a_reverse_only_lookup_stages_the_mapping_in_the_allowed_direction(tmp_path):
    package = _package(
        tmp_path,
        """
        CREATE TABLE accounts(account_id INT PRIMARY KEY);
        CREATE TABLE profiles(profile_id INT PRIMARY KEY, account_id INT UNIQUE);
        """,
        [("account", "accounts", ["account_id"]), ("profile", "profiles", ["profile_id"])],
    )
    path = package / "package.yml"
    document = yaml.safe_load(path.read_text())
    document["defaults"]["relationship"]["traversal"] = ["reverse"]
    _write(path, document)
    project = ArchitectProject(package, workspace_root=tmp_path)
    args = {
        "from_entity": "profile",
        "to_entity": "account",
        "columns": ["account_id"],
        "cardinality": "one_to_one",
    }
    preview = project.upsert_relationship(**args, dry_run=True).report
    assert preview["ok"] and preview["status"] == "preview"
    written = project.upsert_relationship(**args).report
    assert written["ok"] and written["revision"] == preview["proposed_revision"]
    config = load_package_config(str(package))
    (edge,) = config.relationships
    assert edge.source_columns == edge.target_columns == ["account_id"]
    forward = (edge.source_entity, edge.target_entity)
    reverse = forward[::-1]
    outcomes = resolve_pairs(config, [forward, reverse])
    assert outcomes[forward].shape() == {"refused": "PATH_NOT_FOUND"}
    assert outcomes[reverse].shape() == {"relationship_path": [edge.id]}


def test_named_role_edits_preserve_reverse_only_and_already_ambiguous_pairs(tmp_path):
    package = _package(
        tmp_path,
        """
        CREATE TABLE customers(customer_id INTEGER PRIMARY KEY);
        CREATE TABLE sales(sale_id INTEGER PRIMARY KEY, buyer_id INTEGER, seller_id INTEGER,
                           recipient_id INTEGER, agent_id INTEGER);
        """,
        [("customer", "customers", ["customer_id"]), ("sale", "sales", ["sale_id"])],
    )
    graph_path = package / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["relationships"] = {
        "recipient": {
            "as": "relationship.recipient_v1",
            "entities": ["sale", "customer"],
            "via": ["recipient_id"],
            "cardinality": "many_to_one",
            "allowed_directions": ["reverse"],
        }
    }
    _write(graph_path, graph)
    project = ArchitectProject(package, workspace_root=tmp_path)
    pair = ("entity.shop_sale", "entity.shop_customer")
    assert project.upsert_relationship(
        from_entity="sale", to_entity="customer", name="recipient", columns=["recipient_id"]
    ).report["ok"]
    config = load_package_config(str(package))
    assert resolve_pairs(config, [pair, pair[::-1]])[pair[::-1]].path == (
        "relationship.recipient_v1",
    )
    preview = project.upsert_relationship(
        from_entity="sale",
        to_entity="customer",
        name="buyer__role",
        columns=["buyer_id"],
        keep_existing_routes=True,
        dry_run=True,
    ).report
    assert preview["ok"], preview.get("errors")
    for name in ("buyer", "seller"):
        graph["graph"]["relationships"][name] = {
            "entities": ["sale", "customer"],
            "via": [f"{name}_id"],
            "cardinality": "many_to_one",
        }
    _write(graph_path, graph)
    assert (
        resolve_pairs(load_package_config(str(package)), [pair])[pair].refused == "AMBIGUOUS_PATH"
    )
    assert project.upsert_relationship(
        from_entity="sale", to_entity="customer", name="agent", columns=["agent_id"]
    ).report["ok"]
    assert (
        resolve_pairs(load_package_config(str(package)), [pair])[pair].refused == "AMBIGUOUS_PATH"
    )


@pytest.mark.parametrize("repeat", [False, True], ids=["graph-only", "after-repeat"])
def test_an_unnamed_lookup_repairs_a_stale_mapping_after_a_key_migration(tmp_path, repeat):
    package = _package(
        tmp_path,
        """
        CREATE TABLE customers(store_id INT, customer_id INT, buyer VARCHAR,
                               PRIMARY KEY (store_id, customer_id));
        CREATE TABLE sales(sale_id INT PRIMARY KEY, store_id INT, buyer_id INT, amount INT);
        INSERT INTO customers VALUES (1, 1, 'Alice'), (2, 1, 'Bob');
        INSERT INTO sales VALUES (10, 1, 1, 100), (11, 2, 1, 60);
        """,
        [("customer", "customers", ["customer_id"]), ("sale", "sales", ["sale_id"])],
        {
            "customer": {"dimensions": {"buyer": {"kind": "categorical"}}},
            "sale": {"measures": {"amount": _aggregate("amount")}},
        },
    )
    graph_path = package / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["relationships"] = {
        "sales_customer": {
            "entities": ["sale", "customer"],
            "cardinality": "many_to_one",
            "via": ["buyer_id"],
        }
    }
    _write(graph_path, graph)
    project = ArchitectProject(package, workspace_root=tmp_path)
    if repeat:
        repeated = project.upsert_relationship(
            from_entity="sale", to_entity="customer", columns=["buyer_id"]
        ).report
        assert repeated["ok"], repeated
        model = yaml.safe_load((package / "models/sales.yml").read_text())
        assert model["model"]["entities"]["customer"] == {"expr": "buyer_id"}
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["entities"]["customer"]["key"] = ["store_id", "customer_id"]
    _write(graph_path, graph)
    with pytest.raises(SemanticLayerError, match="key width does not match target key") as invalid:
        load_package_config(str(package))
    assert invalid.value.code == "INVALID_CONFIG"

    revision, files = project.revision(), _files_and_receipts(project)
    args = {
        "from_entity": "sale",
        "to_entity": "customer",
        "columns": ["store_id", "buyer_id"],
        "expected_revision": revision,
        "idempotency_key": "repair-key",
    }
    preview = project.upsert_relationship(**args, dry_run=True).report
    assert preview["ok"] and preview["status"] == "preview"
    assert (project.revision(), _files_and_receipts(project)) == (revision, files)
    written = project.upsert_relationship(**args).report
    assert written["ok"] and written["revision"] == preview["proposed_revision"]
    (edge,) = load_package_config(str(package)).relationships
    assert edge.id == "relationship.sales_customer"
    assert edge.source_columns == ["store_id", "buyer_id"]
    assert edge.target_columns == ["store_id", "customer_id"]
    assert (
        _query(package, "amount", grouping="buyer")
        == _gold(
            package,
            "SELECT c.buyer, sum(s.amount) FROM sales s LEFT JOIN customers c "
            "ON c.store_id = s.store_id AND c.customer_id = s.buyer_id GROUP BY 1",
        )
        == {"Alice": 100, "Bob": 60}
    )
