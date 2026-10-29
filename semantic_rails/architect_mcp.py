"""Developer-facing MCP server for Semantic Rails package authoring.

This module exposes the Architect MCP tool surface used to create package
scaffolds, edit project YAML, and run parse/runtime/release validations from
MCP clients while keeping writes scoped to a configured workspace root.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from . import architect_introspection as introspection
from . import dbt_artifacts
from .architect_service import (
    DECIDE_ROUTES_ACTION,
    NEW_PROJECT_ROUTES_ACTION,
    ArchitectProject,
    FirstModel,
    ProjectSpec,
    ProjectWarehouse,
    project_setup_questions,
)
from .architect_service import create_project as create_project_service
from .architect_transactions import (
    ABSENT_PROJECT_REVISION,
    project_revision,
)
from .config import repo_root
from .config_validation import PackageReference, parse_config_report, validate_config_report
from .dialects import (
    connection_option_errors,
    normalize_connection_option_name,
    snowflake_adbc_connect_errors,
    snowflake_native_direct_connect_errors,
    warehouse_connector,
)
from .errors import SemanticLayerError
from .lookalike_models import LOOKALIKE_MODELS
from .mcp import SemanticLayerMCPAdapter, json_text
from .naming import slug as _slug
from .package_tools import (
    diff_package_report,
    impact_report,
    promote_package_report,
    run_examples_report,
    run_package_tests_report,
)
from .upgrade.service import legacy_form_hint
from .upgrade.service import upgrade_project as upgrade_project_service

DEFAULT_ARCHITECT_PORT = 8010
DEFAULT_WORKSPACE_ROOT = repo_root()
ARCHITECT_INTERFACE_VERSION = "v1"
ArchitectTransport = Literal["stdio", "streamable-http"]
# The network transports can write files, so they require a bearer token.
ARCHITECT_TOKEN_ENV = "SEMANTIC_RAILS_ARCHITECT_TOKEN"
ARCHITECT_TOKEN_FILE_ENV = "SEMANTIC_RAILS_ARCHITECT_TOKEN_FILE"
MIN_ARCHITECT_TOKEN_LENGTH = 32
MAX_PREVIEW_ROWS = 200
# Long lists come in pages, counted in items from offset as the query server's discover and
# valid-values count them: a page holds what fits in PAGE_CHARS characters of compact JSON (at
# least one item; limit lowers it). Paging is not a limit: full_report: true returns every item.
PAGE_CHARS = 16_000
# A page holds at most this many items unless limit says otherwise, as valid-values does.
PAGE_ITEMS = 100
# A profile_columns page profiles at most this many columns before it is cut to fit.
PROFILE_PAGE_COLUMNS = 50
# A default response is a summary an MCP client shows whole; full_report returns everything.
SUMMARY_WARNINGS = 10
SUMMARY_UNDECIDED_ROUTES = 3
# The undecided pairs a summary shows are whole, every option with them, within this budget.
SUMMARY_PAIRS_CHARS = 8_000
SUMMARY_DIFF_CHARS = 12_000
# Any other summary list keeps the first items that fit in this many characters (at least one).
SUMMARY_LIST_CHARS = 2_000


@dataclass(frozen=True)
class _Cut:
    """How much of each list a summary keeps."""

    warnings: int = SUMMARY_WARNINGS
    list_chars: int = SUMMARY_LIST_CHARS
    diff_chars: int = SUMMARY_DIFF_CHARS
    pairs_chars: int = SUMMARY_PAIRS_CHARS


# A summary that passes PAGE_CHARS is cut again, each time to half as much (the fewest: one
# warning, and the first row of each list), so a large package's summary still fits a page.
_CUTS = tuple(
    _Cut(
        max(1, SUMMARY_WARNINGS >> level),
        SUMMARY_LIST_CHARS >> level,
        SUMMARY_DIFF_CHARS >> level,
        SUMMARY_PAIRS_CHARS >> level,
    )
    for level in range(4)
)
# Refusals, a release's blockers, the next actions and the files a write changed are never cut.
_NEVER_CUT = frozenset({"error", "errors", "blockers", "next_actions", "changed_files"})
_SUMMARY_NOTE = (
    "summary: the first warnings without details, the route census's counts and first undecided "
    "pairs (whole), failing checks only, the first items of long lists; pages names each list "
    "cut short, with its total_count and next_offset: pass page (its path), offset and limit "
    "for the rest, or full_report: true for the whole report"
)
_WRITE_SUMMARY_NOTE = (
    "summary: the first warnings without details, the route census's counts and first undecided "
    "pairs (whole), the first items of long lists, and the first file changes, their diffs "
    "without file contents; pages counts each list cut short. The same call with full_report: "
    "true returns everything (a retry with the same idempotency_key replays a write)"
)
FullReport = Annotated[bool, Field(description="true: the whole report, not a summary")]
# Paging is a convention every list tool shares, which the server instructions state once.
FullList = Annotated[bool, Field(description="true: every item, unpaged")]
Offset = Annotated[int, Field(ge=0)]
Limit = Annotated[int, Field(ge=1)]
Page = Annotated[str, Field(description="a path from the summary's pages")]
# RFC 6750 b64token: what a client can send in an Authorization header.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_TOKEN_HINT = (
    "create one without printing it: (umask 077; python3 -c "
    '"import secrets; print(secrets.token_urlsafe(32))" > ~/.config/semantic-rails/architect.token)'
)
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class ProjectSetupAnswers(BaseModel):
    package_id: str = Field(
        description="Lowercase package directory name, for example analytics_core."
    )
    description: str = Field(default="Semantic Rails package managed through Architect MCP.")
    warehouse: str = Field(default="duckdb", description="Warehouse kind, for example duckdb.")
    data: str = Field(
        default="starter",
        description="DuckDB only: 'starter' (a two-row CSV) or 'external' (another tool builds it).",
    )
    default_db: str = Field(default="", description="DuckDB database path inside the package.")
    connection_kind: str = Field(default="", description="Connection kind for other warehouses.")
    connection_name: str = Field(default="", description="Named Snowflake connection or profile.")
    connection_options: str = Field(
        default="{}", description="JSON object of connection options; use *_env names for secrets."
    )
    first_entity: str = Field(default="event", description="Business entity to model first.")
    relation: str = Field(
        default="raw_events", description="Table or view backing it, schema-qualified if needed."
    )
    primary_key: str = Field(default="event_id")
    time_column: str = Field(
        default="occurred_at", description="Date/timestamp column; blank for no time."
    )
    amount_column: str = Field(
        default="", description="Numeric column to sum; blank for none (starter data: amount)."
    )
    dimension_column: str = Field(default="", description="Categorical column; blank for none.")


class ArchitectMutationIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ArchitectFileChange(BaseModel):
    path: str
    operation: Literal["create", "update", "delete"]
    before_sha256: str | None = None
    after_sha256: str | None = None
    before_bytes: int = 0
    after_bytes: int = 0
    content_encoding: Literal["utf-8", "base64", "none"] = "none"
    proposed_content: str | None = None
    diff: str = ""
    diff_truncated: bool = False


class ArchitectMutationResult(BaseModel):
    """Stable structured output shared by every Architect mutation tool."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str
    project_path: str
    workspace_root: str = ""
    expected_revision: str
    base_revision: str
    current_revision: str
    revision: str
    proposed_revision: str
    idempotency_key: str
    idempotent_replay: bool
    dry_run: bool
    changed_files: list[str]
    changes: list[ArchitectFileChange]
    parse: dict[str, Any] | None = None
    route_decisions_added: list[dict[str, Any]] = Field(default_factory=list)
    route_changes: list[dict[str, Any]] = Field(default_factory=list)
    error: ArchitectMutationIssue | None = None
    errors: list[ArchitectMutationIssue] = Field(default_factory=list)


def _read_only_annotations(title: str, *, open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=open_world,
    )


def _mutation_annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )


def _without_titles(schema: Any) -> Any:
    """``schema`` without ``title`` annotations; a property named title, and values, stay."""
    if isinstance(schema, list):
        return [_without_titles(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        key: value if key in {"default", "const", "enum", "examples"} else _without_titles(value)
        for key, value in schema.items()
        if not (key == "title" and isinstance(value, str))
    }


def _within(path: Path, root: Path) -> bool:
    try:
        os.path.commonpath([str(path), str(root)])
    except ValueError:
        return False
    return os.path.commonpath([str(path), str(root)]) == str(root)


def _server_workspace_root(workspace_root: str | os.PathLike[str] | None) -> Path:
    return Path(workspace_root or DEFAULT_WORKSPACE_ROOT).expanduser().resolve()


def _resolve_project_path(
    project_path: str,
    *,
    workspace_root: Path,
    package_id: str = "",
    require_exists: bool = True,
    require_package_root: bool = True,
) -> Path:
    raw = str(project_path or "").strip()
    if not raw:
        if not package_id:
            raise SemanticLayerError("INVALID_CONFIG", "Provide project_path or package_id")
        raw = f"configs/semantic_rails/{_slug(package_id, fallback='semantic_project')}"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    path = path.resolve()
    if not _within(path, workspace_root):
        missing = "does not exist and " if not path.exists() else ""
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Project path '{path}' {missing}is outside the Architect MCP workspace root "
            f"'{workspace_root}'; pass a package directory inside it, relative to it or absolute",
            details={"workspace_root": str(workspace_root), "requested_path": str(path)},
        )
    if require_exists and not path.exists():
        raise SemanticLayerError("INVALID_CONFIG", f"Project path '{path}' does not exist")
    if require_package_root and path.exists() and not (path / "package.yml").is_file():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Project path must be a Semantic Rails package directory with package.yml",
            details={"project_path": str(path)},
        )
    return path


def _resolve_relative_path(project: Path, relative_path: str) -> Path:
    raw = str(relative_path or "").strip().lstrip("/")
    if not raw:
        raise SemanticLayerError("INVALID_CONFIG", "relative_path is required")
    path = (project / raw).resolve()
    if not _within(path, project):
        raise SemanticLayerError(
            "INVALID_CONFIG", "relative_path must stay inside the project directory"
        )
    return path


