"""End-to-end tests for the ``aggregate_if`` shorthand.

Covers:
- query-level compile against jaffle_shop (DuckDB default + Snowflake)
- the synthetic-measure rewrite (``lift_conditional_aggregates``)
- the dialect-dispatch optimisation that lights up ``COUNT_IF`` /
  ``SUM_IF`` on Snowflake and falls back to portable ``CASE WHEN``
  elsewhere
- error paths surfaced by ``UNSUPPORTED_CONDITIONAL_AGGREGATE``
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query, plan_query
from semantic_rails.compiler_parts.bind import (
    _aggregation_expr,
    lift_conditional_aggregates,
)
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.dialects import SnowflakeDialect, SqlDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    AggregateExpr,
)
from semantic_rails.schema import ConnectionSpec
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlLiteral,
)


@pytest.fixture
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


COUNT_IF_PAYLOAD = {
    "version": 2,
    "select": [
        {
            "expression": {
                "kind": "aggregate_if",
                "aggregation": "count",
                "condition": {
                    "kind": "comparison",
                    "op": "=",
                    "left": {
                        "kind": "column",
                        "column": "is_new_customer_order",
                        "entity": "entity.jaffle_order",
                    },
                    "right": {"kind": "literal", "value": True},
                },
            },
            "as": "new_customer_order_count",
        }
    ],
}

SUM_IF_PAYLOAD = {
    "version": 2,
    "select": [
        {
            "expression": {
                "kind": "aggregate_if",
                "aggregation": "sum",
                "condition": {
                    "kind": "comparison",
                    "op": "=",
                    "left": {
                        "kind": "column",
                        "column": "is_large_order",
                        "entity": "entity.jaffle_order",
                    },
                    "right": {"kind": "literal", "value": True},
                },
                "value": {
                    "kind": "column",
                    "column": "order_total_cents",
                    "entity": "entity.jaffle_order",
                },
            },
            "as": "large_order_revenue_cents",
        }
    ],
}


def test_count_if_compiles_on_duckdb_to_portable_case_when(jaffle_config):
    result = compile_query(jaffle_config, None, COUNT_IF_PAYLOAD)
    sql = result["sql"]
    # Default dialect (duckdb): portable form.
    assert re.search(r"COUNT\(CASE WHEN .* THEN 1 END\)", sql), sql
    assert "new_customer_order_count" in sql


def test_count_if_compiles_on_snowflake_to_native_count_if(jaffle_config):
    config = _snowflake(jaffle_config)
    result = compile_query(config, None, COUNT_IF_PAYLOAD)
    sql = result["sql"]
    assert "COUNT_IF(" in sql, sql
    # Should NOT emit the portable form on Snowflake.
    assert "COUNT(CASE WHEN" not in sql, sql


def test_sum_if_compiles_on_duckdb_to_portable_case_when(jaffle_config):
    result = compile_query(jaffle_config, None, SUM_IF_PAYLOAD)
    sql = result["sql"]
    assert re.search(r"SUM\(CASE WHEN .* THEN .*order_total_cents.* END\)", sql), sql


def test_sum_if_compiles_on_snowflake_to_portable_case_when(jaffle_config):
    # Snowflake has no SUM_IF — the old native lowering raised
    # "Unknown function SUM_IF" live and was caught by the cross-warehouse
    # conformance suite. The sum shape must use the portable CASE form.
    config = _snowflake(jaffle_config)
    result = compile_query(config, None, SUM_IF_PAYLOAD)
    sql = result["sql"]
    assert "SUM_IF(" not in sql, sql
    assert re.search(r"SUM\(CASE WHEN .* THEN .*order_total_cents.* END\)", sql), sql


def test_synthetic_measure_attached_to_logical_plan(jaffle_config):
    plan = plan_query(jaffle_config, None, COUNT_IF_PAYLOAD)
    assert len(plan.synthetic_measures) == 1
    measure_id, measure = next(iter(plan.synthetic_measures.items()))
    assert measure_id.startswith("measure.__aggif__.")
    assert measure.default_aggregation == "count"
    assert measure.entity == "entity.jaffle_order"
    assert measure.meta.get("source") == "aggregate_if"


def test_lift_conditional_aggregates_is_idempotent_for_identical_payloads(jaffle_config):
    """Two select items that desugar to the same aggregate_if collapse
    to a single synthetic measure (deterministic hash + dedup).
    """
    payload = {
        "version": 2,
        "select": [
            {
                "expression": COUNT_IF_PAYLOAD["select"][0]["expression"],
                "as": "a",
            },
            {
                "expression": COUNT_IF_PAYLOAD["select"][0]["expression"],
                "as": "b",
            },
        ],
    }
    plan = plan_query(jaffle_config, None, payload)
    assert len(plan.synthetic_measures) == 1


def test_column_without_entity_or_table_is_rejected(jaffle_config):
    payload = {
        "version": 2,
        "select": [
            {
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    # Missing entity/table — can't resolve.
                    "condition": {"kind": "column", "column": "is_new_customer_order"},
                },
                "as": "x",
            }
        ],
    }
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(jaffle_config, None, payload)
    assert exc.value.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"


def test_unsupported_aggregation_is_rejected(jaffle_config):
    payload = {
        "version": 2,
        "select": [
            {
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "stddev",  # not supported
                    "condition": {
                        "kind": "column",
                        "column": "is_new_customer_order",
                        "entity": "entity.jaffle_order",
                    },
                    "value": {
                        "kind": "column",
                        "column": "order_total_cents",
                        "entity": "entity.jaffle_order",
                    },
                },
                "as": "x",
            }
        ],
    }
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(jaffle_config, None, payload)
    assert exc.value.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"


# ---------------------------------------------------------------------------
# Dialect-hook unit tests — verify the pattern detection in
# ``_maybe_conditional_aggregate`` lights up native forms when present
# and falls through otherwise.
# ---------------------------------------------------------------------------


def _case(condition, body):
    return SqlCase(whens=[SqlCaseWhen(condition=condition, result=body)], else_expr=None)


def test_dialect_hook_emits_count_if_on_snowflake():
    cond = SqlBinary(SqlLiteral("col"), "=", SqlLiteral(True))
    sf = SnowflakeDialect()
    result = _aggregation_expr(_case(cond, SqlLiteral(1)), "count", dialect=sf)
    assert isinstance(result, SqlCall)
    assert result.name == "COUNT_IF"
    assert result.args == [cond]


def test_dialect_hook_keeps_portable_sum_on_snowflake():
    # Snowflake has COUNT_IF but NOT SUM_IF (verified live) — the sum
    # shape must stay on the portable CASE form.
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    body = SqlLiteral("amount")
    sf = SnowflakeDialect()
    result = _aggregation_expr(_case(cond, body), "sum", dialect=sf)
    assert isinstance(result, SqlCall)
    assert result.name == "SUM"
    assert isinstance(result.args[0], SqlCase)


def test_dialect_hook_falls_back_to_portable_case_when_on_duckdb():
    cond = SqlBinary(SqlLiteral("col"), "=", SqlLiteral(True))
    duck = SqlDialect(name="duckdb")
    result = _aggregation_expr(_case(cond, SqlLiteral(1)), "count", dialect=duck)
    assert isinstance(result, SqlCall)
    assert result.name == "COUNT"
    assert isinstance(result.args[0], SqlCase)


def test_dialect_hook_skipped_for_multi_when_case_expressions():
    """The optimisation only fires on the single-branch shape that
    ``aggregate_if`` produces. Multi-branch CASE WHENs (e.g. bucketing
    measures) should keep the regular ``<AGG>(CASE WHEN…)`` form so we
    don't change semantics for handwritten measures."""
    cond_a = SqlBinary(SqlLiteral("x"), "=", SqlLiteral(1))
    cond_b = SqlBinary(SqlLiteral("x"), "=", SqlLiteral(2))
    multi_case = SqlCase(
        whens=[
            SqlCaseWhen(condition=cond_a, result=SqlLiteral(10)),
            SqlCaseWhen(condition=cond_b, result=SqlLiteral(20)),
        ],
        else_expr=None,
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(multi_case, "sum", dialect=sf)
    # No SUM_IF — falls through to plain SUM around the CASE.
    assert isinstance(result, SqlCall) and result.name == "SUM"
    assert isinstance(result.args[0], SqlCase)


# ---------------------------------------------------------------------------
# Detector extension — ELSE NULL and ELSE 0 patterns.
#
# Real-world configs and the mf2sr translator emit explicit ELSE branches
# that are semantically equivalent to no-else. The detector folds them so
# every fixture that uses the legacy CASE WHEN idiom benefits from native
# COUNT_IF / SUM_IF on Snowflake without any YAML or translator change.
# ---------------------------------------------------------------------------


def test_dialect_hook_folds_else_null_for_count():
    """Handwritten measures (jaffle_shop orders.yml uses this 4×) write
    ``CASE WHEN cond THEN key END`` with an explicit ``else: literal null``.
    The detector should treat this exactly like the no-else form.
    """
    cond = SqlBinary(SqlLiteral("is_new_customer_order"), "=", SqlLiteral(True))
    body = SqlLiteral("order_id")
    case_with_null_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=body)],
        else_expr=SqlLiteral(None),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_null_else, "count", dialect=sf)
    # Non-literal body so we get the explicit-value form, not COUNT_IF.
    # The Snowflake override falls back to portable form for this shape
    # (no native form exists for COUNT with a non-1 body).
    assert isinstance(result, SqlCall) and result.name == "COUNT"


