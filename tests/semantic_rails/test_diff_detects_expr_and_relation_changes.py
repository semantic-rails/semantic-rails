"""diff-package / impact-report must see the highest-risk authoring edits.

Redefining a measure's SQL expression and repointing a model at a
different physical relation are exactly the changes a PR gate exists to
catch. Both previously diffed as "0 changes" because the package snapshot
omitted measure expressions and entity tables entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.package_tools import diff_package_report, impact_report
from tests.semantic_rails.conftest import copy_package_config


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("expr: revenue_ytd_cents / 100.0", "expr: revenue_ytd_cents / 1000.0", "expression"),
        (
            "relation: jaffle_daily_metric_rollup",
            "relation: jaffle_daily_metric_rollup_v2",
            "source_relation",
        ),
    ],
    ids=["measure-expression", "model-relation"],
)
def test_expression_and_relation_edits_are_high_risk_behavior_changes(
    tmp_path: Path, old: str, new: str, field: str
) -> None:
    base_dir = copy_package_config(tmp_path, "jaffle_shop")
    changed_dir = copy_package_config(tmp_path, "jaffle_shop")
    model = changed_dir / "models" / "core" / "daily_metrics.yml"
    text = model.read_text(encoding="utf-8")
    assert old in text
    model.write_text(text.replace(old, new, 1), encoding="utf-8")
    changed = resolve_package_reference(path=str(changed_dir))

    diff = diff_package_report(changed, compare_path=str(base_dir))
    (row,) = [
        row for row in diff["changes"] if row["object_id"] == "measure.jaffle.revenue_ytd_usd"
    ]
    assert row["behavior_change"] is True
    assert field in row["changed_fields"]
    assert impact_report(changed, compare_path=str(base_dir))["impact"]["risk"] == "high"
