"""Regression tests for three confirmed runtime/contract bugs:

4. A bare comparison/boolean expression in ``metric_filters[]`` (no
   explicit op/value) used to be compared against the envelope defaults
   (op ``=``, value ``null``) and compiled to ``(expr) IS NULL`` —
   silently dropping every row. It must be treated as the predicate
   itself.
5. Malformed payload shapes (non-dict ``metric_filters[]`` items,
   non-dict / unknown-key ``path_policy``) raised bare AttributeError /
   TypeError that surfaced as INTERNAL_ERROR instead of a structured
   INVALID_QUERY.
6. The schema advertised ``time.range.last.unit`` values ``minute`` /
   ``hour`` that the runtime rejects; the schema enum now matches the
   runtime's supported units exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_rails.ast import _SUPPORTED_RELATIVE_UNITS, normalize_partial_query, normalize_query
from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "query_ir.v1.json"


def _select_revenue() -> list[dict]:
    return [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}]


# ---------------------------------------------------------------------
# Bug 4 — bare comparison/boolean metric_filters expressions are
# predicates, not values to compare against NULL.
# ---------------------------------------------------------------------


def _metric_filter_sql(config, metric_filter: dict) -> str:
    query = {
        "select": _select_revenue(),
        "group_by": ["dimension.jaffle_store_name"],
        "metric_filters": [metric_filter],
    }
    return compile_query(config, Registry(config), query)["sql"]


def test_bare_comparison_metric_filter_is_the_predicate(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _metric_filter_sql(
        config,
        {
            "expression": {
                "kind": "comparison",
                "op": ">",
                "left": {"measure": "measure.jaffle.revenue_usd"},
                "right": {"kind": "literal", "value": 100},
            }
        },
    )
    assert "IS NULL" not in rendered, f"comparison filter compared to NULL:\n{rendered}"
    assert "= TRUE" in rendered


def test_bare_boolean_metric_filter_is_the_predicate(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _metric_filter_sql(
        config,
        {
            "expression": {
                "kind": "boolean",
                "op": "and",
                "args": [
                    {
                        "kind": "comparison",
                        "op": ">",
                        "left": {"measure": "measure.jaffle.revenue_usd"},
                        "right": {"kind": "literal", "value": 100},
                    },
                    {
                        "kind": "comparison",
                        "op": "<",
                        "left": {"measure": "measure.jaffle.revenue_usd"},
                        "right": {"kind": "literal", "value": 10000},
                    },
                ],
            }
        },
    )
    assert "IS NULL" not in rendered
    assert "= TRUE" in rendered


def test_comparison_metric_filter_with_explicit_envelope_still_compares(package_config_factory):
    """An explicit op/value envelope on a comparison expression keeps the
    existing compare-the-result behavior (`(expr) = TRUE` etc.)."""
    config, _ = package_config_factory("jaffle_shop")
    rendered = _metric_filter_sql(
        config,
        {
            "expression": {
                "kind": "comparison",
                "op": ">",
                "left": {"measure": "measure.jaffle.revenue_usd"},
                "right": {"kind": "literal", "value": 100},
            },
            "op": "=",
            "value": True,
        },
    )
    assert "IS NULL" not in rendered
    assert "= TRUE" in rendered


def test_measure_metric_filter_with_value_keeps_threshold_semantics(package_config_factory):
    """Non-predicate expressions (a measure ref with an explicit value)
    are unaffected — the envelope still supplies the comparison."""
    config, _ = package_config_factory("jaffle_shop")
    rendered = _metric_filter_sql(
        config,
        {
            "expression": {"measure": "measure.jaffle.revenue_usd"},
            "op": ">",
            "value": 100,
        },
    )
    assert "> 100" in rendered


# ---------------------------------------------------------------------
# Bug 5 — malformed payload shapes return structured INVALID_QUERY.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_item", ["oops", 7, ["nested"]])
def test_non_dict_metric_filters_item_is_invalid_query(bad_item):
    with pytest.raises(SemanticLayerError) as exc:
        normalize_query({"version": 1, "select": _select_revenue(), "metric_filters": [bad_item]})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "metric_filters[0]"
    assert exc.value.details["received_type"] == type(bad_item).__name__


def test_non_dict_metric_filters_item_is_invalid_query_in_partial_normalize():
    with pytest.raises(SemanticLayerError) as exc:
        normalize_partial_query(
            {"version": 1, "select": _select_revenue(), "metric_filters": ["oops"]}
        )
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "metric_filters[0]"


@pytest.mark.parametrize("bad_policy", ["fewest_hops", 7, ["fewest_hops"]])
def test_non_dict_path_policy_is_invalid_query(bad_policy):
    with pytest.raises(SemanticLayerError) as exc:
        normalize_query({"version": 1, "select": _select_revenue(), "path_policy": bad_policy})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "path_policy"
    assert exc.value.details["supported_keys"] == ["preference", "ask_if_ambiguous"]


def test_path_policy_with_unknown_keys_is_invalid_query():
    with pytest.raises(SemanticLayerError) as exc:
        normalize_query(
            {
                "version": 1,
                "select": _select_revenue(),
                "path_policy": {"prefer": "fewest_hops"},
            }
        )
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["unsupported_keys"] == ["prefer"]
    hints = exc.value.details.get("recovery_hints", [])
    assert hints and hints[0]["code"] == "REMOVE_UNKNOWN_KEY"


def test_valid_path_policy_still_normalizes():
    normalized = normalize_query(
        {
            "version": 1,
            "select": _select_revenue(),
            "path_policy": {"preference": "newest_first", "ask_if_ambiguous": False},
        }
    )
    assert normalized.path_policy.preference == "newest_first"
    assert normalized.path_policy.ask_if_ambiguous is False


# ---------------------------------------------------------------------
# Bug 6 — relative range units: schema and runtime agree.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["minute", "hour"])
def test_sub_day_relative_units_are_rejected_with_structured_error(unit):
    with pytest.raises(SemanticLayerError) as exc:
        normalize_query(
            {
                "version": 1,
                "select": _select_revenue(),
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "range": {"last": {"unit": unit, "value": 3}},
                },
            }
        )
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "time.range.last.unit"
    assert unit not in exc.value.details["supported_units"]


def test_schema_relative_unit_enum_matches_runtime_supported_units():
    schema = json.loads(SCHEMA_PATH.read_text())
    enum = schema["$defs"]["TimeBlock"]["properties"]["range"]["properties"]["last"]["properties"][
        "unit"
    ]["enum"]
    assert set(enum) == set(_SUPPORTED_RELATIVE_UNITS), (
        "schemas/query_ir.v1.json time.range.last.unit enum must match "
        "semantic_rails.ast._SUPPORTED_RELATIVE_UNITS"
    )
