"""The CLI and REPL packages import cleanly in any order.

``semantic_rails.repl`` needs ``semantic_rails.cli.common``, so the CLI imports
the REPL only inside the functions that start it. Each module must import
first, alone, in a fresh interpreter.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys

import pytest

import semantic_rails.cli
import semantic_rails.repl


def _modules() -> list[str]:
    names = ["semantic_rails.cli", "semantic_rails.repl"]
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
