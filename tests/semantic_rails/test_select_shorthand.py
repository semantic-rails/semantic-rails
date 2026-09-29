"""Unambiguous select shorthands are accepted and reported; ambiguous ones keep the error."""

from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_partial_query, normalize_query, rewrite_select_shorthand
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


@pytest.mark.parametrize(("shorthand", "canonical"), ACCEPTED)
def test_bare_select_item_normalizes_like_canonical(shorthand, canonical):
    assert (
        normalize_query(_query([shorthand])).to_dict()
        == normalize_query(_query([canonical])).to_dict()
    )


def test_expression_dimension_in_select_moves_to_group_by():
    shorthand = _query([{"expression": {"dimension": DIM}}, {"metric": METRIC}])
    canonical = _query([{"expression": {"kind": "metric", "metric": METRIC}}], group_by=[DIM])
    assert normalize_query(shorthand).to_dict() == normalize_query(canonical).to_dict()


def test_expression_dimension_already_in_group_by_is_not_duplicated():
    shorthand = _query([{"expression": {"dimension": DIM}}, {"metric": METRIC}], group_by=[DIM])
    assert normalize_query(shorthand).group_by == [DIM]


@pytest.mark.parametrize(
    "select",
    [
        pytest.param([{"metric": METRIC, "measure": MEASURE}], id="metric-and-measure"),
        pytest.param([{"metric": METRIC, "aggregation": "sum"}], id="metric-with-aggregation"),
        pytest.param([{"measure": MEASURE, "agregation": "sum"}], id="unknown-key"),
        pytest.param(
            [{"measure": MEASURE, "temporal_role": "temporal_role.jaffle_order_time"}],
            id="undocumented-measure-key",
        ),
        pytest.param([{"measure": MEASURE, "parameters": {}}], id="undocumented-parameters"),
        pytest.param([{"aggregation": "sum"}], id="aggregation-only"),
        pytest.param([{"as": "x"}], id="alias-only"),
        pytest.param([{}], id="empty-item"),
    ],
)
def test_ambiguous_bare_select_item_is_refused_with_canonical_form(select):
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(_query(select))
    err = excinfo.value
    assert err.code == "INVALID_EXPRESSION_AST"
    assert '"kind": "measure"' in str(err) and '"kind": "metric"' in str(err)
    assert err.details["path"] == "select[0]"
    assert {h["code"] for h in err.details["recovery_hints"]} == {"WRAP_SELECT_EXPRESSION"}


# A dimension never travels with another key: nothing is dropped, so nothing is rewritten.
DIMENSION_MIXED = [
    pytest.param({"dimension": DIM, "metric": METRIC}, id="bare-dimension-and-metric"),
    pytest.param({"dimension": DIM, "measure": MEASURE}, id="bare-dimension-and-measure"),
    pytest.param({"dimension": DIM, "foo": 1}, id="bare-dimension-unknown-key"),
    pytest.param({"dimension": DIM, "as": "store"}, id="bare-dimension-alias"),
    pytest.param({"expression": {"dimension": DIM}, "metric": METRIC}, id="wrapped-and-metric"),
    pytest.param({"expression": {"dimension": DIM}, "measure": MEASURE}, id="wrapped-and-measure"),
    pytest.param({"expression": {"dimension": DIM}, "x": 1}, id="wrapped-unknown-key"),
    pytest.param({"expression": {"dimension": DIM}, "as": "store"}, id="wrapped-alias"),
    pytest.param(
        {"expression": {"kind": "metric", "metric": METRIC}, "dimension": DIM},
        id="expression-beside-dimension",
    ),
    pytest.param(
        {"expression": {"kind": "metric", "metric": METRIC}, "metric": METRIC},
        id="expression-beside-metric",
    ),
]


@pytest.mark.parametrize("item", DIMENSION_MIXED)
def test_dimension_item_carrying_other_keys_is_refused(item):
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(_query([item, {"metric": METRIC}]))
    err = excinfo.value
    assert err.code == "INVALID_EXPRESSION_AST"
    assert err.details["path"] == "select[0]"
    assert 'group_by: ["<dimension id>"]' in str(err)


def test_expression_dimension_is_refused_when_group_by_names_other_dimensions():
    payload = _query(
        [{"expression": {"dimension": DIM}}, {"metric": METRIC}],
        group_by=["dimension.jaffle_order_status"],
    )
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(payload)
    assert excinfo.value.code == "INVALID_EXPRESSION_AST"
    codes = {h["code"] for h in excinfo.value.details["recovery_hints"]}
    assert "MOVE_DIMENSION_TO_GROUP_BY" in codes


def test_kindless_expression_error_shows_canonical_form():
    with pytest.raises(SemanticLayerError) as excinfo:
        normalize_query(_query([{"expression": {"aggregation": "sum"}}]))
    assert '{"kind": "measure", "measure": "<measure id>"' in str(excinfo.value)


