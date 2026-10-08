"""The default-axis upgrade drops only the axis hint, including inherited defaults."""

from copy import deepcopy

import pytest

from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.rules_time import RULES
from semantic_rails.upgrade.service import upgrade_project
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import write_single_file_package
from tests.semantic_rails.test_upgrade_rules_authoring import _write_defaults


@pytest.mark.parametrize("layout", ["single-file", "directory"])
@pytest.mark.parametrize("axis", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_default_axis_golden_and_effective_semantics(tmp_path, layout, axis, local):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    doc["defaults"] = {"time": {"default_query_axis": axis}}
    time = doc["models"]["orders"]["times"]["ordered_at"]
    if local:
        time["default_query_axis"] = not axis
    canonical = deepcopy(doc)
    del canonical["defaults"]["time"]["default_query_axis"]
    canonical["models"]["orders"]["times"]["ordered_at"].pop("default_query_axis", None)
    package = _write_defaults(source, canonical, canonical["defaults"], layout)
    expected = load_package_snapshot(package)
    _write_defaults(source, doc, doc["defaults"], layout)
    files = PackageFiles(package)
    result = plan(files, RULES, {})
    assert len(result.findings) == 1 + local and not result.pending
    for data in result.files.values():
        assert b"default_query_axis" not in data
    report = upgrade_project(files.source, workspace_root=tmp_path, dry_run=False)
    assert report["ok"] and report["status"] == "upgraded", report
    assert report["proof"]["tier"] == "proven"
    upgraded = load_package_snapshot(files.source)
    assert upgraded.semantic_fingerprint == expected.semantic_fingerprint
    query = {"select": [{"expression": {"metric": "metric.shop.revenue_usd"}, "as": "revenue"}]}
    before, after = Runtime.from_snapshot(expected), Runtime.from_snapshot(upgraded)
    try:
        assert before.compile(query)["rendered_sql"] == after.compile(query)["rendered_sql"]
    finally:
        before.close()
        after.close()
    assert upgrade_project(files.source, workspace_root=tmp_path)["status"] == "up_to_date"


def test_axis_edit_preserves_comments(tmp_path):
    source = tmp_path / "pkg.yml"
    source.write_text(
        "defaults:\n  time:\n    # Keep timezone.\n    timezone: UTC\n"
        "    default_query_axis: false\nmodels:\n  orders:\n    times:\n"
        "      ordered_at:\n        default: true # Keep the clock.\n"
        "        default_query_axis: true\n"
    )
    files = PackageFiles(source)
    result = plan(files, RULES, {})
    assert result.files[source.name].decode() == source.read_text().replace(
        "    default_query_axis: false\n", ""
    ).replace("        default_query_axis: true\n", "")
    assert not plan(
        PackageFiles(source, contents={**files.contents, **result.files}), RULES, {}
    ).findings
