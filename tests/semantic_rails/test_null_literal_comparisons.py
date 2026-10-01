"""Null tests agree with independent SQL across all comparison lowering paths."""

from __future__ import annotations

import re
from dataclasses import replace

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.bind import _config_expr_to_sql
from semantic_rails.compiler_parts.post_aggregation import _compile_post_expr
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.relation_pipelines import _join_condition, _predicate, _semantic_expr_to_sql
from semantic_rails.renderer import render_expr
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlIdentifier,
    SqlLiteral,
    build_comparison_condition,
)

ENTITY = "entity.nulls_record"
KEY = "dimension.nulls_record_id"
COUNT = {"measure": "measure.nulls.records"}
NULL = {"kind": "literal", "value": None}
NOT_NULL = {"kind": "boolean", "op": "not", "args": [NULL]}
SEED = """
CREATE TABLE records (id INTEGER, value INTEGER);
INSERT INTO records VALUES (1, NULL), (2, NULL), (3, NULL), (4, NULL),
                          (5, NULL), (6, NULL), (7, 10);
"""


def _comparison(op="=", *, column="value", reverse=False):
    left = {"kind": "column", "entity": ENTITY, "column": column}
    right = {"kind": "literal", "value": None}
    return {
        "kind": "comparison",
        "op": op,
        "left": right if reverse else left,
        "right": left if reverse else right,
    }


def _conditional_count(op="=", *, column="value", reverse=False):
    return {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": _comparison(op, column=column, reverse=reverse),
    }


def _query(expression, **extra):
    return {"version": 2, "select": [{"expression": expression, "as": "n"}], **extra}


def _metric_predicate(input_, op=">", value=0):
    return {
        "expression": {
            "kind": "metric_predicate",
            "entity": ENTITY,
            "scope_mode": "entity_only",
            "input": input_,
            "op": op,
            "value": value,
        },
        "op": "=",
        "value": True,
    }


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("null-comparisons")
    (root / "models").mkdir()
    (root / "segments").mkdir()
    (root / "seed.sql").write_text(SEED)
    (root / "package.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "package": {
                    "id": "nulls",
                    "namespace": "nulls",
                    "warehouse": "duckdb",
                    "default_db": "records.duckdb",
                    "seed": {"kind": "sql_script", "source": "seed.sql"},
                },
            }
        )
    )
    (root / "graph.yml").write_text(
        yaml.safe_dump({"graph": {"entities": {"record": {"key": ["id"], "model": "records"}}}})
    )
    (root / "models" / "records.yml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "id": "records",
                    "relation": "records",
                    "entities": {"record": {}},
                    "dimensions": {
                        "id": {"kind": "categorical", "as": KEY},
                        "value": {"kind": "categorical", "as": "dimension.nulls_value"},
                    },
                    "measures": {
                        "records": {
                            "kind": "entity_count",
                            "entity_key": "id",
                            "value_type": "count",
                            "accumulation": {"kind": "event"},
                        },
                        "value": {
                            "kind": "aggregate",
                            "expr": "value",
                            "value_type": "number",
                            "accumulation": {"kind": "flow"},
                        },
                    },
                }
            }
        )
    )
    (root / "segments" / "missing.yml").write_text(
        yaml.safe_dump(
            {
                "segments": {
                    "missing": {
                        "entity": ENTITY,
                        "basis_metric": "metric.nulls.records",
                        "membership": {"metric_filters": [_metric_predicate(_conditional_count())]},
                    }
                }
            }
        )
    )
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def _gold(sql):
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return connection.execute(sql).fetchall()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "op, gold_op", [("=", "IS NULL"), ("!=", "IS NOT NULL"), ("<>", "IS NOT NULL")]
)
def test_conditional_count_matches_sql(runtime, op, gold_op, reverse):
    query = _query(_conditional_count(op, reverse=reverse))
    expected = _gold(f"SELECT COUNT(*) FROM records WHERE value {gold_op}")[0][0]
    assert runtime.query(query)["rows"] == [{"n": expected}]
    assert gold_op in compile_query(runtime.config, runtime.registry, query)["sql"]


