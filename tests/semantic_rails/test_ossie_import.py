from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_snapshot
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import expr_to_dict
from semantic_rails.interop.ossie import import_ossie, write_ossie_export
from semantic_rails.interop.ossie.reader import _MetricParser
from semantic_rails.planner.plan import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    "jaffle_shop": ROOT / "configs/semantic_rails/jaffle_shop",
    "tpch_sf1_showcase": ROOT / "configs/semantic_rails/tpch_sf1_showcase",
    "package": ROOT / "comparisons/semantic_layers/semantic_rails/package",
    "shop_starter": ROOT / "configs/examples/semantic_rails_package_starter.yml",
}
# Without the sidecar: what each export's document alone imports with (construct: count).
WITHOUT_SIDECAR = {
    "jaffle_shop": {
        "dimensions typed by default": 67,
        "facts outside the supported expression grammar": 6,  # CASE WHEN facts
        "measures given defaults": 27,
        "temporal roles with default grains": 26,
    },
    "tpch_sf1_showcase": {
        "dimensions typed by default": 14,
        "measures given defaults": 2,
        "temporal roles with default grains": 1,
    },
    "package": {
        "dimensions typed by default": 31,
        "facts outside the supported expression grammar": 1,
        "measures given defaults": 7,
        "metrics outside the aggregate grammar": 1,  # its fact was skipped
        "temporal roles with default grains": 10,
    },
    "shop_starter": {
        "dimensions typed by default": 13,
        "measures given defaults": 3,
        "temporal roles with default grains": 2,
    },
}


def _export(package: Path, directory: Path) -> Path:
    return Path(write_ossie_export(package, directory)["document"])


def _errors(path: Path) -> list[str]:
    return [error.replace(str(path), "<pkg>") for error in validate_runtime_package(path)]


@pytest.mark.parametrize("package_id", PACKAGES)
def test_export_import_export_is_exact(package_id, tmp_path) -> None:
    package = PACKAGES[package_id]
    report = import_ossie(_export(package, tmp_path / "ossie"), tmp_path / "imported")
    imported = Path(report["package_dir"])
    assert (report["round_trip"], report["warnings"]) == ("exact", [])
    assert load_package_snapshot(imported).semantic == load_package_snapshot(package).semantic
    # The comparison package's one existing error (a date dimension without a time role) stays.
    assert _errors(imported) == _errors(package)


@pytest.mark.parametrize("package_id", PACKAGES)
def test_import_without_the_sidecar_validates_with_counted_defaults(package_id, tmp_path) -> None:
    document = _export(PACKAGES[package_id], tmp_path / "ossie")
    _sidecar(document).unlink()
    report = import_ossie(document, tmp_path / "imported")
    assert report["sidecar"] is None and "round_trip" not in report
    assert {w["construct"]: w["count"] for w in report["warnings"]} == WITHOUT_SIDECAR[package_id]
    assert validate_runtime_package(Path(report["package_dir"])) == []
    # The warehouse comes from the dialect the document's SQL is written in.
    warehouse = load_package_snapshot(report["package_dir"]).config.package.warehouse
    assert warehouse == ("snowflake" if package_id == "tpch_sf1_showcase" else "duckdb")


def _answer(runtime: Runtime, example: dict) -> tuple:
    try:
        rows = runtime.query(example["query"])["rows"]
    except SemanticLayerError as exc:
        rows = exc.code
    plan = plan_payload(runtime, intent=example["question"])
    return rows, plan["status"], (plan.get("best") or {}).get("query_ir")


def test_imported_jaffle_answers_every_example_like_the_original(tmp_path) -> None:
    source = copy_package_config(tmp_path, "jaffle_shop")
    examples = {
        name: example
        for group in ("core", "advanced")
        for name, example in yaml.safe_load(
            (source / "examples" / f"{group}.yml").read_text(encoding="utf-8")
        )["examples"].items()
    }
    original = Runtime.from_path(str(source))
    try:
        expected = {name: _answer(original, example) for name, example in examples.items()}
    finally:
        original.close()
    document = _export(source, tmp_path / "ossie")
    report = import_ossie(document, tmp_path / "imported", default_db="jaffle_shop.duckdb")
    shutil.copy2(source / "jaffle_shop.duckdb", Path(report["package_dir"]) / "jaffle_shop.duckdb")
    imported = Runtime.from_path(report["package_dir"])
    try:
        actual = {name: _answer(imported, example) for name, example in examples.items()}
    finally:
        imported.close()
    assert len(examples) == 22 and all(isinstance(rows, list) for rows, *_ in expected.values())
    assert actual == expected


