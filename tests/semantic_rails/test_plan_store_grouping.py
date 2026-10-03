"""Store groupings preserve the requested attribute even when names repeat."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_value_lists import _force_fallback

STORE_ID = "dimension.retail_store_id"
STORE_NAME = "dimension.retail_store_name"
STORE_LABEL = "dimension.retail_store_label"
STORE_COLUMNS = {STORE_ID: "store_id", STORE_NAME: "store_name", STORE_LABEL: "store_label"}
ID_FILTER = {"field": STORE_ID, "op": "IN", "value": ["a", "b", "c"]}
NAME_FILTER = {"field": STORE_NAME, "op": "IN", "value": ["Central", "Harbor"]}
# A question that names store attributes plainly is answered, grouped by those attributes.
ANSWERED = {
    "revenue by store id",
    "revenue by store name",
    "revenue by store label",
    "revenue by store id and store name",
}


@pytest.fixture()
def retail(tmp_path: Path) -> Iterator[Runtime]:
    (tmp_path / "models").mkdir()
    (tmp_path / "data" / "csv").mkdir(parents=True)
    (tmp_path / "data" / "csv" / "stores.csv").write_text(
        "store_id,store_name,store_label,reported_at,revenue\n"
        "a,Central,East,2026-01-01T09:00:00,10\n"
        "b,Central,West,2026-01-02T09:00:00,25\n"
        "c,Harbor,South,2026-02-01T09:00:00,7\n",
        encoding="utf-8",
    )
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "retail",
                "namespace": "retail",
                "name": "Retail",
                "warehouse": "duckdb",
                "default_db": "data/retail.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/csv"},
            },
        },
        "graph.yml": {"graph": {"entities": {"store": {"key": ["store_id"], "model": "stores"}}}},
        "models/stores.yml": {
            "model": {
                "id": "stores",
                "relation": "stores",
                "entities": {"store": {}},
                "times": {
                    "reported_at": {"column": "reported_at", "kind": "timestamp", "default": True}
                },
                "dimensions": {
                    "store_id": {"as": STORE_ID, "column": "store_id", "label": "Store id"},
                    "store_name": {"as": STORE_NAME, "column": "store_name", "label": "Store name"},
                    "store_label": {
                        "as": STORE_LABEL,
                        "column": "store_label",
                        "label": "Store label",
                    },
                },
                "measures": {
                    "revenue": {
                        "label": "Revenue",
                        "kind": "aggregate",
                        "expr": "revenue",
                        "default_agg": "sum",
                    }
                },
            }
        },
    }
    for name, body in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


# Filters deliberately mention the requested attributes: their words cannot
# discharge the question's obligation to group by those attributes.
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    ("intent", "dimensions", "filters"),
    [
        ("revenue by store id", [STORE_ID], []),
        ("revenue by store id", [STORE_ID], [ID_FILTER]),
        ("revenue by store name", [STORE_NAME], [NAME_FILTER]),
        (
            "revenue by store label",
            [STORE_LABEL],
            [{"field": STORE_LABEL, "op": "IN", "value": ["East", "West", "South"]}],
        ),
        ("revenue by store", None, []),
        ("revenue by stores", None, [ID_FILTER]),
        ("revenue by, store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by, store name and store id", [STORE_NAME, STORE_ID], [NAME_FILTER, ID_FILTER]),
        ("revenue for each store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by\tstore name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by\nstore name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by\tstore id", [STORE_ID], [ID_FILTER]),
        ("revenue by\nstore id", [STORE_ID], [ID_FILTER]),
        ("revenue per store id", [STORE_ID], [ID_FILTER]),
        ("revenue for each store id", [STORE_ID], [ID_FILTER]),
        ("revenue at the store id level", [STORE_ID], [ID_FILTER]),
        ("revenue by the store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by store name per month", [STORE_NAME], [NAME_FILTER]),
        ("revenue by store name sorted by revenue", [STORE_NAME], [NAME_FILTER]),
        ("revenue by store mystery", [STORE_ID], [ID_FILTER]),
        (
            "revenue by store id and store name",
            [STORE_ID, STORE_NAME],
            [
                {"field": STORE_ID, "op": "IN", "value": ["a", "b"]},
                {"field": STORE_NAME, "op": "IN", "value": ["Central"]},
            ],
        ),
    ],
)
def test_store_attribute_matches_reference_sql_or_withholds_execution(
    retail: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    intent: str,
    dimensions: list[str] | None,
    filters: list[dict[str, Any]],
) -> None:
    _force_fallback(retail, monkeypatch, intent, path)
    payload = plan_payload(retail, intent=intent, partial_query={"where": filters})
    if intent in ANSWERED:
        assert payload["status"] == "ok", payload
    if payload["status"] != "ok":
        assert "execute" not in payload["next"].get("ready_for", []), payload
        return

    assert "execute" in payload["next"].get("ready_for", []), payload
    query = payload["best"]["query_ir"]
    if dimensions is None:
        dimensions = query.get("group_by", [])
        assert len(dimensions) == 1 and dimensions[0] in STORE_COLUMNS, payload
    assert query.get("group_by") == dimensions, payload
    alias = query["select"][0]["as"]
    columns = [STORE_COLUMNS[dimension] for dimension in dimensions]
    result_columns = list(dimensions)
    if "per month" in intent:
        assert query["time"]["grain"] == "month"
        columns.append("DATE_TRUNC('month', reported_at)")
        result_columns.append(query["time"]["temporal_role"] + "__month")
    predicates = []
    parameters = []
    for row in filters:
        predicates.append(
            STORE_COLUMNS[row["field"]] + " IN (" + ",".join("?" for _ in row["value"]) + ")"
        )
        parameters.extend(row["value"])
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    reference_sql = (
        "SELECT "
        + ", ".join(columns)
        + ", SUM(revenue) FROM stores"
        + where
        + " GROUP BY "
        + ", ".join(str(index + 1) for index in range(len(columns)))
    )
    actual = typed_rows(retail.query(query))
    retail.close()
    with duckdb.connect(retail.db_path, read_only=True) as connection:
        expected = [
            dict(zip([*result_columns, alias], row, strict=True))
            for row in connection.execute(reference_sql, parameters).fetchall()
        ]
    assert sorted(actual, key=str) == sorted(expected, key=str)


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    "intent",
    [
        "revenue by the store name",
        "revenue by store name per month",
        "revenue by store name sorted by revenue",
        "revenue by, store name",
        "revenue for each store name",
        "revenue by\tstore name",
        "revenue by\nstore name",
    ],
)
def test_filtered_store_name_matches_reference_sql_or_withholds_execution(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    dimension = "dimension.jaffle_store_name"
    filters = [{"field": dimension, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}]
    try:
        _force_fallback(runtime, monkeypatch, intent, path)
        payload = plan_payload(runtime, intent=intent, partial_query={"where": filters})
        if payload["status"] != "ok":
            assert "execute" not in payload["next"].get("ready_for", []), payload
            return
        query = payload["best"]["query_ir"]
        assert "execute" in payload["next"].get("ready_for", []), payload
        assert query.get("group_by") == [dimension], payload
        alias = query["select"][0]["as"]
        rows = typed_rows(runtime.query(query))
        actual = {row[dimension]: row[alias] for row in rows}
        assert len(actual) == len(rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = dict(
                connection.execute(
                    "SELECT s.store_name, SUM(o.order_total_cents / 100.0) "
                    "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
                    "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1"
                ).fetchall()
            )
        assert actual == pytest.approx(expected)
        assert actual == pytest.approx({"Brooklyn": 259424.85, "Philadelphia": 486468.18})
    finally:
        runtime.close()


# A question that lists stores is answered one row per store, never as one total.
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("intent", ["stores with more than 2000 orders in 2017"])
def test_store_list_is_grouped_by_store_or_withholds_execution(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        _force_fallback(runtime, monkeypatch, intent, path)
        payload = plan_payload(runtime, intent=intent)
        if payload["status"] == "ok" and "execute" in payload["next"].get("ready_for", []):
            group_by = payload["best"]["query_ir"].get("group_by") or []
            assert any(field.startswith("dimension.jaffle_store") for field in group_by), payload
    finally:
        runtime.close()


# Two stores share a name, so a ranking by name merges them into one row.
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("intent", ["top 1 store by revenue", "top 3 stores by revenue"])
def test_store_ranking_with_shared_name_matches_reference_sql_or_withholds_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "UPDATE jaffle_store SET store_name = 'Brooklyn' WHERE store_name = 'Philadelphia'"
        )
    runtime = Runtime.from_path(str(package))
    try:
        _force_fallback(runtime, monkeypatch, intent, path)
        payload = plan_payload(runtime, intent=intent)
        if payload["status"] != "ok":
            assert "execute" not in payload["next"].get("ready_for", []), payload
            return
        assert "execute" in payload["next"].get("ready_for", []), payload
        query = payload["best"]["query_ir"]
        alias = query["select"][0]["as"]
        actual = [row[alias] for row in typed_rows(runtime.query(query))]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = [
                revenue
                for _, _, revenue in connection.execute(
                    "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) AS revenue "
                    "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
                    "GROUP BY s.store_id, s.store_name ORDER BY revenue DESC LIMIT ?",
                    [query["limit"]],
                ).fetchall()
            ]
        assert actual == pytest.approx(expected), payload
    finally:
        runtime.close()