def test_non_null_share_is_one_hundred_percent(runtime):
    expression = {
        "kind": "arithmetic",
        "op": "multiply",
        "left": {
            "kind": "ratio",
            "numerator": _conditional_count("!=", column="id"),
            "denominator": COUNT,
        },
        "right": {"kind": "literal", "value": 100},
    }
    expected = _gold("SELECT 100.0 * COUNT(id) / COUNT(*) FROM records")[0][0]
    assert expected == 100
    assert runtime.query(_query(expression))["rows"] == [{"n": expected}]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("op", ["IS DISTINCT FROM", "IS NOT DISTINCT FROM"])
def test_null_safe_comparisons_keep_their_meaning(runtime, op, reverse):
    query = _query(_conditional_count(op, reverse=reverse))
    expected = _gold(f"SELECT COUNT(*) FROM records WHERE value {op} NULL")[0][0]
    assert expected == (1 if op == "IS DISTINCT FROM" else 6)
    assert runtime.query(query)["rows"] == [{"n": expected}]
    sql = compile_query(runtime.config, runtime.registry, query)["sql"]
    assert (f"NULL {op} records.value" if reverse else f"records.value {op} NULL") in sql


@pytest.mark.parametrize("op", ["IS DISTINCT FROM", "IS NOT DISTINCT FROM"])
def test_where_filter_null_safe_comparison_matches_sql(runtime, op):
    where = [{"field": "dimension.nulls_value", "op": op, "value": None}]
    expected = _gold(f"SELECT COUNT(*) FROM records WHERE value {op} NULL")[0][0]
    assert runtime.query(_query(COUNT, where=where))["rows"] == [{"n": expected}]


@pytest.mark.parametrize(
    ("op", "expected"), [("IS DISTINCT FROM", False), ("IS NOT DISTINCT FROM", True)]
)
def test_two_null_literals_under_a_null_safe_op(op, expected):
    sql = render_expr(build_comparison_condition(SqlLiteral(None), op, SqlLiteral(None)))
    assert sql == f"NULL {op} NULL"
    assert _gold(f"SELECT {sql}") == [(expected,)]


@pytest.mark.parametrize("warehouse", ["databricks", "clickhouse"])
def test_null_safe_equality_operator_renders_unchanged(runtime, warehouse):
    config = replace(runtime.config, package=replace(runtime.config.package, warehouse=warehouse))
    sql = compile_query(config, None, _query(_conditional_count("<=>")))["sql"]
    assert "records.value <=> NULL" in sql


@pytest.mark.parametrize("op", ["<", "<=", ">", ">="])
@pytest.mark.parametrize("reverse", [False, True])
def test_ordering_null_refuses(runtime, op, reverse):
    with pytest.raises(SemanticLayerError) as exc:
        compile_query(
            runtime.config, runtime.registry, _query(_conditional_count(op, reverse=reverse))
        )
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details["recovery_hints"][0]["code"] == "USE_NULL_TEST_OR_SCALAR"


def test_metric_predicate_conditional_count_matches_sql(runtime):
    query = _query(COUNT, metric_filters=[_metric_predicate(_conditional_count())])
    expected = _gold("SELECT COUNT(*) FROM records WHERE value IS NULL")[0][0]
    assert runtime.query(query)["rows"] == [{"n": expected}]


def test_segment_conditional_count_matches_sql(runtime):
    expected = _gold("SELECT id FROM records WHERE value IS NULL ORDER BY id")
    preview = runtime.segment_preview("segment.nulls.missing")
    assert preview["member_count"] == len(expected) == 6
    assert sorted(row[KEY] for row in preview["rows"]) == [row[0] for row in expected]
    assert "IS NULL" in runtime.segment_explain("segment.nulls.missing")["rendered_sql"]


@pytest.mark.parametrize("warehouse", supported_warehouses())
@pytest.mark.parametrize(
    "op, sql_op", [("=", "IS NULL"), ("!=", "IS NOT NULL"), ("<>", "IS NOT NULL")]
)
def test_null_tests_render_on_every_dialect(runtime, warehouse, op, sql_op):
    config = replace(runtime.config, package=replace(runtime.config.package, warehouse=warehouse))
    sql = compile_query(config, None, _query(_conditional_count(op)))["sql"]
    assert sql_op in sql
    assert not re.search(r"(?:=|!=|<>|<|>)\s*NULL\b", sql)