def _without_metric(document: dict, name: str) -> None:
    [model] = document["semantic_model"]
    model["metrics"] = [m for m in model["metrics"] if m["name"] != name]


def _renamed_field(document: dict, old: str, new: str) -> None:
    for dataset in document["semantic_model"][0]["datasets"]:
        for field in dataset.get("fields") or []:
            field["name"] = new if field["name"] == old else field["name"]


def _without_dataset(document: dict, name: str) -> None:
    [model] = document["semantic_model"]
    model["datasets"] = [d for d in model["datasets"] if d["name"] != name]
    model["relationships"] = [r for r in model["relationships"] if name not in (r["from"], r["to"])]


@pytest.mark.parametrize(
    ("package_id", "edit", "named"),
    [
        (
            "tpch_sf1_showcase",
            lambda text: text.replace(
                "SUM(tpch_order.tpch_revenue)", "MAX(tpch_order.tpch_revenue)"
            ),
            "document.metrics.sales_revenue",
        ),
        (
            "tpch_sf1_showcase",
            lambda text: _edited(text, lambda d: _without_metric(d, "sales_orders")),
            "metrics sales_orders (not in the document)",
        ),
        (
            "tpch_sf1_showcase",
            lambda text: _edited(
                text, lambda d: _renamed_field(d, "tpch_customer_market_segment", "segment")
            ),
            "fields tpch_customer.segment (not in the sidecar)",
        ),
        (
            "tpch_sf1_showcase",
            lambda text: _edited(text, lambda d: _renamed_field(d, "tpch_order_order_date", "day")),
            "fields tpch_order.tpch_order_order_date (not in the document)",
        ),
        (
            "jaffle_shop",
            lambda text: _edited(text, lambda d: _without_dataset(d, "jaffle_customer_history")),
            "datasets jaffle_customer_history (not in the document)",
        ),
        (  # every name still matches, but the import would skip the dataset
            "shop_starter",
            lambda text: _edited(
                text, lambda d: d["semantic_model"][0]["datasets"][0].pop("primary_key")
            ),
            "datasets without a primary key or defined by a query: ",
        ),
    ],
)
def test_a_document_edited_after_the_export_is_refused(package_id, edit, named, tmp_path):
    document = _export(PACKAGES[package_id], tmp_path / "ossie")
    document.write_text(edit(document.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(SemanticLayerError, match="match its sidecar") as caught:
        import_ossie(document, tmp_path / "imported")
    assert named in str(caught.value)
    assert not (tmp_path / "imported").exists()


def _edited(text: str, change) -> str:
    document = yaml.safe_load(text)
    change(document)
    return yaml.safe_dump(document, sort_keys=False)


def _sidecar(document: Path) -> Path:
    return document.with_name(f"{document.name.split('.')[0]}.semantic_rails.json")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda doc, side: doc.write_text("version: '1.0'\n"), "not an Ossie 0.1.x or 0.2"),
        (lambda doc, side: doc.write_text("version: 0.2.0\nname: [\n"), "can't import it"),
        (lambda doc, side: doc.unlink(), "can't import it"),
        (lambda doc, side: side.write_text("{", encoding="utf-8"), "can't import it"),
        (
            lambda doc, side: side.write_text(
                side.read_text().replace('"format_version": 1', '"format_version": 2')
            ),
            "not a version-1 Semantic Rails sidecar",
        ),
        (
            lambda doc, side: (
                side.unlink(),
                doc.write_text("version: 0.2.0\ndatasets: [orders]\n"),
            ),
            "can't import it",
        ),
        (
            lambda doc, side: (
                side.unlink(),
                doc.write_text(doc.read_text().replace("ANSI_SQL", "DATABRICKS")),
            ),
            "Importing a databricks package isn't supported yet",
        ),
        (
            lambda doc, side: (
                side.unlink(),
                doc.write_text(
                    doc.read_text()
                    .replace("ANSI_SQL", "SNOWFLAKE", 1)
                    .replace("ANSI_SQL", "DATABRICKS", 1)
                ),
            ),
            "expressions for more than one warehouse",
        ),
    ],
)
def test_input_it_cannot_read_is_refused_with_a_typed_error(edit, message, tmp_path) -> None:
    document = _export(PACKAGES["shop_starter"], tmp_path / "ossie")
    edit(document, _sidecar(document))
    with pytest.raises(SemanticLayerError, match=message):
        import_ossie(document, tmp_path / "imported")
    assert not (tmp_path / "imported").exists()


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ({"package_id": "shop_copy"}, "the package id is 'shop_starter'"),
        ({"namespace": "store"}, "the package namespace is 'shop'"),
    ],
)
def test_with_its_sidecar_the_package_keeps_its_id(given, message, tmp_path) -> None:
    document = _export(PACKAGES["shop_starter"], tmp_path / "ossie")
    with pytest.raises(SemanticLayerError, match=message):
        import_ossie(document, tmp_path / "imported", **given)
    assert not (tmp_path / "imported").exists()


