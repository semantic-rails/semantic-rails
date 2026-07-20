from __future__ import annotations

import json
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from semantic_rails import cli as cli_module
from semantic_rails import config as config_module
from semantic_rails.config import resolve_repo_path
from semantic_rails.config_validation import (
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)
from semantic_rails.db import Database, SnowflakeCliAdapter, load_csv_dir_to_duckdb
from semantic_rails.runtime import Runtime
from semantic_rails.yaml_loader import safe_load as yaml12_safe_load


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _minimal_package_payload(package_id: str) -> dict:
    return {
        "schema_version": 1,
        "package": {
            "id": package_id,
            "name": package_id,
            "description": f"{package_id} demo package",
            "warehouse": "duckdb",
            "default_db": "data/example.duckdb",
            "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
        },
        "defaults": {
            "time": {
                "timezone": "UTC",
                "default_query_axis": False,
                "supported_grains": ["day", "week", "month", "quarter", "year"],
            }
        },
    }


def _minimal_snowflake_package_payload(package_id: str) -> dict:
    return {
        "schema_version": 1,
        "package": {
            "id": package_id,
            "name": package_id,
            "description": f"{package_id} snowflake demo package",
            "warehouse": "snowflake",
            "connection": {"kind": "snowflake_cli", "name": "semantic_views_trial"},
        },
        "defaults": {
            "time": {
                "timezone": "UTC",
                "default_query_axis": False,
                "supported_grains": ["day", "week", "month", "quarter", "year"],
            }
        },
    }


