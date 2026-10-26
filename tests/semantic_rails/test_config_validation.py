from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from semantic_rails import cli as cli_module
from semantic_rails import config as config_module
from semantic_rails.cli.reports import project_validation_report
from semantic_rails.config import resolve_repo_path
from semantic_rails.config_validation import (
    _run_probe,
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
    validate_runtime_package,
)
from semantic_rails.db import Database, SnowflakeCliAdapter, load_csv_dir_to_duckdb
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.yaml_loader import safe_load as yaml12_safe_load
from tests.semantic_rails.result_helpers import typed_rows


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize("debug_setting", [None, "0", "1"])
def test_probe_diagnostics_do_not_change_shared_debug_authority(
    runtime_factory, monkeypatch, debug_setting
):
    setting_name = "SEMANTIC_RAILS_ALLOW_DEBUG_SQL"
    if debug_setting is None:
        monkeypatch.delenv(setting_name, raising=False)
    else:
        monkeypatch.setenv(setting_name, debug_setting)
    runtime = runtime_factory("jaffle_shop")
    query = {"version": 1, "policy_context": {"roles": ["finance"]}}

    def fail(payload):
        assert payload == query
        assert os.environ.get(setting_name) == debug_setting
        raise SemanticLayerError("QUERY_EXECUTION_ERROR", "Redacted failure")

    monkeypatch.setattr(runtime, "query_internal", fail)
    monkeypatch.setattr(runtime, "compile_internal", lambda payload: {"rendered_sql": "SELECT 1"})
    result = _run_probe(
        runtime, kind="measure", object_id="measure.example", query=query, repair=True
    )
    assert result["rendered_sql"] == "SELECT 1"
    assert result["error"]["message"] == "Redacted failure"
    assert os.environ.get(setting_name) == debug_setting
    assert query["policy_context"]["roles"] == ["finance"]


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


# The order count's metric, as its measure used to publish it.
ORDERS_METRIC = {
    "as": "metric.sales.orders",
    "name": "sales.orders",
    "label": "Orders",
    "description": "Order count",
    "kind": "aggregate",
    "measure": "measure.demo.order_count",
    "aggregation": "count_distinct",
    "value_type": "number",
    "temporal_role": "temporal_role.demo_order_time",
}


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
                        "as": "entity.demo_order",
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
            "relation": "order_fact",
            "entities": {"order": {}},
            "times": {
                "ordered_at": {
                    "as": "temporal_role.demo_order_time",
                    "name": "demo.Order.ordered_at",
                    "label": "Order time",
                    "column": "ordered_at",
                    "kind": "timestamp",
                    "class": "event_time",
                },
                **dict(extra_times or {}),
            },
            "measures": {
                "order_count": {
                    "as": "measure.demo.order_count",
                    "name": "sales.orders",
                    "label": "Orders",
                    "description": "Order count",
                    "kind": "entity_count",
                    "times": ["ordered_at"],
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
        {"metrics": {"orders": ORDERS_METRIC, **dict(extra_metrics or {})}},
    )
    _write_seed_sql(package_dir / "data" / "seed_example.sql")


@pytest.mark.parametrize("target", ["measure.demo.order_count", "metric.sales.orders"])
@pytest.mark.parametrize("constraint", ["required_group_by", "required_where"])
def test_runtime_validation_probes_constrained_objects(tmp_path, target, constraint):
    package_dir = tmp_path / "constrained_orders"
    field = "dimension.demo_order_kind"
    _write_minimal_package(
        package_dir,
        extra_dimensions={
            "kind": {"as": field, "column": "kind", "kind": "categorical"},
        },
    )
    (package_dir / "data" / "seed_example.sql").write_text(
        "CREATE TABLE order_fact (order_id INTEGER, ordered_at TIMESTAMP, kind VARCHAR);\n"
        "INSERT INTO order_fact VALUES (1, '2024-01-01', 'retail'), "
        "(2, '2024-01-02', 'retail'), (3, '2024-01-02', 'wholesale');",
        encoding="utf-8",
    )
    _write_yaml(
        package_dir / "policies.yml",
        {
            "semantic_policies": [
                {
                    "id": "policy.orders.cut",
                    "kind": "metric_constraint",
                    "object_ids": [target],
                    constraint: [field],
                }
            ]
        },
    )
    report = project_validation_report(
        resolve_package_reference(path=str(package_dir)), mode="runtime"
    )
    assert report["ok"] is True, report["errors"]
    affected = {target}
    if target.startswith("measure."):
        affected.add("metric.sales.orders")
    skipped = len(affected) if constraint == "required_where" else 0
    assert report["summary"]["runtime"]["skipped"] == skipped
    assert report["summary"]["runtime"]["passed"] == 2 - skipped
    assert report["summary"]["runtime"]["failed"] == 0
    for probe in report["runtime"]["probes"]:
        if probe["object_id"] not in affected:
            assert not probe.get("skipped")
        elif constraint == "required_group_by":
            assert probe["query"]["group_by"] == [field]
            assert not probe.get("skipped")
        else:
            assert probe["skipped"] is True
            assert "required_where" in probe["reason"]
            assert "where" not in probe["query"]
    if constraint == "required_group_by":
        runtime = Runtime.from_path(str(package_dir))
        try:
            actual = [
                # Probes use LIMIT 0; read the fixture rows for the reference comparison.
                typed_rows(runtime.query_internal({**probe["query"], "limit": 10}))
                for probe in report["runtime"]["probes"]
                if probe["object_id"] in affected
            ]
        finally:
            runtime.close()
        database = Database.connect(runtime.db_path, read_only=True)
        try:
            expected = database.query(
                f'SELECT kind AS "{field}", COUNT(DISTINCT order_id) AS probe_value '
                "FROM order_fact GROUP BY kind ORDER BY kind"
            )
        finally:
            database.close()
        assert all(sorted(rows, key=lambda row: row[field]) == expected for rows in actual), (
            actual,
            expected,
        )


@pytest.mark.parametrize("constraint", ["required_group_by", "required_where"])
def test_runtime_validation_does_not_repair_segment_constraints(tmp_path, constraint):
    package_dir = tmp_path / "constrained_segment"
    field = "dimension.demo_order_kind"
    key = "dimension.demo_order_order_id"
    segment_id = "segment.demo.orders"
    _write_minimal_package(
        package_dir,
        extra_dimensions={
            "kind": {"as": field, "column": "kind", "kind": "categorical"},
            "order_key": {"as": key, "column": "order_id", "kind": "number"},
        },
    )
    (package_dir / "data" / "seed_example.sql").write_text(
        "CREATE TABLE order_fact (order_id INTEGER, ordered_at TIMESTAMP, kind VARCHAR);\n"
        "INSERT INTO order_fact VALUES (1, '2024-01-01', 'retail');",
        encoding="utf-8",
    )
    _write_yaml(
        package_dir / "policies.yml",
        {
            "semantic_policies": [
                {
                    "id": "policy.orders.cut",
                    "kind": "metric_constraint",
                    "object_ids": ["metric.sales.orders"],
                    constraint: [field],
                }
            ]
        },
    )
    where = [{"field": key, "op": ">", "value": 0}]
    _write_yaml(
        package_dir / "segments.yml",
        {
            "segments": {
                "orders": {
                    "as": segment_id,
                    "entity": "entity.demo_order",
                    "basis_metric": "metric.sales.orders",
                    "membership": {"where": where},
                }
            }
        },
    )
    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    assert report["parse"]["ok"] is True, report["errors"]
    assert report["ok"] is False, report
    probe = next(row for row in report["probes"] if row["object_id"] == segment_id)
    assert probe["ok"] is False
    assert probe["error"]["code"] == "POLICY_DENIED"
    assert not probe.get("skipped")
    assert probe["query"]["group_by"] == [key]
    assert probe["query"]["where"] == where


def test_probe_combines_required_grouping_with_non_additive_grain(tmp_path):
    package_dir = tmp_path / "stored_orders"
    field = "dimension.demo_order_kind"
    _write_minimal_package(
        package_dir,
        extra_dimensions={"kind": {"as": field, "column": "kind", "kind": "categorical"}},
        extra_measures={
            "stored_value": {
                "as": "measure.demo.stored_value",
                "kind": "aggregate",
                "expr": "order_id",
                "additive": False,
                "times": ["ordered_at"],
                "publish": False,
            }
        },
    )
    (package_dir / "data" / "seed_example.sql").write_text(
        "CREATE TABLE order_fact (order_id INTEGER, ordered_at TIMESTAMP, kind VARCHAR);\n"
        "INSERT INTO order_fact VALUES (1, '2024-01-01', 'retail');",
        encoding="utf-8",
    )
    _write_yaml(
        package_dir / "policies.yml",
        {
            "semantic_policies": [
                {
                    "id": "policy.orders.cut",
                    "kind": "metric_constraint",
                    "object_ids": ["measure.demo.stored_value"],
                    "required_group_by": [field],
                }
            ]
        },
    )
    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    assert report["ok"] is True, report["errors"]
    probe = next(row for row in report["probes"] if row["object_id"] == "measure.demo.stored_value")
    assert field in probe["query"]["group_by"]
    assert len(probe["query"]["group_by"]) > 1


@pytest.mark.parametrize(
    ("denial", "constraint"),
    [
        ("access", "required_group_by"),
        ("grouping", "required_group_by"),
        ("grouping", "required_where"),
        ("scope", "required_where"),
    ],
)
def test_constrained_probe_preserves_denials_and_policy_scope(tmp_path, constraint, denial):
    package_dir = tmp_path / "policy_scope"
    field = "dimension.demo_order_order_id"
    _write_minimal_package(
        package_dir,
        extra_dimensions={
            "order_key": {"as": field, "column": "order_id", "kind": "categorical"},
        },
    )
    policy = {
        "id": "policy.orders.cut",
        "kind": "metric_constraint",
        "object_ids": ["measure.demo.order_count"],
        constraint: [field],
    }
    policies = [policy]
    if denial == "access":
        policies.append(
            {
                "id": "policy.orders.deny",
                "kind": "object_access",
                "action": "deny",
                "object_ids": ["measure.demo.order_count"],
            }
        )
    elif denial == "grouping":
        # This second violation appears only after a grouping retry.
        policy["allowed_group_by"] = []
    else:
        policy["roles"] = ["analyst"]
    _write_yaml(package_dir / "policies.yml", {"semantic_policies": policies})
    # A probe runs the compiler's own form of the query.
    query = {"select": [{"expression": {"measure": "measure.demo.order_count"}, "as": "orders"}]}
    if denial == "grouping" and constraint == "required_where":
        query["group_by"] = [field]
    runtime = Runtime.from_path(str(package_dir))
    try:
        probe = _run_probe(
            runtime, kind="measure", object_id="measure.demo.order_count", query=query, repair=True
        )
    finally:
        runtime.close()
    assert probe["ok"] is (denial == "scope"), probe
    assert not probe.get("skipped")
    if denial != "scope":
        assert probe["error"]["code"] == "POLICY_DENIED"


@pytest.mark.parametrize(
    "targets, kind, duplicate",
    [
        (["measure.demo.constrained_orders"], "metric_constraint", False),
        (["measure.demo.order_count"], "metric_constraint", False),
        (
            ["measure.demo.order_count", "measure.demo.constrained_orders"],
            "metric_constraint",
            True,
        ),
        (["measure.demo.constrained_orders"], "object_visibility", True),
    ],
)
def test_duplicate_measure_warning_respects_constraint_identity(tmp_path, targets, kind, duplicate):
    package_dir = tmp_path / "measure_twins"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "constrained_orders": {
                "as": "measure.demo.constrained_orders",
                "kind": "entity_count",
                "times": ["ordered_at"],
            }
        },
    )
    policy = {"id": "policy.orders.cut", "kind": kind, "object_ids": targets}
    if kind == "metric_constraint":
        policy["required_group_by"] = ["dimension.demo_order_order_id"]
    else:
        policy["action"] = "hidden"
    _write_yaml(package_dir / "policies.yml", {"semantic_policies": [policy]})
    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))
    assert report["ok"] is True, report["errors"]
    assert any("duplicates" in warning["message"] for warning in report["warnings"]) is duplicate


