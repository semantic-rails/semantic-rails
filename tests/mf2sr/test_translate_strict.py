"""mf2sr --schema-strict: a strict package whose relations keep their dbt schema."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from mf2sr import translate
from mf2sr.cli import main as cli_main
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse

NODE = {"database": "analytics", "schema_name": "main_marts", "alias": "fct_orders"}


def _manifest(tmp_path: Path, node_relation: dict[str, Any]) -> Path:
    """A dbt semantic_manifest.json with one semantic model over ``node_relation``."""
    orders = {
        "name": "orders",
        "node_relation": node_relation,
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
        "dimensions": [
            {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "status", "type": "categorical"},
        ],
        "measures": [{"name": "order_total", "expr": "order_total", "agg": "sum"}],
    }
    revenue = {
        "name": "revenue",
        "label": "Revenue",
        "type": "simple",
        "type_params": {"measure": {"name": "order_total"}},
    }
    path = tmp_path / "semantic_manifest.json"
    path.write_text(json.dumps({"semantic_models": [orders], "metrics": [revenue]}))
    return path


def _model(report: Any) -> dict[str, Any]:
    return yaml.safe_load((report.package_dir / "models" / "orders.yml").read_text())["model"]


NO_SCHEMA = ["semantic model `orders` names no schema"]
NO_CONNECTION = ["parse: ", "package.connection"]  # mf2sr writes one for Snowflake only


@pytest.mark.parametrize(
    ("warehouse", "node_relation", "strict", "relation", "warnings"),
    [
        ("duckdb", NODE, False, "fct_orders", []),  # unchanged without the flag
        ("duckdb", NODE, True, "main_marts.fct_orders", []),
        ("duckdb", {**NODE, "schema_name": "main"}, True, "fct_orders", []),  # dbt-duckdb's default
        ("snowflake", NODE, True, "main_marts.fct_orders", []),  # the usual database is implied
        ("postgres", NODE, True, "main_marts.fct_orders", [NO_CONNECTION, NO_CONNECTION]),
        ("duckdb", {"alias": "fct_orders"}, True, "fct_orders", [NO_SCHEMA]),  # a YAML directory
    ],
)
def test_relations_keep_their_schema(
    tmp_path: Path,
    warehouse: str,
    node_relation: dict[str, Any],
    strict: bool,
    relation: str,
    warnings: list[list[str]],
) -> None:
    report = translate(
        _manifest(tmp_path, node_relation),
        tmp_path / "out",
        package_id="shop",
        warehouse=warehouse,
        schema_strict=strict,
    )

    assert _model(report)["relation"] == relation
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["schema_strict"] is strict
    assert len(report.warnings) == len(warnings), report.warnings
    for warning, parts in zip(report.warnings, warnings, strict=True):
        assert all(part in warning for part in parts), warning
    if warehouse == "snowflake":  # the implied database is pinned, not left to the session
        assert package["connection"]["options"]["database"] == "analytics"


def test_a_relation_outside_the_usual_database_names_it(tmp_path: Path) -> None:
    """As import_dbt_project names dbt relations: the database only when it differs."""
    manifest = json.loads(_manifest(tmp_path, NODE).read_text())
    manifest["semantic_models"].append(
        {
            "name": "customers",
            "node_relation": {"database": "raw", "schema_name": "crm", "alias": "dim_customers"},
            "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        }
    )
    source = tmp_path / "two.json"
    source.write_text(json.dumps(manifest))

    report = translate(
        source, tmp_path / "out", package_id="shop", warehouse="snowflake", schema_strict=True
    )

    relations = {
        name: yaml.safe_load((report.package_dir / "models" / f"{name}.yml").read_text())["model"][
            "relation"
        ]
        for name in ("orders", "customers")
    }
    assert relations == {"orders": "main_marts.fct_orders", "customers": "raw.crm.dim_customers"}
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["connection"]["options"]["database"] == "analytics"


def test_a_strict_package_queries_the_dbt_built_marts(tmp_path: Path) -> None:
    report = translate(
        _manifest(tmp_path, NODE),
        tmp_path / "out",
        package_id="shop",
        default_db="data/warehouse.duckdb",
        schema_strict=True,
    )
    build_dbt_warehouse(report.package_dir / "data" / "warehouse.duckdb")

    assert report.warnings == []  # including no parse errors
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["seed"] == {"kind": "external"}  # dbt built it; never rebuilt
    engine = Runtime.from_path(str(report.package_dir))
    try:
        rows = engine.query(
            {
                "version": 1,
                "select": [{"expression": {"metric": "metric.shop.revenue"}, "as": "revenue"}],
            }
        )["rows"]
    finally:
        engine.close()
    assert rows and rows[0]["revenue"] > 0


def test_parse_errors_fail_strict_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A Postgres package has no connection block yet, so it doesn't parse."""
    source = str(_manifest(tmp_path, NODE))
    arguments = ["--source", source, "--package-id", "shop", "--warehouse", "postgres"]

    assert cli_main([*arguments, "--output", str(tmp_path / "a"), "--schema-strict"]) == 0
    assert (
        cli_main([*arguments, "--output", str(tmp_path / "b"), "--schema-strict", "--strict"]) == 2
    )
    assert "parse: " in capsys.readouterr().out


def test_semantic_rails_import_takes_schema_strict(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails.cli",
            "import",
            "--from",
            "metricflow",
            "--source",
            str(_manifest(tmp_path, NODE)),
            "--output",
            str(tmp_path / "out"),
            "--package-id",
            "shop",
            "--schema-strict",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout)["ok"] is True
    assert (
        yaml.safe_load((tmp_path / "out" / "shop" / "models" / "orders.yml").read_text())["model"][
            "relation"
        ]
        == "main_marts.fct_orders"
    )