def _write_seed_sql(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
CREATE TABLE order_fact (
  order_id INTEGER,
  ordered_at TIMESTAMP
);

INSERT INTO order_fact VALUES
  (1, '2024-01-01 00:00:00'),
  (2, '2024-01-02 00:00:00');
""".strip(),
        encoding="utf-8",
    )


def _write_minimal_package(
    package_dir: Path,
    *,
    extra_measures: dict | None = None,
    extra_metrics: dict | None = None,
    extra_dimensions: dict | None = None,
    extra_times: dict | None = None,
    operational_defaults: dict | None = None,
    model_operational_defaults: dict | None = None,
) -> None:
    _write_yaml(package_dir / "package.yml", _minimal_package_payload(package_dir.name))
    if operational_defaults:
        payload = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
        payload.setdefault("defaults", {})["operational"] = operational_defaults
        _write_yaml(package_dir / "package.yml", payload)
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
    model_payload = {
        "model": {
            "id": "orders",
            "entity": "order",
            "relation": "order_fact",
            "grain": ["order_id"],
            "keys": {"primary": ["order_id"]},
            "times": {
                "ordered_at": {
                    "id": "temporal_role.demo_order_time",
                    "name": "demo.Order.ordered_at",
                    "label": "Order time",
                    "column": "ordered_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "default_query_axis": True,
                },
                **dict(extra_times or {}),
            },
            "measures": {
                "order_count": {
                    "id": "measure.demo.order_count",
                    "name": "sales.orders",
                    "label": "Orders",
                    "description": "Order count",
                    "kind": "entity_count",
                    "time": "ordered_at",
                    "topics": ["orders"],
                    "publish": {"id": "metric.sales.orders"},
                },
                **dict(extra_measures or {}),
            },
        }
    }
    if extra_dimensions:
        model_payload["model"]["dimensions"] = dict(extra_dimensions)
    if model_operational_defaults:
        model_payload["model"]["operational_defaults"] = model_operational_defaults
    _write_yaml(package_dir / "models" / "orders.yml", model_payload)
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                **dict(extra_metrics or {}),
            }
        },
    )
    _write_seed_sql(package_dir / "data" / "seed_example.sql")


def test_measure_aggregation_warning(tmp_path: Path):
    package_dir = tmp_path / "aggregation_warning_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "customer_distinct_bad": {
                "id": "measure.demo.customer_distinct_bad",
                "name": "sales.bad_customer_distinct",
                "label": "Bad customer distinct",
                "description": "Badly authored customer distinct count",
                "expr": "order_id",
                "aggregation": "count_distinct",
                "time": "ordered_at",
                "topics": ["customers"],
                "publish": {"id": "metric.sales.bad_customer_distinct"},
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert any(
        warning["message"].endswith(
            "use kind: entity_count with entity_key for count-distinct measures"
        )
        for warning in report["warnings"]
    )


def test_csv_null_strings_default(tmp_path: Path):
    csv_dir = tmp_path / "seed"
    csv_dir.mkdir()
    (csv_dir / "events.csv").write_text(
        "event_id,event_at\n1,\n2,2026-01-01 00:00:00\n", encoding="utf-8"
    )
    db_path = tmp_path / "seed.duckdb"

    load_csv_dir_to_duckdb(str(db_path), str(csv_dir))
    db = Database.connect(str(db_path), read_only=True)
    try:
        rows = db.query("SELECT event_id, event_at FROM events ORDER BY event_id")
    finally:
        db.close()

    assert len(rows) == 2
    assert rows[0]["event_at"] is None
    assert rows[1]["event_at"] is not None


def _write_monolithic_package(path: Path, package_id: str) -> None:
    payload = _minimal_package_payload(package_id)
    payload["graph"] = {
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
    payload["models"] = {
        "orders": {
            "id": "orders",
            "entity": "order",
            "relation": "order_fact",
            "grain": ["order_id"],
            "keys": {"primary": ["order_id"]},
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
            "measures": {
                "order_count": {
                    "id": "measure.demo.order_count",
                    "name": "sales.orders",
                    "label": "Orders",
                    "description": "Order count",
                    "kind": "entity_count",
                    "time": "ordered_at",
                    "topics": ["orders"],
                    "publish": {"id": "metric.sales.orders"},
                }
            },
        }
    }
    payload["metrics"] = {}
    _write_yaml(path, payload)


def _write_minimal_snowflake_package(package_dir: Path) -> None:
    _write_yaml(package_dir / "package.yml", _minimal_snowflake_package_payload(package_dir.name))
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "id": "entity.tpch_order",
                        "name": "tpch.Order",
                        "label": "Order",
                        "key": ["O_ORDERKEY"],
                        "model": "orders",
                    }
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS",
                "grain": ["O_ORDERKEY"],
                "keys": {"primary": ["O_ORDERKEY"]},
                "dimensions": {
                    "order_priority": {
                        "id": "dimension.tpch_order_priority",
                        "name": "tpch.Order.priority",
                        "label": "Order priority",
                        "column": "O_ORDERPRIORITY",
                        "kind": "categorical",
                    }
                },
                "times": {
                    "order_date": {
                        "id": "temporal_role.tpch_order_date",
                        "name": "tpch.Order.order_date",
                        "label": "Order date",
                        "column": "O_ORDERDATE",
                        "kind": "date",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    "order_count": {
                        "id": "measure.tpch.order_count",
                        "name": "sales.orders",
                        "label": "Orders",
                        "description": "Count of TPCH orders.",
                        "kind": "entity_count",
                        "entity_key": ["O_ORDERKEY"],
                        "time": "order_date",
                        "topics": ["orders"],
                        "publish": {"id": "metric.sales.orders"},
                    },
                    "revenue": {
                        "id": "measure.tpch.revenue",
                        "name": "sales.revenue",
                        "label": "Revenue",
                        "description": "TPCH order revenue.",
                        "expr": {"kind": "column", "column": "O_TOTALPRICE"},
                        "time": "order_date",
                        "topics": ["revenue"],
                        "publish": {"id": "metric.sales.revenue"},
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "sales.average_order_value": {
                    "id": "metric.sales.average_order_value",
                    "name": "sales.average_order_value",
                    "label": "Average order value",
                    "description": "Revenue divided by orders.",
                    "kind": "derived",
                    "temporal_role": "temporal_role.tpch_order_date",
                    "topics": ["revenue", "orders"],
                    "expression": {
                        "kind": "binary",
                        "op": "divide",
                        "left": {"kind": "metric", "metric": "metric.sales.revenue"},
                        "right": {"kind": "metric", "metric": "metric.sales.orders"},
                    },
                }
            }
        },
    )


def _copy_external_jaffle_package(tmp_path: Path) -> Path:
    source = Path(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    target = tmp_path / "jaffle_shop"
    shutil.copytree(source, target)
    shutil.copytree(Path(resolve_repo_path("data/jaffle_csv")), target / "data" / "jaffle_csv")
    shutil.copy2(
        Path(resolve_repo_path("data/seed_jaffle.sql")), target / "data" / "seed_jaffle.sql"
    )
    return target


def test_resolve_package_reference_supports_package_id_and_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    package_dir = tmp_path / "reference_demo"
    _write_minimal_package(package_dir)
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"reference_demo": str(package_dir)}
    )

    by_id = resolve_package_reference(package_id="reference_demo")
    by_path = resolve_package_reference(path=str(package_dir))

    assert by_id.source_path == str(package_dir)
    assert by_id.package_id == "reference_demo"
    assert by_path.source_path == str(package_dir)
    assert by_path.package_id == ""


def test_runtime_from_path_prefers_package_relative_assets_for_external_packages(tmp_path: Path):
    package_dir = _copy_external_jaffle_package(tmp_path)
    runtime = Runtime.from_path(str(package_dir))
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "limit": 0,
            }
        )
        assert result["row_count"] == 0
        assert runtime.db_path == str(package_dir / "data" / "jaffle_shop.duckdb")
        assert Path(runtime.db_path).exists()
    finally:
        runtime.close()


def test_parse_config_succeeds_for_monolithic_yaml(tmp_path: Path):
    package_file = tmp_path / "monolithic.yml"
    _write_monolithic_package(package_file, "monolithic_demo")

    report, _ = parse_config_report(resolve_package_reference(path=str(package_file)))

    assert report["ok"] is True
    assert report["package"]["id"] == "monolithic_demo"


def test_parse_config_succeeds_for_snowflake_package(tmp_path: Path):
    package_dir = tmp_path / "snowflake_demo"
    _write_minimal_snowflake_package(package_dir)

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert report["package"]["id"] == "snowflake_demo"
    assert config is not None
    assert config.package.warehouse == "snowflake"
    assert config.package.connection.kind == "snowflake_cli"
    assert config.package.connection.name == "semantic_views_trial"


def test_jaffle_reference_package_has_release_authoring_profile():
    report, config = parse_config_report(resolve_package_reference(package_id="jaffle_shop"))
    assert report["ok"] is True
    # The reference package intentionally retains one independently computed
    # metric/measure pair with the same label. Other authoring-quality
    # warnings still must be zero.
    non_collision_warnings = [
        w
        for w in report["warnings"]
        if not (isinstance(w, dict) and str(w.get("code", "")).startswith("SEMANTIC_TERM_"))
    ]
    assert non_collision_warnings == []
    collision_warnings = [
        w
        for w in report["warnings"]
        if isinstance(w, dict) and str(w.get("code", "")).startswith("SEMANTIC_TERM_")
    ]
    assert [warning["details"]["object_ids"] for warning in collision_warnings] == [
        [
            "measure.jaffle.month_over_month_revenue_growth_ratio",
            "metric.sales.month_over_month_revenue_growth",
        ]
    ]

    # Authoring quality is checked via meta fields (owner_team,
    # review_priority, change_risk). The test runner walks
    # `<package>/tests/*.yml` directly — there is no per-measure or
    # per-metric `tests:` cross-reference field.
    missing_measures = [
        measure.id
        for measure in config.measures
        if not measure.meta.get("owner_team")
        or not measure.meta.get("review_priority")
        or not measure.meta.get("change_risk")
    ]
    assert missing_measures == []

    measure_metric_ids = {
        f"metric.{measure.name or measure.id.split('measure.', 1)[-1]}"
        for measure in config.measures
    }
    missing_curated_metrics = [
        recipe.id
        for recipe in config.metric_recipes
        if not (recipe.id in measure_metric_ids and recipe.kind in {"aggregate", "semi_additive"})
        and (
            not recipe.meta.get("owner_team")
            or not recipe.meta.get("review_priority")
            or not recipe.meta.get("change_risk")
        )
    ]
    assert missing_curated_metrics == []


def test_parse_config_accepts_standard_snowflake_cli_options(tmp_path: Path):
    package_dir = tmp_path / "snowflake_options_demo"
    _write_minimal_snowflake_package(package_dir)
    package_root = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    package_root["package"]["connection"]["options"] = {
        "database": "SNOWFLAKE_SAMPLE_DATA",
        "schema": "TPCH_SF1",
        "warehouse": "COMPUTE_WH",
        "role": "ANALYST",
    }
    _write_yaml(package_dir / "package.yml", package_root)

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert config is not None
    assert config.package.connection.options == {
        "database": "SNOWFLAKE_SAMPLE_DATA",
        "schema": "TPCH_SF1",
        "warehouse": "COMPUTE_WH",
        "role": "ANALYST",
    }


def test_yaml_loader_uses_yaml_1_2_boolean_resolution():
    payload = yaml12_safe_load(
        """
truthy: true
falsey: false
enabled: on
disabled: off
confirmed: yes
denied: no
""".strip()
    )

    assert payload == {
        "truthy": True,
        "falsey": False,
        "enabled": "on",
        "disabled": "off",
        "confirmed": "yes",
        "denied": "no",
    }


def test_parse_config_uses_yaml_1_2_loader_for_unquoted_connection_options(tmp_path: Path):
    package_dir = tmp_path / "snowflake_yaml12_demo"
    _write_minimal_snowflake_package(package_dir)
    (package_dir / "package.yml").write_text(
        f"""
schema_version: 1
package:
  id: {package_dir.name}
  name: {package_dir.name}
  description: Snowflake YAML 1.2 demo
  warehouse: snowflake
  connection:
    kind: snowflake_cli
    name: semantic_views_trial
    options:
      database: SNOWFLAKE_SAMPLE_DATA
      schema: yes
      warehouse: off
      role: on
defaults:
  time:
    timezone: UTC
    default_query_axis: true
    supported_grains: [day]
""".lstrip(),
        encoding="utf-8",
    )

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert config is not None
    assert config.package.connection.options["schema"] == "yes"
    assert config.package.connection.options["warehouse"] == "off"
    assert config.package.connection.options["role"] == "on"


def test_parse_config_accepts_snowflake_native_secret_indirection(tmp_path: Path):
    package_dir = tmp_path / "snowflake_native_demo"
    _write_minimal_snowflake_package(package_dir)
    payload = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    payload["package"]["connection"] = {
        "kind": "snowflake_native",
        "name": "prod_native",
        "options": {
            "account_env": "SNOW_ACCOUNT",
            "user_env": "SNOW_USER",
            "password_env": "SNOW_PASSWORD",
            "database": "ANALYTICS",
            "schema": "CORE",
            "warehouse": "COMPUTE_WH",
            "role": "ANALYST",
            "query_tag": "semantic-rails",
            "statement_timeout_seconds": "60",
        },
    }
    _write_yaml(package_dir / "package.yml", payload)

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert config is not None
    assert config.package.connection.kind == "snowflake_native"
    assert config.package.connection.options["password_env"] == "SNOW_PASSWORD"


def test_parse_config_accepts_snowflake_native_direct_options_without_name(tmp_path: Path):
    package_dir = tmp_path / "snowflake_native_direct_demo"
    _write_minimal_snowflake_package(package_dir)
    payload = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    payload["package"]["connection"] = {
        "kind": "snowflake_native",
        "options": {
            "account_env": "SNOW_ACCOUNT",
            "user_env": "SNOW_USER",
            "password_env": "SNOW_PASSWORD",
            "database": "ANALYTICS",
        },
    }
    _write_yaml(package_dir / "package.yml", payload)

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert config is not None
    assert config.package.connection.kind == "snowflake_native"
    assert config.package.connection.name == ""


def test_parse_config_rejects_snowflake_native_direct_options_without_auth(tmp_path: Path):
    package_dir = tmp_path / "snowflake_native_direct_incomplete"
    _write_minimal_snowflake_package(package_dir)
    payload = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    payload["package"]["connection"] = {
        "kind": "snowflake_native",
        "options": {
            "account_env": "SNOW_ACCOUNT",
            "user_env": "SNOW_USER",
        },
    }
    _write_yaml(package_dir / "package.yml", payload)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any(
        "snowflake_native direct connection" in error["message"] for error in report["errors"]
    )


def test_parse_config_rejects_snowflake_cli_without_name(tmp_path: Path):
    package_dir = tmp_path / "snowflake_cli_missing_name"
    _write_minimal_snowflake_package(package_dir)
    payload = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    payload["package"]["connection"] = {"kind": "snowflake_cli"}
    _write_yaml(package_dir / "package.yml", payload)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any(
        "snowflake_cli packages must declare package.connection.name" in error["message"]
        for error in report["errors"]
    )


def test_parse_config_rejects_unsupported_snowflake_cli_options(tmp_path: Path):
    package_dir = tmp_path / "snowflake_bad_options_demo"
    _write_minimal_snowflake_package(package_dir)
    package_root = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
    package_root["package"]["connection"]["options"] = {"authenticator": "externalbrowser"}
    _write_yaml(package_dir / "package.yml", package_root)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any(
        "unsupported package.connection option 'authenticator'" in error["message"]
        for error in report["errors"]
    )


def test_parse_config_reports_invalid_yaml(tmp_path: Path):
    broken = tmp_path / "broken.yml"
    broken.write_text("package: [", encoding="utf-8")

    report, _ = parse_config_report(resolve_package_reference(path=str(broken)))

    assert report["ok"] is False
    assert "invalid YAML" in report["errors"][0]["message"]


def test_parse_config_reports_missing_graph_file(tmp_path: Path):
    package_dir = tmp_path / "missing_graph"
    _write_yaml(package_dir / "package.yml", _minimal_package_payload("missing_graph"))

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any("graph.yml is missing" in error["message"] for error in report["errors"])


def test_parse_config_reports_broken_graph_model_reference(tmp_path: Path):
    package_dir = tmp_path / "broken_graph"
    _write_minimal_package(package_dir)
    graph = yaml.safe_load((package_dir / "graph.yml").read_text(encoding="utf-8"))
    graph["graph"]["entities"]["order"]["model"] = "missing_model"
    _write_yaml(package_dir / "graph.yml", graph)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any(
        "does not resolve to a declared model" in error["message"] for error in report["errors"]
    )


def test_parse_config_reports_loader_failures(tmp_path: Path):
    package_dir = tmp_path / "loader_failure"
    _write_minimal_package(
        package_dir,
        model_operational_defaults={"owner": "finance"},
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any("failed to load package config" in error["message"] for error in report["errors"])


def test_validate_config_succeeds_on_jaffle_shop(tmp_path: Path):
    package_dir = _copy_external_jaffle_package(tmp_path)

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert report["summary"]["failed"] == 0
    assert report["summary"]["probes_total"] == len(report["probes"])
    assert report["summary"]["probes_total"] > 0


def test_validate_config_writes_compiled_manifest(tmp_path: Path):
    """`validate-config` writes `.compiled/manifest.json` so the MCP
    catalog handler can read from it on hot paths instead of recomputing
    from the registry every request."""
    package_dir = tmp_path / "manifest_demo"
    _write_minimal_package(package_dir)

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    assert report["ok"] is True

    # validate-config itself doesn't write — the CLI wrapper does. Call
    # write_manifest directly to assert the artifact's shape and on-disk
    # location are stable.
    from semantic_rails.manifest import write_manifest

    runtime = Runtime.from_path(str(package_dir))
    try:
        manifest_path = write_manifest(runtime)
    finally:
        runtime.close()

    assert manifest_path.exists()
    assert manifest_path.parent.name == ".compiled"
    payload = json.loads(manifest_path.read_text())
    assert payload["schema_version"] == 1
    assert payload["fingerprint"]
    assert payload["catalogs"]
    # Default variants must include the keys used by the MCP catalog
    # handler's hot path.
    assert "summary|summary" in payload["catalogs"]
    assert "summary|compact" in payload["catalogs"]
    assert "summary|full" in payload["catalogs"]


def test_runtime_manifest_catalog_returns_isolated_dicts(tmp_path: Path):
    """The Runtime.manifest_catalog reader must return freshly parsed
    dict trees so middleware mutation cannot poison the manifest cache."""
    package_dir = tmp_path / "manifest_isolation_demo"
    _write_minimal_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    try:
        from semantic_rails.manifest import write_manifest

        write_manifest(runtime)
        first = runtime.manifest_catalog(view="summary", verbosity="compact")
        assert first is not None
        # Poison the returned dict — a subsequent call must be untouched.
        first["__corrupted"] = True
        second = runtime.manifest_catalog(view="summary", verbosity="compact")
        assert second is not None
        assert "__corrupted" not in second
    finally:
        runtime.close()


def test_runtime_manifest_catalog_returns_none_when_no_manifest(tmp_path: Path):
    """No manifest → graceful None, callers fall back to live compute."""
    package_dir = tmp_path / "no_manifest_demo"
    _write_minimal_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    try:
        assert runtime.manifest_catalog(view="summary", verbosity="compact") is None
    finally:
        runtime.close()


def test_shared_catalog_resolver_bypasses_manifest_for_every_policy_context(monkeypatch):
    from semantic_rails import catalog_service

    class FakeRuntime:
        def request_scope(self):
            return nullcontext(self)

        def manifest_catalog(self, *, view, verbosity):
            return {"source": "manifest", "view": view, "verbosity": verbosity}

    monkeypatch.setattr(
        catalog_service,
        "catalog_payload",
        lambda _runtime, **kwargs: {"source": "live", **kwargs},
    )
    runtime = FakeRuntime()

    assert catalog_service.resolve_catalog(runtime)["source"] == "manifest"
    for context in (
        {"roles": ["finance"]},
        {"tenant": "tenant-a"},
        {"actor": "user-a"},
        {"project": "project-a"},
        {"environment": "prod"},
        {"audience": "internal"},
    ):
        resolved = catalog_service.resolve_catalog(runtime, policy_context=context)
        assert resolved["source"] == "live"
        assert resolved["policy_context"] == context

    assert catalog_service.resolve_catalog(runtime, search="orders")["source"] == "live"


def test_validate_config_warns_on_semantic_collision(tmp_path: Path):
    """Two measures with identical labels (e.g. "Revenue") must trigger
    a collision warning so the package author knows MCP `discover` will
    have a hard time distinguishing them."""
    package_dir = tmp_path / "collision_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "revenue_a": {
                "id": "measure.demo.revenue_a",
                "name": "sales.revenue_a",
                "label": "Revenue",
                "description": "Revenue computed via path A",
                "kind": "aggregate",
                "expr": "revenue_a",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
            "revenue_b": {
                "id": "measure.demo.revenue_b",
                "name": "sales.revenue_b",
                "label": "Revenue",
                "description": "Revenue computed via path B",
                "kind": "aggregate",
                "expr": "revenue_b",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
        },
    )

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    collision_warnings = [
        w
        for w in report.get("warnings", [])
        if isinstance(w, dict) and w.get("code") == "SEMANTIC_TERM_COLLISION"
    ]
    assert collision_warnings, (
        f"expected a collision warning for two measures with label='Revenue'; "
        f"got warnings={report.get('warnings', [])}"
    )
    warning = collision_warnings[0]
    assert warning["severity"] == "warning"
    assert warning["details"]["collision_type"] == "exact_normalized"
    assert warning["details"]["normalized_term"] == "revenue"
    assert warning["details"]["object_ids"] == [
        "measure.demo.revenue_a",
        "measure.demo.revenue_b",
    ]
    assert all(row["matching_fields"] == ["label"] for row in warning["details"]["objects"])
    assert warning["details"]["recovery_hints"]


def test_validate_config_warns_on_alias_vs_canonical_name_collision(tmp_path: Path):
    package_dir = tmp_path / "alias_collision_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "booked_revenue": {
                "id": "measure.demo.booked_revenue",
                "name": "finance.booked_revenue",
                "label": "Booked revenue",
                "synonyms": ["Sales Revenue"],
                "description": "Revenue at booking time",
                "kind": "aggregate",
                "expr": "booked_revenue",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
            "recognized_revenue": {
                "id": "measure.demo.recognized_revenue",
                "name": "sales.revenue",
                "label": "Recognized revenue",
                "description": "Revenue after recognition",
                "kind": "aggregate",
                "expr": "recognized_revenue",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))
    warning = next(
        row
        for row in report["warnings"]
        if row.get("code") == "SEMANTIC_TERM_COLLISION"
        and row.get("details", {}).get("normalized_term") == "salesrevenue"
    )

    objects = {row["id"]: row for row in warning["details"]["objects"]}
    assert objects["measure.demo.booked_revenue"]["matching_fields"] == ["alias"]
    assert objects["measure.demo.recognized_revenue"]["matching_fields"] == ["canonical_name"]


def test_validate_config_warns_on_single_edit_semantic_term_collision(tmp_path: Path):
    package_dir = tmp_path / "near_collision_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "recurring_revenue": {
                "id": "measure.demo.recurring_revenue",
                "name": "sales.recurring_revenue",
                "label": "Recurring revenue",
                "description": "Recurring revenue",
                "kind": "aggregate",
                "expr": "recurring_revenue",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
            "recurring_revenue_typo": {
                "id": "measure.demo.recurring_revenue_typo",
                "name": "sales.recurring_revenue_typo",
                "label": "Recurring reveneu",
                "description": "Recurring revenue from a second source",
                "kind": "aggregate",
                "expr": "recurring_revenue_typo",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))
    warning = next(
        row for row in report["warnings"] if row.get("code") == "SEMANTIC_TERM_NEAR_COLLISION"
    )

    assert warning["details"]["collision_type"] == "single_edit_typo"
    assert warning["details"]["differing_tokens"] == ["revenue", "reveneu"]
    assert warning["details"]["edit_distance"] == 1
    assert warning["details"]["recovery_hints"]


def test_validate_config_does_not_warn_on_qualified_semantic_siblings(tmp_path: Path):
    package_dir = tmp_path / "qualified_siblings_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "gross_revenue": {
                "id": "measure.demo.gross_revenue",
                "name": "finance.revenue",
                "label": "Revenue (gross)",
                "description": "Revenue before deductions",
                "kind": "aggregate",
                "expr": "gross_revenue",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
            "net_revenue": {
                "id": "measure.demo.net_revenue",
                "name": "sales.revenue",
                "label": "Revenue (net)",
                "description": "Revenue after deductions",
                "kind": "aggregate",
                "expr": "net_revenue",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))
    object_ids = {"measure.demo.gross_revenue", "measure.demo.net_revenue"}
    sibling_warnings = [
        row
        for row in report["warnings"]
        if str(row.get("code", "")).startswith("SEMANTIC_TERM_")
        and object_ids <= set(row.get("details", {}).get("object_ids", []))
    ]
    assert sibling_warnings == []


def test_validate_config_no_collision_warning_when_labels_differ(tmp_path: Path):
    """A package with distinct labels should produce no collision
    warning — the detector must not false-positive on standard
    domain authoring."""
    package_dir = tmp_path / "no_collision_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "gross_revenue": {
                "id": "measure.demo.gross_revenue",
                "name": "sales.gross_revenue",
                "label": "Gross revenue",
                "description": "Top-line revenue before refunds",
                "kind": "aggregate",
                "expr": "gross_revenue",
                "time": "ordered_at",
                "topics": ["revenue"],
            },
        },
    )

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    collision_warnings = [
        w
        for w in report.get("warnings", [])
        if isinstance(w, dict) and str(w.get("code", "")).startswith("SEMANTIC_TERM_")
    ]
    assert not collision_warnings, (
        f"unexpected collision warnings for a clean package; got {collision_warnings}"
    )


def test_validate_config_generates_query_time_for_time_required_metrics(tmp_path: Path):
    package_dir = tmp_path / "time_probe_demo"
    _write_minimal_package(
        package_dir,
        extra_metrics={
            "sales.orders_cumulative": {
                "id": "metric.sales.orders_cumulative",
                "name": "sales.orders_cumulative",
                "label": "Cumulative orders",
                "description": "Cumulative orders",
                "kind": "cumulative",
                "temporal_role": "temporal_role.demo_order_time",
                "topics": ["orders"],
                "expression": {
                    "kind": "cumulative",
                    "input": {"kind": "metric", "metric": "metric.sales.orders"},
                },
            }
        },
    )

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    cumulative = next(
        probe
        for probe in report["probes"]
        if probe["object_id"] == "metric.sales.orders_cumulative"
    )

    assert cumulative["ok"] is True
    assert cumulative["query"]["time"] == {
        "temporal_role": "temporal_role.demo_order_time",
        "grain": "day",
    }


def test_validate_config_continues_after_failures_and_reports_execution_errors(tmp_path: Path):
    package_dir = tmp_path / "probe_failure_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "broken_orders": {
                "id": "measure.demo.broken_orders",
                "name": "sales.broken_orders",
                "label": "Broken orders",
                "description": "Broken orders",
                "kind": "entity_count",
                "expr": {"kind": "column", "column": "missing_order_id"},
                "time": "ordered_at",
                "topics": ["orders"],
                "publish": False,
            }
        },
        extra_metrics={
            "sales.orders_cumulative_broken_role": {
                "id": "metric.sales.orders_cumulative_broken_role",
                "name": "sales.orders_cumulative_broken_role",
                "label": "Broken cumulative orders",
                "description": "Broken cumulative orders",
                "kind": "cumulative",
                "temporal_role": "temporal_role.missing",
                "topics": ["orders"],
                "expression": {
                    "kind": "cumulative",
                    "input": {"kind": "metric", "metric": "metric.sales.orders"},
                },
            }
        },
    )

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert report["summary"]["probes_total"] == len(report["probes"])
    assert report["summary"]["failed"] >= 2
    assert any(probe["ok"] for probe in report["probes"])

    broken_measure = next(
        probe for probe in report["probes"] if probe["object_id"] == "measure.demo.broken_orders"
    )
    broken_metric = next(
        probe
        for probe in report["probes"]
        if probe["object_id"] == "metric.sales.orders_cumulative_broken_role"
    )

    assert broken_measure["ok"] is False
    assert broken_measure["error"]["code"] == "QUERY_EXECUTION_ERROR"
    assert "rendered_sql" in broken_measure
    assert broken_metric["ok"] is False
    assert broken_metric["error"]["code"] == "INVALID_TEMPORAL_ROLE"


def test_validate_config_runs_snowflake_probes_with_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    package_dir = tmp_path / "snowflake_validate_demo"
    _write_minimal_snowflake_package(package_dir)
    queries: list[str] = []

    def _fake_query(self, sql: str, *, limits=None):
        queries.append(sql)
        return []

    monkeypatch.setattr(SnowflakeCliAdapter, "query", _fake_query)

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    assert report["summary"]["failed"] == 0
    assert report["summary"]["probes_total"] == 5
    assert len(queries) == 5
    assert any("SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS" in sql for sql in queries)
    average_order_value_probe = next(
        probe
        for probe in report["probes"]
        if probe["object_id"] == "metric.sales.average_order_value"
    )
    assert average_order_value_probe["ok"] is True


def test_cli_parse_config_emits_json_and_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    package_dir = tmp_path / "cli_parse_demo"
    _write_minimal_package(package_dir)
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"cli_parse_demo": str(package_dir)}
    )
    monkeypatch.setattr(
        sys, "argv", ["semantic-rails", "parse-config", "--package", "cli_parse_demo"]
    )

    cli_module.main()
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert payload["ok"] is True
    assert "Parsing package" in out.err


def test_cli_validate_config_exits_nonzero_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    package_dir = tmp_path / "cli_validate_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "broken_orders": {
                "id": "measure.demo.broken_orders",
                "name": "sales.broken_orders",
                "label": "Broken orders",
                "description": "Broken orders",
                "kind": "entity_count",
                "expr": {"kind": "column", "column": "missing_order_id"},
                "time": "ordered_at",
                "topics": ["orders"],
                "publish": False,
            }
        },
    )
    monkeypatch.setattr(
        sys, "argv", ["semantic-rails", "validate-config", "--path", str(package_dir)]
    )

    with pytest.raises(SystemExit) as exc:
        cli_module.main()

    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert exc.value.code == 1
    assert payload["ok"] is False
    assert "Validating measure" in out.err


def test_cli_validate_config_quiet_suppresses_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    package_dir = tmp_path / "cli_validate_quiet_demo"
    _write_minimal_package(package_dir)
    monkeypatch.setattr(
        sys, "argv", ["semantic-rails", "validate-config", "--path", str(package_dir), "--quiet"]
    )

    cli_module.main()
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert payload["ok"] is True
    assert out.err == ""


def test_cli_init_creates_runnable_starter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target = tmp_path / "starter_pkg"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "semantic-rails",
            "init",
            "--output",
            str(target),
            "--package-id",
            "starter_pkg",
            "--namespace",
            "starter",
        ],
    )

    cli_module.main()
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert payload["ok"] is True
    assert (target / "package.yml").exists()
    assert (target / "data" / "seed_example.sql").exists()
    report, config = parse_config_report(
        resolve_package_reference(path=str(target / "package.yml"))
    )
    assert report["ok"] is True
    assert config is not None
    assert config.package.package_id == "starter_pkg"


def test_cli_doctor_reports_package_mcp_and_docker_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(sys, "argv", ["semantic-rails", "doctor", "--package", "jaffle_shop"])

    cli_module.main()
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert "checks" in payload
    assert any(check["name"] == "mcp_adapter" and check["ok"] for check in payload["checks"])


def test_key_roles_default_and_derive_relationship_cardinality(tmp_path: Path):
    package_dir = tmp_path / "key_role_demo"
    _write_yaml(package_dir / "package.yml", _minimal_package_payload(package_dir.name))
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "parent": {
                        "id": "entity.demo_parent",
                        "name": "demo.Parent",
                        "model": "parents",
                    },
                    "child": {"id": "entity.demo_child", "name": "demo.Child", "model": "children"},
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "models.yml",
        {
            "models": {
                "parents": {
                    "relation": "parent_dim",
                    "keys": {"primary": {"columns": ["parent_id"], "role": "unique"}},
                    "dimensions": {"parent_id": {"id": "dimension.demo_parent_id", "kind": "id"}},
                },
                "children": {
                    "relation": "child_fact",
                    "keys": {
                        "primary": ["child_id"],
                        "foreign": {"parent": {"columns": ["parent_id"], "role": "foreign"}},
                    },
                    "joins": {"parent": {"to": "parent"}},
                    "dimensions": {
                        "child_id": {"id": "dimension.demo_child_id", "kind": "id"},
                        "parent_id": {"id": "dimension.demo_child_parent_id", "kind": "id"},
                    },
                    "measures": {
                        "child_count": {
                            "id": "measure.demo.child_count",
                            "kind": "entity_count",
                            "expr": {"kind": "column", "column": "child_id"},
                            "publish": False,
                        }
                    },
                },
            }
        },
    )

    config = config_module.load_package_config(str(package_dir))
    parent = next(row for row in config.entities if row.id == "entity.demo_parent")
    child = next(row for row in config.entities if row.id == "entity.demo_child")
    rel = next(row for row in config.relationships if row.id == "relationship.children_parent")

    assert parent.key_roles == {"parent_id": "unique"}
    assert child.key_roles == {"child_id": "primary"}
    assert child.foreign_keys == {"parent": ["parent_id"]}
    assert child.foreign_key_roles == {"parent": "foreign"}
    assert rel.source_key_role == "foreign"
    assert rel.target_key_role == "unique"
    assert rel.cardinality == "N:1"
    assert rel.safety == "safe"


def test_parse_config_rejects_typo_in_dimension_kind(tmp_path: Path):
    """`kind: catagorical` (typo) must be rejected, not silently stored.

    Pre-fix behavior: the dimension was loaded with semantic_kind="catagorical"
    and data_type="string" (the fallback in _map_dimension_kind). The package
    "worked" but downstream consumers branching on the kind flag would treat
    the dimension as non-categorical.
    """
    package_dir = tmp_path / "typo_kind_demo"
    _write_minimal_package(
        package_dir,
        extra_dimensions={
            "channel": {
                "label": "Channel",
                "column": "channel",
                "kind": "catagorical",  # typo for "categorical"
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any(
        "dimension orders.channel has unknown kind 'catagorical'" in msg for msg in messages
    ), messages
    # The error must list valid kinds so the author can recover.
    assert any("categorical" in msg and "boolean" in msg for msg in messages), messages


def test_parse_config_accepts_canonical_dimension_kinds(tmp_path: Path):
    """All documented kinds should validate without error."""
    package_dir = tmp_path / "valid_kinds_demo"
    _write_minimal_package(
        package_dir,
        extra_dimensions={
            "status": {"label": "Status", "column": "status", "kind": "categorical"},
            "is_promo": {"label": "Is promo", "column": "is_promo", "kind": "boolean"},
            "item_count": {"label": "Items", "column": "item_count", "kind": "integer"},
            "discount_pct": {"label": "Discount", "column": "discount_pct", "kind": "percent"},
            "amount_usd": {"label": "Amount", "column": "amount_usd", "kind": "currency"},
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True, report["errors"]


def test_docs_minimal_example_validates_when_copy_pasted(tmp_path: Path):
    """The "Minimal example" section in docs/PACKAGE_AUTHORING.md must
    copy-paste into a fresh package and validate without errors.

    Pre-fix: the example used `key: order_id` (scalar), authored `grain:`
    alongside `entities:`, omitted `model:` on graph entities, and shipped
    a metric file that duplicated an auto-published metric. All four
    failed validation under `schema_strict: true`.

    This test extracts every fenced ```yaml`` block from the "Minimal
    example" section by filename comment, drops them into a tmp package,
    seeds a stub `data/seed.sql`, and runs `parse_config_report`. Keeps
    the docs and loader contracts in lockstep going forward.
    """
    import re

    doc_path = Path(__file__).resolve().parents[2] / "docs" / "PACKAGE_AUTHORING.md"
    doc_text = doc_path.read_text(encoding="utf-8")
    start = doc_text.index("## Minimal example")
    end = doc_text.index("\n## ", start + 1)
    section = doc_text[start:end]

    pattern = re.compile(r"```yaml\n# ([^\n]+)\n(.*?)```", re.DOTALL)
    blocks = pattern.findall(section)
    assert blocks, "no fenced yaml blocks found under 'Minimal example'"

    package_dir = tmp_path / "shop"
    package_dir.mkdir()
    written: list[str] = []
    for header, content in blocks:
        # Header is a filename comment like "package.yml", "models/orders.yml",
        # or "models/customers.yml — every graph entity needs a model: pointer".
        # The filename is the first whitespace-separated token, with any
        # trailing em-dash narrative stripped.
        filename = header.split(" ", 1)[0].split("—", 1)[0].strip()
        target = package_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(filename)

    # The docs reference data/seed.sql but the example doesn't show its
    # contents (it's not authoring guidance). Stub a seed that matches the
    # `relation:` columns referenced in the orders/customers models so the
    # loader is satisfied; the stub is never executed by parse-config.
    (package_dir / "data").mkdir(exist_ok=True)
    (package_dir / "data" / "seed.sql").write_text(
        "CREATE TABLE shop_order (\n"
        "  order_id INTEGER, customer_id INTEGER, ordered_at TIMESTAMP,\n"
        "  status VARCHAR, amount_usd DOUBLE\n"
        ");\n"
        "CREATE TABLE shop_customer (\n"
        "  customer_id INTEGER, signed_up_at TIMESTAMP\n"
        ");\n",
        encoding="utf-8",
    )

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True, (
        f"docs minimal example failed to validate. files: {written}. "
        f"errors: {[e.get('message') for e in report.get('errors', [])]}"
    )
    # Sanity-check that the expected shape compiled.
    assert config is not None
    entity_ids = {entity.id for entity in config.entities}
    assert "entity.shop_order" in entity_ids
    assert "entity.shop_customer" in entity_ids


def test_parse_config_rejects_typo_in_times_kind(tmp_path: Path):
    """`times.<role>.kind:` must be `date` or `timestamp`."""
    package_dir = tmp_path / "typo_time_kind_demo"
    _write_minimal_package(
        package_dir,
        extra_times={
            "shipped_at": {
                "label": "Shipped at",
                "column": "shipped_at",
                "kind": "datetimestamp",  # typo
                "class": "event_time",
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any(
        "times orders.shipped_at has unknown kind 'datetimestamp'" in msg for msg in messages
    ), messages
    assert any("date" in msg and "timestamp" in msg for msg in messages), messages


def _write_strict_minimal_package(
    package_dir: Path,
    *,
    extra_metrics: dict | None = None,
    extra_dimensions: dict | None = None,
    measure_overrides: dict | None = None,
    package_overrides: dict | None = None,
) -> None:
    """Build a minimal strict-mode-compliant package, then optionally
    inject violations. Use this as the baseline for strict-mode tests
    so a single violation under test is the only failure surfaced."""
    pkg = _minimal_package_payload(package_dir.name)
    pkg["package"]["schema_strict"] = True
    if package_overrides:
        pkg["package"].update(package_overrides)
    _write_yaml(package_dir / "package.yml", pkg)

    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    }
                }
            }
        },
    )

    measure_payload = {
        "label": "Order Count",
        "description": "Number of orders",
        "kind": "entity_count",
        "entity_key": "order",
        "accumulation": {"kind": "event"},
        "value_type": "count",
        "publish": False,  # author explicit metric below; avoids dup
    }
    if measure_overrides:
        measure_payload.update(measure_overrides)
    model_payload = {
        "model": {
            "id": "orders",
            "relation": "order_fact",
            "entities": {"order": {}},
            "times": {
                "ordered_at": {
                    "label": "Order time",
                    "column": "ordered_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "default": True,
                }
            },
            "measures": {"order_count": measure_payload},
        }
    }
    if extra_dimensions:
        model_payload["model"]["dimensions"] = dict(extra_dimensions)
    _write_yaml(package_dir / "models" / "orders.yml", model_payload)

    # Always author one valid metric. Tests that want to violate strict
    # mode on metrics pass extra_metrics that overrides or adds new ones.
    metrics = {
        "order_count_metric": {
            "label": "Order count",
            "description": "Total orders",
            "kind": "aggregate",
            "measure": "order_count",
            "value_type": "count",
        },
        **dict(extra_metrics or {}),
    }
    _write_yaml(package_dir / "metrics" / "metrics.yml", {"metrics": metrics})
    _write_seed_sql(package_dir / "data" / "seed_example.sql")