FOREIGN = """
version: 0.2.0
name: shop
ai_context: {instructions: Answer in dollars}
datasets:
  - name: orders
    source: shop.orders
    primary_key: [order_id]
    unique_keys: [[order_number]]
    fields:
      - {name: order_id, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: order_id}]}}
      - {name: ordered_at, datatype: timestamp, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: ordered_at}]}}
      - {name: ordered_on, datatype: date, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: ordered_on}]}}
      - {name: status_code, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: UPPER(status)}]}}
      - {name: customer_id, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: customer_id}]}}
      - {name: amount, expression: {dialects: [{dialect: ANSI_SQL, expression: amount_cents / 100.0}]}}
      - {name: orders, expression: {dialects: [{dialect: ANSI_SQL, expression: order_id}]}}
      - {name: big, expression: {dialects: [{dialect: ANSI_SQL, expression: "CASE WHEN amount_cents > 100 THEN 1 END"}]}}
  - {name: recent, source: SELECT * FROM shop.orders, primary_key: [order_id]}
  - name: customers
    source: shop.customers
    primary_key: [customer_id]
    fields:
      - {name: customer_id, label: null, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: customer_id}]}}
  - {name: Customers, source: crm.customers, primary_key: [customer_id]}
relationships:
  - {name: orders_customer, from: orders, to: customers, from_columns: [customer_id], to_columns: [customer_id]}
  - {name: recent_customer, from: recent, to: customers, from_columns: [customer_id], to_columns: [customer_id]}
  - {name: orders_by_email, from: orders, to: customers, from_columns: [email], to_columns: [email]}
metrics:
  - {name: revenue, expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(orders.amount)}]}}
  - {name: order_count, expression: {dialects: [{dialect: ANSI_SQL, expression: COUNT(DISTINCT orders.orders)}]}}
  - name: aov
    expression: {dialects: [{dialect: ANSI_SQL, expression: "SUM(orders.amount) / NULLIF(COUNT(DISTINCT orders.orders), 0)"}]}
    custom_extensions: [{vendor_name: COMMON, data: "{}"}]
  - {name: max_orders, expression: {dialects: [{dialect: ANSI_SQL, expression: MAX(orders.orders)}]}}
  - {name: share, expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(orders.amount) / SUM(orders.amount)}]}}
  - {name: broken, expression: null}
  - {name: Revenue, expression: {dialects: [{dialect: ANSI_SQL, expression: AVG(orders.amount)}]}}
"""


