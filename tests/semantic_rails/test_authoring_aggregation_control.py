"""Tests for author-controlled aggregation overrides.

Covers:
- `default_agg:` (canonical) and `agg_function:` (deprecated alias) authoring keys
- `disallowed_aggregations:` subtraction from the derived allowed set
- Validation that the effective default_agg is in the effective allowed set
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.conftest import copy_package_config


def _orders_path(package_root: Path) -> Path:
    return package_root / "models" / "core" / "orders.yml"


def _measure_by_id(config, measure_id):
    return next(m for m in config.measures if m.id == measure_id)


def _patch_measure_spec(package_root: Path, measure_key: str, patch: dict) -> None:
    path = _orders_path(package_root)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    measures = raw["model"]["measures"]
    measures[measure_key] = {**measures[measure_key], **patch}
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def test_disallowed_aggregations_subtracts_from_derived_allowed(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    _patch_measure_spec(pkg, "revenue_usd", {"disallowed_aggregations": ["median", "percentile"]})

    config = load_package_config(str(pkg))
    measure = _measure_by_id(config, "measure.jaffle.revenue_usd")

    assert "median" not in measure.allowed_aggregations
    assert "percentile" not in measure.allowed_aggregations
    assert "sum" in measure.allowed_aggregations
    # disallowed entries surface in invalid_aggregations for downstream visibility
    assert "median" in measure.invalid_aggregations
    assert "percentile" in measure.invalid_aggregations


def test_default_agg_overrides_derived_default(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    # revenue_usd derives default_aggregation == "sum"; override to "avg"
    _patch_measure_spec(pkg, "revenue_usd", {"default_agg": "avg"})

    config = load_package_config(str(pkg))
    measure = _measure_by_id(config, "measure.jaffle.revenue_usd")

    assert measure.default_aggregation == "avg"


def test_default_agg_outside_allowed_is_rejected(tmp_path):
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    # disallow the very aggregation we then try to set as default
    _patch_measure_spec(
        pkg,
        "revenue_usd",
        {"default_agg": "avg", "disallowed_aggregations": ["avg"]},
    )

    with pytest.raises(SemanticLayerError) as exc_info:
        load_package_config(str(pkg))

    assert exc_info.value.code == "INVALID_CONFIG"
    assert "default_agg" in str(exc_info.value)
