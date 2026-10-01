"""Packages without dates answer non-time questions and refuse time analysis."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.architect_mcp import _project_spec
from semantic_rails.architect_scaffold import (
    FirstModel,
    ProjectSpec,
    ProjectWarehouse,
    project_scaffold_files,
)
from semantic_rails.compiler import bind_query, compile_query, plan_query
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import CumulativeExpr, MeasureRefExpr
from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
    valid_values_payload,
)
from semantic_rails.package_tools import check_package_report
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

COUNT = "measure.catalogue.item_count"
AMOUNT = "measure.catalogue.total_amount"
CATEGORY = "dimension.catalogue_item_category"
BASE = {"version": 1, "select": [{"expression": {"measure": COUNT}, "as": "value"}]}


@pytest.fixture
def package_path(tmp_path: Path) -> Path:
    path = tmp_path / "catalogue"
    spec = ProjectSpec(
        package_id="catalogue",
        description="Items and their catalogue categories and amounts.",
        first_model=FirstModel(
            entity="item",
            relation="items",
            primary_key="item_id",
            time_column="",
            amount_column="amount",
            dimension_column="category",
        ),
    )
    for relative, contents in project_scaffold_files(spec).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    (path / "data/catalogue_csv/items.csv").write_text(
        "item_id,category,amount\n1,A,10\n2,A,20\n3,B,30\n4,B,40\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def runtime(package_path: Path):
    runtime = Runtime.from_path(str(package_path))
    try:
        yield runtime
    finally:
        runtime.close()


def test_validate_and_check_package_without_dates(package_path: Path) -> None:
    ref = PackageReference(source_path=str(package_path))
    report, config = parse_config_report(ref)
    assert report["ok"], report
    assert config is not None and config.temporal_roles == []
    report = check_package_report(ref, artifact_path=str(package_path.parent / "catalogue.tar.gz"))
    assert report["ok"], report
    assert report["checks"]["validate"]["summary"]["passed"] == 4
    assert report["checks"]["examples"]["summary"]["passed"] == 1
    assert report["checks"]["tests"]["summary"]["passed"] == 1
    assert report["artifact"]
    model = yaml.safe_load((package_path / "models/core/items.yml").read_text())
    assert "times" not in model["model"]
    assert "occurred_at" not in (package_path / "data/catalogue_csv/items.csv").read_text()


@pytest.mark.parametrize(
    ("expression", "group_by", "where", "sql"),
    [
        (
            {"measure": COUNT},
            [CATEGORY],
            [],
            "SELECT category, COUNT(DISTINCT item_id) AS value FROM items GROUP BY category",
        ),
        (
            {"kind": "ratio", "numerator": {"measure": AMOUNT}, "denominator": {"measure": COUNT}},
            [CATEGORY],
            [],
            "SELECT category, SUM(amount) / COUNT(DISTINCT item_id) AS value FROM items GROUP BY category",
        ),
        (
            {"measure": AMOUNT},
            [],
            [{"field": CATEGORY, "op": "=", "value": "A"}],
            "SELECT SUM(amount) AS value FROM items WHERE category = 'A'",
        ),
    ],
)
def test_non_time_answers_match_independent_sql(runtime, expression, group_by, where, sql) -> None:
    query = {
        "version": 1,
        "select": [{"expression": expression, "as": "value"}],
        "group_by": group_by,
        "where": where,
    }
    actual = runtime.query(query)["rows"]
    with duckdb.connect(":memory:") as db:
        db.execute("CREATE TABLE items(item_id INT, category VARCHAR, amount INT)")
        db.execute(
            "INSERT INTO items VALUES (1, 'A', 10), (2, 'A', 20), (3, 'B', 30), (4, 'B', 40)"
        )
        expected = db.execute(sql).fetchall()
    columns = [CATEGORY, "value"] if group_by else ["value"]
    assert sorted(tuple(row[column] for column in columns) for row in actual) == sorted(expected)


TIME_REQUESTS = [
    {**BASE, "time": {}},
    {**BASE, "time": {"grain": "month"}},
    {**BASE, "time": {"temporal_role": "temporal_role.absent", "grain": "month"}},
    {**BASE, "grain": "month"},
    {**BASE, "default_query_axis": True},
    {**BASE, "temporal_role_overrides": {COUNT: "temporal_role.absent"}},
    {
        **BASE,
        "select": [{"expression": {"measure": COUNT, "temporal_role": "temporal_role.absent"}}],
    },
    *[
        {**BASE, "select": [{"expression": {"kind": kind, "input": {"measure": COUNT}, **extra}}]}
        for kind, extra in [
            ("cumulative", {}),
            ("prior_period", {"offset": {"unit": "month", "value": 1}}),
            ("rolling", {"window": {"unit": "day", "value": 7}}),
            ("period_to_date", {"period": "year"}),
            ("rolling", {"window": {"unit": "day", "value": 1}}),
        ]
    ],
    {
        **BASE,
        "select": [
            {
                "expression": {
                    "kind": "aggregate",
                    "measure": COUNT,
                    "window": {"unit": "day", "value": 7},
                }
            }
        ],
    },
    {
        **BASE,
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.catalogue_item",
                    "input": {"measure": COUNT},
                    "op": ">",
                    "value": 1,
                    "time_grain": "month",
                },
                "op": "=",
                "value": True,
            }
        ],
    },
    {
        **BASE,
        "where": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.catalogue_item",
                    "input": {"measure": COUNT},
                    "op": ">",
                    "value": 1,
                    "window": {"unit": "day", "value": 7},
                }
            }
        ],
    },
    {
        **BASE,
        "select": [
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": COUNT,
                    "anchor": {"temporal_role": "temporal_role.absent"},
                    "window": {"unit": "day", "value": 7},
                }
            }
        ],
    },
    {**BASE, "select": [{"expression": {"measure": AMOUNT, "aggregation": "first_value"}}]},
]


@pytest.mark.parametrize("query", TIME_REQUESTS)
@pytest.mark.parametrize(
    "operation", ["validate", "compile", "query", "plan_query", "bind_query", "compile_query"]
)
def test_every_time_request_refuses_with_the_same_error(runtime, operation, query) -> None:
    if operation == "validate":
        result = runtime.validate(query)
        assert result["ok"] is False
        assert result["errors"][0]["code"] == "INVALID_TEMPORAL_ROLE"
        assert "declares no time" in result["errors"][0]["message"]
        assert result["recovery_hints"][0]["kind"] == "remove_time_or_declare_role"
    else:
        with pytest.raises(SemanticLayerError) as exc:
            if operation in {"plan_query", "bind_query", "compile_query"}:
                {
                    "plan_query": plan_query,
                    "bind_query": bind_query,
                    "compile_query": compile_query,
                }[operation](runtime._config, runtime.registry, query)
            else:
                getattr(runtime, operation)(query)
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
        assert "declares no time" in str(exc.value)


def test_nested_authored_time_expression_cannot_bypass_guard(runtime) -> None:
    recipe = replace(
        runtime._config.metric_recipes[0],
        id="metric.catalogue.hidden_time",
        expression=CumulativeExpr(MeasureRefExpr(COUNT)),
    )
    config = replace(runtime._config, metric_recipes=[*runtime._config.metric_recipes, recipe])
    query = {
        **BASE,
        "select": [
            {
                "expression": {
                    "kind": "ratio",
                    "numerator": {"metric": recipe.id},
                    "denominator": {"measure": COUNT},
                }
            }
        ],
    }
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        compile_query(config, None, query)
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


@pytest.mark.parametrize(
    "intent",
    [
        "item count last month",
        "item count by month",
        "item count over time",
        "item count vs prior year",
        "item count last few weeks",
        "rolling item count",
        "item count year to date",
        "cumulative item count",
        "item count per quarter",
        "item count YTD",
        "item count MTD",
        "item count QTD",
        "running total item count",
        "item count this week",
        "item count next year",
        "item count previous day",
        "item count since 2023-01-01",
        "item count between 2023-01-01 and 2023-02-01",
        "item count before 2023-01-01",
        "item count after 2023-01-01",
        "item count in 2023",
        "daily item count",
        "weekly item count",
        "monthly item count",
        "quarterly item count",
        "yearly item count",
        "annual item count",
        "item count annually",
        "item count every month",
        "item count by calendar month",
        "item count tomorrow",
        "item count trend",
        "item count each month",
        "item count time series",
        "hourly item count",
        "item count at month grain",
        "item count trending",
        "item count history",
        "item count historical",
        "item count per second",
        "item count by minute",
        "item count per hour",
        "item count every second",
        "item count at second grain",
    ],
)
def test_plan_time_phrases_refuse(runtime, intent: str) -> None:
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        plan_payload(runtime, intent=intent)
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
def test_plan_fallback_cannot_ignore_time_intent(package_path, detail) -> None:
    package_file = package_path / "package.yml"
    package = yaml.safe_load(package_file.read_text())
    package["package"]["planner"] = {"disabled_patterns": ["metric_by_dimension_rollup"]}
    package_file.write_text(yaml.safe_dump(package, sort_keys=False))
    runtime = Runtime.from_path(str(package_path))
    try:
        from semantic_rails.planner import compose

        assert compose(runtime, "item count every month").draft is None
        with pytest.raises(SemanticLayerError, match="declares no time") as exc:
            plan_payload(runtime, intent="item count every month", detail=detail)
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "measure_label", "dimension_label"),
    [
        ("item count for the History category", "Item count", "Category"),
        ("item count by daycare", "Item count", "Daycare"),
        ("count of monthly plans", "Monthly plans", "Category"),
        ("moving company count", "Moving company count", "Category"),
        ("second item count", "Second item count", "Category"),
    ],
)
def test_plan_ordinary_time_words_answer(
    package_path, intent: str, measure_label: str, dimension_label: str
) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["measures"]["item_count"]["label"] = measure_label
    model["model"]["dimensions"]["category"]["label"] = dimension_label
    model["model"]["dimensions"]["category"]["domain"] = ["History", "B"]
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    metrics_path = package_path / "metrics/core.yml"
    metrics = yaml.safe_load(metrics_path.read_text())
    metrics["metrics"]["item_count"]["label"] = measure_label
    metrics_path.write_text(yaml.safe_dump(metrics, sort_keys=False))
    (package_path / "data/catalogue_csv/items.csv").write_text(
        "item_id,category,amount\n1,History,10\n2,History,20\n3,B,30\n4,B,40\n"
    )
    runtime = Runtime.from_path(str(package_path))
    try:
        result = plan_payload(runtime, intent=intent)
        assert result["status"] == "ok", result
        query = result["best"]["query_ir"]
        assert "time" not in query
        rows = runtime.query(query)["rows"]
        values = [next(value for key, value in row.items() if key != CATEGORY) for row in rows]
        assert sorted(values) == (
            [2] if "History" in intent else [2, 2] if "daycare" in intent else [4]
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("phrase", "match_by"),
    [
        (phrase, match_by)
        for phrase in (
            "by month",
            "over time",
            "year to date",
            "rolling",
            "cumulative",
            "monthly",
            "every month",
            "by calendar month",
            "trend",
            "each month",
            "time series",
            "at month grain",
            "İ monthly",
        )
        for match_by in ("value", "label", "alias")
    ]
    + [("hourly", "value")],
)
def test_plan_time_phrase_category_values_answer(package_path, phrase, match_by) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    value = phrase if match_by == "value" else "A"
    entry = {"value": value, "label": phrase if match_by == "label" else value}
    if match_by == "alias":
        entry["aliases"] = [phrase]
    model["model"]["dimensions"]["category"]["domain"] = [entry, "B"]
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    (package_path / "data/catalogue_csv/items.csv").write_text(
        f"item_id,category,amount\n1,{value},10\n2,{value},20\n3,B,30\n4,B,40\n"
    )
    runtime = Runtime.from_path(str(package_path))
    try:
        result = plan_payload(runtime, intent=f"item count for the {phrase} category")
        assert result["status"] == "ok", result.get("why")
        query = result["best"]["query_ir"]
        assert "time" not in query
        assert query["where"] == [{"field": CATEGORY, "op": "=", "value": value}]
        rows = runtime.query(query)["rows"]
        assert len(rows) == 1
        assert [v for k, v in rows[0].items() if k != CATEGORY] == [2]
        with pytest.raises(SemanticLayerError, match="declares no time") as exc:
            plan_payload(runtime, intent=f"item count for the {phrase} category per day")
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "phrase",
    ["last month", "since 2023-01-01", "in 2023", "tomorrow", "next month", "İ last month"],
)
@pytest.mark.parametrize("match_by", ["value", "label", "alias"])
def test_plan_window_shaped_category_values_downgrade(package_path, phrase, match_by) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    value = phrase if match_by == "value" else "A"
    entry = {"value": value, "label": phrase if match_by == "label" else value}
    if match_by == "alias":
        entry["aliases"] = [phrase]
    model["model"]["dimensions"]["category"]["domain"] = [entry, "B"]
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    (package_path / "data/catalogue_csv/items.csv").write_text(
        f"item_id,category,amount\n1,{value},10\n2,{value},20\n3,B,30\n4,B,40\n"
    )
    runtime = Runtime.from_path(str(package_path))
    try:
        result = plan_payload(runtime, intent=f"item count for the {phrase} category")
        assert result["status"] == "low_confidence", result.get("why")
        query = result["best"].get("query_ir")
        assert query is None or "time" not in query
        with pytest.raises(SemanticLayerError, match="declares no time") as exc:
            plan_payload(runtime, intent=f"item count for the {phrase} category per day")
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


@pytest.mark.parametrize("partial_query", [None, {"time": {}}])
def test_plan_off_topic_time_phrase_is_out_of_scope(runtime, partial_query) -> None:
    result = plan_payload(
        runtime, intent="write a poem about last month", partial_query=partial_query
    )
    assert result["status"] == "out_of_scope"
    assert result["best"] is None


def test_plan_irrelevant_time_phrase_is_out_of_scope(runtime) -> None:
    result = plan_payload(runtime, intent="rainfall last month")
    assert result["status"] == "out_of_scope"
    assert result["best"] is None


def test_plan_drafted_time_cannot_bypass_guard(runtime, monkeypatch) -> None:
    from semantic_rails.planner.orchestrator import compose

    result = compose(runtime, "item count")
    assert result.draft is not None
    result = replace(result, draft=replace(result.draft, query={**BASE, "time": {}}))
    monkeypatch.setattr("semantic_rails.planner.plan.compose", lambda *_: result)
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        plan_payload(runtime, intent="item count")
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


def test_plan_unrealized_category_cannot_hide_time(runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        "semantic_rails.planner.plan._matched_value_rows",
        lambda *_: [{"dimension_id": CATEGORY, "value": "A", "label": "rolling"}],
    )
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        plan_payload(runtime, intent="rolling item count")
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


@pytest.mark.parametrize(
    ("measure", "label", "intent"),
    [
        ("total_amount", "Monthly rent", "monthly rent by category"),
        ("item_count", "Monthly plans", "count of monthly plans"),
    ],
)
def test_plan_measure_label_without_recipe_answers(package_path, measure, label, intent) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["measures"][measure]["label"] = label
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    metrics_path = package_path / "metrics/core.yml"
    metrics = yaml.safe_load(metrics_path.read_text())
    del metrics["metrics"][measure]
    metrics_path.write_text(yaml.safe_dump(metrics, sort_keys=False))
    runtime = Runtime.from_path(str(package_path))
    try:
        result = plan_payload(runtime, intent=intent)
        assert result["status"] == "ok", result
        assert "time" not in result["best"]["query_ir"]
        with pytest.raises(SemanticLayerError) as exc:
            plan_payload(runtime, intent=f"{intent} last month")
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


def test_plan_partial_category_value_cannot_hide_time(package_path) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["dimensions"]["category"]["domain"] = ["last", "B"]
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    runtime = Runtime.from_path(str(package_path))
    try:
        with pytest.raises(SemanticLayerError) as exc:
            plan_payload(runtime, intent="item count last month")
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


def test_plan_short_category_keeps_original_question(runtime, monkeypatch) -> None:
    from semantic_rails.planner.faithfulness import unconsumed_terms

    questions = []

    def record(runtime, question, query):
        questions.append(question)
        return unconsumed_terms(runtime, question, query)

    monkeypatch.setattr("semantic_rails.planner.plan.unconsumed_terms", record)
    intent = "item count for A as a category"
    plan_payload(runtime, intent=intent)
    assert questions and all(question == intent for question in questions)


def test_scalar_date_arithmetic_without_time_matches_independent_sql(runtime) -> None:
    def column(name):
        return {"kind": "column", "entity": "entity.catalogue_item", "column": name}

    date_add = {
        "kind": "date_add",
        "unit": "day",
        "value": {"kind": "literal", "value": 1},
        "date": column("delivery_date"),
    }
    query = {
        "version": 2,
        "select": [
            {
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": {
                        "kind": "comparison",
                        "op": "=",
                        "left": date_add,
                        "right": column("due_date"),
                    },
                },
                "as": "value",
            }
        ],
    }
    compiled = compile_query(runtime._config, runtime.registry, query)
    with duckdb.connect(":memory:") as db:
        db.execute("CREATE TABLE items(item_id INT, delivery_date DATE, due_date DATE)")
        db.execute(
            "INSERT INTO items VALUES (1, '2026-01-01', '2026-01-02'), "
            "(2, '2026-01-01', '2026-01-03')"
        )
        actual = db.execute(compiled["sql"]).fetchall()
        expected = db.execute(
            "SELECT COUNT(*) FROM items WHERE delivery_date + INTERVAL '1 day' = due_date"
        ).fetchall()
    assert actual == expected == [(1,)]
    # Scalar arithmetic still traverses operands that themselves require time.
    date_add["date"] = {"kind": "cumulative", "input": {"measure": COUNT}}
    with pytest.raises(SemanticLayerError) as exc:
        compile_query(runtime._config, runtime.registry, query)
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


def test_plan_dimension_label_with_time_word_answers(package_path) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["dimensions"]["category"]["label"] = "Monthly plans"
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    runtime = Runtime.from_path(str(package_path))
    try:
        result = plan_payload(runtime, intent="item count by monthly plans")
        assert result["status"] == "ok", result
        query = result["best"]["query_ir"]
        assert query["group_by"] == [CATEGORY]
        assert "time" not in query
        assert sorted(row["item_count"] for row in runtime.query(query)["rows"]) == [2, 2]
    finally:
        runtime.close()


def test_plan_relevant_revenue_window_refuses(package_path) -> None:
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["measures"]["total_amount"]["label"] = "Revenue"
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    runtime = Runtime.from_path(str(package_path))
    try:
        with pytest.raises(SemanticLayerError, match="declares no time") as exc:
            plan_payload(runtime, intent="revenue last month")
        assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "variant",
    [
        "cumulative",
        "prior_period",
        "rolling",
        "period_to_date",
        "first_value",
        "last_value",
        "metric",
        "measure",
    ],
)
@pytest.mark.parametrize(
    "operation",
    ["validate", "compile", "inspect", "discover", "build-options", "valid-values", "plan"],
)
def test_normalized_time_expressions_refuse(runtime, variant: str, operation: str) -> None:
    if variant in {"metric", "measure"}:
        if variant == "metric":
            obj = replace(
                runtime._config.metric_recipes[0],
                expression=CumulativeExpr(MeasureRefExpr(COUNT)),
            )
            runtime._config = replace(runtime._config, metric_recipes=[obj])
        else:
            obj = replace(
                next(row for row in runtime._config.measures if row.id == AMOUNT),
                expr=CumulativeExpr(MeasureRefExpr(COUNT)),
            )
            runtime._config = replace(
                runtime._config,
                measures=[obj if row.id == obj.id else row for row in runtime._config.measures],
            )
        expression = {variant: f" {obj.id} "}
    elif variant in {"first_value", "last_value"}:
        expression = {"measure": AMOUNT, "aggregation": f" {variant} "}
    else:
        expression = {"kind": f" {variant} ", "input": {"measure": COUNT}}
        expression.update(
            {
                "prior_period": {"offset": {"unit": "month", "value": 1}},
                "rolling": {"window": {"unit": "day", "value": 7}},
                "period_to_date": {"period": "year"},
            }.get(variant, {})
        )
    query = {**BASE, "select": [{"expression": expression}]}
    if operation == "validate":
        result = runtime.validate(query)
        assert result["ok"] is False
        assert result["errors"][0]["code"] == "INVALID_TEMPORAL_ROLE"
        assert result["recovery_hints"][0]["kind"] == "remove_time_or_declare_role"
        return
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        if operation == "compile":
            runtime.compile(query)
        elif operation == "inspect":
            inspect_payload(runtime, object_id=COUNT, partial_query=query)
        elif operation == "discover":
            discover_payload(runtime, terms="item count", partial_query=query)
        elif operation == "build-options":
            build_options_payload(runtime, partial_query=query)
        elif operation == "valid-values":
            valid_values_payload(runtime, dimension_id=CATEGORY, query=query)
        else:
            plan_payload(runtime, intent="item count", partial_query=query)
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"
    assert exc.value.details["available_temporal_roles"] == []


def test_plan_partial_time_refuses_before_it_can_be_dropped(runtime) -> None:
    with pytest.raises(SemanticLayerError, match="declares no time"):
        plan_payload(runtime, intent="item count", partial_query={"time": {}})


def test_catalog_inspect_and_builder_without_dates(runtime) -> None:
    assert not any(row["kind"] == "temporal_role" for row in runtime.catalog()["objects"])
    assert catalog_payload(runtime)["temporal_roles"] == []
    card = inspect_payload(runtime, object_id=COUNT)["card"]
    assert card["compatible_temporal_roles"] == []
    builder = build_options_payload(runtime, partial_query=BASE, verbosity="full")
    assert "time" not in builder["next_legal_steps"]
    assert not any(row.get("kind") == "temporal_role" for row in builder["available"])
    plan = plan_payload(runtime, intent="item count by category")
    assert plan["status"] == "ok", plan
    assert "time" not in plan["best"]["query_ir"]


def test_literal_filter_values_and_caller_metadata_are_not_time_requests(runtime) -> None:
    query = {
        **BASE,
        "where": [{"field": CATEGORY, "op": "=", "value": "last month"}],
        "policy_context": {"note": {"time": {"grain": "month"}}},
    }
    assert runtime.validate(query)["ok"]


@pytest.mark.parametrize("time_column", ["", "   ", "\t\n"])
def test_architect_blank_time_column_is_preserved(time_column) -> None:
    assert (
        _project_spec(
            {"package_id": "catalogue", "time_column": time_column}
        ).first_model.time_column
        == ""
    )


@pytest.mark.parametrize("data", ["starter", "external"])
def test_scaffold_whitespace_time_column_is_absent(data) -> None:
    spec = ProjectSpec(
        package_id="catalogue",
        warehouse=ProjectWarehouse(data=data),
        first_model=FirstModel(time_column=" \t "),
    )
    files = project_scaffold_files(spec)
    model = yaml.safe_load(
        next(contents for path, contents in files.items() if path.startswith("models/"))
    )
    assert "times" not in model["model"]
    assert all(b"occurred_at" not in contents for contents in files.values())


@pytest.mark.parametrize("operation", ["inspect", "build-options", "discover", "valid-values"])
def test_metadata_partial_time_refuses(runtime, operation) -> None:
    query = {"time": {}}
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        if operation == "inspect":
            inspect_payload(runtime, object_id=COUNT, partial_query=query)
        elif operation == "build-options":
            build_options_payload(runtime, partial_query=query)
        elif operation == "discover":
            discover_payload(runtime, terms="item count", partial_query=query)
        else:
            valid_values_payload(runtime, dimension_id=CATEGORY, query=query)
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


def test_default_query_axis_requires_declared_time(package_path) -> None:
    path = package_path / "package.yml"
    payload = yaml.safe_load(path.read_text())
    payload["defaults"]["time"]["default_query_axis"] = True
    path.write_text(yaml.safe_dump(payload))
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        load_package_config(str(package_path))
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


def test_non_time_lookup_matches_independent_sql(package_path) -> None:
    graph_path = package_path / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["entities"]["category"] = {"key": ["category"], "model": "categories"}
    graph_path.write_text(yaml.safe_dump(graph))
    model_path = package_path / "models/core/items.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["entities"]["category"] = {}
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    (package_path / "models/core/categories.yml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "id": "categories",
                    "relation": "categories",
                    "entities": {"category": {}},
                    "dimensions": {"label": {"kind": "categorical", "label": "Category label"}},
                }
            }
        )
    )
    (package_path / "data/catalogue_csv/categories.csv").write_text(
        "category,label\nA,Alpha\nB,Beta\n"
    )
    dimension = "dimension.catalogue_category_label"
    runtime = Runtime.from_path(str(package_path))
    try:
        actual = runtime.query({**BASE, "group_by": [dimension]})["rows"]
    finally:
        runtime.close()
    with duckdb.connect(":memory:") as db:
        db.execute("CREATE TABLE items(item_id INT, category VARCHAR)")
        db.execute("INSERT INTO items VALUES (1, 'A'), (2, 'A'), (3, 'B'), (4, 'B')")
        db.execute("CREATE TABLE categories(category VARCHAR, label VARCHAR)")
        db.execute("INSERT INTO categories VALUES ('A', 'Alpha'), ('B', 'Beta')")
        expected = db.execute(
            "SELECT c.label, COUNT(DISTINCT i.item_id) FROM items i "
            "JOIN categories c ON i.category = c.category GROUP BY c.label"
        ).fetchall()
    assert sorted((row[dimension], row["value"]) for row in actual) == sorted(expected)


def test_bound_compilation_cannot_bypass_time_guard(runtime) -> None:
    binding = bind_query(runtime._config, runtime.registry, BASE)
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        compile_query(runtime._config, runtime.registry, {**BASE, "time": {}}, binding=binding)
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


@pytest.mark.parametrize("command", ["init", "project"])
def test_cli_can_create_a_package_without_time(tmp_path, command) -> None:
    prefix = ["init", "catalogue"] if command == "init" else ["project", "new", "catalogue"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails",
            *prefix,
            "--workspace-root",
            str(tmp_path),
            "--time-column",
            "",
            *(["--yes"] if command == "init" else []),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout)["ok"]
    assert load_package_config(str(tmp_path / "catalogue")).temporal_roles == []


def test_external_scaffold_can_omit_time(tmp_path) -> None:
    spec = ProjectSpec(
        package_id="catalogue",
        warehouse=ProjectWarehouse(data="external"),
        first_model=FirstModel(time_column=""),
    )
    for relative, contents in project_scaffold_files(spec).items():
        target = tmp_path / "catalogue" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    report, config = parse_config_report(PackageReference(source_path=str(tmp_path / "catalogue")))
    assert report["ok"], report
    assert config is not None and config.temporal_roles == []


@pytest.mark.parametrize("field", ["step", "stage"])
def test_builder_explicit_time_step_refuses(runtime, field) -> None:
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        build_options_payload(runtime, partial_query=BASE, **{field: "time"})
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


def test_authored_metric_window_cannot_bypass_guard(runtime) -> None:
    recipe = replace(
        runtime._config.metric_recipes[0],
        id="metric.catalogue.windowed",
        window_spec={"unit": "day", "value": 7},
    )
    config = replace(runtime._config, metric_recipes=[recipe])
    with pytest.raises(SemanticLayerError, match="declares no time") as exc:
        compile_query(config, None, {**BASE, "select": [{"expression": {"metric": recipe.id}}]})
    assert exc.value.code == "INVALID_TEMPORAL_ROLE"


@pytest.mark.parametrize("mode", ["run", "validate", "sql"])
def test_mcp_time_refusal_keeps_code_and_actionable_hint(runtime, mode) -> None:
    result = SemanticLayerMCPAdapter(runtime).call_tool(
        "execute",
        {
            "query": {**BASE, "time": {}},
            "mode": mode,
        },
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_TEMPORAL_ROLE"
    assert "declares no time" in result["error"]["message"]
    assert result["recovery_hints"][0]["kind"] == "remove_time_or_declare_role"


def test_http_time_refusal_is_a_client_error(runtime) -> None:
    service = SemanticHTTPService(runtime)
    try:
        body, status = service.handle(
            "POST", normalize_route("/api/v1/query"), {**BASE, "time": {}}
        )
    except SemanticLayerError as exc:
        body, status = service.exception_payload(exc, stage="http")
    assert status == 400
    assert body["error"]["code"] == "INVALID_TEMPORAL_ROLE"
    assert "declares no time" in body["error"]["message"]
    assert body["recovery_hints"][0]["kind"] == "remove_time_or_declare_role"
