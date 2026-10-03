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
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    CONVERSION_WINDOW_UNITS,
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
from semantic_rails.sql_ast import SqlCast, SqlIdentifier, SqlLiteral

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
    "DATE_DIFF": ["day", date(2020, 1, 1), date(2020, 1, 3)],
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
        if warehouse == "athena" and name == "DATE_DIFF":
            continue  # Registered shape, but every unit is covered by the refusal tests.
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


@pytest.mark.parametrize("warehouse", WAREHOUSES)
@pytest.mark.parametrize(
    "expression",
    [
        call("ROUND", column("text_value"), literal(1)),
        call("UPPER", column("amount")),
        call("ROUND", call("LOWER", column("text_value"))),
        call("ROUND", cast(column("amount"), "VARCHAR")),
    ],
)
def test_argument_types_are_deferred_to_warehouse(package, warehouse, expression):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    assert compile_query(config, None, query(maximum(expression)))["sql"]


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


def test_package_cast_and_call_preserve_other_check_errors(package):
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
    assert {error["code"] for error in report["blockers"]} == {"INVALID_CONFIG"}
    assert any(
        "package.id must equal directory name" in error["message"] for error in report["blockers"]
    )
    assert any(
        "dimension rows.text_value has unknown kind 'string'" in error["message"]
        for error in report["blockers"]
    )
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
    ]
    + [
        (warehouse, expression, expected)
        for expression, expected in [
            (
                call(
                    "DATE_PART",
                    call("SPLIT", literal("year,month"), literal(",")),
                    column("placed_at"),
                ),
                {"year": 2020, "month": 1},
            ),
            (call("SUBSTRING", literal("Thomas"), cast(literal("...$"), "VARCHAR")), None),
            (
                call(
                    "DATE_TRUNC",
                    literal("day"),
                    column("placed_at"),
                    cast(literal("UTC"), "VARCHAR"),
                ),
                None,
            ),
        ]
        for warehouse in WAREHOUSES
        if expression["name"] in accepted_call_names(warehouse)
        and (expression["name"] != "DATE_PART" or "SPLIT" in accepted_call_names(warehouse))
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
    if warehouse == "duckdb" and expected is not None:
        if expression["name"] == "DATE_PART":
            sql = compile_query(config, None, query(maximum(expression)))["sql"]
            with duckdb.connect() as conn:
                conn.execute("CREATE TABLE numbers(amount INTEGER, placed_at TIMESTAMP)")
                conn.execute("INSERT INTO numbers VALUES (1, TIMESTAMP '2020-01-01')")
                assert conn.execute(sql).fetchall() == [(expected,)]
        else:
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
    raw["relations"] = {
        "projected": {
            "source": "numbers",
            "columns": ["id", "amount", "text_value", "placed_at"],
            "steps": [{"select": {"columns": {"id": "id", "projected_value": expression}}}],
        }
    }
    path.write_text(yaml.safe_dump(raw))
    path = package / "models/rows.yml"
    raw = yaml.safe_load(path.read_text())
    raw["model"]["relation"] = "projected"
    raw["model"]["measures"]["amount"]["expr"] = "projected_value"
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


@pytest.mark.parametrize("path", ["select", "measure", "relation"])
def test_warehouse_type_error_is_redacted_on_every_call_path(package, path):
    expression = call("ROUND", literal("private-value"))
    payload = query(maximum(expression))
    if path != "select":
        model_path = package / "models/rows.yml"
        raw = yaml.safe_load(model_path.read_text())
        raw["model"]["measures"]["amount"]["expr"] = expression
        if path == "relation":
            raw["model"]["relation"] = "projected"
            raw["model"]["measures"]["amount"]["expr"] = "amount"
            package_path = package / "package.yml"
            metadata = yaml.safe_load(package_path.read_text())
            metadata["relations"] = {
                "projected": {
                    "source": "numbers",
                    "columns": ["id", "text_value", "amount"],
                    "steps": [
                        {
                            "select": {
                                "columns": {
                                    "id": "id",
                                    "text_value": "text_value",
                                    "amount": expression,
                                }
                            }
                        }
                    ],
                }
            }
            package_path.write_text(yaml.safe_dump(metadata))
        model_path.write_text(yaml.safe_dump(raw))
        payload = query({"measure": "measure.numbers.amount", "aggregation": "max"})
    runtime = Runtime.from_path(str(package))
    try:
        assert runtime.validate(payload)["ok"]
        assert runtime.compile(payload)["ok"]
        assert "ROUND('private-value')" in compile_query(runtime.config, None, payload)["sql"]
        with pytest.raises(SemanticLayerError) as exc:
            runtime.query(payload)
        assert exc.value.code == "QUERY_EXECUTION_ERROR"
        assert exc.value.details["sql_redacted"] is True
        assert "private-value" not in str(exc.value) + str(exc.value.details)
        assert "Binder" not in str(exc.value) + str(exc.value.details)
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
    validate_expression_calls(expr, config)
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


DATE_DIFF_WAREHOUSES = [*WAREHOUSES, "motherduck", "ducklake"]
START = "2024-01-01 23:00:00"
END = "2024-01-03 01:00:00"


UNSUPPORTED_DATE_DIFF = [
    *[("athena", unit) for unit in CONVERSION_WINDOW_UNITS],
    *[(warehouse, "week") for warehouse in ["snowflake", "bigquery", "clickhouse"]],
]


@pytest.mark.parametrize("warehouse,unit", UNSUPPORTED_DATE_DIFF)
@pytest.mark.parametrize("surface", ["select", "package", "aggregate_if", "relation"])
def test_unsupported_date_diff_refused_on_every_surface(package, warehouse, unit, surface):
    expression = call("date_diff", literal(unit.upper()), literal(START), literal(END))
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    parsed = parse_semantic_expression(expression, context="query")
    with pytest.raises(SemanticLayerError) as validation:
        validate_expression_calls(parsed, config)
    with pytest.raises(SemanticLayerError) as lowering:
        if surface == "package":
            path = package / "package.yml"
            manifest = yaml.safe_load(path.read_text())
            manifest["package"]["warehouse"] = warehouse
            manifest["package"]["connection"] = {"kind": f"{warehouse}_native", "name": "test"}
            path.write_text(yaml.safe_dump(manifest))
            path = package / "models/rows.yml"
            model = yaml.safe_load(path.read_text())
            model["model"]["measures"]["amount"]["expr"] = expression
            path.write_text(yaml.safe_dump(model))
            load_package_config(str(package))
        elif surface == "relation":
            _semantic_expr_to_sql(parsed, warehouse=warehouse)
        elif surface == "select":
            compile_query(
                config,
                None,
                query(
                    call("DATE_DIFF", literal(unit), maximum(literal(START)), maximum(literal(END)))
                ),
            )
        else:
            compile_query(config, None, query(maximum(expression)))
    assert validation.value.code == lowering.value.code == "INVALID_EXPRESSION_AST"
    assert str(validation.value) == str(lowering.value)
    assert "Unsupported DATE_DIFF" in str(lowering.value)
    assert warehouse in str(lowering.value)
    assert unit in str(lowering.value)
    assert lowering.value.details == {"function": "DATE_DIFF", "warehouse": warehouse, "unit": unit}


@pytest.mark.parametrize("warehouse,unit", UNSUPPORTED_DATE_DIFF)
def test_unsupported_date_diff_scalar_guard_refuses_direct_lowering(warehouse, unit):
    with pytest.raises(SemanticLayerError, match="Unsupported DATE_DIFF") as exc:
        dialect_for_warehouse(warehouse).scalar_call(
            "DATE_DIFF", [SqlLiteral(unit), SqlLiteral(START), SqlLiteral(END)]
        )
    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert exc.value.details["warehouse"] == warehouse
    assert exc.value.details["unit"] == unit


@pytest.mark.parametrize("unit", [unit for unit in CONVERSION_WINDOW_UNITS if unit != "week"])
@pytest.mark.parametrize("start,end", [(None, END), (START, None), (None, None), (START, END)])
def test_clickhouse_date_diff_casts_both_endpoints_to_nullable_timestamps(unit, start, end):
    lowered = dialect_for_warehouse("clickhouse").scalar_call(
        "DATE_DIFF", [SqlLiteral(unit), SqlLiteral(start), SqlLiteral(end)]
    )
    assert render_expr(lowered) == (
        f"DATE_DIFF('{unit}', CAST({render_expr(SqlLiteral(start))} AS Nullable(DateTime)), "
        f"CAST({render_expr(SqlLiteral(end))} AS Nullable(DateTime)))"
    )


@pytest.mark.parametrize(
    "warehouse,unit",
    [
        (warehouse, unit)
        for warehouse in DATE_DIFF_WAREHOUSES
        for unit in CONVERSION_WINDOW_UNITS
        if (warehouse, unit) not in UNSUPPORTED_DATE_DIFF
    ],
)
@pytest.mark.parametrize("surface", ["select", "package", "aggregate_if", "relation"])
def test_date_diff_calls_use_dialect_lowering(package, warehouse, unit, surface, monkeypatch):
    dialect = dialect_for_warehouse(warehouse)
    lowered_calls = []
    original = type(dialect).date_diff

    def date_diff(self, actual_unit, start, end):
        result = original(self, actual_unit, start, end)
        lowered_calls.append(render_expr(result))
        assert actual_unit == unit
        return result

    monkeypatch.setattr(type(dialect), "date_diff", date_diff)
    expression = call("date_diff", literal(unit.upper()), literal(START), literal(END))
    if surface == "package":
        path = package / "models/rows.yml"
        model = yaml.safe_load(path.read_text())
        model["model"]["measures"]["amount"]["expr"] = expression
        path.write_text(yaml.safe_dump(model))
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    if surface == "select":
        expression = call(
            "DATE_DIFF", literal(unit), maximum(literal(START)), maximum(literal(END))
        )
    elif surface == "package":
        expression = {"measure": "measure.numbers.amount", "aggregation": "avg"}
    elif surface == "aggregate_if":
        expression = maximum(expression)
    if surface == "relation":
        sql = render_expr(
            _semantic_expr_to_sql(
                parse_semantic_expression(expression, context="config"), warehouse=warehouse
            ),
        )
    else:
        sql = compile_query(config, None, query(expression))["sql"]
    assert lowered_calls, f"{surface} bypassed dialect.date_diff"
    if surface == "select":
        # Final SQL assigns short aliases to the two aggregate leaves.
        expected = render_expr(
            original(
                dialect,
                unit,
                SqlIdentifier(parts=["base", "m1"]),
                SqlIdentifier(parts=["base", "m2"]),
            )
        )
        assert expected in sql
    else:
        assert any(lowered in sql for lowered in lowered_calls)
    if warehouse in {"postgres", "snowflake", "bigquery", "databricks"}:
        assert "DATE_DIFF(" not in sql.upper()


@pytest.mark.parametrize(
    "warehouse,expected",
    [
        (warehouse, f"DATE_DIFF('day', CAST('{START}' AS TIMESTAMP), CAST('{END}' AS TIMESTAMP))")
        for warehouse in ["duckdb", "motherduck", "ducklake"]
    ]
    + [
        (
            "clickhouse",
            f"DATE_DIFF('day', CAST('{START}' AS Nullable(DateTime)), CAST('{END}' AS Nullable(DateTime)))",
        ),
        (
            "postgres",
            f"CAST(CAST('{END}' AS TIMESTAMP) AS DATE) - CAST(CAST('{START}' AS TIMESTAMP) AS DATE)",
        ),
        (
            "snowflake",
            f"DATEDIFF('day', CAST('{START}' AS TIMESTAMP_NTZ), CAST('{END}' AS TIMESTAMP_NTZ))",
        ),
        ("bigquery", f"DATETIME_DIFF('{END}', '{START}', DAY)"),
        (
            "databricks",
            f"TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CAST('{START}' AS TIMESTAMP)), DATE_TRUNC('day', CAST('{END}' AS TIMESTAMP)))",
        ),
    ],
)
def test_date_diff_day_sql_preserves_endpoint_order(warehouse, expected):
    lowered = dialect_for_warehouse(warehouse).scalar_call(
        "DATE_DIFF", [SqlLiteral("day"), SqlLiteral(START), SqlLiteral(END)]
    )
    assert render_expr(lowered) == expected


@pytest.mark.parametrize("start,end", [(None, END), (START, None), (None, None)])
def test_date_diff_either_null_endpoint_executes_as_null(start, end):
    lowered = dialect_for_warehouse("duckdb").scalar_call(
        "DATE_DIFF", [SqlLiteral("day"), SqlLiteral(start), SqlLiteral(end)]
    )
    with duckdb.connect() as conn:
        assert conn.execute("SELECT " + render_expr(lowered)).fetchall() == [(None,)]


def test_date_diff_average_excludes_either_null_endpoint():
    lowered = dialect_for_warehouse("duckdb").scalar_call(
        "DATE_DIFF",
        [SqlLiteral("day"), SqlIdentifier(parts=["opened"]), SqlIdentifier(parts=["closed"])],
    )
    with duckdb.connect() as conn:
        conn.execute("CREATE TABLE dates(opened TIMESTAMP, closed TIMESTAMP)")
        conn.executemany(
            "INSERT INTO dates VALUES (?, ?)",
            [(START, END), (None, END), (START, None), (None, None)],
        )
        assert conn.execute(f"SELECT AVG({render_expr(lowered)}) FROM dates").fetchall() == [(2.0,)]


INVALID_DATE_DIFF_ARGS = [
    [],
    [literal("day")],
    [literal("day"), literal(START)],
    [literal("day"), literal(START), literal(END), literal(1)],
    [column("text_value"), literal(START), literal(END)],
    [call("COALESCE", literal("day"), literal("day")), literal(START), literal(END)],
    *[
        [literal(unit), literal(START), literal(END)]
        for unit in [None, 1, "", "second", "days", "day); SELECT 1"]
    ],
]


@pytest.mark.parametrize("warehouse", DATE_DIFF_WAREHOUSES)
@pytest.mark.parametrize("args", INVALID_DATE_DIFF_ARGS)
def test_malformed_date_diff_refused_by_validation_and_lowering(package, warehouse, args):
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    expression = call("DATE_DIFF", *args)
    parsed = parse_semantic_expression(expression, context="query")
    with pytest.raises(SemanticLayerError) as validation:
        validate_expression_calls(parsed, config)
    with pytest.raises(SemanticLayerError) as lowering:
        _semantic_expr_to_sql(parsed, warehouse=warehouse)
    assert validation.value.code == lowering.value.code == "INVALID_EXPRESSION_AST"
    assert str(validation.value) == str(lowering.value)
    assert validation.value.details["supported_units"] == list(CONVERSION_WINDOW_UNITS)
    assert "three args: a string literal unit, start, end" in str(validation.value)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        report = runtime.validate(query(maximum(expression)))
        assert not report["ok"]
        assert report["errors"][0]["code"] == validation.value.code
        assert report["errors"][0]["message"] == str(validation.value)
        with pytest.raises(SemanticLayerError) as compilation:
            compile_query(config, None, query(maximum(expression)))
        assert compilation.value.code == validation.value.code
        assert str(compilation.value) == str(validation.value)
    finally:
        runtime.close()


@pytest.mark.parametrize("args", INVALID_DATE_DIFF_ARGS)
def test_malformed_package_date_diff_refused_at_load(package, args):
    path = package / "models/rows.yml"
    model = yaml.safe_load(path.read_text())
    model["model"]["measures"]["amount"]["expr"] = call("DATE_DIFF", *args)
    path.write_text(yaml.safe_dump(model))
    with pytest.raises(
        SemanticLayerError, match="three args: a string literal unit, start, end"
    ) as exc:
        load_package_config(str(package))
    assert exc.value.code == "INVALID_EXPRESSION_AST"
