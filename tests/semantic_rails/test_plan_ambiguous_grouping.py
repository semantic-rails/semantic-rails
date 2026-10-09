"""A shared grouping asks for a business meaning instead of choosing by score."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import duckdb
import pytest
import yaml

from semantic_rails.planner import generators, grouping_checks, plan_payload
from semantic_rails.planner import plan as plan_module
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.test_plan_value_lists import _force_fallback, _with_districts

CUSTOMER = "dimension.customer_district"
STORE = "dimension.store_district"
ITEM_NAME = "dimension.item_name"
CUSTOMER_NAME = "dimension.customer_name"
JAFFLE_STORE = "dimension.jaffle_store_name"
JAFFLE_CUSTOMER = "dimension.jaffle_customer_name"
PRODUCT_TYPE = "dimension.jaffle_product_type"
ITEM_PRODUCT_TYPE = "dimension.jaffle_item_product_type"
ITEM_PRODUCT_NAME = "dimension.jaffle_item_product_name"


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
        "version": 1,
        "select": [{"as": "revenue", "expression": {"measure": "measure.shop.item_revenue"}}],
        "group_by": [STORE],
    }
    why = grouping_checks._dropped_grouping_why(
        shop, "item revenue by district", query, {"group_by": [STORE]}
    )
    assert why is not None
    assert why["code"] == "PLAN_UNMATCHED_TERMS"
    assert why["details"]["terms"] == ["district"]


@pytest.mark.parametrize("term", ["product type", "district"])
def test_multiple_ambiguous_grouping_options_compose(runtime_factory, tmp_path, monkeypatch, term):
    if term == "district":
        package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
        with duckdb.connect(str(package / "jaffle_shop.duckdb")) as connection:
            connection.execute("ALTER TABLE jaffle_store ADD COLUMN district VARCHAR")
            connection.execute(
                "UPDATE jaffle_store SET district = "
                "CASE WHEN store_name IN ('Brooklyn', 'Philadelphia') THEN 'east' ELSE 'west' END"
            )
        runtime = Runtime.from_path(str(package))
    else:
        runtime = runtime_factory("jaffle_shop")
    try:
        if term == "district":
            _with_districts(runtime, monkeypatch)
        where = [{"field": JAFFLE_STORE, "op": "in", "value": ["Brooklyn", "Philadelphia"]}]
        kept_sort = {"field": "order_count", "direction": "desc"}
        payload = plan_payload(
            runtime,
            intent=f"order count by name and {term}",
            partial_query={
                "where": where,
                "order_by": [
                    {"field": "dimension.jaffle_customer_name", "direction": "asc"},
                    kept_sort,
                ],
            },
        )
        assert payload["status"] == "low_confidence", payload.get("why")
        assert "execute" not in payload["next"].get("ready_for", [])
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        details = payload["why"]["details"]
        assert details["ambiguous_groupings"] == ["name", term]
        options = details["clarification"]["options"]
        assert {option["term"] for option in options} == {"name", term}
        draft = payload["best"]["query_ir"]
        for option in options:
            assert set(option) == {"id", "label", "term", "replaces"}
            assert option["term"] in details["ambiguous_groupings"]
            meanings = {other["id"] for other in options if other["term"] == option["term"]}
            assert option["replaces"] == [item for item in draft["group_by"] if item in meanings]
        for message in (payload["why"]["message"], payload["why"]["recovery_hints"][0]["message"]):
            for field in ("best.query_ir", "replaces", "group_by", "order_by", "id", "validate"):
                assert field in message
            assert "partial_query.group_by" not in message
        chosen_ids = [JAFFLE_STORE, PRODUCT_TYPE if term == "product type" else STORE]
        chosen = [next(option for option in options if option["id"] == item) for item in chosen_ids]
        queries = []
        for choices in (chosen, chosen[::-1]):
            query = deepcopy(draft)
            for option in choices:
                replaced = set(option["replaces"])
                query["group_by"] = sorted((set(query["group_by"]) - replaced) | {option["id"]})
                query["order_by"] = [
                    item for item in query.get("order_by", []) if item["field"] not in replaced
                ]
            assert runtime.validate(query)["ok"]
            queries.append(query)
        assert queries[0] == queries[1]
        assert queries[0]["group_by"] == sorted(chosen_ids)
        assert queries[0]["where"] == draft["where"] == where
        assert queries[0]["order_by"] == [kept_sort]
        query = queries[0]
        rows = runtime.query(query)["rows"]
        alias = query["select"][0]["as"]
        actual = sorted((row[chosen_ids[0]], row[chosen_ids[1]], row[alias]) for row in rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            reference = (
                "SELECT s.store_name, p.product_type, COUNT(DISTINCT o.order_id) "
                "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
                "JOIN jaffle_item i ON o.order_id = i.order_id "
                "JOIN jaffle_product p ON i.sku = p.sku "
                "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1, 2 ORDER BY 1, 2"
                if term == "product type"
                else "SELECT s.store_name, s.district, COUNT(DISTINCT o.order_id) "
                "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
                "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1, 2 ORDER BY 1, 2"
            )
            expected = connection.execute(reference).fetchall()
        assert actual
        assert actual == expected
    finally:
        runtime.close()


def _assert_no_grouping_options(why, ambiguous):
    assert why["code"] == "PLAN_UNMATCHED_TERMS"
    assert why["details"]["ambiguous_groupings"] == ambiguous
    assert "clarification" not in why["details"]
    text = json.dumps(why)
    assert '"replaces"' not in text
    assert '"group_by"' not in text
    hint = why["recovery_hints"][0]
    assert hint["kind"] == "clarify_grouping"
    assert "ask the user" in hint["message"]
    assert "replaces" not in why["message"] + hint["message"]


@pytest.mark.parametrize(
    ("path", "intent", "ambiguous"),
    [
        # The draft groups by customer and item product name; "name" could replace both.
        ("primary", "order count by name and product name", ["name", "product name"]),
        # "name" could replace the store-name grouping "store name" settles.
        ("primary", "order count by store name and name", ["name"]),
        ("fallback", "order count by store name and name", ["name"]),
    ],
)
def test_terms_that_could_replace_the_same_grouping_offer_no_options(
    runtime_factory, monkeypatch, path, intent, ambiguous
):
    runtime = runtime_factory("jaffle_shop")
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent)
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    _assert_no_grouping_options(payload["why"], ambiguous)


def test_fallback_draft_without_overlap_keeps_composable_options(runtime_factory, monkeypatch):
    runtime = runtime_factory("jaffle_shop")
    intent = "order count by name and product name"
    _force_fallback(runtime, monkeypatch, intent, "fallback")
    payload = plan_payload(runtime, intent=intent)
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "execute" not in payload["next"].get("ready_for", [])
    # Only "name" has a draft grouping to replace, so the choices can't overwrite each other.
    draft = payload["best"]["query_ir"]
    assert draft["group_by"] == [JAFFLE_CUSTOMER]
    options = payload["why"]["details"]["clarification"]["options"]
    chosen = [
        next(option for option in options if (option["term"], option["id"]) == pick)
        for pick in (("name", JAFFLE_STORE), ("product name", ITEM_PRODUCT_NAME))
    ]
    assert [option["replaces"] for option in chosen] == [[JAFFLE_CUSTOMER], []]
    queries = []
    for choices in (chosen, chosen[::-1]):
        query = deepcopy(draft)
        for option in choices:
            replaced = set(option["replaces"])
            query["group_by"] = sorted((set(query["group_by"]) - replaced) | {option["id"]})
            query["order_by"] = [
                item for item in query.get("order_by", []) if item["field"] not in replaced
            ]
        queries.append(query)
    assert queries[0] == queries[1]
    assert queries[0]["group_by"] == [ITEM_PRODUCT_NAME, JAFFLE_STORE]
    assert runtime.validate(queries[0])["ok"]
    rows = runtime.query(queries[0])["rows"]
    alias = queries[0]["select"][0]["as"]
    actual = sorted((row[JAFFLE_STORE], row[ITEM_PRODUCT_NAME], row[alias]) for row in rows)
    runtime.close()
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        expected = connection.execute(
            "SELECT s.store_name, i.product_name, COUNT(DISTINCT o.order_id) "
            "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
            "JOIN jaffle_item i ON o.order_id = i.order_id GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    assert actual
    assert actual == expected


def test_settled_grouping_overlap_offers_no_options_for_several_terms(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    intent = "order count by store name, name and product type"
    draft = plan_payload(runtime, intent=intent)["best"]["query_ir"]
    query = {**draft, "group_by": [JAFFLE_STORE, JAFFLE_CUSTOMER, ITEM_PRODUCT_TYPE]}
    assert runtime.validate(query)["ok"]
    why = grouping_checks._dropped_grouping_why(runtime, intent, query)
    _assert_no_grouping_options(why, ["name", "product type"])


@pytest.mark.parametrize("shop", ["plural_store"], indirect=True)
@pytest.mark.parametrize(
    ("term", "changes", "ambiguous"),
    [
        ("district", {"label": "District"}, False),
        ("district", {"id": "dimension.district"}, False),
        ("customer_district", {"label": "Customer District"}, False),
        ("customer district", {"label": "Customer districts"}, False),
        ("district", {}, True),
    ],
    ids=["whole-label", "whole-id", "underscored-term", "underscored-id", "column-only"],
)
def test_discovery_cannot_widen_a_whole_dimension_name(shop, monkeypatch, term, changes, ambiguous):
    customer = next(row for row in shop._config.dimensions if row.id == CUSTOMER)
    customer = replace(customer, **changes)
    monkeypatch.setattr(
        shop,
        "_config",
        replace(
            shop._config,
            dimensions=[customer, *[row for row in shop._config.dimensions if row.id != CUSTOMER]],
        ),
    )
    monkeypatch.setattr(grouping_checks, "_grouping_term_matches", lambda *args, **kwargs: [STORE])
    query = {
        "version": 1,
        "select": [{"as": "revenue", "expression": {"measure": "measure.shop.item_revenue"}}],
        "group_by": [customer.id],
    }
    why = grouping_checks._dropped_grouping_why(shop, f"item revenue by {term}", query)
    if ambiguous:
        assert why["code"] == "PLAN_UNMATCHED_TERMS"
        assert why["details"]["ambiguous_groupings"] == [term]
    else:
        assert why is None


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


@pytest.mark.parametrize("hidden", [CUSTOMER, STORE])
def test_an_alias_ambiguous_only_through_a_hidden_dimension_answers_the_visible_one(
    shop, monkeypatch, hidden
):
    """Both districts answer to "district". A caller who cannot see one of them is answered
    by the other, as the package without it would; everyone else still gets the question."""
    policy = SemanticPolicyConfig(
        id="policy.hide_district",
        kind="object_visibility",
        object_ids=[hidden],
        action="hidden",
        audiences=["external"],
    )
    dimensions = [
        replace(row, aliases=[*row.aliases, "district"]) if row.id in {CUSTOMER, STORE} else row
        for row in shop._config.dimensions
    ]
    monkeypatch.setattr(
        shop, "_config", replace(shop._config, dimensions=dimensions, semantic_policies=[policy])
    )
    north = {"all": [{"dimension": "district", "op": "=", "value": "north"}]}
    revenue = {"kind": "aggregate", "measure": "measure.shop.item_revenue", "filter": north}
    query = {"select": [{"as": "revenue", "expression": revenue}]}
    assert shop.validate(query)["errors"][0]["code"] == "AMBIGUOUS_ALIAS"
    external = {**query, "policy_context": {"audience": "external"}}
    assert shop.validate(external)["ok"] is True
    rows = shop.query(external)["rows"]
    shop.close()
    owner = "store" if hidden == CUSTOMER else "customer"
    reference = (
        f"SELECT SUM(i.revenue) FROM item i JOIN {owner} o ON i.{owner}_id = o.{owner}_id "
        "WHERE o.district = 'north'"
    )
    with duckdb.connect(shop.db_path, read_only=True) as connection:
        (expected,) = connection.execute(reference).fetchone()
    assert expected == (1 if owner == "store" else 3)
    assert [float(row["revenue"]) for row in rows] == [expected]


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
