"""The ``semantic-rails`` entry point: argument parsing and error rendering."""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Any

from ..config import get_package_config, list_package_ids, load_package_config
from ..diagnostics import exception_issue
from ..errors import SemanticLayerError
from ..mcp_manager import CLIENTS, DEFAULT_MCP_HOST, DEFAULT_MCP_PORT, MCP_KINDS
from ..repl.shell import run_interactive_shell
from ..runtime import _enrich_runtime_error
from .commands.mcp import (
    cmd_mcp_client_config,
    cmd_mcp_doctor,
    cmd_mcp_http,
    cmd_mcp_setup,
    cmd_mcp_start,
    cmd_mcp_status,
    cmd_mcp_stdio,
    cmd_mcp_stop,
)
from .commands.package import (
    cmd_build_package,
    cmd_check,
    cmd_diff_package,
    cmd_doctor,
    cmd_export_contract,
    cmd_impact_report,
    cmd_import,
    cmd_init_dispatch,
    cmd_parse_config,
    cmd_promote_package,
    cmd_run_examples,
    cmd_test_package,
    cmd_validate_config,
)
from .commands.project import add_developer_cli
from .commands.query import (
    cmd_build_options,
    cmd_catalog,
    cmd_compile,
    cmd_discover,
    cmd_inspect,
    cmd_packages,
    cmd_plan,
    cmd_query,
    cmd_segment_explain,
    cmd_segment_preview,
    cmd_segment_validate,
    cmd_serve,
    cmd_valid_values,
    cmd_validate,
)
from .common import (
    _add_config_reference_args,
    _add_optional_package_or_path_args,
    _add_package_or_path_args,
    _add_policy_context_args,
    _add_response_detail_args,
    _package_ref_from_args,
    _print_stderr,
)
from .output import _print_error_envelope
from .registry import CommandRegistry, load_extensions


