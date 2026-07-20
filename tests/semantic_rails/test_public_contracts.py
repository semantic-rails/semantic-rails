from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from semantic_rails.contracts import (
    CONTRACT_NAMES,
    contract_path,
    export_semantic_contract,
    load_contract,
    semantic_contract_fingerprint,
)
from semantic_rails.contracts.compatibility import compare_contract_bundles
from semantic_rails.contracts.generation import PUBLIC_SCHEMA_BASE, generated_artifacts
from semantic_rails.errors import SemanticLayerError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
JAFFLE_SHOP = REPO_ROOT / "configs" / "semantic_rails" / "jaffle_shop"


def _jsonschema():
    return pytest.importorskip("jsonschema")


def test_contract_bundle_is_packaged_and_has_compatibility_copies() -> None:
    for name in CONTRACT_NAMES:
        packaged = contract_path(name)
        compatibility_copy = REPO_ROOT / "schemas" / name
        assert packaged.is_file()
        assert compatibility_copy.is_file()
        assert packaged.read_bytes() == compatibility_copy.read_bytes()
        assert load_contract(name)


def test_code_derived_contract_artifacts_have_no_drift() -> None:
    for name, expected in generated_artifacts().items():
        assert load_contract(name) == expected


def test_public_schema_ids_use_the_controlled_http_mirror() -> None:
    assert PUBLIC_SCHEMA_BASE == "https://semantic-rails.com/schemas/"
    for name in CONTRACT_NAMES:
        payload = load_contract(name)
        schema_id = payload.get("$id")
        if schema_id is not None:
            assert schema_id == f"{PUBLIC_SCHEMA_BASE}{name}"


def test_every_json_schema_is_well_formed() -> None:
    jsonschema = _jsonschema()
    for name in (
        "package.v1.json",
        "query_ir.preview.v2.json",
        "query_ir.v1.json",
        "semantic_contract.v1.json",
        "validation_report.v1.json",
    ):
        jsonschema.Draft202012Validator.check_schema(load_contract(name))


def test_export_semantic_contract_uses_authored_model_ids_and_validates() -> None:
    jsonschema = _jsonschema()
    payload = export_semantic_contract(JAFFLE_SHOP)
    jsonschema.Draft202012Validator(load_contract("semantic_contract.v1.json")).validate(payload)

    assert payload["contract_format_version"] == 1
    semantic = payload["semantic"]
    assert semantic["producer"]["name"] == "semantic-rails"
    package = semantic["packages"][0]
    assert package["package_id"] == "jaffle_shop"
    assert package["namespace"] == "jaffle"
    assert package["package_schema_version"] == 1
    assert package["semantic_hash"].startswith("sha256:")

    resources = {row["semantic_model_id"]: row for row in package["resources"]}
    assert "orders" in resources
    assert resources["orders"]["relation"] == "jaffle_order"
    order_columns = {row["name"]: row for row in resources["orders"]["columns"]}
    assert "order_id" in order_columns
    assert "measure.jaffle.order_count" in order_columns["order_id"]["required_by"]
    assert order_columns["ordered_at"]["data_type"] == "timestamp"


def test_semantic_contract_fingerprint_is_deterministic_and_matches_export() -> None:
    first = semantic_contract_fingerprint(JAFFLE_SHOP)
    second = semantic_contract_fingerprint(str(JAFFLE_SHOP))
    exported = export_semantic_contract(JAFFLE_SHOP)
    assert first == second
    assert exported["semantic"]["packages"][0]["semantic_hash"] == first
    assert len(first) == len("sha256:") + 64


