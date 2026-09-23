"""Line-based prompts, choices and confirmations used by the REPL authoring wizards."""

from __future__ import annotations

from ..cli.common import _slug


class _AuthoringCancelled(Exception):
    """Return from a nested authoring wizard without ending the REPL."""


def _author_choice(label: str, options: list[tuple[str, str]], *, default: str) -> str:
    print(f"{label}")
    for index, (value, description) in enumerate(options, start=1):
        recommended = " (recommended)" if value == default else ""
        print(f"  {index}. {description}{recommended}")
    aliases = {str(index): value for index, (value, _) in enumerate(options, start=1)}
    values = {value for value, _ in options}
    while True:
        raw = _author_prompt("Choose", default).lower()
        selected = aliases.get(raw, raw)
        if selected == "cancel":
            raise _AuthoringCancelled
        if selected in values:
            return selected
        print("Choose a number or one of: " + ", ".join(value for value, _ in options))


def _author_prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _AuthoringCancelled from exc
    if value.lower() in {"cancel", ":q", "quit"}:
        raise _AuthoringCancelled
    return value or default


def _author_confirm(label: str, *, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        value = input(f"{label}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _AuthoringCancelled from exc
    if value in {"cancel", ":q", "quit"}:
        raise _AuthoringCancelled
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def _author_slug_prompt(label: str, default: str) -> str:
    raw = _author_prompt(label, default)
    value = _slug(raw, fallback=default)
    if value != raw:
        print(f"  normalized `{raw}` -> `{value}`")
    return value
