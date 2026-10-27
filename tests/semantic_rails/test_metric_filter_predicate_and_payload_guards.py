"""Regression tests for three confirmed runtime/contract bugs:

4. A bare comparison/boolean expression in ``metric_filters[]`` (no
   explicit op/value) used to be compared against the envelope defaults
   (op ``=``, value ``null``) and compiled to ``(expr) IS NULL`` —
   silently dropping every row. It must be treated as the predicate
   itself.
5. Malformed payload shapes (non-dict ``metric_filters[]`` items) raised
   bare AttributeError / TypeError that surfaced as INTERNAL_ERROR instead
   of a structured INVALID_QUERY. ``path_policy`` is no longer a query key,
   so a payload that sends it is refused like any unknown key.
6. The schema advertised relative-window units ``minute`` / ``hour``
   that the runtime rejects; the schema enum for ``time.last.unit`` now
   matches the runtime's supported units exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_rails.ast import normalize_partial_query, normalize_query
from semantic_rails.clock import UNITS
from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "query.v2.json"


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


_REVENUE_OVER_100 = {
    "kind": "comparison",
    "op": ">",
    "left": {"measure": "measure.jaffle.revenue_usd"},
    "right": {"kind": "literal", "value": 100},
}
_REVENUE_UNDER_10000 = {
    **_REVENUE_OVER_100,
    "op": "<",
    "right": {"kind": "literal", "value": 10000},
}


@pytest.mark.parametrize(
    "metric_filter",
    [
        {"expression": _REVENUE_OVER_100},
        {
            "expression": {
                "kind": "boolean",
                "op": "and",
                "args": [_REVENUE_OVER_100, _REVENUE_UNDER_10000],
            }
        },
        # An explicit op/value envelope on a comparison expression keeps the existing
        # compare-the-result behavior (`(expr) = TRUE` etc.).
        {"expression": _REVENUE_OVER_100, "op": "=", "value": True},
    ],
    ids=["bare-comparison", "bare-boolean", "explicit-envelope"],
)
def test_a_predicate_metric_filter_is_the_predicate(package_config_factory, metric_filter):
    config, _ = package_config_factory("jaffle_shop")
    rendered = _metric_filter_sql(config, metric_filter)
    predicate = rendered.rsplit("WHERE", 1)[-1]
    assert "IS NULL" not in predicate, f"predicate filter compared to NULL:\n{rendered}"
    assert "= TRUE" in predicate


# ---------------------------------------------------------------------
# Bug 5 — malformed payload shapes return structured INVALID_QUERY.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("normalize", [normalize_query, normalize_partial_query])
@pytest.mark.parametrize("bad_item", ["oops", 7, ["nested"]])
def test_non_dict_metric_filters_item_is_invalid_query(normalize, bad_item):
    with pytest.raises(SemanticLayerError) as exc:
        normalize({"select": _select_revenue(), "metric_filters": [bad_item]})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "metric_filters[0]"
    assert exc.value.details["received_type"] == type(bad_item).__name__


@pytest.mark.parametrize("normalize", [normalize_query, normalize_partial_query])
@pytest.mark.parametrize(
    "policy", [{"preference": "fewest_hops", "ask_if_ambiguous": True}, "fewest_hops"]
)
def test_path_policy_is_refused_as_an_unknown_key(normalize, policy):
    with pytest.raises(SemanticLayerError) as exc:
        normalize({"version": 1, "select": _select_revenue(), "path_policy": policy})
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["unsupported_keys"] == ["path_policy"]
    assert "path_policy" not in exc.value.details["supported_keys"]
    assert exc.value.details["recovery_hints"][0]["code"] == "REMOVE_UNKNOWN_KEY"


# ---------------------------------------------------------------------
# Bug 6 — relative range units: schema and runtime agree.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["minute", "hour"])
def test_sub_day_relative_units_are_rejected_with_structured_error(unit):
    with pytest.raises(SemanticLayerError) as exc:
        read_call(
            {
                "select": ["measure.jaffle.revenue_usd"],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "last": {"unit": unit, "value": 3},
                },
            }
        )
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["path"] == "time.last.unit"
    (hint,) = exc.value.details["recovery_hints"]
    assert unit not in hint["message"]


def test_schema_relative_unit_enum_matches_runtime_supported_units():
    schema = json.loads(SCHEMA_PATH.read_text())
    enum = schema["properties"]["time"]["properties"]["last"]["properties"]["unit"]["enum"]
    assert set(enum) == set(UNITS), (
        "schemas/query.v2.json time.last.unit enum must match semantic_rails.clock.UNITS"
    )
