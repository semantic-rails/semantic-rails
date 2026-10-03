"""A shared grouping asks for a business meaning instead of choosing by score."""

from __future__ import annotations

import json
from dataclasses import replace

import duckdb
import pytest
import yaml

from semantic_rails.planner import generators, plan_payload
from semantic_rails.planner import plan as plan_module
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.test_plan_value_lists import _force_fallback, _with_districts

CUSTOMER = "dimension.customer_district"
STORE = "dimension.store_district"
ITEM_NAME = "dimension.item_name"
CUSTOMER_NAME = "dimension.customer_name"
JAFFLE_STORE = "dimension.jaffle_store_name"
PRODUCT_TYPE = "dimension.jaffle_product_type"


@pytest.fixture()
def shop(tmp_path, request):
    store_district = "districts" if getattr(request, "param", "") == "plural_store" else "district"
    (tmp_path / "models").mkdir()
    (tmp_path / "data").mkdir()
    for entity, csv in {
        "item": "item_id,customer_id,store_id,sold_at,name,revenue\n"
        "1,1,1,2026-01-01,Tea,1\n2,1,2,2026-01-01,Tea,2\n3,2,2,2026-01-01,Cake,3\n",
        "customer": "customer_id,name,district\n1,Pat,north\n2,Pat,south\n",
        "store": f"store_id,{store_district}\n1,north\n2,south\n",
    }.items():
        (tmp_path / "data" / f"{entity}.csv").write_text(csv)
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "shop",
                "namespace": "shop",
                "name": "Shop",
                "warehouse": "duckdb",
                "default_db": "shop.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    entity: {"key": [f"{entity}_id"], "model": entity, "label": entity.title()}
                    for entity in ("item", "customer", "store")
                }
            }
        },
    }
    for entity in ("item", "customer", "store"):
        dimensions = {}
        if entity != "store":
            dimensions["name"] = {
                "as": f"dimension.{entity}_name",
                "label": "Item product name" if entity == "item" else "Customer name",
                "kind": "categorical",
            }
        if entity != "item":
            column = store_district if entity == "store" else "district"
            dimensions[column] = {
                "as": f"dimension.{entity}_district",
                "label": f"{entity.title()} {column}",
                "kind": "categorical",
            }
        model = {
            "id": entity,
            "relation": entity,
            "entities": {
                key: {}
                for key in (("item", "customer", "store") if entity == "item" else (entity,))
            },
            "dimensions": dimensions,
        }
        if entity == "item":
            model["times"] = {"sold_at": {"kind": "date", "default": True}}
            model["measures"] = {
                "item_revenue": {
                    "label": "Item revenue",
                    "kind": "aggregate",
                    "expr": "revenue",
                    "default_agg": "sum",
                    "publish": False,
                }
            }
        files[f"models/{entity}.yml"] = {"model": model}
    for name, body in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(body, sort_keys=False))
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


def _rows_match_reference(runtime, query, dimension, reference, expected):
    assert runtime.validate(query)["ok"]
    rows = runtime.query(query)["rows"]
    alias = query["select"][0]["as"]
    actual = {row[dimension]: row[alias] for row in rows}
    runtime.close()
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        sql_rows = dict(connection.execute(reference).fetchall())
    assert len(actual) == len(rows) == len(sql_rows)
    assert actual == sql_rows == expected


REFERENCES = {
    CUSTOMER: (
        "SELECT c.district, SUM(i.revenue) FROM item i "
        "JOIN customer c ON i.customer_id = c.customer_id GROUP BY 1",
        {"north": 3, "south": 3},
    ),
    STORE: (
        "SELECT s.district, SUM(i.revenue) FROM item i "
        "JOIN store s ON i.store_id = s.store_id GROUP BY 1",
        {"north": 1, "south": 5},
    ),
}


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
@pytest.mark.parametrize(
    "term", ["district", "districts", "the districts", "their districts", "each districts"]
)
def test_shared_grouping_offers_each_visible_meaning_with_reference_rows(
    shop, monkeypatch, path, detail, term
):
    intent = f"item revenue by {term}"
    _force_fallback(shop, monkeypatch, intent, path)
    payload = plan_payload(shop, intent=intent, detail=detail)
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["ambiguous_groupings"] == [term]
    options = payload["why"]["details"]["clarification"]["options"]
    assert [option["id"] for option in options] == [CUSTOMER, STORE]
    assert [option["label"] for option in options] == ["Customer district", "Store district"]
    for option in options:
        query = {
            **payload["best"]["query_ir"],
            "group_by": option["group_by"],
            "where": option["where"],
            "order_by": option["order_by"],
        }
        _rows_match_reference(shop, query, option["id"], *REFERENCES[option["id"]])


