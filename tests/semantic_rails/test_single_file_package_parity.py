"""Artifact/manifest parity for single-file packages.

A package authored as a lone ``package.yml`` (``--path pkg/package.yml``)
must behave like a directory package at the packaging boundary:

- ``build-package`` / ``check --artifact`` must include the seed SQL the
  config references via ``package.seed`` plus the sibling ``examples/``
  and ``tests/`` directories, so a check-passing artifact can hydrate
  its own warehouse. Previously the tarball contained only the manifest
  and the package YAML.
- ``validate-config`` must write ``.compiled/manifest.json`` next to the
  package file (it used to fail silently for single-file packages).
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from semantic_rails import cli as cli_module
from semantic_rails.cli import cmd_init
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import (
    ARTIFACT_MANIFEST_NAME,
    build_package_artifact_report,
)


def _scaffold_single_file_package(tmp_path: Path, package_id: str = "starter_demo") -> Path:
    """Scaffold via ``semantic-rails init`` and return the package.yml path.

    ``namespace="shop"`` keeps the starter's hard-coded derived-metric
    reference (``measure.shop.line_revenue_usd``) consistent.
    """
    target = tmp_path / package_id
    cmd_init(
        argparse.Namespace(output=str(target), package_id=package_id, namespace="shop", force=False)
    )
    return target / "package.yml"


def _write_examples_and_tests(package_yml: Path) -> None:
    config = load_package_config(str(package_yml))
    order_count = next(row.id for row in config.measures if row.id.endswith("order_count"))
    query = {
        "version": 1,
        "select": [{"expression": {"measure": order_count}, "as": "orders"}],
    }
    examples_path = package_yml.parent / "examples" / "core.yml"
    examples_path.parent.mkdir(parents=True, exist_ok=True)
    examples_path.write_text(
        yaml.safe_dump(
            {"examples": {"total_orders": {"query": query, "expected_shape": {"min_rows": 1}}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tests_path = package_yml.parent / "tests" / "basic.yml"
    tests_path.parent.mkdir(parents=True, exist_ok=True)
    tests_path.write_text(
        yaml.safe_dump(
            {
                "tests": {
                    "orders_bounds": {
                        "kind": "query_row_count_bounds",
                        "query": query,
                        "min_rows": 1,
                        "max_rows": 1,
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_single_file_artifact_includes_seed_examples_and_tests(tmp_path: Path):
    package_yml = _scaffold_single_file_package(tmp_path)
    _write_examples_and_tests(package_yml)
    ref = resolve_package_reference(path=str(package_yml))

    report = build_package_artifact_report(ref, output_path=str(tmp_path / "artifact.tar.gz"))

    assert report["ok"] is True, report.get("errors")
    assert report["checks"]["examples"]["examples_total"] == 1
    assert report["checks"]["tests"]["tests_total"] == 1

    manifest = report["manifest"]
    assert manifest["source"]["layout"] == "file"
    files = manifest["source"]["files"]
    assert "package.yml" in files
    assert "data/seed_example.sql" in files, (
        "the manifest must inventory the seed SQL the package references via "
        "package.seed.source — without it the artifact cannot hydrate its warehouse"
    )
    assert "examples/core.yml" in files
    assert "tests/basic.yml" in files
    assert report["artifact"]["file_count"] == len(files)

    with tarfile.open(report["artifact"]["path"], "r:gz") as archive:
        names = set(archive.getnames())
    assert ARTIFACT_MANIFEST_NAME in names
    assert "package/package.yml" in names
    assert "package/data/seed_example.sql" in names
    assert "package/examples/core.yml" in names
    assert "package/tests/basic.yml" in names
    # Build cruft stays out: the freshly seeded DuckDB file must not ship.
    assert not any(name.endswith(".duckdb") for name in names)


def test_single_file_artifact_fails_with_structured_error_when_seed_missing(tmp_path: Path):
    package_yml = _scaffold_single_file_package(tmp_path)
    ref = resolve_package_reference(path=str(package_yml))

    # First build hydrates data/<id>.duckdb from the seed and succeeds.
    first = build_package_artifact_report(ref, output_path=str(tmp_path / "first.tar.gz"))
    assert first["ok"] is True, first.get("errors")

    # Delete the seed: checks still pass against the cached warehouse,
    # but the artifact would no longer be able to hydrate itself — the
    # build must fail with a structured error naming the missing asset.
    seed_path = package_yml.parent / "data" / "seed_example.sql"
    seed_path.unlink()
    with pytest.raises(SemanticLayerError) as excinfo:
        build_package_artifact_report(ref, output_path=str(tmp_path / "second.tar.gz"))

    error = excinfo.value
    assert error.code == "INVALID_CONFIG"
    assert "data/seed_example.sql" in str(error)
    missing = error.details["missing_assets"]
    assert missing and missing[0]["field"] == "package.seed.source"
    assert missing[0]["declared"] == "data/seed_example.sql"
    assert error.details["recovery_hints"]
    assert not (tmp_path / "second.tar.gz").exists()


def test_validate_config_writes_manifest_for_single_file_package(
    tmp_path: Path, monkeypatch, capsys
):
    package_yml = _scaffold_single_file_package(tmp_path)
    capsys.readouterr()  # drain the init scaffold's own JSON output
    monkeypatch.delenv("SR_DEV_NO_MANIFEST", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["semantic-rails", "validate-config", "--path", str(package_yml), "--quiet"]
    )
    cli_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True

    manifest_path = package_yml.parent / ".compiled" / "manifest.json"
    assert manifest_path.is_file(), (
        "validate-config must write .compiled/manifest.json next to a single-file "
        "package, exactly as it does inside a directory package"
    )

    # The manifest round-trips through the runtime read path: same source
    # path, matching fingerprint, precomputed catalog variants present.
    from semantic_rails.manifest import load_manifest

    loaded = load_manifest(str(package_yml))
    assert loaded is not None, "written manifest must load back (fingerprint must match)"
    assert loaded["catalogs"], "manifest must carry precomputed catalog variants"


def test_validate_config_honors_no_manifest_flag_for_single_file_package(
    tmp_path: Path, monkeypatch, capsys
):
    package_yml = _scaffold_single_file_package(tmp_path)
    capsys.readouterr()  # drain the init scaffold's own JSON output
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "semantic-rails",
            "validate-config",
            "--path",
            str(package_yml),
            "--quiet",
            "--no-manifest",
        ],
    )
    cli_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert not (package_yml.parent / ".compiled" / "manifest.json").exists()
