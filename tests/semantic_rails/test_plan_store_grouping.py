"""Store grouping preserves the requested attribute and reporting grain."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

STORE_ID = "dimension.retail_store_id"
STORE_NAME = "dimension.retail_store_name"
JAFFLE_STORE = "dimension.jaffle_store_name"
STORE_FILTER = {"field": JAFFLE_STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}


@pytest.fixture()
def jaffle(runtime_factory) -> Iterator[Runtime]:
    runtime = runtime_factory("jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


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
        f"    store_id: {{as: {STORE_ID}, label: Store id, "
        "synonyms: [Store key, Store number, Store code]}\n"
        f"    store_name: {{as: {STORE_NAME}, label: Store name}}\n"
        "    store_label: {label: Store label}\n"
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
        ("revenue by store key", STORE_ID, "store_id", 3),
        ("revenue by store number", STORE_ID, "store_id", 3),
        ("revenue by store code", STORE_ID, "store_id", 3),
        ("revenue by store name", STORE_NAME, "store_name", 2),
        ("revenue by store", STORE_NAME, "store_name", 2),
        ("revenue by stores", STORE_NAME, "store_name", 2),
        # These retain the store-name shortcut's behavior on main.
        ("revenue by, store name", STORE_NAME, "store_name", 2),
        ("revenue for each store name", STORE_NAME, "store_name", 2),
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
    ("question", "groups"),
    [
        (
            "revenue by customer type, store name",
            ["dimension.jaffle_customer_type", JAFFLE_STORE],
        ),
        ("revenue for each store name", [JAFFLE_STORE]),
        (
            "revenue by customer type; by store name",
            ["dimension.jaffle_customer_type", JAFFLE_STORE],
        ),
    ],
)
def test_filtered_store_grouping_retains_main_behavior(
    jaffle: Runtime, question: str, groups: list[str]
) -> None:
    plan = plan_payload(jaffle, intent=question, partial_query={"where": [STORE_FILTER]})
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    assert set(query.get("group_by", [])) == set(groups)
    [selected] = query["select"]
    actual = sorted(
        tuple(row[group] for group in groups) + (row[selected["as"]],)
        for row in jaffle.query(query)["rows"]
    )
    columns = "c.customer_type, s.store_name" if len(groups) == 2 else "s.store_name"
    group_columns = "1, 2" if len(groups) == 2 else "1"
    reference = (
        jaffle._get_adapter()
        ._db.conn.execute(
            f"SELECT {columns}, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
            "JOIN jaffle_customer c USING (customer_id) "
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
            f"GROUP BY {group_columns} ORDER BY {group_columns}"
        )
        .fetchall()
    )
    assert [row[:-1] for row in actual] == [row[:-1] for row in reference]
    assert [row[-1] for row in actual] == pytest.approx([row[-1] for row in reference])


@pytest.mark.parametrize(
    "question",
    [
        "revenue by store id name",
        "revenue by store id and store name",
        "revenue by store label",
        "revenue by store mystery",
    ],
)
def test_unclear_store_attribute_is_not_ready(retail: Runtime, question: str) -> None:
    plan = plan_payload(retail, intent=question)
    assert plan["status"] != "ok"
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    assert plan["why"]["code"] == "PLAN_UNMATCHED_TERMS"


@pytest.mark.parametrize("term", ["store id", "store key", "store number", "store code"])
def test_store_attribute_matching_both_dimensions_is_not_ready(retail: Runtime, term: str) -> None:
    retail._config = replace(
        retail._config,
        dimensions=[
            replace(dim, aliases=[*(dim.aliases or []), term])
            if dim.id == STORE_NAME
            else dim
            for dim in retail._config.dimensions
        ],
    )
    plan = plan_payload(retail, intent=f"revenue by {term}")
    assert plan["status"] != "ok"
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    assert plan["why"]["code"] == "PLAN_UNMATCHED_TERMS"


@pytest.mark.parametrize("term", ["store id", "store key", "store number"])
def test_missing_store_id_dimension_is_not_ready(retail: Runtime, term: str) -> None:
    retail._config = replace(
        retail._config, dimensions=[dim for dim in retail._config.dimensions if dim.id != STORE_ID]
    )
    plan = plan_payload(retail, intent=f"revenue by {term}")
    assert plan["status"] != "ok"
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    assert plan["why"]["code"] == "PLAN_UNMATCHED_TERMS"
