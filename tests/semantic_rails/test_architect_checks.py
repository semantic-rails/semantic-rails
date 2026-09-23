"""Examples, package tests and query previews through the Architect."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import project_revision
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import _normalize_rows
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

COUNTRY = "dimension.shop_customer_customer_country"


def _query(measure: str, *, dimension: str = COUNTRY, limit: int = 10) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": {"measure": f"measure.shop.{measure}"}, "as": measure}],
        "group_by": [dimension],
        "order_by": [{"field": dimension}],
        "limit": limit,
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders and customers over the dbt warehouse, plus a tax measure with cents."""
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=True)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    mutation = ArchitectProject(package, workspace_root=tmp_path).upsert_model(
        model_id="orders",
        entity_key="order",
        relation="main_marts.fct_orders",
        primary_key=["order_id"],
        measures={
            "tax": {
                "label": "Tax",
                "kind": "aggregate",
                "expr": "order_total * 0.15",
                "default_agg": "sum",
                "accumulation": {"kind": "flow"},
                "value_type": "currency",
            }
        },
    )
    assert mutation.report["ok"] is True, mutation.report
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def test_mcp_session_previews_values_then_writes_a_snapshot_test_and_an_example(
    workspace: Path,
) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            preview_hints = tools["preview_query"].annotations
            assert preview_hints is not None and preview_hints.readOnlyHint is True

            async def call(name: str, **arguments: Any) -> dict[str, Any]:
                return dict((await session.call_tool(name, arguments)).structuredContent or {})

            preview = await call("preview_query", project_path="shop", query=_query("tax"))
            test = {
                "project_path": "shop",
                "test_key": "tax_by_country",
                "spec": {"kind": "query_matches_snapshot", "query": _query("tax")},
                "capture_snapshot": True,
            }
            revision = project_revision(workspace / "shop")
            dry = await call(
                "upsert_test",
                **test,
                expected_revision=revision,
                idempotency_key="t0",
                dry_run=True,
            )
            written = await call(
                "upsert_test", **test, expected_revision=revision, idempotency_key="t1"
            )
            example = await call(
                "upsert_example",
                project_path="shop",
                example_key="orders_by_country",
                spec={
                    "question": "How many orders came from each country?",
                    "query": _query("order_count"),
                    "expected_shape": {"columns": [COUNTRY, "order_count"], "min_rows": 3},
                },
                expected_revision=written["revision"],
                idempotency_key="e1",
            )
            tests = await call("validate_project", project_path="shop", mode="tests")
            examples = await call("validate_project", project_path="shop", mode="examples")
            return [preview, dry, written, example, tests, examples]

    preview, dry, written, example, tests, examples = asyncio.run(run())

    assert preview["ok"] is True, preview
    assert preview["rows"] == [
        {COUNTRY: "GB", "tax": 183},
        {COUNTRY: "NL", "tax": 12.75},
        {COUNTRY: "US", "tax": 309.6},
    ]
    assert preview["truncated"] is False
    assert dry["ok"] is True and dry["changed_files"] == ["tests/core.yml"]
    assert written["ok"] is True and example["ok"] is True, (written, example)
    snapshot = _yaml(workspace / "shop" / "tests" / "core.yml")["tests"]["tax_by_country"]
    assert snapshot["expected_rows"] == preview["rows"]
    assert tests["ok"] is True, tests  # DECIMAL cents match the YAML snapshot
    assert examples["ok"] is True, examples


def test_snapshot_rows_compare_numbers_by_value() -> None:
    warehouse = [{"tax": Decimal("12.7500"), "orders": 3, "total": Decimal("262.00")}]
    authored = [{"tax": 12.75, "orders": 3.0, "total": 262}]

    assert _normalize_rows(warehouse) == _normalize_rows(authored)
    assert _normalize_rows([{"flag": True}])[0]["flag"] is True  # bools stay bools


