"""The CLI and REPL packages import cleanly in any order.

``semantic_rails.repl`` needs ``semantic_rails.cli.common``, and the CLI's
commands need the REPL, so the package ``__init__`` files resolve their
re-exports lazily. Each module must import first, alone, in a fresh
interpreter.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys

import pytest

import semantic_rails.cli
import semantic_rails.repl


def _modules() -> list[str]:
    names = ["semantic_rails.cli", "semantic_rails.repl", "semantic_rails.dev_cli"]
    for package in (semantic_rails.cli, semantic_rails.repl):
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
            names.append(info.name)
    return sorted(names)


@pytest.mark.parametrize("module", _modules())
def test_each_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0, proc.stderr


def test_lazy_re_exports_resolve_and_are_listed() -> None:
    from semantic_rails.cli import cmd_init, main
    from semantic_rails.repl import run_interactive_shell

    assert callable(main) and callable(cmd_init) and callable(run_interactive_shell)
    assert {"main", "cmd_init", "cmd_mcp_stdio"} <= set(dir(semantic_rails.cli))
    with pytest.raises(AttributeError):
        _ = semantic_rails.cli.no_such_name
