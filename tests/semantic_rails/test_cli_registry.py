"""The CLI command-registration hook (``semantic_rails.cli.registry``).

Other packages add ``semantic-rails`` commands, or extend existing ones,
through the ``semantic_rails.cli`` entry-point group without editing the CLI.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.cli import app as cli_app
from semantic_rails.cli import registry as registry_module
from semantic_rails.cli.registry import (
    ENTRY_POINT_GROUP,
    CommandRegistrationError,
    CommandRegistry,
    add_package_arguments,
    load_extensions,
    package_ref_from_args,
    print_json,
)
from semantic_rails.errors import SemanticLayerError


def _run(parser: argparse.ArgumentParser, *argv: str) -> Any:
    args = parser.parse_args(list(argv))
    return args.func(args)


def test_without_extensions_the_tree_is_the_builtin_one() -> None:
    assert CommandRegistry().build_parser().format_help() == cli_app.build_parser().format_help()


def test_add_command_under_a_new_group_runs_its_handler(capsys: pytest.CaptureFixture[str]) -> None:
    registry = CommandRegistry()
    registry.add_group(("cloud",), help="Semantic Rails Cloud commands.")
    registry.add_command(
        ("cloud", "link"),
        lambda args: print(f"linked {args.workspace}"),
        help="Link this package to a workspace.",
        configure=lambda parser: parser.add_argument("--workspace", required=True),
        human=True,
    )
    parser = registry.build_parser()

    _run(parser, "cloud", "link", "--workspace", "acme")

    assert capsys.readouterr().out == "linked acme\n"
    assert parser.parse_args(["cloud", "link", "--workspace", "a"]).human_cli is True
    assert "cloud" in parser.format_help()
    with pytest.raises(SystemExit):
        parser.parse_args(["cloud"])  # a group needs a subcommand


def test_add_command_under_a_builtin_group(capsys: pytest.CaptureFixture[str]) -> None:
    registry = CommandRegistry()
    registry.add_command(("mcp", "hello"), lambda _args: print("hello"))

    _run(registry.build_parser(), "mcp", "hello")

    assert capsys.readouterr().out == "hello\n"


def _noop(_args: argparse.Namespace) -> None:
    pass


@pytest.mark.parametrize(
    "record",
    [
        lambda r: r.add_command(("ask",), _noop),  # shadows a built-in command
        lambda r: r.add_command(("mcp", "stdio"), _noop),  # shadows a built-in subcommand
        lambda r: r.add_command(("ask", "more"), _noop),  # the parent is a command
        lambda r: r.add_group(("serve",)),  # a command is not a group
        lambda r: r.extend_command(("no-such-command",), configure=lambda _p: None),
        lambda r: r.extend_command(("mcp",), configure=lambda _p: None),  # a group
        lambda r: r.add_import_source("metricflow", _noop),  # built-in source
        lambda r: r.add_export_format("metrics", _noop),  # built-in format
    ],
)
def test_conflicts_fail_loudly(record: Any) -> None:
    registry = CommandRegistry()
    record(registry)

    with pytest.raises(CommandRegistrationError):
        registry.build_parser()


@pytest.mark.parametrize("path", [(), ("Cloud",), ("cloud", "link now"), ("-x",)])
def test_command_names_are_checked_when_recorded(path: tuple[str, ...]) -> None:
    with pytest.raises(CommandRegistrationError):
        CommandRegistry().add_command(path, _noop)


def test_extend_command_adds_arguments_and_wraps_the_handler() -> None:
    seen: list[tuple[str, str]] = []

    def with_interface(handler: Any) -> Any:
        def run(args: argparse.Namespace) -> None:
            seen.append((args.interface, handler.__name__))

        return run

    registry = CommandRegistry()
    registry.extend_command(
        ("mcp", "stdio"),
        configure=lambda parser: parser.add_argument(
            "--interface", choices=["v1", "v2"], default="v1"
        ),
        wrap=with_interface,
    )

    _run(registry.build_parser(), "mcp", "stdio", "--interface", "v2")

    assert seen == [("v2", "cmd_mcp_stdio")]


def test_wraps_compose_in_load_order() -> None:
    calls: list[str] = []

    def tag(name: str) -> Any:
        def wrap(handler: Any) -> Any:
            def run(args: argparse.Namespace) -> None:
                calls.append(name)
                handler(args)

            return run

        return wrap

    registry = CommandRegistry()
    registry.add_command(("demo",), lambda _args: calls.append("handler"))
    registry.extend_command(("demo",), wrap=tag("first"))
    registry.extend_command(("demo",), wrap=tag("second"))

    _run(registry.build_parser(), "demo")

    assert calls == ["second", "first", "handler"]


def test_import_sources_and_export_formats_dispatch_to_extensions(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []
    registry = CommandRegistry()
    registry.add_import_source(
        "ossie",
        lambda args: seen.append(("import", args.source, args.ossie_version)),
        configure=lambda parser: parser.add_argument("--ossie-version", default="0.1.1"),
    )
    registry.add_export_format("ossie", lambda args: seen.append(("export", args.format)))
    parser = registry.build_parser()
    import_args = ("--source", "model.yml", "--output", str(tmp_path), "--package-id", "x")

    _run(parser, "import", "--from", "ossie", *import_args)
    _run(parser, "export-contract", "--package", "jaffle_shop", "--format", "ossie")

    assert seen == [("import", "model.yml", "0.1.1"), ("export", "ossie")]
    assert not any(tmp_path.iterdir())  # the built-in importer never ran


class _EntryPoint:
    def __init__(self, name: str, register: Any, dist: str = "acme-semantic-rails-plugin") -> None:
        self.name, self.value, self._register = name, f"acme.cli:{name}", register
        self.dist = SimpleNamespace(name=dist)

    def load(self) -> Any:
        if isinstance(self._register, Exception):
            raise self._register
        return self._register


def _install(monkeypatch: pytest.MonkeyPatch, *entry_points: _EntryPoint) -> None:
    def entry_points_for(group: str) -> list[_EntryPoint]:
        return list(entry_points) if group == ENTRY_POINT_GROUP else []

    monkeypatch.setattr(registry_module, "metadata", SimpleNamespace(entry_points=entry_points_for))


def _adds(path: tuple[str, ...], text: str) -> Any:
    return lambda registry: registry.add_command(path, lambda _args: print(text))


def test_plugins_load_in_name_order_and_a_broken_one_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _EntryPoint("zeta", _adds(("zeta",), "z")),
        _EntryPoint("broken", RuntimeError("import failed")),
        _EntryPoint("shadow", _adds(("ask",), "hijacked")),
        _EntryPoint("alpha", _adds(("alpha",), "a")),
    )
    warnings = io.StringIO()
    registry = CommandRegistry()

    loaded = load_extensions(registry, stderr=warnings)
    parser = registry.build_parser()

    assert loaded == ["alpha", "zeta"]
    assert parser.parse_args(["alpha"]).func is not None
    assert parser.parse_args(["ask", "q"]).func.__name__ == "cmd_ask"  # not shadowed
    assert warnings.getvalue().splitlines() == [
        "semantic-rails: skipped CLI plugin 'broken' from acme-semantic-rails-plugin: "
        "RuntimeError: import failed",
        "semantic-rails: skipped CLI plugin 'shadow' from acme-semantic-rails-plugin: "
        "CommandRegistrationError: 'ask' already exists",
    ]


def test_builtin_extensions_raise_and_cannot_be_turned_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_CLI_PLUGINS", "0")
    _install(
        monkeypatch,
        _EntryPoint("core", _adds(("core-extra",), "core"), dist="semantic_rails"),
        _EntryPoint("plugin", _adds(("plugin",), "plugin")),
    )
    registry = CommandRegistry()

    assert load_extensions(registry, stderr=io.StringIO()) == ["core"]

    _install(monkeypatch, _EntryPoint("core", RuntimeError("bug"), dist="semantic-rails"))
    with pytest.raises(RuntimeError, match="bug"):
        load_extensions(CommandRegistry())


def test_main_runs_a_plugin_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _EntryPoint("hello", _adds(("hello",), "hello from a plugin")))
    monkeypatch.setattr(sys, "argv", ["semantic-rails", "hello"])

    cli_app.main()

    assert capsys.readouterr().out == "hello from a plugin\n"


def test_plugin_errors_render_like_builtin_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_args: argparse.Namespace) -> None:
        raise SemanticLayerError("INVALID_CONFIG", "workspace not linked")

    def register(registry: CommandRegistry) -> None:
        registry.add_command(("cloud", "status"), fail, human=True)
        registry.add_command(("cloud", "raw"), fail)

    _install(monkeypatch, _EntryPoint("cloud", register))
    for argv, stream in ((["cloud", "status"], "err"), (["cloud", "raw"], "out")):
        monkeypatch.setattr(sys, "argv", ["semantic-rails", *argv])
        with pytest.raises(SystemExit):
            cli_app.main()
        captured = capsys.readouterr()
        if stream == "err":
            assert captured.err == "error [INVALID_CONFIG]: workspace not linked\n"
        else:
            assert json.loads(captured.out)["error"]["message"] == "workspace not linked"


def test_stable_helpers_for_extension_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = argparse.ArgumentParser()
    add_package_arguments(parser)
    assert parser.parse_args(["--package", "jaffle_shop"]).package == "jaffle_shop"
    with pytest.raises(SystemExit):
        parser.parse_args(["--package", "jaffle_shop", "--path", "x"])

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    with pytest.raises(SemanticLayerError) as exc:
        package_ref_from_args(argparse.Namespace(package="", path="", json=False))
    assert exc.value.details["reason"] == "no_package_selected"
    assert package_ref_from_args(argparse.Namespace(package="jaffle_shop", path="")).package_id

    print_json({"linked": True})
    assert json.loads(capsys.readouterr().out) == {"linked": True, "ok": True}
