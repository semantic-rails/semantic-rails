"""Examples, package tests and query previews through the Architect."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import project_revision
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

COUNTRY = "dimension.shop_customer_customer_country"


def _query(expression: dict[str, Any], alias: str = "value", **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": expression, "as": alias}],
        "group_by": [COUNTRY],
        **extra,
    }


ORDERS = _query({"measure": "measure.shop.order_count"}, "orders")
UNKNOWN = _query({"measure": "measure.shop.refunds"})


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders and customers over the dbt-shaped DuckDB marts."""
    package = write_orders_package(tmp_path, seed={"kind": "external"})
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def test_mcp_session_previews_rows_and_writes_tests_and_an_example_that_pass(
    workspace: Path,
) -> None:
    """The value-free test kinds a package author needs, then the runner's verdict."""
    tests = {
        "columns": {"kind": "query_returns_columns", "query": ORDERS, "columns": [COUNTRY]},
        "three_countries": {
            "kind": "query_row_count_bounds",
            "query": ORDERS,
            "min_rows": 3,
            "max_rows": 3,
        },
        "revenue_sums_order_totals": {
            "kind": "metric_equals_query",
            "metric_query": _query({"metric": "metric.shop.revenue"}),
            "expected_query": _query({"measure": "measure.shop.order_total", "aggregation": "sum"}),
        },
        "no_unknown_measures": {
            "kind": "validate_fails_with_code",
            "query": UNKNOWN,
            "code": "OBJECT_NOT_FOUND",
        },
    }
    server = create_architect_mcp_server(workspace_root=workspace)

    async def run() -> dict[str, Any]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}

            async def call(name: str, **arguments: Any) -> dict[str, Any]:
                return dict((await session.call_tool(name, arguments)).structuredContent or {})

            revision = project_revision(workspace / "shop")
            written = []
            for key, spec in tests.items():
                result = await call(
                    "upsert_test",
                    project_path="shop",
                    test_key=key,
                    spec=spec,
                    file_name="regression.yml",
                    expected_revision=revision,
                    idempotency_key=key,
                )
                written.append(result)
                revision = result["revision"]
            example = await call(
                "upsert_example",
                project_path="shop",
                example_key="orders_by_country",
                spec={
                    "question": "How many orders came from each country?",
                    "query": ORDERS,
                    "expected_shape": {"columns": [COUNTRY, "orders"], "min_rows": 3},
                },
                file_name="questions.yml",
                expected_revision=revision,
                idempotency_key="example",
            )
            too_many = await session.call_tool(
                "preview_query", {"project_path": "shop", "query": ORDERS, "max_rows": 201}
            )
            return {
                "annotations": tools["preview_query"].annotations,
                "written": written,
                "example": example,
                "preview": await call("preview_query", project_path="shop", query=ORDERS),
                "capped": await call(
                    "preview_query", project_path="shop", query=ORDERS, max_rows=2
                ),
                "refused": await call("preview_query", project_path="shop", query=UNKNOWN),
                "too_many": too_many.isError,
                "tests": await call("validate_project", project_path="shop", mode="tests"),
                "examples": await call("validate_project", project_path="shop", mode="examples"),
            }

    out = asyncio.run(run())

    assert all(result["ok"] for result in out["written"]), out["written"]
    assert out["example"]["ok"] is True, out["example"]
    assert set(_yaml(workspace / "shop" / "tests" / "regression.yml")["tests"]) == set(tests)
    assert out["tests"]["ok"] is True and out["tests"]["summary"]["passed"] == 4, out["tests"]
    assert out["examples"]["ok"] is True and out["examples"]["summary"]["passed"] == 1
    assert out["annotations"].readOnlyHint is True and out["annotations"].openWorldHint is True
    assert sorted(row[COUNTRY] for row in out["preview"]["rows"]) == ["GB", "NL", "US"]
    assert out["preview"]["truncated"] is False
    assert out["capped"]["row_count"] == 2 and out["capped"]["truncated"] is True
    assert out["capped"]["total_row_count"] == 3
    assert out["refused"]["ok"] is False
    assert out["refused"]["error"]["code"] == "OBJECT_NOT_FOUND"
    assert out["too_many"] is True


