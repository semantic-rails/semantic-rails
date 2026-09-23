"""mf2sr's strict mode: schema-qualified relations and a schema_strict package."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from mf2sr.translate import translate
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_dbt_artifacts

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPARISON = REPO_ROOT / "comparisons" / "semantic_layers" / "metricflow" / "models"

SEMANTIC_MODELS = [
    {
        "name": "orders",
        "model": "ref('fct_orders')",
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": [
            {"name": "order", "type": "primary", "expr": "order_id"},
            {"name": "customer", "type": "foreign", "expr": "customer_id"},
        ],
        "dimensions": [
            {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "status", "type": "categorical"},
        ],
        "measures": [
            {"name": "orders", "expr": "1", "agg": "sum"},
            {"name": "revenue", "expr": "order_total", "agg": "sum"},
        ],
    },
    {
        "name": "customers",
        "model": "ref('dim_customers')",
        "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        "dimensions": [{"name": "customer_country", "type": "categorical"}],
    },
]
METRICS = [
    {"name": "orders", "type": "simple", "type_params": {"measure": "orders"}},
    {"name": "revenue", "type": "simple", "type_params": {"measure": "revenue"}},
    {
        "name": "average_order",
        "type": "ratio",
        "type_params": {"numerator": "revenue", "denominator": "orders"},
    },
    {
        "name": "revenue_per_order",
        "type": "derived",
        "type_params": {
            "expr": "revenue / orders",
            "metrics": [{"name": "revenue"}, {"name": "orders"}],
        },
    },
]


def _dbt_project(root: Path, semantic_models: list[dict[str, Any]] | None = None) -> Path:
    """A dbt project's semantic YAML, in the schema-2 form dbt projects write."""
    models = root / "models"
    models.mkdir(parents=True)
    (models / "semantic.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "semantic_models": semantic_models or SEMANTIC_MODELS,
                "metrics": METRICS,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return models


def _package(report: Any) -> dict[str, Any]:
    return dict(yaml.safe_load((report.package_dir / "package.yml").read_text())["package"])


def _relation(report: Any, model: str) -> str:
    path = report.package_dir / "models" / f"{model}.yml"
    return str(yaml.safe_load(path.read_text())["model"]["relation"])


def test_the_comparison_project_translates_to_a_strict_package(tmp_path: Path) -> None:
    report = translate(COMPARISON, tmp_path, package_id="mfshop", schema_strict=True)

    assert _package(report)["schema_strict"] is True
    assert not [warning for warning in report.warnings if warning.startswith("strict parse")]
    parse, _ = parse_config_report(PackageReference(source_path=str(report.package_dir)))
    assert parse["ok"] is True, parse["errors"]


def test_a_dbt_project_translates_onto_its_warehouse(tmp_path: Path) -> None:
    models = _dbt_project(tmp_path / "dbt")
    target = write_dbt_artifacts(
        build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb"), tmp_path / "dbt" / "target"
    )

    report = translate(
        models,
        tmp_path / "out",
        package_id="shop",
        schema_strict=True,
        dbt_target=target,
        default_db="data/warehouse.duckdb",
    )
    build_dbt_warehouse(report.package_dir / "data" / "warehouse.duckdb")

    assert _relation(report, "orders") == "main_marts.fct_orders"
    assert _relation(report, "customers") == "main_marts.dim_customers"
    assert _package(report)["seed"] == {"kind": "external"}
    assert not [warning for warning in report.warnings if warning.startswith("strict parse")]
    metrics = yaml.safe_load((report.package_dir / "metrics" / "orders.yml").read_text())
    # Money per order stays money, whether written as a ratio or a quotient.
    assert metrics["metrics"]["average_order"]["value_type"] == "currency"
    assert metrics["metrics"]["revenue_per_order"]["value_type"] == "currency"
    engine = Runtime.from_path(str(report.package_dir))
    country = "dimension.shop_customer_customer_country"
    try:
        rows = engine.query(
            {
                "version": 1,
                "select": [{"expression": {"metric": "metric.shop.orders"}, "as": "orders"}],
                "group_by": [country],
                "order_by": [{"field": country}],
                "limit": 10,
            }
        )["rows"]
    finally:
        engine.close()
    assert [(row[country], row["orders"]) for row in rows] == [("GB", 3), ("NL", 1), ("US", 4)]


def test_semantic_manifest_relations_keep_their_schema(tmp_path: Path) -> None:
    manifest = tmp_path / "semantic_manifest.json"
    semantic_models = []
    for model in SEMANTIC_MODELS:
        body = {key: value for key, value in model.items() if key != "model"}
        table = model["model"].split("'")[1]
        body["node_relation"] = {"alias": table, "schema_name": "main_marts", "database": "dw"}
        semantic_models.append(body)
    manifest.write_text(
        json.dumps({"semantic_models": semantic_models, "metrics": METRICS}), encoding="utf-8"
    )

    strict = translate(manifest, tmp_path / "strict", package_id="shop", schema_strict=True)
    loose = translate(manifest, tmp_path / "loose", package_id="shop")

    assert _relation(strict, "orders") == "main_marts.fct_orders"
    assert _relation(loose, "orders") == "fct_orders"  # unchanged without strict mode
    assert _package(loose)["schema_strict"] is False


def test_unresolved_relations_are_reported(tmp_path: Path) -> None:
    unknown = [
        {**SEMANTIC_MODELS[0], "model": "ref('fct_refunds')"},
        SEMANTIC_MODELS[1],
    ]
    models = _dbt_project(tmp_path / "dbt", unknown)
    target = write_dbt_artifacts(
        build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb"), tmp_path / "dbt" / "target"
    )

    resolved = translate(models, tmp_path / "out", package_id="shop", dbt_target=target)
    unqualified = translate(models, tmp_path / "bare", package_id="shop", schema_strict=True)

    assert any("ref('fct_refunds') is not in the dbt manifest" in w for w in resolved.warnings)
    assert _relation(resolved, "customers") == "main_marts.dim_customers"
    assert any("`fct_refunds` has no schema" in w for w in unqualified.warnings)


def test_derived_metrics_without_a_clear_type_are_flagged(tmp_path: Path) -> None:
    models = _dbt_project(tmp_path / "dbt")
    semantic = yaml.safe_load((models / "semantic.yml").read_text())
    semantic["metrics"].append(
        {
            "name": "weighted",
            "type": "derived",
            "type_params": {
                "expr": "revenue * orders",
                "metrics": [{"name": "revenue"}, {"name": "orders"}],
            },
        }
    )
    (models / "semantic.yml").write_text(yaml.safe_dump(semantic), encoding="utf-8")

    report = translate(models, tmp_path / "out", package_id="shop", schema_strict=True)

    assert any("metric `weighted`: check its value_type" in w for w in report.warnings)


def test_the_cli_takes_the_strict_flags(tmp_path: Path) -> None:
    models = _dbt_project(tmp_path / "dbt")
    target = write_dbt_artifacts(
        build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb"), tmp_path / "dbt" / "target"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mf2sr",
            "--source",
            str(models),
            "--output",
            str(tmp_path / "out"),
            "--package-id",
            "shop",
            "--schema-strict",
            "--dbt-target",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    package = yaml.safe_load((tmp_path / "out" / "shop" / "package.yml").read_text())["package"]
    assert package["schema_strict"] is True and package["seed"] == {"kind": "external"}


def test_mf2sr_imports_without_semantic_rails() -> None:
    code = "import sys; sys.modules['semantic_rails'] = None; import mf2sr.translate"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _metrics_doc(report: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (report.package_dir / "metrics").glob("*.yml"):
        merged.update(yaml.safe_load(path.read_text())["metrics"])
    return merged


def test_value_types_follow_the_metrics_a_ratio_names(tmp_path: Path) -> None:
    """Current MetricFlow names metrics, not measures, as ratio and derived inputs."""
    renamed = [
        {"name": "total_revenue", "type": "simple", "type_params": {"measure": "revenue"}},
        {"name": "order_total_count", "type": "simple", "type_params": {"measure": "orders"}},
        {"name": "customer_orders", "type": "simple", "type_params": {"measure": "orders"}},
        {
            "name": "average_order",
            "type": "ratio",
            "type_params": {"numerator": "total_revenue", "denominator": "order_total_count"},
        },
        {
            "name": "orders_per_order",
            "type": "ratio",
            "type_params": {"numerator": "customer_orders", "denominator": "order_total_count"},
        },
        {
            "name": "revenue_per_order",
            "type": "derived",
            "type_params": {
                "expr": "money / n",
                "metrics": [
                    {"name": "total_revenue", "alias": "money"},
                    {"name": "order_total_count", "alias": "n"},
                ],
            },
        },
        {
            "name": "average_of_average",
            "type": "derived",
            "type_params": {"expr": "average_order", "metrics": [{"name": "average_order"}]},
        },
    ]
    models = tmp_path / "models"
    models.mkdir()
    (models / "semantic.yml").write_text(
        yaml.safe_dump({"version": 2, "semantic_models": SEMANTIC_MODELS, "metrics": renamed}),
        encoding="utf-8",
    )

    report = translate(models, tmp_path / "out", package_id="shop", schema_strict=True)

    types = {key: spec["value_type"] for key, spec in _metrics_doc(report).items()}
    assert types["average_order"] == "currency"  # money per order
    assert types["orders_per_order"] == "ratio"  # count per count
    assert types["revenue_per_order"] == "currency"
    assert types["average_of_average"] == "currency"  # through another ratio
    assert not [warning for warning in report.warnings if warning.startswith("strict parse")]


def test_a_strict_parse_error_is_a_warning_and_fails_strict_runs(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    scored = [
        {
            **SEMANTIC_MODELS[0],
            "measures": [*SEMANTIC_MODELS[0]["measures"], {"name": "score", "agg": "average"}],
        },
        SEMANTIC_MODELS[1],
    ]
    untyped = [
        *METRICS,
        {"name": "score", "type": "simple", "type_params": {"measure": "score"}},
        {
            "name": "score_twice",
            "type": "derived",
            "type_params": {"expr": "score + score", "metrics": [{"name": "score"}]},
        },
    ]
    (models / "semantic.yml").write_text(
        yaml.safe_dump({"version": 2, "semantic_models": scored, "metrics": untyped}),
        encoding="utf-8",
    )
    arguments = [
        sys.executable,
        "-m",
        "mf2sr",
        "--source",
        str(models),
        "--output",
        str(tmp_path / "out"),
        "--package-id",
        "shop",
        "--schema-strict",
    ]

    report = translate(models, tmp_path / "direct", package_id="shop", schema_strict=True)
    loose = subprocess.run(arguments, capture_output=True, text=True, check=False)
    strict = subprocess.run([*arguments, "--strict"], capture_output=True, text=True, check=False)

    assert any(w.startswith("metric `score_twice`: check its value_type") for w in report.warnings)
    assert any(w.startswith("strict parse:") and "score_twice" in w for w in report.warnings)
    assert loose.returncode == 0 and strict.returncode == 2


def test_the_model_ref_never_changes_the_input_hash(tmp_path: Path) -> None:
    dbt_form = _dbt_project(tmp_path / "dbt")
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    documents = [
        {
            "semantic_model": {
                **{k: v for k, v in model.items() if k != "model"},
                "node_relation": {"alias": model["model"].split("'")[1]},
            }
        }
        for model in SEMANTIC_MODELS
    ] + [{"metric": metric} for metric in METRICS]
    (standalone / "all.yml").write_text(yaml.safe_dump_all(documents), encoding="utf-8")

    with_ref = translate(dbt_form, tmp_path / "a", package_id="shop")
    without = translate(standalone, tmp_path / "b", package_id="shop")

    assert with_ref.provenance["parsed_input_hash"] == without.provenance["parsed_input_hash"]


def test_only_a_dbt_target_makes_the_database_external(tmp_path: Path) -> None:
    models = _dbt_project(tmp_path / "dbt")

    report = translate(models, tmp_path / "out", package_id="shop", schema_strict=True)

    assert _package(report)["seed"]["kind"] == "sql_script"


def test_refs_resolve_to_the_relation_dbt_built_not_a_same_named_model(tmp_path: Path) -> None:
    target = write_dbt_artifacts(
        build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb"), tmp_path / "dbt" / "target"
    )
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    orders = manifest["nodes"]["model.shop_dbt.fct_orders"]
    orders["alias"] = "orders_mart"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    models = _dbt_project(tmp_path / "dbt")
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    body = {k: v for k, v in SEMANTIC_MODELS[1].items() if k != "model"}
    (standalone / "customers.yml").write_text(
        yaml.safe_dump({"semantic_model": {**body, "node_relation": {"alias": "dim_customers"}}}),
        encoding="utf-8",
    )

    by_ref = translate(models, tmp_path / "out", package_id="shop", dbt_target=target)
    by_alias = translate(standalone, tmp_path / "alias", package_id="shop", dbt_target=target)

    assert _relation(by_ref, "orders") == "main_marts.orders_mart"
    assert _relation(by_alias, "customers") == "main_marts.dim_customers"


def test_snowflake_relations_keep_their_database(tmp_path: Path) -> None:
    manifest = tmp_path / "semantic_manifest.json"
    stores = {
        "name": "stores",
        "node_relation": {"alias": "STORES", "schema_name": "DBT_PROD", "database": "ANALYTICS"},
        "entities": [{"name": "store", "type": "primary", "expr": "store_id"}],
        "dimensions": [{"name": "store_country", "type": "categorical"}],
        "measures": [{"name": "stores", "expr": "1", "agg": "sum"}],
    }
    manifest.write_text(
        json.dumps(
            {
                "semantic_models": [stores],
                "metrics": [
                    {"name": "stores", "type": "simple", "type_params": {"measure": "stores"}}
                ],
            }
        ),
        encoding="utf-8",
    )

    snowflake = translate(
        manifest, tmp_path / "sf", package_id="shop", schema_strict=True, warehouse="snowflake"
    )
    duckdb = translate(manifest, tmp_path / "db", package_id="shop", schema_strict=True)

    assert _relation(snowflake, "stores") == "ANALYTICS.DBT_PROD.STORES"
    assert _relation(duckdb, "stores") == "DBT_PROD.STORES"


def test_a_target_built_for_another_warehouse_is_reported(tmp_path: Path) -> None:
    models = _dbt_project(tmp_path / "dbt")
    target = write_dbt_artifacts(
        build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb"), tmp_path / "dbt" / "target"
    )

    report = translate(
        models, tmp_path / "out", package_id="shop", dbt_target=target, warehouse="snowflake"
    )

    assert any(
        "built for duckdb, but the package warehouse is snowflake" in w for w in report.warnings
    )