def test_preview_query_caps_rows_and_reports_truncation(workspace: Path) -> None:
    project = _project(workspace)

    capped = project.preview_query(_query("order_count"), max_rows=2)
    ceiling = project.preview_query(_query("order_count", limit=1000), max_rows=5000)

    assert capped["row_count"] == 2 and capped["truncated"] is True
    assert capped["columns"] == [COUNTRY, "order_count"]
    assert ceiling["row_count"] == 3 and ceiling["truncated"] is False


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"kind": "query_is_fast", "query": _query("tax")}, "test kind must be one of"),
        ({"kind": "query_returns_columns", "query": _query("tax")}, "needs columns"),
        ({"kind": "query_row_count_bounds", "query": _query("tax")}, "min_rows or max_rows"),
        (
            {"kind": "query_returns_columns", "query": _query("refunds"), "columns": ["x"]},
            "the query does not compile",
        ),
        (
            {"kind": "validate_fails_with_code", "query": _query("tax"), "code": "X"},
            "expects the query to fail with X, but it is valid",
        ),
        (
            {"kind": "metric_equals_query", "query": _query("tax")},
            "needs expected_query",
        ),
    ],
)
def test_tests_the_package_cannot_run_are_refused(
    workspace: Path, spec: dict[str, Any], message: str
) -> None:
    before = project_revision(workspace / "shop")

    with pytest.raises(SemanticLayerError, match=message):
        _project(workspace).upsert_test(test_key="t", spec=spec)

    assert project_revision(workspace / "shop") == before


def test_a_query_that_must_fail_is_checked_for_its_code(workspace: Path) -> None:
    query = _query("tax")
    query["select"].append({"expression": {"measure": "measure.shop.order_count"}, "as": "tax"})

    mutation = _project(workspace).upsert_test(
        test_key="duplicate_alias",
        spec={"kind": "validate_fails_with_code", "query": query, "code": "DUPLICATE_OUTPUT_ALIAS"},
    )

    assert mutation.report["ok"] is True, mutation.report


def test_examples_need_a_query_that_compiles(workspace: Path) -> None:
    project = _project(workspace)

    # The runner doesn't need a question, and create_project's starter example has none.
    unasked = project.upsert_example(example_key="tax_only", spec={"query": _query("tax")})
    assert unasked.report["ok"] is True, unasked.report
    with pytest.raises(SemanticLayerError, match="does not compile"):
        project.upsert_example(
            example_key="e", spec={"question": "Refunds?", "query": _query("refunds")}
        )
    with pytest.raises(SemanticLayerError, match="capture_snapshot applies"):
        project.upsert_test(
            test_key="t",
            spec={"kind": "query_returns_columns", "query": _query("tax"), "columns": ["x"]},
            capture_snapshot=True,
        )


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            {"kind": "query_row_count_bounds", "query": _query("tax"), "min_rows": "many"},
            "min_rows must be a non-negative integer",
        ),
        (
            {
                "kind": "query_row_count_bounds",
                "query": _query("tax"),
                "min_rows": 3,
                "max_rows": 1,
            },
            "min_rows must not exceed max_rows",
        ),
        (
            {"kind": "query_matches_snapshot", "query": _query("tax"), "expected_rows": "abc"},
            "expected_rows must be a list of rows",
        ),
        (
            {
                "kind": "metric_equals_query",
                "query": _query("tax"),
                "metric_query": None,
                "expected_query": _query("tax"),
            },
            "needs metric_query",
        ),
        (
            {"kind": "query_returns_columns", "query": _query("tax"), "columns": "tax"},
            "columns must be a list of names",
        ),
    ],
)
def test_fields_the_test_runner_would_crash_on_are_refused(
    workspace: Path, spec: dict[str, Any], message: str
) -> None:
    with pytest.raises(SemanticLayerError, match=message):
        _project(workspace).upsert_test(test_key="t", spec=spec)


