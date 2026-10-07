"""mf2sr writes strict packages; --keep-schema preserves dbt relations."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from mf2sr import translate
from mf2sr.cli import main as cli_main
from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse
from tests.semantic_rails.result_helpers import typed_rows

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
    ("warehouse", "node_relation", "relation", "warnings"),
    [
        ("duckdb", NODE, "main_marts.fct_orders", []),
        ("duckdb", {**NODE, "schema_name": "main"}, "fct_orders", []),  # dbt-duckdb's default
        ("snowflake", NODE, "main_marts.fct_orders", []),  # the usual database is implied
        ("postgres", NODE, "main_marts.fct_orders", [NO_CONNECTION, NO_CONNECTION]),
        ("duckdb", {"alias": "fct_orders"}, "fct_orders", [NO_SCHEMA]),  # a YAML directory
    ],
)
def test_relations_keep_their_schema(
    tmp_path: Path,
    warehouse: str,
    node_relation: dict[str, Any],
    relation: str,
    warnings: list[list[str]],
) -> None:
    report = translate(
        _manifest(tmp_path, node_relation),
        tmp_path / "out",
        package_id="shop",
        warehouse=warehouse,
        keep_schema=True,
    )

    assert _model(report)["relation"] == relation
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["schema_strict"] is True
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
        source, tmp_path / "out", package_id="shop", warehouse="snowflake", keep_schema=True
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
        keep_schema=True,
    )
    build_dbt_warehouse(report.package_dir / "data" / "warehouse.duckdb")

    assert report.warnings == []  # including no parse errors
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["seed"] == {"kind": "external"}  # dbt built it; never rebuilt
    engine = Runtime.from_path(str(report.package_dir))
    try:
        rows = typed_rows(
            engine.query(
                {
                    "version": 1,
                    "select": [{"expression": {"metric": "metric.shop.revenue"}, "as": "revenue"}],
                }
            )
        )
    finally:
        engine.close()
    assert rows and rows[0]["revenue"] > 0


@pytest.mark.parametrize("flags", [[], ["--keep-schema"]])
def test_parse_errors_fail_strict_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    """A Postgres package has no connection block yet, so it doesn't parse."""
    source = str(_manifest(tmp_path, NODE))
    arguments = ["--source", source, "--package-id", "shop", "--warehouse", "postgres"]

    assert cli_main([*arguments, "--output", str(tmp_path / "a"), *flags]) == 0
    assert cli_main([*arguments, "--output", str(tmp_path / "b"), *flags, "--strict"]) == 2
    assert "parse: " in capsys.readouterr().out


def test_semantic_rails_import_takes_keep_schema(tmp_path: Path) -> None:
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
            "--keep-schema",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert json.loads(result.stdout)["ok"] is True
    assert (
        yaml.safe_load((tmp_path / "out" / "shop" / "models" / "orders.yml").read_text())["model"][
            "relation"
        ]
        == "main_marts.fct_orders"
    )


@pytest.mark.parametrize("same_name", [False, True])
def test_default_output_exposes_only_explicit_metrics(tmp_path: Path, same_name: bool) -> None:
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    if same_name:
        raw["metrics"][0]["name"] = "order_total"
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop")
    cfg = load_package_config(report.package_dir)

    assert {metric.id for metric in cfg.metric_recipes} == {
        f"metric.shop.{metric['name']}" for metric in raw["metrics"]
    }
    assert _model(report)["relation"] == "fct_orders"
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["schema_strict"] is True
    assert package["seed"] == {"kind": "sql_script", "source": "data/seed_shop.sql"}
    assert all("publish" not in measure for measure in _model(report)["measures"].values())
    assert report.warnings == []