def test_measure_aggregation_is_refused(tmp_path: Path):
    """The loader never read a measure's aggregation:, so it is an unknown key."""
    package_dir = tmp_path / "aggregation_warning_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "customer_distinct_bad": {
                "as": "measure.demo.customer_distinct_bad",
                "name": "sales.bad_customer_distinct",
                "label": "Bad customer distinct",
                "description": "Badly authored customer distinct count",
                "kind": "aggregate",
                "expr": "order_id",
                "aggregation": "count_distinct",
                "times": ["ordered_at"],
            }
        },
    )

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any(
        "measure 'customer_distinct_bad' has unknown key 'aggregation'" in error["message"]
        and "`default_agg:`" in error["message"]
        for error in report["errors"]
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
                "as": "entity.demo_order",
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
            "relation": "order_fact",
            "entities": {"order": {}},
            "times": {
                "ordered_at": {
                    "as": "temporal_role.demo_order_time",
                    "name": "demo.Order.ordered_at",
                    "label": "Order time",
                    "column": "ordered_at",
                    "kind": "timestamp",
                    "class": "event_time",
                }
            },
            "measures": {
                "order_count": {
                    "as": "measure.demo.order_count",
                    "name": "sales.orders",
                    "label": "Orders",
                    "description": "Order count",
                    "kind": "entity_count",
                    "times": ["ordered_at"],
                }
            },
        }
    }
    payload["metrics"] = {"orders": ORDERS_METRIC}
    _write_yaml(path, payload)


def _write_minimal_snowflake_package(package_dir: Path) -> None:
    _write_yaml(package_dir / "package.yml", _minimal_snowflake_package_payload(package_dir.name))
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "as": "entity.tpch_order",
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
                "relation": "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS",
                "entities": {"order": {}},
                "dimensions": {
                    "order_priority": {
                        "as": "dimension.tpch_order_priority",
                        "name": "tpch.Order.priority",
                        "label": "Order priority",
                        "column": "O_ORDERPRIORITY",
                        "kind": "categorical",
                    }
                },
                "times": {
                    "order_date": {
                        "as": "temporal_role.tpch_order_date",
                        "name": "tpch.Order.order_date",
                        "label": "Order date",
                        "column": "O_ORDERDATE",
                        "kind": "date",
                        "class": "event_time",
                    }
                },
                "measures": {
                    "order_count": {
                        "as": "measure.tpch.order_count",
                        "name": "sales.orders",
                        "label": "Orders",
                        "description": "Count of TPCH orders.",
                        "kind": "entity_count",
                        "entity_key": ["O_ORDERKEY"],
                        "times": ["order_date"],
                    },
                    "revenue": {
                        "as": "measure.tpch.revenue",
                        "name": "sales.revenue",
                        "label": "Revenue",
                        "description": "TPCH order revenue.",
                        "kind": "aggregate",
                        "expr": {"kind": "column", "column": "O_TOTALPRICE"},
                        "times": ["order_date"],
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                # The two metrics its measures used to publish.
                "orders": {
                    "as": "metric.sales.orders",
                    "name": "sales.orders",
                    "label": "Orders",
                    "description": "Count of TPCH orders.",
                    "kind": "aggregate",
                    "measure": "measure.tpch.order_count",
                    "aggregation": "count_distinct",
                    "value_type": "number",
                    "temporal_role": "temporal_role.tpch_order_date",
                },
                "revenue": {
                    "as": "metric.sales.revenue",
                    "name": "sales.revenue",
                    "label": "Revenue",
                    "description": "TPCH order revenue.",
                    "kind": "aggregate",
                    "measure": "measure.tpch.revenue",
                    "aggregation": "sum",
                    "value_type": "number",
                    "temporal_role": "temporal_role.tpch_order_date",
                },
                "sales.average_order_value": {
                    "as": "metric.sales.average_order_value",
                    "name": "sales.average_order_value",
                    "label": "Average order value",
                    "description": "Revenue divided by orders.",
                    "kind": "derived",
                    "value_type": "number",
                    "temporal_role": "temporal_role.tpch_order_date",
                    "expression": {
                        "kind": "arithmetic",
                        "op": "divide",
                        "left": {"kind": "metric", "metric": "metric.sales.revenue"},
                        "right": {"kind": "metric", "metric": "metric.sales.orders"},
                    },
                },
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
                "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
                "limit": 1,
            }
        )
        assert result["row_count"] == 1
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
        and w["code"] not in {"ROUTES_UNDECIDED", "ROUTE_PASS_THROUGH"}
    ]
    # Lifetime spend is a stock on the one-row-per-customer table, a current-state
    # shape the snapshot-key warning can't tell from a mis-keyed snapshot table.
    assert [(w["code"], w["details"]["measure_id"]) for w in non_collision_warnings] == [
        ("STOCK_SNAPSHOT_KEY_MISSING_CLOCK", "measure.jaffle.lifetime_spend_usd")
    ]
    # Its undecided join routes are reviewed in test_route_census.py.
    (routes,) = [w for w in report["warnings"] if w["code"] == "ROUTES_UNDECIDED"]
    assert routes["details"]["count"] == len(report["route_census"]["undecided"])
    crossings = [w for w in report["warnings"] if w["code"] == "ROUTE_PASS_THROUGH"]
    assert len(crossings) == len(report["route_census"]["pass_through"]) == 5
    assert [w["object_ids"] for w in crossings] == [
        [row["source_entity"], row["target_entity"]]
        for row in report["route_census"]["pass_through"]
    ]
    assert all(w["severity"] == "warning" for w in crossings)
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


