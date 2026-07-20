"""Lightweight tests for the mf2sr translator.

These tests use only inline / synthetic MetricFlow input and verify
that the translator emits a Semantic Rails package the loader
accepts. No external repository fixtures are required, so they run
identically in CI and locally.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from mf2sr import translate


def _load_package(package_dir: Path, *, seed_basename: str):
    """Load via Semantic Rails' package loader.

    DuckDB packages declare a `package.seed.source:` path that must
    exist on disk at load time. The translator emits a relative
    placeholder (``data/seed_<id>.sql``); for tests we materialize a
    stub at that path inside the emitted package.
    """
    from semantic_rails.config import load_package_config

    seed = package_dir / "data" / f"seed_{seed_basename}.sql"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.touch()

    return load_package_config(package_dir)


def test_entity_count_uses_column_not_entity_name(tmp_path):
    """Regression: mf2sr previously emitted ``entity_key: <entity_name>``
    on ``kind: entity_count`` measures. Semantic Rails interprets
    ``entity_key:`` as a column name, so the compiler emitted
    ``COUNT(DISTINCT orders."order")`` and the warehouse binder
    failed. The fix resolves ``entity_key`` to the canonical key
    column from the graph (with per-model FK ``expr:`` override).
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "semantic_model": {
                    "name": "orders",
                    "node_relation": {"alias": "fct_orders"},
                    "defaults": {"agg_time_dimension": "ordered_at"},
                    "entities": [
                        {"name": "order", "type": "primary", "expr": "order_id"},
                    ],
                    "dimensions": [
                        {
                            "name": "ordered_at",
                            "type": "time",
                            "type_params": {"time_granularity": "day"},
                        },
                    ],
                    "measures": [
                        {"name": "order_count", "expr": "1", "agg": "sum"},
                    ],
                }
            }
        )
    )

    translate(src, tmp_path / "out", package_id="ent_test", warehouse="duckdb")

    model_yaml = yaml.safe_load(
        (tmp_path / "out" / "ent_test" / "models" / "orders.yml").read_text()
    )
    measure = model_yaml["model"]["measures"]["order_count"]
    assert measure["kind"] == "entity_count"
    assert measure["entity_key"] == "order_id", (
        f"entity_key must be the canonical key column, got {measure['entity_key']!r}"
    )


def test_translator_emits_loadable_package(tmp_path):
    """Smoke test: a minimal MetricFlow project (two semantic_models,
    three metrics covering ``aggregate`` / ``ratio``) translates to a
    Semantic Rails package the loader accepts. Asserts the loaded
    config exposes the expected entities, measures, and metrics."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "semantic_model": {
                    "name": "orders",
                    "description": "orders fact table",
                    "node_relation": {"alias": "fct_orders"},
                    "defaults": {"agg_time_dimension": "ordered_at"},
                    "entities": [
                        {"name": "order", "type": "primary", "expr": "order_id"},
                        {"name": "customer", "type": "foreign", "expr": "customer_id"},
                    ],
                    "dimensions": [
                        {
                            "name": "ordered_at",
                            "type": "time",
                            "type_params": {"time_granularity": "day"},
                        },
                        {"name": "channel", "type": "categorical"},
                    ],
                    "measures": [
                        {"name": "orders", "expr": "1", "agg": "sum"},
                        {"name": "revenue_usd", "expr": "amount_usd", "agg": "sum"},
                    ],
                }
            }
        )
    )
    (src / "customers.yaml").write_text(
        yaml.safe_dump(
            {
                "semantic_model": {
                    "name": "customers",
                    "description": "customers dim",
                    "node_relation": {"alias": "dim_customers"},
                    "entities": [
                        {"name": "customer", "type": "primary", "expr": "customer_id"},
                    ],
                }
            }
        )
    )
    (src / "metrics.yaml").write_text(
        yaml.safe_dump_all(
            [
                {
                    "metric": {
                        "name": "orders",
                        "description": "Orders placed",
                        "type": "simple",
                        "type_params": {"measure": {"name": "orders"}},
                    }
                },
                {
                    "metric": {
                        "name": "revenue_usd",
                        "description": "Revenue (USD)",
                        "type": "simple",
                        "type_params": {"measure": {"name": "revenue_usd"}},
                    }
                },
                {
                    "metric": {
                        "name": "aov",
                        "description": "Average order value",
                        "type": "ratio",
                        "type_params": {
                            "numerator": {"name": "revenue_usd"},
                            "denominator": {"name": "orders"},
                        },
                    }
                },
            ]
        )
    )

    report = translate(
        src,
        tmp_path / "out",
        package_id="shop_smoke",
        warehouse="duckdb",
    )
    assert "orders" in report.models_emitted
    assert "customers" in report.models_emitted
    assert "aov" in report.metrics_emitted

    cfg = _load_package(report.package_dir, seed_basename="shop_smoke")
    entity_labels = {e.label for e in cfg.entities}
    assert {"Order", "Customer"} <= entity_labels
    measure_labels = {m.label for m in cfg.measures}
    assert "Orders" in measure_labels
    metric_ids = {m.id for m in cfg.metric_recipes}
    assert "metric.shop_smoke.aov" in metric_ids
    assert "metric.shop_smoke.revenue_usd" in metric_ids


def test_cli_warehouse_choices_track_dialect_registry():
    """`mf2sr --warehouse` must accept every warehouse registered in
    `semantic_rails.dialects` — not a hardcoded subset. A new dialect
    becomes translatable the moment it is registered."""
    from mf2sr.cli import _build_parser
    from semantic_rails.dialects import supported_warehouses

    parser = _build_parser()
    warehouse_action = next(action for action in parser._actions if action.dest == "warehouse")
    assert tuple(warehouse_action.choices) == supported_warehouses()
    assert warehouse_action.default == "duckdb"
    assert {"duckdb", "snowflake", "postgres", "clickhouse"} <= set(warehouse_action.choices)


def test_cli_subcommand_round_trips(tmp_path):
    """End-to-end smoke for the `semantic-rails import` subcommand.

    Invokes the CLI as a subprocess against an inline minimal
    MetricFlow project and parses the JSON envelope. This exercises
    the wiring from `semantic_rails/cli.py` through to
    `mf2sr.translate()` — the other tests call `translate()`
    directly and would miss subparser typos or import-path bugs.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "semantic_model": {
                    "name": "orders",
                    "node_relation": {"alias": "fct_orders"},
                    "defaults": {"agg_time_dimension": "ordered_at"},
                    "entities": [
                        {"name": "order", "type": "primary", "expr": "order_id"},
                    ],
                    "dimensions": [
                        {
                            "name": "ordered_at",
                            "type": "time",
                            "type_params": {"time_granularity": "day"},
                        },
                    ],
                    "measures": [
                        {"name": "order_count", "expr": "1", "agg": "sum"},
                    ],
                }
            }
        )
    )
    (src / "metrics.yaml").write_text(
        yaml.safe_dump(
            {
                "metric": {
                    "name": "orders",
                    "description": "Total orders",
                    "type": "simple",
                    "type_params": {"measure": {"name": "order_count"}},
                }
            }
        )
    )

    out_dir = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails",
            "import",
            "--from",
            "metricflow",
            "--source",
            str(src),
            "--output",
            str(out_dir),
            "--package-id",
            "cli_smoke",
            "--warehouse",
            "duckdb",
            "--default-db",
            str(tmp_path / "cli_smoke.duckdb"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["package_dir"].endswith("cli_smoke")
    assert "orders" in envelope["models_emitted"]
    assert "orders" in envelope["metrics_emitted"]
    assert (out_dir / "cli_smoke" / "package.yml").exists()
