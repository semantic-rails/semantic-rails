from __future__ import annotations

from dataclasses import replace
from datetime import date

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query, plan_query
from semantic_rails.compiler_parts.bind import _config_expr_to_sql
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, validate_runtime_package
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    CallExpr,
    ColumnRefExpr,
    LiteralExpr,
    accepted_call_names,
    parse_semantic_expression,
    validate_expression_calls,
)
from semantic_rails.package_tools import check_package_report
from semantic_rails.relation_pipelines import _semantic_expr_to_sql
from semantic_rails.renderer import render_expr
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlCast, SqlLiteral

WAREHOUSES = ["duckdb", "postgres", "snowflake", "bigquery", "databricks", "clickhouse", "athena"]


def literal(value):
    return {"kind": "literal", "value": value}


def call(name, *args):
    return {"kind": "call", "name": name, "args": list(args)}


def cast(value, target="DOUBLE"):
    return call("CAST", value, literal(target))


def column(name):
    return {"kind": "column", "column": name, "entity": "entity.numbers_row"}


def maximum(value):
    return {
        "kind": "aggregate_if",
        "aggregation": "max",
        "condition": {
            "kind": "comparison",
            "op": ">",
            "left": column("amount"),
            "right": literal(0),
        },
        "value": value,
    }


def query(expression):
    return {"select": [{"expression": expression, "as": "v"}]}