def test_strict_rejects_metric_without_value_type(tmp_path: Path):
    """Docs PACKAGE_AUTHORING.md L641 lists `Metric without value_type:`
    as a strict-mode rejection. Pre-fix: silently defaulted to "number".
    """
    package_dir = tmp_path / "strict_no_vt"
    _write_strict_minimal_package(
        package_dir,
        extra_metrics={
            "no_vt": {
                "label": "No VT",
                "description": "metric missing value_type",
                "kind": "aggregate",
                "measure": "order_count",
                # value_type intentionally omitted
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any("missing 'value_type:'" in msg and "'no_vt'" in msg for msg in messages), messages


def test_strict_accepts_metric_with_value_type(tmp_path: Path):
    """Baseline: a strict package with all metrics declaring value_type validates."""
    package_dir = tmp_path / "strict_ok"
    _write_strict_minimal_package(package_dir)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True, [e.get("message") for e in report.get("errors", [])]


def test_strict_accepts_measure_rollup_semantics(tmp_path: Path):
    """Model variants rely on measure rollup semantics in strict packages."""
    package_dir = tmp_path / "strict_measure_rollup"
    _write_strict_minimal_package(
        package_dir,
        measure_overrides={"rollup": "additive"},
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True, [e.get("message") for e in report.get("errors", [])]


def test_strict_rejects_id_on_measure(tmp_path: Path):
    """Strict row: `id:` on semantic objects."""
    package_dir = tmp_path / "strict_id_measure"
    _write_strict_minimal_package(
        package_dir,
        measure_overrides={"id": "measure.custom.explicit"},
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any("authors 'id:'" in msg for msg in messages), messages


def test_strict_rejects_dimension_preferred_filter_ops(tmp_path: Path):
    """Strict row: `dimension.preferred_filter_ops`."""
    package_dir = tmp_path / "strict_pref_ops"
    _write_strict_minimal_package(
        package_dir,
        extra_dimensions={
            "status": {
                "label": "Status",
                "kind": "categorical",
                "preferred_filter_ops": ["="],
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any("preferred_filter_ops" in msg for msg in messages), messages


def test_strict_rejects_accumulation_outside_enum(tmp_path: Path):
    """Strict row: `accumulation:` value not in {flow, stock, event, population}."""
    package_dir = tmp_path / "strict_bad_accum"
    _write_strict_minimal_package(
        package_dir,
        measure_overrides={"accumulation": "bogus"},
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any("accumulation" in msg and "bogus" in msg for msg in messages), messages


def test_strict_rejects_topics_on_dimension(tmp_path: Path):
    """Strict row: `topics:` on any object."""
    package_dir = tmp_path / "strict_topics_dim"
    _write_strict_minimal_package(
        package_dir,
        extra_dimensions={
            "status": {
                "label": "Status",
                "kind": "categorical",
                "topics": ["sales"],
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any("topics" in msg for msg in messages), messages


def test_check_passes_column_reachability_on_jaffle(tmp_path: Path):
    """The reference jaffle_shop package must pass column reachability —
    every authored dimension and measure expr resolves against the
    duckdb seed table.
    """
    from semantic_rails.package_tools import (
        check_warehouse_column_reachability_report,
    )

    package_dir = _copy_external_jaffle_package(tmp_path)
    ref = resolve_package_reference(path=str(package_dir))

    report = check_warehouse_column_reachability_report(ref)

    assert report["ok"] is True, report["errors"]
    assert report["summary"]["tables_probed"] > 0
    assert report["summary"]["missing_columns"] == 0


def test_check_flags_dimension_referencing_missing_column(tmp_path: Path):
    """Authoring a dimension whose `column:` doesn't exist in the
    warehouse table fails column reachability with a clear error
    naming the missing column and the dimension that references it.

    Pre-fix: this passed both `parse-config` and `check` and only blew
    up at query time with a binder error.
    """
    from semantic_rails.package_tools import (
        check_warehouse_column_reachability_report,
    )

    package_dir = _copy_external_jaffle_package(tmp_path)
    orders_path = package_dir / "models" / "core" / "orders.yml"
    payload = yaml.safe_load(orders_path.read_text(encoding="utf-8"))
    # Inject a dimension whose column does not exist in the jaffle_order
    # warehouse table.
    payload["model"]["dimensions"]["bogus_dim"] = {
        "label": "Bogus dimension",
        "description": "Points at a non-existent column",
        "column": "this_column_does_not_exist",
        "kind": "categorical",
    }
    _write_yaml(orders_path, payload)

    ref = resolve_package_reference(path=str(package_dir))
    report = check_warehouse_column_reachability_report(ref)

    assert report["ok"] is False
    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert any("this_column_does_not_exist" in msg and "jaffle_order" in msg for msg in messages), (
        messages
    )
    # Affected dimension is named in the details.
    affected_ids = [err.get("details", {}).get("dimension_id", "") for err in report["errors"]]
    assert any("bogus_dim" in did for did in affected_ids), affected_ids


def test_check_pipeline_integrates_reachability_blocker(tmp_path: Path):
    """A reachability failure must flow through `check_package_report` as
    a blocker, not silently pass."""
    from semantic_rails.package_tools import check_package_report

    package_dir = _copy_external_jaffle_package(tmp_path)
    orders_path = package_dir / "models" / "core" / "orders.yml"
    payload = yaml.safe_load(orders_path.read_text(encoding="utf-8"))
    payload["model"]["dimensions"]["bogus_dim"] = {
        "label": "Bogus dimension",
        "description": "Points at a non-existent column",
        "column": "absolutely_not_a_real_column",
        "kind": "categorical",
    }
    _write_yaml(orders_path, payload)

    ref = resolve_package_reference(path=str(package_dir))
    report = check_package_report(ref)

    assert report["ok"] is False
    # The check pipeline surfaces column reachability as one of its checks.
    assert "column_reachability" in report["checks"]
    assert report["checks"]["column_reachability"]["ok"] is False
    blocker_checks = {row.get("check", "") for row in report["blockers"]}
    assert "column_reachability" in blocker_checks, report["blockers"]


def test_describe_table_columns_rejects_adversarial_identifier():
    """An authored ``table:`` containing SQL punctuation must be rejected
    before any probe SQL is built. Otherwise an adversarial package YAML
    could mutate the DESCRIBE / information_schema probe."""
    from semantic_rails.package_tools import _describe_table_columns

    class _CapturingAdapter:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, sql: str, *args, **kwargs):  # noqa: D401
            self.queries.append(sql)
            return []

    for bad in (
        "foo; drop table x",
        "foo' or '1'='1",
        "schema.foo--",
        "x` /* comment */",
        "",
        '"; select 1; --',
    ):
        adapter = _CapturingAdapter()
        cols, err = _describe_table_columns(adapter, warehouse="duckdb", table=bad)
        assert cols == set()
        assert err, f"expected rejection for {bad!r}, got empty error"
        assert adapter.queries == [], (
            f"adapter was queried for adversarial identifier {bad!r}: {adapter.queries}"
        )


def test_describe_table_columns_accepts_dotted_identifier():
    """A normal DB.SCHEMA.TABLE identifier must still probe cleanly after
    the strict validation."""
    from semantic_rails.package_tools import _describe_table_columns

    class _StubAdapter:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple]] = []

        def query(self, sql: str, params: tuple[str, ...] = ()):  # noqa: D401
            self.queries.append((sql, tuple(params)))
            return [{"column_name": "id"}, {"column_name": "amount"}]

    adapter = _StubAdapter()
    cols, err = _describe_table_columns(adapter, warehouse="snowflake", table="DB.SCHEMA.ORDERS")
    assert err == ""
    assert cols == {"id", "amount"}
    assert adapter.queries, "snowflake probe should have run"
    sql, params = adapter.queries[0]
    assert "?" in sql
    assert params == ("ORDERS", "SCHEMA")
