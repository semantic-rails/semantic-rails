"""Golden rows for the expression rule: each retired expression spelling becomes the one that
parses to the same node."""

from __future__ import annotations

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.registry import RULES
from semantic_rails.upgrade.rules_expressions import RULES as EXPRESSION_RULES
from semantic_rails.upgrade.service import upgrade_project
from tests.semantic_rails.conftest import write_single_file_package

EXPRESSION_CASES = {
    "examples/convert.yml": (
        "examples:\n"
        "  convert:\n"
        "    query:\n"
        "      select:\n"
        "        - as: rate\n"
        "          expression:\n"
        "            kind: conversion\n"
        "            matching: first_converted_after_base\n"
        "        - as: both\n"
        "          expression:\n"
        "            kind: conversion\n"
        "            matching_mode: first_converted_after_base\n"
        "            matching: closest_converted_after_base\n",
        "examples:\n"
        "  convert:\n"
        "    query:\n"
        "      select:\n"
        "        - as: rate\n"
        "          expression:\n"
        "            kind: conversion\n"
        "            matching_mode: first_converted_after_base\n"
        "        - as: both\n"
        "          expression:\n"
        "            kind: conversion\n"
        "            matching_mode: first_converted_after_base\n",
    ),
    "pkg.yml": (
        "segments:\n"
        "  web:\n"
        "    membership:\n"
        "      where:\n"
        "        - kind: not_in\n"
        "          left: {kind: column, column: channel}\n"
        "          values: [web]\n"
        "        - {kind: in, expr: {kind: column, column: a}, left: {kind: column, column: b}}\n"
        "metrics:\n"
        "  margin:\n"
        "    kind: derived\n"
        "    expression:\n"
        "      kind: binary # retired\n"
        "      op: subtract\n"
        "      left: {kind: measure_ref, measure: measure.shop.revenue}\n"
        "      right: {kind: literal, value: {kind: binary, left: 1}}\n",
        "segments:\n"
        "  web:\n"
        "    membership:\n"
        "      where:\n"
        "        - kind: not_in\n"
        "          expr: {kind: column, column: channel}\n"
        "          values: [web]\n"
        "        - {kind: in, expr: {kind: column, column: a}}\n"
        "metrics:\n"
        "  margin:\n"
        "    kind: derived\n"
        "    expression:\n"
        "      kind: arithmetic # retired\n"
        "      op: subtract\n"
        "      left: {kind: measure, measure: measure.shop.revenue}\n"
        "      right: {kind: literal, value: {kind: binary, left: 1}}\n",
    ),
    "tests/current.yml": (
        "tests:\n  current: {query: {select: [{expression: {kind: arithmetic, left: {}}}]}}\n",
        None,
    ),
}


def test_retired_expression_spellings_become_current(tmp_path):
    for name, (legacy, _) in EXPRESSION_CASES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(legacy)
    files = PackageFiles(tmp_path / "pkg.yml")

    result = plan(files, EXPRESSION_RULES, {})

    expected = {
        name: current for name, (_, current) in EXPRESSION_CASES.items() if current is not None
    }
    assert {name: data.decode() for name, data in result.files.items() if data} == expected
    assert sorted((finding.file, finding.line) for finding in result.findings) == [
        ("examples/convert.yml", 8),
        ("examples/convert.yml", 13),
        ("pkg.yml", 6),
        ("pkg.yml", 8),
        ("pkg.yml", 13),
        ("pkg.yml", 15),
    ]
    upgraded = PackageFiles(files.source, contents={**files.contents, **result.files})
    assert plan(upgraded, RULES, {}).findings == ()


def test_a_retired_expression_upgrade_loads_with_the_same_semantics(tmp_path):
    source = write_single_file_package(tmp_path / "project")
    baseline = load_package_snapshot(source)
    text = source.read_text()
    current = (
        "      kind: arithmetic\n      op: divide\n"
        "      left:  { kind: metric, metric: revenue_usd }\n"
        "      right: { kind: measure, measure: measure.shop.line_revenue_usd }\n"
    )
    assert text.count(current) == 1
    source.write_text(
        text.replace(
            current,
            current.replace("arithmetic", "binary").replace("kind: measure,", "kind: measure_ref,"),
        )
    )
    with pytest.raises(SemanticLayerError, match="Write kind 'arithmetic'"):
        load_package_config(str(source))

    report = upgrade_project(source, workspace_root=tmp_path, dry_run=False)

    assert report["ok"] and report["status"] == "upgraded", report
    assert {rule["id"]: rule["tier"] for rule in report["rules"]} == {
        "expression-arithmetic": "translated"
    }
    assert source.read_text() == text
    assert load_package_snapshot(source).semantic_fingerprint == baseline.semantic_fingerprint
    assert upgrade_project(source, workspace_root=tmp_path)["status"] == "up_to_date"


BINARY = "{kind: binary, op: subtract, left: {kind: column, column: a}, right: 1}"
ARITHMETIC = "{kind: arithmetic, op: subtract, left: {kind: column, column: a}, right: 1}"
NOT_IN = "{kind: not_in, left: {kind: column, column: channel}, values: [web]}"
CURRENT_NOT_IN = "{kind: not_in, expr: {kind: column, column: channel}, values: [web]}"
# The parser refuses a retired spelling in a measure's `expr:` and in a relation step, so the
# rule reaches both.
PLACES = {
    "measure": (
        "models:\n  orders:\n    measures:\n      margin:\n        expr: <expr>\n",
        ((5, "kind binary becomes arithmetic"),),
    ),
    "relation": (
        "relations:\n"
        "  recent:\n"
        "    steps:\n"
        "      - source: orders\n"
        "      - select:\n"
        "          columns:\n"
        "            margin: <expr>\n"
        "      - where: [<filter>]\n",
        ((7, "kind binary becomes arithmetic"), (8, "left becomes expr")),
    ),
}


@pytest.mark.parametrize(("template", "findings"), PLACES.values(), ids=list(PLACES))
def test_retired_expression_spellings_become_current_in_measures_and_relations(
    tmp_path, template, findings
):
    legacy = template.replace("<expr>", BINARY).replace("<filter>", NOT_IN)
    (tmp_path / "pkg.yml").write_text(legacy)
    files = PackageFiles(tmp_path / "pkg.yml")

    result = plan(files, EXPRESSION_RULES, {})

    current = template.replace("<expr>", ARITHMETIC).replace("<filter>", CURRENT_NOT_IN)
    assert result.files == {"pkg.yml": current.encode()}
    assert tuple((row.line, row.message) for row in result.findings) == findings
    upgraded = PackageFiles(files.source, contents={**files.contents, **result.files})
    assert plan(upgraded, RULES, {}).findings == ()
