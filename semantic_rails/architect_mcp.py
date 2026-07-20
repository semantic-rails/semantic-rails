"""Developer-facing MCP server for Semantic Rails package authoring.

This module exposes the Architect MCP tool surface used to create package
scaffolds, edit project YAML, and run parse/runtime/release validations from
MCP clients while keeping writes scoped to a configured workspace root.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .architect_service import ArchitectProject
from .architect_transactions import (
    ABSENT_PROJECT_REVISION,
    ProjectFileUpdate,
    ProjectTransaction,
    project_revision,
)
from .config import repo_root
from .config_validation import PackageReference, parse_config_report, validate_config_report
from .errors import SemanticLayerError
from .package_tools import (
    diff_package_report,
    impact_report,
    promote_package_report,
    run_examples_report,
    run_package_tests_report,
)

DEFAULT_ARCHITECT_PORT = 8010
DEFAULT_WORKSPACE_ROOT = repo_root()
ARCHITECT_INTERFACE_VERSION = "v1"
ArchitectTransport = Literal["stdio", "sse", "streamable-http"]


class ProjectSetupAnswers(BaseModel):
    package_id: str = Field(
        description="Lowercase package directory name, for example analytics_core."
    )
    description: str = Field(default="Semantic Rails package managed through Architect MCP.")
    first_entity: str = Field(default="event", description="Business entity to model first.")
    relation: str = Field(
        default="raw_events", description="Warehouse table or CSV-derived relation name."
    )
    primary_key: str = Field(default="event_id")
    time_column: str = Field(default="occurred_at")
    amount_column: str = Field(default="amount")


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
    error: ArchitectMutationIssue | None = None
    errors: list[ArchitectMutationIssue] = Field(default_factory=list)


def _mutation_annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )


def _slug(value: str, *, fallback: str = "semantic_project") -> str:
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "")).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or fallback


def _title(value: str) -> str:
    return (
        " ".join(part.capitalize() for part in str(value or "").replace("_", " ").split()) or value
    )


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
        raw = f"configs/semantic_rails/{_slug(package_id)}"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    path = path.resolve()
    if not _within(path, workspace_root):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Architect MCP only writes inside its configured workspace root",
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


def _mutation_result(payload: dict[str, Any]) -> ArchitectMutationResult:
    return ArchitectMutationResult.model_validate(payload)


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
            "Prefer upsert_model for entity, dimension, time, measure, and join changes so graph.yml stays aligned.",
            "Run validate_project with mode=parse after every structural edit; use mode=runtime before promoting.",
            "Treat runtime validation as operational: DuckDB validation can create or refresh the package database, and Snowflake validation can issue live queries.",
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
                "tool": "upsert_model / upsert_metric / upsert_segment / write_project_file",
                "result": (
                    "Preview or atomically commit scoped changes with expected_revision "
                    "and a caller-generated idempotency_key."
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


def _setup_dialog(package_id: str = "", project_path: str = "", goal: str = "") -> dict[str, Any]:
    package_slug = _slug(package_id, fallback="my_semantic_package")
    return {
        "goal": goal,
        "message": "Collect these answers before creating or reshaping a Semantic Rails package.",
        "questions": [
            {
                "id": "package_id",
                "prompt": "What package directory name should be used?",
                "default": package_slug,
            },
            {
                "id": "description",
                "prompt": "What business domain does this package govern?",
                "default": "Semantic Rails package managed through Architect MCP.",
            },
            {
                "id": "first_entity",
                "prompt": "What is the first business entity to model?",
                "default": "event",
            },
            {
                "id": "relation",
                "prompt": "Which warehouse table or CSV-derived relation backs that entity?",
                "default": "raw_events",
            },
            {
                "id": "primary_key",
                "prompt": "Which column uniquely identifies one row/entity?",
                "default": "event_id",
            },
            {
                "id": "time_column",
                "prompt": "Which timestamp/date column anchors the first metric?",
                "default": "occurred_at",
            },
            {
                "id": "amount_column",
                "prompt": "Which numeric column should become the starter flow metric?",
                "default": "amount",
            },
        ],
        "recommended_next_tool": "create_project",
        "draft_arguments": {
            "project_path": project_path or f"configs/semantic_rails/{package_slug}",
            "package_id": package_slug,
            "description": "Semantic Rails package managed through Architect MCP.",
            "first_entity": "event",
            "relation": "raw_events",
            "primary_key": "event_id",
            "time_column": "occurred_at",
            "amount_column": "amount",
            "expected_revision": ABSENT_PROJECT_REVISION,
            "idempotency_key": "<caller-generated-unique-key>",
            "dry_run": True,
        },
    }


def _starter_documents(
    *,
    package_id: str,
    description: str,
    first_entity: str,
    relation: str,
    primary_key: str,
    time_column: str,
    amount_column: str,
) -> dict[str, Any]:
    entity = _slug(first_entity, fallback="event")
    model_id = f"{entity}s" if not entity.endswith("s") else entity
    entity_title = _title(entity)
    event_type_dim = f"dimension.{package_id}_{entity}_type"
    temporal_role = f"temporal_role.{package_id}_{entity}_time"
    count_metric = f"metric.{package_id}.{entity}_count"
    amount_metric = f"metric.{package_id}.total_amount"
    package = {
        "schema_version": 1,
        "package": {
            "id": package_id,
            "namespace": package_id,
            "name": package_id,
            "description": description,
            "warehouse": "duckdb",
            "default_db": f"data/{package_id}.duckdb",
            "schema_strict": True,
            "seed": {"kind": "csv_dir_duckdb", "source": f"data/{package_id}_csv"},
            "environments": ["development", "staging", "production"],
        },
        "defaults": {
            "dimension": {"groupable": True, "filterable": True},
            "time": {
                "timezone": "UTC",
                "default_query_axis": False,
                "supported_grains": ["day", "week", "month", "quarter", "year"],
            },
            "measure": {"subject_entity": "self", "aggregation_entity": "self"},
            "relationship": {"traversal": ["forward", "reverse"]},
        },
    }
    graph = {
        "graph": {
            "entities": {
                entity: {
                    "label": entity_title,
                    "key": [primary_key],
                    "model": model_id,
                    "allowed_as_root": True,
                }
            }
        }
    }
    model = {
        "model": {
            "id": model_id,
            "relation": relation,
            "entities": {entity: {}},
            "description": f"One row per {entity.replace('_', ' ')}.",
            "topics": [entity, "starter"],
            "dimensions": {
                "event_type": {
                    "as": event_type_dim,
                    "label": "Event type",
                    "description": "Starter category used to verify grouping behavior.",
                    "kind": "categorical",
                    "synonyms": ["type", "category"],
                },
            },
            "times": {
                time_column: {
                    "as": temporal_role,
                    "label": _title(time_column),
                    "column": time_column,
                    "kind": "timestamp",
                    "class": "event_time",
                    "default_query_axis": True,
                    "default": True,
                }
            },
            "measures": {
                f"{entity}_count": {
                    "label": f"{entity_title} count",
                    "description": f"Count of unique {entity.replace('_', ' ')} rows.",
                    "kind": "entity_count",
                    "entity_key": primary_key,
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "examples": [f"How many {entity.replace('_', ' ')} rows are there by day?"],
                    "meta": {
                        "owner_team": "analytics",
                        "review_priority": "medium",
                        "change_risk": "low",
                    },
                },
                "total_amount": {
                    "label": "Total amount",
                    "description": f"Sum of {amount_column} for the starter {entity.replace('_', ' ')} relation.",
                    "expr": amount_column,
                    "kind": "aggregate",
                    "default_agg": "sum",
                    "accumulation": {"kind": "flow"},
                    "value_type": "number",
                    "examples": ["How does total amount trend over time?"],
                    "meta": {
                        "owner_team": "analytics",
                        "review_priority": "medium",
                        "change_risk": "low",
                    },
                },
            },
        }
    }
    metrics = {
        "metrics": {
            f"{entity}_count": {
                "as": count_metric,
                "label": f"{entity_title} count",
                "description": f"Count of unique {entity.replace('_', ' ')} rows.",
                "kind": "aggregate",
                "measure": f"{entity}_count",
                "value_type": "count",
                "temporal_role": temporal_role,
                "meta": {
                    "owner_team": "analytics",
                    "review_priority": "medium",
                    "change_risk": "low",
                },
            },
            "total_amount": {
                "as": amount_metric,
                "label": "Total amount",
                "description": f"Sum of {amount_column} for the starter {entity.replace('_', ' ')} relation.",
                "kind": "aggregate",
                "measure": "total_amount",
                "value_type": "number",
                "temporal_role": temporal_role,
                "meta": {
                    "owner_team": "analytics",
                    "review_priority": "medium",
                    "change_risk": "low",
                },
            },
        }
    }
    examples = {
        "examples": {
            "starter_amount_by_type": {
                "query": {
                    "version": 1,
                    "select": [{"expression": {"metric": amount_metric}, "as": "total_amount"}],
                    "group_by": [event_type_dim],
                    "time": {"temporal_role": temporal_role, "grain": "day"},
                    "limit": 10,
                },
                "expected_shape": {"min_rows": 1},
            }
        }
    }
    tests = {
        "tests": {
            "starter_count_returns_rows": {
                "kind": "query_row_count_bounds",
                "query": {
                    "version": 1,
                    "select": [{"expression": {"metric": count_metric}, "as": "row_count"}],
                    "time": {"temporal_role": temporal_role, "grain": "day"},
                    "limit": 10,
                },
                "min_rows": 1,
            }
        }
    }
    csv = (
        f"{primary_key},{time_column},event_type,{amount_column}\n"
        f"1,2026-01-01T09:00:00,starter,100.0\n"
        f"2,2026-01-02T09:00:00,starter,75.5\n"
    )
    return {
        "package": package,
        "graph": graph,
        "model": model,
        "metrics": metrics,
        "examples": examples,
        "tests": tests,
        "csv": csv,
        "model_id": model_id,
        "entity": entity,
    }


def _create_project_impl(
    *,
    workspace_root: Path,
    project_path: str,
    package_id: str,
    description: str,
    first_entity: str,
    relation: str,
    primary_key: str,
    time_column: str,
    amount_column: str,
    overwrite: bool,
    expected_revision: str,
    idempotency_key: str,
    dry_run: bool,
) -> dict[str, Any]:
    package_slug = _slug(package_id)
    relation_slug = _slug(relation, fallback="raw_events")
    primary_key_slug = _slug(primary_key, fallback="event_id")
    time_column_slug = _slug(time_column, fallback="occurred_at")
    amount_column_slug = _slug(amount_column, fallback="amount")
    project = _resolve_project_path(
        project_path,
        workspace_root=workspace_root,
        package_id=package_slug,
        require_exists=False,
        require_package_root=False,
    )
    if project.name != package_slug:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "For schema_version: 1 packages, package_id must match the project directory name",
            details={"package_id": package_slug, "project_directory": project.name},
        )
    if (
        project.exists()
        and any(project.iterdir())
        and not overwrite
        and project_revision(project) == expected_revision
    ):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Project directory already exists and is not empty; pass overwrite=true to replace starter files",
            details={"project_path": str(project)},
        )
    docs = _starter_documents(
        package_id=package_slug,
        description=description,
        first_entity=first_entity,
        relation=relation_slug,
        primary_key=primary_key_slug,
        time_column=time_column_slug,
        amount_column=amount_column_slug,
    )
    payloads: dict[str, bytes] = {}
    for rel, payload in {
        "package.yml": docs["package"],
        "graph.yml": docs["graph"],
        f"models/core/{docs['model_id']}.yml": docs["model"],
        "metrics/core.yml": docs["metrics"],
        "examples/core.yml": docs["examples"],
        "tests/core.yml": docs["tests"],
    }.items():
        payloads[rel] = yaml.safe_dump(
            payload,
            sort_keys=False,
            allow_unicode=False,
        ).encode("utf-8")
    csv_relative = f"data/{package_slug}_csv/{relation_slug}.csv"
    payloads[csv_relative] = str(docs["csv"]).encode("utf-8")
    outcome = ProjectTransaction(
        project,
        workspace_root=workspace_root,
    ).apply(
        [
            ProjectFileUpdate(
                relative_path,
                content,
                ((project / relative_path).stat().st_mode & 0o777)
                if (project / relative_path).exists()
                else None,
            )
            for relative_path, content in payloads.items()
        ],
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        intent={
            "operation": "create_project",
            "expected_revision": expected_revision,
            "project_path": str(project),
            "package_id": package_slug,
            "description": description,
            "first_entity": first_entity,
            "relation": relation_slug,
            "primary_key": primary_key_slug,
            "time_column": time_column_slug,
            "amount_column": amount_column_slug,
            "overwrite": overwrite,
        },
        dry_run=dry_run,
        validate_after=True,
        success_status="created",
        metadata={
            "operation": "created",
            "package_id": package_slug,
            "next_actions": [
                "Run validate_project with mode=runtime before trusting queries.",
                "Use upsert_model to add dimensions, measures, joins, or additional entities.",
                "When comparing changes, run impact_project with compare_path or base_ref before opening a release review.",
            ],
        },
    )
    return outcome.report


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


def create_architect_mcp_server(*, workspace_root: str | os.PathLike[str] | None = None) -> FastMCP:
    root = _server_workspace_root(workspace_root)
    mcp = FastMCP(
        name="Semantic Rails Architect MCP",
        instructions=(
            "Use architect_guidance and project_status before editing. Prefer setup_project_dialog "
            "for new packages, then create_project, upsert_model, validate_project, and impact_project. "
            "All writes are scoped to the configured workspace root and this server does not manage "
            "cloud service processes."
        ),
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

    @mcp.tool()
    def architect_guidance(goal: str = "", project_path: str = "") -> dict[str, Any]:
        """Return the recommended Architect MCP workflow and safety guidance."""
        return _guidance_payload(goal=goal, project_path=project_path)

    @mcp.tool()
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
        except Exception as exc:  # pragma: no cover - depends on MCP client support
            return {**dialog, **_report_error(exc), "mode": "dialog_schema"}
        if result.action != "accept" or result.data is None:
            return {"ok": False, "status": str(result.action), "mode": "elicitation", **dialog}
        answers = result.data.model_dump()
        package_slug = _slug(str(answers.get("package_id") or package_id))
        return {
            "ok": True,
            "mode": "elicitation",
            "answers": answers,
            "recommended_next_tool": "create_project",
            "draft_arguments": {
                "project_path": project_path or f"configs/semantic_rails/{package_slug}",
                "package_id": package_slug,
                "description": answers.get("description", ""),
                "first_entity": answers.get("first_entity", "event"),
                "relation": answers.get("relation", "raw_events"),
                "primary_key": answers.get("primary_key", "event_id"),
                "time_column": answers.get("time_column", "occurred_at"),
                "amount_column": answers.get("amount_column", "amount"),
                "expected_revision": ABSENT_PROJECT_REVISION,
                "idempotency_key": "<caller-generated-unique-key>",
                "dry_run": True,
            },
        }

    @mcp.tool(annotations=_mutation_annotations("Create Semantic Rails project"))
    def create_project(
        package_id: str,
        expected_revision: str,
        idempotency_key: str,
        project_path: str = "",
        description: str = "Semantic Rails package managed through Architect MCP.",
        first_entity: str = "event",
        relation: str = "raw_events",
        primary_key: str = "event_id",
        time_column: str = "occurred_at",
        amount_column: str = "amount",
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically create a runnable schema_version: 1 project."""
        try:
            return _mutation_result(
                _create_project_impl(
                    workspace_root=root,
                    project_path=project_path,
                    package_id=package_id,
                    description=description,
                    first_entity=first_entity,
                    relation=relation,
                    primary_key=primary_key,
                    time_column=time_column,
                    amount_column=amount_column,
                    overwrite=overwrite,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path or f"configs/semantic_rails/{_slug(package_id)}",
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool()
    def project_status(project_path: str, include_runtime_checks: bool = False) -> dict[str, Any]:
        """Inspect package files and optionally run runtime validation, examples, and package tests."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            parse = _parse_report(project)
            out: dict[str, Any] = {
                "ok": bool(parse.get("ok")),
                "project_path": str(project),
                "workspace_root": str(root),
                "revision": project_revision(project),
                "files": _project_files(project),
                "parse": parse,
                "next_actions": ["Fix parse errors first."]
                if not parse.get("ok")
                else [
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
            return out
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool()
    def list_project_files(project_path: str) -> dict[str, Any]:
        """List files inside a Semantic Rails project directory."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            return {
                "ok": True,
                "project_path": str(project),
                "revision": project_revision(project),
                "files": _project_files(project),
            }
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool()
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

    @mcp.tool(annotations=_mutation_annotations("Write project file"))
    def write_project_file(
        project_path: str,
        relative_path: str,
        content: str,
        expected_revision: str,
        idempotency_key: str,
        overwrite: bool = True,
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically write a UTF-8 file, rolling back parse failures."""
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .write_file(
                    relative_path=relative_path,
                    content=content,
                    overwrite=overwrite,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(annotations=_mutation_annotations("Upsert semantic model"))
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
        joins: dict[str, Any] | None = None,
        group: str = "core",
        description: str = "",
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically upsert a model and aligned graph entity."""
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
                    joins=joins,
                    group=group,
                    description=description,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(annotations=_mutation_annotations("Upsert metric"))
    def upsert_metric(
        project_path: str,
        metric_key: str,
        spec: dict[str, Any],
        expected_revision: str,
        idempotency_key: str,
        group: str = "core",
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically upsert a curated metric recipe."""
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_metric(
                    metric_key=metric_key,
                    spec=spec,
                    group=group,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(annotations=_mutation_annotations("Upsert segment"))
    def upsert_segment(
        project_path: str,
        segment_key: str,
        spec: dict[str, Any],
        expected_revision: str,
        idempotency_key: str,
        file_name: str = "core.yml",
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically upsert a segment definition."""
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_segment(
                    segment_key=segment_key,
                    spec=spec,
                    file_name=file_name,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool(annotations=_mutation_annotations("Archive project file"))
    def archive_project_file(
        project_path: str,
        relative_path: str,
        expected_revision: str,
        idempotency_key: str,
        reason: str = "",
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically move a file into the internal archive."""
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .archive_file(
                    relative_path=relative_path,
                    reason=reason,
                    validate_after=True,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                )
                .report
            )
        except Exception as exc:
            return _mutation_error_result(
                exc,
                project_path=project_path,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )

    @mcp.tool()
    def validate_project(
        project_path: str,
        mode: str = "parse",
        environment: str = "",
        compare_path: str = "",
        base_ref: str = "",
    ) -> dict[str, Any]:
        """Run parse, runtime, examples, tests, impact, or release validation for a project path."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            ref = _package_ref(project)
            check = str(mode or "parse").strip().lower()
            if check == "parse":
                return _parse_report(project)
            if check == "runtime":
                return validate_config_report(ref)
            if check == "examples":
                return run_examples_report(ref)
            if check == "tests":
                return run_package_tests_report(ref)
            if check == "impact":
                return impact_report(
                    ref,
                    compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                    base_ref=base_ref,
                )
            if check in {"release", "promotion", "promote"}:
                if not environment:
                    raise SemanticLayerError(
                        "INVALID_CONFIG", "environment is required for release validation"
                    )
                return promote_package_report(
                    ref,
                    environment=environment,
                    compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                    base_ref=base_ref,
                )
            raise SemanticLayerError(
                "INVALID_CONFIG", "Unsupported validation mode", details={"mode": mode}
            )
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool()
    def diff_project(
        project_path: str, compare_path: str = "", base_ref: str = ""
    ) -> dict[str, Any]:
        """Diff one project against another path or a git base ref."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            return diff_package_report(
                _package_ref(project),
                compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                base_ref=base_ref,
            )
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool()
    def impact_project(
        project_path: str, compare_path: str = "", base_ref: str = ""
    ) -> dict[str, Any]:
        """Return behavior-change impact, reviewer teams, and risk for a package change."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            return impact_report(
                _package_ref(project),
                compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                base_ref=base_ref,
            )
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool()
    def promotion_check(
        project_path: str, environment: str, compare_path: str = "", base_ref: str = ""
    ) -> dict[str, Any]:
        """Run promotion readiness checks for a target package environment."""
        try:
            project = _resolve_project_path(project_path, workspace_root=root)
            return promote_package_report(
                _package_ref(project),
                environment=environment,
                compare_path=_resolve_compare_path(compare_path, workspace_root=root),
                base_ref=base_ref,
            )
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool()
    def mcp_client_config(
        transport: str = "stdio", host: str = "127.0.0.1", port: int = DEFAULT_ARCHITECT_PORT
    ) -> dict[str, Any]:
        """Return copy-ready client configuration hints for running Architect MCP."""
        selected = str(transport or "stdio")
        if selected not in {"stdio", "sse", "streamable-http"}:
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
        url_path = "/sse" if selected == "sse" else "/mcp"
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
        sse_args = [
            "-m",
            "semantic_rails.architect_mcp",
            "--transport",
            "sse",
            "--host",
            host,
            "--port",
            str(port),
            "--workspace-root",
            str(root),
        ]
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
                "url": f"http://{host}:{port}{url_path}",
                "command": [sys.executable, *http_args],
                "cwd": str(root),
            },
            "sse": {
                "url": f"http://{host}:{port}/sse",
                "command": [sys.executable, *sse_args],
                "cwd": str(root),
            },
            "note": "Use port 8010 by default so the Architect MCP does not collide with the query MCP or local semantic-rails API.",
        }

    return mcp


def run_architect_mcp_server(
    *,
    transport: ArchitectTransport = "stdio",
    host: str = "127.0.0.1",
    port: int = DEFAULT_ARCHITECT_PORT,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
) -> None:
    server = create_architect_mcp_server(workspace_root=workspace_root)
    server.settings.host = host
    server.settings.port = port
    server.run(transport)


def main() -> None:
    parser = argparse.ArgumentParser(prog="semantic-rails-architect-mcp")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_ARCHITECT_PORT)
    parser.add_argument("--workspace-root", default=DEFAULT_WORKSPACE_ROOT)
    args = parser.parse_args()
    transport: ArchitectTransport = args.transport
    run_architect_mcp_server(
        transport=transport, host=args.host, port=args.port, workspace_root=args.workspace_root
    )


if __name__ == "__main__":  # pragma: no cover
    main()
