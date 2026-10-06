"""CLI construction does not discover or import external command plugins."""

from importlib import metadata

import pytest

from semantic_rails.cli.app import build_parser


@pytest.mark.parametrize("setting", ["", "0", "1"])
def test_cli_does_not_discover_external_plugins(monkeypatch, setting):
    monkeypatch.setenv("SEMANTIC_RAILS_CLI_PLUGINS", setting)

    def unexpected_discovery(*args, **kwargs):
        raise AssertionError("CLI must not discover installed entry points")

    monkeypatch.setattr(metadata, "entry_points", unexpected_discovery)
    parser = build_parser()
    assert parser.parse_args(["packages"]).cmd == "packages"
    assert "hello" not in parser.format_help()
