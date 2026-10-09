from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml

from semantic_rails.contracts import (
    CONTRACT_NAMES,
    contract_path,
    diff_semantic_contract,
    export_semantic_contract,
    load_contract,
    semantic_contract_fingerprint,
)
from semantic_rails.contracts.compatibility import (
    compare_contract_bundles,
    load_contract_directory,
)
from semantic_rails.contracts.generation import PUBLIC_SCHEMA_BASE, generated_artifacts
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.dbt_warehouse import ORDER_COUNT_QUERY, write_orders_package

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
JAFFLE_SHOP = REPO_ROOT / "configs" / "semantic_rails" / "jaffle_shop"


@pytest.fixture
def typed_contract_project(tmp_path: Path) -> Path:
    project = tmp_path / "typed_contract"
    project.mkdir()
    raw = {
        "schema_version": 1,
        "package": {
            "id": "typed_contract",
            "warehouse": "duckdb",
            "default_db": "warehouse.duckdb",
            "seed": {"kind": "external"},
        },
        "graph": {"entities": {"event": {"key": "event_id", "model": "events"}}},
        "models": {
            "events": {
                "relation": "analytics.fct_events",
                "grain": ["event_id"],
                "entities": {"event": {}},
                "times": {"occurred_at": {"kind": "timestamp"}},
                "dimensions": {
                    "tenant_id": {"kind": "categorical"},
                    "local_time": {"kind": "timestamp"},
                },
            }
        },
    }
    (project / "package.yml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return project


@pytest.mark.parametrize("legacy", [False, True], ids=["models", "typed-config"])
@pytest.mark.parametrize("relation_kind", ["table", "view"])
@pytest.mark.parametrize("qualified_catalog", [False, True])
@pytest.mark.parametrize("shadow", ["", "duckdb_columns", "current_database", "lower"])
def test_export_semantic_contract_reports_physical_column_types(
    typed_contract_project: Path,
    legacy: bool,
    relation_kind: str,
    qualified_catalog: bool,
    shadow: str,
) -> None:
    from semantic_rails.config import LoadedPackageSnapshot, load_package_snapshot

    database = typed_contract_project / "warehouse.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE SCHEMA analytics")
        connection.execute(
            "CREATE TABLE analytics.source_events "
            "(event_id BIGINT, tenant_id UUID, occurred_at TIMESTAMPTZ, local_time TIMESTAMP)"
        )
        connection.execute(
            f"CREATE {relation_kind.upper()} analytics.fct_events AS "
            "SELECT * FROM analytics.source_events"
        )
        if shadow:
            arguments = "x" if shadow == "lower" else ""
            body = (
                "TABLE SELECT 'file_macro' AS sentinel"
                if shadow == "duckdb_columns"
                else "'file_macro'"
            )
            connection.execute(f"CREATE MACRO {shadow}({arguments}) AS {body}")
    if qualified_catalog:
        package_file = typed_contract_project / "package.yml"
        raw = yaml.safe_load(package_file.read_text())
        raw["models"]["events"]["relation"] = "warehouse.analytics.fct_events"
        package_file.write_text(yaml.safe_dump(raw))
    snapshot = load_package_snapshot(typed_contract_project)
    if legacy:
        snapshot = LoadedPackageSnapshot.from_config(
            snapshot.config, source_path=snapshot.source_path
        )
    before = database.read_bytes()
    if shadow:
        with pytest.raises(SemanticLayerError) as exc:
            export_semantic_contract(snapshot)
        assert exc.value.code == "INVALID_CONFIG"
        assert exc.value.details == {"reason": "duckdb_builtin_macro_collision", "macros": [shadow]}
        assert database.read_bytes() == before
        return
    payload = export_semantic_contract(snapshot)
    resource = payload["semantic"]["packages"][0]["resources"][0]
    columns = {column["name"]: column for column in resource["columns"]}
    assert columns["occurred_at"]["data_type"] == "timestamp_tz"
    assert columns["tenant_id"]["data_type"] == "uuid"
    assert columns["event_id"]["data_type"] == "bigint"
    assert columns["local_time"]["data_type"] == "timestamp"
    assert resource["relation"].endswith("analytics.fct_events")
    assert database.read_bytes() == before
    assert payload["semantic"]["packages"][0]["semantic_hash"] == snapshot.semantic_fingerprint
    _jsonschema().Draft202012Validator(load_contract("semantic_contract.v1.json")).validate(payload)