@pytest.mark.parametrize("op, sql_op", [("=", "IS NULL"), ("!=", "IS NOT NULL")])
def test_post_aggregate_comparison_matches_sql(runtime, op, sql_op):
    expression = {
        "kind": "comparison",
        "op": op,
        "left": COUNT,
        "right": {"kind": "literal", "value": None},
    }
    expected = _gold(f"SELECT COUNT(*) {sql_op} FROM records")[0][0]
    assert runtime.query(_query(expression))["rows"] == [{"n": expected}]


@pytest.mark.parametrize("op, sql_op", [("=", "IS NULL"), ("!=", "IS NOT NULL")])
def test_relation_comparison_uses_null_test(op, sql_op):
    expr = parse_semantic_expression(_comparison(op), context="relation")
    assert sql_op in render_expr(_semantic_expr_to_sql(expr))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(("op", "gold_op"), [("=", "IS NULL"), ("!=", "IS NOT NULL")])
def test_lower_cased_join_on_null_matches_null_test(op, gold_op, reverse):
    comparison = {**_comparison(op, reverse=reverse), "transform": "lower"}
    condition = render_expr(_join_condition(comparison, warehouse="duckdb"))
    alias = "right" if reverse else "left"
    rows = _gold(
        f"SELECT {condition}, value {gold_op} FROM (VALUES (NULL), ('x')) AS \"{alias}\"(value)"
    )
    assert len(rows) == 2 and all(got == want for got, want in rows)


def test_post_aggregate_not_null_reads_null(runtime):
    query = {
        "version": 2,
        "select": [{"expression": COUNT, "as": "n"}, {"expression": NOT_NULL, "as": "flag"}],
    }
    ((n, flag),) = _gold("SELECT COUNT(*), NOT NULL FROM records")
    assert flag is None
    assert runtime.query(query)["rows"] == [{"n": n, "flag": flag}]


def test_configured_not_null_reads_null(runtime):
    # FALSE in place of NULL would make the null test on the negation false and count 0.
    negation_is_null = {"kind": "comparison", "op": "=", "left": NOT_NULL, "right": NULL}
    condition = {"kind": "boolean", "op": "and", "args": [negation_is_null, _comparison()]}
    query = _query({"kind": "aggregate_if", "aggregation": "count", "condition": condition})
    expected = _gold("SELECT COUNT(*) FROM records WHERE (NOT NULL) IS NULL AND value IS NULL")
    assert expected == [(6,)]
    assert runtime.query(query)["rows"] == [{"n": 6}]


def test_relation_not_null_reads_null():
    expr = parse_semantic_expression(NOT_NULL, context="relation")
    assert _gold(f"SELECT {render_expr(_semantic_expr_to_sql(expr))}") == [(None,)]