@pytest.mark.parametrize(
    ("kind", "spec", "message"),
    [
        ("test", {"kind": "query_is_fast", "query": ORDERS}, "kind must be one of"),
        ("test", {"kind": "query_returns_columns", "query": ORDERS}, "needs columns"),
        (
            "test",
            {"kind": "query_returns_columns", "query": ORDERS, "columns": COUNTRY},
            "columns must be a list of column names",
        ),
        ("test", {"kind": "query_row_count_bounds", "query": ORDERS}, "min_rows or max_rows"),
        (
            "test",
            {"kind": "query_row_count_bounds", "query": ORDERS, "min_rows": "3"},
            "min_rows must be a non-negative integer",
        ),
        (
            "test",
            {"kind": "query_matches_snapshot", "query": ORDERS, "expected_rows": "GB"},
            "expected_rows must be a list of rows",
        ),
        ("test", {"kind": "explain_contains", "query": ORDERS, "text": " "}, "text must be"),
        (
            "test",
            {"kind": "metric_equals_query", "metric_query": None, "expected_query": ORDERS},
            "needs metric_query",
        ),
        ("test", {"kind": "metric_equals_query", "query": ORDERS}, "needs expected_query"),
        (
            "test",
            {"kind": "query_returns_columns", "query": UNKNOWN, "columns": ["value"]},
            "query does not validate \\(OBJECT_NOT_FOUND",
        ),
        (
            "test",
            {"kind": "validate_fails_with_code", "query": ORDERS, "code": "OBJECT_NOT_FOUND"},
            "must fail with OBJECT_NOT_FOUND, but it is valid",
        ),
        (
            "test",
            {"kind": "validate_fails_with_code", "query": UNKNOWN, "code": "PATH_NOT_FOUND"},
            "must fail with PATH_NOT_FOUND, but fails with OBJECT_NOT_FOUND",
        ),
        ("example", {"question": "Refunds?"}, "needs query"),
        ("example", {"query": UNKNOWN}, "query does not validate"),
        ("example", {"query": ORDERS, "expected_shape": [3]}, "expected_shape must be a mapping"),
        (
            "example",
            {"query": ORDERS, "expected_shape": {"max_rows": -1}},
            "expected_shape.max_rows must be a non-negative integer",
        ),
    ],
)
def test_entries_the_runner_cannot_check_are_refused(
    workspace: Path, kind: str, spec: dict[str, Any], message: str
) -> None:
    before = project_revision(workspace / "shop")

    with pytest.raises(SemanticLayerError, match=message):
        _project(workspace).upsert_check(kind=kind, key="entry", spec=spec)

    assert project_revision(workspace / "shop") == before


def test_entries_merge_in_the_file_the_runner_reads_them_from(workspace: Path) -> None:
    project = _project(workspace)
    package = workspace / "shop"
    bounds = {"kind": "query_row_count_bounds", "query": ORDERS, "min_rows": 1}
    (package / "tests").mkdir()
    (package / "tests" / "single.yml").write_text(yaml.safe_dump({"test": bounds}))
    # The runner doesn't read a tests.yml beside package.yml.
    (package / "tests.yml").write_text(yaml.safe_dump({"tests": {"unread": bounds}}))

    project.upsert_check(
        kind="example",
        key="orders",
        spec={"question": "Orders?", "query": ORDERS, "expected_shape": {"min_rows": 1}},
        file_name="questions.yml",
    )
    project.upsert_check(kind="example", key="orders", spec={"question": "Orders by country?"})
    project.upsert_check(kind="test", key="single", spec={"max_rows": 3})
    project.upsert_check(kind="test", key="unread", spec=bounds)
    with pytest.raises(SemanticLayerError, match="holds a single test"):
        project.upsert_check(kind="test", key="other", spec=bounds, file_name="single.yml")

    example = _yaml(package / "examples" / "questions.yml")["examples"]["orders"]
    assert example["question"] == "Orders by country?"
    assert example["expected_shape"] == {"min_rows": 1}
    assert not (package / "examples" / "core.yml").exists()
    assert _yaml(package / "tests" / "single.yml") == {"test": {**bounds, "max_rows": 3}}
    assert list(_yaml(package / "tests" / "core.yml")["tests"]) == ["unread"]
    assert _yaml(package / "tests.yml") == {"tests": {"unread": bounds}}


def test_dry_run_apply_replay_and_undo(workspace: Path) -> None:
    project = _project(workspace)
    before = project.revision()
    spec = {"kind": "query_row_count_bounds", "query": ORDERS, "min_rows": 3}

    def upsert(key: str, entry: dict[str, Any], **arguments: Any) -> dict[str, Any]:
        return project.upsert_check(
            kind="test",
            key="three",
            spec=entry,
            expected_revision=before,
            idempotency_key=key,
            **arguments,
        ).report

    preview = upsert("preview", spec, dry_run=True)
    assert preview["ok"] is True and preview["changed_files"] == ["tests/core.yml"]
    assert project.revision() == before
    applied = project.upsert_check(
        kind="test", key="three", spec=spec, expected_revision=before, idempotency_key="apply"
    )
    assert applied.report["revision"] == preview["proposed_revision"]
    # A retry replays; a stale writer is told so before its entry is checked.
    assert upsert("apply", spec)["status"] == "replayed"
    with pytest.raises(SemanticLayerError) as stale:
        upsert("stale", {"kind": "query_is_fast"})
    assert stale.value.details["conflict_kind"] == "stale_revision"

    assert applied.undo()["ok"] is True
    assert project.revision() == before
    assert not (workspace / "shop" / "tests" / "core.yml").exists()
