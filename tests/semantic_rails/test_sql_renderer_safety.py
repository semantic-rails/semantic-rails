from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.dialects import DuckDbDialect, SnowflakeDialect
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr, render_select
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCast,
    SqlDatePart,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlIn,
    SqlInterval,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlOrderTerm,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)


def _col() -> SqlIdentifier:
    return SqlIdentifier(parts=["source", "amount"])


def _unsafe_instance(cls: type[Any], **attrs: Any) -> Any:
    obj = object.__new__(cls)
    for key, value in attrs.items():
        object.__setattr__(obj, key, value)
    return obj


@pytest.mark.parametrize(
    ("token_kind", "factory"),
    [
        ("operator", lambda: SqlBinary(_col(), "= 1); DROP TABLE sensitive; --", SqlLiteral(1))),
        ("function", lambda: SqlCall("COUNT); DROP TABLE sensitive; --", [_col()])),
        (
            "join",
            lambda: SqlJoin(
                "INNER; DROP TABLE sensitive; --", SqlTableRef("safe_table"), SqlLiteral(True)
            ),
        ),
        ("sort", lambda: SqlOrder(_col(), "DESC; DROP TABLE sensitive; --")),
        ("sort", lambda: SqlOrderTerm(_col(), "ASC NULLS LAST; DROP TABLE sensitive; --")),
        ("cast", lambda: SqlCast(_col(), "TIMESTAMP); DROP TABLE sensitive; --")),
        ("date part", lambda: SqlDatePart("DAY); DROP TABLE sensitive; --")),
        ("date part", lambda: SqlInterval(SqlLiteral(1), "DAY); DROP TABLE sensitive; --")),
    ],
)
def test_sql_ast_rejects_unsafe_tokens_before_rendering(
    token_kind: str, factory: Callable[[], Any]
):
    with pytest.raises(SemanticLayerError) as exc:
        factory()

    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert exc.value.details["token_kind"] == token_kind


@pytest.mark.parametrize(
    ("token_kind", "node_factory", "renderer"),
    [
        (
            "operator",
            lambda: _unsafe_instance(
                SqlBinary, left=_col(), op="= 1); DROP TABLE sensitive; --", right=SqlLiteral(1)
            ),
            render_expr,
        ),
        (
            "function",
            lambda: _unsafe_instance(
                SqlCall, name="COUNT); DROP TABLE sensitive; --", args=[_col()], distinct=False
            ),
            render_expr,
        ),
        (
            "cast",
            lambda: _unsafe_instance(
                SqlCast, expr=_col(), type_name="TIMESTAMP); DROP TABLE sensitive; --"
            ),
            render_expr,
        ),
        (
            "sort",
            lambda: SqlWindow(
                function=SqlCall("SUM", [_col()]),
                order_by=[
                    _unsafe_instance(
                        SqlOrderTerm, expr=_col(), direction="DESC; DROP TABLE sensitive; --"
                    ),
                ],
            ),
            render_expr,
        ),
        (
            "join",
            lambda: SqlSelect(
                select=[SqlField(SqlLiteral(1), "one")],
                from_table=SqlTableRef("safe_table"),
                joins=[
                    _unsafe_instance(
                        SqlJoin,
                        join_type="INNER; DROP TABLE sensitive; --",
                        table=SqlTableRef("joined_table"),
                        on=SqlLiteral(True),
                    )
                ],
            ),
            render_select,
        ),
        (
            "sort",
            lambda: SqlSelect(
                select=[SqlField(SqlLiteral(1), "one")],
                from_table=SqlTableRef("safe_table"),
                order_by=[
                    _unsafe_instance(
                        SqlOrder,
                        expression=SqlIdentifier(parts=["one"]),
                        direction="DESC; DROP TABLE sensitive; --",
                    )
                ],
            ),
            render_select,
        ),
    ],
)
def test_renderer_revalidates_tokens_before_interpolation(
    token_kind: str,
    node_factory: Callable[[], Any],
    renderer: Callable[[Any], str],
):
    with pytest.raises(SemanticLayerError) as exc:
        renderer(node_factory())

    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert exc.value.details["token_kind"] == token_kind


