from __future__ import annotations

import importlib.util
import json
import shutil
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from mf2sr.translate import translate
from semantic_rails.config import LoadedPackageSnapshot, load_package_snapshot
from semantic_rails.contracts import (
    compare_metric_portability,
    export_metric_portability,
    export_semantic_contract,
    load_contract,
    load_contract_fixture,
)
from semantic_rails.runtime import Runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def portable(tmp_path):
    corpus = load_contract_fixture("metric_portability.v1.json")
    source = tmp_path / "semantic_manifest.json"
    source.write_text(json.dumps(corpus["framework_input"]))
    report = translate(
        source,
        tmp_path,
        package_id=corpus["package_id"],
        namespace=corpus["namespace"],
        default_db="data.duckdb",
    )
    return corpus, report


def _validate(payload):
    query = load_contract("query_ir.v1.json")
    registry = Registry().with_resource(query["$id"], Resource.from_contents(query))
    Draft202012Validator(load_contract("metric_portability.v1.json"), registry=registry).validate(
        payload
    )


def test_framework_import_to_governed_bi_card(portable, tmp_path):
    corpus, report = portable
    snapshot = load_package_snapshot(report.package_dir)
    artifact = export_metric_portability(snapshot, import_provenance=report.provenance)
    _validate(artifact)
    assert [row["id"] for row in artifact["metrics"]] == corpus["expected_metric_ids"]
    assert artifact["provenance"]["import"]["framework"] == "metricflow"
    assert artifact["provenance"]["import"]["warnings"] == report.warnings
    assert (
        artifact["package"]["semantic_hash"]
        == export_semantic_contract(snapshot)["semantic"]["packages"][0]["semantic_hash"]
    )
    assert compare_metric_portability(artifact, artifact)["classification"] == "none"
    with duckdb.connect(str(report.package_dir / "data.duckdb")) as connection:
        connection.execute(corpus["seed_sql"])
    spec = importlib.util.spec_from_file_location(
        "metric_card", ROOT / "examples/bi_consumer/metric_card.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A catalog cannot retarget a saved BI binding through a forged template.
    forged = deepcopy(artifact)
    forged["metrics"][0]["query_template"]["select"][0]["expression"]["metric"] = "metric.foreign"
    calls = []
    module.render_metric_card(
        forged,
        (corpus["namespace"], corpus["expected_metric_ids"][0]),
        lambda query: calls.append(query) or {},
    )
    assert calls[0]["select"][0]["expression"]["metric"] == corpus["expected_metric_ids"][0]
    runtime = Runtime.from_path(str(report.package_dir))
    try:
        card = module.render_metric_card(
            artifact, (corpus["namespace"], corpus["expected_metric_ids"][0]), runtime.query
        )
        assert card["result"]["rows"] == [{"value": corpus["expected_value"]}]
        with pytest.raises(ValueError, match="namespace"):
            module.render_metric_card(
                artifact, ("foreign", corpus["expected_metric_ids"][0]), runtime.query
            )
    finally:
        runtime.close()


def test_complex_builtin_definitions_validate():
    _validate(export_metric_portability(ROOT / "configs/semantic_rails/jaffle_shop"))


@pytest.mark.parametrize(
    "case", load_contract_fixture("metric_portability.v1.json")["compatibility_cases"]
)
def test_shared_compatibility_corpus(portable, case):
    _, report = portable
    before = export_metric_portability(report.package_dir)
    path = report.package_dir / "metrics/customers.yml"
    content = yaml.safe_load(path.read_text())
    if case["change"] == "source_comment":
        path.write_text(path.read_text() + "\n# formatting-only edit\n")
    elif case["change"] == "metric_label":
        content["metrics"]["customer_count"]["label"] = "Customer card"
        path.write_text(yaml.safe_dump(content))
    elif case["change"] == "add_metric":
        content["metrics"]["customer_card"] = deepcopy(content["metrics"]["customer_count"])
        path.write_text(yaml.safe_dump(content))
    elif case["change"] == "rename_metric":
        content["metrics"]["renamed_count"] = content["metrics"].pop("customer_count")
        path.write_text(yaml.safe_dump(content))
    else:
        path = report.package_dir / "models/customers.yml"
        content = yaml.safe_load(path.read_text())
        content["model"]["measures"]["customer_count"]["expr"] = "customer_id + 1"
        path.write_text(yaml.safe_dump(content))
    after = export_metric_portability(report.package_dir)
    result = compare_metric_portability(before, after)
    assert result["classification"] == case["classification"]
    assert case["code"] in [row["code"] for row in result["changes"]]


def test_snapshot_export_survives_disk_mutation_and_relocation(portable, tmp_path):
    _, report = portable
    snapshot = load_package_snapshot(report.package_dir)
    before = export_metric_portability(snapshot)
    validation = export_semantic_contract(snapshot)
    relocated = tmp_path / "moved"
    shutil.copytree(report.package_dir, relocated)
    assert export_metric_portability(relocated) == before
    (report.package_dir / "metrics/customers.yml").write_text("malformed: [")
    assert export_metric_portability(snapshot) == before
    assert export_semantic_contract(snapshot) == validation


@pytest.mark.parametrize("change", ["major", "duplicate", "hash", "definition", "template"])
def test_invalid_exports_fail_closed(portable, change):
    _, report = portable
    before = export_metric_portability(report.package_dir)
    after = deepcopy(before)
    if change == "major":
        after["contract_format_version"] = 2
    elif change == "duplicate":
        after["metrics"].append(deepcopy(after["metrics"][0]))
    elif change == "hash":
        after["metrics"][0]["definition_hash"] = "sha256:" + "0" * 64
    elif change == "definition":
        after["metrics"][0]["definition"]["value_type"] = "currency"
    else:
        after["metrics"][0]["query_template"]["select"][0]["expression"]["metric"] = "other"
    with pytest.raises(ValueError):
        compare_metric_portability(before, after)


def test_optional_fields_are_ignored_but_engine_changes_requalify(portable):
    _, report = portable
    before = export_metric_portability(report.package_dir)
    after = deepcopy(before)
    after["future_optional_field"] = "new"
    assert compare_metric_portability(before, after)["compatible"]
    after["producer"]["version"] = "999.0.0"
    assert not compare_metric_portability(before, after)["compatible"]


def test_namespace_rename_and_deployment_locator_rules(portable):
    _, report = portable
    before = export_metric_portability(report.package_dir)
    path = report.package_dir / "package.yml"
    content = yaml.safe_load(path.read_text())
    content["package"]["default_db"] = "new-data.duckdb"
    path.write_text(yaml.safe_dump(content))
    moved = export_metric_portability(report.package_dir)
    assert moved["metrics"] == before["metrics"]
    assert compare_metric_portability(before, moved)["classification"] == "metadata"
    content["package"]["namespace"] = "renamed"
    path.write_text(yaml.safe_dump(content))
    assert not compare_metric_portability(before, export_metric_portability(report.package_dir))[
        "compatible"
    ]


def test_cli_selects_portability_without_changing_default(portable, tmp_path):
    import subprocess
    import sys

    _, report = portable
    output = tmp_path / "metrics.json"
    base = [
        sys.executable,
        "-m",
        "semantic_rails",
        "export-contract",
        "--path",
        str(report.package_dir),
        "--output",
        str(output),
    ]
    subprocess.run([*base, "--format", "metrics"], check=True, capture_output=True, text=True)
    assert json.loads(output.read_text()) == export_metric_portability(report.package_dir)
    subprocess.run(base, check=True, capture_output=True, text=True)
    assert json.loads(output.read_text()) == export_semantic_contract(report.package_dir)


def test_in_memory_snapshot_has_explicit_identity_and_valid_provenance(portable):
    corpus, report = portable
    captured = load_package_snapshot(report.package_dir)
    snapshot = LoadedPackageSnapshot.from_config(captured.config)
    with pytest.raises(ValueError, match="explicit namespace"):
        export_metric_portability(snapshot)
    exported = export_metric_portability(snapshot, namespace=corpus["namespace"])
    _validate(exported)
    assert exported["metrics"] == export_metric_portability(captured)["metrics"]
    with pytest.raises(ValueError, match="authored package"):
        export_metric_portability(captured, namespace="foreign")


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_import_provenance_version_is_strict(portable, version):
    from semantic_rails.errors import SemanticLayerError

    _, report = portable
    provenance = {**report.provenance, "format_version": version}
    with pytest.raises(SemanticLayerError, match="provenance"):
        export_metric_portability(report.package_dir, import_provenance=provenance)


def test_import_loss_warning_survives_export(tmp_path):
    corpus = load_contract_fixture("metric_portability.v1.json")
    corpus["framework_input"]["metrics"].append({"name": "unrecognized", "type": "future_type"})
    source = tmp_path / "semantic_manifest.json"
    source.write_text(json.dumps(corpus["framework_input"]))
    report = translate(
        source, tmp_path, package_id=corpus["package_id"], namespace=corpus["namespace"]
    )
    artifact = export_metric_portability(report.package_dir, import_provenance=report.provenance)
    assert artifact["provenance"]["import"]["warnings"] == report.warnings
    assert any("unrecognized" in warning and "skipped" in warning for warning in report.warnings)
    assert [row["id"] for row in artifact["metrics"]] == corpus["expected_metric_ids"]


def test_distribution_sidecar_does_not_become_semantic_authority(portable):
    _, report = portable
    before = export_metric_portability(report.package_dir)
    sidecar = report.package_dir / "artifacts/metric_portability.v1.json"
    sidecar.parent.mkdir()
    sidecar.write_text(json.dumps(before))
    after = export_metric_portability(report.package_dir)
    assert after["metrics"] == before["metrics"]
    assert after["package"]["semantic_hash"] == before["package"]["semantic_hash"]
    # Source identity covers the newly added file; the artifact attests the
    # captured generation before publication, not a self-referential digest.
    assert after["provenance"]["source_fingerprint"] != before["provenance"]["source_fingerprint"]


def test_definitions_share_snapshot_canonicalization_for_metadata(portable):
    from datetime import date

    corpus, report = portable
    config = load_package_snapshot(report.package_dir).config
    config.metric_recipes[0].meta["reviewed_at"] = date(2026, 1, 1)
    snapshot = LoadedPackageSnapshot.from_config(config)
    artifact = export_metric_portability(snapshot, namespace=corpus["namespace"])
    _validate(artifact)
    assert artifact["metrics"][0]["definition"]["meta"]["reviewed_at"] == "2026-01-01"
