"""diff-package / impact-report must see the highest-risk authoring edits.

Redefining a measure's SQL expression and repointing a model at a
different physical relation are exactly the changes a PR gate exists to
catch. Both previously diffed as "0 changes" because the package snapshot
omitted measure expressions and entity tables entirely.
"""

from __future__ import annotations

from pathlib import Path

from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.package_tools import diff_package_report, impact_report
from tests.semantic_rails.conftest import copy_package_config


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"expected {old!r} in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_measure_expr_change_is_a_behavior_change(tmp_path: Path) -> None:
    base_dir = copy_package_config(tmp_path, "jaffle_shop")
    changed_dir = copy_package_config(tmp_path, "jaffle_shop")
    _edit(
        changed_dir / "models" / "core" / "daily_metrics.yml",
        "expr: revenue_ytd_cents / 100.0",
        "expr: revenue_ytd_cents / 1000.0",
    )

    diff = diff_package_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )
    measure_changes = [
        row for row in diff["changes"] if row["object_id"] == "measure.jaffle.revenue_ytd_usd"
    ]
    assert measure_changes, "measure expr edit must appear in diff-package output"
    assert measure_changes[0]["behavior_change"] is True
    assert "expression" in measure_changes[0]["changed_fields"]

    impact = impact_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )
    assert impact["impact"]["risk"] == "high"


def test_entity_relation_change_is_a_behavior_change(tmp_path: Path) -> None:
    base_dir = copy_package_config(tmp_path, "jaffle_shop")
    changed_dir = copy_package_config(tmp_path, "jaffle_shop")
    _edit(
        changed_dir / "models" / "core" / "daily_metrics.yml",
        "relation: jaffle_daily_metric_rollup",
        "relation: jaffle_daily_metric_rollup_v2",
    )

    diff = diff_package_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )
    behavior_changes = [row for row in diff["changes"] if row["behavior_change"]]
    assert behavior_changes, "repointing a model relation must appear as a behavior change"
    changed_fields = {field for row in behavior_changes for field in row["changed_fields"]}
    assert "table" in changed_fields or "source_relation" in changed_fields
