"""Canonical authoring rewrites preserve payloads and stop at conflicts."""

import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
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
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = _row(doc, path)
    if path[0] == "segments":
        row.update(entity="order", membership={})
    expected = {"snapshot": value} if old == "snapshot_policy" else value
    row[new] = expected
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    baseline = load_package_snapshot(source)
    del row[new]
    row[old] = value
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    files = PackageFiles(source)
    result = plan(files, RULES, {})
    assert _row(safe_load(result.files[source.name]), path) == {
        new: expected,
        **{key: val for key, val in row.items() if key != old},
    }
    assert not result.pending
    assert not plan(
        PackageFiles(source, contents={**files.contents, **result.files}), RULES, {}
    ).findings
    source.write_bytes(result.files[source.name])
    upgraded = load_package_snapshot(source)
    assert upgraded.semantic_fingerprint == baseline.semantic_fingerprint
    if old == "snapshot_policy":
        assert (
            next(
                m for m in upgraded.config.measures if m.id == "measure.shop.revenue_usd"
            ).default_aggregation
            == "sum"
        )


def _nested(path, row):
    for key in reversed(path):
        row = {key: row}
    return row


@pytest.mark.parametrize(("path", "old", "new", "value"), CASES)
def test_conflicting_alias_stops(tmp_path, path, old, new, value):
    source = tmp_path / "pkg.yml"
    canonical = {"snapshot": "different"} if old == "snapshot_policy" else "different"
    source.write_text(yaml.safe_dump(_nested(path, {old: value, new: canonical})))
    files = PackageFiles(source)
    result = plan(files, RULES, {})
    assert len(result.pending) == 1 and not result.files
    assert old in result.pending[0].message and new in result.pending[0].message
    assert "disagree; rewrite by hand" in result.pending[0].message
    assert source.read_bytes() == files.contents[source.name]


@pytest.mark.parametrize(("path", "old", "new", "value"), CASES)
def test_authoring_alias_upgrade_loads_and_is_idempotent(tmp_path, path, old, new, value):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = _row(doc, path)
    row[old] = value
    if path[0] == "segments":
        row.update(entity="order", membership={})
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    canonical = deepcopy(doc)
    expected_row = _row(canonical, path)
    del expected_row[old]
    if old == "snapshot_policy":
        expected_row[new]["snapshot"] = value
    else:
        expected_row[new] = value
    source.write_text(yaml.safe_dump(canonical))
    baseline = load_package_snapshot(source)
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
    assert load_package_snapshot(source).semantic_fingerprint == baseline.semantic_fingerprint
    assert upgrade_project(source, workspace_root=tmp_path)["status"] == "up_to_date"


def _write_defaults(source, doc, defaults, layout):
    if layout == "single-file":
        doc["defaults"] = defaults
    else:
        doc.pop("defaults", None)
        (source.parent / "defaults.yml").write_text(yaml.safe_dump({"defaults": defaults}))
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    return source if layout == "single-file" else source.parent


def _apply(files, result):
    for name, data in result.files.items():
        (files.root / name).write_bytes(data)


@pytest.mark.parametrize("layout", ["single-file", "directory"])
@pytest.mark.parametrize(
    ("section", "old", "new", "value"),
    [
        ("dimension", "type", "kind", "categorical"),
        ("time", "temporal_class", "class", "as_of_time"),
        ("measure", "snapshot_policy", "accumulation", "end_of_period"),
    ],
)
@pytest.mark.parametrize("mode", ["rename", "equal", "conflict"])
def test_defaults_alias_golden(tmp_path, layout, section, old, new, value, mode):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = {old: value}
    if mode != "rename":
        row[new] = value if mode == "equal" else "different"
    package = _write_defaults(source, doc, {section: row}, layout)
    files = PackageFiles(package)
    result = plan(files, RULES, {})
    if section == "measure" or mode == "conflict":
        assert len(result.pending) == 1 and not result.files
        assert "rewrite by hand" in result.pending[0].message
        assert PackageFiles(package).contents == files.contents
        report = upgrade_project(package, workspace_root=tmp_path)
        assert not report["ok"] and report["status"] == "unverified"
        assert report["rules"][0]["id"] == "authoring-aliases"
        assert len(report["rules"][0]["hits"]) == 1
        return
    # The effective legacy default is shadowed by a member's canonical field;
    # only members without that field inherit it.
    model = doc["models"]["orders"]
    member = (
        model["dimensions"]["channel"] if section == "dimension" else model["times"]["ordered_at"]
    )
    member.pop(new)
    _write_defaults(source, doc, {section: {new: value}}, layout)
    baseline = load_package_snapshot(package)
    _write_defaults(source, doc, {section: row}, layout)
    files = PackageFiles(package)
    result = plan(files, RULES, {})
    assert not result.pending and len(result.findings) == 1
    report = upgrade_project(package, workspace_root=tmp_path)
    assert report["ok"] and len(report["rules"][0]["hits"]) == 1
    _apply(files, result)
    upgraded = load_package_snapshot(package)
    assert upgraded.semantic_fingerprint == baseline.semantic_fingerprint
    assert not plan(PackageFiles(package), RULES, {}).findings