@pytest.fixture
def package(tmp_path):
    files = {
        "package.yml": "schema_version: 1\npackage: {id: numbers, namespace: numbers, warehouse: duckdb, default_db: data/db.duckdb, seed: {kind: sql_script, source: data/seed.sql}}\n",
        "graph.yml": "graph: {entities: {row: {key: [id], model: rows}}}\n",
        "models/rows.yml": """model:
  id: rows
  relation: numbers
  entities: {row: {}}
  dimensions:
    text_value: {column: text_value, kind: string}
    amount: {column: amount, kind: number}
  measures:
    amount: {kind: aggregate, expr: amount, accumulation: {kind: flow}}
""",
        "data/seed.sql": "CREATE TABLE numbers(id INTEGER, text_value VARCHAR, amount DOUBLE, unknown_value VARCHAR); INSERT INTO numbers VALUES (1, '9.5', 9.5, 'bad'), (2, '120.25', 120.25, 'bad'), (3, '64.0', 64.0, 'bad'), (4, NULL, NULL, 'bad');",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def execute(config, expression):
    sql = compile_query(config, None, query(expression))["sql"]
    with duckdb.connect() as conn:
        conn.execute(
            "CREATE TABLE numbers(id INTEGER, text_value VARCHAR, amount DOUBLE, unknown_value VARCHAR)"
        )
        conn.execute(
            "INSERT INTO numbers VALUES (1, '9.5', 9.5, 'bad'), (2, '120.25', 120.25, 'bad'), (3, '64.0', 64.0, 'bad'), (4, NULL, NULL, 'bad')"
        )
        return conn.execute(sql).fetchall()


def test_numeric_cast_max_and_post_aggregation_subtraction(package):
    config = load_package_config(str(package))
    assert execute(config, maximum(column("text_value"))) == [("9.5",)]
    assert execute(config, maximum(cast(column("text_value")))) == [(120.25,)]
    subtraction = {
        "kind": "arithmetic",
        "op": "subtract",
        "left": cast(maximum(column("text_value"))),
        "right": cast(literal("2.5")),
    }
    assert execute(config, subtraction) == [(7.0,)]
    assert execute(config, maximum(cast(literal(None)))) == [(None,)]


@pytest.mark.parametrize("warehouse", WAREHOUSES)
@pytest.mark.parametrize(
    "target",
    ["DOUBLE", "INTEGER", "BIGINT", "DECIMAL(12,3)", "DECIMAL(38,10)", "DECIMAL(38,0)", "VARCHAR"],
)
def test_cast_sql_for_every_dialect(package, warehouse, target):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    if warehouse == "bigquery" and target.startswith("DECIMAL"):
        with pytest.raises(SemanticLayerError) as exc:
            compile_query(config, None, query(maximum(cast(column("text_value"), target))))
        assert exc.value.code == "INVALID_EXPRESSION_AST"
        with pytest.raises(SemanticLayerError) as exc:
            dialect_for_warehouse(warehouse).scalar_call(
                "CAST", [SqlLiteral(1), SqlLiteral(target)]
            )
        assert exc.value.code == "INVALID_EXPRESSION_AST"
        return
    sql = compile_query(config, None, query(maximum(cast(column("text_value"), target))))["sql"]
    expected = "BIGINT" if target in {"INTEGER", "BIGINT"} else target
    if warehouse == "postgres" and target == "DOUBLE":
        expected = "FLOAT8"
    elif warehouse == "bigquery":
        expected = {
            "DOUBLE": "FLOAT64",
            "INTEGER": "INT64",
            "BIGINT": "INT64",
            "VARCHAR": "STRING",
        }[target]
    elif warehouse == "databricks" and target == "VARCHAR":
        expected = "STRING"
    elif warehouse == "clickhouse":
        expected = (
            "Nullable("
            + {"DOUBLE": "Float64", "INTEGER": "Int64", "BIGINT": "Int64", "VARCHAR": "String"}.get(
                target, target
            )
            + ")"
        )
    lowered = dialect_for_warehouse(warehouse).scalar_call(
        "CAST", [SqlLiteral(1), SqlLiteral(target)]
    )
    assert lowered.type_name == expected
    assert render_expr(lowered) == f"CAST(1 AS {expected})"
    assert f" AS {lowered.type_name})" in sql


@pytest.mark.parametrize(
    "target",
    [
        "DATE",
        "TIMESTAMP",
        "BOOLEAN",
        "FLOAT",
        "DOUBLE PRECISION",
        "DECIMAL",
        "DECIMAL(0,0)",
        "DECIMAL(2,3)",
        "DECIMAL(39,0)",
        "DOUBLE); SELECT 1",
        12,
        None,
    ],
)
def test_refused_cast_types_list_accepted_forms(package, target):
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(load_package_config(str(package)), None, query(cast(literal(1), target)))
    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert "DOUBLE, DECIMAL(p,s), INTEGER, BIGINT, VARCHAR" in str(exc.value)


@pytest.mark.parametrize(
    "args",
    [
        [],
        [literal(1)],
        [literal(1), column("text_value")],
        [literal(1), literal("DOUBLE"), literal(2)],
    ],
)
def test_refused_cast_shapes(package, args):
    with pytest.raises(SemanticLayerError, match="string literal type"):
        validate_expression_calls(
            parse_semantic_expression(call("CAST", *args), context="query"),
            load_package_config(str(package)),
        )


def test_cast_case_and_invalid_value(package):
    config = load_package_config(str(package))
    assert execute(config, maximum(call("cast", literal("120.25"), literal(" double ")))) == [
        (120.25,)
    ]
    runtime = Runtime.from_path(str(package))
    try:
        with pytest.raises(SemanticLayerError) as exc:
            runtime.query(query(maximum(cast(column("unknown_value")))))
        assert exc.value.code == "QUERY_EXECUTION_ERROR"
        assert "bad" not in str(exc.value) + str(exc.value.details)
    finally:
        runtime.close()


SMOKE_ARGS = {
    "CAST": ["120.25", "DOUBLE"],
    "COALESCE": [None, 1],
    "NULLIF": [1, 0],
    "CONCAT": ["a", "b"],
    "POWER": [2, 3],
    "REPLACE": ["abc", "a", "z"],
    "SUBSTR": ["abc", 1, 2],
    "SUBSTRING": ["abc", 1, 2],
    "LEFT": ["abc", 1],
    "RIGHT": ["abc", 1],
    "DATE_PART": ["year", date(2020, 1, 1)],
    "DATE_TRUNC": ["year", date(2020, 1, 1)],
    "JSON_EXTRACT": ['{"a":1}', "$.a"],
    "JSON_EXTRACT_STRING": ['{"a":"x"}', "$.a"],
    "SPLIT": ["a,b", ","],
    "STRING_SPLIT": ["a,b", ","],
    "STR_SPLIT": ["a,b", ","],
}


@pytest.mark.parametrize("warehouse", WAREHOUSES)
def test_allowed_names_are_constructible_and_duckdb_executes_each(package, warehouse):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    dialect = dialect_for_warehouse(warehouse)
    for name in sorted(accepted_call_names(warehouse)):
        values = SMOKE_ARGS.get(
            name, ["abc"] if name in {"LOWER", "UPPER", "LENGTH", "TRIM"} else [2]
        )
        expr = parse_semantic_expression(
            call(name.lower(), *(literal(v) for v in values)), context="query"
        )
        validate_expression_calls(expr, config)
        compile_query(config, None, query(maximum(call(name, *(literal(v) for v in values)))))
        sql_args = [SqlLiteral(v) for v in values]
        if name in {"DATE_PART", "DATE_TRUNC"}:
            sql_args[1] = SqlCast(sql_args[1], "DATE")
        sql_expr = dialect.scalar_call(expr.name, sql_args)
        if warehouse == "duckdb":
            with duckdb.connect() as conn:
                assert len(conn.execute("SELECT " + render_expr(sql_expr)).fetchall()) == 1


@pytest.mark.parametrize("warehouse", WAREHOUSES)
@pytest.mark.parametrize(
    "name",
    [
        "SUM",
        "COUNT",
        "AVG",
        "ROW_NUMBER",
        "LAG",
        "GENERATE_SERIES",
        "UNNEST",
        "SEQUENCE",
        "EXPLODE",
        "TRY_CAST",
        "CONVERT_TIMEZONE",
    ],
)
def test_excluded_calls_report_exact_dialect_list(package, warehouse, name):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(config, None, query(call(name, literal(1))))
    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert exc.value.details["allowed"] == sorted(accepted_call_names(warehouse))


@pytest.mark.parametrize(
    "expression,function,index,expected,received",
    [
        (maximum(call("ROUND", column("text_value"), literal(1))), "ROUND", 0, "number", "text"),
        (call("DATE_PART", literal("year"), literal("2020-01-01")), "DATE_PART", 1, "date", "text"),
        (maximum(call("UPPER", column("amount"))), "UPPER", 0, "text", "number"),
        (maximum(call("ROUND", call("LOWER", column("text_value")))), "ROUND", 0, "number", "text"),
        (maximum(call("ROUND", cast(column("amount"), "VARCHAR"))), "ROUND", 0, "number", "text"),
    ],
)
def test_known_type_errors_before_planning(
    package, expression, function, index, expected, received
):
    config = load_package_config(str(package))
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(config, None, query(expression))
    assert exc.value.code == "CALL_ARGUMENT_TYPE"
    assert exc.value.details == {
        "function": function,
        "argument_index": index,
        "expected": expected,
        "received": received,
    }
    hints = recovery_hints_for_error(exc.value.code, exc.value.details)
    if expected == "number" and received == "text":
        assert "CAST" in hints[0]["message"]


def test_numeric_column_and_cast_pass_but_unknown_reaches_warehouse(package):
    config = load_package_config(str(package))
    assert execute(config, maximum(call("ROUND", column("amount"), literal(1)))) == [(120.3,)]
    assert execute(config, maximum(call("ROUND", cast(column("text_value")), literal(1)))) == [
        (120.3,)
    ]
    unknown = maximum(call("ROUND", column("unknown_value"), literal(1)))
    compile_query(config, None, query(unknown))
    with pytest.raises(duckdb.Error):
        execute(config, unknown)


@pytest.mark.parametrize(
    "expression",
    [
        literal(6),
        {"kind": "arithmetic", "op": "multiply", "left": literal(2), "right": literal(3)},
        cast(literal("6")),
    ],
)
def test_literal_only_select_has_precise_reason(package, expression):
    with pytest.raises(SemanticLayerError) as exc:
        plan_query(load_package_config(str(package)), None, query(expression))
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details == {"reason": "literal_only_select"}
    assert "add a measure, a group_by dimension or time" in str(exc.value)


def test_package_cast_and_package_type_error_fails_check(package):
    path = package / "models/rows.yml"
    model = yaml.safe_load(path.read_text())
    model["model"]["measures"]["amount"]["expr"] = cast({"kind": "column", "column": "text_value"})
    path.write_text(yaml.safe_dump(model))
    config = load_package_config(str(package))
    assert execute(config, {"measure": "measure.numbers.amount", "aggregation": "max"}) == [
        (120.25,)
    ]
    # Relation projections share the dialect CAST path too.
    assert render_expr(
        _semantic_expr_to_sql(config.measures[0].expr, default_alias="n", warehouse="postgres")
    ).endswith(" AS FLOAT8)")
    model["model"]["measures"]["amount"]["expr"] = call(
        "ROUND", {"kind": "column", "column": "text_value"}, literal(1)
    )
    path.write_text(yaml.safe_dump(model))
    errors = validate_runtime_package(package)
    assert errors and all(isinstance(error, str) for error in errors)
    report = check_package_report(PackageReference(source_path=str(package)))
    assert not report["ok"]
    assert report["blockers"][0]["code"] == "CALL_ARGUMENT_TYPE"
    assert report["blockers"][0]["details"]["function"] == "ROUND"
    assert [error["code"] for error in report["blockers"]] == [
        "CALL_ARGUMENT_TYPE",
        "INVALID_CONFIG",
        "INVALID_CONFIG",
    ]
    assert "package.id must equal directory name" in report["blockers"][1]["message"]
    assert "dimension rows.text_value has unknown kind 'string'" in report["blockers"][2]["message"]
    assert not any(
        "failed to load package config" in error["message"] for error in report["blockers"]
    )


@pytest.mark.parametrize("parameter", ["precision", "scale"])
def test_oversized_decimal_parameters_are_structured_errors(package, parameter):
    digits = "9" * 5000
    target = f"DECIMAL({digits},0)" if parameter == "precision" else f"DECIMAL(38,{digits})"
    runtime = Runtime.from_path(str(package))
    try:
        payload = query(maximum(cast(column("text_value"), target)))
        report = runtime.validate(payload)
        assert not report["ok"]
        assert report["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
        with pytest.raises(SemanticLayerError) as exc:
            plan_query(runtime.config, None, payload)
        assert exc.value.code == "INVALID_EXPRESSION_AST"
    finally:
        runtime.close()


@pytest.mark.parametrize("target", ["DECIMAL(12,3)", "DECIMAL(38,10)", "DECIMAL(38,0)"])
def test_bigquery_decimal_constraints_refused_at_load_and_validation(package, target):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse="bigquery"))
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        report = runtime.validate(query(maximum(cast(column("text_value"), target))))
        assert not report["ok"]
        assert report["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
    finally:
        runtime.close()
    path = package / "package.yml"
    raw = yaml.safe_load(path.read_text())
    raw["package"]["warehouse"] = "bigquery"
    raw["package"]["connection"] = {"kind": "bigquery_native", "project": "test"}
    path.write_text(yaml.safe_dump(raw))
    path = package / "models/rows.yml"
    raw = yaml.safe_load(path.read_text())
    raw["model"]["measures"]["amount"]["expr"] = cast(
        {"kind": "column", "column": "text_value"}, target
    )
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package))
    assert exc.value.code == "INVALID_EXPRESSION_AST"


