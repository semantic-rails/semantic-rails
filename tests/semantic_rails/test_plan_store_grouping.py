"""Store grouping preserves the requested attribute and reporting grain."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.patterns import metric_by_dimension_rollup
from semantic_rails.runtime import Runtime

STORE_ID = "dimension.retail_store_id"
STORE_NAME = "dimension.retail_store_name"
STORE_LABEL = "dimension.retail_store_label"
JAFFLE_STORE = "dimension.jaffle_store_name"


@pytest.fixture()
def retail(tmp_path: Path) -> Iterator[Runtime]:
    (tmp_path / "models").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "stores.csv").write_text(
        "store_id,store_name,store_label,opened_at,revenue\n"
        "a,Central,East,2026-01-01,10\n"
        "b,Central,West,2026-01-01,25\n"
        "c,Harbor,South,2026-01-01,7\n",
        encoding="utf-8",
    )
    (tmp_path / "package.yml").write_text(
        "schema_version: 1\n"
        "package:\n"
        "  id: retail\n"
        "  namespace: retail\n"
        "  name: Retail\n"
        "  warehouse: duckdb\n"
        "  default_db: data/retail.duckdb\n"
        "  seed: {kind: csv_dir_duckdb, source: data}\n",
        encoding="utf-8",
    )
    (tmp_path / "graph.yml").write_text(
        "graph:\n  entities:\n    store: {key: [store_id], model: stores}\n",
        encoding="utf-8",
    )
    (tmp_path / "models" / "stores.yml").write_text(
        "model:\n"
        "  id: stores\n"
        "  relation: stores\n"
        "  entities: {store: {}}\n"
        "  times:\n"
        "    opened_at: {column: opened_at, kind: date, default: true}\n"
        "  dimensions:\n"
        f"    store_id: {{as: {STORE_ID}, label: Store id}}\n"
        f"    store_name: {{as: {STORE_NAME}, label: Store name}}\n"
        f"    store_label: {{as: {STORE_LABEL}, label: Store label}}\n"
        "  measures:\n"
        "    revenue: {kind: aggregate, expr: revenue, label: Revenue, default_agg: sum}\n",
        encoding="utf-8",
    )
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "dimension", "column", "row_count"),
    [
        ("revenue by store id", STORE_ID, "store_id", 3),
        ("revenue by store id in January 2026", STORE_ID, "store_id", 3),
        ("revenue by store name", STORE_NAME, "store_name", 2),
        ("revenue by store label", STORE_LABEL, "store_label", 3),
        ("revenue by store", STORE_NAME, "store_name", 2),
        ("revenue per store", STORE_NAME, "store_name", 2),
    ],
)
def test_store_attribute_matches_reference_sql(
    retail: Runtime, question: str, dimension: str, column: str, row_count: int
) -> None:
    plan = plan_payload(retail, intent=question)
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    assert query["group_by"] == [dimension]
    [selected] = query["select"]
    actual = retail.query(query)["rows"]
    reference = (
        retail._get_adapter()
        ._db.conn.execute(
            f"SELECT {column}, SUM(revenue) FROM stores GROUP BY {column} ORDER BY {column}"
        )
        .fetchall()
    )
    assert len(actual) == row_count
    assert sorted((row[dimension], row[selected["as"]]) for row in actual) == reference


@pytest.mark.parametrize(
    "question", ["revenue by store", "revenue per store", "top stores by revenue"]
)
def test_bare_store_keeps_jaffle_names(runtime_factory, question: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        plan = plan_payload(runtime, intent=question)
        assert plan["status"] == "ok", plan.get("why")
        assert plan["best"]["query_ir"]["group_by"] == [JAFFLE_STORE]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "grouped"),
    [
        ("how many stores are open", False),
        ("number of stores open", False),
        ("number of stores open by store", True),
    ],
)
def test_open_store_count_matches_reference_sql(
    runtime_factory, question: str, grouped: bool
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        plan = plan_payload(runtime, intent=question)
        query = plan["best"]["query_ir"]
        assert query.get("group_by", []) == ([JAFFLE_STORE] if grouped else [])
        [selected] = query["select"]
        assert selected["expression"]["measure"] == "measure.jaffle.open_store_count_eop"
        actual = runtime.query(query)["rows"]
        prefix = "s.store_name, " if grouped else ""
        suffix = "GROUP BY s.store_name ORDER BY s.store_name" if grouped else ""
        reference = (
            runtime._get_adapter()
            ._db.conn.execute(
                "WITH latest AS (SELECT store_id, open_store_count, "
                "ROW_NUMBER() OVER (PARTITION BY store_id ORDER BY date_day DESC) AS n "
                "FROM jaffle_store_inventory_snapshot) "
                f"SELECT {prefix}SUM(open_store_count) FROM latest "
                "JOIN jaffle_store s USING (store_id) WHERE n = 1 "
                f"{suffix}"
            )
            .fetchall()
        )
        if grouped:
            assert sorted((row[JAFFLE_STORE], row[selected["as"]]) for row in actual) == reference
        else:
            assert [(row[selected["as"]],) for row in actual] == reference
    finally:
        runtime.close()


@pytest.mark.parametrize("attribute", ["id", "name", "label"])
def test_ambiguous_store_attribute_is_not_ready(retail: Runtime, attribute: str) -> None:
    original = next(
        row for row in retail.config.dimensions if row.label.lower() == f"store {attribute}"
    )
    config = replace(
        retail.config,
        dimensions=[*retail.config.dimensions, replace(original, id="dimension.other_store")],
    )
    runtime = Runtime.from_config(config, source_path=retail.source_path)
    try:
        plan = plan_payload(runtime, intent=f"revenue by store {attribute}")
        assert plan["status"] == "low_confidence"
        assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert "ready_for" not in plan["next"]
    finally:
        runtime.close()


def test_a_bypassed_store_resolution_is_not_ready(retail: Runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        metric_by_dimension_rollup, "_maybe_group_by", lambda *a, **kw: [STORE_NAME]
    )
    plan = plan_payload(retail, intent="revenue by store id")
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "ready_for" not in plan["next"]


def test_an_unknown_store_attribute_is_not_ready(retail: Runtime) -> None:
    plan = plan_payload(retail, intent="revenue by store color")
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "ready_for" not in plan["next"]