@pytest.mark.parametrize("layout", ["single-file", "directory"])
@pytest.mark.parametrize(
    ("path", "section", "old", "new", "default", "override"),
    [
        (CASES[1][0], "dimension", "type", "kind", "integer", "categorical"),
        (CASES[2][0], "time", "temporal_class", "class", "as_of_time", "event_time"),
    ],
)
def test_member_override_of_alias_default_keeps_effective_semantics(
    tmp_path, layout, path, section, old, new, default, override
):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    member = _row(doc, path)
    member[new] = override
    package = _write_defaults(source, doc, {section: {new: default}}, layout)
    baseline = load_package_snapshot(package)
    del member[new]
    member[old] = override
    _write_defaults(source, doc, {section: {old: default}}, layout)
    files = PackageFiles(package)
    result = plan(files, RULES, {})
    assert not result.pending and len(result.findings) == 2
    _apply(files, result)
    assert load_package_snapshot(package).semantic_fingerprint == baseline.semantic_fingerprint


@pytest.mark.parametrize("layout", ["single-file", "directory"])
@pytest.mark.parametrize(
    ("path", "old", "value", "section", "defaults"),
    [
        (CASES[1][0], "type", "integer", "dimension", {"kind": "categorical"}),
        (CASES[2][0], "temporal_class", "as_of_time", "time", {"class": "event_time"}),
        (CASES[-1][0], "snapshot_policy", "end_of_period", "measure", {"accumulation": "stock"}),
        (
            CASES[-1][0],
            "snapshot_policy",
            "end_of_period",
            "measure",
            {"accumulation": {"kind": "stock"}},
        ),
        (
            CASES[-1][0],
            "snapshot_policy",
            "end_of_period",
            "measure",
            {"snapshot_policy": "start_of_period"},
        ),
    ],
)
def test_member_alias_with_inherited_semantics_stops(
    tmp_path, layout, path, old, value, section, defaults
):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = _row(doc, path)
    row.pop({"dimension": "kind", "time": "class", "measure": "accumulation"}[section])
    row[old] = value
    package = _write_defaults(source, doc, {section: defaults}, layout)
    files = PackageFiles(package)
    result = plan(files, RULES, {})
    assert not result.files
    assert len(result.pending) == (2 if "snapshot_policy" in defaults else 1)
    assert all(
        "rewrite by hand" in f.message and f"defaults.{section}" in f.message
        for f in result.pending
    )
    assert PackageFiles(package).contents == files.contents
    report = upgrade_project(package, workspace_root=tmp_path)
    assert report["status"] == "unverified" and not report["ok"]
    with pytest.raises(SemanticLayerError) as raised:
        upgrade_project(package, workspace_root=tmp_path, dry_run=False)
    assert raised.value.code == "CONFIG_CONFLICT"
    assert PackageFiles(package).contents == files.contents


@pytest.mark.parametrize(
    ("accumulation", "snapshot", "expected"),
    [
        ("stock", "end_of_period", {"kind": "stock", "snapshot": "end_of_period"}),
        ({"kind": "stock"}, "end_of_period", {"kind": "stock", "snapshot": "end_of_period"}),
        ("stock", "start_of_period", {"kind": "stock", "snapshot": "start_of_period"}),
        (
            {"kind": "stock", "snapshot": "end_of_period"},
            " END_OF_PERIOD ",
            {"kind": "stock", "snapshot": "end_of_period"},
        ),
    ],
)
def test_snapshot_golden_keeps_effective_semantics(tmp_path, accumulation, snapshot, expected):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = _row(doc, CASES[-1][0])
    row.pop("default_agg")
    row["accumulation"] = expected
    source.write_text(yaml.safe_dump(doc))
    baseline = load_package_snapshot(source)
    row["accumulation"] = accumulation
    row["snapshot_policy"] = snapshot
    source.write_text(yaml.safe_dump(doc))
    files = PackageFiles(source)
    result = plan(files, RULES, {})
    assert not result.pending
    _apply(files, result)
    upgraded = load_package_snapshot(source)
    assert upgraded.semantic_fingerprint == baseline.semantic_fingerprint
    assert next(
        m for m in upgraded.config.measures if m.id == "measure.shop.revenue_usd"
    ).default_aggregation == ("first_value" if snapshot == "start_of_period" else "last_value")


@pytest.mark.parametrize("inherited", [False, True])
def test_stock_upgrade_matches_weekly_reference_sql(tmp_path, inherited):
    template = Path(__file__).resolve().parents[1] / "integration/correctness/shop"
    package = tmp_path / "shop"
    shutil.copytree(template, package)
    source = package / "models/account_days.yml"
    doc = safe_load(source.read_bytes())
    row = doc["model"]["measures"]["seats"]
    if inherited:
        del row["accumulation"]
        (package / "defaults.yml").write_text("defaults:\n  measure:\n    accumulation: stock\n")
    else:
        row["accumulation"] = "stock"
    row["snapshot_policy"] = "end_of_period"
    source.write_text(yaml.safe_dump(doc))
    files = PackageFiles(package)
    result = plan(files, RULES, {})
    if inherited:
        assert len(result.pending) == 1 and not result.files
        assert PackageFiles(package).contents == files.contents
        return
    assert not result.pending
    _apply(files, result)
    answer = safe_load((package / "tests/answers.yml").read_bytes())["tests"]["shop/stock_by_week"]
    runtime = Runtime.from_path(str(package))
    try:
        rows = runtime.query(deepcopy(answer["query"]))["rows"]
    finally:
        runtime.close()
    with duckdb.connect(str(package / "data/warehouse.duckdb"), read_only=True) as conn:
        reference = conn.execute(answer["reference_sql"]).fetchall()
    actual = sorted(
        (
            datetime.fromisoformat(str(r["temporal_role.shop_account_day_snapshot_day__week"])),
            r["seats"],
        )
        for r in rows
    )
    assert actual == sorted(reference)
    assert actual[0][1] == 1122
