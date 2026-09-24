"""``semantic-rails`` console-script implementation.

Commands live in :mod:`semantic_rails.cli.commands`, shared plumbing in
:mod:`semantic_rails.cli.common`, report builders in
:mod:`semantic_rails.cli.reports` and human output in
:mod:`semantic_rails.cli.output`. :func:`main` is the console-script entry
point and is re-exported by :mod:`semantic_rails.__main__`. The names below
stay importable from ``semantic_rails.cli``. ``main`` and the ``cmd_*``
functions resolve lazily, so importing a submodule (as :mod:`semantic_rails.repl`
does) never pulls in the command modules that import the REPL.
"""

from __future__ import annotations

import importlib
from typing import Any

from ..api import serve
from ..config import list_package_ids
from ..config_validation import (
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)
from ..contracts import export_semantic_contract
from ..diagnostics import exception_issue
from ..errors import SemanticLayerError
from ..mcp import SemanticLayerMCPAdapter
from ..mcp_server import serve_http as serve_mcp_http
from ..mcp_server import serve_stdio as serve_mcp_stdio
from ..metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
    valid_values_payload,
)
from ..package_tools import (
    build_package_artifact_report,
    check_package_report,
    diff_package_report,
    impact_report,
    promote_package_report,
    run_examples_report,
    run_package_tests_report,
)
from ..planner import plan_payload
from ..runtime import Runtime

__all__ = [
    "Runtime",
    "SemanticLayerError",
    "SemanticLayerMCPAdapter",
    "build_options_payload",
    "build_package_artifact_report",
    "catalog_payload",
    "check_package_report",
    "diff_package_report",
    "discover_payload",
    "exception_issue",
    "export_semantic_contract",
    "impact_report",
    "inspect_payload",
    "list_package_ids",
    "main",
    "plan_payload",
    "parse_config_report",
    "promote_package_report",
    "resolve_package_reference",
    "run_examples_report",
    "run_package_tests_report",
    "serve",
    "serve_mcp_http",
    "serve_mcp_stdio",
    "valid_values_payload",
    "validate_config_report",
]

# Resolved on first access (PEP 562) to keep ``import semantic_rails.cli.<module>``
# free of import cycles with ``semantic_rails.repl``.
_LAZY = {
    "main": ".app",
    "MCP_REQUIRED_TOOLS": ".commands.mcp",
    "cmd_mcp_client_config": ".commands.mcp",
    "cmd_mcp_doctor": ".commands.mcp",
    "cmd_mcp_http": ".commands.mcp",
    "cmd_mcp_setup": ".commands.mcp",
    "cmd_mcp_start": ".commands.mcp",
    "cmd_mcp_status": ".commands.mcp",
    "cmd_mcp_stdio": ".commands.mcp",
    "cmd_mcp_stop": ".commands.mcp",
    "cmd_build_package": ".commands.package",
    "cmd_check": ".commands.package",
    "cmd_diff_package": ".commands.package",
    "cmd_doctor": ".commands.package",
    "cmd_export_contract": ".commands.package",
    "cmd_impact_report": ".commands.package",
    "cmd_import": ".commands.package",
    "cmd_init": ".commands.package",
    "cmd_init_dispatch": ".commands.package",
    "cmd_parse_config": ".commands.package",
    "cmd_promote_package": ".commands.package",
    "cmd_run_examples": ".commands.package",
    "cmd_test_package": ".commands.package",
    "cmd_validate_config": ".commands.package",
    "cmd_build_options": ".commands.query",
    "cmd_catalog": ".commands.query",
    "cmd_compile": ".commands.query",
    "cmd_discover": ".commands.query",
    "cmd_inspect": ".commands.query",
    "cmd_packages": ".commands.query",
    "cmd_plan": ".commands.query",
    "cmd_query": ".commands.query",
    "cmd_segment_explain": ".commands.query",
    "cmd_segment_preview": ".commands.query",
    "cmd_segment_validate": ".commands.query",
    "cmd_serve": ".commands.query",
    "cmd_valid_values": ".commands.query",
    "cmd_validate": ".commands.query",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
