"""The interactive ``semantic-rails repl`` and its guided authoring wizards.

``run_interactive_shell`` resolves lazily, so importing a single submodule
(for example :mod:`semantic_rails.repl.backend`) stays light and cycle-free.
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_interactive_shell"]


def __getattr__(name: str) -> Any:
    if name != "run_interactive_shell":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .shell import run_interactive_shell

    globals()[name] = run_interactive_shell
    return run_interactive_shell
