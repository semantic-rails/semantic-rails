"""Tests for the two new compiled-package validators:

- at-most-one default: true time per model
- disallowed-name guard on dimensions and measures
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.config_validation import (
    PackageReference,
    validate_config_report,
)
from tests.semantic_rails.conftest import copy_package_config


def _patch_yaml(path: Path, mutate) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("disallowed_name", "collision"),
    [
        pytest.param("customer_id", True, id="matching-dimension"),
        pytest.param("an_unused_column_name", False, id="no-collision"),
    ],
)
def test_disallowed_names_validation(tmp_path, disallowed_name, collision):
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    graph = pkg / "graph.yml"

    def mutate(raw):
        entities = raw["graph"]["entities"]
        for key, val in entities.items():
            if key == "customer":
                val.setdefault("disallowed_names", []).append(disallowed_name)

    _patch_yaml(graph, mutate)

    ref = PackageReference(source_path=str(pkg))
    report = validate_config_report(ref)
    errors = "\n".join(
        err.get("message", "") if isinstance(err, dict) else str(err)
        for err in (report.get("errors") or [])
    )
    if collision:
        assert "disallowed" in errors.lower()
        assert "customer_id" in errors
    else:
        assert "disallowed" not in errors.lower() or "an_unused_column_name" not in errors


def test_multiple_default_times_rejected(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")

    # Add a second `times:` entry (also marked default) to the orders model.
    # The orders relation already has fulfilled_at as a column, so we can
    # safely register it as a second temporal axis.
    orders = pkg / "models" / "core" / "orders.yml"

    def mutate(raw):
        times = raw["model"]["times"]
        # The first entry (ordered_at) already has default: true.
        # Add fulfilled_at as a second default temporal axis on the same model.
        times["fulfilled_at"] = {
            "as": "temporal_role.jaffle_order_fulfilled_time",
            "name": "jaffle.Order.fulfilled_at",
            "label": "Order fulfilled time",
            "column": "fulfilled_at",
            "kind": "timestamp",
            "class": "event_time",
            "default": True,
        }

    _patch_yaml(orders, mutate)

    ref = PackageReference(source_path=str(pkg))
    report = validate_config_report(ref)
    errors = "\n".join(
        err.get("message", "") if isinstance(err, dict) else str(err)
        for err in (report.get("errors") or [])
    )
    assert "multiple default: true" in errors, f"expected validator to fire; got errors: {errors!r}"
