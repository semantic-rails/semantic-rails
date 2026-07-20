from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import yaml

from semantic_rails import cli as cli_module
from semantic_rails.config_validation import parse_config_report, resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
)
from semantic_rails.package_tools import (
    ARTIFACT_MANIFEST_NAME,
    build_package_artifact_report,
    check_package_report,
    diff_package_report,
    impact_report,
    promote_package_report,
    run_examples_report,
    run_package_tests_report,
)
from semantic_rails.runtime import Runtime


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_seed_sql(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
CREATE TABLE order_fact (
  order_id INTEGER,
  ordered_at TIMESTAMP,
  order_total_cents INTEGER
);

INSERT INTO order_fact VALUES
  (1, '2024-01-01 00:00:00', 100),
  (2, '2024-01-02 00:00:00', 200);
""".strip(),
        encoding="utf-8",
    )


def _write_package(
    package_dir: Path,
    *,
    package_environments: list[str] | None = None,
    semantic_policies: list[dict] | None = None,
    extra_measures: dict | None = None,
    measure_meta: dict | None = None,
) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": package_dir.name,
                "name": package_dir.name,
                "description": f"{package_dir.name} demo package",
                "warehouse": "duckdb",
                "default_db": "data/example.duckdb",
                "environments": package_environments or [],
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                }
            },
            "semantic_policies": list(semantic_policies or []),
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
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
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
                        "meta": dict(measure_meta or {}),
                        "publish": {"id": "metric.sales.orders"},
                    },
                    **dict(extra_measures or {}),
                },
            }
        },
    )
    _write_yaml(package_dir / "metrics.yml", {"metrics": {}})
    _write_seed_sql(package_dir / "data" / "seed_example.sql")


def test_runtime_and_builder_surfaces_include_agentic_contract_fields(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        invalid = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "dup"},
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "dup"},
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        assert invalid["status"] == "error"
        assert invalid["recovery_hints"]
        compiled = runtime.compile(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        queried = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        assert compiled["status"] == "ok"
        assert compiled["provenance_summary"]["root_entity"] == "entity.jaffle_order"
        assert compiled["provenance_summary"]["selected_paths"]
        # jaffle's policies.yml declares a package_release label and
        # three object-scoped policies on revenue/lifetime-spend
        # measures. order_count is not in any policy's object_ids, so
        # the only effect surfaced here is the package-wide release
        # label; no deny/redact/hide actions apply.
        assert queried["policy_effects"]
        assert all(effect.get("kind") == "package_release" for effect in queried["policy_effects"])
        assert queried["provenance_summary"]["selected_paths"]

        build = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
            },
            step="group_by",
            focus_terms="orders by store",
            limit=3,
        )
        assert build["recommended"][0]["why_recommended"]
        assert build["recommended"][0]["result_shape_preview"]["columns"]
    finally:
        runtime.close()


def test_policy_scaffolding_hides_objects_and_blocks_queries(tmp_path: Path):
    package_dir = tmp_path / "policy_demo"
    _write_package(
        package_dir,
        package_environments=["development", "staging", "production"],
        semantic_policies=[
            {
                "id": "policy.demo.hidden_secret",
                "kind": "object_visibility",
                "object_ids": ["measure.demo.secret_orders"],
                "action": "hidden",
                "rationale": "internal only",
            },
            {
                "id": "policy.demo.production_hidden_secret",
                "kind": "object_visibility",
                "object_ids": ["measure.demo.production_secret_orders"],
                "action": "hidden",
                "environments": ["production"],
                "rationale": "production only",
            },
            {
                "id": "policy.demo.deny_orders",
                "kind": "object_access",
                "object_ids": ["metric.sales.orders"],
                "action": "deny",
                "rationale": "blocked for review",
            },
        ],
        extra_measures={
            "secret_orders": {
                "id": "measure.demo.secret_orders",
                "name": "sales.secret_orders",
                "label": "Secret orders",
                "description": "Hidden measure",
                "kind": "entity_count",
                "time": "ordered_at",
                "topics": ["orders"],
                "publish": False,
            },
            "production_secret_orders": {
                "id": "measure.demo.production_secret_orders",
                "name": "sales.production_secret_orders",
                "label": "Production secret orders",
                "description": "Hidden only for production context",
                "kind": "entity_count",
                "time": "ordered_at",
                "topics": ["orders"],
                "publish": False,
            },
        },
        measure_meta={"owner_team": "finance", "review_priority": "high", "change_risk": "medium"},
    )
    runtime = Runtime.from_path(str(package_dir))
    try:
        discovery = discover_payload(runtime, terms="secret", stage="initial", limit=5)
        assert all(row["id"] != "measure.demo.secret_orders" for row in discovery["measures"])
        assert any(
            row["id"] == "measure.demo.production_secret_orders" for row in discovery["measures"]
        )
        production_discovery = discover_payload(
            runtime,
            terms="production secret",
            stage="initial",
            limit=5,
            partial_query={"policy_context": {"environment": "production"}},
        )
        assert all(
            row["id"] != "measure.demo.production_secret_orders"
            for row in production_discovery["measures"]
        )
        catalog = catalog_payload(runtime, kind="metric", search="orders")
        denied_metric = next(
            row for row in catalog["metrics"] if row["id"] == "metric.sales.orders"
        )
        assert any(row["action"] == "deny" for row in denied_metric["policy_effects"])
        try:
            inspect_payload(runtime, object_id="measure.demo.secret_orders")
        except SemanticLayerError as exc:
            assert exc.code == "OBJECT_NOT_FOUND"
        else:  # pragma: no cover - defensive branch
            raise AssertionError("hidden objects should not be inspectable")
        denied = runtime.validate(
            {
                "version": 1,
                "select": [{"expression": {"metric": "metric.sales.orders"}, "as": "orders"}],
                "time": {"temporal_role": "temporal_role.demo_order_time", "grain": "day"},
            }
        )
        assert denied["ok"] is False
        assert denied["errors"][0]["code"] == "POLICY_DENIED"
    finally:
        runtime.close()


def test_role_scoped_object_policy_uses_request_roles(tmp_path: Path):
    package_dir = tmp_path / "role_policy_demo"
    _write_package(
        package_dir,
        semantic_policies=[
            {
                "id": "policy.demo.sales_orders_hold",
                "kind": "object_access",
                "object_ids": ["metric.sales.orders"],
                "roles": ["sales"],
                "action": "deny",
                "rationale": "sales access is paused for review",
            }
        ],
    )
    runtime = Runtime.from_path(str(package_dir))
    try:
        base_query = {
            "version": 1,
            "select": [{"expression": {"metric": "metric.sales.orders"}, "as": "orders"}],
            "time": {"temporal_role": "temporal_role.demo_order_time", "grain": "day"},
        }
        denied = runtime.validate({**base_query, "policy_context": {"roles": ["sales"]}})
        assert denied["ok"] is False
        assert denied["errors"][0]["code"] == "POLICY_DENIED"
        assert denied["policy_effects"][0]["roles"] == ["sales"]

        allowed = runtime.validate({**base_query, "policy_context": {"roles": ["csm"]}})
        assert allowed["ok"] is True
        assert allowed["policy_effects"] == []
    finally:
        runtime.close()


def test_metric_constraint_policy_restricts_role_scoped_revenue_cuts(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        base_query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        }

        allowed = runtime.validate(
            {
                **base_query,
                "group_by": ["dimension.jaffle_store_name"],
                "where": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}],
                "policy_context": {"roles": ["sales"]},
            }
        )
        assert allowed["ok"] is True
        constraint_effect = next(
            row
            for row in allowed["policy_effects"]
            if row["policy_id"] == "policy.jaffle.gtm_revenue_store_cuts"
        )
        assert constraint_effect["action"] == "constrain"
        assert constraint_effect["roles"] == ["sales", "csm"]

        denied_group = runtime.validate(
            {
                **base_query,
                "group_by": ["dimension.jaffle_customer_type"],
                "policy_context": {"roles": ["sales"]},
            }
        )
        assert denied_group["ok"] is False
        assert denied_group["errors"][0]["code"] == "POLICY_DENIED"
        assert denied_group["recovery_hints"][0]["kind"] == "adjust_metric_cuts"
        assert denied_group["policy_effects"][0]["violations"][0]["kind"] == "disallowed_group_by"

        denied_filter = runtime.validate(
            {
                **base_query,
                "where": [
                    {"field": "dimension.jaffle_customer_type", "op": "=", "value": "repeat"}
                ],
                "policy_context": {"roles": ["csm"]},
            }
        )
        assert denied_filter["ok"] is False
        assert denied_filter["policy_effects"][0]["violations"][0]["kind"] == "disallowed_where"

        unconstrained_role = runtime.validate(
            {
                **base_query,
                "group_by": ["dimension.jaffle_customer_type"],
                "policy_context": {"roles": ["finance"]},
            }
        )
        assert unconstrained_role["ok"] is True
        assert not any(
            row["policy_id"] == "policy.jaffle.gtm_revenue_store_cuts"
            for row in unconstrained_role["policy_effects"]
        )
    finally:
        runtime.close()


def test_parse_config_emits_authoring_profile_warnings(tmp_path: Path):
    package_dir = tmp_path / "warning_demo"
    _write_package(package_dir)
    report, _ = parse_config_report(resolve_package_reference(path=str(package_dir)))
    assert report["ok"] is True
    assert report["warnings"]
    assert any(warning["code"] == "AUTHORING_PROFILE_WARNING" for warning in report["warnings"])


def test_package_tools_and_cli_support_examples_tests_diff_and_promotion(
    tmp_path: Path, capsys, monkeypatch
):
    base_dir = tmp_path / "base_demo"
    _write_package(
        base_dir,
        package_environments=["development", "staging"],
        measure_meta={"owner_team": "finance", "review_priority": "high", "change_risk": "low"},
    )
    _write_yaml(
        base_dir / "examples" / "orders.yml",
        {
            "examples": {
                "orders_example": {
                    "question": "Orders by day",
                    "query": {
                        "version": 1,
                        "select": [
                            {"expression": {"measure": "measure.demo.order_count"}, "as": "orders"}
                        ],
                        "time": {"temporal_role": "temporal_role.demo_order_time", "grain": "day"},
                    },
                    "expected_shape": {
                        "columns": ["temporal_role.demo_order_time__day", "orders"],
                        "min_rows": 1,
                    },
                }
            }
        },
    )
    _write_yaml(
        base_dir / "tests" / "orders.yml",
        {
            "tests": {
                "orders_validation": {
                    "kind": "query_row_count_bounds",
                    "query": {
                        "version": 1,
                        "select": [
                            {"expression": {"measure": "measure.demo.order_count"}, "as": "orders"}
                        ],
                    },
                    "min_rows": 1,
                    "max_rows": 1,
                }
            }
        },
    )

    changed_dir = tmp_path / "changed_demo"
    _write_package(
        changed_dir,
        package_environments=["development", "staging"],
        measure_meta={"owner_team": "finance", "review_priority": "high", "change_risk": "high"},
    )
    payload = yaml.safe_load((changed_dir / "models" / "orders.yml").read_text(encoding="utf-8"))
    payload["model"]["measures"]["order_count"]["default_temporal_role"] = (
        "temporal_role.demo_order_time"
    )
    _write_yaml(changed_dir / "models" / "orders.yml", payload)

    ref = resolve_package_reference(path=str(base_dir))
    example_report = run_examples_report(ref)
    test_report = run_package_tests_report(ref)
    check_report = check_package_report(ref)
    artifact_report = build_package_artifact_report(
        ref, output_path=str(tmp_path / "semantic-package.tar.gz")
    )
    diff = diff_package_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )
    impact = impact_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )
    promotion = promote_package_report(
        resolve_package_reference(path=str(base_dir)), environment="staging"
    )

    assert example_report["ok"] is True
    assert test_report["ok"] is True
    assert check_report["ok"] is True
    assert check_report["blockers"] == []
    assert check_report["manifest"]["package"]["id"] == "base_demo"
    assert artifact_report["ok"] is True
    assert artifact_report["blockers"] == []
    assert artifact_report["artifact"]["file_count"] > 0
    with tarfile.open(artifact_report["artifact"]["path"], "r:gz") as archive:
        assert ARTIFACT_MANIFEST_NAME in archive.getnames()
        manifest_handle = archive.extractfile(ARTIFACT_MANIFEST_NAME)
        assert manifest_handle is not None
        manifest = json.loads(manifest_handle.read().decode("utf-8"))
    assert manifest["package"]["id"] == "base_demo"
    assert manifest["package_hash"] == artifact_report["package_hash"]
    assert diff["summary"]["changes_total"] > 0
    assert impact["impact"]["reviewer_teams"] == ["finance"]
    assert promotion["ok"] is True
    assert promotion["blockers"] == []

    monkeypatch.setattr(sys, "argv", ["semantic-rails", "run-examples", "--path", str(base_dir)])
    cli_module.main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True

    monkeypatch.setattr(sys, "argv", ["semantic-rails", "check", "--path", str(base_dir)])
    cli_module.main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["blockers"] == []

    _write_yaml(
        base_dir / "tests" / "failing.yml",
        {
            "tests": {
                "bad_error_expectation": {
                    "kind": "validate_fails_with_code",
                    "query": {
                        "version": 1,
                        "select": [
                            {"expression": {"measure": "measure.demo.order_count"}, "as": "orders"}
                        ],
                    },
                    "code": "NOT_THE_ERROR",
                }
            }
        },
    )
    failed_check = check_package_report(ref)
    failed_promotion = promote_package_report(ref, environment="staging")
    assert failed_check["ok"] is False
    assert failed_check["blockers"]
    assert any(row["check"] == "tests" for row in failed_check["blockers"])
    assert failed_promotion["ok"] is False
    assert failed_promotion["blockers"] == [
        {
            "check": "tests",
            "code": "PROMOTION_CHECK_FAILED",
            "message": "tests check must pass before promotion",
            "details": {"environment": "staging"},
        }
    ]
