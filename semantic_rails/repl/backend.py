"""Prompt backends for the REPL: plain line prompts or arrow-key pickers.

The authoring wizards ask every question through :class:`PromptBackend`:

- :class:`PlainBackend` uses ``input()`` and numbered menus. It needs only the
  standard library, and it is what pipes, tests, dumb terminals and screen
  readers get.
- :class:`PickerBackend` (installed with ``semantic-rails[repl]``, which brings
  questionary and rich) offers arrow-key pickers with type-to-filter,
  checkboxes and syntax-highlighted previews.

:func:`current_backend` picks one on each use: the picker backend only when
its packages are installed and stdin and stdout are real terminals.
``SEMANTIC_RAILS_UI=plain`` forces plain prompts; ``SEMANTIC_RAILS_UI=pickers``
insists on pickers and says why when they are unavailable. Every backend
raises :class:`Cancelled` when the person cancels (``cancel``, Ctrl-C, or
Ctrl-D at a prompt they haven't edited), so no wizard writes a file.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Collection, Sequence
from typing import Any, Protocol

from ..architect_scaffold import dump_project_yaml
from ..errors import SemanticLayerError

UI_ENV = "SEMANTIC_RAILS_UI"
Option = tuple[str, str]  # (value, description shown to the person)
_CANCEL_WORDS = {"cancel", ":q", "quit"}


class Cancelled(Exception):
    """The person cancelled a prompt; return from the wizard without writing files."""


class PromptBackend(Protocol):
    name: str
    # Pickers filter long lists themselves; plain prompts need a search step first.
    filters_long_lists: bool

    def text(self, label: str, *, default: str = "") -> str: ...

    def confirm(self, label: str, *, default: bool) -> bool: ...

    def choose(self, label: str, options: Sequence[Option], *, default: str = "") -> str: ...

    def multi_choose(
        self, label: str, options: Sequence[Option], *, defaults: Collection[str] = ()
    ) -> list[str]: ...

    def show_yaml(self, payload: Any) -> None: ...


class PlainBackend:
    """``input()`` prompts and numbered menus: no dependencies, pipe- and test-friendly."""

    name = "plain"
    filters_long_lists = False

    def text(self, label: str, *, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            value = input(f"{label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise Cancelled from exc
        if value.lower() in _CANCEL_WORDS:
            raise Cancelled
        return value or default

    def confirm(self, label: str, *, default: bool) -> bool:
        suffix = " [Y/n]" if default else " [y/N]"
        try:
            value = input(f"{label}{suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt) as exc:
            raise Cancelled from exc
        if value in _CANCEL_WORDS:
            raise Cancelled
        if not value:
            return default
        return value in {"y", "yes", "true", "1"}

    def choose(self, label: str, options: Sequence[Option], *, default: str = "") -> str:
        values = [value for value, _ in options]
        if default not in values:
            default = ""  # as with pickers, a saved value that is not a choice offers no default
        print(f"{label}")
        for index, (value, description) in enumerate(options, start=1):
            recommended = " (recommended)" if value == default else ""
            print(f"  {index}. {description}{recommended}")
        aliases = {str(index): value for index, (value, _) in enumerate(options, start=1)}
        while True:
            suffix = f" [{default}]" if default else ""
            try:
                raw = input(f"Choose{suffix}: ").strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise Cancelled from exc
            if raw.lower() in _CANCEL_WORDS:
                raise Cancelled
            if not raw and default:
                return default
            # Numbered menu answers win for typed digits; Enter still picks
            # the exact default even if its value looks like a menu number.
            if raw in aliases:
                return aliases[raw]
            if raw in values:
                return raw
            matches = [value for value in values if value.casefold() == raw.casefold()]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                print("Ambiguous choice; use an exact value or a menu number")
            else:
                print("Choose a number or one of: " + ", ".join(values))

    def multi_choose(
        self, label: str, options: Sequence[Option], *, defaults: Collection[str] = ()
    ) -> list[str]:
        print(f"{label}")
        for index, (value, description) in enumerate(options, start=1):
            mark = "x" if value in defaults else " "
            print(f"  [{mark}] {index}. {description}")
        numbers = ",".join(
            str(index) for index, (value, _) in enumerate(options, start=1) if value in defaults
        )
        aliases = {str(index): value for index, (value, _) in enumerate(options, start=1)}
        values = {value for value, _ in options}
        if numbers:
            print("  Enter keeps the checked ones; type none to check nothing.")
        while True:
            raw = self.text("Choose numbers or names, comma separated", default=numbers)
            if raw.lower() == "none" and "none" not in values:
                return []
            picked = [aliases.get(part.strip(), part.strip()) for part in raw.split(",")]
            picked = [part for part in picked if part]
            unknown = [part for part in picked if part not in values]
            if not unknown:
                return [value for value, _ in options if value in picked]
            print("Unknown choice(s): " + ", ".join(unknown))

    def show_yaml(self, payload: Any) -> None:
        rendered = dump_project_yaml(payload).rstrip()
        for line in rendered.splitlines():
            print(f"  {line}")


class PickerBackend:
    """Arrow-key pickers (questionary) and highlighted previews (rich).

    ``input``/``output`` let tests drive prompt_toolkit without a terminal.
    """

    name = "pickers"
    filters_long_lists = True

    def __init__(self, *, input: Any = None, output: Any = None) -> None:
        import questionary
        from rich.console import Console

        self._questionary = questionary
        self._io = {key: value for key, value in (("input", input), ("output", output)) if value}
        self._console = Console()

    def _ask(self, question: Any) -> Any:
        _cancel_keys(question.application)
        try:
            answer = question.unsafe_ask()
        except (EOFError, KeyboardInterrupt) as exc:
            raise Cancelled from exc
        return answer

    def text(self, label: str, *, default: str = "") -> str:
        # The default is a placeholder, not text to edit: typing replaces it, Enter keeps it.
        question = self._questionary.text(label, placeholder=default, **self._io)
        value = str(self._ask(question)).strip()
        if value.lower() in _CANCEL_WORDS:
            raise Cancelled
        return value or default

    def confirm(self, label: str, *, default: bool) -> bool:
        # y or n waits for Enter, so the Enter after `y` cannot answer the next question.
        question = self._questionary.confirm(label, default=default, auto_enter=False, **self._io)
        return bool(self._ask(question))

    def choose(self, label: str, options: Sequence[Option], *, default: str = "") -> str:
        choices = [
            self._questionary.Choice(
                title=description + (" (recommended)" if value == default else ""), value=value
            )
            for value, description in options
        ]
        chosen = next((choice for choice in choices if choice.value == default), None)
        question = self._questionary.select(
            label,
            choices=choices,
            default=chosen,
            use_search_filter=True,  # also collects a typed cancel word
            use_jk_keys=False,
            instruction="(arrows to move, type to filter, Enter to pick, Ctrl-C to cancel)",
            **self._io,
        )
        return str(self._ask(question))

    def multi_choose(
        self, label: str, options: Sequence[Option], *, defaults: Collection[str] = ()
    ) -> list[str]:
        choices = [
            self._questionary.Choice(title=description, value=value, checked=value in defaults)
            for value, description in options
        ]
        question = self._questionary.checkbox(
            label,
            choices=choices,
            instruction="(arrows to move, space to toggle, Enter to confirm, Ctrl-C to cancel)",
            **self._io,
        )
        return [str(value) for value in self._ask(question) or []]

    def show_yaml(self, payload: Any) -> None:
        from rich.padding import Padding
        from rich.syntax import Syntax

        rendered = dump_project_yaml(payload).rstrip()
        self._console.print(
            Padding(Syntax(rendered, "yaml", background_color="default", word_wrap=True), (0, 2))
        )


def _cancel_keys(app: Any) -> None:
    """Cancel a picker the way a line prompt cancels.

    questionary ends only an empty text prompt on Ctrl-D. This also cancels
    lists and checkbox lists on Ctrl-D, and a list when the person types a
    cancel word and presses Enter. Once the text is edited, Ctrl-D edits it.
    """

    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from questionary.prompts.common import InquirerControl

    lists = [item for item in app.layout.find_all_controls() if isinstance(item, InquirerControl)]

    @Condition
    def unedited() -> bool:
        return not app.current_buffer.text  # a dummy empty buffer for lists

    @Condition
    def typed_cancel() -> bool:
        # A cancel word that filters to a real option, such as "Cancelled orders", picks it.
        return any(
            (typed := (item.search_filter or "").strip().lower()) in _CANCEL_WORDS
            and not any(typed in str(choice.title).lower() for choice in item.choices)
            for item in lists
        )

    bindings = KeyBindings()

    @bindings.add("c-d", filter=unedited, eager=True)
    @bindings.add("enter", filter=typed_cancel, eager=True)
    def _cancel(event: Any) -> None:
        event.app.exit(exception=EOFError(), style="class:exiting")

    app.key_bindings = merge_key_bindings(
        [existing for existing in (app.key_bindings, bindings) if existing is not None]
    )


def pickers_available() -> bool:
    return all(importlib.util.find_spec(name) for name in ("questionary", "rich"))


def _real_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty()) and os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def select_backend(stdin: Any = None, stdout: Any = None) -> PromptBackend:
    """Choose the backend for this process (see the module docstring)."""

    mode = os.environ.get(UI_ENV, "").strip().lower() or "auto"
    if mode not in {"auto", "plain", "pickers"}:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{UI_ENV} must be auto, plain or pickers (got {mode!r})",
            details={"reason": "unknown_ui_mode"},
        )
    if mode == "plain":
        return PlainBackend()
    terminal = (
        _real_terminal(stdin or sys.stdin)
        and _real_terminal(stdout or sys.stdout)
        and os.environ.get("TERM", "").lower() != "dumb"
    )
    if terminal and pickers_available():
        return PickerBackend()
    if mode == "pickers":
        why = (
            "install them with: pip install 'semantic-rails[repl]'"
            if not pickers_available()
            else "stdin and stdout must be an interactive terminal"
        )
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{UI_ENV}=pickers needs arrow-key pickers; {why}",
            details={"reason": "pickers_unavailable"},
        )
    return PlainBackend()


_pinned: PromptBackend | None = None


def current_backend() -> PromptBackend:
    """The pinned backend, else a fresh choice for the current terminal and environment."""

    return _pinned if _pinned is not None else select_backend()


def set_backend(backend: PromptBackend | None) -> None:
    """Pin ``backend`` for every prompt (``None`` unpins). For tests and embedders."""

    global _pinned
    _pinned = backend
