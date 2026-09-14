"""Package consumers must agree on one captured semantic generation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import load_package_config, load_package_snapshot
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import capture_package_source
from tests.semantic_rails.conftest import copy_package_config


def _rename_package(path: Path, name: str) -> None:
    package = path / "package.yml"
    raw = yaml.safe_load(package.read_text())
    raw["package"]["name"] = name
    package.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_loaded_snapshot_is_isolated_from_sources_and_returned_views(tmp_path):
    path = copy_package_config(tmp_path, "jaffle_shop")
    snapshot = load_package_snapshot(path)
    config = snapshot.config
    config.measures[0].aliases.append("mutated")
    snapshot.authored["package"]["name"] = "mutated"
    snapshot.normalized["package"]["name"] = "mutated"
    snapshot.semantic["package"]["name"] = "mutated"
    with pytest.raises(FrozenInstanceError):
        snapshot.source_fingerprint = "mutated"
    _rename_package(path, "new generation")
    assert "mutated" not in snapshot.config.measures[0].aliases
    assert snapshot.config.package.name != "new generation"
    assert snapshot.authored["package"]["name"] != "mutated"
    assert snapshot.normalized["package"]["name"] != "mutated"
    assert snapshot.semantic["package"]["name"] != "mutated"
    assert load_package_snapshot(snapshot) is snapshot
    assert load_package_config(snapshot) == snapshot.config
    refreshed = load_package_snapshot(path)
    assert refreshed.config.package.name == "new generation"
    assert refreshed.source_fingerprint != snapshot.source_fingerprint
    assert refreshed.semantic_fingerprint != snapshot.semantic_fingerprint


def test_source_edit_during_parse_cannot_mix_loaded_views(tmp_path, monkeypatch):
    path = copy_package_config(tmp_path, "jaffle_shop")
    original = load_package_snapshot(path)
    parse = config_module._parse_package

    def parse_and_edit(raw, *, path):
        _rename_package(Path(path), "edited while parsing")
        return parse(raw, path=path)

    monkeypatch.setattr(config_module, "_parse_package", parse_and_edit)
    captured = load_package_snapshot(path)
    assert captured.config == original.config
    assert captured.authored == original.authored
    assert captured.normalized == original.normalized
    assert captured.semantic_fingerprint == original.semantic_fingerprint
    assert captured.source_fingerprint == original.source_fingerprint
    assert captured.provenance == original.provenance


def test_unstable_source_capture_fails_closed(tmp_path, monkeypatch):
    path = copy_package_config(tmp_path, "jaffle_shop")
    read = Path.read_bytes
    revision = 0

    def read_and_edit(file):
        nonlocal revision
        data = read(file)
        if file == path / "package.yml":
            revision += 1
            _rename_package(path, f"revision {revision}")
        return data

    monkeypatch.setattr(Path, "read_bytes", read_and_edit)
    with pytest.raises(SemanticLayerError, match="sources changed"):
        capture_package_source(path)


def test_semantic_identity_excludes_deployment_locators(tmp_path):
    path = copy_package_config(tmp_path, "jaffle_shop")
    before = load_package_snapshot(path)
    package = path / "package.yml"
    raw = yaml.safe_load(package.read_text())
    raw["package"]["default_db"] = "another-local-file.duckdb"
    raw["package"]["connection"] = {"name": "another-connection"}
    package.write_text(yaml.safe_dump(raw, sort_keys=False))
    after = load_package_snapshot(path)
    assert after.source_fingerprint != before.source_fingerprint
    assert after.semantic_fingerprint == before.semantic_fingerprint