@pytest.mark.parametrize(
    "warehouse,expression,expected",
    [
        (warehouse, call("LENGTH", call("SPLIT", literal("a,b"), literal(","))), 2)
        for warehouse in ["duckdb", "motherduck", "ducklake"]
    ]
    + [
        (
            "snowflake",
            call(
                "ROUND", cast(literal("2.5"), "DECIMAL(10,1)"), literal(0), literal("HALF_TO_EVEN")
            ),
            None,
        ),
        ("snowflake", call("ROUND", column("text_value"), literal(0)), None),
        ("databricks", call("UPPER", column("amount")), None),
        ("clickhouse", call("LENGTH", literal([1, 2])), None),
        ("postgres", call("ROUND", cast(literal("2.5"), "DECIMAL(10,1)"), literal("0")), None),
    ],
)
def test_supported_overloads_compile_in_queries_and_packages(
    package, warehouse, expression, expected
):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    assert compile_query(config, None, query(maximum(expression)))["sql"]
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        assert runtime.validate(query(maximum(expression)))["ok"]
    finally:
        runtime.close()
    if warehouse == "duckdb":
        assert execute(config, maximum(expression)) == [(expected,)]
    path = package / "models/rows.yml"
    raw = yaml.safe_load(path.read_text())
    raw["model"]["measures"]["amount"]["expr"] = expression
    path.write_text(yaml.safe_dump(raw))
    path = package / "package.yml"
    raw = yaml.safe_load(path.read_text())
    raw["package"]["warehouse"] = warehouse
    if warehouse == "snowflake":
        raw["package"]["connection"] = {"kind": "snowflake_cli", "name": "test"}
    elif warehouse != "duckdb":
        raw["package"]["connection"] = {"kind": f"{warehouse}_native"}
    path.write_text(yaml.safe_dump(raw))
    config = load_package_config(str(package))
    assert compile_query(config, None, query({"measure": "measure.numbers.amount"}))["sql"]