def test_runtime_accepts_shorthand_compiles_identically_and_warns(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        canonical = runtime.compile(
            _query(
                [
                    {
                        "expression": {"kind": "measure", "measure": MEASURE, "aggregation": "sum"},
                        "as": "rev",
                    },
                    {"expression": {"kind": "metric", "metric": METRIC}, "as": "aov"},
                ],
                group_by=[DIM],
                sql_profile="audit",
            )
        )
        shorthand = runtime.compile(
            _query(
                [
                    {"measure": MEASURE, "aggregation": "sum", "as": "rev"},
                    {"metric": METRIC, "as": "aov"},
                    {"dimension": DIM},
                ],
                sql_profile="audit",
            )
        )
        assert shorthand["rendered_sql"] == canonical["rendered_sql"]
        notes = [w for w in shorthand["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED"]
        assert [w["path"] for w in notes] == ["select[0]", "select[1]", "select[2]"]
        assert '"kind":"measure"' in notes[0]["message"]
        assert notes[2]["details"]["canonical"] == {"group_by": [DIM]}
        assert not [w for w in canonical["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED"]
        assert _without_notices(shorthand["warnings"]) == canonical["warnings"]
    finally:
        runtime.close()


def test_runtime_reports_expression_dimension_shorthand(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        canonical = runtime.compile(
            _query(
                [{"expression": {"kind": "metric", "metric": METRIC}, "as": "aov"}],
                group_by=[DIM],
                sql_profile="audit",
            )
        )
        compiled = runtime.compile(
            _query(
                [{"expression": {"dimension": DIM}}, {"metric": METRIC, "as": "aov"}],
                sql_profile="audit",
            )
        )
        notes = [w for w in compiled["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED"]
        assert [n["path"] for n in notes] == ["select[0]", "select[1]"]
        assert notes[0]["details"]["canonical"] == {"group_by": [DIM]}
        assert compiled["rendered_sql"] == canonical["rendered_sql"]
        assert _without_notices(compiled["warnings"]) == canonical["warnings"]
    finally:
        runtime.close()


def test_dimension_moved_to_group_by_is_reported_by_the_rewriter():
    _, notes = rewrite_select_shorthand(
        _query([{"expression": {"dimension": DIM}}, {"dimension": DIM}, {"metric": METRIC}])
    )
    assert [n["path"] for n in notes] == ["select[0]", "select[1]", "select[2]"]
    assert notes[0]["canonical"] == notes[1]["canonical"] == {"group_by": [DIM]}
    assert notes[2]["canonical"] == {"expression": {"kind": "metric", "metric": METRIC}}


def test_rewriting_the_canonical_form_changes_nothing():
    payload = _query([{"metric": METRIC}, {"dimension": DIM}])
    canonical, notes = rewrite_select_shorthand(payload)
    assert notes
    assert rewrite_select_shorthand(canonical) == (canonical, [])


def test_time_grain_warning_reads_the_rewritten_group_by(runtime_factory):
    no_grain = {"temporal_role": "temporal_role.jaffle_order_time"}
    shorthand = {
        "version": 1,
        "select": [{"expression": {"dimension": DIM}}, {"metric": METRIC}],
        "time": no_grain,
    }
    canonical = {
        "version": 1,
        "select": [{"expression": {"kind": "metric", "metric": METRIC}}],
        "group_by": [DIM],
        "time": no_grain,
    }
    for query in (shorthand, canonical):
        assert _grouped_ungrained_time_warning(query)["code"] == "UNGRAINED_GROUPED_TIME_PROJECTION"
    runtime = runtime_factory("jaffle_shop")
    try:
        codes = {
            name: [
                w["code"] for w in runtime.compile({**query, "sql_profile": "audit"})["warnings"]
            ]
            for name, query in (("shorthand", shorthand), ("canonical", canonical))
        }
    finally:
        runtime.close()
    assert "UNGRAINED_TIME_PROJECTION" not in codes["shorthand"]
    assert [c for c in codes["shorthand"] if c != "QUERY_SHORTHAND_NORMALIZED"] == codes[
        "canonical"
    ]


def test_plan_accepts_the_shorthand_execute_accepts(runtime_factory):
    shorthand = {"select": [{"metric": METRIC}, {"expression": {"dimension": DIM}}]}
    canonical = {
        "select": [{"expression": {"kind": "metric", "metric": METRIC}}],
        "group_by": [DIM],
    }
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
        assert excinfo.value.code == "INVALID_EXPRESSION_AST"
    finally:
        runtime.close()


def test_partial_query_still_accepts_an_empty_select_item():
    state = normalize_partial_query({"select": [{"as": "x"}, {"metric": METRIC}]})
    assert [item.as_ for item in state.select] == ["x", "expr_2"]
    assert state.select[0].expression is None