@pytest.mark.parametrize(
    ("catalog_relation", "authored_relation", "catalog_columns", "relation_kind"),
    [
        ("JAFFLE_ORDER", "jaffle_order", "CUSTOMER_ID UUID, ORDERED_AT TIMESTAMPTZ", "table"),
        ("jaffle_order", "jaffle_order", "CUSTOMER_ID UUID, ORDERED_AT TIMESTAMPTZ", "table"),
        (
            "Analytics.Fct_Events",
            "analytics.fct_events",
            "customer_id UUID, ordered_at TIMESTAMPTZ",
            "view",
        ),
        ("jaffle_order", "jaffle_order", "customer_id UUID, ordered_at TIMESTAMPTZ", "table"),
        (
            "jaffle_order",
            "WAREHOUSE.MAIN.JAFFLE_ORDER",
            "customer_id UUID, ordered_at TIMESTAMPTZ",
            "table",
        ),
        ("source_events", "missing_events", "customer_id UUID, ordered_at TIMESTAMPTZ", "table"),
    ],
    ids=[
        "table-and-columns",
        "columns-only",
        "schema-view",
        "one-part",
        "catalog-qualified",
        "missing-relation",
    ],
)
def test_export_semantic_contract_matches_catalog_identifiers(
    typed_contract_project: Path,
    catalog_relation: str,
    authored_relation: str,
    catalog_columns: str,
    relation_kind: str,
) -> None:
    package_file = typed_contract_project / "package.yml"
    raw = yaml.safe_load(package_file.read_text())
    raw["graph"]["entities"]["event"]["key"] = "customer_id"
    raw["models"]["events"].update(
        relation=authored_relation,
        grain=["customer_id"],
        times={"ordered_at": {"kind": "timestamp"}},
        dimensions={"customer_id": {"kind": "categorical"}},
    )
    package_file.write_text(yaml.safe_dump(raw))
    database = typed_contract_project / "warehouse.duckdb"
    with duckdb.connect(str(database)) as connection:
        if "." in catalog_relation:
            connection.execute("CREATE SCHEMA Analytics")
        connection.execute(f"CREATE TABLE source_columns ({catalog_columns})")
        connection.execute(
            f"CREATE {relation_kind.upper()} {catalog_relation} AS SELECT * FROM source_columns"
        )
    before = database.read_bytes()
    files_before = set(typed_contract_project.iterdir())
    payload = export_semantic_contract(typed_contract_project)
    columns = {
        column["name"]: column["data_type"]
        for column in payload["semantic"]["packages"][0]["resources"][0]["columns"]
    }
    assert columns == (
        {"customer_id": "string", "ordered_at": "timestamp"}
        if authored_relation == "missing_events"
        else {"customer_id": "uuid", "ordered_at": "timestamp_tz"}
    )
    assert database.read_bytes() == before
    assert set(typed_contract_project.iterdir()) == files_before