def _report_error(exc: Exception) -> dict[str, Any]:
    hints = _recovery_hints(exc)
    if isinstance(exc, SemanticLayerError):
        return {
            "ok": False,
            "status": "error",
            "error": {"code": exc.code, "message": str(exc), "details": dict(exc.details or {})},
            "errors": [{"code": exc.code, "message": str(exc), "details": dict(exc.details or {})}],
            "recovery_hints": hints,
        }
    return {
        "ok": False,
        "status": "error",
        "error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}},
        "errors": [{"code": "INTERNAL_ERROR", "message": str(exc), "details": {}}],
        "recovery_hints": hints,
    }


_UPGRADE_ACTION = "call upgrade_project (dry_run)"


def _json_size(value: Any) -> int:
    """Characters of ``value`` as compact JSON, as an MCP client receives it."""
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _is_receipt(row: Any) -> bool:
    """Transaction receipts and archives under dot-directories aren't package files."""
    return isinstance(row, dict) and bool(re.match(r"\.[^/]*/", str(row.get("path"))))


def _failing(row: Any) -> bool:
    return not (isinstance(row, dict) and row.get("ok") is True)


# Summary lists that show only some rows: package files, and the checks that failed.
_SUMMARY_ROWS = {
    "files": lambda row: not _is_receipt(row),
    "probes": _failing,
    "examples": _failing,
    "tests": _failing,
}


def _summary(
    report: Any, pages: dict[str, Any] | None = None, path: str = "", cut: _Cut = _CUTS[0]
) -> Any:
    """``report`` as a default response: each ``warnings`` list as its counts by code and its
    first warnings without details, a ``route_census`` as its counts and first undecided pairs,
    ``files`` without those under dot-directories, only the failing ``probes``, ``examples`` and
    ``tests``, and every list as the first rows that fit in ``cut.list_chars`` characters.
    ``pages`` (by path) gets each list the summary leaves rows out of, with its ``total_count``
    and ``next_offset``, the first row it leaves out. Errors and ``changed_files`` stay whole."""
    if not isinstance(report, dict):
        return report
    pages = {} if pages is None else pages
    out: dict[str, Any] = {}
    for key, value in report.items():
        where = f"{path}{key}"
        if key in _NEVER_CUT or not isinstance(value, (dict, list)):
            out[key] = value
        elif key == "warnings" and isinstance(value, list):
            shown = _first_warnings(value, cut.warnings)
            out[key] = [
                {name: item for name, item in value[index].items() if name != "details"}
                if isinstance(value[index], dict)
                else value[index]
                for index in shown
            ]
            out["warning_counts"] = dict(
                Counter(
                    row.get("code", "WARNING") if isinstance(row, dict) else "WARNING"
                    for row in value
                )
            )
            if len(value) > len(shown):
                out["warnings_omitted"] = len(value) - len(shown)
            _note_page(pages, where, len(value), shown)
        elif key == "route_census" and isinstance(value, dict):
            undecided = list(value.get("undecided") or [])
            first, shown = _first_pairs(undecided, cut, pages, f"{where}.undecided")
            out[key] = {
                "counts": {
                    **{name: len(rows) for name, rows in value.items()},
                    "undecided_needed": sum(bool(row.get("needed_by")) for row in undecided),
                },
                "undecided": first,
            }
            if len(undecided) > len(shown):
                out[key]["undecided_omitted"] = len(undecided) - len(shown)
            for name, rows in value.items():
                if isinstance(rows, list):
                    _note_page(
                        pages, f"{where}.{name}", len(rows), shown if name == "undecided" else []
                    )
        elif isinstance(value, list):
            keep = _SUMMARY_ROWS.get(key)
            rows = [index for index, row in enumerate(value) if keep is None or keep(row)]
            out[key], shown = _first_rows(value, rows, pages, where, cut)
            _note_page(pages, where, len(value), shown)
        else:
            out[key] = _summary(value, pages, f"{where}.", cut)
    return out


def _first_rows(
    rows: list[Any], indexes: list[int], pages: dict[str, Any], where: str, cut: _Cut
) -> tuple[list[Any], list[int]]:
    """The summaries of ``rows[indexes]`` that fit in ``cut.list_chars`` (at least one), and
    their indexes; ``pages`` gets the lists each of those rows leaves rows out of."""
    kept: list[Any] = []
    shown: list[int] = []
    used = 0
    for index in indexes:
        nested: dict[str, Any] = {}
        row = _summary(rows[index], nested, f"{where}.{index}.", cut)
        size = _json_size(row)
        if kept and used + size > cut.list_chars:
            break
        kept.append(row)
        shown.append(index)
        pages.update(nested)
        used += size + 1
    return kept, shown


def _first_pairs(
    pairs: list[Any], cut: _Cut, pages: dict[str, Any], where: str
) -> tuple[list[Any], list[int]]:
    """The first undecided pairs, up to ``SUMMARY_UNDECIDED_ROUTES``, that fit whole in
    ``cut.pairs_chars``, and their indexes. A first pair that doesn't fit shows the options that
    do, marked (``_fit_pair``), so a route is never chosen from a list that looks whole."""
    rows: list[Any] = []
    used = 0
    for index, pair in enumerate(pairs[:SUMMARY_UNDECIDED_ROUTES]):
        room = cut.pairs_chars - used
        if rows and _json_size(pair) > room:
            break
        row = _fit_pair(pair, room, f"{where}.{index}.", pages)
        rows.append(row)
        used += _json_size(row) + 1
    return rows, list(range(len(rows)))


def _pair_options(pair: Any) -> list[Any] | None:
    clarification = pair.get("details", {}).get("clarification") if isinstance(pair, dict) else None
    options = clarification.get("options") if isinstance(clarification, dict) else None
    return options if isinstance(options, list) else None


def _options_note(pair: dict[str, Any], start: int, shown: int, total: int, path: str) -> str:
    """What part of a pair's options an answer shows, and where the rest is."""
    where = f"options {start + 1}-{start + shown} of {total} for {pair.get('source_entity')} to "
    where += str(pair.get("target_entity"))
    if start + shown < total:
        return f"{where}, continued on the next page of {path}"
    return f"{where}: the last of them"


def _fit_pair(pair: Any, room: int, where: str, pages: dict[str, Any]) -> Any:
    """``pair`` whole when it fits in ``room`` characters; else with the first of its options
    that fit (at least one), ``options_total`` and an ``options_note`` saying which they are,
    and ``pages`` naming the options list, which pages from where this one stops."""
    options = _pair_options(pair)
    if _json_size(pair) <= room or not options:
        return pair
    path = f"{where}details.clarification.options"
    details = pair["details"]
    clarification = details["clarification"]

    def shown(count: int) -> dict[str, Any]:
        marked = {
            **clarification,
            "options": options[:count],
            "options_total": len(options),
            "options_note": _options_note(pair, 0, count, len(options), path),
        }
        return {**pair, "details": {**details, "clarification": marked}}

    # One option at least; then each that fits (the note's numbers may grow a few digits).
    count, used = 1, _json_size(shown(1)) + 8
    while count < len(options) and used + _json_size(options[count]) + 1 <= room:
        used += _json_size(options[count]) + 1
        count += 1
    pages[path] = {"total_count": len(options), "next_offset": count}
    return shown(count)


def _census_list(path: str) -> bool:
    """Whether ``path`` names a route census list, whose rows a page keeps whole (a pair larger
    than a page continues its options on pages of their own)."""
    parts = path.split(".")
    return len(parts) >= 2 and parts[-2] == "route_census"


def _options_pair(path: str) -> str:
    """The path of the census pair whose options ``path`` names, or ``""``."""
    head, _, tail = path.rpartition(".details.clarification.options")
    return head if not tail and _census_list(head.rpartition(".")[0]) else ""


def _note_page(pages: dict[str, Any], where: str, total: int, shown: Any) -> None:
    """Record the list at ``where`` in ``pages`` when the summary leaves a row of it out."""
    shown = set(shown)
    first_left_out = next((index for index in range(total) if index not in shown), total)
    if first_left_out < total:
        pages[where] = {"total_count": total, "next_offset": first_left_out}


def _first_warnings(warnings: list[Any], count: int = SUMMARY_WARNINGS) -> list[int]:
    """The indexes of the first warning of each code, then the next ones, up to ``count``, in
    report order: a new kind of warning never hides behind many of another."""
    firsts: dict[str, int] = {}
    for index, row in enumerate(warnings):
        firsts.setdefault(row.get("code", "") if isinstance(row, dict) else "", index)
    shown = set(list(firsts.values())[:count])
    for index in range(len(warnings)):
        if len(shown) >= count:
            break
        shown.add(index)
    return sorted(shown)


def _summary_changes(
    changes: list[dict[str, Any]], pages: dict[str, Any], cut: _Cut
) -> list[dict[str, Any]]:
    """File changes without their proposed contents, in order while they fit in one shared
    budget: the last one's diff cut to fit. ``pages`` counts the ones left out."""
    budget, rows = cut.diff_chars, list[dict[str, Any]]()
    for change in changes:
        row = {**change, "proposed_content": None}
        room = budget - _json_size({**row, "diff": ""})
        if rows and room <= 0:
            break
        diff = str(row.get("diff") or "")
        if len(diff) > room:
            row.update(diff=diff[: diff.rfind("\n", 0, max(room, 0)) + 1], diff_truncated=True)
        budget -= _json_size(row)
        rows.append(row)
    _note_page(pages, "changes", len(changes), range(len(rows)))
    return rows


def _fitted(build: Any) -> dict[str, Any]:
    """The first of ``build(cut, pages)`` over ``_CUTS`` that fits in ``PAGE_CHARS``, else the
    most cut one."""
    out: dict[str, Any] = {}
    for cut in _CUTS:
        pages: dict[str, Any] = {}
        out = build(cut, pages)
        if pages:
            out["pages"] = pages
        if _json_size(out) <= PAGE_CHARS:
            break
    return out


def _full_or_summary(report: dict[str, Any], full_report: bool) -> dict[str, Any]:
    if full_report:
        return report
    return _fitted(lambda cut, pages: {**_summary(report, pages, "", cut), "report": _SUMMARY_NOTE})


def _mutation_result(
    payload: dict[str, Any], *, full_report: bool = True
) -> ArchitectMutationResult:
    if payload.get("status") in {"rolled_back_after_parse_error", "preview_invalid"}:
        hint = legacy_form_hint(payload["project_path"], _UPGRADE_ACTION)
        payload = {**payload, "next_actions": [*payload.get("next_actions", []), *hint]}
    if not full_report:
        changes = list(payload.get("changes") or [])
        rest = {key: value for key, value in payload.items() if key != "changes"}
        payload = _fitted(
            lambda cut, pages: {
                **_summary(rest, pages, "", cut),
                "changes": _summary_changes(changes, pages, cut),
                "report": _WRITE_SUMMARY_NOTE,
            }
        )
    return ArchitectMutationResult.model_validate(payload)


