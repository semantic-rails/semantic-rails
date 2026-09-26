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
    document.with_name(f"{document.name.split('.')[0]}.semantic_rails.json").unlink()
    report = import_ossie(document, tmp_path / "imported")
    assert report["sidecar"] is None and "round_trip" not in report
    assert {w["construct"]: w["count"] for w in report["warnings"]} == WITHOUT_SIDECAR[package_id]
    assert validate_runtime_package(Path(report["package_dir"])) == []


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


def test_an_edited_document_is_reported_against_its_stale_sidecar(tmp_path) -> None:
    document = _export(PACKAGES["tpch_sf1_showcase"], tmp_path / "ossie")
    text = document.read_text(encoding="utf-8")
    document.write_text(
        text.replace("SUM(tpch_order.tpch_revenue)", "MAX(tpch_order.tpch_revenue)")
    )
    report = import_ossie(document, tmp_path / "imported")
    assert report["round_trip"] == "differs"
    [differences] = [w for w in report["warnings"] if w["construct"] == "round-trip differences"]
    assert differences["ids"] == [
        "document.metrics.sales_average_order_value",
        "document.metrics.sales_revenue",
    ]


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
      - {name: customer_id, dimension: {}, expression: {dialects: [{dialect: ANSI_SQL, expression: customer_id}]}}
relationships:
  - {name: orders_customer, from: orders, to: customers, from_columns: [customer_id], to_columns: [customer_id]}
  - {name: recent_customer, from: recent, to: customers, from_columns: [customer_id], to_columns: [customer_id]}
metrics:
  - {name: revenue, expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(orders.amount)}]}}
  - {name: order_count, expression: {dialects: [{dialect: ANSI_SQL, expression: COUNT(DISTINCT orders.orders)}]}}
  - name: aov
    expression: {dialects: [{dialect: ANSI_SQL, expression: "SUM(orders.amount) / NULLIF(COUNT(DISTINCT orders.orders), 0)"}]}
    custom_extensions: [{vendor_name: COMMON, data: "{}"}]
  - {name: max_orders, expression: {dialects: [{dialect: ANSI_SQL, expression: MAX(orders.orders)}]}}
  - {name: share, expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(orders.amount) / SUM(orders.amount)}]}}
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
        ],
        "dimensions with computed expressions": ["orders.status_code"],
        "facts outside the supported expression grammar": ["orders.big"],
        "measures given defaults": ["measure.shop.amount", "measure.shop.orders"],
        "metric custom_extensions": ["aov"],
        "metrics outside the aggregate grammar": ["share"],
        "metrics summing a field other metrics count distinct": ["max_orders"],
        "model ai_context": ["shop"],
        "model ai_context text": ["shop"],
        "relationships to datasets not imported": ["recent_customer"],
        "temporal roles with default grains": ["temporal_role.shop_orders_ordered_at"],
    }
    config = load_package_snapshot(report["package_dir"]).config
    assert {m.id: m.kind for m in config.metric_recipes} == {
        "metric.shop.aov": "ratio",
        "metric.shop.order_count": "aggregate",
        "metric.shop.revenue": "aggregate",
    }
    assert validate_runtime_package(Path(report["package_dir"])) == []


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