@pytest.mark.parametrize("ambiguity", ["column", "relation"])
def test_export_semantic_contract_keeps_hints_for_ambiguous_catalog(
    typed_contract_project: Path, monkeypatch: pytest.MonkeyPatch, ambiguity: str
) -> None:
    database = typed_contract_project / "warehouse.duckdb"
    with duckdb.connect(str(database)):
        pass
    rows = [
        {
            "database_name": "warehouse",
            "schema_name": "analytics",
            "table_name": "fct_events",
            "column_name": "occurred_at",
            "data_type": "TIMESTAMP WITH TIME ZONE",
        },
        {
            "database_name": "warehouse",
            "schema_name": "analytics",
            "table_name": "fct_events",
            "column_name": "tenant_id",
            "data_type": "UUID",
        },
    ]
    duplicate = dict(rows[0])
    duplicate["column_name" if ambiguity == "column" else "schema_name"] = (
        "OCCURRED_AT" if ambiguity == "column" else "Analytics"
    )
    rows.append(duplicate)
    monkeypatch.setattr(
        "semantic_rails.architect_introspection.open_duckdb",
        lambda _: nullcontext(SimpleNamespace(rows=lambda *_: rows)),
    )
    payload = export_semantic_contract(typed_contract_project)
    columns = {
        column["name"]: column
        for column in payload["semantic"]["packages"][0]["resources"][0]["columns"]
    }
    assert columns["occurred_at"]["data_type"] == "timestamp"
    assert columns["tenant_id"]["data_type"] == ("uuid" if ambiguity == "column" else "string")


def test_export_semantic_contract_does_not_create_missing_database(
    typed_contract_project: Path,
) -> None:
    payload = export_semantic_contract(typed_contract_project)
    columns = {
        column["name"]: column
        for column in payload["semantic"]["packages"][0]["resources"][0]["columns"]
    }
    assert columns["occurred_at"]["data_type"] == "timestamp"
    assert columns["tenant_id"]["data_type"] == "string"
    assert not (typed_contract_project / "warehouse.duckdb").exists()


def test_export_semantic_contract_refuses_unreadable_existing_database(
    typed_contract_project: Path,
) -> None:
    database = typed_contract_project / "warehouse.duckdb"
    database.write_bytes(b"invalid database")
    with pytest.raises(SemanticLayerError) as exc:
        export_semantic_contract(typed_contract_project)
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["reason"] == "database_unreadable"
    assert database.read_bytes() == b"invalid database"


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
        "query_ir.v1.json",
        "semantic_contract.v1.json",
        "metric_portability.v1.json",
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
                            "order_month": {"column": "ordered_at"},
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
    assert "coalesce" not in names
    assert "orders" not in names
    assert all(not name.startswith(("{", "[")) for name in names)


@pytest.mark.parametrize(
    ("section", "label"), [("dimensions", "dimension"), ("times", "times entry")]
)
def test_contract_export_refuses_ignored_expression_keys(
    typed_contract_project: Path, section: str, label: str
) -> None:
    source = typed_contract_project / "package.yml"
    raw = yaml.safe_load(source.read_text())
    raw["models"]["events"][section]["authored_time"] = {
        "expr": {"kind": "column", "column": "occurred_at"}
    }
    source.write_text(yaml.safe_dump(raw))
    with pytest.raises(SemanticLayerError) as raised:
        export_semantic_contract(typed_contract_project)
    assert raised.value.code == "INVALID_CONFIG"
    assert f"{label} 'authored_time' has unknown key 'expr'" in str(raised.value)


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
        "TrustedAttributes",
        "PolicyContextResolver",
        "AuditSink",
        "ConnectionCredentialProvider",
        "WarehouseAdapter",
        "create_warehouse_adapter",
        "warehouse_connector",
        "normalize_connection_options",
        "PackageReference",
        "validate_runtime_package",
        "run_examples_report",
        "run_package_tests_report",
    }
    assert required.issubset(set(embedding.__all__))
    assert all(hasattr(embedding, name) for name in required)