def _page_fields(offset: int, shown: int, total: int) -> dict[str, Any]:
    end = offset + shown
    return {
        "offset": offset,
        "total_count": total,
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }


def _check_offset(offset: int, total: int, what: str) -> None:
    if offset and offset >= total:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            f"offset {offset} is past the last of the {total} {what}: pass the next_offset the "
            "previous page returned, or 0 for the first page",
            details={"offset": offset, "total_count": total},
        )


def _check_list_paging(offset: int, full_report: bool, what: str) -> None:
    if full_report and offset:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            f"full_report returns every {what} in one response: drop offset, or drop "
            "full_report to page",
            details={"offset": offset},
        )


def _read_revision(project: Path, expected_revision: str) -> str:
    """The package's revision; a page call that names another one (the revision of the page
    before) refuses, since its offsets count a list the package no longer has."""
    current = project_revision(project)
    if expected_revision and expected_revision != current:
        raise SemanticLayerError(
            "CONFIG_CONFLICT",
            "the package changed after the answer that gave expected_revision, so its offsets "
            "count another list: read the summary again, or page from offset 0 with the "
            "current revision",
            details={
                "conflict_kind": "stale_revision",
                "expected_revision": expected_revision,
                "current_revision": current,
            },
        )
    return current


def _check_report_paging(page: str, offset: int, full_report: bool) -> None:
    if page and full_report:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            "full_report returns the whole report and page one list in it: pass one of them",
            details={"page": page},
        )
    if not page and offset:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            "offset pages the list that page names: pass page, a path from the summary's pages",
            details={"offset": offset},
        )


def _fill(sizes: Any, limit: int, room: int) -> int:
    """How many items, in order, a page holds: at most ``limit``, as many as fit in ``room``
    characters, and at least one."""
    count = used = 0
    for size in sizes:
        if count >= limit or (count and used + size > room):
            break
        count, used = count + 1, used + size + 1
    return count


def _paged(
    payload: dict[str, Any],
    key: str,
    offset: int,
    limit: int,
    full_report: bool,
    what: str,
) -> dict[str, Any]:
    """``payload`` with the page of its ``key`` list that starts at ``offset``, and the paging
    fields; every item when ``full_report``."""
    items = list(payload[key])
    _check_offset(offset, len(items), what)
    if full_report:
        return {**payload, **_page_fields(0, len(items), len(items))}
    room = PAGE_CHARS - _json_size({**payload, key: [], **_page_fields(offset, 0, len(items))})
    shown = _fill((_json_size(item) for item in items[offset:]), limit, room)
    return {
        **payload,
        key: items[offset : offset + shown],
        **_page_fields(offset, shown, len(items)),
    }


def _paged_groups(
    payload: dict[str, Any],
    groups: tuple[str, ...],
    offset: int,
    limit: int,
    full_report: bool,
    what: str,
    *,
    extra: Any = None,
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """Page several lists of ``payload`` as one, ``groups`` in order: the payload with each list
    cut to the page's items, and those ``(group, item)`` pairs. ``extra(group, item)`` is what
    else the page carries for an item, counted in its size."""
    items = [(group, item) for group in groups for item in payload.get(group) or []]
    _check_offset(offset, len(items), what)
    if full_report:
        return {**payload, **_page_fields(0, len(items), len(items))}, items
    envelope = {key: value for key, value in payload.items() if key not in groups}
    room = PAGE_CHARS - _json_size({**envelope, **_page_fields(offset, 0, len(items))})
    sizes = (
        _json_size(item) + (_json_size(extra(group, item)) if extra else 0)
        for group, item in items[offset:]
    )
    page = items[offset : offset + _fill(sizes, limit, room)]
    return {
        **{
            key: [item for group, item in page if group == key] if key in groups else value
            for key, value in payload.items()
        },
        **_page_fields(offset, len(page), len(items)),
    }, page


_ROLES = ("times", "dimensions", "measures")
_LINK_BUCKETS = ("proposals", "below_threshold", "not_proposed")


def _paged_suggestion(
    suggestion: dict[str, Any], offset: int, limit: int, full_report: bool
) -> dict[str, Any]:
    """``suggestion`` with one page of its times, then dimensions, then measures, and its draft
    ``upsert_model`` arguments for those: applied in turn, the pages' drafts merge into one
    model. Low-confidence suggestions are in no draft, as on the whole suggestion."""
    draft = suggestion["upsert_model"]
    if full_report:
        return _paged_groups(suggestion, _ROLES, offset, limit, True, "suggestions")[0]

    def key(role: str, item: dict[str, Any]) -> str:
        return str(item["key"] if role == "measures" else item["column"])

    skeleton = {name: value for name, value in draft.items() if name not in _ROLES}
    paged, page = _paged_groups(
        {**suggestion, "upsert_model": skeleton},
        _ROLES,
        offset,
        limit,
        False,
        "suggestions",
        # The draft's entry for the item, under its key, as the page's draft carries it.
        extra=lambda role, item: {key(role, item): draft[role].get(key(role, item))},
    )
    keys = {(role, key(role, item)) for role, item in page}
    paged["upsert_model"] = {
        name: {k: v for k, v in value.items() if (name, k) in keys} if name in _ROLES else value
        for name, value in draft.items()
    }
    return paged


def _report_or_page(
    report: dict[str, Any],
    project: Path,
    revision: str,
    full_report: bool,
    page: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """The report's summary with the revision it read, the whole report, or one page of a list
    in it."""
    if full_report:
        return report
    if not page:
        return _full_or_summary({**report, "revision": revision}, False)
    envelope = {"ok": report.get("ok"), "project_path": str(project), "revision": revision}
    return _report_page(report, page, offset, limit, envelope)


def _report_page(
    report: dict[str, Any], page: str, offset: int, limit: int, envelope: dict[str, Any]
) -> dict[str, Any]:
    """One page of the list at ``page`` (dotted keys and list indexes) in ``report``: its items
    as the summary shows rows, and the lists each of them leaves rows out of in ``pages``."""
    target: Any = report
    for part in page.split("."):
        if isinstance(target, dict) and part in target:
            target = target[part]
        elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
            target = target[int(part)]
        else:
            target = None
            break
    if not isinstance(target, list):
        listed: dict[str, Any] = {}
        _summary(report, listed)
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            f"page {page!r} names no list in this report: pass a path from the summary's pages",
            details={"page": page, "pages": sorted(listed)},
        )
    _check_offset(offset, len(target), f"items in {page}")
    census, pair_path = _census_list(page), _options_pair(page)
    pair = _at_path(report, pair_path) if pair_path else None

    def marked(shown: int) -> dict[str, Any]:
        # A pair's options say which pair they are and which part of its options they are.
        if not isinstance(pair, dict):
            return {}
        note = _options_note(pair, offset, shown, len(target), page)
        return {"pair": {key: pair.get(key) for key in _PAIR_KEYS}, "options_note": note}

    head = {**envelope, "page": page, **_page_fields(offset, 0, len(target)), **marked(0)}
    # Room for the pages entry a pair larger than the page adds, and the note's longer numbers.
    room, used = PAGE_CHARS - _json_size(head) - 200, 0
    items: list[Any] = []
    pages: dict[str, Any] = {}
    for index in range(offset, len(target)):
        nested: dict[str, Any] = {}
        if census and not items:
            item = _fit_pair(target[index], room, f"{page}.{index}.", nested)
        elif census or pair_path:
            item = target[index]
        else:
            item = _summary(target[index], nested, f"{page}.{index}.")
        size = _json_size(item) + _json_size(nested)
        if len(items) >= limit or (items and used + size > room):
            break
        items.append(item)
        pages.update(nested)
        used += size + 1
    return {
        **envelope,
        "page": page,
        "items": items,
        **_page_fields(offset, len(items), len(target)),
        **marked(len(items)),
        **({"pages": pages} if pages else {}),
    }


_PAIR_KEYS = ("source_entity", "target_entity")


def _at_path(report: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(report, dict):
            report = report.get(part)
        elif isinstance(report, list) and part.isdigit() and int(part) < len(report):
            report = report[int(part)]
        else:
            return None
    return report


def _mutation_error_result(
    exc: Exception,
    *,
    project_path: str,
    expected_revision: str,
    idempotency_key: str,
    dry_run: bool,
) -> ArchitectMutationResult:
    payload = _report_error(exc)
    details = dict(exc.details or {}) if isinstance(exc, SemanticLayerError) else {}
    current = str(details.get("current_revision", "") or "")
    payload.update(
        {
            "project_path": project_path,
            "expected_revision": expected_revision,
            "base_revision": current,
            "current_revision": current,
            "revision": current,
            "proposed_revision": current,
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
            "dry_run": dry_run,
            "changed_files": [],
            "changes": [],
        }
    )
    return _mutation_result(payload)


def _recovery_hints(exc: Exception) -> list[dict[str, str]]:
    message = str(exc)
    if "does not exist" in message and "Project path" in message:
        return [
            {
                "message": (
                    "Call setup_project_dialog, then create_project, or pass an existing "
                    "project_path inside the Architect MCP workspace root."
                )
            }
        ]
    if "Provide either compare_path or base_ref" in message:
        return [
            {
                "message": (
                    "Call diff_project or impact_project with compare_path for a baseline "
                    "package directory, or base_ref for a git baseline."
                )
            }
        ]
    details = (exc.details or {}) if isinstance(exc, SemanticLayerError) else {}
    if getattr(exc, "code", "") == "OBJECT_NOT_FOUND" and "duckdb_path" in details:
        return [
            {
                "message": (
                    "Call list_tables for the relations in this warehouse, or pass duckdb_path "
                    "(or project_path) for the warehouse that holds the relation."
                )
            }
        ]
    return []


def _package_ref(project: Path) -> PackageReference:
    return PackageReference(source_path=str(project))


def _parse_report(project: Path) -> dict[str, Any]:
    report, _ = parse_config_report(_package_ref(project))
    return report


def _guidance_payload(goal: str = "", project_path: str = "") -> dict[str, Any]:
    return {
        "role": "Architect MCP",
        "goal": goal,
        "project_path": project_path,
        "principles": [
            "Start with project_status before editing an existing package.",
            "Use setup_project_dialog for new-package discovery, then create_project when the required fields are known.",
            "Prefer upsert_model for entity, dimension, time, and measure changes, and upsert_relationship for foreign keys, so graph.yml stays aligned.",
            "Run validate_project with mode=parse after every structural edit; use mode=runtime before promoting.",
            "A LOOKALIKE_MODELS warning names models an agent can read for one another (the same rows, such as a table and its history, or one count under one name on several tables): decide which is canonical, asking the package owner when the data doesn't say, and never from the names alone.",
            "Treat runtime validation as operational: DuckDB validation can build a missing package database from its seed but never replaces an existing file; declare seed kind external for a database another tool builds. Snowflake validation can issue live queries.",
            "Use impact_project with compare_path or base_ref before release review; use promotion_check with compare_path or base_ref when an environment gate matters.",
        ],
        "workflow": [
            {
                "step": "orient",
                "tool": "project_status",
                "result": "Package files, parse health, object counts, and next actions.",
            },
            {
                "step": "plan",
                "tool": "setup_project_dialog",
                "result": "A guided dialog or elicited starter-project answers.",
            },
            {
                "step": "create",
                "tool": "create_project",
                "result": "A runnable schema_version: 1 split package scaffold with examples and tests.",
            },
            {
                "step": "edit",
                "tool": "upsert_model / upsert_relationship / upsert_metric / upsert_segment / upsert_example / upsert_test / write_project_files",
                "result": (
                    "Preview or atomically commit scoped changes with expected_revision "
                    "and a caller-generated idempotency_key; use write_project_files for changes "
                    "across files. Separate calls take the revision the previous write returned."
                ),
            },
            {
                "step": "verify",
                "tool": "validate_project",
                "result": "Parse, runtime, example, package-test, or full release check reports.",
            },
            {
                "step": "review",
                "tool": "impact_project",
                "result": "Behavior-change summary, impacted metrics, reviewer teams, and risk. Requires compare_path or base_ref.",
            },
        ],
        "safety": {
            "workspace_scoped": True,
            "default_transport": "stdio",
            "default_http_port": DEFAULT_ARCHITECT_PORT,
            "optimistic_concurrency": True,
            "cross_process_lock": True,
            "parse_gated_rollback": True,
            "preview_without_project_writes": True,
            "cloud_service_note": "This MCP server does not start, stop, or reconfigure cloud service processes.",
        },
    }


_DIALOG_ARGUMENTS = (
    "description",
    "warehouse",
    "data",
    "default_db",
    "connection_kind",
    "connection_name",
    "connection_options",
    "first_entity",
    "relation",
    "primary_key",
    "time_column",
    "amount_column",
    "dimension_column",
)

# These are the option groups each adapter requires before it can attempt a
# connection. BigQuery/ADC and ClickHouse have usable ambient or local
# defaults; Snowflake's named/direct modes are checked separately.
_REQUIRED_CONNECTION_OPTION_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "databricks": (
        ("host", "host_env"),
        ("http_path", "http_path_env"),
        ("token_env", "token_file"),
    ),
    "motherduck": (("database",), ("token_env", "token_file")),
    "ducklake": (("catalog_path", "catalog_path_env"),),
    "athena": (("region", "region_env"), ("s3_staging_dir", "s3_staging_dir_env")),
}


def _setup_dialog(package_id: str = "", project_path: str = "", goal: str = "") -> dict[str, Any]:
    package_slug = _slug(package_id, fallback="my_semantic_package")
    questions = project_setup_questions(ProjectSpec(package_id=package_slug))
    defaults = {row["id"]: row["default"] for row in questions}
    return {
        "goal": goal,
        "message": (
            "Collect these answers before creating or reshaping a Semantic Rails package. "
            "The draft is for DuckDB only; for another warehouse supply its connection "
            "details and use data=external before calling create_project."
        ),
        "draft_warehouse": "duckdb",
        "questions": questions,
        "recommended_next_tool": "create_project",
        "draft_arguments": _draft_arguments(package_slug, project_path, defaults),
        "next_actions": [NEW_PROJECT_ROUTES_ACTION],
    }


def _draft_arguments(
    package_slug: str, project_path: str, answers: dict[str, Any]
) -> dict[str, Any]:
    draft = {
        "project_path": project_path or f"configs/semantic_rails/{package_slug}",
        "package_id": package_slug,
        **{name: answers.get(name, "") for name in _DIALOG_ARGUMENTS},
        "expected_revision": ABSENT_PROJECT_REVISION,
        "idempotency_key": "<caller-generated-unique-key>",
        "dry_run": True,
    }
    raw_options = draft["connection_options"]
    try:
        options = json.loads(raw_options) if isinstance(raw_options, str) else raw_options
    except json.JSONDecodeError as exc:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS", "connection_options must be a JSON object"
        ) from exc
    if not isinstance(options, dict):
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS", "connection_options must be a JSON object"
        )
    draft["connection_options"] = options
    draft["warehouse"] = str(draft["warehouse"] or "duckdb").strip().lower()
    draft["connection_kind"] = str(draft["connection_kind"] or "").strip().lower()
    draft["connection_name"] = str(draft["connection_name"] or "").strip()
    if draft["warehouse"] != "duckdb":
        draft["data"] = "external"
        draft["default_db"] = ""
    return draft