def test_direct_lowering_cannot_bypass_call_guard(package):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse="bigquery"))
    expr = CallExpr("DATE_PART", [LiteralExpr("year"), LiteralExpr(None)])
    with pytest.raises(SemanticLayerError) as exc:
        _config_expr_to_sql(expr, config.measures[0], config)
    assert exc.value.details["allowed"] == sorted(accepted_call_names("bigquery"))


@pytest.mark.parametrize(
    "warehouse,name",
    [
        ("postgres", "SPLIT"),
        ("bigquery", "DATE_PART"),
        ("snowflake", "JSON_EXTRACT"),
        ("clickhouse", "TRIM"),
    ],
)
def test_dialect_spelling_is_refused_by_walker(package, warehouse, name):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    with pytest.raises(SemanticLayerError) as exc:
        validate_expression_calls(
            parse_semantic_expression(call(name, literal(1)), context="config"), config
        )
    assert exc.value.details["allowed"] == sorted(accepted_call_names(warehouse))


@pytest.mark.parametrize(
    "expression",
    [
        maximum(call("ROUND", column("text_value"), literal(1))),
        call("DATE_PART", literal("year"), literal("2020-01-01")),
    ],
)
def test_runtime_validation_compile_and_execution_agree(package, expression):
    runtime = Runtime.from_path(str(package))
    try:
        report = runtime.validate(query(expression))
        assert not report["ok"]
        assert report["errors"][0]["code"] == "CALL_ARGUMENT_TYPE"
        for operation in [runtime.compile, runtime.query]:
            with pytest.raises(SemanticLayerError) as exc:
                operation(query(expression))
            assert exc.value.code == "CALL_ARGUMENT_TYPE"
    finally:
        runtime.close()