def test_embedding_facade_runs_passing_and_failing_package_examples(tmp_path: Path) -> None:
    import semantic_rails.embedding as embedding

    package = write_orders_package(tmp_path, schema="", with_customers=False)
    examples_dir = package / "examples"
    examples_dir.mkdir()
    examples = {
        name: {
            "query": ORDER_COUNT_QUERY,
            "expected_shape": {"min_rows": min_rows, "max_rows": min_rows},
        }
        for name, min_rows in [("passing", 1), ("failing", 2)]
    }
    (examples_dir / "orders.yml").write_text(
        yaml.safe_dump({"examples": examples}), encoding="utf-8"
    )

    report = embedding.run_examples_report(embedding.resolve_package_reference(path=str(package)))

    assert report["ok"] is False
    assert report["summary"] == {"examples_total": 2, "passed": 1, "failed": 1}
    assert {row["id"]: row["ok"] for row in report["examples"]} == {
        "passing": True,
        "failing": False,
    }


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
    current["query_mcp.v2.json"]["tools"] = [
        tool for tool in current["query_mcp.v2.json"]["tools"] if tool["name"] != "execute"
    ]
    current["semantic_contract.v1.json"]["$defs"]["SemanticPackage"]["required"].append(
        "new_required"
    )

    report = compare_contract_bundles(baseline, current)
    assert report["ok"] is False
    kinds = {change["kind"] for change in report["breaking_changes"]}
    assert {"operation_removed", "tool_removed", "required_added"}.issubset(kinds)


@pytest.mark.parametrize("name", ["query_mcp.v1.json", "query_ir.preview.v2.json"])
def test_compatibility_checker_reports_a_retired_contract_as_removed(
    tmp_path: Path, name: str
) -> None:
    # A baseline containing a retired contract must not pass silently.
    (tmp_path / name).write_text(json.dumps({"tools": []}), encoding="utf-8")
    baseline = load_contract_directory(tmp_path)
    assert set(baseline) == {name}
    removed = compare_contract_bundles(baseline, {})["breaking_changes"]
    assert [(change["artifact"], change["kind"]) for change in removed] == [
        (name, "artifact_removed")
    ]


def test_export_contract_wraps_unexpected_loader_shape_errors(monkeypatch) -> None:
    import semantic_rails.contracts.producer as producer

    def broken_loader(_path):
        raise KeyError("raw-internal-reference")

    monkeypatch.setattr(producer, "load_package_snapshot", broken_loader)
    with pytest.raises(SemanticLayerError) as exc:
        producer.export_semantic_contract(JAFFLE_SHOP)

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"exception_type": "KeyError"}
    assert "raw-internal-reference" not in str(exc.value)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("snapshot_input", [False, True])
def test_diff_semantic_contract_round_trip_without_database_access(
    typed_contract_project, monkeypatch, wrapped, snapshot_input
) -> None:
    from semantic_rails.embedding import diff_semantic_contract as facade_diff
    from semantic_rails.embedding import load_package_snapshot

    (typed_contract_project / "warehouse.duckdb").write_bytes(b"not a database")

    def forbidden(*args, **kwargs):
        pytest.fail("static contract comparison must never open a warehouse")

    monkeypatch.setattr(duckdb, "connect", forbidden)
    monkeypatch.setattr("semantic_rails.contracts.producer._apply_physical_column_types", forbidden)
    package = (
        load_package_snapshot(typed_contract_project) if snapshot_input else typed_contract_project
    )
    committed = export_semantic_contract(package, physical_types=False)
    before = deepcopy(committed)
    if wrapped:
        committed = {"semantic_rails_contracts": committed}
    result = facade_diff(package, committed)
    assert facade_diff is diff_semantic_contract
    assert result["ok"] is True
    assert result["package_id"] == "typed_contract"
    assert result["covered_models"] == ["events"]
    assert result["drift"] == []
    assert (committed["semantic_rails_contracts"] if wrapped else committed) == before