def _missing_setup_answers(draft: dict[str, Any]) -> list[str]:
    warehouse = draft["warehouse"]
    kind = draft["connection_kind"]
    connector = warehouse_connector(warehouse)
    if connector is None or not connector.adapter:
        return ["supported warehouse"]
    if warehouse == "duckdb":
        return (
            ["remove connection details for DuckDB"]
            if kind or draft["connection_options"] or draft["connection_name"]
            else []
        )
    missing: list[str] = []
    if kind not in connector.connection_kinds:
        missing.append("connection_kind")
    if warehouse != "snowflake" and draft["connection_name"]:
        missing.append("remove unsupported connection_name")
    if connection_option_errors(warehouse, kind, draft["connection_options"]):
        missing.append("valid connection_options")
    options = {
        normalize_connection_option_name(key): value
        for key, value in draft["connection_options"].items()
    }
    # libpq can inherit environment defaults, but the guided Postgres setup
    # requires an explicit option so an empty, ignored profile is not ready.
    if warehouse == "postgres" and not options:
        missing.append("connection_options")
    for keys in _REQUIRED_CONNECTION_OPTION_GROUPS.get(warehouse, ()):
        if not any(isinstance(options.get(key), str) and options[key].strip() for key in keys):
            missing.append(" or ".join(keys))
    if warehouse == "snowflake" and kind == "snowflake_adbc":
        missing.extend(snowflake_adbc_connect_errors(options))
        if draft["connection_name"]:
            missing.append("remove unsupported connection_name")
    elif warehouse == "snowflake" and not draft["connection_name"]:
        if kind == "snowflake_cli":
            missing.append("connection_name")
        elif kind == "snowflake_native" and snowflake_native_direct_connect_errors(options):
            missing.append("connection_name or direct connection_options")
    return missing


def _project_spec(arguments: dict[str, Any]) -> ProjectSpec:
    return ProjectSpec(
        package_id=str(arguments["package_id"]),
        description=str(arguments.get("description") or ""),
        warehouse=ProjectWarehouse(
            kind=str(arguments.get("warehouse") or "duckdb"),
            data=arguments.get("data") or "starter",
            default_db=str(arguments.get("default_db") or ""),
            connection_kind=str(arguments.get("connection_kind") or ""),
            connection_name=str(arguments.get("connection_name") or ""),
            connection_options=dict(arguments.get("connection_options") or {}),
        ),
        first_model=FirstModel(
            entity=str(arguments.get("first_entity") or "event"),
            relation=str(arguments.get("relation") or "raw_events"),
            primary_key=str(arguments.get("primary_key") or "event_id"),
            time_column=str(arguments.get("time_column", "occurred_at")).strip(),
            amount_column=str(arguments.get("amount_column") or ""),
            dimension_column=str(arguments.get("dimension_column") or ""),
        ),
    )


def _project_files(project: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(project.rglob("*")):
        if path.is_file():
            rel = path.relative_to(project).as_posix()
            files.append({"path": rel, "bytes": path.stat().st_size})
    return files


def _resolve_compare_path(compare_path: str, *, workspace_root: Path) -> str:
    if not str(compare_path or "").strip():
        return ""
    return str(_resolve_project_path(compare_path, workspace_root=workspace_root))


def _bind_kind(host: str) -> Literal["loopback", "wildcard", "concrete"]:
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(int(name) if name.isdigit() else name)
    except ValueError:
        return "wildcard" if not name else "concrete"
    if address.is_unspecified:
        return "wildcard"
    return "loopback" if address.is_loopback else "concrete"


def _url_host(host: str) -> str:
    """The host a client should dial for a server bound to ``host``.

    A wildcard bind is dialed over loopback in the same address family: an IPv6
    listener (``::``) does not accept IPv4 connections.
    """
    name = host.strip().strip("[]")
    if _bind_kind(host) == "wildcard":
        return "[::1]" if ":" in name else "127.0.0.1"
    return f"[{name}]" if ":" in name else name


def _transport_security(host: str) -> TransportSecuritySettings:
    """Host/Origin (DNS-rebinding) checks for the network transports.

    Loopback names are always allowed, as in the MCP SDK's loopback default,
    and so is the address the server binds to, unless it is a wildcard
    (``0.0.0.0``, ``::``): then only loopback names pass, e.g. a container
    port-forwarded to localhost. Each name is allowed with and without a port.
    """
    names = list(_LOOPBACK_NAMES)
    if _bind_kind(host) != "wildcard":
        name = _url_host(host)
        if name.lower() not in names:
            names.append(name)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*names, *(f"{name}:*" for name in names)],
        allowed_origins=[*(f"http://{name}" for name in names)]
        + [f"http://{name}:*" for name in names],
    )


def _check_token(token: str) -> str:
    if len(token) < MIN_ARCHITECT_TOKEN_LENGTH:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"the Architect MCP token must be at least {MIN_ARCHITECT_TOKEN_LENGTH} characters; "
            f"{_TOKEN_HINT}",
        )
    if not _TOKEN_PATTERN.fullmatch(token):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "the Architect MCP token may contain only letters, digits and - . _ ~ + / "
            "(optionally ending in =), the characters a bearer token header can carry",
        )
    return token