@pytest.mark.parametrize(
    ("token_kind", "query"),
    [
        (
            "operator",
            {
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "where": [
                    {
                        "field": "dimension.jaffle_store_name",
                        "op": "= 1); DROP TABLE jaffle_order; --",
                        "value": "Downtown",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            },
        ),
        (
            "sort",
            {
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
                "order_by": [{"field": "orders", "direction": "DESC; DROP TABLE jaffle_order; --"}],
            },
        ),
        (
            "function",
            {
                "select": [
                    {
                        "expression": {
                            "kind": "call",
                            "name": "COALESCE); DROP TABLE jaffle_order; --",
                            "args": [
                                {"measure": "measure.jaffle.order_count"},
                                {"kind": "literal", "value": 0},
                            ],
                        },
                        "as": "orders",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            },
        ),
    ],
)
def test_query_compile_rejects_adversarial_tokens(
    package_config_factory, token_kind: str, query: dict[str, Any]
):
    config, _ = package_config_factory("jaffle_shop")

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), query)

    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert exc.value.details["token_kind"] == token_kind


def test_dialect_tokens_still_render_with_expected_duckdb_and_snowflake_sql():
    expr = _col()

    assert render_expr(DuckDbDialect().timestamp_cast(expr)) == "CAST(source.amount AS TIMESTAMP)"
    assert (
        render_expr(SnowflakeDialect().timestamp_cast(expr))
        == "CAST(source.amount AS TIMESTAMP_NTZ)"
    )
    assert (
        render_expr(DuckDbDialect().percentile_cont(expr, 0.95))
        == "QUANTILE_CONT(source.amount, 0.95)"
    )
    assert render_expr(SnowflakeDialect().percentile_cont(expr, 0.95)) == (
        "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY source.amount ASC)"
    )
    assert (
        render_expr(DuckDbDialect().date_add("day", SqlLiteral(2), expr))
        == "DATE_ADD(source.amount, INTERVAL (2) DAY)"
    )
    assert (
        render_expr(SnowflakeDialect().date_add("day", SqlLiteral(2), expr))
        == "DATEADD(DAY, 2, source.amount)"
    )


def test_in_interval_and_exists_render_with_safe_typed_sql():
    subquery = SqlSelect(
        select=[SqlField(SqlLiteral(1), "match")],
        from_table=SqlTableRef("excluded_accounts", alias="right"),
        where=[
            SqlBinary(
                SqlIdentifier(parts=["right", "account_id"]),
                "=",
                SqlIdentifier(parts=["src", "account_id"]),
            )
        ],
    )

    assert (
        render_expr(SqlIn(_col(), [SqlLiteral("sms"), SqlLiteral("email")]))
        == "(source.amount IN ('sms', 'email'))"
    )
    assert (
        render_expr(SqlIn(_col(), [SqlLiteral("sms")], negated=True))
        == "(source.amount NOT IN ('sms'))"
    )
    assert render_expr(SqlInterval(SqlLiteral(7), "day")) == "INTERVAL (7) DAY"
    rendered_exists = render_expr(SqlExists(subquery, negated=True))
    assert rendered_exists.startswith(
        'NOT EXISTS (\nSELECT\n  1 AS match\nFROM excluded_accounts AS "right"'
    )
    # Top-level SqlBinary inside a WHERE clause renders without redundant parens.
    assert '"right".account_id = src.account_id' in rendered_exists


# --- string-literal escaping across dialects -------------------------------
#
# Doubling the single quote is enough on standard-conforming warehouses, but
# Snowflake, BigQuery, ClickHouse and Databricks/Spark also honour backslash
# escapes inside single-quoted literals. On those, a value ending in a
# backslash escapes the closing quote and everything after it parses as SQL.

_BACKSLASH_ESCAPING_WAREHOUSES = ("snowflake", "bigquery", "clickhouse", "databricks")
_STANDARD_CONFORMING_WAREHOUSES = (
    "duckdb",
    "motherduck",
    "ducklake",
    "postgres",
    "athena",
)

_HOSTILE_VALUES = (
    "\\",
    "\\' OR 1=1 --",
    "ok\\",
    "a'b",
    "a\\'b",
    "\\\\",
    "'; DROP TABLE sensitive; --",
)


def _closes_exactly_once(rendered: str, *, backslash_escapes: bool) -> bool:
    """Walk `rendered` the way the target warehouse's lexer would.

    Returns True when the literal terminates at the final character and
    nowhere earlier — i.e. nothing the caller supplied escaped into SQL.
    """
    assert rendered.startswith("'") and rendered.endswith("'")
    i = 1
    end = len(rendered) - 1
    while i < end:
        ch = rendered[i]
        if backslash_escapes and ch == "\\":
            i += 2  # consumes whatever follows, including a quote
            continue
        if ch == "'":
            if i + 1 < end + 1 and rendered[i + 1] == "'":
                i += 2
                continue
            return False  # closed early: the rest is live SQL
        i += 1
    return i == end


@pytest.mark.parametrize("warehouse", _BACKSLASH_ESCAPING_WAREHOUSES)
@pytest.mark.parametrize("value", _HOSTILE_VALUES)
def test_backslash_dialects_neutralize_hostile_literals(warehouse: str, value: str):
    from semantic_rails.dialects import dialect_for_warehouse

    rendered = dialect_for_warehouse(warehouse).quote_string_literal(value)
    assert _closes_exactly_once(rendered, backslash_escapes=True), (
        f"{warehouse}: {value!r} -> {rendered} escaped the literal"
    )


@pytest.mark.parametrize("warehouse", _STANDARD_CONFORMING_WAREHOUSES)
@pytest.mark.parametrize("value", _HOSTILE_VALUES)
def test_standard_dialects_neutralize_hostile_literals(warehouse: str, value: str):
    from semantic_rails.dialects import dialect_for_warehouse

    rendered = dialect_for_warehouse(warehouse).quote_string_literal(value)
    assert _closes_exactly_once(rendered, backslash_escapes=False), (
        f"{warehouse}: {value!r} -> {rendered} escaped the literal"
    )


@pytest.mark.parametrize("warehouse", _STANDARD_CONFORMING_WAREHOUSES)
def test_standard_dialects_do_not_corrupt_backslashes(warehouse: str):
    """Doubling backslashes here would silently change the user's data."""
    from semantic_rails.dialects import dialect_for_warehouse

    assert dialect_for_warehouse(warehouse).quote_string_literal("C:\\tmp") == "'C:\\tmp'"


def test_renderer_uses_the_bound_dialect_for_literals():
    from semantic_rails.dialects import DuckDbDialect, SnowflakeDialect
    from semantic_rails.renderer import use_dialect

    literal = SqlLiteral("ok\\")
    assert render_expr(literal) == "'ok\\'"  # unbound: standard-conforming
    with use_dialect(DuckDbDialect()):
        assert render_expr(literal) == "'ok\\'"
    with use_dialect(SnowflakeDialect()):
        assert render_expr(literal) == "'ok\\\\'"
    # The binding is scoped, not sticky.
    assert render_expr(literal) == "'ok\\'"


def test_compile_binds_the_warehouse_dialect_for_literal_quoting(package_config_factory):
    """Guards the `_render_string_literal` fallback from going unnoticed.

    `render_expr` outside a binding escapes ANSI-style, which would
    under-escape a Snowflake query. Compilation must therefore always bind
    the package's dialect — assert it end to end rather than trusting the
    single call site to stay wired.
    """
    import semantic_rails.compiler as compiler_module

    seen: list[Any] = []
    original = compiler_module.render_select_for_profile

    def _spy(query: Any, profile: Any = None, **kwargs: Any) -> str:
        seen.append(kwargs.get("dialect"))
        return original(query, profile, **kwargs)

    config, _ = package_config_factory("jaffle_shop")
    registry = Registry(config)
    compiler_module.render_select_for_profile = _spy
    try:
        compile_query(
            config,
            registry,
            {
                "select": [
                    {
                        "expression": {"kind": "metric", "metric": "metric.sales.aov_usd"},
                        "alias": "aov",
                    }
                ]
            },
        )
    finally:
        compiler_module.render_select_for_profile = original

    assert seen and seen[0] is not None, "compile_query must bind a dialect for rendering"
    assert seen[0].name == config.package.warehouse