def _config_for_error_enrichment(args: argparse.Namespace) -> Any | None:
    """Best-effort ``PackageConfig`` lookup for diagnostics enrichment.

    Mirrors :func:`_runtime_from_package_or_path` precedence (``--path``
    wins over ``--package``). Returns ``None`` when the command carries
    no package reference or the config itself fails to load — error
    enrichment must never mask the original error.
    """
    with contextlib.suppress(Exception):
        ref = _package_ref_from_args(args)
        if ref.source_path:
            return load_package_config(ref.source_path)
        if ref.package_id:
            return get_package_config(ref.package_id)
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the built-in command tree; :mod:`.registry` applies extensions on top."""

    package_choices = list_package_ids()
    parser = argparse.ArgumentParser(
        prog="semantic-rails",
        description=(
            "Semantic Rails CLI — inspect, validate, compile, and execute "
            "Semantic Rails packages. See docs/QUERY_API.md and docs/CAPABILITIES.md "
            "for the agent loop and supported runtime surfaces. Commands that use a package "
            "take --package or --path, else the package directory you are in, else the local "
            "profile (semantic-rails profile init). With none of these, ask, ls, project "
            "status/validate, repl and bare semantic-rails offer the bundled jaffle_shop "
            "sample package at a terminal (default No); noninteractive package reads stop "
            "with selection guidance. Setup and debug report no package selected. Packages, "
            "project list and init need no existing package. Package build and check commands "
            "require explicit --package or --path."
        ),
    )
    from semantic_rails import __version__

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    add_developer_cli(sub, package_choices)

    p_packages = sub.add_parser(
        "packages",
        description="List the registered package ids under configs/semantic_rails/.",
    )
    p_packages.set_defaults(func=cmd_packages)

    p_catalog = sub.add_parser(
        "catalog",
        description="Print the package catalog (entities, dimensions, measures, metrics, segments).",
    )
    _add_package_or_path_args(p_catalog, package_choices)
    p_catalog.add_argument(
        "--view",
        default="summary",
        help="Catalog view shape: 'summary' (default) or 'full'.",
    )
    p_catalog.add_argument(
        "--verbosity",
        default="compact",
        help="Output verbosity: 'compact' (default) or 'detailed'.",
    )
    p_catalog.add_argument(
        "--kind",
        default="",
        help="Filter to a single object kind (entity, dimension, measure, metric, segment).",
    )
    p_catalog.add_argument(
        "--search",
        default="",
        help="Substring filter against object names and aliases.",
    )
    p_catalog.add_argument(
        "--entity",
        default="",
        help="Filter to objects attached to a single entity id.",
    )
    _add_policy_context_args(p_catalog)
    p_catalog.set_defaults(func=cmd_catalog)

    p_discover = sub.add_parser(
        "discover",
        description="Rank catalog objects by multi-term relevance for an in-progress query (the recommended agent entry point).",
    )
    _add_package_or_path_args(p_discover, package_choices)
    p_discover.add_argument(
        "--terms",
        required=True,
        help="Comma- or whitespace-separated list of terms to match.",
    )
    p_discover.add_argument(
        "--kinds",
        default="",
        help="Comma-separated kinds to include (entity, dimension, measure, metric, segment).",
    )
    p_discover.add_argument(
        "--query-json",
        default="",
        help="Optional partial query (JSON or @file) to bias ranking against an in-progress build.",
    )
    p_discover.add_argument(
        "--stage",
        default="",
        help="Target builder stage to filter relevance against (e.g. 'metric', 'dimension').",
    )
    p_discover.add_argument(
        "--verbosity",
        default="compact",
        help="Output verbosity: 'compact' (default) or 'detailed'.",
    )
    p_discover.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of ranked candidates to return (default: 10).",
    )
    _add_policy_context_args(p_discover)
    p_discover.set_defaults(func=cmd_discover)

    p_inspect = sub.add_parser(
        "inspect",
        description="Inspect a single object id and return its full metadata, including paths and constraints.",
    )
    _add_package_or_path_args(p_inspect, package_choices)
    p_inspect.add_argument(
        "--object-id",
        required=True,
        help="Fully qualified object id (e.g. 'metric.revenue', 'dimension.order_status').",
    )
    p_inspect.add_argument(
        "--query-json",
        default="",
        help="Optional partial query (JSON or @file) for path-aware inspection in context.",
    )
    p_inspect.add_argument(
        "--verbosity",
        default="compact",
        help="Output verbosity: 'compact' (default) or 'detailed'.",
    )
    _add_policy_context_args(p_inspect)
    p_inspect.set_defaults(func=cmd_inspect)

    p_validate = sub.add_parser(
        "validate",
        description="Validate a query payload against the package without compiling SQL. Returns issues and recovery hints.",
    )
    _add_package_or_path_args(p_validate, package_choices)
    p_validate.add_argument(
        "--query-json",
        required=True,
        help="Query IR payload as JSON, or @path/to/file.json to load from disk.",
    )
    _add_policy_context_args(p_validate)
    _add_response_detail_args(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_compile = sub.add_parser(
        "compile",
        description="Compile a query to SQL without executing it. Returns SQL, output columns, dialect, and physical plan.",
    )
    _add_package_or_path_args(p_compile, package_choices)
    p_compile.add_argument(
        "--query-json",
        required=True,
        help="Query IR payload as JSON, or @path/to/file.json to load from disk.",
    )
    _add_policy_context_args(p_compile)
    _add_response_detail_args(p_compile)
    p_compile.set_defaults(func=cmd_compile)

    p_parse_config = sub.add_parser(
        "parse-config",
        description="Parse a package config and return the normalized PackageConfig dataclass payload.",
    )
    _add_config_reference_args(p_parse_config, package_choices)
    p_parse_config.set_defaults(func=cmd_parse_config)

    p_export_contract = sub.add_parser(
        "export-contract",
        description=(
            "Export the canonical framework-neutral semantic validation contract "
            "for dbt, SQLMesh, and other binding packages."
        ),
    )
    _add_config_reference_args(p_export_contract, package_choices)
    p_export_contract.add_argument(
        "--format",
        choices=["validation", "metrics"],
        default="validation",
        help="Validation contract (default) or read-only v1 metric portability catalog.",
    )
    p_export_contract.add_argument(
        "--output",
        "-o",
        default="",
        help="Optional JSON output path. Omit to print the canonical payload to stdout.",
    )
    p_export_contract.set_defaults(func=cmd_export_contract)

    p_validate_config = sub.add_parser(
        "validate-config",
        description="Validate a package config (entities, joins, measures, metrics) and report structural errors.",
    )
    _add_config_reference_args(p_validate_config, package_choices)
    p_validate_config.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages while keeping the JSON validation report.",
    )
    p_validate_config.add_argument(
        "--no-manifest",
        action="store_true",
        help="Skip writing .compiled/manifest.json after successful validation.",
    )
    p_validate_config.set_defaults(func=cmd_validate_config)

    p_check = sub.add_parser(
        "check",
        description="One-command package gate: parse + validate + run examples + run tests, optionally write a manifest-backed artifact.",
    )
    _add_config_reference_args(p_check, package_choices)
    p_check.add_argument(
        "--compare-path",
        default="",
        help="Optional path to a baseline package for diff/impact comparison.",
    )
    p_check.add_argument(
        "--base-ref",
        default="",
        help="Optional git ref (e.g. 'main') to use as a baseline if --compare-path is not given.",
    )
    p_check.add_argument(
        "--artifact",
        default="",
        help="Path to write a manifest-backed deployable artifact (.tar.gz). Optional.",
    )
    p_check.add_argument(
        "--full",
        action="store_true",
        help="Print the full check report instead of the compact summary.",
    )
    p_check.set_defaults(func=cmd_check)

    p_build_package = sub.add_parser(
        "build-package",
        description="Build a manifest-backed deployable package artifact (.tar.gz) from a config.",
    )
    _add_config_reference_args(p_build_package, package_choices)
    p_build_package.add_argument(
        "--output",
        required=True,
        help="Output path for the artifact (typically ending in .tar.gz).",
    )
    p_build_package.add_argument(
        "--compare-path",
        default="",
        help="Optional path to a baseline package for diff/impact comparison.",
    )
    p_build_package.add_argument(
        "--base-ref",
        default="",
        help="Optional git ref (e.g. 'main') to use as a baseline if --compare-path is not given.",
    )
    p_build_package.set_defaults(func=cmd_build_package)

    p_run_examples = sub.add_parser(
        "run-examples",
        description="Run the package-local example queries (declared in examples blocks) against the package.",
    )
    _add_config_reference_args(p_run_examples, package_choices)
    p_run_examples.set_defaults(func=cmd_run_examples)

    p_test_package = sub.add_parser(
        "test-package",
        description="Run the package-local tests (declared in tests blocks) against the package.",
    )
    _add_config_reference_args(p_test_package, package_choices)
    p_test_package.set_defaults(func=cmd_test_package)

    p_diff_package = sub.add_parser(
        "diff-package",
        description="Diff a package config against a baseline (path or git ref) and report structural changes.",
    )
    _add_config_reference_args(p_diff_package, package_choices)
    p_diff_package.add_argument(
        "--compare-path",
        default="",
        help="Path to a baseline package config to diff against.",
    )
    p_diff_package.add_argument(
        "--base-ref",
        default="",
        help="Git ref (e.g. 'main') to use as a baseline if --compare-path is not given.",
    )
    p_diff_package.set_defaults(func=cmd_diff_package)

    p_impact_report = sub.add_parser(
        "impact-report",
        description="Report the downstream impact of changes between a config and a baseline (path or git ref).",
    )
    _add_config_reference_args(p_impact_report, package_choices)
    p_impact_report.add_argument(
        "--compare-path",
        default="",
        help="Path to a baseline package config for impact comparison.",
    )
    p_impact_report.add_argument(
        "--base-ref",
        default="",
        help="Git ref (e.g. 'main') to use as a baseline if --compare-path is not given.",
    )
    p_impact_report.set_defaults(func=cmd_impact_report)

    p_promote_package = sub.add_parser(
        "promote-package",
        description="Promote a package to a target environment (gates on validation + impact + tests).",
    )
    _add_config_reference_args(p_promote_package, package_choices)
    p_promote_package.add_argument(
        "--environment",
        required=True,
        help="Target environment to promote to (e.g. 'staging', 'production').",
    )
    p_promote_package.add_argument(
        "--compare-path",
        default="",
        help="Path to a baseline package config for impact comparison.",
    )
    p_promote_package.add_argument(
        "--base-ref",
        default="",
        help="Git ref (e.g. 'main') to use as a baseline if --compare-path is not given.",
    )
    p_promote_package.set_defaults(func=cmd_promote_package)

    p_query = sub.add_parser(
        "query",
        description="Compile and execute a query against the configured warehouse (DuckDB by default).",
    )
    _add_package_or_path_args(p_query, package_choices)
    p_query.add_argument(
        "--query-json",
        required=True,
        help="Query IR payload as JSON, or @path/to/file.json to load from disk.",
    )
    _add_policy_context_args(p_query)
    _add_response_detail_args(p_query)
    p_query.set_defaults(func=cmd_query)

    p_segment_validate = sub.add_parser(
        "segment-validate",
        description="Validate a named segment definition against the package.",
    )
    _add_package_or_path_args(p_segment_validate, package_choices)
    p_segment_validate.add_argument(
        "--segment-id",
        required=True,
        help="Segment id (e.g. 'segment.high_value_customers').",
    )
    _add_policy_context_args(p_segment_validate)
    p_segment_validate.set_defaults(func=cmd_segment_validate)

    p_segment_explain = sub.add_parser(
        "segment-explain",
        description="Explain a segment: the basis metric, predicates, anchor entity, and chosen path.",
    )
    _add_package_or_path_args(p_segment_explain, package_choices)
    p_segment_explain.add_argument(
        "--segment-id",
        required=True,
        help="Segment id (e.g. 'segment.high_value_customers').",
    )
    _add_policy_context_args(p_segment_explain)
    p_segment_explain.set_defaults(func=cmd_segment_explain)

    p_segment_preview = sub.add_parser(
        "segment-preview",
        description="Preview the resolved member list for a segment (executes against the warehouse).",
    )
    _add_package_or_path_args(p_segment_preview, package_choices)
    p_segment_preview.add_argument(
        "--segment-id",
        required=True,
        help="Segment id (e.g. 'segment.high_value_customers').",
    )
    _add_policy_context_args(p_segment_preview)
    p_segment_preview.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum members to return in the preview (default: 50).",
    )
    p_segment_preview.set_defaults(func=cmd_segment_preview)

    p_values = sub.add_parser(
        "valid-values",
        description="List the curated allowed values for a dimension (paginated, supports search).",
    )
    _add_package_or_path_args(p_values, package_choices)
    p_values.add_argument(
        "--dimension",
        required=True,
        help="Dimension id (e.g. 'dimension.order_status').",
    )
    p_values.add_argument(
        "--query-json",
        default="",
        help="Optional partial query (JSON or @file) to scope value retrieval to the in-progress build.",
    )
    p_values.add_argument(
        "--search",
        default="",
        help="Substring filter against the dimension values.",
    )
    p_values.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum values to return (default: 100).",
    )
    p_values.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Pagination offset (default: 0).",
    )
    p_values.add_argument(
        "--include-counts",
        action="store_true",
        help="Include row counts per value (executes a count query against the warehouse).",
    )
    _add_policy_context_args(p_values)
    p_values.set_defaults(func=cmd_valid_values)

    p_build = sub.add_parser(
        "build-options",
        description="Ranked builder surface: the recommended next-edit candidates for an in-progress query.",
    )
    _add_package_or_path_args(p_build, package_choices)
    p_build.add_argument(
        "--query-json",
        required=True,
        help="Current query IR payload as JSON, or @path/to/file.json to load from disk.",
    )
    p_build.add_argument(
        "--focus-terms",
        default="",
        help="Comma- or whitespace-separated terms to bias ranking around.",
    )
    p_build.add_argument(
        "--focus-object-id",
        default="",
        help="Object id to focus the build options around.",
    )
    p_build.add_argument(
        "--step",
        default="",
        help="Restrict to a specific builder step (e.g. 'add_dimension', 'add_filter').",
    )
    p_build.add_argument(
        "--stage",
        default="",
        help="Target builder stage filter (e.g. 'metric', 'dimension').",
    )
    p_build.add_argument(
        "--verbosity",
        default="compact",
        help="Output verbosity: 'compact' (default) or 'detailed'.",
    )
    p_build.add_argument(
        "--include-blocked",
        action="store_true",
        help="Include candidates that are currently blocked by guardrails (with reasons).",
    )
    p_build.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of candidates to return (default: 10).",
    )
    _add_policy_context_args(p_build)
    p_build.set_defaults(func=cmd_build_options)

    p_plan = sub.add_parser(
        "plan",
        description="Plan one best Query IR from a free-text intent and optional partial query.",
    )
    _add_package_or_path_args(p_plan, package_choices)
    p_plan.add_argument(
        "--intent",
        required=True,
        help="Free-text intent (e.g. 'monthly revenue by channel for the last quarter').",
    )
    p_plan.add_argument(
        "--query-json",
        default="",
        help="Optional partial query (JSON or @file) to preserve and plan against.",
    )
    p_plan.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Maximum number of planner alternatives to return when detail is full/debug (default: 3).",
    )
    p_plan.add_argument(
        "--detail",
        choices=["best", "full", "debug"],
        default="best",
        help="Planner response detail (default: best). Use full for alternatives/blocked, debug for compose hints.",
    )
    _add_policy_context_args(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_mcp = sub.add_parser(
        "mcp",
        description="Run the packaged Model Context Protocol (MCP) server (stdio or HTTP/SSE transport).",
    )
    mcp_sub = p_mcp.add_subparsers(dest="mcp_cmd", required=True)

    p_mcp_stdio = mcp_sub.add_parser(
        "stdio",
        description="Run the MCP server over stdio (for desktop hosts like Claude Desktop).",
    )
    _add_package_or_path_args(p_mcp_stdio, package_choices)
    p_mcp_stdio.set_defaults(func=cmd_mcp_stdio)

    p_mcp_http = mcp_sub.add_parser(
        "http",
        description="Run the MCP server over HTTP/SSE for remote agent integration.",
    )
    _add_package_or_path_args(p_mcp_http, package_choices)
    p_mcp_http.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1).",
    )
    p_mcp_http.add_argument(
        "--port",
        type=int,
        default=8091,
        help="Port to bind (default: 8091).",
    )
    p_mcp_http.set_defaults(func=cmd_mcp_http)

    p_mcp_doctor = mcp_sub.add_parser(
        "doctor",
        description=(
            "Load the package and MCP adapter once, list tools, and print commands "
            "for stdio/http startup verification."
        ),
    )
    _add_package_or_path_args(p_mcp_doctor, package_choices)
    p_mcp_doctor.set_defaults(func=cmd_mcp_doctor)

    p_mcp_setup = mcp_sub.add_parser(
        "setup",
        description=(
            "Run an MCP package check and preview or install Claude/Codex MCP client config."
        ),
    )
    _add_optional_package_or_path_args(p_mcp_setup, package_choices)
    p_mcp_setup.add_argument(
        "--client",
        choices=CLIENTS,
        default="both",
        help="Client config to preview or install.",
    )
    p_mcp_setup.add_argument(
        "--mcp",
        choices=MCP_KINDS,
        default="both",
        help="MCP server config to include: query, architect, or both.",
    )
    p_mcp_setup.add_argument(
        "--workspace-root",
        default="",
        help="Workspace root for Architect MCP. Defaults to the package parent.",
    )
    p_mcp_setup.add_argument(
        "--install",
        action="store_true",
        help="Write config after the package check passes. Requires --yes when non-interactive.",
    )
    p_mcp_setup.add_argument("--yes", action="store_true", help="Confirm writes with --install.")
    p_mcp_setup.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_mcp_setup.set_defaults(func=cmd_mcp_setup, human_cli=True)

    p_mcp_start = mcp_sub.add_parser(
        "start",
        description="Start a local managed MCP HTTP server in the background (POSIX only).",
    )
    _add_optional_package_or_path_args(p_mcp_start, package_choices)
    p_mcp_start.add_argument("--name", default="default", help="Local server name.")
    p_mcp_start.add_argument("--host", default=DEFAULT_MCP_HOST, help="Host to bind.")
    p_mcp_start.add_argument("--port", type=int, default=DEFAULT_MCP_PORT, help="Port to bind.")
    p_mcp_start.set_defaults(func=cmd_mcp_start)

    p_mcp_stop = mcp_sub.add_parser(
        "stop",
        description=(
            "Stop a managed local MCP HTTP server started by `semantic-rails mcp start`, "
            "by server name or package path (POSIX only)."
        ),
    )
    _add_optional_package_or_path_args(p_mcp_stop, package_choices)
    p_mcp_stop.add_argument(
        "--name",
        default="default",
        help="Local server name. Ignored when --package or --path is provided.",
    )
    p_mcp_stop.set_defaults(func=cmd_mcp_stop)

    p_mcp_status = mcp_sub.add_parser(
        "status",
        description="Show managed local MCP HTTP servers and available MCP launch commands.",
    )
    _add_optional_package_or_path_args(p_mcp_status, package_choices)
    p_mcp_status.set_defaults(func=cmd_mcp_status)

    p_mcp_client_config = mcp_sub.add_parser(
        "client-config",
        description="Preview or install Claude/Codex MCP client configuration.",
    )
    _add_optional_package_or_path_args(p_mcp_client_config, package_choices)
    p_mcp_client_config.add_argument(
        "--client",
        choices=CLIENTS,
        default="both",
        help="Client config to preview or install.",
    )
    p_mcp_client_config.add_argument(
        "--mcp",
        choices=MCP_KINDS,
        default="both",
        help="MCP server config to include: query, architect, or both.",
    )
    p_mcp_client_config.add_argument(
        "--workspace-root",
        default="",
        help="Workspace root for Architect MCP. Defaults to the package parent.",
    )
    p_mcp_client_config.add_argument(
        "--install",
        action="store_true",
        help="Write the generated config into the selected client config file(s).",
    )
    p_mcp_client_config.add_argument(
        "--yes",
        action="store_true",
        help="Confirm writes when --install is used.",
    )
    p_mcp_client_config.set_defaults(func=cmd_mcp_client_config)

    p_doctor = sub.add_parser(
        "doctor",
        description="Run a configuration doctor against a package: structural checks, common authoring pitfalls, fix hints.",
    )
    _add_config_reference_args(p_doctor, package_choices)
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser(
        "init",
        description=(
            "Initialize a Semantic Rails package. `init <name>` creates a split-layout "
            "developer package; the legacy `init --output ... --package-id ...` form "
            "creates a single-file package."
        ),
    )
    p_init.add_argument(
        "name",
        nargs="?",
        help="Package id to create. When provided, init creates a split-layout package.",
    )
    p_init.add_argument(
        "--output",
        default="",
        help="Target directory for the new package (created if it does not exist).",
    )
    p_init.add_argument(
        "--package-id",
        default="",
        help="Package id to write into package.yml. Defaults to the package name/output directory.",
    )
    p_init.add_argument(
        "--namespace",
        default="",
        help="Namespace for derived ids. Defaults to the package id.",
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing non-empty output directory.",
    )
    p_init.add_argument(
        "--split",
        action="store_true",
        help="Create the split-layout developer package even when using --output.",
    )
    p_init.add_argument(
        "--single-file",
        action="store_true",
        help="Create the legacy single-file package.yml scaffold.",
    )
    p_init.add_argument(
        "--workspace-root",
        default="",
        help="Base directory for default split-layout output.",
    )
    p_init.add_argument(
        "--description",
        default="",
        help="Description for split-layout packages.",
    )
    p_init.add_argument(
        "--entity",
        default="event",
        help="Starter entity for split-layout packages.",
    )
    p_init.add_argument(
        "--relation",
        default="raw_events",
        help="Starter relation/table for split-layout packages.",
    )
    p_init.add_argument(
        "--primary-key",
        default="event_id",
        help="Starter primary key for split-layout packages.",
    )
    p_init.add_argument(
        "--time-column",
        default="occurred_at",
        help="Starter time column for split-layout packages.",
    )
    p_init.add_argument(
        "--amount-column",
        default="amount",
        help="Starter numeric measure column for split-layout packages.",
    )
    p_init.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip split-layout parse/runtime/example/test checks after creating files.",
    )
    p_init.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Accept defaults and do not prompt for split-layout package fields.",
    )
    p_init.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_init.set_defaults(func=cmd_init_dispatch, human_cli=True)

    p_import = sub.add_parser(
        "import",
        description=(
            "Import a package from an external semantic-layer format. "
            "Today supports `--from metricflow` (a MetricFlow YAML "
            "directory or a dbt-emitted semantic_manifest.json). No "
            "MetricFlow runtime is required — the translator reads "
            "YAML/JSON files standalone."
        ),
    )
    p_import.add_argument(
        "--from",
        dest="source_format",
        required=True,
        choices=["metricflow"],
        help="Source format. Today: 'metricflow' (YAML dir or semantic_manifest.json).",
    )
    p_import.add_argument(
        "--source",
        required=True,
        help="Path to the source artifact (directory or JSON file).",
    )
    p_import.add_argument(
        "--output",
        required=True,
        help="Directory under which <package-id>/ will be created.",
    )
    p_import.add_argument(
        "--package-id",
        required=True,
        help="Semantic Rails package id (drives namespace by default).",
    )
    p_import.add_argument(
        "--namespace",
        default=None,
        help="Namespace prefix for auto-derived ids. Defaults to --package-id.",
    )
    p_import.add_argument(
        "--warehouse",
        default="duckdb",
        choices=("duckdb", "snowflake"),
        help="Warehouse kind to declare in the emitted package.yml.",
    )
    p_import.add_argument(
        "--default-db",
        default=None,
        help="DuckDB file path; recommended for --warehouse duckdb.",
    )
    p_import.add_argument(
        "--description",
        default=None,
        help="Optional package description.",
    )
    p_import.set_defaults(func=cmd_import)

    p_serve = sub.add_parser(
        "serve",
        description="Start the local HTTP API server (foreground/blocking) on --host:--port.",
    )
    _add_package_or_path_args(p_serve, package_choices)
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1).",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8090,
        help="Port to bind (default: 8090).",
    )
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main() -> None:
    registry = CommandRegistry()
    load_extensions(registry)
    parser = registry.build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", ""):
        if not sys.stdin.isatty():
            parser.print_help()
            return
        # Bare `semantic-rails` opens the REPL; route it through the same
        # error handling so "no package selected" reads as guidance.
        args.func = lambda _args: run_interactive_shell()
        args.human_cli = True
    try:
        args.func(args)
    except SemanticLayerError as exc:
        # Same diagnostics enrichment as the HTTP boundary
        # (SemanticHTTPService.exception_payload) and the MCP adapter
        # (_error_response) — closest_matches on OBJECT_NOT_FOUND etc.
        # Enrichment is a pure read on the in-memory config, but it must
        # never mask the original error, hence the suppress.
        config = _config_for_error_enrichment(args)
        if config is not None:
            with contextlib.suppress(Exception):
                exc = _enrich_runtime_error(exc, config)
        issue = exception_issue(exc, stage="cli")
        if getattr(args, "human_cli", False) and not getattr(args, "json", False):
            _print_stderr(f"error [{issue['code']}]: {issue['message']}")
            for hint in list(issue.get("recovery_hints", []) or [])[:3]:
                message = str(hint.get("message", "") or "").strip()
                if message:
                    _print_stderr(f"hint: {message}")
        else:
            _print_error_envelope(issue)
        raise SystemExit(1) from exc
    except Exception as exc:  # pragma: no cover - defensive CLI guardrail
        _print_error_envelope(
            {
                "code": "INTERNAL_ERROR",
                "message": str(exc),
                "severity": "error",
                "stage": "cli",
                "details": {},
                "object_ids": [],
                "path": "",
                "recovery_hints": [],
            }
        )
        raise SystemExit(1) from exc
