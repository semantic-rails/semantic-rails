"""A call's select shorthands: a dimension in select is lifted and reported; a bare item is
refused with the reference to send."""

from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_partial_query, normalize_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import _grouped_ungrained_time_warning
from semantic_rails.planner import plan_payload

MEASURE = "measure.jaffle.revenue_usd"
METRIC = "metric.sales.aov_usd"
DIM = "dimension.jaffle_store_name"
TIME = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}


def _without_notices(warnings: list) -> list:
    return [w for w in warnings if w["code"] != "QUERY_SHORTHAND_NORMALIZED"]


def _query(select: list, **extra: object) -> dict:
    return {"version": 1, "select": select, "time": TIME, **extra}


# (shorthand select item, canonical select item)
ACCEPTED = [
    pytest.param(
        {"metric": METRIC},
        {"expression": {"kind": "metric", "metric": METRIC}},
        id="bare-metric",
    ),
    pytest.param(
        {"metric": METRIC, "as": "aov"},
        {"expression": {"kind": "metric", "metric": METRIC}, "as": "aov"},
        id="bare-metric-alias",
    ),
    pytest.param(
        {"measure": MEASURE, "aggregation": "sum", "as": "rev"},
        {"expression": {"kind": "measure", "measure": MEASURE, "aggregation": "sum"}, "as": "rev"},
        id="bare-measure-aggregation",
    ),
    pytest.param(
        {"measure": MEASURE, "aggregation": "sum"},
        {"expression": {"kind": "measure", "measure": MEASURE, "aggregation": "sum"}},
        id="bare-measure-no-alias",
    ),
    pytest.param(
        {"measure": MEASURE},
        {"expression": {"kind": "measure", "measure": MEASURE}},
        id="bare-measure-no-aggregation",
    ),
]


def test_kindless_expression_error_shows_canonical_form():
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(_query([{"expression": {"aggregation": "sum"}}]))
    assert '{"kind": "measure", "measure": "<measure id>"' in str(excinfo.value)


def _call(select: list, **extra: object) -> dict:
    return {"select": select, "time": TIME, **extra}


def test_runtime_lifts_a_dimension_in_select_into_group_by_and_reports_it(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    select = [
        {"expression": {"kind": "measure", "measure": MEASURE, "aggregation": "sum"}, "as": "rev"},
        {"expression": METRIC, "as": "aov"},
    ]
    try:
        canonical = runtime.compile(_call(select, group_by=[DIM]), sql_profile="audit")
        shorthand = runtime.compile(_call([*select, DIM]), sql_profile="audit")
        assert shorthand["rendered_sql"] == canonical["rendered_sql"]
        notes = [w for w in shorthand["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED"]
        assert [w["path"] for w in notes] == ["select[2]"]
        assert notes[0]["details"]["canonical"] == {"group_by": [DIM]}
        assert not [w for w in canonical["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED"]
        assert _without_notices(shorthand["warnings"]) == canonical["warnings"]
    finally:
        runtime.close()


@pytest.mark.parametrize(("shorthand", "canonical"), ACCEPTED)
def test_runtime_refuses_a_bare_select_item_with_the_reference_to_send(
    runtime_factory, shorthand, canonical
):
    """A bare ``{metric}`` / ``{measure}`` item is not a call's spelling: the refusal names
    the item and hands back the id to send instead."""
    runtime = runtime_factory("jaffle_shop")
    try:
        (error,) = runtime.validate(_call([shorthand]))["errors"]
    finally:
        runtime.close()
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"].startswith("select[0]")
    (hint,) = error["recovery_hints"]
    assert canonical["expression"].get("metric", MEASURE) in hint["message"]


@pytest.mark.parametrize("kind", [None, "dimension", "group", "ref"])
def test_runtime_refuses_a_dimension_wrapped_in_an_expression(runtime_factory, kind):
    runtime = runtime_factory("jaffle_shop")
    try:
        item = {"expression": {**({"kind": kind} if kind else {}), "dimension": DIM}}
        (error,) = runtime.validate(_call([item, {"expression": METRIC, "as": "aov"}]))["errors"]
    finally:
        runtime.close()
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"].startswith("select[0].expression")


def test_time_grain_warning_reads_the_group_by():
    """A grouped engine query whose clock has no grain gets the grouped warning."""
    canonical = {
        "select": [{"expression": {"kind": "metric", "metric": METRIC}}],
        "group_by": [DIM],
        "time": {"temporal_role": "temporal_role.jaffle_order_time"},
    }
    assert (
        _grouped_ungrained_time_warning(canonical, {})["code"]
        == "UNGRAINED_GROUPED_TIME_PROJECTION"
    )


def test_plan_accepts_the_shorthand_execute_accepts(runtime_factory):
    shorthand = {"select": [METRIC, DIM]}
    canonical = {"select": [METRIC], "group_by": [DIM]}
    runtime = runtime_factory("jaffle_shop")
    try:
        planned = plan_payload(runtime, intent="revenue by store", partial_query=shorthand)
        assert planned == plan_payload(runtime, intent="revenue by store", partial_query=canonical)
        with pytest.raises(SemanticLayerError) as excinfo:
            plan_payload(
                runtime,
                intent="revenue by store",
                partial_query={"select": [{"dimension": DIM, "metric": METRIC}]},
            )
        assert excinfo.value.code == "INVALID_QUERY"
    finally:
        runtime.close()


def test_partial_query_still_accepts_an_empty_select_item():
    state = normalize_partial_query(
        {"select": [{"as": "x"}, {"expression": {"kind": "metric", "metric": METRIC}}]}
    )
    assert [item.as_ for item in state.select] == ["x", "expr_2"]
    assert state.select[0].expression is None
