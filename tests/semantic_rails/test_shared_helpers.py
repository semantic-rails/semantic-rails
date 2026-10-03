from __future__ import annotations

import stat
from pathlib import Path

import pytest

from semantic_rails import atomic_files
from semantic_rails.architect_scaffold import dump_project_yaml
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config_validation import PackageReference
from semantic_rails.local_config import init_local_profile
from semantic_rails.mcp_manager import save_mcp_registry
from semantic_rails.naming import slug, title
from semantic_rails.repl.authoring import (
    _authoring_namespace,
    _model_entity_defaults,
    _package_block,
)


@pytest.mark.parametrize(
    ("value", "expected_slug", "expected_title"),
    [
        ("ordered__at", "ordered_at", "Ordered At"),
        ("status", "status", "Status"),
        ("Été 東京", "été_東京", "Été 東京"),
        ("İstanbul", "i̇stanbul", "İstanbul"),
        ("_Order--ID_", "order_id", "Order--id"),
        ("   ___   ", "", ""),
        ("", "", ""),
    ],
)
def test_authored_naming_equivalence(value, expected_slug, expected_title):
    assert slug(value) == expected_slug
    assert title(value) == expected_title
    assert slug(value, fallback="relation") == (expected_slug or "relation")
    assert title(value, fallback=value) == (expected_title or value)


@pytest.mark.parametrize("mode", [None, 0o600, 0o640])
def test_atomic_write_bytes_replaces_file_with_requested_mode(tmp_path, mode):
    path = tmp_path / "state" / "config.yml"
    atomic_files.atomic_write_bytes(path, b"old", mode=mode)
    atomic_files.atomic_write_bytes(path, "Été".encode(), mode=mode)
    assert path.read_bytes() == "Été".encode()
    assert stat.S_IMODE(path.stat().st_mode) == (0o600 if mode is None else mode)
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["fsync", "chmod", "replace"])
def test_atomic_write_failure_preserves_destination(tmp_path, monkeypatch, operation):
    path = tmp_path / "state.yml"
    path.write_bytes(b"old")

    def fail(*args):
        raise OSError("write failed")

    monkeypatch.setattr(atomic_files.os, operation, fail)
    with pytest.raises(OSError, match="write failed"):
        atomic_files.atomic_write_bytes(path, b"new", mode=0o600)
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("writer", ["profile", "registry"])
def test_local_state_files_are_private(tmp_path, monkeypatch, writer):
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    if writer == "registry":
        path = save_mcp_registry({"version": 1, "servers": {}})
    else:
        package = tmp_path / "package"
        package.mkdir()
        (package / "package.yml").write_text("package: {id: example}\n")
        path = tmp_path / "home" / "profiles.yml"
        init_local_profile(package_path=str(package), path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_repl_yaml_round_trip_preserves_boolean_like_strings(tmp_path: Path):
    package_path = tmp_path / "package.yml"
    package_path.write_text("package: {id: on, labels: [no, on]}\n", encoding="utf-8")
    (tmp_path / "graph.yml").write_text(
        "graph: {entities: {event: {model: events, key: [no, on]}}}\n", encoding="utf-8"
    )
    ref = PackageReference(str(tmp_path))
    project = ArchitectProject(tmp_path)
    assert _model_entity_defaults(project, "events")["primary_key"] == ["no", "on"]
    assert _authoring_namespace(project) == "on"
    package = _package_block(ref)
    assert package == {"id": "on", "labels": ["no", "on"]}
    package_path.write_text(dump_project_yaml({"package": package}), encoding="utf-8")
    assert _package_block(ref) == package