def load_architect_token(token_file: str = "") -> str:
    """Return the network transports' bearer token, or "" when none is configured.

    Sources, first match wins: ``token_file`` (``--token-file``), the file named
    by ``SEMANTIC_RAILS_ARCHITECT_TOKEN_FILE``, then ``SEMANTIC_RAILS_ARCHITECT_TOKEN``.
    A configured token must be at least 32 characters of RFC 6750 token text.
    """
    path = token_file or os.environ.get(ARCHITECT_TOKEN_FILE_ENV, "")
    if path:
        resolved = Path(path).expanduser()
        try:
            token = resolved.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"cannot read the Architect MCP token file {str(resolved)!r}: "
                f"{exc.strerror or type(exc).__name__}",
            ) from exc
        except UnicodeDecodeError as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"the Architect MCP token file {str(resolved)!r} is not UTF-8 text",
            ) from exc
        if not token:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"the Architect MCP token file {str(resolved)!r} is empty"
            )
        return _check_token(token)
    token = os.environ.get(ARCHITECT_TOKEN_ENV, "").strip()
    return _check_token(token) if token else ""


class _BearerTokenGate:
    """Reject HTTP requests without ``Authorization: Bearer <token>`` before MCP sees them."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = _check_token(token).encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        supplied = b""
        values = [
            value for name, value in scope.get("headers", []) if name.lower() == b"authorization"
        ]
        # More than one Authorization header is ambiguous, so it never authenticates.
        if len(values) == 1 and values[0][:7].lower() == b"bearer ":
            supplied = values[0][7:].strip()
        if not supplied or not hmac.compare_digest(supplied, self._token):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "Missing or invalid bearer token."},
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


class ArchitectMCPServer(FastMCP[Any]):
    """FastMCP whose HTTP apps always sit behind the Architect bearer-token gate.

    Every way to serve Streamable HTTP (``run("streamable-http")``,
    ``streamable_http_app()``) goes
    through these overrides, so none serves without ``bearer_token`` set to a
    valid token.
    """

    bearer_token: str = ""

    async def list_tools(self) -> list[MCPTool]:
        """The tools, without the title pydantic gives each schema and property.

        Titles such as ``"Project Path"`` only repeat the name, and were a
        fifth of the tool list.
        """
        return [
            tool.model_copy(
                update={
                    "inputSchema": _without_titles(tool.inputSchema),
                    "outputSchema": _without_titles(tool.outputSchema),
                }
            )
            for tool in await super().list_tools()
        ]

    def sse_app(self, mount_path: str | None = None) -> Starlette:
        raise SemanticLayerError(
            "INVALID_CONFIG", "SSE transport is unsupported; use stdio or streamable-http."
        )

    def streamable_http_app(self) -> Starlette:
        return self._gated(super().streamable_http_app())

    def _gated(self, app: Starlette) -> Starlette:
        if not self.bearer_token:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "the Architect MCP network transports require a bearer token; "
                "set bearer_token or use run_architect_mcp_server",
            )
        app.add_middleware(_BearerTokenGate, token=_check_token(self.bearer_token))
        return app


def architect_http_app(server: ArchitectMCPServer, token: str) -> Starlette:
    """The ASGI app for a network transport, behind the bearer-token gate."""
    server.bearer_token = _check_token(token)
    return server.streamable_http_app()


def create_architect_mcp_server(
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_ARCHITECT_PORT,
) -> ArchitectMCPServer:
    root = _server_workspace_root(workspace_root)
    mcp = ArchitectMCPServer(
        name="Semantic Rails Architect MCP",
        instructions=(
            "Semantic Rails Architect writes a package's YAML inside the workspace root. A new "
            "package starts with setup_project_dialog, then create_project (expected_revision: "
            "absent).\n"
            "Order: project_status (note its revision); explore a DuckDB warehouse (list_tables, "
            "describe_table, profile_columns, suggest_model, suggest_one_per_key, suggest_links) or "
            "a dbt target "
            "(suggest_models_from_dbt); write (upsert_model, upsert_relationship, upsert_metric, "
            "upsert_segment, upsert_example, upsert_test, import_dbt_project, remove_object, "
            "record_route_decision, upgrade_project); "
            "check (validate_project mode=parse after each change, mode=runtime before trusting "
            "answers; preview_query); review (diff_project, impact_project).\n"
            "Every write previews with dry_run: true and takes expected_revision (from "
            "project_status or the last write) and a new idempotency_key per change; a retry "
            "with the same key replays. Send a change that spans several files as one "
            "write_project_files call. A write the package can't parse is "
            "rolled back, and a stale revision returns CONFIG_CONFLICT. Prefer the typed tools over "
            "write_project_files. Writes, validate_project and project_status answer with a "
            "summary; full_report: true returns every detail. Long lists come in pages: pass "
            "the answer's next_offset as offset for the next, and its revision as "
            "expected_revision to refuse a package that changed in between; a summary's pages "
            "names each list it cut short, which page (its path) pages. This server doesn't "
            "manage cloud services."
        ),
        host=host,
        port=port,
        transport_security=_transport_security(host),
    )

    @mcp.prompt()
    def architect_project_plan(goal: str = "", project_path: str = "") -> str:
        """Create a developer-focused plan for a Semantic Rails package change."""
        return (
            "You are using Semantic Rails Architect MCP.\n"
            f"Goal: {goal or 'Plan a safe semantic package change.'}\n"
            f"Project path: {project_path or '(ask for or discover the package path)'}\n"
            "First call project_status. Then propose the smallest package-file changes, list validation "
            "commands/tools, and identify behavior risks before applying edits."
        )

    @mcp.tool(annotations=_read_only_annotations("Architect guidance"))
    def architect_guidance(goal: str = "", project_path: str = "") -> dict[str, Any]:
        """Return the recommended Architect MCP workflow and safety guidance."""
        return _guidance_payload(goal=goal, project_path=project_path)

    @mcp.tool(annotations=_read_only_annotations("New project dialog"))
    async def setup_project_dialog(
        ctx: Context,
        package_id: str = "",
        project_path: str = "",
        goal: str = "",
        interactive: bool = False,
    ) -> dict[str, Any]:
        """Start a guided project setup dialog, using MCP elicitation when requested and supported."""
        dialog = _setup_dialog(package_id=package_id, project_path=project_path, goal=goal)
        if not interactive:
            return {"ok": True, "mode": "dialog_schema", **dialog}
        try:
            result = await ctx.elicit(
                "Answer these starter-package questions. Architect MCP will return create_project arguments.",
                ProjectSetupAnswers,
            )
        except Exception:  # pragma: no cover - depends on MCP client support
            return {
                **_report_error(
                    SemanticLayerError(
                        "INVALID_MCP_ARGUMENTS", "Project setup answers were not accepted"
                    )
                ),
                "mode": "dialog_schema",
            }
        if result.action != "accept" or result.data is None:
            return {"ok": False, "status": str(result.action), "mode": "elicitation", **dialog}
        answers = result.data.model_dump()
        package_slug = _slug(
            str(answers.get("package_id") or package_id), fallback="semantic_project"
        )
        try:
            draft = _draft_arguments(package_slug, project_path, answers)
        except SemanticLayerError as exc:
            return {**_report_error(exc), "mode": "elicitation"}
        missing = _missing_setup_answers(draft)
        if missing:
            return {
                "ok": False,
                "status": "needs_connection_details",
                "mode": "elicitation",
                "warehouse": draft["warehouse"],
                "required_answers": missing,
                "recommended_next_tool": "setup_project_dialog",
            }
        return {
            "ok": True,
            "mode": "elicitation",
            "recommended_next_tool": "create_project",
            "draft_arguments": draft,
            "next_actions": [NEW_PROJECT_ROUTES_ACTION],
        }

    @mcp.tool(
        annotations=_mutation_annotations("Create Semantic Rails project"),
        description=(
            "Create a strict schema_version: 1 project. DuckDB packages "
            "use a two-row starter CSV (data=starter) or read a database another tool builds, "
            "such as dbt (data=external); other warehouses need connection_kind, with secrets "
            "named by environment variable only."
        ),
    )
    def create_project(
        package_id: str,
        expected_revision: str,
        idempotency_key: str,
        project_path: str = "",
        description: str = "Semantic Rails package managed through Architect MCP.",
        warehouse: str = "duckdb",
        data: Literal["starter", "external"] = "starter",
        default_db: str = "",
        connection_kind: str = "",
        connection_name: str = "",
        connection_options: dict[str, Any] | None = None,
        first_entity: str = "event",
        relation: str = "raw_events",
        primary_key: str = "event_id",
        time_column: str = "occurred_at",
        amount_column: str = "",
        dimension_column: str = "",
        overwrite: bool = False,
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            spec = _project_spec(
                {
                    "package_id": package_id,
                    "description": description,
                    "warehouse": warehouse,
                    "data": data,
                    "default_db": default_db,
                    "connection_kind": connection_kind,
                    "connection_name": connection_name,
                    "connection_options": connection_options,
                    "first_entity": first_entity,
                    "relation": relation,
                    "primary_key": primary_key,
                    "time_column": time_column,
                    "amount_column": amount_column,
                    "dimension_column": dimension_column,
                }
            )
            project = _resolve_project_path(
                project_path,
                workspace_root=root,
                package_id=package_id,
                require_exists=False,
                require_package_root=False,
            )
            return _mutation_result(
                create_project_service(
                    project,
                    spec,
                    workspace_root=root,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    overwrite=overwrite,
                    dry_run=dry_run,
                ).report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path
                or f"configs/semantic_rails/{_slug(package_id, fallback='semantic_project')}",
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_read_only_annotations("Project status", open_world=True),
        description=(
            "Return the package's revision (pass it as expected_revision), files and parse "
            "report. Use first, and again when the package may have changed. Gotcha: "
            "include_runtime_checks also runs runtime validation, examples and tests, which query "
            "the warehouse."
        ),
    )
    def project_status(
        project_path: str,
        include_runtime_checks: bool = False,
        full_report: FullReport = False,
        page: Page = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        try:
            _check_report_paging(page, offset, full_report)
            project = _resolve_project_path(project_path, workspace_root=root)
            revision = _read_revision(project, expected_revision)
            parse = _parse_report(project)
            census = parse.pop("route_census", None)
            needed = sum(bool(row["needed_by"]) for row in census["undecided"]) if census else 0
            lookalikes = sum(
                isinstance(row, dict) and row.get("code") == LOOKALIKE_MODELS
                for row in parse.get("warnings") or []
            )
            out: dict[str, Any] = {
                "ok": bool(parse.get("ok")),
                "project_path": str(project),
                "workspace_root": str(root),
                "revision": revision,
                "files": _project_files(project),
                "parse": parse,
                "route_census": census,
                "next_actions": [
                    "Fix parse errors first.",
                    *legacy_form_hint(project, _UPGRADE_ACTION),
                ]
                if not parse.get("ok")
                else [
                    *(
                        [
                            f"Decide the {needed} join route{'s' if needed > 1 else ''} the "
                            "package's own metrics, examples or tests need: the first pairs in "
                            "route_census.undecided, each naming them in needed_by (a summary "
                            "lists the first that fit whole; page: route_census.undecided lists "
                            "the rest). "
                            f"{DECIDE_ROUTES_ACTION}"
                        ]
                        if needed
                        else []
                    ),
                    *(
                        [
                            f"Name the canonical model of each of the {lookalikes} "
                            f"group{'s' if lookalikes > 1 else ''} of look-alike models in "
                            "parse.warnings (code LOOKALIKE_MODELS; page: parse.warnings lists "
                            "them all): an agent can answer one question from any model of a "
                            "group."
                        ]
                        if lookalikes
                        else []
                    ),
                    "Run validate_project with mode=runtime before release.",
                    "For release review, run impact_project with compare_path or base_ref.",
                ],
            }
            if include_runtime_checks:
                out["runtime"] = validate_config_report(_package_ref(project))
                out["examples"] = run_examples_report(_package_ref(project))
                out["tests"] = run_package_tests_report(_package_ref(project))
                out["ok"] = bool(
                    out["runtime"].get("ok")
                    and out["examples"].get("ok")
                    and out["tests"].get("ok")
                )
            if page:
                envelope = {key: out[key] for key in ("ok", "project_path", "revision")}
                return _report_page(out, page, offset, limit, envelope)
            return _full_or_summary(out, full_report)
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("List project files"))
    def list_project_files(
        project_path: str,
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        """List files inside a Semantic Rails project directory."""
        try:
            _check_list_paging(offset, full_report, "file")
            project = _resolve_project_path(project_path, workspace_root=root)
            payload = {
                "ok": True,
                "project_path": str(project),
                "revision": _read_revision(project, expected_revision),
                "files": _project_files(project),
            }
            return _paged(payload, "files", offset, limit, full_report, "files")
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("Read project file"))
    def read_project_file(project_path: str, relative_path: str) -> dict[str, Any]:
        """Read a UTF-8 project file by relative path."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            path = _resolve_relative_path(project, relative_path)
            return {
                "ok": True,
                "project_path": str(project),
                "revision": project_revision(project),
                "relative_path": path.relative_to(project).as_posix(),
                "content": path.read_text(encoding="utf-8"),
            }
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_mutation_annotations("Write project files"))
    def write_project_files(
        project_path: str,
        files: list[dict[str, Any]],
        expected_revision: str,
        idempotency_key: str,
        reason: str = "",
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        """Write or archive files in one transaction, validating the final package as a whole."""
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .write_files(
                    files,
                    reason=reason,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_mutation_annotations("Upgrade project"),
        description=(
            "Rewrite a package's legacy forms to the current ones in one transaction, "
            "refusing any rewrite that changes an answer; dry_run (the default) previews the "
            "rules, proof and diff, and choices ({key: option}) answers pending choices."
        ),
    )
    def upgrade_project(
        project_path: str,
        dry_run: bool = True,
        choices: dict[str, str] | None = None,
        expected_revision: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        try:
            return upgrade_project_service(
                _resolve_project_path(
                    project_path, workspace_root=root, require_package_root=False
                ),
                workspace_root=root,
                dry_run=dry_run,
                choices=choices,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_mutation_annotations("Upsert semantic model"),
        description=(
            "Upsert a model and its graph entity. calendar: true makes "
            'it the package calendar for calendar_id (default "default", which a package with '
            "calendars needs): its columns are ordinary dimensions; time.fill and every time "
            "bucket use the implicit Gregorian calendar. calendar: false reverts that. "
            "On a regular model, calendar_id binds its times to a calendar; a non-default "
            "calendar is refused in this release. Fields merge into "
            "an existing model. An existing dimension, time or measure given only label, "
            "description, synonyms or meta keeps its other fields; given anything else it is "
            "rewritten, and dropped_fields lists what that drops. replace: true rewrites the "
            "model from the arguments, keeping only its id, entities, calendar_id, "
            "zero_through (how far its tables are loaded) and one_per_key, and lists "
            "what it drops in dropped_fields. one_per_key: true reads a relation whose rows "
            "repeat the key as one row per primary_key, each column the key's one value (NULL "
            "where its rows disagree); keep the columns that vary on a row-grain model of the "
            "same relation. primary_key [] makes a row-grain model (no key); entities "
            "({parent: {} or {expr: column}}) merges the model's parent entities. "
            "Relationships also go through upsert_relationship."
        ),
    )
    def upsert_model(
        project_path: str,
        model_id: str,
        entity_key: str,
        relation: str,
        primary_key: list[str],
        expected_revision: str,
        idempotency_key: str,
        dimensions: dict[str, Any] | None = None,
        times: dict[str, Any] | None = None,
        measures: dict[str, Any] | None = None,
        group: str = "core",
        description: str = "",
        label: str = "",
        calendar: bool | None = None,
        calendar_id: str = "",
        entities: dict[str, Any] | None = None,
        replace: bool = False,
        one_per_key: bool | None = None,
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_model(
                    model_id=model_id,
                    entity_key=entity_key,
                    relation=relation,
                    primary_key=primary_key,
                    dimensions=dimensions,
                    times=times,
                    measures=measures,
                    group=group,
                    description=description,
                    label=label,
                    calendar=calendar,
                    calendar_id=calendar_id,
                    entities=entities,
                    replace=replace,
                    one_per_key=one_per_key,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_mutation_annotations("Upsert relationship"),
        description=(
            "Relate two entities: columns on from_entity's model hold "
            "to_entity's key, in key order. cardinality: many_to_one or one_to_one. from_entity "
            "may be a row-grain model's id. name writes graph.relationships.<name> with these "
            "columns as its via: a second role on the pair (a buyer and a seller), or that role "
            "again; a pair with two roles needs it."
            " Set keep_existing_routes=true to record every moved pair's previous route "
            "in this transaction; otherwise unrecorded route changes refuse the write."
        ),
    )
    def upsert_relationship(
        project_path: str,
        from_entity: str,
        to_entity: str,
        columns: list[str],
        expected_revision: str,
        idempotency_key: str,
        cardinality: str = "many_to_one",
        name: str = "",
        keep_existing_routes: bool = False,
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_relationship(
                    from_entity=from_entity,
                    to_entity=to_entity,
                    columns=columns,
                    cardinality=cardinality,
                    name=name,
                    keep_existing_routes=keep_existing_routes,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_mutation_annotations("Record route decision"),
        description=(
            "Record which route a question between two entities means, as the package "
            "default: write or replace the pair's graph.path_preferences row (pass the "
            "decision of an AMBIGUOUS_PATH clarification option). An off-route path or an "
            "unknown entity or relationship is INVALID_CONFIG with nothing written. Returns "
            "replaced (the previous row) and summary, one sentence for the review."
            " For several pairs, pass decisions=[{source_entity, target_entity, "
            "relationship_path, label?}, ...] instead of single-pair fields. All rows are "
            "validated together in one transaction; one invalid or conflicting row refuses all."
        ),
    )
    def record_route_decision(
        project_path: str,
        expected_revision: str,
        idempotency_key: str,
        relationship_path: Annotated[list[str], Field(default_factory=list)],
        source_entity: str = "",
        target_entity: str = "",
        label: str = "",
        decisions: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .record_route_decision(
                    source_entity=source_entity or None,
                    target_entity=target_entity or None,
                    relationship_path=relationship_path or None,
                    label=label,
                    decisions=decisions,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_mutation_annotations("Upsert metric"),
        description=(
            "Upsert a metric from spec (kind, measure or inputs, value_type, label, synonyms, description). "
            "A new metric goes in metrics/<file_name>, or metrics/<group>/<metric_key>.yml; an "
            "existing one stays in its file. Fields merge; replace: true rewrites it, keeping its "
            "id. Gotcha: strict packages need an explicit value_type."
        ),
    )
    def upsert_metric(
        project_path: str,
        metric_key: str,
        spec: Annotated[
            dict[str, Any],
            Field(
                json_schema_extra={
                    "properties": {
                        "synonyms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Other declared names for this metric.",
                        }
                    },
                }
            ),
        ],
        expected_revision: str,
        idempotency_key: str,
        file_name: str = "",
        group: str = "core",
        replace: bool = False,
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_metric(
                    metric_key=metric_key,
                    spec=spec,
                    file_name=file_name,
                    group=group,
                    replace=replace,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_mutation_annotations("Upsert segment"),
        description=(
            "Upsert a segment in segments/<file_name>: entity, basis_metric, label, membership. "
            "Fields merge; replace: true rewrites it, keeping its id. Gotcha: membership needs "
            "where, metric_filters or a time window, and fields outside membership: are refused."
        ),
    )
    def upsert_segment(
        project_path: str,
        segment_key: str,
        spec: dict[str, Any],
        expected_revision: str,
        idempotency_key: str,
        file_name: str = "core.yml",
        replace: bool = False,
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_segment(
                    segment_key=segment_key,
                    spec=spec,
                    file_name=file_name,
                    replace=replace,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    def _upsert_check(
        kind: str, project_path: str, full_report: bool, **arguments: Any
    ) -> ArchitectMutationResult:
        try:
            project = ArchitectProject(project_path, workspace_root=root)
            return _mutation_result(
                project.upsert_check(kind=kind, **arguments).report, full_report=full_report
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=arguments["expected_revision"],
                idempotency_key=arguments["idempotency_key"],
                dry_run=arguments["dry_run"],
            )

    @mcp.tool(
        annotations=_mutation_annotations("Upsert example"),
        description=(
            "Upsert an example question in examples/<file_name>: spec "
            "has query, and optionally question and expected_shape (columns, min_rows, "
            "max_rows). The query must validate. spec merges into an existing example."
        ),
    )
    def upsert_example(
        project_path: str,
        example_key: str,
        spec: dict[str, Any],
        expected_revision: str,
        idempotency_key: str,
        file_name: str = "core.yml",
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        return _upsert_check(
            "example",
            project_path,
            full_report,
            key=example_key,
            spec=spec,
            file_name=file_name,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    @mcp.tool(
        annotations=_mutation_annotations("Upsert package test"),
        description=(
            "Upsert a package test in tests/<file_name>. spec.kind is "
            "query_returns_columns (query, columns), query_row_count_bounds (query, min_rows "
            "and/or max_rows), query_matches_snapshot (query, expected_rows), "
            "validate_fails_with_code (query, code), explain_contains (query, text) or "
            "metric_equals_query (metric_query, expected_query). Queries must validate; a "
            "validate_fails_with_code query must fail with its code. spec merges into an "
            "existing test."
        ),
    )
    def upsert_test(
        project_path: str,
        test_key: str,
        spec: dict[str, Any],
        expected_revision: str,
        idempotency_key: str,
        file_name: str = "core.yml",
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        return _upsert_check(
            "test",
            project_path,
            full_report,
            key=test_key,
            spec=spec,
            file_name=file_name,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    @mcp.tool(
        annotations=_read_only_annotations("Preview query", open_world=True),
        description=(
            "Run a semantic query on the package's warehouse, as the query server's execute "
            "does, and return at most max_rows rows (1-200); truncated is true when there are more, "
            "which are not counted (total_row_count null). Values are real warehouse data."
        ),
    )
    def preview_query(
        project_path: str,
        query: dict[str, Any],
        max_rows: Annotated[int, Field(ge=1, le=MAX_PREVIEW_ROWS)] = 20,
    ) -> dict[str, Any]:
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            adapter = SemanticLayerMCPAdapter.from_path(str(project))
            try:
                result = adapter.call_tool("execute", {"query": query, "max_rows": max_rows})
            finally:
                adapter.close()
            return dict(json.loads(json_text(result)))
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_mutation_annotations("Remove object"),
        description=(
            "Remove a model, dimension, time, measure, metric, segment or "
            "relationship (a model's foreign key, keyed by its entity), archiving its YAML. model "
            "picks the model when several hold the key. A model takes its entity and relationships "
            "along. Refused if a metric would still name it; impact lists the behavior changes "
            "and the files still naming a removed id."
        ),
    )
    def remove_object(
        project_path: str,
        kind: str,
        key: str,
        expected_revision: str,
        idempotency_key: str,
        model: str = "",
        reason: str = "",
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .remove_object(
                    kind=kind,
                    key=key,
                    model=model,
                    reason=reason,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report,
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    def _warehouse_path(project_path: str, duckdb_path: str) -> str:
        if bool(str(project_path or "").strip()) == bool(str(duckdb_path or "").strip()):
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "Pass exactly one of project_path (a DuckDB package) or duckdb_path",
            )
        if project_path:
            project = _resolve_project_path(project_path, workspace_root=root)
            path = Path(introspection.package_duckdb_path(project)).resolve()
        else:
            raw = Path(duckdb_path).expanduser()
            path = (raw if raw.is_absolute() else root / raw).resolve()
        if not _within(path, root):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect MCP only reads databases inside its configured workspace root",
                details={"workspace_root": str(root), "requested_path": str(path)},
            )
        return str(path)

    @mcp.tool(annotations=_read_only_annotations("List warehouse tables"))
    def list_tables(
        project_path: str = "",
        duckdb_path: str = "",
        schema: str = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
    ) -> dict[str, Any]:
        """List tables and views (read-only) in a DuckDB package's database or a DuckDB file."""
        try:
            _check_list_paging(offset, full_report, "table")
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                tables = introspection.list_tables(warehouse, schema=schema)
            return _paged(
                {"ok": True, "tables": tables}, "tables", offset, limit, full_report, "tables"
            )
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("Describe warehouse table"))
    def describe_table(
        relation: str,
        project_path: str = "",
        duckdb_path: str = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
    ) -> dict[str, Any]:
        """Columns (type, nullability, default) and declared primary, unique and foreign keys."""
        try:
            _check_list_paging(offset, full_report, "column")
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                described = {"ok": True, **introspection.describe_table(warehouse, relation)}
            return _paged(described, "columns", offset, limit, full_report, "columns")
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_read_only_annotations("Profile table columns"),
        description=(
            "Per-column counts, min/max and up to 20 samples (sample_limit=0 for none), sampling "
            "tables above max_rows (at most one million). Read-only."
        ),
    )
    def profile_columns(
        relation: str,
        columns: list[str] | None = None,
        sample_limit: int = 5,
        max_rows: int = introspection.DEFAULT_PROFILE_ROWS,
        project_path: str = "",
        duckdb_path: str = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
    ) -> dict[str, Any]:
        try:
            _check_list_paging(offset, full_report, "column")
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                described = introspection.describe_table(warehouse, relation)
                names = [column["name"] for column in described["columns"]]
                wanted = list(columns or names)
                if full_report or set(wanted) - set(names):
                    # Every column, or the refusal naming the unknown ones.
                    chunk = wanted
                else:
                    _check_offset(offset, len(wanted), "columns")
                    # Profile only this page's columns: what fits is known once they are.
                    chunk = wanted[offset : offset + min(limit, PROFILE_PAGE_COLUMNS)]
                profile = introspection.profile_columns(
                    warehouse, relation, chunk, sample_limit=sample_limit, max_rows=max_rows
                )
            if full_report:
                return _paged({"ok": True, **profile}, "columns", 0, limit, True, "columns")
            page = _paged({"ok": True, **profile}, "columns", 0, limit, False, "columns")
            return {**page, **_page_fields(offset, len(page["columns"]), len(wanted))}
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_read_only_annotations("Suggest a model"),
        description=(
            "Propose a key, times, dimensions, measures and foreign keys for a relation, with "
            "confidences, reasons and draft upsert_model arguments. Pages hold times, then "
            "dimensions, then measures, and a draft of those; drafts merge, and full_report: true "
            "gives one. Read-only."
        ),
    )
    def suggest_model(
        relation: str,
        project_path: str = "",
        duckdb_path: str = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
    ) -> dict[str, Any]:
        try:
            _check_list_paging(offset, full_report, "suggestion")
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                suggestion = {"ok": True, **introspection.suggest_model(warehouse, relation)}
            return _paged_suggestion(suggestion, offset, limit, full_report)
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_read_only_annotations("Suggest a one-per-key model"),
        description=(
            "For a table with no key of its own, propose reading it one row per a key it repeats "
            "(one_per_key): per candidate key, the rows, NULL-key rows, distinct keys, keys with "
            "several rows, and each column fixed or varying within a key; the proposal's "
            "coverage, draft upsert_model arguments and, with project_path, the "
            "upsert_relationship that links the table's row-grain model to it. key tries one "
            "column set. Read-only: apply the drafts with dry_run, then expected_revision."
        ),
    )
    def suggest_one_per_key(
        relation: str,
        key: list[str] | None = None,
        project_path: str = "",
        duckdb_path: str = "",
    ) -> dict[str, Any]:
        try:
            context = (
                ArchitectProject(project_path, workspace_root=root).one_per_key_context(relation)
                if project_path
                else {}
            )
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                return {
                    "ok": True,
                    **introspection.suggest_one_per_key(warehouse, relation, key, **context),
                }
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_read_only_annotations("Suggest relationships"),
        description=(
            "Propose relationships from a table to other tables' keys from their values: per "
            "candidate column, the distinct values and rows found among the target's keys "
            "(coverage), a few not found, and the cardinality. proposals reach min_coverage "
            "(default 0.95, stated as threshold); below_threshold lists the rest with the same "
            "evidence for you to decide. columns tries those columns against every key whatever "
            "their names. With project_path the targets are the package's entities and each "
            "carries draft upsert_relationship arguments. Pages hold proposals, then the rest; "
            "counts totals each. Read-only."
        ),
    )
    def suggest_links(
        relation: str,
        columns: list[str] | None = None,
        min_coverage: float = introspection.DEFAULT_LINK_COVERAGE,
        project_path: str = "",
        duckdb_path: str = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        try:
            _check_list_paging(offset, full_report, "candidate")
            if expected_revision and not project_path:
                raise SemanticLayerError(
                    "INVALID_MCP_ARGUMENTS",
                    "expected_revision checks the package project_path names: pass "
                    "project_path, or drop expected_revision",
                    details={"expected_revision": expected_revision},
                )
            package: dict[str, Any] = {}
            context: dict[str, Any] = {}
            if project_path:
                project = ArchitectProject(project_path, workspace_root=root)
                package["revision"] = _read_revision(project.project_path, expected_revision)
                context = project.link_context(relation)
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                links = introspection.suggest_links(
                    warehouse, relation, columns, min_coverage=min_coverage, **context
                )
            payload = {
                "ok": True,
                **package,
                **links,
                "counts": {bucket: len(links[bucket]) for bucket in _LINK_BUCKETS},
            }
            return _paged_groups(payload, _LINK_BUCKETS, offset, limit, full_report, "candidates")[
                0
            ]
        except Exception as exc:
            return _report_error(exc)

    def _workspace_file(value: str, *, argument: str) -> Path:
        raw = Path(value).expanduser()
        path = (raw if raw.is_absolute() else root / raw).resolve()
        if not _within(path, root):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"Architect MCP only reads {argument} inside its configured workspace root",
                details={"workspace_root": str(root), "requested_path": str(path)},
            )
        return path

    def _dbt_project(target_dir: str, manifest_path: str, catalog_path: str) -> Any:
        if not (target_dir or manifest_path):
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "Pass target_dir (dbt's target/ directory) or manifest_path",
            )
        target = _workspace_file(target_dir, argument="target_dir") if target_dir else None
        if manifest_path:
            manifest = _workspace_file(manifest_path, argument="manifest_path")
        else:
            assert target is not None  # target_dir or manifest_path is required above
            manifest = _workspace_file(str(target / "manifest.json"), argument="manifest_path")
        catalog = None
        if catalog_path:
            catalog = _workspace_file(catalog_path, argument="catalog_path")
        elif target is not None and (target / "catalog.json").exists():
            catalog = _workspace_file(str(target / "catalog.json"), argument="catalog_path")
        return dbt_artifacts.load_dbt_artifacts(
            manifest_path=manifest,
            catalog_path=catalog,
        )

    @mcp.tool(
        annotations=_read_only_annotations("Suggest models from dbt"),
        description=(
            "suggest_model for each dbt model, from manifest.json and catalog.json (dbt never "
            "runs); keys, links and value sets come from dbt tests and contracts. select narrows "
            "by model name. Read-only."
        ),
    )
    def suggest_models_from_dbt(
        target_dir: str = "",
        manifest_path: str = "",
        catalog_path: str = "",
        select: list[str] | None = None,
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        full_report: FullList = False,
    ) -> dict[str, Any]:
        try:
            _check_list_paging(offset, full_report, "model")
            project = _dbt_project(target_dir, manifest_path, catalog_path)
            payload = {
                "ok": True,
                "dbt_project": project.project_name,
                "adapter_type": project.adapter_type,
                "models": dbt_artifacts.suggest_models_from_dbt(project, list(select or [])),
                "dbt_warnings": project.warnings,
            }
            return _paged(payload, "models", offset, limit, full_report, "models")
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_mutation_annotations("Import dbt models"),
        description=(
            "Create or update package models from the selected dbt models in one transaction, "
            "writing their foreign keys as entity references. Review with "
            "suggest_models_from_dbt first; dry_run=true previews without writing. "
            "skipped_models and skipped_references say what was left out."
        ),
    )
    def import_dbt_project(
        project_path: str,
        select: list[str],
        expected_revision: str,
        idempotency_key: str,
        target_dir: str = "",
        manifest_path: str = "",
        catalog_path: str = "",
        group: str = "dbt",
        dry_run: bool = False,
        full_report: FullReport = False,
    ) -> ArchitectMutationResult:
        try:
            dbt = _dbt_project(target_dir, manifest_path, catalog_path)
            project = ArchitectProject(project_path, workspace_root=root)
            owners = {row["key"]: row["model_key"] for row in project.inventory()["measures"]}
            items, skipped, unresolved = dbt_artifacts.dbt_import_models(
                dbt, list(select or []), owners
            )
            if not items:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "none of the selected dbt models has a key to import",
                    details={"skipped_models": skipped},
                )
            report = project.upsert_models(
                # Existing objects stay as authored: the draft would revert what an
                # author changed. The flag is part of each item, so replays still match.
                [{**item, "keep_existing": True} for item in items],
                group=group,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            ).report
            return _mutation_result(
                {
                    **report,
                    "skipped_references": [*report.get("skipped_references", []), *unresolved],
                    "skipped_models": skipped,
                    "dbt_warnings": dbt.warnings,
                },
                full_report=full_report,
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(
        annotations=_read_only_annotations("Validate project", open_world=True),
        description=(
            "Validate the package: mode parse, runtime, examples, tests, impact or release. Use "
            "parse after each change and runtime (compiles and queries every measure and metric) "
            "before trusting answers. Gotcha: runtime, examples, tests and release query the "
            "warehouse and may build a seeded DuckDB file; impact needs compare_path or base_ref."
        ),
    )
    def validate_project(
        project_path: str,
        mode: str = "parse",
        environment: str = "",
        compare_path: str = "",
        base_ref: str = "",
        full_report: FullReport = False,
        page: Page = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        try:
            _check_report_paging(page, offset, full_report)
            project = _resolve_project_path(project_path, workspace_root=root)
            revision = _read_revision(project, expected_revision)
            ref = _package_ref(project)
            check = str(mode or "parse").strip().lower()
            if check == "parse":
                report = _parse_report(project)
            elif check == "runtime":
                report = validate_config_report(ref)
            elif check == "examples":
                report = run_examples_report(ref)
            elif check == "tests":
                report = run_package_tests_report(ref)
            elif check == "impact":
                report = impact_report(
                    ref,
                    compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                    base_ref=base_ref,
                )
            elif check in {"release", "promotion", "promote"}:
                if not environment:
                    raise SemanticLayerError(
                        "INVALID_CONFIG", "environment is required for release validation"
                    )
                report = promote_package_report(
                    ref,
                    environment=environment,
                    compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                    base_ref=base_ref,
                )
            else:
                raise SemanticLayerError(
                    "INVALID_CONFIG", "Unsupported validation mode", details={"mode": mode}
                )
            return _report_or_page(report, project, revision, full_report, page, offset, limit)
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("Diff project"))
    def diff_project(
        project_path: str,
        compare_path: str = "",
        base_ref: str = "",
        full_report: FullReport = False,
        page: Page = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        """Diff one project against another path or a git base ref."""
        try:
            _check_report_paging(page, offset, full_report)
            project = _resolve_project_path(project_path, workspace_root=root)
            revision = _read_revision(project, expected_revision)
            report = diff_package_report(
                _package_ref(project),
                compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                base_ref=base_ref,
            )
            return _report_or_page(report, project, revision, full_report, page, offset, limit)
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("Impact of a change"))
    def impact_project(
        project_path: str,
        compare_path: str = "",
        base_ref: str = "",
        full_report: FullReport = False,
        page: Page = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        """Return behavior-change impact, reviewer teams, and risk for a package change."""
        try:
            _check_report_paging(page, offset, full_report)
            project = _resolve_project_path(project_path, workspace_root=root)
            revision = _read_revision(project, expected_revision)
            report = impact_report(
                _package_ref(project),
                compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                base_ref=base_ref,
            )
            return _report_or_page(report, project, revision, full_report, page, offset, limit)
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_read_only_annotations("Promotion check", open_world=True),
        description=(
            "Return whether the package is ready for an environment: parse, runtime, examples, "
            "tests. Pass compare_path or base_ref to include impact. Gotcha: it queries the "
            "warehouse."
        ),
    )
    def promotion_check(
        project_path: str,
        environment: str,
        compare_path: str = "",
        base_ref: str = "",
        full_report: FullReport = False,
        page: Page = "",
        offset: Offset = 0,
        limit: Limit = PAGE_ITEMS,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        try:
            _check_report_paging(page, offset, full_report)
            project = _resolve_project_path(project_path, workspace_root=root)
            revision = _read_revision(project, expected_revision)
            report = promote_package_report(
                _package_ref(project),
                environment=environment,
                compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                base_ref=base_ref,
            )
            return _report_or_page(report, project, revision, full_report, page, offset, limit)
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("Client configuration"))
    def mcp_client_config(
        transport: str = "stdio", host: str = "127.0.0.1", port: int = DEFAULT_ARCHITECT_PORT
    ) -> dict[str, Any]:
        """Return copy-ready client configuration hints for running Architect MCP."""
        selected = str(transport or "stdio")
        if selected not in {"stdio", "streamable-http"}:
            return _report_error(
                SemanticLayerError(
                    "INVALID_CONFIG",
                    "Unsupported Architect MCP transport",
                    details={"transport": selected},
                )
            )
        command = [sys.executable, "-m", "semantic_rails.architect_mcp", "--transport", selected]
        if selected != "stdio":
            command.extend(["--host", host, "--port", str(port)])
        command.extend(["--workspace-root", str(root)])
        stdio_args = [
            "-m",
            "semantic_rails.architect_mcp",
            "--transport",
            "stdio",
            "--workspace-root",
            str(root),
        ]
        http_args = [
            "-m",
            "semantic_rails.architect_mcp",
            "--transport",
            "streamable-http",
            "--host",
            host,
            "--port",
            str(port),
            "--workspace-root",
            str(root),
        ]
        # Network transports require a bearer token. The server and the client
        # both read it from the environment; this payload never carries it.
        auth_headers = {"Authorization": f"Bearer ${{{ARCHITECT_TOKEN_ENV}}}"}
        return {
            "ok": True,
            "server_name": "semantic-rails-architect",
            "transport": selected,
            "command": command,
            "cwd": str(root),
            "workspace_root": str(root),
            "stdio": {
                "command": sys.executable,
                "args": stdio_args,
                "cwd": str(root),
            },
            "http": {
                "url": f"http://{_url_host(host)}:{port}/mcp",
                "command": [sys.executable, *http_args],
                "cwd": str(root),
                "headers": auth_headers,
            },
            "auth": {
                "required_for": ["streamable-http"],
                "token_env": ARCHITECT_TOKEN_ENV,
                "token_file_env": ARCHITECT_TOKEN_FILE_ENV,
                "min_length": MIN_ARCHITECT_TOKEN_LENGTH,
            },
            "note": (
                "Use port 8010 by default so the Architect MCP does not collide with the query MCP "
                f"or local semantic-rails API. Streamable HTTP needs {ARCHITECT_TOKEN_ENV} (or a token "
                "file) set for both the server and the client."
            ),
        }

    return mcp


def run_architect_mcp_server(
    *,
    transport: ArchitectTransport = "stdio",
    host: str = "127.0.0.1",
    port: int = DEFAULT_ARCHITECT_PORT,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    token_file: str = "",
) -> None:
    if transport not in {"stdio", "streamable-http"}:
        raise SemanticLayerError("INVALID_CONFIG", "Unsupported Architect MCP transport")
    server = create_architect_mcp_server(workspace_root=workspace_root, host=host, port=port)
    if transport == "stdio":
        server.run("stdio")
        return
    token = load_architect_token(token_file)
    if not token:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"the {transport} transport requires a bearer token of at least "
            f"{MIN_ARCHITECT_TOKEN_LENGTH} characters: pass --token-file, or set "
            f"{ARCHITECT_TOKEN_FILE_ENV} or {ARCHITECT_TOKEN_ENV}; {_TOKEN_HINT}. "
            "Clients send it as 'Authorization: Bearer <token>'.",
        )
    import uvicorn

    uvicorn.run(
        architect_http_app(server, token),
        host=host,
        port=port,
        log_level=server.settings.log_level.lower(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="semantic-rails-architect-mcp")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_ARCHITECT_PORT)
    parser.add_argument("--workspace-root", default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument(
        "--token-file",
        default="",
        help=(
            "file holding the bearer token the network transports require "
            f"(or set {ARCHITECT_TOKEN_FILE_ENV} or {ARCHITECT_TOKEN_ENV})"
        ),
    )
    args = parser.parse_args()
    transport: ArchitectTransport = args.transport
    try:
        run_architect_mcp_server(
            transport=transport,
            host=args.host,
            port=args.port,
            workspace_root=args.workspace_root,
            token_file=args.token_file,
        )
    except SemanticLayerError as exc:
        parser.error(str(exc))


if __name__ == "__main__":  # pragma: no cover
    main()