def test_an_empty_snapshot_is_a_valid_test(workspace: Path) -> None:
    query = _query("tax")
    query["where"] = [{"field": COUNTRY, "op": "=", "value": "XX"}]

    written = _project(workspace).upsert_test(
        test_key="no_rows",
        spec={"kind": "query_matches_snapshot", "query": query},
        capture_snapshot=True,
    )

    assert written.report["ok"] is True, written.report
    snapshot = _yaml(workspace / "shop" / "tests" / "core.yml")["tests"]["no_rows"]
    assert snapshot["expected_rows"] == []


def test_snapshots_hold_only_values_yaml_gives_back() -> None:
    import uuid
    from datetime import time

    from semantic_rails.architect_service import _snapshot_rows

    assert _snapshot_rows([{"tags": ["a", "b"], "n": Decimal("2.50")}]) == [
        {"tags": ["a", "b"], "n": 2.5}
    ]
    for value in (uuid.uuid4(), time(10, 1), float("nan")):
        with pytest.raises(SemanticLayerError, match="snapshot"):
            _snapshot_rows([{"value": value}])
    with pytest.raises(SemanticLayerError, match="don't read back"):
        _snapshot_rows([{"exact": Decimal("12345678.123456789012")}])


def test_numbers_compare_by_value_to_the_last_digit() -> None:
    assert _normalize_rows([{"x": 1.5}]) != _normalize_rows([{"x": 2.5}])
    assert _normalize_rows([{"x": Decimal("12345678.123456789012")}]) != _normalize_rows(
        [{"x": Decimal("12345678.123456789013")}]
    )
    assert _normalize_rows([{"x": Decimal("0.10")}]) == _normalize_rows([{"x": 0.1}])


def test_preview_limits_are_honoured_and_empty_results_name_their_columns(
    workspace: Path,
) -> None:
    project = _project(workspace)
    none = _query("order_count", limit=0)
    nowhere = _query("order_count")
    nowhere["where"] = [{"field": COUNTRY, "op": "=", "value": "XX"}]

    assert project.preview_query(none)["row_count"] == 0
    empty = project.preview_query(nowhere)
    assert empty["rows"] == [] and empty["columns"] == [COUNTRY, "order_count"]
    with pytest.raises(SemanticLayerError, match="limit must be a non-negative integer"):
        project.preview_query({**_query("order_count"), "limit": "2"})


def test_specs_merge_unless_replaced_and_stay_in_their_file(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_example(
        example_key="tax",
        spec={
            "question": "Tax by country",
            "query": _query("tax"),
            "expected_shape": {"min_rows": 1},
        },
        file_name="finance.yml",
    )

    project.upsert_example(example_key="tax", spec={"question": "Tax per country"})
    merged = _yaml(workspace / "shop" / "examples" / "finance.yml")["examples"]["tax"]
    project.upsert_example(
        example_key="tax",
        spec={"question": "Tax per country", "query": _query("tax")},
        replace=True,
    )
    replaced = _yaml(workspace / "shop" / "examples" / "finance.yml")["examples"]["tax"]

    assert merged["question"] == "Tax per country" and merged["expected_shape"] == {"min_rows": 1}
    assert "expected_shape" not in replaced
    assert not (workspace / "shop" / "examples" / "core.yml").exists()


def test_upsert_test_dry_run_apply_and_undo(workspace: Path) -> None:
    project = _project(workspace)
    before = project_revision(workspace / "shop")
    spec = {"kind": "query_row_count_bounds", "query": _query("tax"), "min_rows": 3}

    preview = project.upsert_test(test_key="three_countries", spec=spec, dry_run=True)
    assert preview.report["ok"] is True, preview.report
    assert project_revision(workspace / "shop") == before

    applied = project.upsert_test(test_key="three_countries", spec=spec)
    assert applied.report["revision"] == preview.report["proposed_revision"]

    assert applied.undo()["ok"] is True
    assert project_revision(workspace / "shop") == before
    assert not (workspace / "shop" / "tests" / "core.yml").exists()
