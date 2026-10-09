"""Golden rows for the 0.3.2rc3 retirement rules: legacy text becomes current text."""

from __future__ import annotations

from pathlib import Path

import pytest

from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.registry import RULES

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = [
    "configs/semantic_rails/jaffle_shop",
    "configs/semantic_rails/tpch_sf1_showcase",
    "comparisons/semantic_layers/semantic_rails/package",
    "tests/integration/correctness/shop",
    "configs/examples/semantic_rails_package_starter.yml",
]
RULE = {rule.id: rule for rule in RULES}

CASES = {
    "null-behavior": (
        {
            "pkg.yml": """\
metrics:
  # Ratio that carried a null rule.
  ratio:
    kind: ratio
    numerator: a
    denominator: b
    null_behavior: zero # retired
  derived:
    kind: derived
    expression:
      kind: arithmetic
      op: /
      left: {metric: ratio}
      right: {metric: ratio}
      null_behavior: null_if_zero
segments:
  big:
    membership:
      where:
      - expression: {kind: ratio, numerator: a, denominator: b, null_behavior: zero}
""",
            "examples/e.yml": """\
examples:
  share:
    query:
      version: 1
      null_behavior: zero
      select:
      - as: share # inline ratio
        expression: {kind: ratio, numerator: a, denominator: b, null_behavior: zero}
""",
        },
        {
            "pkg.yml": """\
metrics:
  # Ratio that carried a null rule.
  ratio:
    kind: ratio
    numerator: a
    denominator: b
  derived:
    kind: derived
    expression:
      kind: arithmetic
      op: /
      left: {metric: ratio}
      right: {metric: ratio}
segments:
  big:
    membership:
      where:
      - expression: {kind: ratio, numerator: a, denominator: b}
""",
            "examples/e.yml": """\
examples:
  share:
    query:
      version: 1
      select:
      - as: share # inline ratio
        expression: {kind: ratio, numerator: a, denominator: b}
""",
        },
    ),
    "measure-parent-rollup": (
        {
            "pkg.yml": """\
defaults:
  # Parent rollups.
  measure:
    subject_entity: self
    aggregation_entity: self
  time:
    timezone: UTC
models:
  orders:
    measures:
      revenue:
        expr: amount
        subject_entity: customer # retired
        aggregation_entity: self
      count: {kind: count, aggregation_entity: self}
"""
        },
        {
            "pkg.yml": """\
defaults:
  # Parent rollups.
  time:
    timezone: UTC
models:
  orders:
    measures:
      revenue:
        expr: amount
      count: {kind: count}
"""
        },
    ),
    "measure-parent-rollup-kept-defaults": (
        {
            "pkg.yml": "defaults:\n  measure:\n    value_type: number\n    aggregation_entity: self\n"
        },
        {"pkg.yml": "defaults:\n  measure:\n    value_type: number\n"},
    ),
    "forward-rollup-hints": (
        {
            "pkg.yml": """\
defaults:
  relationship:
    traversal: [forward, reverse]
    rollup_safe_aggregations: [sum] # forward hint
graph:
  relationships:
    history:
      entities: [history, customer]
      rollup_safe:
        forward: [sum]
        reverse: [count_distinct]
    orders:
      entities: [order, customer]
      rollup_safe:
        forward: [sum]
    items:
      entities: [item, order]
      rollup_safe: [sum, count]
    stores:
      entities: [store, region]
      rollup_safe: {reverse: [count_distinct]}
models:
  orders:
    joins:
      customer:
        to: customer
        rollup_safe: {forward: [sum]}
        rollup_safe_aggregations: [sum]
"""
        },
        {
            "pkg.yml": """\
defaults:
  relationship:
    traversal: [forward, reverse]
graph:
  relationships:
    history:
      entities: [history, customer]
      rollup_safe:
        reverse: [count_distinct]
    orders:
      entities: [order, customer]
    items:
      entities: [item, order]
    stores:
      entities: [store, region]
      rollup_safe: {reverse: [count_distinct]}
models:
  orders:
    joins:
      customer:
        to: customer
"""
        },
    ),
    "forward-rollup-hints-emptied-defaults": (
        {
            "pkg.yml": """\
defaults:
  # Only a forward hint here.
  relationship:
    rollup_safe: {forward: [sum]}
  dimension:
    groupable: true
"""
        },
        {
            "pkg.yml": """\
defaults:
  # Only a forward hint here.
  dimension:
    groupable: true
"""
        },
    ),
    "relationship-path-preference": (
        {
            "pkg.yml": """\
graph:
  relationships:
    orders:
      entities: [order, customer]
      path_preference: 10 # the cheaper route
      cardinality: many_to_one
models:
  orders:
    joins:
      store: {to: store, path_preference: 5}
"""
        },
        {
            "pkg.yml": """\
graph:
  relationships:
    orders:
      entities: [order, customer]
      cardinality: many_to_one
models:
  orders:
    joins:
      store: {to: store}
"""
        },
    ),
    "query-path-policy": (
        {
            "pkg.yml": """\
graph:
  path_policy:
    max_hops: 3
segments:
  big:
    membership:
      path_policy: {max_hops: 2}
      where: [{dimension: dimension.shop_region, op: '=', value: west}]
""",
            "tests/t.yml": """\
tests:
  by_store:
    kind: query_returns_columns
    query:
      version: 1
      # The route policy an earlier release read.
      path_policy:
        max_hops: 2
      select: [{expression: {metric: metric.shop.revenue}, as: revenue}]
    columns: [revenue]
""",
        },
        {
            "pkg.yml": """\
graph:
  path_policy:
    max_hops: 3
segments:
  big:
    membership:
      where: [{dimension: dimension.shop_region, op: '=', value: west}]
""",
            "tests/t.yml": """\
tests:
  by_store:
    kind: query_returns_columns
    query:
      version: 1
      # The route policy an earlier release read.
      select: [{expression: {metric: metric.shop.revenue}, as: revenue}]
    columns: [revenue]
""",
        },
    ),
}


def _files(root: Path, contents: dict[str, str]) -> PackageFiles:
    for name, text in contents.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return PackageFiles(root / "pkg.yml")


@pytest.mark.parametrize("case", CASES)
def test_legacy_text_becomes_current_text(tmp_path, case):
    legacy, current = CASES[case]
    rule = RULE[next(rule_id for rule_id in RULE if case.startswith(rule_id))]
    files = _files(tmp_path, legacy)

    result = plan(files, (rule,), {})

    assert {name: data.decode() for name, data in result.files.items() if data} == current
    assert all(finding.edits and finding.line > 1 for finding in result.findings)
    upgraded = PackageFiles(files.source, contents={**files.contents, **result.files})
    # These cases keep model joins: blocks, a legacy form the model-joins rule rewrites next.
    assert plan(upgraded, [row for row in RULES if row.id != "model-joins"], {}).findings == ()


@pytest.mark.parametrize("package", PACKAGES)
def test_bundled_packages_hold_no_retired_form(package):
    assert plan(PackageFiles(ROOT / package), RULES, {}).findings == ()
