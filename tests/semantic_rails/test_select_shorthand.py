"""Unambiguous select shorthands are accepted and reported; ambiguous ones keep the error."""

from __future__ import annotations

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.errors import SemanticLayerError

MEASURE = "measure.jaffle.revenue_usd"
METRIC = "metric.sales.aov_usd"
DIM = "dimension.jaffle_store_name"
TIME = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}


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
    finally:
        runtime.close()


def test_runtime_reports_expression_dimension_shorthand(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        compiled = runtime.compile(
            _query([{"expression": {"dimension": DIM}}, {"metric": METRIC}], sql_profile="audit")
        )
        notes = [w for w in compiled["warnings"] if w["code"] == "QUERY_SHORTHAND_NORMALIZED"]
        note = notes[0]
        assert note["path"] == "select[0]"
        assert note["details"]["canonical"] == {"group_by": [DIM]}
    finally:
        runtime.close()