@pytest.mark.parametrize(
    ("warehouse", "package_hash"),
    [
        ("duckdb", "6445b85967658cf392872909fb60b1966d92880ae188a2e1f89085ea7de475b0"),
        ("snowflake", "8961fe986a64dd6fc2c227e94f37add8467b7f0e41408465ea30dfae12b979c1"),
    ],
)
def test_keep_schema_preserves_previous_output_bytes(
    tmp_path: Path, warehouse: str, package_hash: str
) -> None:
    """Frozen --schema-strict output bytes, with only publish: false lines removed."""
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    raw["metrics"].append(
        {
            "name": "order_total",
            "type": "simple",
            "type_params": {"measure": {"name": "order_total"}},
        }
    )
    source.write_text(json.dumps(raw))
    assert (
        cli_main(
            [
                "--source",
                str(source),
                "--output",
                str(tmp_path / "out"),
                "--package-id",
                "shop",
                "--warehouse",
                warehouse,
                "--keep-schema",
            ]
        )
        == 0
    )
    package_dir = tmp_path / "out" / "shop"
    expected = {
        "graph.yml": "d46b79dd7d6dc9277ae8009993a42db597f758a02c53c67fe5de018a22daf350",
        "metrics/orders.yml": "29d0dcae021d2b78b80e36ac237a5b7d888830287e208b1ecc60dd7c4e7647c1",
        "models/orders.yml": "af2683b371e51f813e7dae0718ad084b247ef09775c4b4b468d9cc337edafb9f",
        "package.yml": package_hash,
    }
    assert {
        str(path.relative_to(package_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package_dir.rglob("*.yml")
    } == expected


@pytest.mark.parametrize("entrypoint", ["mf2sr", "import"])
def test_old_schema_strict_flag_is_refused(tmp_path: Path, entrypoint: str) -> None:
    command = [sys.executable, "-m", "mf2sr"]
    if entrypoint == "import":
        command = [sys.executable, "-m", "semantic_rails.cli", "import", "--from", "metricflow"]
    result = subprocess.run(
        [
            *command,
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
        timeout=120,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --schema-strict" in result.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("dimension_expr", ["status", "lower(status)"])
def test_dimension_and_percentile_output_uses_loadable_keys(
    tmp_path: Path, dimension_expr: str
) -> None:
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    model = raw["semantic_models"][0]
    model["dimensions"].append(
        {"name": "order_status", "type": "categorical", "expr": dimension_expr}
    )
    model["measures"].append(
        {
            "name": "p90_total",
            "expr": "order_total",
            "agg": "percentile",
            "agg_params": {"percentile": 0.9},
        }
    )
    raw["metrics"].extend(
        [
            {
                "name": "p90",
                "type": "simple",
                "type_params": {"measure": {"name": "p90_total"}},
            },
            {"name": "p90_running", "type": "cumulative", "type_params": {"measure": "p90_total"}},
            {
                "name": "p90_share",
                "type": "ratio",
                "type_params": {
                    "numerator": "p90_total",
                    "denominator": "order_total",
                },
            },
            {
                "name": "p90_twice",
                "type": "derived",
                "type_params": {
                    "expr": "p90 * 2",
                    "metrics": [{"name": "p90"}],
                },
            },
        ]
    )
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop", keep_schema=True)
    build_dbt_warehouse(report.package_dir / "data" / "shop.duckdb")
    cfg = load_package_config(report.package_dir)

    assert {metric.id for metric in cfg.metric_recipes} == {"metric.shop.revenue"}
    assert "p90_total" not in _model(report)["measures"]
    assert any("unsupported percentile parameters" in warning for warning in report.warnings)
    if dimension_expr == "status":
        dimension = next(
            dim for dim in cfg.dimensions if dim.id == "dimension.shop_order_order_status"
        )
        assert dimension.column == "status"
        assert _model(report)["dimensions"]["order_status"]["column"] == "status"
        engine = Runtime.from_path(str(report.package_dir))
        try:
            rows = typed_rows(
                engine.query(
                    {
                        "version": 1,
                        "select": [
                            {"expression": {"metric": "metric.shop.revenue"}, "as": "revenue"}
                        ],
                        "where": [
                            {
                                "field": "dimension.shop_order_order_status",
                                "op": "=",
                                "value": "delivered",
                            }
                        ],
                    }
                )
            )
        finally:
            engine.close()
        with duckdb.connect(
            str(report.package_dir / "data" / "shop.duckdb"), read_only=True
        ) as conn:
            expected = conn.execute(
                "SELECT SUM(order_total) FROM main_marts.fct_orders WHERE status = 'delivered'"
            ).fetchone()
        assert expected is not None
        assert rows == [{"revenue": expected[0]}]
    else:
        assert "order_status" not in _model(report)["dimensions"]
        assert any("non-column expr" in warning for warning in report.warnings)
    assert not any(warning.startswith("parse:") for warning in report.warnings)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails.cli",
            "validate-config",
            "--path",
            str(report.package_dir),
            "--quiet",
            "--no-manifest",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True