_NATIVE_ENV = {"account_env": "SNOW_ACCOUNT", "user_env": "SNOW_USER"}
_STANDARD_OPTIONS = {
    "database": "SNOWFLAKE_SAMPLE_DATA",
    "schema": "TPCH_SF1",
    "warehouse": "COMPUTE_WH",
    "role": "ANALYST",
}


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (None, {"kind": "snowflake_cli", "name": "semantic_views_trial", "options": {}}),
        (
            {"kind": "snowflake_cli", "name": "semantic_views_trial", "options": _STANDARD_OPTIONS},
            {"options": _STANDARD_OPTIONS},
        ),
        (
            {
                "kind": "snowflake_native",
                "name": "prod_native",
                "options": {
                    **_NATIVE_ENV,
                    "password_env": "SNOW_PASSWORD",
                    **_STANDARD_OPTIONS,
                    "query_tag": "semantic-rails",
                    "statement_timeout_seconds": "60",
                },
            },
            {"kind": "snowflake_native", "name": "prod_native"},
        ),
        (
            {
                "kind": "snowflake_native",
                "options": {
                    **_NATIVE_ENV,
                    "password_env": "SNOW_PASSWORD",
                    "database": "ANALYTICS",
                },
            },
            {"kind": "snowflake_native", "name": ""},
        ),
        (
            {"kind": "snowflake_native", "options": _NATIVE_ENV},
            "snowflake_native direct connection",
        ),
        (
            {
                "kind": "snowflake_native",
                "name": "prod_native",
                "options": {"cert_revocation_check_mode": "Enabled"},
            },
            {"options": {"cert_revocation_check_mode": "Enabled"}},
        ),
        (
            {
                "kind": "snowflake_native",
                "name": "prod_native",
                "options": {"cert_revocation_check_mode": "strict"},
            },
            "'cert_revocation_check_mode' must be one of: advisory, enabled, disabled",
        ),
        ({"kind": "snowflake_cli"}, "snowflake_cli packages must declare package.connection.name"),
        (
            {
                "kind": "snowflake_cli",
                "name": "semantic_views_trial",
                "options": {"authenticator": "externalbrowser"},
            },
            "unsupported package.connection option 'authenticator'",
        ),
    ],
    ids=[
        "cli",
        "cli-options",
        "native-secret-env",
        "native-direct",
        "native-without-auth",
        "native-revocation-mode",
        "native-unknown-revocation-mode",
        "cli-without-name",
        "cli-unsupported-option",
    ],
)
def test_parse_config_reads_snowflake_connections(tmp_path: Path, connection, expected):
    package_dir = tmp_path / "snowflake_demo"
    _write_minimal_snowflake_package(package_dir)
    if connection is not None:
        payload = yaml.safe_load((package_dir / "package.yml").read_text(encoding="utf-8"))
        payload["package"]["connection"] = connection
        _write_yaml(package_dir / "package.yml", payload)

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    if isinstance(expected, str):
        assert report["ok"] is False
        assert any(expected in error["message"] for error in report["errors"]), report["errors"]
        return
    assert report["ok"] is True, report["errors"]
    assert report["package"]["id"] == "snowflake_demo"
    assert (config.package.warehouse, config.package.default_db) == ("snowflake", "")
    actual = config.package.connection
    assert {key: getattr(actual, key) for key in expected} == expected


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
    supported_grains: [day]