@pytest.mark.parametrize("warehouse", ["DuckDB", "Snowflake"])
@pytest.mark.parametrize(
    "expression",
    [
        {
            "kind": "nullif",
            "value": {"kind": "column", "column": "amount"},
            "null_value": literal(0),
        },
        call("ABS", {"kind": "column", "column": "amount"}),
    ],
)
def test_mixed_case_warehouse_loads_and_compiles_calls(package, warehouse, expression):
    path = package / "package.yml"
    raw = yaml.safe_load(path.read_text())
    raw["package"]["warehouse"] = warehouse
    if warehouse == "Snowflake":
        raw["package"]["connection"] = {"kind": "snowflake_cli", "name": "test"}
    path.write_text(yaml.safe_dump(raw))
    path = package / "models/rows.yml"
    raw = yaml.safe_load(path.read_text())
    raw["model"]["measures"]["amount"]["expr"] = expression
    path.write_text(yaml.safe_dump(raw))
    config = load_package_config(str(package))
    assert config.package.warehouse == warehouse.lower()
    assert compile_query(config, None, query({"measure": "measure.numbers.amount"}))["sql"]


@pytest.mark.parametrize("kind", [None, "categorical"])
def test_untyped_integer_dimension_in_query_and_package_measure(package, kind):
    path = package / "models/rows.yml"
    raw = yaml.safe_load(path.read_text())
    raw["model"]["dimensions"]["id"] = {"column": "id"}
    if kind:
        raw["model"]["dimensions"]["id"]["kind"] = kind
    raw["model"]["measures"]["amount"]["expr"] = call("ABS", {"kind": "column", "column": "id"})
    path.write_text(yaml.safe_dump(raw))
    config = load_package_config(str(package))
    assert execute(config, maximum(call("ABS", column("id")))) == [(3,)]
    assert execute(config, {"measure": "measure.numbers.amount", "aggregation": "max"}) == [(4,)]