@pytest.mark.parametrize("shop", ["plural_store"], indirect=True)
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_strict_match_does_not_hide_a_discovered_meaning(shop, monkeypatch, path, detail):
    intent = "item revenue by district"
    _force_fallback(shop, monkeypatch, intent, path)
    payload = plan_payload(shop, intent=intent, detail=detail)
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["ambiguous_groupings"] == ["district"]
    options = payload["why"]["details"]["clarification"]["options"]
    assert [option["id"] for option in options] == [CUSTOMER, STORE]
    assert [option["label"] for option in options] == ["Customer district", "Store districts"]
    for option in options:
        query = {
            **payload["best"]["query_ir"],
            **{field: option[field] for field in ("group_by", "where", "order_by")},
        }
        reference, expected = REFERENCES[option["id"]]
        if option["id"] == STORE:
            reference = reference.replace("s.district", "s.districts")
        _rows_match_reference(shop, query, option["id"], reference, expected)


@pytest.mark.parametrize("shop", ["plural_store"], indirect=True)
def test_discovery_only_match_does_not_satisfy_an_explicit_grouping(shop):
    query = {
        "version": 2,
        "select": [{"as": "revenue", "expression": {"measure": "measure.shop.item_revenue"}}],
        "group_by": [STORE],
    }
    why = plan_module._dropped_grouping_why(
        shop, "item revenue by district", query, {"group_by": [STORE]}
    )
    assert why is not None
    assert why["code"] == "PLAN_UNMATCHED_TERMS"
    assert why["details"]["terms"] == ["district"]


@pytest.mark.parametrize("term", ["product type", "district"])
def test_multiple_ambiguous_groupings_offer_ids_for_replanning(runtime_factory, monkeypatch, term):
    runtime = runtime_factory("jaffle_shop")
    try:
        if term == "district":
            _with_districts(runtime, monkeypatch)
        payload = plan_payload(runtime, intent=f"order count by name and {term}")
        assert payload["status"] == "low_confidence", payload.get("why")
        assert "execute" not in payload["next"].get("ready_for", [])
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        details = payload["why"]["details"]
        assert details["ambiguous_groupings"] == ["name", term]
        options = details["clarification"]["options"]
        assert {option["term"] for option in options} == {"name", term}
        assert all(set(option) == {"id", "label", "term"} for option in options)
        assert "partial_query.group_by" in payload["why"]["message"]
        assert "partial_query.group_by" in payload["why"]["recovery_hints"][0]["message"]
    finally:
        runtime.close()