def test_a_foreign_0_2_document_imports_what_it_can_and_counts_the_rest(tmp_path) -> None:
    document = tmp_path / "shop.yaml"
    document.write_text(FOREIGN, encoding="utf-8")
    report = import_ossie(document, tmp_path / "imported")
    assert {w["construct"]: w["ids"] for w in report["warnings"]} == {
        "dataset unique_keys": ["orders"],
        "datasets without a primary key or defined by a query": ["recent"],
        "dimensions typed by default": [
            "dimension.shop_customers_customer_id",
            "dimension.shop_orders_customer_id",
            "dimension.shop_orders_order_id",
            "dimension.shop_orders_ordered_at",
            "dimension.shop_orders_ordered_on",
        ],
        "dimensions with computed expressions": ["orders.status_code"],
        "facts outside the supported expression grammar": ["orders.big"],
        "measures given defaults": ["measure.shop.orders_amount", "measure.shop.orders_orders"],
        "metric custom_extensions": ["aov"],
        "metrics outside the aggregate grammar": ["broken", "share"],
        "metrics summing a field other metrics count distinct": ["max_orders"],
        "names that collide once normalized": ["entity.shop_customers", "metric.shop.revenue"],
        "model ai_context text": ["shop"],
        "relationships not to their target's primary key": ["orders_by_email"],
        "relationships to datasets not imported": ["recent_customer"],
        "temporal roles with default grains": [
            "temporal_role.shop_orders_ordered_at",
            "temporal_role.shop_orders_ordered_on",
        ],
    }
    config = load_package_snapshot(report["package_dir"]).config
    assert {d.label for d in config.dimensions if d.entity == "entity.shop_customers"} == {
        "Customer Id"
    }
    kinds = {d.name: d.semantic_kind for d in config.dimensions if d.name.startswith("ordered")}
    assert kinds == {"ordered_at": "timestamp", "ordered_on": "date"}
    assert {m.id: m.kind for m in config.metric_recipes} == {
        "metric.shop.aov": "ratio",
        "metric.shop.order_count": "aggregate",
        "metric.shop.revenue": "aggregate",
    }
    assert validate_runtime_package(Path(report["package_dir"])) == []
    with pytest.raises(SemanticLayerError, match="can't name a directory"):
        import_ossie(document, tmp_path / "other", package_id="../escaped")


def _aggregate(measure: str, aggregation: str) -> dict:
    return {
        "kind": "aggregate",
        "measure": measure,
        "aggregation": aggregation,
        "temporal_role": "",
    }


def _arithmetic(op: str, left: dict, right: dict, null_behavior: str = "") -> dict:
    row = {"kind": "arithmetic", "op": op, "left": left, "right": right}
    return {**row, "null_behavior": null_behavior} if null_behavior else row


SUM_A, COUNT_B = _aggregate("measure.a", "sum"), _aggregate("measure.b", "count_distinct")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SUM(d.a)", SUM_A),
        (
            "sum(d.a) / NULLIF(count(DISTINCT d.b), 0)",
            _arithmetic("divide", SUM_A, COUNT_B, "null_if_zero"),
        ),
        (
            "(SUM(d.a) - COUNT(DISTINCT d.b)) * 100",
            _arithmetic(
                "multiply",
                _arithmetic("subtract", SUM_A, COUNT_B),
                {"kind": "literal", "value": 100},
            ),
        ),
        (
            "COALESCE(SUM(d.a), 0) + COALESCE(COUNT(DISTINCT d.b), 0)",
            _arithmetic("add", SUM_A, COUNT_B, "coalesce_zero"),
        ),
        ("SUM(d.a) / SUM(d.b)", None),  # the export always divides by NULLIF(x, 0)
        ("COUNT(d.a)", None),
        ("MEDIAN(d.a)", None),
        ("SUM(d.missing)", None),
        ("COALESCE(SUM(d.a), 0)", None),
        ("COALESCE(SUM(d.a), 0) * 2", None),
        ("SUM(CASE WHEN d.a > 0 THEN d.a END)", None),
        ("-SUM(d.a)", None),
        ("SUM(d.a) +", None),
    ],
)
def test_metric_parser_reads_only_the_export_grammar(sql, expected) -> None:
    expr = _MetricParser(sql, {"d.a": "measure.a", "d.b": "measure.b"}).parse()
    assert (expr_to_dict(expr) if expr else None) == expected


def test_cli_import_writes_a_package_and_refuses_to_overwrite(
    tmp_path, monkeypatch, capsys
) -> None:
    from semantic_rails.cli import main

    document = _export(PACKAGES["shop_starter"], tmp_path / "ossie")
    argv = ["semantic-rails", "import", "--from", "ossie", "--source", str(document)]
    argv += ["--output", str(tmp_path / "imported"), "--package-id", "shop_starter"]
    monkeypatch.setattr("sys.argv", argv)
    main()
    report = json.loads(capsys.readouterr().out)
    assert (report["round_trip"], report["package_dir"]) == (
        "exact",
        str(tmp_path / "imported/shop_starter"),
    )
    with pytest.raises(SystemExit):
        main()
    assert "CONFIG_CONFLICT" in capsys.readouterr().out