@pytest.mark.parametrize(
    "expression,expected",
    [
        (call("CONCAT", literal("order-"), literal(1)), "order-1"),
        (
            call(
                "CONCAT", literal("Q"), call("DATE_PART", literal("quarter"), column("placed_at"))
            ),
            "Q1",
        ),
        (call("JSON_EXTRACT", literal('{"a":1}'), literal(["$.a"])), ["1"]),
        (call("JSON_EXTRACT_STRING", literal('{"a":1}'), literal(["$.a"])), ["1"]),
    ],
)
def test_warehouse_overloads_execute(package, expression, expected):
    config = load_package_config(str(package))
    sql = compile_query(config, None, query(maximum(expression)))["sql"]
    with duckdb.connect() as conn:
        conn.execute("CREATE TABLE numbers(id INTEGER, amount INTEGER, placed_at DATE)")
        conn.execute("INSERT INTO numbers VALUES (1, 1, DATE '2020-01-01')")
        if expression["name"] in {"JSON_EXTRACT", "JSON_EXTRACT_STRING"}:
            # Array literal rendering is separate from overload type validation.
            values = [arg["value"] for arg in expression["args"]]
            assert conn.execute(f"SELECT {expression['name']}(?, ?)", values).fetchall() == [
                (expected,)
            ]
        else:
            assert conn.execute(sql).fetchall() == [(expected,)]


def test_operational_expression_shaped_metadata_stays_data(package):
    path = package / "package.yml"
    raw = yaml.safe_load(path.read_text())
    raw["defaults"] = {
        "operational": {
            "measure": {
                "fields": {
                    "kind": {"type": "string"},
                    "owner": {"type": "string"},
                }
            }
        }
    }
    raw["defaults"]["operational"]["metric"] = raw["defaults"]["operational"]["measure"]
    path.write_text(yaml.safe_dump(raw))
    path = package / "models/rows.yml"
    raw = yaml.safe_load(path.read_text())
    raw["model"]["operational_defaults"] = {"kind": "literal", "owner": "finance"}
    path.write_text(yaml.safe_dump(raw))
    config = load_package_config(str(package))
    assert config.measures[0].operational == {"kind": "literal", "owner": "finance"}


def test_unowned_projection_column_does_not_borrow_dimension_type(package):
    config = load_package_config(str(package))
    expr = CallExpr("ABS", [ColumnRefExpr("text_value", table="src")])
    validate_expression_calls(expr, config, owner="entity.numbers_row")
    assert (
        render_expr(_semantic_expr_to_sql(expr, default_alias="src", warehouse="duckdb"))
        == "ABS(src.text_value)"
    )


def test_distribution_constant_branch_uses_internal_root_diagnostic(package):
    config = load_package_config(str(package))
    distribution = {
        "kind": "distribution",
        "function": "avg",
        "over": {
            "kind": "entity_value",
            "entity": "entity.numbers_row",
            "input": {"measure": "measure.numbers.amount"},
        },
    }
    payload = {
        "select": [
            {"expression": distribution, "as": "avg_amount"},
            {"expression": literal(6), "as": "constant"},
        ]
    }
    plan = plan_query(config, None, payload)
    assert plan.post_aggregation_exprs["constant"] == literal(6)
    # The existing branch planner still requires a grouping/time root for a constant.
    # It must reach that planner rather than the top-level literal-only guard.
    with pytest.raises(
        SemanticLayerError, match="Distinct-values queries require group_by or time"
    ) as exc:
        compile_query(config, None, payload)
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details.get("reason") != "literal_only_select"
