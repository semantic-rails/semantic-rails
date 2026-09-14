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


def test_source_fingerprint_frames_names_and_contents():
    from semantic_rails.package_snapshot import CapturedSource

    separate = CapturedSource("/package", True, (("a.yml", b"x"), ("b.yml", b"y")))
    joined = CapturedSource("/package", True, (("a.yml", b"xb.ymly"),))
    assert separate.fingerprint != joined.fingerprint


def test_runtime_manifest_and_reload_share_the_loaded_generation(tmp_path):
    import json

    from semantic_rails.manifest import load_manifest, write_manifest
    from semantic_rails.runtime import Runtime

    path = copy_package_config(tmp_path, "jaffle_shop")
    runtime = Runtime.from_path(str(path))
    snapshot = runtime.snapshot
    try:
        runtime.config.measures.clear()
        assert runtime.config.measures == snapshot.config.measures
        _rename_package(path, "next generation")
        manifest = json.loads(write_manifest(runtime).read_text())
        assert manifest["fingerprint"] == snapshot.source_fingerprint
        assert manifest["semantic_fingerprint"] == snapshot.semantic_fingerprint
        assert manifest["provenance"] == dict(snapshot.provenance)
        assert load_manifest(str(path), snapshot=snapshot) is not None
        assert load_manifest(str(path)) is None
        assert runtime.manifest_catalog(view="summary", verbosity="compact") is not None
        result = runtime.reload()
        assert result["changed"] is True
        assert runtime.config.package.name == "next generation"
        assert result["semantic_fingerprint"] == runtime.snapshot.semantic_fingerprint
        assert runtime.manifest_catalog(view="summary", verbosity="compact") is None
    finally:
        runtime.close()


def test_manifest_variants_remain_one_generation_if_source_changes(tmp_path, monkeypatch):
    import json

    from semantic_rails import metadata
    from semantic_rails.manifest import write_manifest
    from semantic_rails.runtime import Runtime

    path = copy_package_config(tmp_path, "jaffle_shop")
    runtime = Runtime.from_path(str(path))
    original = metadata.catalog_payload
    calls = []

    def catalog_and_edit(runtime, **kwargs):
        calls.append(runtime.snapshot.semantic_fingerprint)
        _rename_package(path, f"edited {len(calls)}")
        return original(runtime, **kwargs)

    monkeypatch.setattr(metadata, "catalog_payload", catalog_and_edit)
    try:
        payload = json.loads(write_manifest(runtime).read_text())
        assert len(calls) > 1
        assert set(calls) == {payload["semantic_fingerprint"]}
    finally:
        runtime.close()


def test_impact_uses_captured_current_semantics_and_complete_fields(tmp_path, monkeypatch):
    from dataclasses import replace

    from semantic_rails import package_tools
    from semantic_rails.config_validation import resolve_package_reference
    from semantic_rails.package_snapshot import LoadedPackageSnapshot

    path = copy_package_config(tmp_path, "jaffle_shop")
    before = load_package_snapshot(path)
    config = before.config
    config.dimensions[0] = replace(config.dimensions[0], column="changed_column")
    after = LoadedPackageSnapshot.from_config(config, source_path=str(path))
    ref = resolve_package_reference(path=str(path))
    original = package_tools._comparison_source

    def compare_and_edit(*args, **kwargs):
        _rename_package(path, "edited during impact")
        return original(*args, **kwargs)

    monkeypatch.setattr(package_tools, "_comparison_source", compare_and_edit)
    monkeypatch.setattr(package_tools, "load_package_snapshot", lambda _: before)
    report = package_tools.impact_report(ref, compare_path=str(path), snapshot=after)
    assert report["semantic_fingerprint"] == after.semantic_fingerprint
    change = next(row for row in report["changes"] if row["object_id"] == config.dimensions[0].id)
    assert "column" in change["changed_fields"]
    assert change["behavior_change"] is True
    assert report["impact"]["impacted_metrics"] == sorted(row.id for row in config.metric_recipes)


def test_validation_rejects_source_edits_during_checks(tmp_path, monkeypatch):
    from semantic_rails import config_validation

    path = copy_package_config(tmp_path, "jaffle_shop")
    ref = config_validation.resolve_package_reference(path=str(path))
    validate = config_validation.validate_runtime_package

    def validate_and_edit(path):
        result = validate(path)
        _rename_package(path, "edited during validation")
        return result

    monkeypatch.setattr(config_validation, "validate_runtime_package", validate_and_edit)
    report, snapshot = config_validation.parse_snapshot_report(ref)
    assert report["ok"] is False
    assert snapshot is None
    assert "changed during validation" in report["errors"][0]["message"]


def test_package_artifact_rejects_changed_semantics_before_writing(tmp_path):
    from semantic_rails.config_validation import resolve_package_reference
    from semantic_rails.package_tools import _write_package_artifact, package_manifest

    path = copy_package_config(tmp_path, "jaffle_shop")
    ref = resolve_package_reference(path=str(path))
    snapshot = load_package_snapshot(path)
    manifest = package_manifest(ref, snapshot=snapshot)
    _rename_package(path, "edited after checks")
    target = tmp_path / "artifact.tar.gz"
    with pytest.raises(SemanticLayerError, match="changed after validation"):
        _write_package_artifact(
            ref, output_path=str(target), manifest=manifest, config=snapshot.config
        )
    assert not target.exists()
