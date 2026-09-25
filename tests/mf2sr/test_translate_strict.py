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
from semantic_rails import config_validation
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


@pytest.mark.parametrize(
    ("warehouse", "node_relation", "strict", "relation", "warned"),
    [
        ("duckdb", NODE, False, "fct_orders", False),  # unchanged without the flag
        ("duckdb", NODE, True, "main_marts.fct_orders", False),  # a DuckDB database is its file
        ("snowflake", NODE, True, "analytics.main_marts.fct_orders", False),
        ("bigquery", NODE, True, "analytics.main_marts.fct_orders", False),
        ("databricks", NODE, True, "analytics.main_marts.fct_orders", False),
        ("postgres", NODE, True, "main_marts.fct_orders", False),
        ("duckdb", {"alias": "fct_orders"}, True, "fct_orders", True),  # YAML input: no schema
    ],
)
def test_relations_keep_their_schema(
    tmp_path: Path,
    warehouse: str,
    node_relation: dict[str, Any],
    strict: bool,
    relation: str,
    warned: bool,
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
    assert any("names no schema" in warning for warning in report.warnings) is warned


def test_a_strict_package_queries_the_dbt_built_marts(tmp_path: Path) -> None:
    report = translate(
        _manifest(tmp_path, NODE),
        tmp_path / "out",
        package_id="shop",
        default_db="data/warehouse.duckdb",
        schema_strict=True,
    )
    build_dbt_warehouse(report.package_dir / "data" / "warehouse.duckdb")

    assert report.warnings == []  # including no strict parse errors
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


def test_strict_parse_errors_are_warnings_that_fail_strict_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def parse(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], None]:
        return {"ok": False, "errors": [{"message": "a strict problem"}]}, None

    monkeypatch.setattr(config_validation, "parse_config_report", parse)
    source = str(_manifest(tmp_path, NODE))
    arguments = ["--source", source, "--package-id", "shop", "--schema-strict"]

    assert cli_main([*arguments, "--output", str(tmp_path / "loose")]) == 0
    assert cli_main([*arguments, "--output", str(tmp_path / "strict"), "--strict"]) == 2
    assert "strict parse: a strict problem" in capsys.readouterr().out


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