def test_dialect_hook_folds_else_null_for_sum_to_portable_case():
    # The detector still folds ELSE NULL; with no native SUM_IF on
    # Snowflake the result is the portable SUM(CASE WHEN … END) with the
    # redundant ELSE dropped.
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    body = SqlLiteral("amount")
    case_with_null_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=body)],
        else_expr=SqlLiteral(None),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_null_else, "sum", dialect=sf)
    assert isinstance(result, SqlCall) and result.name == "SUM"
    folded = result.args[0]
    assert isinstance(folded, SqlCase) and folded.else_expr is None
    assert folded.whens[0].condition == cond and folded.whens[0].result == body


def test_dialect_hook_folds_else_zero_for_sum_to_portable_case():
    """mf2sr's `sum_boolean` and count-fallback paths emit
    ``SUM(CASE WHEN cond THEN 1 ELSE 0 END)`` — 0 contributes nothing to
    a SUM, so the detector folds the ELSE away. With no native SUM_IF on
    Snowflake the lowering is the portable else-less CASE.
    """
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    case_with_zero_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=SqlLiteral(1))],
        else_expr=SqlLiteral(0),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_zero_else, "sum", dialect=sf)
    assert isinstance(result, SqlCall) and result.name == "SUM"
    folded = result.args[0]
    assert isinstance(folded, SqlCase) and folded.else_expr is None


