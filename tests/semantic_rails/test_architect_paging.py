"""The Architect MCP pages its long answers, and paging is not a limit.

On a 100-relation warehouse and a 34-model package over it (``scripts/large_warehouse.py``), a
list tool answers with the page of its list that starts at ``offset``: as many items as fit in
``PAGE_CHARS`` (at least one, at most ``limit``), the list's ``total_count``, and the
``next_offset`` to pass for the next page. A report tool answers with a summary whose ``pages``
names each list it cuts short; ``page`` (that path) pages the list, and each page names the lists
its own items cut short. Walking the pages reaches every item once, in order, the same on every
call, and ``full_report: true`` still returns all of it in one answer.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from scripts.large_warehouse import (
    PACKAGE_MARTS,
    WIDE_FACT,
    LargeWorkspace,
    build_large_workspace,
    call_tool,
)
from scripts.mcp_context import architect_page_calls
from semantic_rails import architect_introspection as introspection
from semantic_rails.architect_mcp import (
    PAGE_CHARS,
    PAGE_ITEMS,
    _full_or_summary,
    _report_page,
    create_architect_mcp_server,
)
from semantic_rails.architect_service import DECIDE_ROUTES_ACTION
from semantic_rails.architect_transactions import project_revision

PAGING = ("offset", "total_count", "has_more", "next_offset")
ROLES = ("times", "dimensions", "measures")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> LargeWorkspace:
    return build_large_workspace(tmp_path_factory.mktemp("large"))


@pytest.fixture(scope="module")
def server(workspace: LargeWorkspace) -> Any:
    return create_architect_mcp_server(workspace_root=workspace.root)


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _walk(
    server: Any, tool: str, keys: tuple[str, ...], arguments: dict[str, Any]
) -> tuple[list[tuple[str, Any]], list[dict[str, Any]]]:
    """Every ``(key, item)`` of the lists ``keys``, page by page from offset 0, and the pages."""
    items: list[tuple[str, Any]] = []
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = call_tool(server, tool, {**arguments, "offset": offset})
        assert page["ok"] is True, page
        shown = [(key, item) for key in keys for item in page[key]]
        assert page["offset"] == offset and shown
        assert len(shown) == 1 or _size(page) <= PAGE_CHARS
        items += shown
        pages.append(page)
        if not page["has_more"]:
            assert page["next_offset"] is None
            assert len(items) == page["total_count"]
            return items, pages
        assert page["next_offset"] == offset + len(shown)
        offset = page["next_offset"]


def _rest(answer: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in answer.items() if key not in (*keys, *PAGING)}


LISTS: dict[str, Callable[[LargeWorkspace], tuple[str, tuple[str, ...], dict[str, Any]]]] = {
    "tables": lambda ws: ("list_tables", ("tables",), {"duckdb_path": ws.duckdb_path, "limit": 30}),
    "wide_table_columns": lambda ws: (
        "describe_table",
        ("columns",),
        {"duckdb_path": ws.duckdb_path, "relation": WIDE_FACT},
    ),
    "wide_table_profile": lambda ws: (
        "profile_columns",
        ("columns",),
        {"duckdb_path": ws.duckdb_path, "relation": WIDE_FACT, "sample_limit": 2},
    ),
    "dbt_models": lambda ws: (
        "suggest_models_from_dbt",
        ("models",),
        {"target_dir": ws.target_dir},
    ),
    "project_files": lambda ws: (
        "list_project_files",
        ("files",),
        {"project_path": ws.package, "limit": 7},
    ),
}


@pytest.mark.parametrize("case", sorted(LISTS))
def test_every_item_of_a_long_list_is_on_exactly_one_page(
    server: Any, workspace: LargeWorkspace, case: str
) -> None:
    tool, keys, arguments = LISTS[case](workspace)

    items, pages = _walk(server, tool, keys, arguments)
    full = call_tool(server, tool, {**arguments, "full_report": True})

    assert len(pages) > 1
    assert items == [(key, item) for key in keys for item in full[key]]
    assert (full["offset"], full["total_count"], full["has_more"], full["next_offset"]) == (
        0,
        len(items),
        False,
        None,
    )
    # A page is the same on every call, and so is the rest of the answer on every page.
    assert call_tool(server, tool, {**arguments, "offset": pages[1]["offset"]}) == pages[1]
    assert all(_rest(page, keys) == _rest(full, keys) for page in pages)


def test_a_whole_list_is_what_introspection_reads(server: Any, workspace: LargeWorkspace) -> None:
    warehouse_path = workspace.root / workspace.duckdb_path
    with introspection.open_duckdb(warehouse_path) as warehouse:
        tables = introspection.list_tables(warehouse)
        described = introspection.describe_table(warehouse, WIDE_FACT)
    listed = call_tool(server, "list_tables", {"duckdb_path": workspace.duckdb_path})
    every = call_tool(
        server,
        "describe_table",
        {"duckdb_path": workspace.duckdb_path, "relation": WIDE_FACT, "full_report": True},
    )

    # All 100 relations fit one page: list_tables no longer stops at a cap.
    assert listed == {
        "ok": True,
        "tables": tables,
        **dict(zip(PAGING, (0, 100, False, None), strict=True)),
    }
    assert every == {
        "ok": True,
        **described,
        **dict(zip(PAGING, (0, 180, False, None), strict=True)),
    }


def test_suggest_model_pages_carry_drafts_that_merge_into_the_whole_one(
    server: Any, workspace: LargeWorkspace
) -> None:
    arguments = {"duckdb_path": workspace.duckdb_path, "relation": WIDE_FACT}

    items, pages = _walk(server, "suggest_model", ROLES, arguments)
    full = call_tool(server, "suggest_model", {**arguments, "full_report": True})

    assert len(pages) > 1
    assert items == [(role, item) for role in ROLES for item in full[role]]
    merged: dict[str, dict[str, Any]] = {role: {} for role in ROLES}
    for page in pages:
        for name, value in page["upsert_model"].items():
            if name in ROLES:
                assert not set(value) & set(merged[name])
                merged[name].update(value)
            else:
                assert value == full["upsert_model"][name]
    assert merged == {role: full["upsert_model"][role] for role in ROLES}


def _put(item: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        item = item[int(part)] if isinstance(item, list) else item[part]
    item[int(parts[-1]) if isinstance(item, list) else parts[-1]] = value


def _walk_report(fetch: Callable[[str, int], dict[str, Any]], path: str) -> list[Any]:
    """Every item of the list at ``path``, page by page, each with the lists its page cut short
    walked in turn."""
    items: list[Any] = []
    offset = 0
    while True:
        page = fetch(path, offset)
        assert (page["page"], page["offset"]) == (path, offset) and page["items"]
        assert len(page["items"]) == 1 or _size(page) <= PAGE_CHARS
        nested = sorted(page.get("pages", {}))
        for index, item in enumerate(page["items"], start=offset):
            prefix, walked = f"{path}.{index}.", []
            for inner in nested:
                if inner.startswith(prefix) and not any(inner.startswith(f"{w}.") for w in walked):
                    _put(item, inner[len(prefix) :], _walk_report(fetch, inner))
                    walked.append(inner)
            items.append(item)
        if not page["has_more"]:
            assert page["next_offset"] is None and len(items) == page["total_count"]
            return items
        assert page["next_offset"] == offset + len(page["items"])
        offset = page["next_offset"]


def _at(report: Any, path: str) -> Any:
    for part in path.split("."):
        report = report[int(part)] if isinstance(report, list) else report[part]
    return report


def test_every_list_a_summary_cuts_short_pages_to_its_last_item(
    server: Any, workspace: LargeWorkspace
) -> None:
    package = {"project_path": workspace.package}
    summary = call_tool(server, "project_status", package)
    full = call_tool(server, "project_status", {**package, "full_report": True})

    assert _size(summary) <= PAGE_CHARS < _size(full)
    undecided = full["route_census"]["undecided"]
    assert summary["pages"]["route_census.undecided"] == {
        "total_count": len(undecided),
        "next_offset": len(summary["route_census"]["undecided"]),
    }
    # Each listed path pages its list, with the lists inside its items, to the last item.
    for path, listed in summary["pages"].items():
        everything = _at(full, path)
        assert listed["total_count"] == len(everything) and listed["next_offset"] < len(everything)
        walked = _walk_report(
            lambda page, offset: _report_page(full, page, offset, PAGE_ITEMS, {}), path
        )
        assert walked == everything, path


def test_report_pages_come_from_the_tool_the_same_on_every_call(
    server: Any, workspace: LargeWorkspace
) -> None:
    package = {"project_path": workspace.package}
    full = call_tool(server, "project_status", {**package, "full_report": True})
    summary = call_tool(server, "project_status", package)

    def fetch(page: str, offset: int) -> dict[str, Any]:
        answer = call_tool(
            server,
            "project_status",
            {**package, "page": page, "offset": offset, "expected_revision": summary["revision"]},
        )
        assert (answer["ok"], answer["project_path"]) == (full["ok"], full["project_path"])
        assert answer["revision"] == full["revision"] == summary["revision"]
        return answer

    first = fetch("route_census.undecided", 0)
    second = fetch("route_census.undecided", first["next_offset"])
    assert fetch("route_census.undecided", first["next_offset"]) == second
    # A route is chosen from all of a pair's options: the summary and its pages show whole pairs.
    undecided = full["route_census"]["undecided"]
    shown = summary["route_census"]["undecided"]
    assert shown and shown == undecided[: len(shown)]
    assert summary["route_census"]["undecided_omitted"] == len(undecided) - len(shown)
    walked = first["items"] + second["items"]
    assert walked == undecided[: len(walked)]
    assert not [path for path in summary["pages"] if ".undecided." in path]
    assert _walk_report(fetch, "route_census.assumed") == full["route_census"]["assumed"]


def test_a_release_summary_keeps_every_blocker_and_next_action_whole(
    server: Any, workspace: LargeWorkspace
) -> None:
    package = {"project_path": workspace.package}
    status = call_tool(server, "project_status", {**package, "full_report": True})
    gate = call_tool(
        server,
        "promotion_check",
        {
            **package,
            "environment": "production",
            "compare_path": workspace.scaffold,
            "full_report": True,
        },
    )
    # Eight examples fail after a change (more than any summary list keeps), and the actions
    # an agent follows next, the long one first, as project_status puts it when the package's
    # own metrics need undecided routes.
    actions = [
        "Decide the 2 join routes the package's own metrics, examples or tests need: the first "
        f"pairs in route_census.undecided, each naming them in needed_by. {DECIDE_ROUTES_ACTION}",
        *status["next_actions"],
    ]
    blockers = [
        {
            "check": "examples",
            "code": "EXAMPLE_FAILED",
            "message": f"example orders_by_region_{n} returned other rows than it expects",
            "details": {
                "example": f"example.large.orders_by_region_{n}",
                "query": {
                    "select": ["measure.large.sales_order_count"],
                    "group_by": ["dimension.large_region_attribute_001"],
                },
            },
        }
        for n in range(8)
    ]
    failing = {
        **gate,
        "ok": False,
        "promotion_checks": {**gate["promotion_checks"], "examples": False},
        "blockers": blockers,
        "next_actions": actions,
    }

    summary = _full_or_summary(failing, False)

    assert "artifacts.parse.warnings" in summary["pages"]
    assert summary["blockers"] == blockers
    assert summary["next_actions"] == actions
    assert call_tool(server, "project_status", package)["next_actions"] == status["next_actions"]


def test_a_page_after_a_write_refuses_with_the_revision_it_read(
    workspace: LargeWorkspace, tmp_path: Path
) -> None:
    """Deciding a route removes pairs from the undecided list, so an offset read before the
    write counts another list: the next page refuses instead of skipping pairs."""
    shutil.copytree(
        workspace.root / workspace.package,
        tmp_path / "large",
        ignore=shutil.ignore_patterns("*.duckdb*", ".*"),
    )
    server = create_architect_mcp_server(workspace_root=tmp_path)
    package = {"project_path": "large"}
    summary = call_tool(server, "project_status", package)
    first = call_tool(
        server,
        "project_status",
        {**package, "page": "route_census.undecided", "expected_revision": summary["revision"]},
    )
    [decision] = [
        option["decision"]
        for option in first["items"][0]["details"]["clarification"]["options"][:1]
    ]
    decided = call_tool(
        server,
        "record_route_decision",
        {
            **package,
            "expected_revision": first["revision"],
            "idempotency_key": "decide-the-first-pair",
            **decision,
        },
    )
    assert decided["ok"] is True, decided

    stale = {"expected_revision": first["revision"], "offset": first["next_offset"]}
    for tool, arguments in (
        ("project_status", {**package, "page": "route_census.undecided", **stale}),
        ("validate_project", {**package, "page": "route_census.undecided", **stale}),
        ("list_project_files", {**package, "expected_revision": first["revision"]}),
    ):
        assert _refusal(call_tool(server, tool, arguments)) == (
            "CONFIG_CONFLICT",
            "the package changed after the answer that gave expected_revision, so its offsets "
            "count another list: read the summary again, or page from offset 0 with the current "
            "revision",
            {
                "conflict_kind": "stale_revision",
                "expected_revision": first["revision"],
                "current_revision": decided["revision"],
            },
        ), tool
    # The current revision pages on, and every summary names the revision it read.
    again = call_tool(
        server,
        "project_status",
        {**package, "page": "route_census.undecided", "expected_revision": decided["revision"]},
    )
    assert again["ok"] is True and again["total_count"] < first["total_count"]
    assert call_tool(server, "validate_project", package)["revision"] == decided["revision"]


@pytest.mark.parametrize("tool", ["diff_project", "impact_project"])
def test_diff_and_impact_answer_with_a_summary_whose_changes_page(
    server: Any, workspace: LargeWorkspace, tool: str
) -> None:
    arguments = {"project_path": workspace.package, "compare_path": workspace.scaffold}
    summary = call_tool(server, tool, arguments)
    full = call_tool(server, tool, {**arguments, "full_report": True})

    assert summary["ok"] is True and _size(summary) <= PAGE_CHARS < _size(full)
    assert summary["pages"]["changes"]["total_count"] == len(full["changes"]) > 1000
    assert summary["changes"] == full["changes"][: len(summary["changes"])]


def test_a_diff_pages_to_its_last_change(server: Any, workspace: LargeWorkspace) -> None:
    arguments = {"project_path": workspace.package, "compare_path": workspace.scaffold}
    full = call_tool(server, "diff_project", {**arguments, "full_report": True})

    def fetch(page: str, offset: int) -> dict[str, Any]:
        return call_tool(server, "diff_project", {**arguments, "page": page, "offset": offset})

    assert _walk_report(fetch, "changes") == full["changes"]


def test_a_write_summary_counts_the_changes_it_leaves_out(
    server: Any, workspace: LargeWorkspace
) -> None:
    [arguments] = [
        arguments
        for label, _tool, arguments in architect_page_calls(workspace)
        if label == "import_dbt_project_preview"
    ]
    summary = call_tool(server, "import_dbt_project", arguments)
    full = call_tool(server, "import_dbt_project", {**arguments, "full_report": True})

    assert summary["ok"] is full["ok"] is True
    assert _size(summary) <= PAGE_CHARS < _size(full)
    assert summary["changed_files"] == full["changed_files"]
    assert summary["pages"]["changes"]["total_count"] == len(full["changes"])
    assert len(full["changes"]) == len(full["changed_files"]) > len(PACKAGE_MARTS)
    assert summary["pages"]["models"]["total_count"] == len(PACKAGE_MARTS)
    assert "full_report" in summary["report"] and "idempotency_key" in summary["report"]
    # A dry run writes nothing.
    assert project_revision(workspace.root / workspace.scaffold) == workspace.scaffold_revision


def _refusal(answer: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    assert answer["ok"] is False
    error = answer["error"]
    return error["code"], error["message"], error["details"]


def test_paging_misuse_names_the_fix(server: Any, workspace: LargeWorkspace) -> None:
    warehouse = {"duckdb_path": workspace.duckdb_path}
    package = {"project_path": workspace.package}
    summary = call_tool(server, "project_status", package)

    assert _refusal(call_tool(server, "list_tables", {**warehouse, "offset": 100})) == (
        "INVALID_MCP_ARGUMENTS",
        "offset 100 is past the last of the 100 tables: pass the next_offset the previous page "
        "returned, or 0 for the first page",
        {"offset": 100, "total_count": 100},
    )
    assert _refusal(
        call_tool(server, "list_tables", {**warehouse, "offset": 5, "full_report": True})
    ) == (
        "INVALID_MCP_ARGUMENTS",
        "full_report returns every table in one response: drop offset, or drop full_report to page",
        {"offset": 5},
    )
    assert _refusal(call_tool(server, "project_status", {**package, "offset": 5})) == (
        "INVALID_MCP_ARGUMENTS",
        "offset pages the list that page names: pass page, a path from the summary's pages",
        {"offset": 5},
    )
    paged_whole = {**package, "page": "parse.warnings", "full_report": True}
    assert _refusal(call_tool(server, "validate_project", paged_whole)) == (
        "INVALID_MCP_ARGUMENTS",
        "full_report returns the whole report and page one list in it: pass one of them",
        {"page": "parse.warnings"},
    )
    past = {**package, "page": "route_census.assumed", "offset": 35}
    assert _refusal(call_tool(server, "project_status", past))[1] == (
        "offset 35 is past the last of the 35 items in route_census.assumed: pass the "
        "next_offset the previous page returned, or 0 for the first page"
    )
    # A misspelt path, and one naming no list (the census itself), list the paths there are.
    for page in ("route_census.undecdied", "route_census"):
        assert _refusal(call_tool(server, "project_status", {**package, "page": page})) == (
            "INVALID_MCP_ARGUMENTS",
            f"page {page!r} names no list in this report: pass a path from the summary's pages",
            {"page": page, "pages": sorted(summary["pages"])},
        )
    assert _refusal(
        call_tool(
            server,
            "suggest_links",
            {**warehouse, "relation": WIDE_FACT, "expected_revision": summary["revision"]},
        )
    ) == (
        "INVALID_MCP_ARGUMENTS",
        "expected_revision checks the package project_path names: pass project_path, or drop "
        "expected_revision",
        {"expected_revision": summary["revision"]},
    )
    # The schema refuses a negative offset and an empty page before the tool runs.
    for wrong in ({"offset": -1}, {"limit": 0}):
        with pytest.raises(ToolError, match="greater than or equal to"):
            call_tool(server, "list_tables", {**warehouse, **wrong})