@pytest.mark.parametrize("group_by", [[JAFFLE_STORE, PRODUCT_TYPE], [PRODUCT_TYPE, JAFFLE_STORE]])
def test_all_explicit_grouping_choices_preserve_reference_rows(runtime_factory, group_by):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="order count by name and product type",
            partial_query={"group_by": group_by},
        )
        query = payload["best"]["query_ir"]
        assert query["group_by"] == group_by
        assert runtime.validate(query)["ok"]
        rows = runtime.query(query)["rows"]
        alias = query["select"][0]["as"]
        actual = sorted((row[JAFFLE_STORE], row[PRODUCT_TYPE], row[alias]) for row in rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = connection.execute(
                "SELECT s.store_name, p.product_type, COUNT(DISTINCT o.order_id) "
                "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
                "JOIN jaffle_item i ON o.order_id = i.order_id "
                "JOIN jaffle_product p ON i.product_id = p.product_id GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
        assert actual
        assert actual == expected
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
@pytest.mark.parametrize("hidden", [CUSTOMER, STORE])
def test_hidden_candidate_is_neither_counted_nor_named(shop, monkeypatch, path, detail, hidden):
    policy = SemanticPolicyConfig(
        id="policy.hide_district",
        kind="object_visibility",
        object_ids=[hidden],
        action="hidden",
    )
    monkeypatch.setattr(shop, "_config", replace(shop._config, semantic_policies=[policy]))
    intent = "item revenue by district"
    _force_fallback(shop, monkeypatch, intent, path)
    payload = plan_payload(shop, intent=intent, detail=detail)
    text = json.dumps(payload).lower()
    assert hidden not in text
    assert ("customer district" if hidden == CUSTOMER else "store district") not in text
    assert payload["status"] == "ok", payload.get("why")
    if detail != "query":
        assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    visible = STORE if hidden == CUSTOMER else CUSTOMER
    assert query["group_by"] == [visible]
    _rows_match_reference(shop, query, visible, *REFERENCES[visible])


def test_fallback_clarification_is_not_capped_by_the_discovery_shortlist(shop, monkeypatch):
    source = next(row for row in shop._config.dimensions if row.id == STORE)
    copies = [replace(source, id=f"dimension.other_district_{index}") for index in range(6)]
    monkeypatch.setattr(
        shop, "_config", replace(shop._config, dimensions=[*shop._config.dimensions, *copies])
    )
    intent = "item revenue by their districts"
    _force_fallback(shop, monkeypatch, intent, "fallback")
    payload = plan_payload(shop, intent=intent)
    options = payload["why"]["details"]["clarification"]["options"]
    assert {option["id"] for option in options} == {CUSTOMER, STORE, *(row.id for row in copies)}
    assert payload["status"] == "low_confidence"


@pytest.mark.parametrize("hidden", [CUSTOMER, STORE])
@pytest.mark.parametrize("term", ["districts", "the districts"])
def test_sole_visible_fallback_plural_matches_reference_rows(shop, monkeypatch, hidden, term):
    policy = SemanticPolicyConfig(
        id="policy.hide_district", kind="object_visibility", object_ids=[hidden], action="hidden"
    )
    monkeypatch.setattr(shop, "_config", replace(shop._config, semantic_policies=[policy]))
    intent = f"item revenue by {term}"
    _force_fallback(shop, monkeypatch, intent, "fallback")
    payload = plan_payload(shop, intent=intent)
    assert payload["status"] == "ok", payload.get("why")
    visible = STORE if hidden == CUSTOMER else CUSTOMER
    assert payload["best"]["query_ir"]["group_by"] == [visible]
    assert hidden not in json.dumps(payload)
    _rows_match_reference(shop, payload["best"]["query_ir"], visible, *REFERENCES[visible])


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("dimension", [ITEM_NAME, CUSTOMER_NAME])
def test_root_owned_match_is_required_even_for_a_bypassing_draft(
    shop, monkeypatch, path, dimension
):
    intent = "item revenue by name"
    result = plan_module.compose(shop, intent)
    if path == "primary":
        draft = replace(
            result.draft, query={**result.draft.query, "group_by": [dimension], "order_by": []}
        )
        monkeypatch.setattr(plan_module, "compose", lambda *args: replace(result, draft=draft))
    else:
        _force_fallback(shop, monkeypatch, intent, path)
        monkeypatch.setattr(generators, "_choose_group_dimensions", lambda *args: [dimension])
    payload = plan_payload(shop, intent=intent)
    assert payload["best"]["query_ir"]["group_by"] == [dimension]
    if dimension == CUSTOMER_NAME:
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert "execute" not in payload["next"].get("ready_for", [])
    else:
        assert payload["status"] == "ok", payload.get("why")
        _rows_match_reference(
            shop,
            payload["best"]["query_ir"],
            dimension,
            "SELECT name, SUM(revenue) FROM item GROUP BY 1",
            {"Cake": 3, "Tea": 3},
        )


def test_options_preserve_the_callers_where(shop):
    where = [{"field": STORE, "op": "in", "value": ["north", "south"]}]
    payload = plan_payload(shop, intent="item revenue by district", partial_query={"where": where})
    for option in payload["why"]["details"]["clarification"]["options"]:
        assert option["where"] == where
        query = {
            **payload["best"]["query_ir"],
            "group_by": option["group_by"],
            "where": option["where"],
            "order_by": option["order_by"],
        }
        _rows_match_reference(shop, query, option["id"], *REFERENCES[option["id"]])