def test_dialect_hook_does_not_fold_else_zero_for_count():
    """``COUNT(CASE WHEN cond THEN 1 ELSE 0 END)`` counts every row
    (0 is non-null), so it is NOT equivalent to ``COUNT_IF(cond)``.
    The detector must skip this pattern to preserve semantics.
    """
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    case_with_zero_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=SqlLiteral(1))],
        else_expr=SqlLiteral(0),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_zero_else, "count", dialect=sf)
    # NOT COUNT_IF — falls through to plain COUNT(CASE …).
    assert isinstance(result, SqlCall) and result.name == "COUNT"
    assert isinstance(result.args[0], SqlCase)


def test_dialect_hook_does_not_fold_else_zero_for_avg():
    """``AVG(CASE WHEN cond THEN x ELSE 0 END)`` would mix zeros into
    the population, changing the average. Reject the fold for AVG/MIN/MAX.
    """
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    case_with_zero_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=SqlLiteral("x"))],
        else_expr=SqlLiteral(0),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_zero_else, "avg", dialect=sf)
    assert isinstance(result, SqlCall) and result.name == "AVG"
    assert isinstance(result.args[0], SqlCase)


def test_dialect_hook_does_not_fold_arbitrary_else_literal():
    """A non-NULL, non-0 ELSE literal materially changes results and
    must never be folded.
    """
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    case_with_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=SqlLiteral(1))],
        else_expr=SqlLiteral(-1),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_else, "sum", dialect=sf)
    assert isinstance(result, SqlCall) and result.name == "SUM"
    assert isinstance(result.args[0], SqlCase)


def test_dialect_hook_does_not_fold_else_null_for_count_with_non_literal_body():
    """``COUNT(CASE WHEN cond THEN col ELSE NULL END)`` is equivalent
    to no-else, but COUNT_IF(cond) ≠ COUNT(... THEN col ...) — the
    latter excludes rows where col is null. The detector routes to
    ``dialect.conditional_aggregate("count", cond, col)``; the
    Snowflake override falls back to the portable form since no native
    COUNT_IF-with-value form exists. Result: no regression on the
    handwritten ``COUNT(DISTINCT CASE WHEN cond THEN key END)`` idiom.
    """
    cond = SqlBinary(SqlLiteral("flag"), "=", SqlLiteral(True))
    body = SqlLiteral("order_id")  # non-literal stand-in
    case_with_null_else = SqlCase(
        whens=[SqlCaseWhen(condition=cond, result=body)],
        else_expr=SqlLiteral(None),
    )
    sf = SnowflakeDialect()
    result = _aggregation_expr(case_with_null_else, "count", dialect=sf)
    # Routed through dialect hook; Snowflake falls back to portable
    # COUNT(CASE …) because COUNT_IF doesn't take a value argument.
    assert isinstance(result, SqlCall) and result.name == "COUNT"


def test_lift_conditional_aggregates_replaces_node_with_aggregate_expr(jaffle_config):
    """The rewrite pass replaces ConditionalAggregateExpr with a normal
    AggregateExpr pointing at the synthetic measure."""
    from semantic_rails.ast import normalize_query

    query = normalize_query(COUNT_IF_PAYLOAD)
    rewritten, synthetics = lift_conditional_aggregates(query, jaffle_config)
    assert len(synthetics) == 1
    synthetic_id = next(iter(synthetics))
    assert isinstance(rewritten.select[0].expression, AggregateExpr)
    assert rewritten.select[0].expression.measure == synthetic_id
