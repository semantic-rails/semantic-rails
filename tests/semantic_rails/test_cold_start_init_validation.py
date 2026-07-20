"""Cold-start authoring contract — ``semantic-rails init`` output must work.

A first-time author's very first loop is: ``init`` a package, then run
``validate-config --path <out>/package.yml``. A cold-start evaluation
found three cliffs in that loop: the starter template authored
``entity_key: order`` (the entity name, which compiles to a nonexistent
column instead of the key column ``order_id``), a derived metric
referenced a measure package-relative (the expression AST requires the
fully-qualified ``measure.<namespace>.<key>`` form), and ``domain`` was
authored as a mapping (the loader expects a list). These tests pin the
whole loop: a fresh ``init`` output validates with zero errors, the
generated header points at the generated file (not the repo-internal
template), and the directory-form loader error mentions the single-file
``--path`` escape hatch.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from semantic_rails.cli import cmd_init
from semantic_rails.config_validation import (
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)


@pytest.fixture(scope="module")
def init_package_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("cold_start") / "first_shop"
    cmd_init(
        argparse.Namespace(output=str(target), package_id="first_shop", namespace="", force=False)
    )
    return target


def test_init_output_passes_validate_config_with_zero_errors(init_package_dir: Path) -> None:
    """The exact first-author loop: init, then validate-config --path."""
    ref = resolve_package_reference(path=str(init_package_dir / "package.yml"))
    report = validate_config_report(ref, progress=None)
    assert report["errors"] == [], (
        "a fresh `semantic-rails init` package must validate clean; got: "
        f"{[e.get('message') for e in report['errors']]}"
    )
    assert report["ok"] is True


def test_init_header_points_at_the_generated_file(init_package_dir: Path) -> None:
    """The header must tell the author to validate THEIR file via --path,
    not the repo-internal starter template path."""
    header = (init_package_dir / "package.yml").read_text(encoding="utf-8")
    assert "configs/examples/semantic_rails_package_starter.yml" not in header, (
        "the generated header must not reference the repo-internal template path"
    )
    expected = f"--path {init_package_dir.resolve() / 'package.yml'}"
    assert expected in header, f"header must reference the generated file: {expected}"
    assert "validate-config" in header


def test_init_rewrites_qualified_ids_into_the_new_namespace(init_package_dir: Path) -> None:
    """Expression-tree references are fully qualified with the template's
    `shop` namespace; init must rewrite them or derived metrics dangle."""
    text = (init_package_dir / "package.yml").read_text(encoding="utf-8")
    assert "measure.shop." not in text
    assert "measure.first_shop.line_revenue_usd" in text


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
