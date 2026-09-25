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
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from . import architect_introspection as introspection
from . import dbt_artifacts
from .architect_service import (
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
    snowflake_native_direct_connect_errors,
    warehouse_connector,
)
from .errors import SemanticLayerError
from .mcp import SemanticLayerMCPAdapter, json_text
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
# The network transports can write files, so they require a bearer token.
ARCHITECT_TOKEN_ENV = "SEMANTIC_RAILS_ARCHITECT_TOKEN"
ARCHITECT_TOKEN_FILE_ENV = "SEMANTIC_RAILS_ARCHITECT_TOKEN_FILE"
MIN_ARCHITECT_TOKEN_LENGTH = 32
# Response caps for unnarrowed listings: narrow with schema, or with select.
MAX_LISTED_TABLES = 200
MAX_UNSELECTED_DBT_SUGGESTIONS = 20
MAX_PREVIEW_ROWS = 200
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
    time_column: str = Field(default="occurred_at")
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
            "Prefer upsert_model for entity, dimension, time, and measure changes, and upsert_relationship for foreign keys, so graph.yml stays aligned.",
            "Run validate_project with mode=parse after every structural edit; use mode=runtime before promoting.",
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
                "tool": "upsert_model / upsert_relationship / upsert_metric / upsert_segment / upsert_example / upsert_test / write_project_file",
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
    if warehouse == "snowflake" and not draft["connection_name"]:
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
            time_column=str(arguments.get("time_column") or "occurred_at"),
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

    Every way to serve the network transports (``run("sse")``,
    ``run("streamable-http")``, ``sse_app()``, ``streamable_http_app()``) goes
    through these overrides, so none serves without ``bearer_token`` set to a
    valid token.
    """

    bearer_token: str = ""

    def sse_app(self, mount_path: str | None = None) -> Starlette:
        return self._gated(super().sse_app(mount_path))

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


def architect_http_app(
    server: ArchitectMCPServer, transport: Literal["sse", "streamable-http"], token: str
) -> Starlette:
    """The ASGI app for a network transport, behind the bearer-token gate."""
    server.bearer_token = _check_token(token)
    return server.sse_app() if transport == "sse" else server.streamable_http_app()


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
            "Use architect_guidance and project_status before editing. Prefer setup_project_dialog "
            "for new packages, then create_project, upsert_model, validate_project, and impact_project. "
            "All writes are scoped to the configured workspace root and this server does not manage "
            "cloud service processes."
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
        package_slug = _slug(str(answers.get("package_id") or package_id))
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
        }

    @mcp.tool(
        annotations=_mutation_annotations("Create Semantic Rails project"),
        description=(
            "Preview or atomically create a strict schema_version: 1 project. DuckDB packages "
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
                ).report
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

    @mcp.tool(
        annotations=_mutation_annotations("Upsert semantic model"),
        description=(
            "Preview or atomically upsert a model and aligned graph entity. calendar: true makes "
            'it the package calendar for calendar_id (default "default", which a package with '
            "calendars needs): time.fill reads its date_day time and week_start, month_start, "
            "quarter_start and year_start kind: date dimensions. calendar: false reverts that. "
            "On a regular model, calendar_id binds its times to a calendar. Fields merge into "
            "an existing model; replace: true rewrites it from the arguments, keeping only its "
            "id, entities and calendar_id, and lists what it drops in dropped_fields."
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
        joins: dict[str, Any] | None = None,
        group: str = "core",
        description: str = "",
        label: str = "",
        calendar: bool | None = None,
        calendar_id: str = "",
        replace: bool = False,
        dry_run: bool = False,
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
                    joins=joins,
                    group=group,
                    description=description,
                    label=label,
                    calendar=calendar,
                    calendar_id=calendar_id,
                    replace=replace,
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

    @mcp.tool(
        annotations=_mutation_annotations("Upsert relationship"),
        description=(
            "Preview or atomically relate two entities: columns on from_entity's model hold "
            "to_entity's key, in key order. cardinality: many_to_one or one_to_one."
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
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_relationship(
                    from_entity=from_entity,
                    to_entity=to_entity,
                    columns=columns,
                    cardinality=cardinality,
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
        replace: bool = False,
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically upsert a metric; replace: true rewrites it, keeping its id."""
        try:
            return _mutation_result(
                ArchitectProject(project_path, workspace_root=root)
                .upsert_metric(
                    metric_key=metric_key,
                    spec=spec,
                    group=group,
                    replace=replace,
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
        replace: bool = False,
        dry_run: bool = False,
    ) -> ArchitectMutationResult:
        """Preview or atomically upsert a segment; replace: true rewrites it, keeping its id."""
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

    def _upsert_check(kind: str, project_path: str, **arguments: Any) -> ArchitectMutationResult:
        try:
            project = ArchitectProject(project_path, workspace_root=root)
            return _mutation_result(project.upsert_check(kind=kind, **arguments).report)
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
            "Preview or atomically upsert an example question in examples/<file_name>: spec "
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
    ) -> ArchitectMutationResult:
        return _upsert_check(
            "example",
            project_path,
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
            "Preview or atomically upsert a package test in tests/<file_name>. spec.kind is "
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
    ) -> ArchitectMutationResult:
        return _upsert_check(
            "test",
            project_path,
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
            "does, and return at most max_rows rows (1-200), with truncated and total_row_count "
            "when there are more. Values are real warehouse data."
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
        project_path: str = "", duckdb_path: str = "", schema: str = ""
    ) -> dict[str, Any]:
        """List tables and views (read-only) in a DuckDB package's database or a DuckDB file."""
        try:
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                tables = introspection.list_tables(warehouse, schema=schema)
            return {
                "ok": True,
                "tables": tables[:MAX_LISTED_TABLES],
                "truncated": len(tables) > MAX_LISTED_TABLES,
                **({"hint": "narrow with schema"} if len(tables) > MAX_LISTED_TABLES else {}),
            }
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(annotations=_read_only_annotations("Describe warehouse table"))
    def describe_table(
        relation: str, project_path: str = "", duckdb_path: str = ""
    ) -> dict[str, Any]:
        """Columns (type, nullability, default) and declared primary, unique and foreign keys."""
        try:
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                return {"ok": True, **introspection.describe_table(warehouse, relation)}
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
    ) -> dict[str, Any]:
        try:
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                return {
                    "ok": True,
                    **introspection.profile_columns(
                        warehouse,
                        relation,
                        columns,
                        sample_limit=sample_limit,
                        max_rows=max_rows,
                    ),
                }
        except Exception as exc:
            return _report_error(exc)

    @mcp.tool(
        annotations=_read_only_annotations("Suggest a model"),
        description=(
            "Propose a key, times, dimensions, measures and foreign keys for a relation, with "
            "confidences, reasons and draft upsert_model arguments. Read-only."
        ),
    )
    def suggest_model(
        relation: str, project_path: str = "", duckdb_path: str = ""
    ) -> dict[str, Any]:
        try:
            with introspection.open_duckdb(_warehouse_path(project_path, duckdb_path)) as warehouse:
                return {"ok": True, **introspection.suggest_model(warehouse, relation)}
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
    ) -> dict[str, Any]:
        try:
            project = _dbt_project(target_dir, manifest_path, catalog_path)
            models = dbt_artifacts.suggest_models_from_dbt(project, list(select or []))
            limit = len(models) if select else MAX_UNSELECTED_DBT_SUGGESTIONS
            return {
                "ok": True,
                "dbt_project": project.project_name,
                "adapter_type": project.adapter_type,
                "models": models[:limit],
                "truncated": len(models) > limit,
                **({"hint": "narrow with select"} if len(models) > limit else {}),
                "dbt_warnings": project.warnings,
            }
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
                items,
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
                }
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
                "url": f"http://{_url_host(host)}:{port}{url_path}",
                "command": [sys.executable, *http_args],
                "cwd": str(root),
                "headers": auth_headers,
            },
            "sse": {
                "url": f"http://{_url_host(host)}:{port}/sse",
                "command": [sys.executable, *sse_args],
                "cwd": str(root),
                "headers": auth_headers,
            },
            "auth": {
                "required_for": ["sse", "streamable-http"],
                "token_env": ARCHITECT_TOKEN_ENV,
                "token_file_env": ARCHITECT_TOKEN_FILE_ENV,
                "min_length": MIN_ARCHITECT_TOKEN_LENGTH,
            },
            "note": (
                "Use port 8010 by default so the Architect MCP does not collide with the query MCP "
                f"or local semantic-rails API. HTTP and SSE need {ARCHITECT_TOKEN_ENV} (or a token "
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
        architect_http_app(server, transport, token),
        host=host,
        port=port,
        log_level=server.settings.log_level.lower(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="semantic-rails-architect-mcp")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
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
