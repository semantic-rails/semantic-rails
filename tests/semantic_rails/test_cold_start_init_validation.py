"""Standalone authored packages validate and retain directory-path guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

from semantic_rails.config_validation import (
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)
from tests.semantic_rails.conftest import write_single_file_package


@pytest.fixture(scope="module")
def init_package_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("cold_start") / "first_shop"
    write_single_file_package(target)
    return target


def test_single_file_package_validates_with_zero_errors(init_package_dir: Path) -> None:
    """validate-config keeps accepting authored standalone YAML."""
    ref = resolve_package_reference(path=str(init_package_dir / "package.yml"))
    report = validate_config_report(ref, progress=None)
    assert report["errors"] == [], (
        "an authored single-file package must validate clean; got: "
        f"{[e.get('message') for e in report['errors']]}"
    )
    assert report["ok"] is True


def test_parse_config_directory_form_hints_at_single_file_path(init_package_dir: Path) -> None:
    """`parse-config --path <dir>` on a single-file package (package.yml,
    no graph.yml) must point at the `--path <dir>/package.yml` form
    instead of dead-ending on 'graph.yml is missing'."""
    ref = resolve_package_reference(path=str(init_package_dir))
    report, _ = parse_config_report(ref, progress=None)
    assert report["ok"] is False
    messages = [str(error.get("message", "")) for error in report["errors"]]
    graph_errors = [message for message in messages if "graph.yml is missing" in message]
    assert graph_errors, f"expected a graph.yml-is-missing error, got: {messages}"
    hint = graph_errors[0]
    package_yml = init_package_dir.resolve() / "package.yml"
    assert f"pass --path {package_yml} to load it" in hint, (
        f"the error must mention the single-file --path form; got: {hint}"
    )
