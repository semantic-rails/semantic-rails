"""Tests for the two new compiled-package validators:

- at-most-one default_query_axis per entity
- disallowed-name guard on dimensions and measures
"""

from __future__ import annotations

from pathlib import Path

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


def test_disallowed_names_rejects_matching_dimension(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    graph = pkg / "graph.yml"

    def mutate(raw):
        # disallow `customer_id` (the canonical key column) so authoring a
        # near-duplicate dimension would fail. Jaffle already authors a
        # dimension with column=customer_id; this test confirms the validator
        # catches it.
        entities = raw["graph"]["entities"]
        # Find the customer entity
        for key, val in entities.items():
            if key == "customer":
                val.setdefault("disallowed_names", []).append("customer_id")

    _patch_yaml(graph, mutate)

    ref = PackageReference(source_path=str(pkg))
    report = validate_config_report(ref)
    errors = "\n".join(
        err.get("message", "") if isinstance(err, dict) else str(err)
        for err in (report.get("errors") or [])
    )
    # The customer model authors a dimension with column=customer_id; the
    # validator should fire.
    assert "disallowed" in errors.lower()
    assert "customer_id" in errors


def test_disallowed_names_passes_when_no_collision(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    graph = pkg / "graph.yml"

    def mutate(raw):
        entities = raw["graph"]["entities"]
        for key, val in entities.items():
            if key == "customer":
                val.setdefault("disallowed_names", []).append("an_unused_column_name")

    _patch_yaml(graph, mutate)

    ref = PackageReference(source_path=str(pkg))
    report = validate_config_report(ref)
    errors = "\n".join(
        err.get("message", "") if isinstance(err, dict) else str(err)
        for err in (report.get("errors") or [])
    )
    assert "disallowed" not in errors.lower() or "an_unused_column_name" not in errors


def test_multiple_default_query_axis_rejected(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")

    # Add a second `times:` entry (also marked default) to the orders model.
    # The orders relation already has fulfilled_at as a column, so we can
    # safely register it as a second temporal axis.
    orders = pkg / "models" / "core" / "orders.yml"

    def mutate(raw):
        times = raw["model"]["times"]
        # The first entry (ordered_at) already has default_query_axis: true.
        # Add fulfilled_at as a second default temporal axis on the same model.
        times["fulfilled_at"] = {
            "id": "temporal_role.jaffle_order_fulfilled_time",
            "name": "jaffle.Order.fulfilled_at",
            "label": "Order fulfilled time",
            "column": "fulfilled_at",
            "kind": "timestamp",
            "class": "event_time",
            "default_query_axis": True,
        }

    _patch_yaml(orders, mutate)

    ref = PackageReference(source_path=str(pkg))
    report = validate_config_report(ref)
    errors = "\n".join(
        err.get("message", "") if isinstance(err, dict) else str(err)
        for err in (report.get("errors") or [])
    )
    assert "default_query_axis" in errors or "default time axis" in errors.lower(), (
        f"expected validator to fire; got errors: {errors!r}"
    )
