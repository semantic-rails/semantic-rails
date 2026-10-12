"""The ``aggregate_if`` shorthand.

Covers compiling it against jaffle_shop (DuckDB and Snowflake), its rewrite to a synthetic
measure, the dialect hook that picks ``COUNT_IF`` where a warehouse has it and keeps the
portable ``CASE WHEN`` elsewhere, and its refusals.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.compiler import compile_query, plan_query
from semantic_rails.compiler_parts.bind import _aggregation_expr, lift_conditional_aggregates
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.dialects import SnowflakeDialect, SqlDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import AggregateExpr
from semantic_rails.schema import ConnectionSpec
from semantic_rails.sql_ast import SqlBinary, SqlCall, SqlCase, SqlCaseWhen, SqlLiteral


@pytest.fixture(scope="module")
def jaffle_config():
    return load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))


def _snowflake(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="snowflake",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(kind="snowflake_cli", name="semantic_views_trial"),
        ),
    )


def _order_column(column: str) -> dict:
    return {"kind": "column", "column": column, "entity": "entity.jaffle_order"}


def _is_true(column: str) -> dict:
    return {
        "kind": "comparison",
        "op": "=",
        "left": _order_column(column),
        "right": {"kind": "literal", "value": True},
    }


COUNT_IF = {
    "kind": "aggregate_if",
    "aggregation": "count",
    "condition": _is_true("is_new_customer_order"),
}
COUNT_IF_PAYLOAD = {"select": [{"expression": COUNT_IF, "as": "new_customer_order_count"}]}
SUM_IF_PAYLOAD = {
    "select": [
        {
            "expression": {
                "kind": "aggregate_if",
                "aggregation": "sum",
                "condition": _is_true("is_large_order"),
                "value": _order_column("order_total_cents"),
            },
            "as": "large_order_revenue_cents",
        }
    ],
}
SUM_CASE = r"SUM\(CASE WHEN .* THEN .*order_total_cents.* END\)"


def test_conditional_aggregates_compile_to_the_dialects_form(jaffle_config):
    # Snowflake has COUNT_IF, but SUM must use portable CASE WHEN.
    for snowflake, payload, pattern, forbidden in [
        (False, COUNT_IF_PAYLOAD, r"COUNT\(CASE WHEN .* THEN 1 END\)", ""),
        (True, COUNT_IF_PAYLOAD, r"COUNT_IF\(", "COUNT(CASE WHEN"),
        (False, SUM_IF_PAYLOAD, SUM_CASE, ""),
        (True, SUM_IF_PAYLOAD, SUM_CASE, "SUM_IF("),
    ]:
        config = _snowflake(jaffle_config) if snowflake else jaffle_config
        sql = compile_query(config, None, payload)["sql"]
        assert re.search(pattern, sql), sql
        assert not forbidden or forbidden not in sql, sql
        assert payload["select"][0]["as"] in sql


def test_identical_conditional_aggregates_lift_to_one_synthetic_measure(jaffle_config):
    """Two select items that desugar to the same aggregate_if share one synthetic measure,
    and the rewrite points each at it with a plain aggregate."""
    twice = {"select": [{"expression": COUNT_IF, "as": "a"}, {"expression": COUNT_IF, "as": "b"}]}
    [(measure_id, measure)] = plan_query(jaffle_config, None, twice).synthetic_measures.items()
    assert measure_id.startswith("measure.__aggif__.")
    assert (measure.default_aggregation, measure.entity, measure.meta.get("source")) == (
        "count",
        "entity.jaffle_order",
        "aggregate_if",
    )
    rewritten, synthetics = lift_conditional_aggregates(
        normalize_query(COUNT_IF_PAYLOAD), jaffle_config
    )
    assert list(synthetics) == [measure_id]
    assert isinstance(rewritten.select[0].expression, AggregateExpr)
    assert rewritten.select[0].expression.measure == measure_id


@pytest.mark.parametrize(
    "expression",
    [
        {**COUNT_IF, "condition": {"kind": "column", "column": "is_new_customer_order"}},
        {**COUNT_IF, "aggregation": "stddev", "value": _order_column("order_total_cents")},
    ],
    ids=["column_without_entity_or_table", "unsupported_aggregation"],
)
def test_unresolvable_conditional_aggregates_are_refused(jaffle_config, expression):
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(jaffle_config, None, {"select": [{"expression": expression, "as": "x"}]})
    assert exc.value.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"


FLAG = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
ONE, AMOUNT, NULL, ZERO = SqlLiteral(1), SqlLiteral("amount"), SqlLiteral(None), SqlLiteral(0)
SNOWFLAKE, DUCKDB = SnowflakeDialect(), SqlDialect(name="duckdb")


def _case(result, else_expr=None):
    return SqlCase(whens=[SqlCaseWhen(condition=FLAG, result=result)], else_expr=else_expr)


TWO_WHENS = SqlCase(
    whens=[
        SqlCaseWhen(condition=FLAG, result=SqlLiteral(10)),
        SqlCaseWhen(condition=SqlBinary(SqlLiteral("x"), "=", SqlLiteral(2)), result=ONE),
    ]
)

# (dialect, aggregation, CASE, the aggregate it lowers to). Only aggregate_if's one-branch
# shape takes a native form, and Snowflake has COUNT_IF but no SUM_IF. ELSE NULL folds away,
# as in handwritten measures; a zero or any other ELSE value changes results and stays.
HOOK_CASES = {
    "count_if_on_snowflake": (SNOWFLAKE, "count", _case(ONE), SqlCall("COUNT_IF", [FLAG])),
    "portable_count_on_duckdb": (DUCKDB, "count", _case(ONE), SqlCall("COUNT", [_case(ONE)])),
    "no_sum_if_on_snowflake": (SNOWFLAKE, "sum", _case(AMOUNT), SqlCall("SUM", [_case(AMOUNT)])),
    "two_whens_stay": (SNOWFLAKE, "sum", TWO_WHENS, SqlCall("SUM", [TWO_WHENS])),
    "else_null_folds_for_count_of_a_value": (
        SNOWFLAKE,
        "count",
        _case(SqlLiteral("order_id"), NULL),
        SqlCall("COUNT", [_case(SqlLiteral("order_id"))]),
    ),
    "else_null_folds_for_sum": (
        SNOWFLAKE,
        "sum",
        _case(AMOUNT, NULL),
        SqlCall("SUM", [_case(AMOUNT)]),
    ),
    **{
        f"else_zero_stays_for_{aggregation}": (
            SNOWFLAKE,
            aggregation,
            _case(ONE, ZERO),
            SqlCall(aggregation.upper(), [_case(ONE, ZERO)]),
        )
        for aggregation in ("sum", "count", "avg", "min", "max")
    },
    "other_else_literal_stays": (
        SNOWFLAKE,
        "sum",
        _case(ONE, SqlLiteral(-1)),
        SqlCall("SUM", [_case(ONE, SqlLiteral(-1))]),
    ),
}


def test_the_dialect_hook_picks_a_native_form_only_for_the_aggregate_if_shape():
    for case, (dialect, aggregation, expression, expected) in HOOK_CASES.items():
        assert _aggregation_expr(expression, aggregation, dialect=dialect) == expected, case


def test_aggregate_if_by_time_is_refused_without_naming_a_synthetic_measure(jaffle_config):
    """An aggregate_if has no clock and no measure to declare one on: its refusal says so and
    carries no hint that points at an internal measure id."""
    role = "temporal_role.jaffle_order_time"
    payload = {**COUNT_IF_PAYLOAD, "time": {"temporal_role": role, "grain": "month"}}
    with pytest.raises(SemanticLayerError) as raised:
        plan_query(jaffle_config, None, payload)
    error = raised.value
    assert error.code == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert error.details == {"requested": role, "compatible": [], "source": "aggregate_if"}
    assert "__aggif__" not in str(error) + repr(error.details)
    assert recovery_hints_for_error(error.code, error.details) == []
