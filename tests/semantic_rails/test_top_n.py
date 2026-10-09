"""Limited queries preserve requested ranking and disclose observed cutoff ties."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.db_parts.base import QueryRows, WarehouseAdapter
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_select
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlField, SqlIdentifier, SqlLiteral, SqlOrder, SqlSelect
from semantic_rails.top_n import limit_order, limit_rows


@pytest.mark.parametrize("warehouse", supported_warehouses())
@pytest.mark.parametrize("profile", ["audit", "compact", "debug"])
def test_compiled_top_n_prepares_one_extra_row(package_config_factory, warehouse, profile):
    package_config, _ = package_config_factory("jaffle_shop")
    config = replace(package_config, package=replace(package_config.package, warehouse=warehouse))
    compiled = compile_query(
        config,
        Registry(config),
        {
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "group_by": ["dimension.jaffle_store_name"],
            "order_by": [{"field": "orders", "direction": "DESC"}],
            "limit": 2,
            "sql_profile": profile,
        },
    )
    assert compiled["limit_order_keys"] == ("orders",)
    assert [term.direction for term in compiled["sql_ast"].order_by] == ["DESC", "ASC"]
    assert [term.nulls_last for term in compiled["sql_ast"].order_by] == [True, True]
    assert "ASC NULLS LAST" in compiled["sql"]
    assert "LIMIT 2" in compiled["prepared_query"].sql
    assert "LIMIT 3" in compiled["limit_probe"].sql
    assert compiled["limit_probe"].column_mapping == compiled["prepared_query"].column_mapping


@pytest.mark.parametrize("limit,ordered", [(None, True), (2, False), (0, True)])
def test_unlimited_or_unordered_queries_keep_their_order(limit, ordered):
    query = SqlSelect(
        select=[SqlField(SqlLiteral(1), "score"), SqlField(SqlLiteral(2), "name")],
        order_by=[SqlOrder(SqlIdentifier(["score"]), "DESC")] if ordered else [],
        limit=limit,
    )
    final, _ = limit_order(query)
    if limit is None or not ordered:
        assert final is query
        assert "NULLS LAST" not in render_select(final)
    else:
        assert final.limit == 0


def test_tiebreaks_follow_output_order_and_skip_requested_columns():
    query = SqlSelect(
        select=[SqlField(SqlLiteral(index), name) for index, name in enumerate(["b", "c", "a"])],
        order_by=[SqlOrder(SqlIdentifier(["c"]), "DESC"), SqlOrder(SqlIdentifier(["b"]), "ASC")],
        limit=1,
    )
    final, keys = limit_order(query)
    assert keys == ("c", "b")
    assert final.order_by[:2] == query.order_by
    assert final.order_by[2:] == [SqlOrder(SqlIdentifier(["a"]), "ASC", nulls_last=True)]
    final, keys = limit_order(replace(query, order_by=query.order_by[:1]))
    assert keys == ("c",)
    assert [term.expression.parts for term in final.order_by[1:]] == [["b"], ["a"]]


def test_unprojected_order_cannot_bypass_the_limited_order_guard():
    with pytest.raises(SemanticLayerError) as error:
        limit_order(
            SqlSelect(
                select=[SqlField(SqlLiteral(1), "score")],
                order_by=[SqlOrder(SqlIdentifier(["hidden"]), "ASC")],
                limit=2,
            )
        )
    assert error.value.code == "INVALID_ORDER_BY"


@pytest.mark.parametrize(
    "values,limit,count",
    [
        ([3, 2, 2], 2, 2),
        ([2, 2, 2], 2, 3),
        ([None, None], 1, 2),
        ([3, 2, 1], 2, None),
        ([3, 2], 2, None),
        ([], 2, None),
        ([1], 0, None),
    ],
)
def test_boundary_warning_counts_only_observed_ties(values, limit, count):
    rows, warnings = limit_rows([{"score": value} for value in values], limit, ("score",))
    assert rows == [{"score": value} for value in values[:limit]]
    if count is None:
        assert warnings == []
    else:
        assert warnings[0]["code"] == "TIES_AT_LIMIT"
        assert warnings[0]["details"] == {
            "limit": limit,
            "tie_count": count,
            "tie_count_is_lower_bound": True,
        }


@pytest.mark.parametrize("keys", [("a", "b"), ("A", "B")])
def test_all_requested_sort_keys_must_tie(keys):
    rows = [{"a": 1, "b": 2}, {"a": 1, "b": 3}]
    assert limit_rows(rows, 1, keys)[1] == []


def test_missing_sort_key_is_not_treated_as_null():
    with pytest.raises(KeyError):
        limit_rows([{}, {}], 1, ("score",))


@pytest.mark.parametrize(
    "rows",
    [
        [{"SCORE": 1, "Score": 1}, {"SCORE": 1, "Score": 1}],
        [{"SCORE": 1}, {"score": 1}],
    ],
    ids=["ambiguous-folded-key", "inconsistent-row-keys"],
)
def test_unresolvable_sort_keys_fail_closed(rows):
    with pytest.raises(KeyError):
        limit_rows(rows, 1, ("score",))


def test_exact_sort_key_takes_precedence_over_folded_matches():
    rows = [{"score": 1, "SCORE": 2}, {"score": 1, "SCORE": 3}]
    limited, warnings = limit_rows(rows, 1, ("score",))
    assert limited == rows[:1]
    assert warnings[0]["code"] == "TIES_AT_LIMIT"


@pytest.mark.parametrize(
    "warehouse,alias,returned_key,store_key",
    [
        ("snowflake", "orders", "ORDERS", "STORE"),
        ("postgres", "orderCount", "ordercount", "store"),
        ("athena", "orderCount", "ordercount", "store"),
    ],
)
@pytest.mark.parametrize("tied", [False, True], ids=["distinct-cutoff", "tied-cutoff"])
def test_runtime_cutoff_uses_warehouse_result_key_casing(
    package_config_factory, warehouse, alias, returned_key, store_key, tied
):
    config, config_path = package_config_factory("jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    rows = [
        {returned_key: value, store_key: store}
        for value, store in zip([3, 2, 2 if tied else 1], ["a", "b", "c"], strict=True)
    ]
    statements = []

    class ResultAdapter(WarehouseAdapter):
        engine = warehouse

        def query(self, sql, *, limits=None):
            statements.append(sql)
            return rows

        def close(self):
            pass

    runtime = Runtime.from_config(config, source_path=str(config_path))
    runtime.set_adapter(ResultAdapter())
    try:
        result = runtime.query(
            {
                "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": alias}],
                "group_by": ["dimension.jaffle_store_name"],
                "order_by": [{"field": alias, "direction": "DESC"}],
                "limit": 2,
            }
        )
    finally:
        runtime.close()
    assert result["status"] == "ok"
    assert result["rows"] == rows[:2]
    assert result["row_count"] == 2
    assert not result["truncated"]
    assert len(statements) == 1
    assert "LIMIT 3" in statements[0]
    warnings = [warning for warning in result["warnings"] if warning["code"] == "TIES_AT_LIMIT"]
    if tied:
        assert len(warnings) == 1
        assert warnings[0]["details"] == {
            "limit": 2,
            "tie_count": 2,
            "tie_count_is_lower_bound": True,
        }
    else:
        assert not warnings


def test_probe_removal_preserves_adapter_truncation():
    rows, _ = limit_rows(QueryRows([{"score": 1}, {"score": 2}], truncated=True), 1, ("score",))
    assert rows.truncated