""".lstrip(),
        encoding="utf-8",
    )

    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True
    options = config.package.connection.options
    assert (options["schema"], options["warehouse"], options["role"]) == ("yes", "off", "on")
    assert yaml12_safe_load("truthy: true\nfalsey: false\ndenied: no") == {
        "truthy": True,
        "falsey": False,
        "denied": "no",
    }


def _broken_yaml(package_dir: Path) -> Path:
    package_dir.mkdir(parents=True)
    (package_dir / "broken.yml").write_text("package: [", encoding="utf-8")
    return package_dir / "broken.yml"


def _without_graph(package_dir: Path) -> Path:
    _write_yaml(package_dir / "package.yml", _minimal_package_payload(package_dir.name))
    return package_dir


def _with_unknown_graph_model(package_dir: Path) -> Path:
    _write_minimal_package(package_dir)
    graph = yaml.safe_load((package_dir / "graph.yml").read_text(encoding="utf-8"))
    graph["graph"]["entities"]["order"]["model"] = "missing_model"
    _write_yaml(package_dir / "graph.yml", graph)
    return package_dir


def _minimal(**options):
    def build(package_dir: Path) -> Path:
        _write_minimal_package(package_dir, **options)
        return package_dir

    return build


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (_broken_yaml, ["invalid YAML"]),
        (_without_graph, ["graph.yml is missing"]),
        (_with_unknown_graph_model, ["does not resolve to a declared model"]),
        (
            _minimal(model_operational_defaults={"owner": "finance"}),
            ["failed to load package config"],
        ),
        # A typo in a kind is refused, naming the kinds to choose from.
        (
            _minimal(
                extra_dimensions={
                    "channel": {"label": "Channel", "column": "channel", "kind": "catagorical"}
                }
            ),
            ["dimension orders.channel has unknown kind 'catagorical'", "categorical", "boolean"],
        ),
        (
            _minimal(
                extra_times={
                    "shipped_at": {
                        "label": "Shipped at",
                        "column": "shipped_at",
                        "kind": "datetimestamp",
                        "class": "event_time",
                    }
                }
            ),
            ["times orders.shipped_at has unknown kind 'datetimestamp'", "date", "timestamp"],
        ),
    ],
    ids=[
        "invalid-yaml",
        "missing-graph",
        "unknown-graph-model",
        "loader-failure",
        "dimension-kind",
        "times-kind",
    ],
)
def test_parse_config_reports_an_unreadable_package(tmp_path: Path, build, expected):
    source = build(tmp_path / "broken_package")

    report, config = parse_config_report(resolve_package_reference(path=str(source)))

    assert (report["ok"], config) == (False, None)
    messages = [error["message"] for error in report["errors"]]
    assert any(all(part in message for part in expected) for message in messages), messages


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
    from semantic_rails.catalog_service import write_manifest

    runtime = Runtime.from_path(str(package_dir))
    try:
        manifest_path = write_manifest(runtime)
    finally:
        runtime.close()

    assert manifest_path.exists()
    assert manifest_path.parent.name == ".compiled"
    for name in ("manifest.json", "sources.sha256"):
        assert stat.S_IMODE((manifest_path.parent / name).stat().st_mode) == 0o644
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
        from semantic_rails.catalog_service import write_manifest

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

    runtime = Runtime.from_config(
        config_module.load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop")),
        source_path=resolve_repo_path("configs/semantic_rails/jaffle_shop"),
    )
    monkeypatch.setattr(
        runtime,
        "manifest_catalog",
        lambda *, view, verbosity: {"source": "manifest", "view": view, "verbosity": verbosity},
    )
    monkeypatch.setattr(
        catalog_service,
        "catalog_payload",
        lambda _runtime, **kwargs: {"source": "live", **kwargs},
    )

    assert catalog_service.resolve_catalog(runtime)["source"] == "manifest"
    for context in (
        {"roles": ["finance"]},
        {"tenant": "tenant-a"},
        {"actor": "user-a"},
        {"project": "project-a"},
        {"environment": "production"},
        {"audience": "internal"},
    ):
        resolved = catalog_service.resolve_catalog(runtime, policy_context=context)
        assert resolved["source"] == "live"
        assert resolved["policy_context"] == context

    assert catalog_service.resolve_catalog(runtime, search="orders")["source"] == "live"


def _revenue(key: str, name: str, label: str, **extra) -> dict:
    return {
        key: {
            "as": f"measure.demo.{key}",
            "name": name,
            "label": label,
            "description": f"{label} ({key})",
            "kind": "aggregate",
            "expr": key,
            "times": ["ordered_at"],
            **extra,
        }
    }


@pytest.mark.parametrize(
    ("measures", "code", "details", "matching_fields"),
    [
        # Two measures labeled "Revenue": discover can't tell them apart.
        (
            {
                **_revenue("revenue_a", "sales.revenue_a", "Revenue"),
                **_revenue("revenue_b", "sales.revenue_b", "Revenue"),
            },
            "SEMANTIC_TERM_COLLISION",
            {
                "collision_type": "exact_normalized",
                "normalized_term": "revenue",
                "object_ids": ["measure.demo.revenue_a", "measure.demo.revenue_b"],
            },
            {"measure.demo.revenue_a": ["label"], "measure.demo.revenue_b": ["label"]},
        ),
        (
            {
                **_revenue(
                    "booked_revenue",
                    "finance.booked_revenue",
                    "Booked revenue",
                    synonyms=["Sales Revenue"],
                ),
                **_revenue("recognized_revenue", "sales.revenue", "Recognized revenue"),
            },
            "SEMANTIC_TERM_COLLISION",
            {
                "normalized_term": "salesrevenue",
                "object_ids": ["measure.demo.booked_revenue", "measure.demo.recognized_revenue"],
            },
            {
                "measure.demo.booked_revenue": ["alias"],
                "measure.demo.recognized_revenue": ["canonical_name"],
            },
        ),
        (
            {
                **_revenue("recurring_revenue", "sales.recurring_revenue", "Recurring revenue"),
                **_revenue("recurring_typo", "sales.recurring_typo", "Recurring reveneu"),
            },
            "SEMANTIC_TERM_NEAR_COLLISION",
            {
                "collision_type": "single_edit_typo",
                "differing_tokens": ["revenue", "reveneu"],
                "edit_distance": 1,
                "object_ids": ["measure.demo.recurring_revenue", "measure.demo.recurring_typo"],
            },
            None,
        ),
        # Qualified siblings and distinct labels are ordinary authoring.
        (
            {
                **_revenue("gross_revenue", "finance.revenue", "Revenue (gross)"),
                **_revenue("net_revenue", "sales.revenue", "Revenue (net)"),
            },
            None,
            None,
            None,
        ),
        (_revenue("gross_revenue", "sales.gross_revenue", "Gross revenue"), None, None, None),
    ],
    ids=["same-label", "alias-vs-name", "single-edit", "qualified-siblings", "distinct-labels"],
)
def test_parse_config_warns_on_semantic_term_collisions(
    tmp_path: Path, measures, code, details, matching_fields
):
    package_dir = tmp_path / "collision_demo"
    _write_minimal_package(package_dir, extra_measures=measures)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    ids = {f"measure.demo.{key}" for key in measures}
    collisions = [
        row
        for row in report["warnings"]
        if str(row.get("code", "")).startswith("SEMANTIC_TERM_")
        and set(row["details"]["object_ids"]) & ids
    ]
    if code is None:
        assert collisions == []
        return
    (warning,) = [row for row in collisions if row["code"] == code]
    assert warning["severity"] == "warning"
    assert {key: warning["details"][key] for key in details} == details
    assert warning["details"]["recovery_hints"]
    if matching_fields:
        objects = {row["id"]: row["matching_fields"] for row in warning["details"]["objects"]}
        assert {key: objects[key] for key in matching_fields} == matching_fields


def test_validate_config_generates_query_time_for_time_required_metrics(tmp_path: Path):
    package_dir = tmp_path / "time_probe_demo"
    _write_minimal_package(
        package_dir,
        extra_metrics={
            "sales.orders_cumulative": {
                "as": "metric.sales.orders_cumulative",
                "name": "sales.orders_cumulative",
                "label": "Cumulative orders",
                "description": "Cumulative orders",
                "kind": "cumulative",
                "value_type": "number",
                "temporal_role": "temporal_role.demo_order_time",
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


@pytest.mark.parametrize(
    ("op", "value", "suggestions"),
    [
        ("=", "Completed", ["completed"]),
        ("in", ["Complete", "pending"], ["completed"]),
        ("!=", "zzz", [None]),
        ("not in", ["completed"], []),
    ],
)
def test_validation_warns_on_a_filter_value_the_data_lacks(
    tmp_path: Path, op: str, value: str | list[str], suggestions: list[str | None]
):
    package_dir = tmp_path / "status_filter"
    _write_minimal_package(
        package_dir,
        extra_dimensions={
            "status": {
                "as": "dimension.demo_order_status",
                "column": "status",
                "kind": "categorical",
            }
        },
        extra_metrics={
            "sales.completed_orders": {
                "as": "metric.sales.completed_orders",
                "label": "Completed orders",
                "kind": "aggregate",
                "value_type": "number",
                "expression": {
                    "kind": "aggregate",
                    "measure": "measure.demo.order_count",
                    "filter": {
                        "all": [{"field": "dimension.demo_order_status", "op": op, "value": value}]
                    },
                },
            }
        },
    )
    (package_dir / "data" / "seed_example.sql").write_text(
        "CREATE TABLE order_fact (order_id INTEGER, ordered_at TIMESTAMP, status VARCHAR);\n"
        "INSERT INTO order_fact VALUES (1, '2024-01-01', 'completed'), (2, '2024-01-02', 'pending');",
        encoding="utf-8",
    )
    ref = resolve_package_reference(path=str(package_dir))

    def suggested(warnings: list) -> list[str | None]:
        return [
            w["details"]["suggestion"]
            for w in warnings
            if isinstance(w, dict) and w.get("code") == "FILTER_VALUE_NOT_FOUND"
        ]

    assert suggested(project_validation_report(ref, mode="parse")["warnings"]) == []
    for mode in ("runtime", "full"):
        report = project_validation_report(ref, mode=mode)
        assert report["ok"] is True, report["errors"]
        assert suggested(report["warnings"]) == suggestions, mode


def test_validate_config_continues_after_failures_and_reports_execution_errors(tmp_path: Path):
    package_dir = tmp_path / "probe_failure_demo"
    _write_minimal_package(
        package_dir,
        extra_measures={
            "broken_orders": {
                "as": "measure.demo.broken_orders",
                "name": "sales.broken_orders",
                "label": "Broken orders",
                "description": "Broken orders",
                "kind": "entity_count",
                "expr": {"kind": "column", "column": "missing_order_id"},
                "times": ["ordered_at"],
                "publish": False,
            }
        },
        extra_metrics={
            "sales.orders_cumulative_broken_role": {
                "as": "metric.sales.orders_cumulative_broken_role",
                "name": "sales.orders_cumulative_broken_role",
                "label": "Broken cumulative orders",
                "description": "Broken cumulative orders",
                "kind": "cumulative",
                "value_type": "number",
                "temporal_role": "temporal_role.missing",
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

    def _fake_query(self, prepared, *, limits=None):
        queries.append(prepared.sql)
        return []

    monkeypatch.setattr(SnowflakeCliAdapter, "query_prepared", _fake_query)

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
                "as": "measure.demo.broken_orders",
                "name": "sales.broken_orders",
                "label": "Broken orders",
                "description": "Broken orders",
                "kind": "entity_count",
                "expr": {"kind": "column", "column": "missing_order_id"},
                "times": ["ordered_at"],
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
            "starter_pkg",
            "--workspace-root",
            str(tmp_path),
            "--json",
            "--yes",
            "--namespace",
            "starter",
        ],
    )

    cli_module.main()
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert payload["ok"] is True
    assert (target / "package.yml").exists()
    assert (target / "graph.yml").exists()
    report, config = parse_config_report(resolve_package_reference(path=str(target)))
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
                        "as": "entity.demo_parent",
                        "name": "demo.Parent",
                        "key": {"columns": ["parent_id"], "role": "unique"},
                        "model": "parents",
                    },
                    "child": {
                        "as": "entity.demo_child",
                        "name": "demo.Child",
                        "key": ["child_id"],
                        "model": "children",
                    },
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "parents.yml",
        {
            "model": {
                "relation": "parent_dim",
                "entities": {"parent": {}},
                "dimensions": {"parent_id": {"as": "dimension.demo_parent_id"}},
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "children.yml",
        {
            "model": {
                "relation": "child_fact",
                "entities": {"child": {}, "parent": {}},
                "dimensions": {
                    "child_id": {"as": "dimension.demo_child_id"},
                    "parent_id": {"as": "dimension.demo_child_parent_id"},
                },
                "measures": {
                    "child_count": {
                        "as": "measure.demo.child_count",
                        "kind": "entity_count",
                        "expr": {"kind": "column", "column": "child_id"},
                        "publish": False,
                    }
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
    a metric file that duplicated an auto-published metric. All four are
    refused now.

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


def _write_current_minimal_package(
    package_dir: Path,
    *,
    extra_metrics: dict | None = None,
    extra_dimensions: dict | None = None,
    measure_overrides: dict | None = None,
    package_overrides: dict | None = None,
) -> None:
    """Build a minimal package in the current authoring forms, then optionally
    inject violations, so a single violation under test is the only failure surfaced."""
    pkg = _minimal_package_payload(package_dir.name)
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

    # Always author one valid metric. Tests that want an invalid metric
    # pass extra_metrics that overrides or adds new ones.
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


@pytest.mark.parametrize(
    ("kind", "value_type", "ok"),
    [
        ("aggregate", "omitted", False),
        ("ratio", "omitted", False),
        # The loader turns a null or empty value_type into the "number" default.
        ("ratio", None, False),
        ("ratio", "", False),
        # An authored `number` is not mistaken for that default.
        ("ratio", "number", True),
        ("derived", "number", True),
    ],
)
def test_strict_requires_an_authored_value_type(tmp_path: Path, kind, value_type, ok):
    spec: dict = {"label": "Typed", "description": "a metric", "kind": kind}
    if value_type != "omitted":
        spec["value_type"] = value_type
    if kind == "aggregate":
        spec["measure"] = "order_count"
    elif kind == "ratio":
        spec.update(numerator="order_count", denominator="order_count")
    else:
        spec["expression"] = {
            "kind": "arithmetic",
            "op": "multiply",
            "left": {"kind": "metric", "metric": "order_count_metric"},
            "right": {"kind": "literal", "value": 2},
        }
    package_dir = tmp_path / "strict_value_type"
    _write_current_minimal_package(package_dir, extra_metrics={"typed": spec})

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert report["ok"] is ok, messages
    if not ok:
        assert any("metric 'typed': missing 'value_type:'" in msg for msg in messages), messages


def test_strict_rejects_a_missing_value_type_in_a_metrics_folder_file(package_config_factory):
    _, package_dir = package_config_factory("jaffle_shop")
    package_dir = Path(package_dir)
    source = package_dir / "metrics" / "core" / "core_metrics.yml"
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    spec = doc["metrics"].pop("sales.aov_usd")
    del spec["value_type"]
    _write_yaml(source, doc)
    _write_yaml(package_dir / "metrics" / "relocated.yml", {"metrics": {"sales.aov_usd": spec}})

    errors = validate_runtime_package(package_dir)

    assert len(errors) == 1, errors
    assert "metric 'sales.aov_usd': missing 'value_type:'" in errors[0]


def _write_metric_shape_case(tmp_path: Path, layout: str, shape: str, value_type) -> Path:
    if layout == "single_file":
        source = tmp_path / "monolithic.yml"
        _write_monolithic_package(source, "monolithic_demo")
    else:
        source = tmp_path / "shape"
        _write_current_minimal_package(source)
    package_yml = source if source.is_file() else source / "package.yml"

    spec = {
        "label": "Orders ratio",
        "description": "Orders over orders",
        "kind": "ratio",
        "numerator": "order_count",
        "denominator": "order_count",
    }
    if value_type != "omitted":
        spec["value_type"] = value_type
    if shape == "spec_pairs":
        spec = [[key, value] for key, value in spec.items()]
    metrics = {"orders_ratio": spec}
    if shape == "block_pairs":
        metrics = [["orders_ratio", spec]]

    if layout in {"single_file", "package_yml"}:
        target = package_yml
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        raw["metrics"] = metrics
        if shape == "package_pairs":
            raw["package"] = [[key, value] for key, value in raw["package"].items()]
    elif layout == "root_file":
        target = source / "metrics.yml"
        raw = {"metrics": metrics}
    else:
        target = source / "metrics" / "metrics.yml"
        raw = {"metrics": metrics}
    _write_yaml(target, raw)
    return source


@pytest.mark.parametrize("value_type", ["omitted", "number"])
@pytest.mark.parametrize(
    ("layout", "shape"),
    [
        *(
            (layout, "mapping")
            for layout in ("single_file", "package_yml", "root_file", "metric_file")
        ),
        ("single_file", "block_pairs"),
        ("metric_file", "spec_pairs"),
        ("single_file", "package_pairs"),
    ],
)
def test_metric_value_type_rule_holds_in_every_layout_and_shape(
    tmp_path, layout, shape, value_type
):
    source = _write_metric_shape_case(tmp_path, layout, shape, value_type)

    errors = validate_runtime_package(source)
    report, config = parse_config_report(resolve_package_reference(path=str(source)))

    if value_type != "number":
        missing = "metric 'orders_ratio': missing 'value_type:'"
        assert any(missing in error for error in errors)
        assert (report["ok"], config) == (False, None)
        assert any(missing in error["message"] for error in report["errors"])
    else:
        assert errors == []
        assert report["ok"] is True
        assert config is not None


@pytest.mark.parametrize("layout", ["single_file", "package_yml", "root_file", "metric_file"])
@pytest.mark.parametrize("shape", ["invalid_block", "invalid_spec"])
def test_invalid_metric_shapes_return_errors(tmp_path, layout, shape):
    source = _write_metric_shape_case(tmp_path, layout, "mapping", "number")
    if layout == "single_file":
        target = source
    elif layout == "package_yml":
        target = source / "package.yml"
    elif layout == "root_file":
        target = source / "metrics.yml"
    else:
        target = source / "metrics" / "metrics.yml"
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    raw["metrics"] = 7 if shape == "invalid_block" else {"orders_ratio": 7}
    _write_yaml(target, raw)

    errors = validate_runtime_package(source)
    report, config = parse_config_report(resolve_package_reference(path=str(source)))

    assert errors
    assert (report["ok"], config) == (False, None)


def test_strict_accepts_measure_rollup_semantics(tmp_path: Path):
    """Model variants rely on measure rollup semantics in strict packages."""
    package_dir = tmp_path / "strict_measure_rollup"
    _write_current_minimal_package(package_dir, measure_overrides={"rollup": "additive"})

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True, [e.get("message") for e in report.get("errors", [])]


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


@pytest.mark.parametrize("expression", ["1", "1.0"])
def test_constant_measure_checks_and_counts_rows(tmp_path: Path, expression: str):
    package_dir = _copy_external_jaffle_package(tmp_path)
    orders_path = package_dir / "models" / "core" / "orders.yml"
    payload = yaml.safe_load(orders_path.read_text(encoding="utf-8"))
    payload["model"]["measures"]["row_count"] = {
        "label": "Source rows",
        "kind": "aggregate",
        "expr": expression,
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
        "value_type": "count",
    }
    _write_yaml(orders_path, payload)
    # Bound the real CLI check in a subprocess: a recursive walker regression
    # must fail the test rather than strand a test worker indefinitely.
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails.cli", "check", "--path", str(package_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    runtime = Runtime.from_path(str(package_dir))
    try:
        query = {
            "select": [{"expression": "measure.jaffle.row_count", "as": "rows"}],
        }
        sql = runtime.compile(query)["rendered_sql"]
        assert "SUM(" in sql.upper(), sql
        assert f"SUM({expression})" in sql.upper(), sql
        result = runtime.query(query)
        expected = runtime.query(
            {
                "select": [{"expression": "measure.jaffle.order_count", "as": "rows"}],
            }
        )
        assert typed_rows(result) == typed_rows(expected)
        assert result["row_count"] == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "expression, expected",
    [
        ({"kind": "literal", "value": 1}, []),
        ({"kind": "literal", "value": True}, []),
        ({"kind": "literal", "value": "text"}, []),
        ({"kind": "literal", "value": None}, []),
        (
            {
                "kind": "arithmetic",
                "op": "+",
                "left": {"kind": "column", "column": "amount"},
                "right": {"kind": "literal", "value": 1},
            },
            ["amount"],
        ),
        (
            {
                "kind": "call",
                "name": "COALESCE",
                "args": [{"kind": "column", "column": "amount"}, {"kind": "literal", "value": 0}],
            },
            ["amount"],
        ),
        (
            {
                "kind": "case",
                "whens": [
                    {
                        "when": {"kind": "column", "column": "flag"},
                        "then": {"kind": "column", "column": "amount"},
                    }
                ],
                "else": {"kind": "column", "column": "fallback"},
            },
            ["flag", "amount", "fallback"],
        ),
        (
            {
                "kind": "date_add",
                "unit": "day",
                "value": {"kind": "literal", "value": 1},
                "date": {"kind": "column", "column": "created_at"},
            },
            ["created_at"],
        ),
    ],
)
def test_column_refs_visit_only_expression_nodes(expression, expected):
    from semantic_rails.expressions import parse_semantic_expression
    from semantic_rails.package_tools import _collect_column_refs

    out: list[str] = []
    _collect_column_refs(parse_semantic_expression(expression, context="config"), out=out)
    assert out == expected


@pytest.mark.parametrize("field", ["when", "then", "else"])
def test_check_flags_missing_column_inside_case(tmp_path: Path, field: str):
    from semantic_rails.package_tools import check_warehouse_column_reachability_report

    package_dir = _copy_external_jaffle_package(tmp_path)
    orders_path = package_dir / "models" / "core" / "orders.yml"
    payload = yaml.safe_load(orders_path.read_text(encoding="utf-8"))
    expression = {
        "kind": "case",
        "whens": [
            {"when": {"kind": "literal", "value": True}, "then": {"kind": "literal", "value": 1}}
        ],
        "else": {"kind": "literal", "value": 0},
    }
    missing = {"kind": "column", "column": "missing_source_column"}
    if field == "else":
        expression["else"] = missing
    else:
        expression["whens"][0][field] = missing
    payload["model"]["measures"]["row_count"] = {
        "label": "Source rows",
        "kind": "aggregate",
        "expr": expression,
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
        "value_type": "count",
    }
    _write_yaml(orders_path, payload)
    report = check_warehouse_column_reachability_report(
        resolve_package_reference(path=str(package_dir))
    )
    assert report["ok"] is False
    assert any(
        error["code"] == "WAREHOUSE_COLUMN_NOT_FOUND"
        and error["details"].get("measure_id") == "measure.jaffle.row_count"
        and error["details"].get("column") == "missing_source_column"
        for error in report["errors"]
    )


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


# Segment references. The loader keeps an unresolved segment reference as
# written, so the package still loads; catalog, inspect and the segment-*
# surfaces then fail on every request. Package validation must reject it.

_HIGH_VALUE_SEGMENT = "segment.jaffle.high_value_customers"


def _jaffle_with_segment(package_config_factory, mutate) -> Path:
    _, package_dir = package_config_factory("jaffle_shop")
    segment_file = Path(package_dir) / "segments" / "core.yml"
    raw = yaml.safe_load(segment_file.read_text(encoding="utf-8"))
    mutate(raw["segments"]["customer.high_value"])
    _write_yaml(segment_file, raw)
    return Path(package_dir)


def _membership_entity(entity: str):
    def mutate(segment: dict) -> None:
        segment["membership"]["metric_filters"][0]["expression"]["entity"] = entity

    return mutate


def _nested_membership_entity(segment: dict) -> None:
    predicate = segment["membership"]["metric_filters"][0]["expression"]
    nested = {**predicate, "entity": "entity.jaffle.customer"}
    segment["membership"]["metric_filters"][0]["expression"] = {
        "kind": "boolean",
        "op": "and",
        "args": [predicate, nested],
    }


def _membership_expression(change: dict):
    def mutate(segment: dict) -> None:
        segment["membership"]["metric_filters"][0]["expression"].update(change)

    return mutate


_SUGGEST_CUSTOMER = "did you mean 'entity.jaffle_customer'?"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        *(
            pytest.param(lambda s, ref=ref: s.update(entity=ref), [], id=f"entity-{ref}")
            for ref in ("customer", "jaffle.Customer", "entity.jaffle_customer")
        ),
        pytest.param(
            lambda s: s.update(entity="entity.jaffle.customer"),
            ["targets unknown entity 'entity.jaffle.customer'", _SUGGEST_CUSTOMER],
            id="unknown-entity",
        ),
        # The id is suggested for a membership entity written as a key, a name or a typo.
        *(
            pytest.param(
                _membership_entity(ref),
                [f"membership references unknown entity '{ref}'", _SUGGEST_CUSTOMER],
                id=f"membership-entity-{ref}",
            )
            for ref in ("entity.jaffle.customer", "customer", "jaffle.Customer")
        ),
        pytest.param(
            _nested_membership_entity,
            ["membership references unknown entity 'entity.jaffle.customer'", _SUGGEST_CUSTOMER],
            id="nested-boolean-filter",
        ),
        pytest.param(
            lambda s: s.update(preview_dimensions=["dimension.jaffle_store_name"]),
            ["is invalid (INVALID_SEGMENT): ", "dimension_id=dimension.jaffle_store_name"],
            id="runtime-cannot-normalize",
        ),
        pytest.param(
            lambda s: s["membership"].update(
                where=[{"field": "dimension.jaffle_missing", "op": "=", "value": 1}]
            ),
            [
                "query does not compile (OBJECT_NOT_FOUND): Unknown dimension 'dimension.jaffle_missing'"
            ],
            id="query-does-not-compile",
        ),
        # The expression parser raises plain ValueError/TypeError for these.
        pytest.param(
            _membership_expression({"window": {"unit": "day", "value": "oops"}}),
            ["query does not compile (ValueError): "],
            id="window-value",
        ),
        pytest.param(
            _membership_expression({"window": 7}),
            ["query does not compile (TypeError): "],
            id="window-scalar",
        ),
        pytest.param(
            _membership_expression(
                {
                    "input": {
                        "kind": "aggregate",
                        "measure": "measure.jaffle.lifetime_spend_usd",
                        "parameters": "abc",
                    }
                }
            ),
            ["query does not compile (ValueError): "],
            id="parameters",
        ),
    ],
)
def test_validation_checks_segment_references(package_config_factory, mutate, expected):
    package_dir = _jaffle_with_segment(package_config_factory, mutate)

    errors = validate_runtime_package(package_dir)

    if not expected:
        assert errors == []
        return
    assert len(errors) == 1, errors
    assert errors[0].startswith(f"{package_dir}: segment {_HIGH_VALUE_SEGMENT} ")
    assert all(part in errors[0] for part in expected), errors
    report = validate_config_report(resolve_package_reference(path=str(package_dir)))
    assert report["ok"] is False
    assert [error["message"] for error in report["errors"]] == errors


def test_membership_entity_suggestion_skips_labels_shared_by_entities(package_config_factory):
    package_dir = _jaffle_with_segment(package_config_factory, _membership_entity("customer"))
    graph_file = package_dir / "graph.yml"
    graph = yaml.safe_load(graph_file.read_text(encoding="utf-8"))
    graph["graph"]["entities"]["store"]["label"] = "Customer"
    _write_yaml(graph_file, graph)

    (error,) = validate_runtime_package(package_dir)
    assert "membership references unknown entity 'customer'" in error
    assert error.endswith(_SUGGEST_CUSTOMER)


def test_validation_treats_predicate_shaped_literals_as_data(package_config_factory):
    # Agrees with segment-validate: a literal is data, not a predicate to resolve.
    literal = {"kind": "literal", "value": {"kind": "metric_predicate", "entity": "not_an_entity"}}

    def mutate(segment: dict) -> None:
        segment["membership"]["metric_filters"] = [
            {"expression": literal, "op": "=", "value": "foo"}
        ]

    package_dir = _jaffle_with_segment(package_config_factory, mutate)

    assert validate_runtime_package(package_dir) == []
    config = config_module.load_package_config(str(package_dir))
    runtime = Runtime.from_config(config, source_path=str(package_dir), package_id="jaffle_shop")
    try:
        assert runtime.segment_validate(_HIGH_VALUE_SEGMENT)["ok"] is True
    finally:
        runtime.close()


def test_single_file_validation_checks_segment_references(tmp_path: Path):
    package_file = tmp_path / "monolithic.yml"
    _write_monolithic_package(package_file, "monolithic_demo")
    payload = yaml.safe_load(package_file.read_text(encoding="utf-8"))
    payload["segments"] = {
        "big_orders": {
            "as": "segment.demo.big_orders",
            "entity": "entity.demo.order",
            "basis_metric": "metric.sales.orders",
        }
    }
    _write_yaml(package_file, payload)

    assert validate_runtime_package(package_file) == [
        f"{package_file}: segment segment.demo.big_orders targets unknown entity "
        "'entity.demo.order'; did you mean 'entity.demo_order'?"
    ]

    payload["segments"]["big_orders"]["entity"] = "order"
    _write_yaml(package_file, payload)

    assert validate_runtime_package(package_file) == []

    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.demo_order",
        "input": {"measure": "measure.demo.order_count"},
        "op": ">=",
        "value": 1,
        "window": 7,
    }
    payload["segments"]["big_orders"]["membership"] = {
        "metric_filters": [{"expression": predicate, "op": "=", "value": True}]
    }
    _write_yaml(package_file, payload)

    errors = validate_runtime_package(package_file)
    assert len(errors) == 1, errors
    assert errors[0].startswith(
        f"{package_file}: segment segment.demo.big_orders query does not compile (TypeError): "
    )
    report, config = parse_config_report(resolve_package_reference(path=str(package_file)))
    assert (report["ok"], config) == (False, None)


# Directory packages used to hand metric and segment specs to the shape check as
# (path, spec) pairs, which it skipped, so unknown keys there passed validation.


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda segment: segment.update(
                where=[{"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"}]
            ),
            "has 'where' outside membership:",
        ),
        (
            lambda segment: segment["membership"].update(
                metric_filter=segment["membership"].pop("metric_filters")
            ),
            "membership has unknown key 'metric_filter' — the loader reads "
            "membership.metric_filters only",
        ),
        (
            lambda segment: segment["membership"].update(
                filter=[{"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"}]
            ),
            "membership has unknown key 'filter' — the loader reads membership.where only",
        ),
        (
            lambda segment: segment.update(meta={"owner_team": "growth"}),
            "has unknown key 'meta'",
        ),
        (
            lambda segment: segment["membership"].update(
                dimension_filters=[
                    {"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"}
                ]
            ),
            "write it under membership.where as {field, op, value} rows",
        ),
        (
            lambda segment: segment.update(
                filters=[{"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"}]
            ),
            "has 'filters' outside membership: — the loader reads membership.where only",
        ),
        (
            lambda segment: segment["membership"].update(path_policy={"preference": "fewest_hops"}),
            "membership has unknown key 'path_policy'",
        ),
    ],
    ids=[
        "where-outside-membership",
        "membership-typo",
        "membership-filter",
        "segment-meta",
        "dimension-filters",
        "top-level-filters",
        "membership-path-policy",
    ],
)
def test_directory_validation_rejects_unknown_segment_keys(
    package_config_factory, mutate, expected
):
    package_dir = _jaffle_with_segment(package_config_factory, mutate)

    errors = validate_runtime_package(package_dir)

    assert len(errors) == 1, errors
    assert expected in errors[0]


def test_directory_validation_rejects_unknown_metric_keys(package_config_factory):
    _, package_dir = package_config_factory("jaffle_shop")
    metric_file = Path(package_dir) / "metrics" / "core" / "core_metrics.yml"
    raw = yaml.safe_load(metric_file.read_text(encoding="utf-8"))
    metric_key = next(iter(raw["metrics"]))
    raw["metrics"][metric_key]["valeu_type"] = "number"
    _write_yaml(metric_file, raw)

    errors = validate_runtime_package(Path(package_dir))

    assert len(errors) == 1, errors
    assert f"metric '{metric_key}' has unknown key 'valeu_type'" in errors[0]
    assert "did you mean 'value_type'?" in errors[0]


def test_single_file_validation_rejects_unknown_membership_keys(tmp_path: Path):
    package_file = tmp_path / "monolithic.yml"
    _write_monolithic_package(package_file, "monolithic_demo")
    payload = yaml.safe_load(package_file.read_text(encoding="utf-8"))
    payload["segments"] = {
        "big_orders": {
            "as": "segment.demo.big_orders",
            "entity": "order",
            "basis_metric": "metric.sales.orders",
            "membership": {
                "filters": [{"field": "dimension.demo_order_id", "op": ">", "value": 1}]
            },
        }
    }
    _write_yaml(package_file, payload)

    errors = validate_runtime_package(package_file)

    assert len(errors) == 1, errors
    assert "segment 'big_orders' membership has unknown key 'filters'" in errors[0]
    assert "write it under membership.where" in errors[0]


def test_directory_validation_accepts_every_membership_key_the_loader_reads(
    package_config_factory,
):
    def add_every_key(segment):
        for key in ("where", "metric_filters"):
            segment["membership"].setdefault(key, [])
        for key in ("time", "temporal_role_overrides"):
            segment["membership"].setdefault(key, {})

    errors = validate_runtime_package(_jaffle_with_segment(package_config_factory, add_every_key))

    assert errors == []


@pytest.mark.parametrize(
    ("kind", "key", "expected"),
    [
        ("metric", "preferred_filter_ops", "has unknown key 'preferred_filter_ops'"),
        ("metric", "clock_variants", "has unknown key 'clock_variants'"),
        ("segment", "clock_variants", "has unknown key 'clock_variants'"),
    ],
)
def test_strict_legacy_key_is_reported_once(package_config_factory, kind, key, expected):
    _, package_dir = package_config_factory("jaffle_shop")
    source = Path(package_dir) / (
        "segments/core.yml" if kind == "segment" else "metrics/core/core_metrics.yml"
    )
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    next(iter(doc[f"{kind}s"].values()))[key] = ["legacy"]
    _write_yaml(source, doc)

    errors = validate_runtime_package(Path(package_dir))

    assert len(errors) == 1, errors
    assert expected in errors[0]


def _jaffle_with_relocated_spec(
    package_config_factory, kind: str, layout: str, extra_key: str | None
) -> Path:
    """Move one jaffle metric or segment into another layout the loader reads."""
    _, package_dir = package_config_factory("jaffle_shop")
    package_dir = Path(package_dir)
    if kind == "segment":
        source, key = package_dir / "segments" / "core.yml", "customer.high_value"
    else:
        source, key = package_dir / "metrics" / "core" / "core_metrics.yml", "sales.aov_usd"
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    spec = doc[f"{kind}s"].pop(key)
    _write_yaml(source, doc)
    if extra_key:
        target = spec["membership"] if kind == "segment" else spec
        target[extra_key] = [] if kind == "segment" else "number"
    if layout == "folder_file":
        _write_yaml(package_dir / f"{kind}s" / "relocated.yml", {f"{kind}s": {key: spec}})
    elif layout == "root_file":
        _write_yaml(package_dir / f"{kind}s.yml", {f"{kind}s": {key: spec}})
    else:
        package_yml = package_dir / "package.yml"
        root = yaml.safe_load(package_yml.read_text(encoding="utf-8"))
        root[f"{kind}s"] = {key: spec}
        _write_yaml(package_yml, root)
    return package_dir


# Every layout twice: once valid and once with an unknown key, for a segment or a metric.
@pytest.mark.parametrize(
    ("kind", "extra_key", "layout"),
    [
        ("segment", None, "folder_file"),
        ("segment", None, "root_file"),
        ("segment", "filters", "package_yml"),
        ("metric", None, "package_yml"),
        ("metric", "valeu_type", "folder_file"),
        ("metric", "valeu_type", "root_file"),
    ],
)
def test_key_checks_cover_every_layout_the_loader_reads(
    package_config_factory, kind, extra_key, layout
):
    package_dir = _jaffle_with_relocated_spec(package_config_factory, kind, layout, extra_key)

    errors = validate_runtime_package(package_dir)

    if extra_key is None:
        assert errors == []
    else:
        assert len(errors) == 1, errors
        assert f"unknown key '{extra_key}'" in errors[0]


def _jaffle_with_duplicate_metric(package_config_factory, *, typo_in: str) -> Path:
    """Define sales.aov_usd again in metrics/zz_extra.yml, with a typo in one copy.

    The loader merges files in sorted path order, so metrics/zz_extra.yml would replace
    metrics/core/core_metrics.yml's copy.
    """
    _, package_dir = package_config_factory("jaffle_shop")
    package_dir = Path(package_dir)
    core = package_dir / "metrics" / "core" / "core_metrics.yml"
    doc = yaml.safe_load(core.read_text(encoding="utf-8"))
    extra = {"metrics": {"sales.aov_usd": dict(doc["metrics"]["sales.aov_usd"])}}
    target = extra if typo_in == "kept" else doc
    target["metrics"]["sales.aov_usd"]["valeu_type"] = "number"
    _write_yaml(core, doc)
    _write_yaml(package_dir / "metrics" / "zz_extra.yml", extra)
    return package_dir


@pytest.mark.parametrize("typo_in", ["kept", "discarded"])
def test_a_metric_defined_twice_is_refused_whichever_copy_is_kept(package_config_factory, typo_in):
    package_dir = _jaffle_with_duplicate_metric(package_config_factory, typo_in=typo_in)

    errors = validate_runtime_package(package_dir)

    assert len(errors) == 1, errors
    assert "zz_extra.yml defines metric 'sales.aov_usd', which " in errors[0]
    assert errors[0].endswith(
        "core_metrics.yml is ignored by the loader, so this would "
        "silently change behavior; keep one definition"
    )


def test_key_checks_skip_the_directories_the_loader_skips(package_config_factory):
    package_dir = _jaffle_with_segment(
        package_config_factory,
        lambda segment: segment.update(
            where=[{"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"}]
        ),
    )
    stale = {"segments": {"stale": {"entity": "customer", "membership": {"filters": []}}}}
    for skipped in ("__pycache__", ".compiled"):
        _write_yaml(package_dir / "segments" / skipped / "stale.yml", stale)
    (package_dir / "segments" / ".compiled" / "broken.yml").write_text(
        "segments: [unclosed", encoding="utf-8"
    )

    errors = validate_runtime_package(package_dir)

    # The loader never reads the skipped directories: a stale spec there adds no key
    # error, and a broken file there doesn't turn the key checks off. (The per-file
    # YAML readers still report the broken file, as they did before.)
    assert not any("'stale'" in error for error in errors), errors
    assert any(
        "segment 'customer.high_value' has 'where' outside membership:" in error for error in errors
    ), errors


def test_key_checks_accept_a_relative_package_path(package_config_factory, monkeypatch):
    package_dir = _jaffle_with_segment(
        package_config_factory,
        lambda segment: segment.update(
            where=[{"field": "dimension.jaffle_customer_type", "op": "=", "value": "new"}]
        ),
    )
    monkeypatch.chdir(package_dir.parent)

    errors = validate_runtime_package(Path(package_dir.name))

    assert len(errors) == 1, errors
    assert "has 'where' outside membership:" in errors[0]


@pytest.mark.parametrize("version", [".inf", "-.inf", ".nan"])
def test_nonfinite_schema_version_returns_invalid_report(tmp_path: Path, version: str):
    package_dir = tmp_path / "nonfinite_version"
    package_dir.mkdir()
    (package_dir / "package.yml").write_text(
        f"schema_version: {version}\npackage: {{}}\n", encoding="utf-8"
    )

    errors = validate_runtime_package(package_dir)
    report, config = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert any("failed to load package config" in error for error in errors)
    assert config is None
    assert report["ok"] is False
    assert any(
        error["code"] == "INVALID_CONFIG" and "failed to load package config" in error["message"]
        for error in report["errors"]
    )


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, None),
        ({"value_type": None}, "metric 'quoted': missing 'value_type:'"),
        ({"valeu_type": "count"}, "has unknown key 'valeu_type'"),
    ],
    ids=["valid", "value-type", "unknown-key"],
)
def test_strict_checks_run_for_a_quoted_schema_version(tmp_path: Path, extra, expected):
    # The loader accepts schema_version: "1", so the directory checks must run for it.
    spec = {
        "label": "Quoted",
        "description": "metric in a package with a quoted schema_version",
        "kind": "aggregate",
        "measure": "order_count",
        "value_type": "count",
        **extra,
    }
    package_dir = tmp_path / "strict_quoted_version"
    _write_current_minimal_package(package_dir, extra_metrics={"quoted": spec})
    package_yml = package_dir / "package.yml"
    root = yaml.safe_load(package_yml.read_text(encoding="utf-8"))
    root["schema_version"] = "1"
    _write_yaml(package_yml, root)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    messages = [str(err.get("message", "")) for err in report["errors"]]
    assert report["ok"] is (expected is None), messages
    if expected:
        assert any(expected in msg for msg in messages), messages


# A metric's temporal_role must be a clock its measures can be timed by. Otherwise each
# leaf falls back to its own clock and the answer is labeled with the declared one.


def _jaffle_with_metric_role(package_config_factory, metric_file: str, key: str, role: str):
    _, package_dir = package_config_factory("jaffle_shop")
    path = Path(package_dir) / "metrics" / metric_file
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"][key]["temporal_role"] = role
    _write_yaml(path, doc)
    return Path(package_dir)


def test_validation_rejects_a_metric_timed_by_a_clock_its_measures_lack(package_config_factory):
    # AOV over order measures, labeled with the sessions table's clock.
    package_dir = _jaffle_with_metric_role(
        package_config_factory,
        "core/core_metrics.yml",
        "sales.aov_usd",
        "temporal_role.jaffle_session_started_at",
    )

    errors = validate_runtime_package(package_dir)

    assert len(errors) == 1, errors
    assert (
        "metric metric.sales.aov_usd has temporal_role "
        "'temporal_role.jaffle_session_started_at', but its measures are timed by "
        "temporal_role.jaffle_order_time"
    ) in errors[0]


def test_validation_times_a_conversion_metric_by_its_base_operand(package_config_factory):
    # Sessions to orders: the sessions clock is right, the orders clock is not.
    package_dir = _jaffle_with_metric_role(
        package_config_factory,
        "extensions/advanced_metrics.yml",
        "sales.session_to_order_conversion_rate_7d",
        "temporal_role.jaffle_order_time",
    )

    errors = validate_runtime_package(package_dir)

    assert len(errors) == 1, errors
    assert "sales.session_to_order_conversion_rate_7d has temporal_role" in errors[0]
    assert "timed by temporal_role.jaffle_session_started_at" in errors[0]


def _add(left: dict, right: dict) -> dict:
    return {"kind": "arithmetic", "op": "add", "left": left, "right": right}


_SESSIONS = {"kind": "aggregate", "measure": "measure.jaffle.session_starts"}
_ORDERS = {"kind": "aggregate", "measure": "measure.jaffle.order_count"}
_SPEND_PREDICATE = {
    "kind": "metric_predicate",
    "entity": "entity.jaffle_customer",
    "scope_mode": "entity_only",
    "input": {"measure": "lifetime_spend"},
    "op": ">=",
    "value": 100,
}


@pytest.mark.parametrize(
    "expression",
    [
        _add(_SESSIONS, _ORDERS),
        _add(_add(_SESSIONS, _ORDERS), _ORDERS),
        _add(_SESSIONS, _add(_ORDERS, _ORDERS)),
    ],
    ids=["a+b", "(a+b)+b", "a+(b+b)"],
)
def test_validation_accepts_a_mixed_clock_metric_however_it_is_grouped(
    package_config_factory, expression
):
    # Sessions plus orders on the sessions clock: the planner aligns the orders to their
    # own clock on purpose, and the label is right for the sessions.
    package_dir = _jaffle_with_sessions_plus_orders(package_config_factory, expression)

    assert validate_runtime_package(package_dir) == []


@pytest.mark.parametrize(
    "expression",
    [
        _add(_SESSIONS, _ORDERS),
        _add(_SESSIONS, _add(_ORDERS, _ORDERS)),
        _add(_SESSIONS, {"kind": "metric", "metric": "metric.sales.repeat_customer_orders"}),
    ],
    ids=["a+b", "a+(b+b)", "a+metric(b)"],
)
def test_validation_rejects_a_mixed_clock_metric_whose_measure_has_several_clocks(
    package_config_factory, expression
):
    # With two clocks of its own, which one the orders should be aligned by is a guess,
    # so a query on the sessions clock is refused. The session-to-order conversions read
    # order_count too, but conversion operands keep their own clock rules.
    package_dir = _jaffle_with_sessions_plus_orders(package_config_factory, expression)
    path = package_dir / "models" / "core" / "orders.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    order_clocks = [
        "temporal_role.jaffle_order_time",
        "temporal_role.jaffle_customer_first_order_at",
    ]
    doc["model"]["measures"]["order_count"]["times"] = order_clocks
    _write_yaml(path, doc)

    errors = validate_runtime_package(package_dir)

    assert len(errors) == 1, errors
    assert "metric metric.sales.sessions_plus_orders has temporal_role" in errors[0]
    assert f"timed by {', '.join(sorted(order_clocks))} instead" in errors[0]


def _jaffle_with_sessions_plus_orders(package_config_factory, expression: dict) -> Path:
    _, package_dir = package_config_factory("jaffle_shop")
    path = Path(package_dir) / "metrics" / "extensions" / "derived_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"]["sales.sessions_plus_orders"] = {
        "as": "metric.sales.sessions_plus_orders",
        "label": "Sessions plus orders",
        "description": "Two clocks in one expression.",
        "kind": "derived",
        "value_type": "count",
        "temporal_role": "temporal_role.jaffle_session_started_at",
        "expression": expression,
    }
    _write_yaml(path, doc)
    return Path(package_dir)


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        (
            {"kind": "aggregate", "measure": "order_cnt"},
            "references unknown measure 'order_cnt'; did you mean 'measure.jaffle.order_count'?",
        ),
        (
            _add(_ORDERS, {"kind": "metric", "metric": "metric.sales.no_such_metric"}),
            "references unknown metric 'metric.sales.no_such_metric'",
        ),
        (
            _add(_SESSIONS, {**_ORDERS, "filter": {"all": [{"expression": _SPEND_PREDICATE}]}}),
            "references unknown measure 'lifetime_spend'; "
            "did you mean 'measure.jaffle.lifetime_spend_usd'?",
        ),
    ],
    ids=["measure", "metric", "filter-predicate"],
)
def test_parse_report_rejects_a_metric_reference_the_package_lacks(
    package_config_factory, expression, error
):
    # The loader keeps an unresolved reference as written, so only a query failed.
    package_dir = _jaffle_with_sessions_plus_orders(package_config_factory, expression)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    messages = [row["message"] for row in report["errors"]]
    assert report["ok"] is False
    assert [message.split(": ", 1)[1] for message in messages] == [
        f"metric metric.sales.sessions_plus_orders {error}"
    ]


def test_package_authoring_suggests_a_measure_hidden_from_every_audience(package_config_factory):
    package_dir = _jaffle_with_sessions_plus_orders(
        package_config_factory, {"kind": "aggregate", "measure": "order_cnt"}
    )
    path = package_dir / "policies.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["semantic_policies"].append(
        {
            "id": "policy.hide_orders",
            "kind": "object_visibility",
            "object_ids": ["measure.jaffle.order_count"],
            "action": "hidden",
        }
    )
    _write_yaml(path, doc)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    messages = [row["message"] for row in report["errors"]]
    assert any("did you mean 'measure.jaffle.order_count'?" in message for message in messages)


@pytest.mark.parametrize("mutual", [False, True], ids=["self-cycle", "mutual-cycle"])
def test_parse_report_rejects_cyclic_metric_references(package_config_factory, mutual):
    _, package_dir = package_config_factory("jaffle_shop")
    path = Path(package_dir) / "metrics" / "core" / "core_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    aov = doc["metrics"]["sales.aov_usd"]
    aov["kind"] = "derived"
    aov.pop("numerator", None)
    aov.pop("denominator", None)
    aov["expression"] = {
        "kind": "metric",
        "metric": "metric.sales.cycle_peer" if mutual else "metric.sales.aov_usd",
    }
    if mutual:
        doc["metrics"]["sales.cycle_peer"] = {
            "as": "metric.sales.cycle_peer",
            "kind": "derived",
            "description": "The other side of an invalid cycle.",
            "value_type": "currency",
            "temporal_role": "temporal_role.jaffle_order_time",
            "expression": {"kind": "metric", "metric": "metric.sales.aov_usd"},
        }
    _write_yaml(path, doc)
    # A conversion over the cycle reports it too, instead of recursing.
    path = Path(package_dir) / "metrics" / "extensions" / "advanced_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    conversion = doc["metrics"]["sales.session_to_order_conversion_rate_7d"]["expression"]
    conversion["base"] = {"kind": "metric", "metric": "metric.sales.aov_usd"}
    _write_yaml(path, doc)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    assert any("cyclic metric recipe reference" in error["message"] for error in report["errors"])


def test_validation_accepts_shared_acyclic_metric_references(package_config_factory):
    _, package_dir = package_config_factory("jaffle_shop")
    path = Path(package_dir) / "metrics" / "core" / "core_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"]["sales.aov_twice"] = {
        "as": "metric.sales.aov_twice",
        "kind": "derived",
        "description": "Average order value reached twice through one recipe.",
        "value_type": "currency",
        "temporal_role": "temporal_role.jaffle_order_time",
        "expression": _add(
            {"kind": "metric", "metric": "metric.sales.aov_usd"},
            {"kind": "metric", "metric": "metric.sales.aov_usd"},
        ),
    }
    _write_yaml(path, doc)

    assert validate_runtime_package(Path(package_dir)) == []


def _operand(measure: str, field: str, value: object) -> dict:
    return {
        "kind": "aggregate",
        "measure": measure,
        "filter": {"all": [{"field": field, "op": "=", "value": value}]},
    }


@pytest.mark.parametrize(
    ("measure", "field", "temporal_role", "refused"),
    [
        # Each customer is one event on one clock, so the window never applies.
        ("customer_count", "customer_type", "customer_first_order_at", True),
        # A first order, then a repeat order by the same customer within the window.
        ("order_count", "order_is_new_customer_order", "order_time", False),
    ],
    ids=["counts-customers", "counts-orders"],
)
def test_parse_report_rejects_a_conversion_whose_window_cannot_apply(
    package_config_factory, measure, field, temporal_role, refused
):
    _, package_dir = package_config_factory("jaffle_shop")
    path = Path(package_dir) / "metrics" / "extensions" / "derived_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    base_value, converted_value = ("new", "repeat") if refused else (True, False)
    doc["metrics"]["sales.repeat_rate_90d"] = {
        "as": "metric.sales.repeat_rate_90d",
        "label": "Repeat rate (90d)",
        "description": "First orders followed by another order within 90 days.",
        "kind": "conversion",
        "value_type": "percent",
        "temporal_role": f"temporal_role.jaffle_{temporal_role}",
        "expression": {
            "kind": "conversion",
            "entity": "entity.jaffle_customer",
            "window": {"unit": "day", "value": 90},
            "matching_mode": "first_converted_after_base",
            "base": _operand(measure, f"dimension.jaffle_{field}", base_value),
            "converted": _operand(measure, f"dimension.jaffle_{field}", converted_value),
        },
    }
    _write_yaml(path, doc)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    messages = [row["message"].split(": ", 1)[1] for row in report["errors"]]
    assert report["ok"] is not refused, messages
    if refused:
        assert messages == [
            "metric metric.sales.repeat_rate_90d: Both conversion operands count "
            "'entity.jaffle_customer' itself on the clock "
            "'temporal_role.jaffle_customer_first_order_at', so each base event converts to "
            "itself at the same time and the 90-day window can never apply. Count events keyed "
            "by 'entity.jaffle_customer' in both operands instead (see `candidate_measures`)."
        ]


@pytest.mark.parametrize(
    ("spec", "twin"),
    [
        ({"entity_key": "order_id"}, "order_count"),
        ({"entity_key": "customer_id"}, "ordering_customer_count"),
        # Another clock beyond the same default one: the warning says to keep both clocks.
        (
            {
                "entity_key": "order_id",
                "times": [
                    "temporal_role.jaffle_order_time",
                    "temporal_role.jaffle_customer_first_order_at",
                ],
            },
            "order_count",
        ),
        # The same count on another default clock, or of another column, is its own measure.
        (
            {"entity_key": "order_id", "times": ["temporal_role.jaffle_customer_first_order_at"]},
            None,
        ),
        ({"entity_key": "store_id"}, None),
    ],
    ids=["same-key", "same-column", "extra-clock", "other-clock", "other-column"],
)
def test_parse_report_warns_on_a_measure_that_duplicates_another(
    package_config_factory, spec, twin
):
    _, package_dir = package_config_factory("jaffle_shop")
    path = Path(package_dir) / "models" / "core" / "orders.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["model"]["measures"]["placed_orders"] = {
        "kind": "entity_count",
        "description": "Orders placed.",
        **spec,
    }
    _write_yaml(path, doc)

    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is True, report["errors"]
    duplicates = [
        row["message"].split(": ", 1)[1]
        for row in report["warnings"]
        if "duplicates" in row["message"]
    ]
    assert duplicates == (
        [
            f"public measure measure.jaffle.placed_orders duplicates measure.jaffle.{twin} "
            "(same entity, expression, aggregation and default clock). Keep one, give it every "
            "clock the other has, and point anything that reads the other at it."
        ]
        if twin
        else []
    )
