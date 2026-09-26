from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path

import pytest

from semantic_rails.config import load_package_snapshot
from semantic_rails.expressions import (
    AggregateExpr,
    ArithmeticExpr,
    ColumnRefExpr,
    LiteralExpr,
    MetricRecipeRefExpr,
)
from semantic_rails.interop.ossie import export_ossie
from semantic_rails.interop.ossie.export import _Exporter
from semantic_rails.schema import MetricConfig, PackageConfig

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests/semantic_rails/fixtures/ossie_0_1_1"
JAFFLE = ROOT / "configs/semantic_rails/jaffle_shop"
PACKAGES = [
    JAFFLE,
    ROOT / "configs/semantic_rails/tpch_sf1_showcase",
    ROOT / "comparisons/semantic_layers/semantic_rails/package",
    ROOT / "configs/examples/semantic_rails_package_starter.yml",
]
IDS = [path.stem if path.suffix else path.name for path in PACKAGES]


def _sql(node: dict) -> str:
    [dialect] = node["expression"]["dialects"]
    return dialect["expression"]


def test_vendored_spec_files_match_the_pinned_upstream_tag() -> None:
    pins = {
        "core-spec/osi-schema.json": "c1e9adec39562786aa78809665fba568797b15f4c53a0847d9cbcf2dead1bc94",
        "validation/validate.py": "3ba4c070faed5738f0a6fee1510accfb4ee9c32dcecde9bf23ff958fd3a19fe8",
    }
    for name, digest in pins.items():
        assert hashlib.sha256((SPEC / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize("package", PACKAGES, ids=IDS)
def test_cli_export_passes_the_spec_validator(package, tmp_path, monkeypatch, capsys) -> None:
    from semantic_rails.cli import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "semantic-rails",
            "export",
            "--format",
            "ossie",
            "--path",
            str(package),
            "--output",
            str(tmp_path),
        ],
    )
    main()
    report = json.loads(capsys.readouterr().out)
    document = Path(report["document"])
    assert document.parent == tmp_path and Path(report["sidecar"]).is_file()
    result = subprocess.run(
        [sys.executable, str(SPEC / "validation/validate.py"), str(document)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert f"Validation PASSED: {document.name}" in result.stdout


@pytest.mark.parametrize("package", PACKAGES, ids=IDS)
def test_every_object_is_exported_or_kept_and_warned(package) -> None:
    snapshot = load_package_snapshot(package)
    document, sidecar = export_ossie(snapshot)
    named = {sr_id for names in sidecar["names"].values() for sr_id in names.values()}
    warned = {row_id for warning in sidecar["warnings"] for row_id in warning["ids"]}
    assert all(w["count"] == len(w["ids"]) for w in sidecar["warnings"])
    for item in fields(PackageConfig):
        rows = getattr(snapshot.config, item.name)
        for row in rows if isinstance(rows, list) else []:
            if not hasattr(row, "id"):
                continue
            kept = sidecar["objects"].get(item.name, {})
            assert row.id in named or row.id in kept, row.id
            assert row.id in warned or (row.id in named and row.id not in kept), row.id
    model = document["semantic_model"][0]
    assert len(sidecar["names"]["datasets"]) == len(model["datasets"])
    assert len(sidecar["names"]["metrics"]) == len(model["metrics"])


def test_jaffle_mapping_and_warning_counts_are_golden() -> None:
    document, sidecar = export_ossie(JAFFLE)
    model = document["semantic_model"][0]
    assert {w["construct"]: w["count"] for w in sidecar["warnings"]} == {
        "conversion metrics": 3,
        "cumulative metrics": 1,
        "dimension attributes": 67,
        "entity attributes": 13,
        "filtered metrics": 2,
        "measure attributes": 33,
        "measures on other relations": 8,
        "metric attributes": 5,
        "metrics on semi-additive measures": 1,
        "metrics using prior_period": 1,
        "package attributes": 1,
        "period_to_date metrics": 1,
        "policy enforcement": 5,
        "prior_period metrics": 1,
        "relationship attributes": 15,
        "rolling metrics": 2,
        "segments": 1,
        "semantic caveats": 1,
        "semantic policies": 5,
        "semi_additive metrics": 8,
        "temporal roles": 18,
        "time-valid relationships": 1,
        "value domains": 7,
    }
    enforcement = next(w for w in sidecar["warnings"] if w["construct"] == "policy enforcement")
    assert "won't enforce" in enforcement["message"]
    assert {metric["name"]: _sql(metric) for metric in model["metrics"]} == {
        "sales_aov_usd": "SUM(jaffle_order.jaffle_revenue_usd) / "
        "NULLIF(COUNT(DISTINCT jaffle_order.jaffle_order_count), 0)",
        "sales_customer_count": "COUNT(DISTINCT jaffle_customer.jaffle_customer_count)",
        "sales_drink_revenue_share": "SUM(jaffle_order.jaffle_drink_revenue_usd) / "
        "NULLIF(SUM(jaffle_order.jaffle_revenue_usd), 0)",
        "sales_food_revenue_share": "SUM(jaffle_order.jaffle_food_revenue_usd) / "
        "NULLIF(SUM(jaffle_order.jaffle_revenue_usd), 0)",
        "sales_gross_margin_pct": "(SUM(jaffle_order.jaffle_gross_order_value_usd) - "
        "SUM(jaffle_order.jaffle_order_cost_usd)) / "
        "NULLIF(SUM(jaffle_order.jaffle_gross_order_value_usd), 0)",
    }
    assert sidecar["names"]["metrics"]["sales_aov_usd"] == "metric.sales.aov_usd"
    assert sidecar["package"]["namespace"] == "jaffle"
    history = next(d for d in model["datasets"] if d["name"] == "jaffle_customer_history")
    assert history["primary_key"] == ["customer_id", "valid_from"]
    customer = next(d for d in model["datasets"] if d["name"] == "jaffle_customer")
    first_order = next(
        f for f in customer["fields"] if f["name"] == "jaffle_customer_first_order_at"
    )
    assert first_order["dimension"] == {"is_time": True}
    assert _sql(first_order) == "first_order_at"
    assert "dimension" not in next(
        f for f in customer["fields"] if f["name"] == "jaffle_customer_count"
    )
    orders = next(r for r in model["relationships"] if r["name"] == "orders_customer")
    assert orders == {
        "name": "orders_customer",
        "from": "jaffle_order",
        "to": "jaffle_customer",
        "from_columns": ["customer_id"],
        "to_columns": ["customer_id"],
    }


def test_snowflake_packages_export_the_snowflake_dialect() -> None:
    document, _ = export_ossie(ROOT / "configs/semantic_rails/tpch_sf1_showcase")
    metrics = document["semantic_model"][0]["metrics"]
    assert {metric["expression"]["dialects"][0]["dialect"] for metric in metrics} == {"SNOWFLAKE"}


def test_exported_metrics_compute_the_engine_numbers(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    document, sidecar = export_ossie(JAFFLE)
    model = document["semantic_model"][0]
    datasets = {dataset["name"]: dataset for dataset in model["datasets"]}
    for metric in model["metrics"]:
        [name] = {ref.split(".")[0] for ref in re.findall(r"\b\w+\.\w+\b", _sql(metric))}
        dataset = datasets[name]
        columns = ", ".join(f"{_sql(field)} AS {field['name']}" for field in dataset["fields"])
        ossie = (
            f"SELECT {_sql(metric)} AS v FROM (SELECT {columns} FROM {dataset['source']}) AS {name}"
        )
        metric_id = sidecar["names"]["metrics"][metric["name"]]
        engine = runtime.query(
            {
                "version": 1,
                "select": [{"expression": {"kind": "metric", "metric": metric_id}, "as": "v"}],
            }
        )
        assert runtime.adapter.query(ossie)[0]["v"] == pytest.approx(engine["rows"][0]["v"])


def _export_with(**changes) -> tuple[dict, _Exporter]:
    exporter = _Exporter(replace(load_package_snapshot(JAFFLE).config, **changes))
    return exporter.model(), exporter


@pytest.mark.parametrize(
    ("change", "exported", "construct"),
    [
        ({"cardinality": "1:N"}, ("jaffle_customer", "jaffle_order"), ""),
        ({"cardinality": "1:1"}, ("jaffle_order", "jaffle_customer"), ""),
        ({"cardinality": "M:N", "safety": "requires_rewrite"}, None, "M:N relationships"),
        ({"safety": "unsafe"}, None, "unsafe relationships"),
    ],
)
def test_relationships_export_only_as_safe_many_to_one_joins(change, exported, construct) -> None:
    config = load_package_snapshot(JAFFLE).config
    relationships = [
        replace(row, **change) if row.id == "relationship.orders_customer" else row
        for row in config.relationships
    ]
    model, exporter = _export_with(relationships=relationships)
    found = [r for r in model["relationships"] if r["name"] == "orders_customer"]
    if exported is None:
        assert not found and "relationship.orders_customer" in exporter.lost[construct]
        return
    assert (found[0]["from"], found[0]["to"]) == exported
    kept = exporter.objects["relationships"]["relationship.orders_customer"]
    assert kept["cardinality"] == change["cardinality"]


REVENUE = AggregateExpr(measure="measure.jaffle.revenue_usd", aggregation="sum")
COST = AggregateExpr(measure="measure.jaffle.order_cost_usd", aggregation="sum")


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            ArithmeticExpr(
                "multiply", MetricRecipeRefExpr("metric.sales.aov_usd"), LiteralExpr(100)
            ),
            "SUM(jaffle_order.jaffle_revenue_usd) / "
            "NULLIF(COUNT(DISTINCT jaffle_order.jaffle_order_count), 0) * 100",
        ),
        (
            ArithmeticExpr("add", REVENUE, COST, null_behavior="coalesce_zero"),
            "COALESCE(SUM(jaffle_order.jaffle_revenue_usd), 0) + "
            "COALESCE(SUM(jaffle_order.jaffle_order_cost_usd), 0)",
        ),
        (replace(REVENUE, aggregation="median"), "metrics using median"),
        (
            replace(REVENUE, temporal_role="temporal_role.jaffle_order_time"),
            "metrics with clock or parameter overrides",
        ),
        (
            MetricRecipeRefExpr("metric.sales.cumulative_revenue"),
            "metrics built on omitted objects",
        ),
        (
            AggregateExpr(measure="measure.jaffle.revenue_ytd_usd"),
            "metrics built on omitted objects",
        ),
    ],
)
def test_metrics_export_their_sql_or_stay_in_the_sidecar(expression, expected) -> None:
    config = load_package_snapshot(JAFFLE).config
    probe = MetricConfig(id="metric.test.probe", kind="derived", expression=expression)
    model, exporter = _export_with(metric_recipes=[*config.metric_recipes, probe])
    found = [metric for metric in model["metrics"] if metric["name"] == "test_probe"]
    if found:
        assert _sql(found[0]) == expected
    else:
        assert "metric.test.probe" in exporter.lost[expected]
        assert exporter.objects["metric_recipes"]["metric.test.probe"]["kind"] == "derived"


def test_measures_reading_another_entity_stay_in_the_sidecar() -> None:
    config = load_package_snapshot(JAFFLE).config
    foreign = ColumnRefExpr(column="customer_id", entity="entity.jaffle_customer")
    measures = [
        replace(row, expr=foreign) if row.id == "measure.jaffle.revenue_usd" else row
        for row in config.measures
    ]
    model, exporter = _export_with(measures=measures)
    assert exporter.lost["measures reading other entities"] == ["measure.jaffle.revenue_usd"]
    assert "metric.sales.aov_usd" in exporter.lost["metrics built on omitted objects"]
    assert "sales_aov_usd" not in {metric["name"] for metric in model["metrics"]}
