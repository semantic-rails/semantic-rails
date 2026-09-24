"""Compatibility shim for the human CLI commands, which moved in 0.3.0.

Commands now live in :mod:`semantic_rails.cli.commands.project`, report
builders in :mod:`semantic_rails.cli.reports`, and the REPL in
:mod:`semantic_rails.repl`. This module re-exports their public names for one
release so existing imports keep working; new code imports the new modules.
"""

from __future__ import annotations

from .cli.commands.project import (
    add_developer_cli,
    cmd_ask,
    cmd_debug,
    cmd_init_project,
    cmd_ls,
    cmd_profile_init,
    cmd_profile_show,
    cmd_project_list,
    cmd_project_new,
    cmd_project_status,
    cmd_project_validate,
    cmd_repl,
    cmd_setup,
)
from .cli.common import DEMO_PACKAGE_ID, default_package_ref
from .cli.interpretation import describe_query
from .cli.reports import (
    CATALOG_KINDS,
    PROJECT_CHECK_MODES,
    ask_report,
    list_objects_report,
    project_list_report,
    project_status_report,
    project_validation_report,
    setup_report,
)
from .cli.scaffold import create_project_report
from .cli.setup_wizard import cmd_setup_interactive
from .repl.shell import run_interactive_shell

__all__ = [
    "CATALOG_KINDS",
    "DEMO_PACKAGE_ID",
    "PROJECT_CHECK_MODES",
    "add_developer_cli",
    "ask_report",
    "cmd_ask",
    "cmd_debug",
    "cmd_init_project",
    "cmd_ls",
    "cmd_profile_init",
    "cmd_profile_show",
    "cmd_project_list",
    "cmd_project_new",
    "cmd_project_status",
    "cmd_project_validate",
    "cmd_repl",
    "cmd_setup",
    "cmd_setup_interactive",
    "create_project_report",
    "default_package_ref",
    "describe_query",
    "list_objects_report",
    "project_list_report",
    "project_status_report",
    "project_validation_report",
    "run_interactive_shell",
    "setup_report",
]
