"""Canonical authoring rewrites preserve payloads and stop at conflicts."""

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.rules_authoring import RULES
from semantic_rails.upgrade.service import upgrade_project
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import write_single_file_package

CASES = [
    (("models", "orders"), "relation_ref", "relation", "shop_order"),
    (("models", "orders", "dimensions", "channel"), "type", "kind", "categorical"),
    (("models", "orders", "times", "ordered_at"), "temporal_class", "class", "event_time"),
    (("metrics", "revenue_usd"), "time", "temporal_role", "temporal_role.shop_order_ordered_at"),
    (("segments", "orders"), "metric", "basis_metric", "metric.shop.revenue_usd"),
    (
        ("models", "orders", "measures", "revenue_usd"),
        "snapshot_policy",
        "accumulation",
        "end_of_period",
    ),
]


def _row(doc, path):
    for key in path:
        doc = doc.setdefault(key, {})
    return doc


@pytest.mark.parametrize(("path", "old", "new", "value"), CASES)
def test_authoring_alias_golden_rewrite(tmp_path, path, old, new, value):
    source = tmp_path / "pkg.yml"
    doc = {}
    _row(doc, path)[old] = value
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    files = PackageFiles(source)
    result = plan(files, RULES, {})
    expected = {"kind": "", "snapshot": value} if old == "snapshot_policy" else value
    assert safe_load(result.files["pkg.yml"]) == _nested(path, {new: expected})
    assert not result.pending
    assert not plan(
        PackageFiles(source, contents={**files.contents, **result.files}), RULES, {}
    ).findings


def _nested(path, row):
    for key in reversed(path):
        row = {key: row}
    return row


@pytest.mark.parametrize(("path", "old", "new", "value"), CASES[:-1])
def test_conflicting_alias_stops(tmp_path, path, old, new, value):
    source = tmp_path / "pkg.yml"
    source.write_text(yaml.safe_dump(_nested(path, {old: value, new: "different"})))
    result = plan(PackageFiles(source), RULES, {})
    assert len(result.pending) == 1 and not result.files
    assert old in result.pending[0].message and new in result.pending[0].message


@pytest.mark.parametrize(("path", "old", "new", "value"), CASES)
def test_authoring_alias_upgrade_loads_and_is_idempotent(tmp_path, path, old, new, value):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = _row(doc, path)
    row[old] = value
    if path[0] == "segments":
        row.update(entity="order", membership={})
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(SemanticLayerError, match=old) as exc:
        load_package_config(str(source))
    assert exc.value.code == "INVALID_CONFIG"
    report = upgrade_project(source, workspace_root=tmp_path, dry_run=False)
    assert report["ok"] and report["status"] == "upgraded", report
    assert report["proof"]["tier"] == "certified"
    assert report["proof"]["baseline"] == "after_certified_rules"
    assert {rule["id"]: rule["tier"] for rule in report["rules"]} == {
        "authoring-aliases": "certified"
    }
    load_package_config(str(source))
    assert upgrade_project(source, workspace_root=tmp_path)["status"] == "up_to_date"