@pytest.mark.parametrize(
    ("change", "code", "fails"),
    [
        ("missing-column", "CONTRACT_COLUMN_MISSING", True),
        ("renamed-column", "CONTRACT_COLUMN_MISSING", True),
        ("relation", "RELATION_CHANGED", True),
        ("type-family", "COLUMN_TYPE_CHANGED", True),
        ("uncovered-model", "CONTRACT_MODEL_NOT_COVERED", False),
        ("unused-column", "COLUMN_UNUSED", False),
        ("unused-model", "COLUMN_UNUSED", False),
        ("unknown-type", "TYPE_NOT_COMPARED", False),
        ("missing-type", "TYPE_NOT_COMPARED", False),
        ("missing-hint", "TYPE_NOT_COMPARED", False),
    ],
)
def test_diff_semantic_contract_classifies_changes(typed_contract_project, change, code, fails):
    committed = export_semantic_contract(typed_contract_project, physical_types=False)
    resources = committed["semantic"]["packages"][0]["resources"]
    model = resources[0]
    column = next(row for row in model["columns"] if row["name"] == "tenant_id")
    if change == "missing-column":
        model["columns"].remove(column)
    elif change == "renamed-column":
        column["name"] = "previous_tenant"
    elif change == "relation":
        model["relation"] = "analytics.old_events"
    elif change == "type-family":
        column["data_type"] = "decimal(18, 2)"
    elif change == "uncovered-model":
        resources.clear()
    elif change == "unused-column":
        model["columns"].append({"name": "obsolete", "required_by": []})
    elif change == "unused-model":
        resources.append({"semantic_model_id": "obsolete", "columns": []})
    elif change == "unknown-type":
        column["data_type"] = "json"
    elif change == "missing-type":
        column.pop("data_type")
    else:
        next(row for row in model["columns"] if row["name"] == "event_id")["data_type"] = "int"
    result = diff_semantic_contract(typed_contract_project, committed)
    assert result["ok"] is not fails
    rows = result["drift"] if fails else result["notes"]
    row = next(row for row in rows if row["code"] == code)
    assert set(row) == {
        "code",
        "semantic_model_id",
        "relation",
        "column",
        "required_by",
        "contract_column",
        "message",
    }
    if change in {"missing-column", "renamed-column"}:
        assert row["column"] == "tenant_id"
        assert row["required_by"] == column["required_by"]
    if change == "renamed-column":
        assert row["contract_column"] == "previous_tenant"
        assert "previous_tenant" in row["message"]
    if change == "uncovered-model":
        assert result["covered_models"] == []


@pytest.mark.parametrize(
    ("hint", "physical"),
    [
        ("categorical", kind)
        for kind in ["string", "varchar(100)", "text", "character varying(9)", "char", "uuid"]
    ]
    + [
        ("number", kind)
        for kind in [
            "integer",
            "int",
            "bigint",
            "smallint",
            "hugeint",
            "number",
            "numeric(9,2)",
            "decimal",
            "double",
            "float",
            "real",
        ]
    ]
    + [("boolean", kind) for kind in ["boolean", "bool"]]
    + [
        ("timestamp", kind)
        for kind in [
            "date",
            "datetime",
            "timestamp",
            "timestamp_tz",
            "timestamp_ns",
            "timestamptz",
            "timestamp(6) with time zone",
            "timestamp without time zone",
        ]
    ],
)
def test_diff_semantic_contract_type_families(typed_contract_project, hint, physical):
    package_file = typed_contract_project / "package.yml"
    raw = yaml.safe_load(package_file.read_text())
    raw["models"]["events"]["dimensions"]["tenant_id"]["kind"] = hint
    package_file.write_text(yaml.safe_dump(raw))
    committed = export_semantic_contract(typed_contract_project, physical_types=False)
    column = next(
        row
        for row in committed["semantic"]["packages"][0]["resources"][0]["columns"]
        if row["name"] == "tenant_id"
    )
    column["data_type"] = physical.upper()
    assert diff_semantic_contract(typed_contract_project, committed)["drift"] == []


def test_diff_semantic_contract_ignores_metadata_and_identifier_case(typed_contract_project):
    committed = export_semantic_contract(typed_contract_project, physical_types=False)
    committed["binding"] = "never inspected"
    committed["semantic"]["producer"] = None
    package = committed["semantic"]["packages"][0]
    package.update(semantic_hash="changed", namespace="other", package_schema_version=99)
    package["resources"][0]["relation"] = "ANALYTICS.FCT_EVENTS"
    for column in package["resources"][0]["columns"]:
        column["name"] = column["name"].upper()
        column["required_by"] = ["old.object"]
    assert diff_semantic_contract(typed_contract_project, committed)["drift"] == []
    package["resources"][0].pop("relation")
    assert diff_semantic_contract(typed_contract_project, committed)["drift"] == []


