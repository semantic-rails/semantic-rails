"""Line-based prompts, choices and confirmations used by the REPL authoring wizards.

Each helper asks through the active :mod:`~semantic_rails.repl.backend`: plain
``input()`` prompts, or arrow-key pickers when ``semantic-rails[repl]`` is
installed and the REPL runs in a terminal.
"""

from __future__ import annotations

from ..cli.common import _slug
from .backend import Cancelled as _AuthoringCancelled
from .backend import current_backend

__all__ = [
    "_AuthoringCancelled",
    "_author_choice",
    "_author_confirm",
    "_author_multi_choice",
    "_author_prompt",
    "_author_slug_prompt",
]


def _author_choice(label: str, options: list[tuple[str, str]], *, default: str) -> str:
    return current_backend().choose(label, options, default=default)


def _author_multi_choice(
    label: str, options: list[tuple[str, str]], *, defaults: list[str]
) -> list[str]:
    return current_backend().multi_choose(label, options, defaults=defaults)


def _author_prompt(label: str, default: str = "") -> str:
    return current_backend().text(label, default=default)


def _author_confirm(label: str, *, default: bool) -> bool:
    return current_backend().confirm(label, default=default)


def _author_slug_prompt(label: str, default: str) -> str:
    raw = _author_prompt(label, default)
    value = _slug(raw, fallback=default)
    if value != raw:
        print(f"  normalized `{raw}` -> `{value}`")
    return value