def test_export_semantic_contract_uses_expression_columns_without_invented_names(
    tmp_path: Path,
) -> None:
    project = tmp_path / "expression_contract"
    (project / "models").mkdir(parents=True)
    (project / "package.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "package": {
                    "id": "expression_contract",
                    "namespace": "expression_contract",
                    "warehouse": "duckdb",
                    "default_db": "data/expression_contract.duckdb",
                    "seed": {"kind": "csv_dir_duckdb", "source": "data"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project / "graph.yml").write_text(
        yaml.safe_dump(
            {
                "graph": {
                    "entities": {
                        "order": {"key": "order_id", "model": "orders"},
                        "customer": {"key": "customer_id", "model": "customers"},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project / "models" / "orders.yml").write_text(
        yaml.safe_dump(
            {
                "models": {
                    "customers": {
                        "relation": "analytics.customers",
                        "grain": ["customer_id"],
                        "entities": {"customer": {}},
                        "dimensions": {},
                        "measures": {},
                    },
                    "orders": {
                        "relation": "analytics.orders",
                        "grain": ["order_id"],
                        "entities": {
                            "order": {},
                            "customer": {"expr": {"kind": "column", "column": "customer_id"}},
                        },
                        "dimensions": {
                            "order_month": {"expr": {"kind": "column", "column": "ordered_at"}},
                            "constant_label": {"expr": {"kind": "literal", "value": "orders"}},
                        },
                        "measures": {
                            "revenue": {
                                "kind": "aggregate",
                                "expr": {
                                    "kind": "call",
                                    "name": "coalesce",
                                    "args": [
                                        {"kind": "column", "column": "order_amount"},
                                        {"kind": "literal", "value": 0},
                                    ],
                                },
                                "agg": "sum",
                            }
                        },
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = export_semantic_contract(project)
    resource = next(
        row
        for row in payload["semantic"]["packages"][0]["resources"]
        if row["semantic_model_id"] == "orders"
    )
    names = {column["name"] for column in resource["columns"]}

    assert {"customer_id", "order_id", "ordered_at", "order_amount"}.issubset(names)
    assert "order_month" not in names
    assert "constant_label" not in names
    assert "coalesce" not in names
    assert "orders" not in names
    assert all(not name.startswith(("{", "[")) for name in names)


def test_semantic_contract_schema_allows_adapter_owned_binding() -> None:
    jsonschema = _jsonschema()
    payload = export_semantic_contract(JAFFLE_SHOP)
    payload["binding"] = {
        "kind": "dbt",
        "binding_version": 1,
        "packages": [{"package_id": "jaffle_shop", "models": []}],
        "adapter_extension": {"strict": True},
    }
    jsonschema.Draft202012Validator(load_contract("semantic_contract.v1.json")).validate(payload)


def test_validation_report_v1_envelope_validates() -> None:
    jsonschema = _jsonschema()
    validator = jsonschema.Draft202012Validator(load_contract("validation_report.v1.json"))
    payload = {
        "report_format_version": 1,
        "validator": {"name": "dbt-semantic-rails-contracts", "version": "0.2.0"},
        "input": {
            "contract_format_version": 1,
            "binding_kind": "dbt",
            "binding_version": 1,
            "legacy": False,
        },
        "ok": False,
        "summary": {
            "package_count": 1,
            "resource_count": 1,
            "error_count": 1,
            "warning_count": 0,
        },
        "issues": [
            {
                "code": "DBT_COLUMN_MISSING",
                "severity": "error",
                "package_id": "jaffle_shop",
                "semantic_model_id": "orders",
                "message": "Expected order_id.",
            }
        ],
    }
    validator.validate(payload)

    # A v1 report must be able to describe unsupported or malformed contract
    # identities without making the report envelope itself invalid.
    payload["input"]["contract_format_version"] = 2
    payload["input"]["binding_version"] = 0
    validator.validate(payload)
    payload["input"]["contract_format_version"] = None
    payload["input"]["binding_version"] = None
    validator.validate(payload)


def test_embedding_facade_exposes_supported_host_seams() -> None:
    import semantic_rails.embedding as embedding

    required = {
        "Runtime",
        "SemanticHTTPService",
        "SemanticLayerMCPAdapter",
        "handle_jsonrpc_message",
        "RequestContext",
        "PolicyContextResolver",
        "AuditSink",
        "ConnectionCredentialProvider",
        "WarehouseAdapter",
        "create_warehouse_adapter",
        "warehouse_connector",
        "normalize_connection_options",
        "PackageReference",
        "validate_runtime_package",
        "run_package_tests_report",
    }
    assert required.issubset(set(embedding.__all__))
    assert all(hasattr(embedding, name) for name in required)


def test_export_contract_cli_prints_unwrapped_canonical_payload(monkeypatch, capsys) -> None:
    from semantic_rails.cli import main

    monkeypatch.setattr(
        "sys.argv",
        ["semantic-rails", "export-contract", "--path", str(JAFFLE_SHOP)],
    )
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_format_version"] == 1
    assert "semantic" in payload
    assert "ok" not in payload


def test_compatibility_checker_flags_breaking_schema_http_and_mcp_changes() -> None:
    baseline = generated_artifacts()
    baseline["semantic_contract.v1.json"] = load_contract("semantic_contract.v1.json")
    current = json.loads(json.dumps(baseline))
    del current["http_api.v1.openapi.json"]["paths"]["/api/v1/query"]["post"]
    current["query_mcp.v1.json"]["tools"] = [
        tool for tool in current["query_mcp.v1.json"]["tools"] if tool["name"] != "execute"
    ]
    current["semantic_contract.v1.json"]["$defs"]["SemanticPackage"]["required"].append(
        "new_required"
    )

    report = compare_contract_bundles(baseline, current)
    assert report["ok"] is False
    kinds = {change["kind"] for change in report["breaking_changes"]}
    assert {"operation_removed", "tool_removed", "required_added"}.issubset(kinds)


def test_export_contract_wraps_unexpected_loader_shape_errors(monkeypatch) -> None:
    import semantic_rails.contracts.producer as producer

    def broken_loader(_path):
        raise KeyError("raw-internal-reference")

    monkeypatch.setattr(producer, "load_package_config", broken_loader)
    with pytest.raises(SemanticLayerError) as exc:
        producer.export_semantic_contract(JAFFLE_SHOP)

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"exception_type": "KeyError"}
    assert "raw-internal-reference" not in str(exc.value)
