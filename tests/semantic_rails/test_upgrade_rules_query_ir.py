"""Golden rows for the Query IR rule: version 2 queries become version 1."""

from __future__ import annotations

import pytest

from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.registry import RULES
from semantic_rails.upgrade.rules_query_ir import RULES as QUERY_IR_RULES

CASES = {
    "examples/block.yml": (
        "examples:\n"
        "  revenue:\n"
        "    # Drafted by a planner.\n"
        "    query:\n"
        "      version: 2 # drafts used 2\n"
        "      select: [{expression: {metric: metric.shop.revenue}, as: revenue}]\n",
        "examples:\n"
        "  revenue:\n"
        "    # Drafted by a planner.\n"
        "    query:\n"
        "      version: 1 # drafts used 2\n"
        "      select: [{expression: {metric: metric.shop.revenue}, as: revenue}]\n",
    ),
    "examples/flow.yml": (
        "example: {id: flow, query: {version: 2, select: []}}\n",
        "example: {id: flow, query: {version: 1, select: []}}\n",
    ),
    "tests/single.yml": (
        "test:\n  id: single\n  query:\n    version: 2\n",
        "test:\n  id: single\n  query:\n    version: 1\n",
    ),
    "tests/current.yml": (
        "tests:\n  current: {query: {version: 1}}\n  default: {query: {}}\n",
        None,
    ),
    "pkg.yml": (
        "segments:\n  big:\n    membership:\n      version: 2\n      where: []\n"
        "metrics:\n  revenue: {version: 2}\n",
        "segments:\n  big:\n    membership:\n      version: 1\n      where: []\n"
        "metrics:\n  revenue: {version: 2}\n",
    ),
}


def test_version_two_queries_become_version_one(tmp_path):
    for name, (legacy, _) in CASES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(legacy)
    files = PackageFiles(tmp_path / "pkg.yml")

    result = plan(files, QUERY_IR_RULES, {})

    expected = {name: current for name, (_, current) in CASES.items() if current is not None}
    assert {name: data.decode() for name, data in result.files.items() if data} == expected
    assert {finding.file: finding.line for finding in result.findings} == {
        "examples/block.yml": 5,
        "examples/flow.yml": 1,
        "tests/single.yml": 4,
        "pkg.yml": 4,
    }
    upgraded = PackageFiles(files.source, contents={**files.contents, **result.files})
    assert plan(upgraded, RULES, {}).findings == ()


@pytest.mark.parametrize("version", [1, "2", None])
def test_other_versions_are_left_alone(tmp_path, version):
    query = {} if version is None else {"version": version}
    (tmp_path / "pkg.yml").write_text("package: {id: shop}\n")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples/e.yml").write_text(f"example: {{id: e, query: {query!r}}}\n")
    assert plan(PackageFiles(tmp_path / "pkg.yml"), QUERY_IR_RULES, {}).findings == ()
