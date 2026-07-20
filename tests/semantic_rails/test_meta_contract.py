"""Tests for the meta-key contract and model-meta cascade."""

from __future__ import annotations

from pathlib import Path

import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import _compiled_package_warnings


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_minimal_package(
    package_dir: Path,
    *,
    defaults_extra: dict | None = None,
    model_extra: dict | None = None,
    measure_extra: dict | None = None,
    extra_measures: dict | None = None,
    metric_extra: dict | None = None,
) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "meta_demo",
                "name": "meta_demo",
                "description": "Meta-contract demo",
                "default_db": "data/example.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                },
                **dict(defaults_extra or {}),
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "id": "entity.demo_order",
                        "name": "demo.Order",
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    }
                }
            }
        },
    )
    measures = {
        "order_count": {
            "id": "measure.demo.order_count",
            "name": "sales.orders",
            "label": "Orders",
            "kind": "entity_count",
            "time": "ordered_at",
            "publish": {"id": "metric.sales.orders"},
            **dict(measure_extra or {}),
        }
    }
    measures.update(dict(extra_measures or {}))
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "order_fact",
                "grain": ["order_id"],
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": measures,
                **dict(model_extra or {}),
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {"metrics": dict(metric_extra or {})},
    )


def test_meta_contract_unknown_key_emits_warning(tmp_path: Path):
    package_dir = tmp_path / "meta_demo"
    _write_minimal_package(
        package_dir,
        defaults_extra={
            "meta": {
                "measure": {
                    "fields": {
                        "owner_team": {"type": "string"},
                    },
                },
            },
        },
        measure_extra={
            "meta": {"owner_team": "finance", "mystery_field": "blue"},
        },
    )

    config = load_package_config(str(package_dir))

    # Loader does not raise — the contract is advisory.
    assert config.meta_contract["measure"]["fields"]["owner_team"]["type"] == "string"
    measure = next(m for m in config.measures if m.id == "measure.demo.order_count")
    assert measure.meta == {"owner_team": "finance", "mystery_field": "blue"}

    warnings = _compiled_package_warnings(config, Path(package_dir))
    mystery_warnings = [
        w
        for w in warnings
        if "mystery_field" in w and "not declared in defaults.meta.measure.fields" in w
    ]
    assert len(mystery_warnings) >= 1
    # And no SemanticLayerError-style error path was triggered.


def test_meta_contract_enum_violation_emits_warning(tmp_path: Path):
    package_dir = tmp_path / "meta_demo"
    _write_minimal_package(
        package_dir,
        defaults_extra={
            "meta": {
                "measure": {
                    "fields": {
                        "owner_team": {
                            "type": "enum",
                            "values": ["finance", "growth", "ops"],
                        },
                    },
                },
            },
        },
        measure_extra={"meta": {"owner_team": "legal"}},
    )

    config = load_package_config(str(package_dir))
    warnings = _compiled_package_warnings(config, Path(package_dir))
    enum_warnings = [
        w for w in warnings if "owner_team" in w and "legal" in w and "declared values" in w
    ]
    assert len(enum_warnings) >= 1


def test_model_meta_cascades_to_measures_with_override(tmp_path: Path):
    package_dir = tmp_path / "meta_demo"
    _write_minimal_package(
        package_dir,
        model_extra={"meta": {"owner_team": "finance", "region": "us"}},
        # measure A (order_count) inherits everything from model.
        # measure B overrides owner_team.
        extra_measures={
            "order_total": {
                "id": "measure.demo.order_total",
                "name": "sales.order_total",
                "label": "Order total",
                "kind": "additive",
                "time": "ordered_at",
                "expr": {"kind": "column", "column": "order_total"},
                "meta": {"owner_team": "growth"},
            },
        },
    )

    config = load_package_config(str(package_dir))
    by_id = {m.id: m for m in config.measures}

    measure_a = by_id["measure.demo.order_count"]
    assert measure_a.meta == {"owner_team": "finance", "region": "us"}

    measure_b = by_id["measure.demo.order_total"]
    assert measure_b.meta == {"owner_team": "growth", "region": "us"}
