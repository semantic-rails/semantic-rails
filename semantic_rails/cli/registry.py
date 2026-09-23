"""Command-registration hook for the ``semantic-rails`` CLI.

Other packages add commands, or extend existing ones, without editing the
CLI modules. An extension is a ``register(registry)`` function exposed as an
entry point in the ``semantic_rails.cli`` group::

    [project.entry-points."semantic_rails.cli"]
    ossie = "semantic_rails.interop.ossie.cli:register"

``register`` only records operations. They are applied on top of the
built-in command tree when the parser is built, and a conflict (shadowing a
built-in, extending a missing command) raises
:class:`CommandRegistrationError`. Entry points shipped in the
``semantic-rails`` distribution are built in: they always load and any
failure raises. Entry points from other distributions are plugins: one that
fails to load is skipped with a warning on stderr, and
``SEMANTIC_RAILS_CLI_PLUGINS=0`` turns all plugins off.

``register`` must not print, prompt or do I/O: every CLI run imports it,
including ``mcp stdio``, whose stdout is the protocol channel. ``configure``
and ``wrap`` callbacks may run more than once (a plugin is test-applied
before it is accepted), so they must only add arguments or wrap handlers.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, TextIO

from ..config import list_package_ids
from ..config_validation import PackageReference
from ..runtime import Runtime
from .common import (
    _add_optional_package_or_path_args,
    _print,
    _prompts_allowed,
    _ref_from_args,
    _runtime_from_ref,
)

Handler = Callable[[argparse.Namespace], None]
Configure = Callable[[argparse.ArgumentParser], None]
Wrap = Callable[[Handler], Handler]

ENTRY_POINT_GROUP = "semantic_rails.cli"
PLUGINS_ENV = "SEMANTIC_RAILS_CLI_PLUGINS"
_DISTRIBUTION = "semantic-rails"

__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGINS_ENV",
    "CommandRegistrationError",
    "CommandRegistry",
    "add_package_arguments",
    "load_extensions",
    "package_ref_from_args",
    "print_json",
    "prompts_allowed",
    "runtime_from_args",
]


class CommandRegistrationError(ValueError):
    """An extension named a command path that clashes with, or misses, the tree."""


@dataclass(frozen=True)
class _Operation:
    kind: str
    path: tuple[str, ...]
    options: dict[str, Any] = field(default_factory=dict)


class CommandRegistry:
    """Records command additions and extensions, then applies them to a parser."""

    api_version = 1

    def __init__(self) -> None:
        self._operations: list[_Operation] = []

    def add_group(self, path: Sequence[str], *, help: str = "", description: str = "") -> None:
        """Add a command group such as ``("cloud",)``; an existing group is kept as is."""

        self._record("group", path, help=help, description=description)

    def add_command(
        self,
        path: Sequence[str],
        handler: Handler,
        *,
        help: str = "",
        description: str = "",
        configure: Configure | None = None,
        human: bool = False,
    ) -> None:
        """Add a new command; missing parent groups are created.

        ``human=True`` prints errors as ``error [CODE]: message`` unless the
        command was given ``--json``; otherwise errors are the JSON envelope.
        """

        self._record(
            "command",
            path,
            handler=handler,
            help=help,
            description=description,
            configure=configure,
            human=human,
        )

    def extend_command(
        self,
        path: Sequence[str],
        *,
        configure: Configure | None = None,
        wrap: Wrap | None = None,
    ) -> None:
        """Add arguments to an existing command and/or wrap its handler.

        ``wrap`` receives the current handler and returns the one to run. It may
        call the original around its own code, or replace it by not calling it.
        Wraps compose in load order.
        """

        self._record("extend", path, configure=configure, wrap=wrap)

    def add_import_source(
        self,
        name: str,
        handler: Handler,
        *,
        help: str = "",
        configure: Configure | None = None,
    ) -> None:
        """Add ``semantic-rails import --from <name>``."""

        self._record("import_source", (name,), handler=handler, help=help, configure=configure)

    def add_export_format(
        self,
        name: str,
        handler: Handler,
        *,
        help: str = "",
        configure: Configure | None = None,
    ) -> None:
        """Add ``semantic-rails export-contract --format <name>``."""

        self._record("export_format", (name,), handler=handler, help=help, configure=configure)

    def build_parser(self) -> argparse.ArgumentParser:
        """Build the built-in command tree, then apply every recorded operation."""

        from .app import build_parser

        parser = build_parser()
        for operation in self._operations:
            _apply(parser, operation)
        return parser

    def _record(self, kind: str, path: Sequence[str], **options: Any) -> None:
        names = tuple(str(part) for part in path)
        if not names or any(not _NAME.fullmatch(name) for name in names):
            raise CommandRegistrationError(
                f"command path {path!r} must be one or more lowercase names like 'cloud' "
                "or 'client-config'"
            )
        self._operations.append(_Operation(kind, names, options))


_NAME = re.compile(r"[a-z][a-z0-9-]*")


def load_extensions(registry: CommandRegistry, *, stderr: TextIO | None = None) -> list[str]:
    """Run every ``semantic_rails.cli`` entry point against ``registry``.

    Returns the names of the extensions that loaded. Built-in entry points
    raise on failure; a failing plugin is skipped with a warning.
    """

    stream = stderr or sys.stderr
    plugins_enabled = os.environ.get(PLUGINS_ENV, "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    loaded: list[str] = []
    for entry_point in sorted(
        metadata.entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: (ep.name, ep.value)
    ):
        builtin = _is_builtin(entry_point)
        if not builtin and not plugins_enabled:
            continue
        scratch = CommandRegistry()
        if builtin:
            entry_point.load()(scratch)
        else:
            try:
                entry_point.load()(scratch)
                # Prove the plugin applies cleanly before accepting any of it.
                probe = CommandRegistry()
                probe._operations = [*registry._operations, *scratch._operations]
                probe.build_parser()
            except Exception as exc:  # noqa: BLE001 - a broken plugin must not break the CLI
                source = _distribution_name(entry_point) or "unknown distribution"
                print(
                    f"semantic-rails: skipped CLI plugin {entry_point.name!r} from {source}: "
                    f"{type(exc).__name__}: {exc}",
                    file=stream,
                )
                continue
        registry._operations.extend(scratch._operations)
        loaded.append(entry_point.name)
    return loaded


def _is_builtin(entry_point: metadata.EntryPoint) -> bool:
    return _distribution_name(entry_point) == _DISTRIBUTION


def _distribution_name(entry_point: metadata.EntryPoint) -> str:
    dist = getattr(entry_point, "dist", None)
    name = str(getattr(dist, "name", "") or "")
    return re.sub(r"[-_.]+", "-", name).lower()


def _apply(parser: argparse.ArgumentParser, operation: _Operation) -> None:
    options = operation.options
    if operation.kind == "group":
        _ensure_group(parser, operation.path, options["help"], options["description"])
    elif operation.kind == "command":
        parent = _ensure_group(parser, operation.path[:-1], "", "")
        subparsers = _group_subparsers(parent, operation.path[:-1])
        name = operation.path[-1]
        if name in subparsers.choices:
            raise CommandRegistrationError(f"'{' '.join(operation.path)}' already exists")
        command = subparsers.add_parser(
            name, help=options["help"] or None, description=options["description"] or None
        )
        if options["configure"] is not None:
            options["configure"](command)
        command.set_defaults(func=options["handler"], human_cli=options["human"])
    elif operation.kind == "extend":
        command = _find(parser, operation.path)
        if command is None or command.get_default("func") is None:
            raise CommandRegistrationError(f"no command '{' '.join(operation.path)}' to extend")
        if options["configure"] is not None:
            options["configure"](command)
        if options["wrap"] is not None:
            command.set_defaults(func=options["wrap"](command.get_default("func")))
    else:
        _add_choice(parser, operation)


def _add_choice(parser: argparse.ArgumentParser, operation: _Operation) -> None:
    command_path, dest, handlers_key = {
        "import_source": (("import",), "source_format", "import_sources"),
        "export_format": (("export-contract",), "format", "export_formats"),
    }[operation.kind]
    command = _find(parser, command_path)
    action = next(
        (item for item in (command._actions if command else []) if item.dest == dest), None
    )
    name = operation.path[0]
    if command is None or action is None or action.choices is None:
        raise CommandRegistrationError(f"'{' '.join(command_path)}' has no {dest} choices")
    if name in action.choices:
        raise CommandRegistrationError(f"'{name}' is already a {dest} choice")
    action.choices = [*action.choices, name]
    handlers = dict(command.get_default(handlers_key) or {})
    handlers[name] = operation.options["handler"]
    command.set_defaults(**{handlers_key: handlers})
    if operation.options["configure"] is not None:
        operation.options["configure"](command)


def _subparsers(parser: argparse.ArgumentParser) -> Any:
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def _find(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> Any:
    current: Any = parser
    for name in path:
        subparsers = _subparsers(current)
        if subparsers is None or name not in subparsers.choices:
            return None
        current = subparsers.choices[name]
    return current


def _group_subparsers(group: Any, path: tuple[str, ...]) -> Any:
    subparsers = _subparsers(group)
    if subparsers is None:
        if group.get_default("func") is not None:
            raise CommandRegistrationError(f"'{' '.join(path)}' is a command, not a group")
        subparsers = group.add_subparsers(dest="_".join(path).replace("-", "_") + "_cmd")
        subparsers.required = True
    return subparsers


def _ensure_group(
    parser: argparse.ArgumentParser, path: tuple[str, ...], help: str, description: str
) -> Any:
    current: Any = parser
    for depth, name in enumerate(path, start=1):
        subparsers = _group_subparsers(current, path[: depth - 1])
        if name not in subparsers.choices:
            last = depth == len(path)
            subparsers.add_parser(
                name,
                help=(help if last else "") or None,
                description=(description if last else "") or None,
            )
        current = subparsers.choices[name]
        if current.get_default("func") is not None:
            raise CommandRegistrationError(f"'{' '.join(path[:depth])}' is a command, not a group")
    return current


def add_package_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the standard mutually exclusive ``--package`` / ``--path`` pair."""

    _add_optional_package_or_path_args(parser, list_package_ids())


def package_ref_from_args(
    args: argparse.Namespace, *, interactive: bool = False
) -> PackageReference:
    """Resolve the package with the CLI's one selection policy.

    An explicit flag wins, then a package directory around the working
    directory, then the local profile. Otherwise it raises guidance, or at an
    interactive terminal (``interactive=True``) first offers the sample package.
    """

    return _ref_from_args(args, interactive=interactive and prompts_allowed(args))


def runtime_from_args(args: argparse.Namespace) -> Runtime:
    """Load the runtime for the package the arguments select."""

    return _runtime_from_ref(package_ref_from_args(args))


def print_json(payload: dict[str, Any]) -> None:
    """Print a JSON result with the CLI's ``ok`` envelope."""

    _print(payload)


def prompts_allowed(args: argparse.Namespace) -> bool:
    """True only when stdin and stdout are terminals and ``--json`` isn't set."""

    return _prompts_allowed(args)