@pytest.mark.parametrize("duplicate", ["model", "column"])
def test_diff_semantic_contract_validates_exported_rows_too(
    typed_contract_project, monkeypatch, duplicate
):
    committed = export_semantic_contract(typed_contract_project, physical_types=False)
    exported = deepcopy(committed)
    models = exported["semantic"]["packages"][0]["resources"]
    if duplicate == "model":
        models.append(deepcopy(models[0]))
    else:
        models[0]["columns"].append(deepcopy(models[0]["columns"][0]))
    monkeypatch.setattr(
        "semantic_rails.contracts.drift.export_semantic_contract", lambda *a, **k: exported
    )
    with pytest.raises(SemanticLayerError) as exc:
        diff_semantic_contract(typed_contract_project, committed)
    assert exc.value.code == "INVALID_CONTRACT"
    assert exc.value.details["reason"] == f"duplicate_{duplicate}"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("root", "not_mapping"),
        ("version", "unsupported_version"),
        ("boolean-version", "unsupported_version"),
        ("legacy", "invalid_semantic"),
        ("nested-wrapper", "unsupported_version"),
        ("semantic", "invalid_semantic"),
        ("packages", "invalid_packages"),
        ("no-package", "package_not_found"),
        ("package-row", "invalid_package"),
        ("package-id", "invalid_package"),
        ("duplicate-package", "duplicate_package"),
        ("resources", "invalid_resources"),
        ("model-row", "invalid_model"),
        ("model-id", "invalid_model"),
        ("relation", "invalid_model"),
        ("duplicate-model", "duplicate_model"),
        ("columns", "invalid_columns"),
        ("column-row", "invalid_column"),
        ("column-name", "invalid_column"),
        ("column-type", "invalid_column"),
        ("required-by", "invalid_column"),
        ("duplicate-column", "duplicate_column"),
        ("other-package", "invalid_resources"),
    ],
)
def test_diff_semantic_contract_refuses_malformed_input(typed_contract_project, change, reason):
    committed = export_semantic_contract(typed_contract_project, physical_types=False)
    semantic = committed["semantic"]
    package = semantic["packages"][0]
    model = package["resources"][0]
    column = model["columns"][0]
    if change == "root":
        committed = []
    elif change == "version":
        committed["contract_format_version"] = 2
    elif change == "boolean-version":
        committed["contract_format_version"] = True
    elif change == "legacy":
        committed = {"contract_format_version": 1, "packages": []}
    elif change == "nested-wrapper":
        committed = {"semantic_rails_contracts": {"semantic_rails_contracts": committed}}
    elif change == "semantic":
        committed["semantic"] = []
    elif change == "packages":
        semantic["packages"] = {}
    elif change == "no-package":
        package["package_id"] = "other"
    elif change == "package-row":
        semantic["packages"] = [None]
    elif change == "package-id":
        package["package_id"] = ""
    elif change == "duplicate-package":
        semantic["packages"].append(deepcopy(package))
    elif change == "resources":
        package["resources"] = {}
    elif change == "model-row":
        package["resources"] = [None]
    elif change == "model-id":
        model["semantic_model_id"] = ""
    elif change == "relation":
        model["relation"] = []
    elif change == "duplicate-model":
        package["resources"].append(deepcopy(model))
    elif change == "columns":
        model["columns"] = {}
    elif change == "column-row":
        model["columns"] = [None]
    elif change == "column-name":
        column["name"] = ""
    elif change == "column-type":
        column["data_type"] = []
    elif change == "required-by":
        column["required_by"] = "object"
    elif change == "duplicate-column":
        model["columns"].append({**column, "name": column["name"].upper()})
    else:
        semantic["packages"].append({"package_id": "other", "resources": None})
    with pytest.raises(SemanticLayerError) as exc:
        diff_semantic_contract(typed_contract_project, committed)
    assert exc.value.code == "INVALID_CONTRACT"
    assert exc.value.details["reason"] == reason