def _false_not_equal_not_null(*, reverse=False):
    false = {"kind": "literal", "value": False}
    return {
        "kind": "comparison",
        "op": "!=",
        "left": NOT_NULL if reverse else false,
        "right": false if reverse else NOT_NULL,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_relation_false_not_equal_not_null_stays_unknown(reverse):
    expr = parse_semantic_expression(_false_not_equal_not_null(reverse=reverse), context="relation")
    condition = render_expr(_semantic_expr_to_sql(expr))
    expected = _gold("SELECT FALSE != (NOT NULL) FROM records")
    assert expected == [(None,)] * 7
    assert _gold(f"SELECT {condition} FROM records") == expected
    assert (
        _gold(f"SELECT id FROM records WHERE {condition}")
        == _gold("SELECT id FROM records WHERE FALSE != (NOT NULL)")
        == []
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_configured_false_not_equal_not_null_stays_unknown(runtime, reverse):
    comparison = _false_not_equal_not_null(reverse=reverse)
    expr = parse_semantic_expression(comparison, context="config")
    config = runtime.config
    measure = next(measure for measure in config.measures if measure.id == COUNT["measure"])
    condition = render_expr(_config_expr_to_sql(expr, measure, config))
    assert _gold(f"SELECT {condition} FROM records") == [(None,)] * 7
    assert (
        _gold(f"SELECT COUNT(*) FROM records WHERE {condition}")
        == _gold("SELECT COUNT(*) FROM records WHERE FALSE != (NOT NULL)")
        == [(0,)]
    )
    expression = {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": comparison,
        "value": {"kind": "column", "entity": ENTITY, "column": "id"},
    }
    # An inline aggregate with no matching rows follows the existing empty-group rule.
    assert runtime.query(_query(expression))["rows"] == [{"n": None}]


@pytest.mark.parametrize("reverse", [False, True])
def test_post_aggregate_false_not_equal_not_null_stays_unknown(runtime, reverse):
    query = {
        "version": 2,
        "select": [
            {"expression": COUNT, "as": "n"},
            {"expression": _false_not_equal_not_null(reverse=reverse), "as": "flag"},
        ],
    }
    ((n, flag),) = _gold("SELECT COUNT(*), FALSE != (NOT NULL) FROM records")
    assert flag is None
    assert runtime.query(query)["rows"] == [{"n": n, "flag": flag}]


@pytest.mark.parametrize("lowering", ["configured", "post_aggregation", "relation"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("op", ["and", "or"])
def test_singleton_boolean_null_comparison_stays_unknown(runtime, op, reverse, lowering):
    boolean_null = {"kind": "boolean", "op": op, "args": [NULL]}
    false = {"kind": "literal", "value": False}
    comparison = {
        "kind": "comparison",
        "op": "!=",
        "left": boolean_null if reverse else false,
        "right": false if reverse else boolean_null,
    }
    context = {"configured": "config", "post_aggregation": "query", "relation": "relation"}
    expr = parse_semantic_expression(comparison, context=context[lowering])
    if lowering == "configured":
        measure = next(m for m in runtime.config.measures if m.id == COUNT["measure"])
        lowered = _config_expr_to_sql(expr, measure, runtime.config)
    elif lowering == "post_aggregation":
        lowered = _compile_post_expr(expr, runtime.config)
    else:
        lowered = _semantic_expr_to_sql(expr)
    condition = render_expr(lowered)
    # Ordinary SQL comparisons against a computed boolean NULL remain UNKNOWN.
    gold_condition = "FALSE != CAST(NULL AS BOOLEAN)"
    assert (
        _gold(f"SELECT {condition} FROM records")
        == _gold(f"SELECT {gold_condition} FROM records")
        == [(None,)] * 7
    )
    assert (
        _gold(f"SELECT id FROM records WHERE {condition}")
        == _gold(f"SELECT id FROM records WHERE {gold_condition}")
        == []
    )


@pytest.mark.parametrize(
    "op, sql_op", [("=", "IS NULL"), ("!=", "IS NOT NULL"), ("<>", "IS NOT NULL")]
)
def test_relation_field_filter_and_join_use_null_test(op, sql_op):
    assert sql_op in render_expr(_predicate({"field": "value", "op": op, "value": None}))
    comparison = _comparison(op)
    assert sql_op in render_expr(_join_condition(comparison, warehouse="duckdb"))


def test_case_condition_and_where_filter_match_sql(runtime):
    expression = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": {"kind": "literal", "value": True},
        "value": {
            "kind": "case",
            "whens": [{"when": _comparison(), "then": {"kind": "literal", "value": 1}}],
            "else": {"kind": "literal", "value": 0},
        },
    }
    expected = _gold("SELECT COUNT(*) FROM records WHERE value IS NULL")[0][0]
    assert runtime.query(_query(expression))["rows"] == [{"n": expected}]
    assert runtime.query(
        _query(COUNT, where=[{"field": "dimension.nulls_value", "op": "=", "value": None}])
    )["rows"] == [{"n": expected}]


@pytest.mark.parametrize("op", ["=", "!=", "<>", "<", "<=", ">", ">=", "LIKE", "NOT LIKE"])
@pytest.mark.parametrize("reverse", [False, True])
def test_direct_sql_binary_cannot_bypass_null_comparison_rule(op, reverse):
    column, null = SqlIdentifier(["value"]), SqlLiteral(None)
    with pytest.raises(SemanticLayerError) as exc:
        SqlBinary(null if reverse else column, op, column if reverse else null)
    assert exc.value.code == "INVALID_EXPRESSION_AST"


def test_two_nullable_columns_keep_sql_semantics():
    left, right = SqlIdentifier(["left"]), SqlIdentifier(["right"])
    assert build_comparison_condition(left, "=", right) == SqlBinary(left, "=", right)
