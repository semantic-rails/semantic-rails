"""Every listed grouping has its own matching dimension before plan is ready.

The invariant: every grouping the question lists that isn't a clock term ("by month", "by order
date") or a declared value has a dimension in the draft's group_by, or plan doesn't call the draft
ready. The check reads a comma as "and" does, so "by incident name, incident" lists two; the draft
still stops at the comma. Two incidents can share a name; grouping by the name alone would add
their costs into one row. A grouping that names an entity has only that entity's key dimension,
or its single declared dimension whose own words name it; an entity with a composite key has
none. The check only holds a plan: it never changes a draft, nor readies one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.groupings import _listed_grouping_terms
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.planner.unmatched_words import unconsumed_catalog_words
from semantic_rails.runtime import Runtime
from semantic_rails.schema import DimensionConfig, SemanticPolicyConfig
from tests.semantic_rails.result_helpers import assert_plan_held, typed_rows
from tests.semantic_rails.test_plan_catalog_words import _with_store_dimensions
from tests.semantic_rails.test_plan_value_lists import (
    STORE_DISTRICT,
    _force_fallback,
    _with_districts,
)

INCIDENT_ID = "dimension.upkeep_incident_incident_id"
INCIDENT_NAME = "dimension.upkeep_incident_incident_name"
REVISION = "dimension.upkeep_incident_revision"
STORE = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER_TIME = "temporal_role.jaffle_order_time"
HAS_FOOD_ITEM = "dimension.jaffle_order_has_food_item"
IS_LARGE_ORDER = "dimension.jaffle_order_is_large_order"
CUSTOMER_NAME = "dimension.jaffle_customer_name"
ITEM_PRODUCT_TYPE = "dimension.jaffle_item_product_type"
PRODUCT_TYPE = "dimension.jaffle_product_type"


def _upkeep(path: Path, noun: str, measure: str, *, revisions: bool = False) -> Runtime:
    """An entity ``noun`` labelled ``noun.title()``, with dimensions "<Noun> id" and "<Noun>
    name", a measure "<Measure> cost" summing ``<measure>_cost``, and two rows that share a name:
    (1, "Leak", 10) and (2, "Leak", 20). With ``revisions``, the key is (id, revision) and the
    rows are revisions 1 and 2 of entity 1."""

    title = noun.title()
    (path / "models").mkdir(parents=True)
    (path / "data" / "csv").mkdir(parents=True)
    (path / "data" / "csv" / f"{noun}s.csv").write_text(
        (
            f"{noun}_id,revision,{noun}_name,reported_at,{measure}_cost\n"
            "1,1,Leak,2026-01-01T09:00:00,10\n"
            "1,2,Leak,2026-01-02T09:00:00,20\n"
            if revisions
            else f"{noun}_id,{noun}_name,reported_at,{measure}_cost\n"
            "1,Leak,2026-01-01T09:00:00,10\n"
            "2,Leak,2026-01-02T09:00:00,20\n"
        ),
        encoding="utf-8",
    )
    key = [f"{noun}_id", "revision"] if revisions else [f"{noun}_id"]
    revision = {"revision": {"column": "revision", "label": "Revision"}} if revisions else {}
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "upkeep",
                "namespace": "upkeep",
                "name": "upkeep",
                "warehouse": "duckdb",
                "default_db": "data/upkeep.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/csv"},
            },
        },
        "graph.yml": {
            "graph": {"entities": {noun: {"key": key, "model": f"{noun}s", "label": title}}}
        },
        f"models/{noun}s.yml": {
            "model": {
                "id": f"{noun}s",
                "relation": f"{noun}s",
                "entities": {noun: {}},
                "times": {
                    "reported_at": {"column": "reported_at", "kind": "timestamp", "default": True}
                },
                "dimensions": {
                    f"{noun}_id": {"column": f"{noun}_id", "label": f"{title} id"},
                    f"{noun}_name": {"column": f"{noun}_name", "label": f"{title} name"},
                    **revision,
                },
                "measures": {
                    f"{measure}_cost": {
                        "label": f"{measure.title()} cost",
                        "kind": "aggregate",
                        "expr": f"{measure}_cost",
                        "default_agg": "sum",
                    }
                },
            }
        },
    }
    for name, body in files.items():
        (path / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return Runtime.from_path(str(path))


@pytest.fixture()
def upkeep(tmp_path: Path) -> Iterator[Callable[..., Runtime]]:
    opened: list[Runtime] = []

    def build(noun: str, measure: str, *, revisions: bool = False) -> Runtime:
        opened.append(_upkeep(tmp_path / noun, noun, measure, revisions=revisions))
        return opened[-1]

    try:
        yield build
    finally:
        for runtime in opened:
            runtime.close()


@pytest.fixture()
def jaffle(runtime_factory: Any) -> Iterator[Runtime]:
    runtime = runtime_factory("jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


def _incident_reference() -> list[tuple[int, str, float]]:
    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TABLE incidents AS SELECT * FROM (VALUES "
            "(1, 'Leak', 10), (2, 'Leak', 20)) AS t(incident_id, incident_name, repair_cost)"
        )
        rows = connection.execute(
            "SELECT incident_id, incident_name, SUM(repair_cost) FROM incidents "
            "GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
    finally:
        connection.close()
    return [(int(key), str(name), float(cost)) for key, name, cost in rows]


@pytest.mark.parametrize(
    ("intent", "group_by"),
    [
        ("repair cost by incident name and incident", [INCIDENT_NAME, INCIDENT_ID]),
        ("repair cost by incident and incident name", [INCIDENT_ID, INCIDENT_NAME]),
    ],
)
def test_two_listed_groupings_never_add_up_one_name(
    upkeep: Callable[[str, str], Runtime], intent: str, group_by: list[str]
) -> None:
    runtime = upkeep("incident", "repair")
    payload = plan_payload(runtime, intent=intent)

    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    assert query["group_by"] == group_by
    rows = [
        (int(row[INCIDENT_ID]), str(row[INCIDENT_NAME]), float(row["repair_cost"]))
        for row in typed_rows(runtime.query(query))
    ]
    assert sorted(rows) == _incident_reference()


@pytest.mark.parametrize(
    ("intent", "group_by", "dropped"),
    [
        # The draft stops at the comma and groups by the name alone; the check reads on, so the
        # incident it dropped holds the plan.
        ("repair cost by incident name, incident", [INCIDENT_NAME], ["incident"]),
        # The draft groups by the key alone, leaving "name" over.
        ("repair cost by incident, incident name", [INCIDENT_ID], ["incident name"]),
    ],
)
def test_a_grouping_after_a_comma_the_draft_drops_holds_the_plan(
    upkeep: Callable[[str, str], Runtime], intent: str, group_by: list[str], dropped: list[str]
) -> None:
    runtime = upkeep("incident", "repair")
    payload = plan_payload(runtime, intent=intent)

    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == dropped
    query = payload["best"]["query_ir"]
    assert query["group_by"] == group_by
    if group_by == [INCIDENT_NAME]:
        # Run anyway, it would add both incidents into one row.
        assert typed_rows(runtime.query(query)) == [{INCIDENT_NAME: "Leak", "repair_cost": 30}]


def test_a_draft_that_drops_a_listed_grouping_is_not_ready(
    upkeep: Callable[[str, str], Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = upkeep("incident", "repair")
    intent = "repair cost by incident name, incident"
    # Every word of the question is consumed: "incident" by Incident name's own words. Only the
    # one-to-one correspondence shows that the draft drops one.
    draft = RuntimeCompositionDraft(
        query={
            "version": 1,
            "select": [
                {"as": "repair_cost", "expression": {"metric": "metric.upkeep.repair_cost"}}
            ],
            "group_by": [INCIDENT_NAME],
        },
        resolved=[],
        rationale=[],
        interpreted_intent={},
    )
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, text: CompositionResult(
            intent_ir=parse_intent(runtime, text), draft=draft, pattern="test"
        ),
    )
    assert unconsumed_catalog_words(runtime, intent, draft.query) == []

    payload = plan_payload(runtime, intent=intent)

    assert payload["status"] == "low_confidence"
    assert payload["best"]["validation_ok"] is True
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"] == {
        "terms": ["incident"],
        "dropped_groupings": ["incident"],
    }
    # Run anyway, it would add both incidents into one row.
    assert typed_rows(runtime.query(draft.query)) == [{INCIDENT_NAME: "Leak", "repair_cost": 30}]


def test_an_unrelated_dimension_never_masks_a_dropped_order_grouping(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="order count by customer type, order for Brooklyn store")
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            "SELECT c.customer_type, o.order_id, COUNT(DISTINCT o.order_id) "
            "FROM jaffle_order o "
            "JOIN jaffle_customer c ON o.customer_id = c.customer_id "
            "JOIN jaffle_store s ON o.store_id = s.store_id "
            "WHERE s.store_name = 'Brooklyn' GROUP BY 1, 2"
        ).fetchall()
    finally:
        connection.close()
    assert len(reference) == 21_465
    assert {row[2] for row in reference} == {1}
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["order"]
    assert payload["best"]["query_ir"]["group_by"] == [CUSTOMER_TYPE]
    assert len(typed_rows(jaffle.query(payload["best"]["query_ir"]))) == 2


def test_a_forced_store_grouping_cannot_stand_in_for_order(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = "order count by customer type, order for Brooklyn store"
    draft = RuntimeCompositionDraft(
        query={
            "version": 1,
            "select": [
                {"as": "order_count", "expression": {"measure": "measure.jaffle.order_count"}}
            ],
            "group_by": [STORE, CUSTOMER_TYPE],
            "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}],
        },
        resolved=[],
        rationale=[],
        interpreted_intent={},
    )
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, text: CompositionResult(
            intent_ir=parse_intent(runtime, text), draft=draft, pattern="test"
        ),
    )
    assert unconsumed_catalog_words(jaffle, intent, draft.query) == []
    payload = plan_payload(jaffle, intent=intent)
    assert payload["best"]["validation_ok"] is True
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["order"]


# "Has food item" names the order only in its id; "Is large order" names it in its label, but so
# do two other Order dimensions. Neither is the order's key, so neither is one row per order.
@pytest.mark.parametrize("dimension", [HAS_FOOD_ITEM, IS_LARGE_ORDER])
def test_a_non_key_order_dimension_cannot_stand_in_for_order(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, dimension: str
) -> None:
    intent = "order count by customer type, order"
    draft = RuntimeCompositionDraft(
        query={
            "version": 1,
            "select": [
                {"as": "order_count", "expression": {"measure": "measure.jaffle.order_count"}}
            ],
            "group_by": [CUSTOMER_TYPE, dimension],
        },
        resolved=[],
        rationale=[],
        interpreted_intent={},
    )
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, text: CompositionResult(
            intent_ir=parse_intent(runtime, text), draft=draft, pattern="test"
        ),
    )
    assert unconsumed_catalog_words(jaffle, intent, draft.query) == []
    payload = plan_payload(jaffle, intent=intent)
    assert payload["best"]["validation_ok"] is True
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["order"]


def test_a_customer_history_grouping_needs_its_whole_key(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="order count by customer history, month")
    # Customer history's key is (customer_id, valid_from). The reference keeps that identity and
    # the declared validity join; the draft groups by valid_from alone.
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            "SELECT h.customer_id, h.valid_from, DATE_TRUNC('month', o.ordered_at), "
            "COUNT(DISTINCT o.order_id) FROM jaffle_order o "
            "LEFT JOIN jaffle_customer_history h ON o.customer_id = h.customer_id "
            "AND o.ordered_at >= h.valid_from "
            "AND (o.ordered_at < h.valid_to OR h.valid_to IS NULL) GROUP BY 1, 2, 3"
        ).fetchall()
    finally:
        connection.close()
    assert len(reference) == 60
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["customer history"]
    query = payload["best"]["query_ir"]
    dimension = "dimension.jaffle_customer_history_valid_from"
    assert query["group_by"] == [dimension]
    assert query["time"] == {"temporal_role": ORDER_TIME, "grain": "month"}
    # Even a held draft reads the validity join; compare its incomplete grouping to the
    # reference collapsed over customer id, while keeping the whole-identity hold above.
    expected = {}
    for _, valid_from, month, count in reference:
        key = (valid_from, month)
        expected[key] = expected.get(key, 0) + count
    assert {
        (row[dimension], row[f"{ORDER_TIME}__month"]): row["order_count"]
        for row in typed_rows(jaffle.query(query))
    } == expected


def test_a_composite_key_entity_grouping_is_never_ready(
    upkeep: Callable[..., Runtime],
) -> None:
    runtime = upkeep("incident", "repair", revisions=True)
    intent = "repair cost by incident name, incident"
    payload = plan_payload(runtime, intent=intent)

    # Incident 1 has two revisions: two incidents by the declared key, costing 10 and 20.
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["incident"]
    # Neither the id alone nor the id with the revision satisfies the guard.
    for group_by in ([INCIDENT_NAME, INCIDENT_ID], [INCIDENT_NAME, INCIDENT_ID, REVISION]):
        why = plan_module._dropped_grouping_why(runtime, intent, {"group_by": group_by})
        assert why is not None
        assert why["details"]["dropped_groupings"] == ["incident"]
    assert plan_payload(runtime, intent="repair cost by incident name and incident")["why"][
        "details"
    ]["dropped_groupings"] == ["incident"]


def test_a_window_inside_the_list_never_drops_a_later_grouping(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="revenue by store, last month and customer type")

    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["customer type"]
    assert payload["best"]["query_ir"]["group_by"] == [
        "dimension.jaffle_customer_history_preferred_store_id"
    ]


@pytest.mark.parametrize(
    ("intent", "details"),
    [
        # Listed with "and", the draft picks Customer name, and the check holds the pick.
        (
            "order count by month and name",
            {"terms": ["name"], "dropped_groupings": [], "ambiguous_groupings": ["name"]},
        ),
        (
            "order count by name",
            {"terms": ["name"], "dropped_groupings": [], "ambiguous_groupings": ["name"]},
        ),
        # After a comma the draft has no name grouping; it still offers the shared meanings.
        (
            "order count by month, name",
            {"terms": ["name"], "dropped_groupings": [], "ambiguous_groupings": ["name"]},
        ),
    ],
)
def test_a_grouping_naming_dimensions_of_other_entities_is_never_a_pick(
    jaffle: Runtime, intent: str, details: dict[str, list[str]]
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    # "Name" is Customer name, Store name, Product name and more, none of them the order's own.
    # Each is a defensible reading with its own rows, so plan holds rather than picking one.
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert {
        key: value for key, value in payload["why"]["details"].items() if key != "clarification"
    } == details


@pytest.mark.parametrize(
    ("measure", "term", "group_by", "chosen", "ambiguous"),
    [
        # Item product type is the item's own, so an item count by product type reads it.
        ("item_count", "product type", [ITEM_PRODUCT_TYPE], [], False),
        # For an order count, Item product type and Product type are two other entities'.
        ("order_count", "product type", [ITEM_PRODUCT_TYPE], [], True),
        ("order_count", "product type", [PRODUCT_TYPE], [], True),
        ("order_count", "name", [CUSTOMER_NAME], [], True),
        ("order_count", "name", [STORE], [], True),
        # Named with its entity, a grouping has one reading.
        ("order_count", "customer name", [CUSTOMER_NAME], [], False),
        ("order_count", "store name", [STORE], [], False),
        # The caller's own group_by says which, unless the draft adds another reading.
        ("order_count", "name", [STORE], [STORE], False),
        ("order_count", "name", [STORE, CUSTOMER_NAME], [STORE], True),
        ("order_count", "name", [CUSTOMER_NAME], [STORE], True),
    ],
)
def test_only_the_measures_own_entity_or_the_caller_settles_a_shared_grouping(
    jaffle: Runtime,
    measure: str,
    term: str,
    group_by: list[str],
    chosen: list[str],
    ambiguous: bool,
) -> None:
    query = {
        "version": 1,
        "select": [{"as": measure, "expression": {"measure": f"measure.jaffle.{measure}"}}],
        "group_by": group_by,
    }
    why = plan_module._dropped_grouping_why(
        jaffle, f"{measure} by {term}", query, {"group_by": chosen}
    )
    assert (why is not None) is ambiguous
    if why:
        assert why["code"] == "PLAN_UNMATCHED_TERMS"
        assert {key: value for key, value in why["details"].items() if key != "clarification"} == {
            "terms": [term],
            "dropped_groupings": [],
            "ambiguous_groupings": [term],
        }


@pytest.mark.parametrize(
    ("intent", "partial_query", "dimension", "column", "join"),
    [
        (
            "order count by month and customer name",
            None,
            CUSTOMER_NAME,
            "c.customer_name",
            "LEFT JOIN jaffle_customer c ON o.customer_id = c.customer_id",
        ),
        (
            "order count by month and store name",
            None,
            STORE,
            "s.store_name",
            "LEFT JOIN jaffle_store s ON o.store_id = s.store_id",
        ),
        # The caller says whose name, and the draft adds no other.
        (
            "order count by month, name",
            {"group_by": [CUSTOMER_NAME]},
            CUSTOMER_NAME,
            "c.customer_name",
            "LEFT JOIN jaffle_customer c ON o.customer_id = c.customer_id",
        ),
    ],
)
def test_a_qualified_name_grouping_stays_ready(
    jaffle: Runtime,
    intent: str,
    partial_query: dict[str, Any] | None,
    dimension: str,
    column: str,
    join: str,
) -> None:
    payload = plan_payload(jaffle, intent=intent, partial_query=partial_query)

    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    assert query["group_by"] == [dimension]
    assert query["time"] == {"temporal_role": ORDER_TIME, "grain": "month"}
    bucket = f"{ORDER_TIME}__month"
    rows = sorted(
        (str(row[dimension]), str(row[bucket])[:10], int(row["order_count"]))
        for row in typed_rows(jaffle.query(query))
    )
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            f"SELECT {column}, CAST(DATE_TRUNC('month', o.ordered_at) AS DATE), "
            f"COUNT(DISTINCT o.order_id) FROM jaffle_order o {join} GROUP BY ALL"
        ).fetchall()
    finally:
        connection.close()
    assert rows
    assert rows == sorted((str(name), str(month), int(count)) for name, month, count in reference)


@pytest.mark.parametrize(
    ("term", "group_by", "matched"),
    [
        # The entity's key dimension.
        ("store", ["dimension.jaffle_store_id"], True),
        ("order", ["dimension.jaffle_order_id"], True),
        ("customer", ["dimension.jaffle_customer_id"], True),
        # The entity's one declared dimension whose own words name it; a clock, such as Store
        # opened at, is the time block's and never stands in.
        ("store", [STORE], True),
        ("stores", [STORE], True),
        ("supply", ["dimension.jaffle_supply_name"], True),
        ("store", ["dimension.jaffle_store_opened_at"], False),
        # One of several: Customer name and Customer type both name the customer.
        ("customer", ["dimension.jaffle_customer_name"], False),
        ("order", [IS_LARGE_ORDER], False),
        ("order", [HAS_FOOD_ITEM], False),
        # Another entity's dimension, even one on the same column.
        ("customer", ["dimension.jaffle_order_customer_id"], False),
        ("customer", ["dimension.jaffle_customer_history_customer_id"], False),
        ("order", ["dimension.jaffle_order_lifecycle_order_id"], False),
        ("item", ["dimension.jaffle_order_has_drink_item"], False),
        ("product", ["dimension.jaffle_item_product_name"], False),
        # A term naming part of an entity's label names no entity whole: "customer" is not
        # Customer segment membership, whose key would split by membership.
        ("customer", ["dimension.jaffle_customer_segment_membership_membership_id"], False),
        ("customer segment", ["dimension.jaffle_customer_history_segment"], False),
        # A composite key: no dimension, nor the whole key, satisfies the guard.
        ("customer history", ["dimension.jaffle_customer_history_customer_id"], False),
        (
            "customer history",
            [
                "dimension.jaffle_customer_history_customer_id",
                "dimension.jaffle_customer_history_valid_from",
            ],
            False,
        ),
        # A term naming no entity matches a dimension by its own words.
        ("store name", [STORE], True),
        ("customer type", [CUSTOMER_TYPE], True),
    ],
)
def test_only_an_entitys_key_or_single_named_dimension_stands_in_for_it(
    jaffle: Runtime, term: str, group_by: list[str], matched: bool
) -> None:
    why = plan_module._dropped_grouping_why(jaffle, f"revenue by {term}", {"group_by": group_by})
    assert (why is None) is matched
    if why:
        assert why["code"] == "PLAN_UNMATCHED_TERMS"
        assert why["details"]["dropped_groupings"] == [term]


@pytest.mark.parametrize(
    ("intent", "status", "groups", "window"),
    [
        ("revenue by store, last month", "ok", [STORE], "month"),
        ("revenue by month, last year", "ok", [], "year"),
        ("revenue by store, sorted by revenue", "low_confidence", [STORE], None),
    ],
)
def test_a_comma_before_a_trailing_clause_keeps_the_original_plan(
    jaffle: Runtime, intent: str, status: str, groups: list[str], window: str | None
) -> None:
    payload = plan_payload(jaffle, intent=intent)
    if intent in {
        "revenue by store, last month",
        "revenue by store, sorted by revenue",
        "revenue by store",
    }:
        assert_plan_held(
            payload,
            "PLAN_FALLBACK_SEMANTIC_DRIFT"
            if intent == "revenue by store" or "sorted" in intent
            else "PLAN_UNMATCHED_TERMS",
        )
        return
    assert payload["status"] == status, payload.get("why")
    query = payload["best"]["query_ir"]
    assert query.get("group_by", []) == groups
    # The check's grouping list ends at the comma, including for a held sort request.
    assert _listed_grouping_terms(intent, jaffle._config) == [
        "month" if "by month" in intent else "store"
    ]
    if window is None:
        assert not query.get("time")
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert payload["why"]["details"]["terms"] == ["sorted"]
        return
    assert "execute" in payload["next"]["ready_for"]
    assert query["time"] == {
        "temporal_role": ORDER_TIME,
        "grain": "month",
        "range": {"last": {"unit": window, "value": 1}},
    }
    rows = typed_rows(jaffle.query({**query, "policy_context": {"now": "2017-04-15"}}))
    fields = [*groups, f"{ORDER_TIME}__month"]
    group_sql = "s.store_name, " if groups else ""
    start, end = ("2017-03-01", "2017-04-01") if window == "month" else ("2016-01-01", "2017-01-01")
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            f"SELECT {group_sql}DATE_TRUNC('month', o.ordered_at), "
            "SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
            "JOIN jaffle_store s ON o.store_id = s.store_id "
            "WHERE o.ordered_at >= ? AND o.ordered_at < ? "
            + ("GROUP BY 1, 2 ORDER BY 1, 2" if groups else "GROUP BY 1 ORDER BY 1"),
            [start, end],
        ).fetchall()
    finally:
        connection.close()
    assert reference
    actual = sorted(
        tuple(row[field] for field in fields) + (float(row["revenue_usd"]),) for row in rows
    )
    assert [row[:-1] for row in actual] == [row[:-1] for row in reference]
    assert [row[-1] for row in actual] == pytest.approx([float(row[-1]) for row in reference])


def test_a_grouping_named_by_the_measure_word_is_not_dropped(
    upkeep: Callable[[str, str], Runtime],
) -> None:
    runtime = upkeep("repair", "repair")
    payload = plan_payload(runtime, intent="repair cost by repair")

    # The draft reads "repair" as the measure's word and groups by nothing: one total, 30.
    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["repair"]
    assert not payload["best"]["query_ir"].get("group_by")


def test_a_label_only_grouping_stays_ready(upkeep: Callable[[str, str], Runtime]) -> None:
    runtime = upkeep("incident", "repair")
    payload = plan_payload(runtime, intent="repair cost by incident name")

    # The question asks for the name only, so one row per name is its answer.
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert query["group_by"] == [INCIDENT_NAME]
    assert typed_rows(runtime.query(query)) == [{INCIDENT_NAME: "Leak", "repair_cost": 30}]


def test_a_geo_entity_keeps_its_plan(upkeep: Callable[[str, str], Runtime]) -> None:
    runtime = upkeep("geo", "geo")
    payload = plan_payload(runtime, intent="geo cost by geo")

    assert payload["status"] == "ok", payload.get("why")
    assert payload["best"]["query_ir"] == {
        "version": 1,
        "select": [{"as": "geo_cost", "expression": {"metric": "metric.upkeep.geo_cost"}}],
        "group_by": ["dimension.upkeep_geo_geo_id"],
        "order_by": [{"field": "dimension.upkeep_geo_geo_id", "direction": "ASC"}],
    }


@pytest.mark.parametrize(
    ("intent", "group_by", "grain"),
    [("revenue by store", [STORE], None), ("revenue by month", None, "month")],
)
def test_a_single_grouping_stays_ready(
    jaffle: Runtime, intent: str, group_by: list[str] | None, grain: str | None
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    if intent in {
        "revenue by store, last month",
        "revenue by store, sorted by revenue",
        "revenue by store",
    }:
        assert_plan_held(
            payload,
            "PLAN_FALLBACK_SEMANTIC_DRIFT"
            if intent == "revenue by store" or "sorted" in intent
            else "PLAN_UNMATCHED_TERMS",
        )
        return
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert query.get("group_by") == group_by
    assert (query.get("time") or {}).get("grain") == grain


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        (
            "revenue by store, customer type, and product type",
            ["store", "customer type", "product type"],
        ),
        (
            "revenue by store, customer type and product type",
            ["store", "customer type", "product type"],
        ),
        ("revenue by store,customer type", ["store", "customer type"]),
        ("revenue by store; customer type", ["store"]),
        ("revenue by store, customer type for 2017", ["store", "customer type"]),
        ("revenue by store, the customer type", ["store", "the customer type"]),
        ("revenue by store, order date", ["store", "order date"]),
        ("top 5 stores, customers by revenue", ["stores", "customers"]),
        ("the top 5 stores by revenue", ["stores"]),
        # A piece after a comma that names no clock, dimension or entity ends the list.
        ("revenue by store, at week grain", ["store"]),
        ("revenue by store, 2017", ["store"]),
        ("revenue by store, nonsense, customer type", ["store"]),
        # A window the question states separates pieces as a comma does.
        ("revenue by store, last month and customer type", ["store", "customer type"]),
        ("revenue by store last month and customer type", ["store", "customer type"]),
        ("revenue by store, last month, nonsense", ["store"]),
    ],
)
def test_the_check_reads_every_listed_grouping(
    jaffle: Runtime, intent: str, terms: list[str]
) -> None:
    assert _listed_grouping_terms(intent, jaffle._config) == terms


@pytest.mark.parametrize(
    ("term", "changes", "matched"),
    [
        ("the case", {"label": "Case"}, True),
        # An id's namespace, model and entity prefix are not the dimension's own words.
        ("case", {"id": "dimension.case"}, False),
        ("case", {"name": "support.case"}, True),
        ("case", {"aliases": ["Case"]}, True),
        ("case", {"label": "Showcase"}, False),
        ("case number", {"label": "Case"}, False),
        ("case", {"description": "Case"}, False),
        ("case", {"topics": ["Case"]}, False),
        ("purchase", {"label": "Order"}, False),
        ("sent", {"label": "Received"}, False),
    ],
)
def test_grouping_correspondence_uses_only_declared_name_words(
    upkeep: Callable[[str, str], Runtime], term: str, changes: dict[str, Any], matched: bool
) -> None:
    runtime = upkeep("incident", "repair")
    dimension = replace(
        DimensionConfig(
            "dimension.neutral", runtime._config.entities[0].id, "incident_name", "string"
        ),
        **changes,
    )
    runtime._config = replace(runtime._config, dimensions=[dimension])
    query = {"group_by": [dimension.id]}
    why = plan_module._dropped_grouping_why(runtime, f"repair cost by {term}", query)
    assert (why is None) is matched
    assert _listed_grouping_terms(f"repair cost by incident, {term}", runtime._config) == (
        ["incident", term] if matched else ["incident"]
    )
    if why:
        assert why["code"] == "PLAN_UNMATCHED_TERMS"
        assert why["details"]["dropped_groupings"] == [term]


@pytest.mark.parametrize("column", ["incident_id", "incident_name"])
def test_an_entity_name_matches_its_declared_key_only(
    upkeep: Callable[[str, str], Runtime], column: str
) -> None:
    runtime = upkeep("incident", "repair")
    dimension = DimensionConfig(
        "dimension.reference", runtime._config.entities[0].id, column, "string"
    )
    runtime._config = replace(runtime._config, dimensions=[dimension])
    why = plan_module._dropped_grouping_why(
        runtime, "repair cost by incident", {"group_by": [dimension.id]}
    )
    assert (why is None) is (column == "incident_id")


def test_repeating_one_dimension_never_satisfies_two_listed_groupings(
    upkeep: Callable[[str, str], Runtime],
) -> None:
    runtime = upkeep("incident", "repair")
    why = plan_module._dropped_grouping_why(
        runtime,
        "repair cost by incident name, incident",
        {"group_by": [INCIDENT_NAME, INCIDENT_NAME, "dimension.unknown"]},
    )
    assert why is not None
    assert why["details"]["dropped_groupings"] == ["incident"]


# What plan answered each question with before the listed-grouping check: ready (OK), or the
# code that held it. Every question these tests ask is here, with every question a review of
# this check has asked.
OK = "ok"
UNMATCHED = "PLAN_UNMATCHED_TERMS"
UNASKED = "PLAN_UNASKED_GROUPING"
INVALID = "VALIDATION_FAILED"
DRIFT = "PLAN_FALLBACK_SEMANTIC_DRIFT"
GAP = "PLAN_INTENT_COVERAGE_GAP"
WINDOW = "TIME_WINDOW_UNRESOLVED"


@dataclass(frozen=True)
class _Before:
    intent: str
    before: str
    # The check holds this plan, which was ready.
    held: bool = False
    # The code that holds it: this check's, or the unasked-grouping check's after it.
    code: str = UNMATCHED
    package: str = "jaffle"
    # The caller's partial_query group_by.
    group_by: tuple[str, ...] = ()
    # Plan drafts with catalog discovery, as when no pattern realizes the question.
    fallback: bool = False
    # Store dimensions added to Jaffle: (name, label, column).
    dims: tuple[tuple[str, str, str], ...] = ()


_TAX = (("tax", "Tax", "store_id"),)
_BOX = (("box", "Box", "store_id"),)
_STATUS = (("status", "Membership status", "store_id"),)
_GRAIN_PHRASES = [
    phrase.format(unit)
    for phrase in ("{} level", "at {} grain", "at {} level")
    for unit in ("day", "week", "month", "quarter", "year")
]
_BEFORE = [
    _Before("revenue by store, last month", "ok", held=True, code="PLAN_UNMATCHED_TERMS"),
    _Before("revenue by month, last year", OK),
    _Before("revenue by store, 2017", WINDOW),
    _Before("revenue by store, sorted by revenue", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("order count by customer type, order for Brooklyn store", OK, held=True),
    _Before("revenue by store, customer type and product type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("What was revenue by month, beside the revenue of the month before?", UNMATCHED),
    _Before(
        "What was revenue by month, and how much of it came from orders of 50 USD or more?",
        UNMATCHED,
    ),
    _Before("order count by customer type, order", OK, held=True),
    _Before("order count by customer history, month", OK, held=True),
    _Before("revenue by store, last month and customer type", UNMATCHED),
    _Before("revenue by store last month and customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("the top 5 stores by revenue", "PLAN_INTENT_COVERAGE_GAP"),
    _Before("revenue by customer segment", INVALID),
    _Before("order count by month, name", UNMATCHED),
    _Before("top customers by revenue", OK),
    _Before("top 10 customers by revenue in Q1 2017", OK),
    _Before("revenue by item in 2017", OK, held=True),
    _Before("order count by month, customer id", UNMATCHED),
    _Before("item revenue by month, name", UNMATCHED),
    _Before("item revenue by name", OK, held=True),
    _Before("item revenue by month and name", OK, held=True),
    _Before("order count by month, name", OK, group_by=(STORE,)),
    _Before("order count by month, name", OK, group_by=(CUSTOMER_NAME,)),
    *(
        _Before(f"item revenue by {words}districts", UNMATCHED, package="districts")
        for words in ("", "the ", "their ", "each ")
    ),
    # Catalog discovery keeps Customer district, the first of two other entities' districts.
    *(
        _Before(
            f"item revenue by {words}districts", OK, held=True, package="districts", fallback=True
        )
        for words in ("", "the ", "their ", "each ")
    ),
    # Store district is hidden.
    _Before("item revenue by districts", UNMATCHED, package="hidden_district"),
    _Before("item revenue by districts", OK, package="hidden_district", fallback=True),
    *(
        _Before(
            "item revenue by district",
            GAP,
            package="hidden_district",
            group_by=(PRODUCT_TYPE,),
            fallback=fallback,
        )
        for fallback in (False, True)
    ),
    _Before("order count by month and name", OK, held=True),
    _Before("order count by month, customer name", UNMATCHED),
    _Before("order count by month, store name", "PLAN_UNMATCHED_TERMS"),
    _Before("order count by month and customer name", OK),
    _Before("order count by month and store name", OK),
    _Before("revenue by store", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by month", OK),
    _Before("revenue by store, customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store and customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, customer type, and product type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store,customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store; customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, customer type for 2017", UNMATCHED),
    _Before("revenue by store, the customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, order date", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, nonsense, customer type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, order", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, date", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue from orders", UNMATCHED),
    _Before("orders by store, time", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by store, statuses", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("food revenue vs drink revenue by store, customer type", UNMATCHED),
    # The comparison buckets by month, which the question never asks for.
    _Before(
        "food revenue vs drink revenue by store and customer type",
        "ok",
        held=True,
        code="PLAN_UNMATCHED_TERMS",
    ),
    _Before("show monthly revenue by store", "ok", held=True, code="PLAN_UNMATCHED_TERMS"),
    _Before("order count by name", OK, held=True),
    _Before("order count by customer name", OK),
    _Before("order count by store name", OK),
    _Before("order count by product type", OK, held=True),
    _Before("item count by product type", OK),
    _Before("revenue by type", OK, held=True),
    _Before("revenue by store and type", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by order", DRIFT),
    _Before("revenue by customer", OK),
    _Before("revenue by stores", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
    _Before("revenue by supply", INVALID),
    _Before("revenue by item", OK, held=True),
    _Before("revenue by product", INVALID),
    _Before("revenue by customer history", OK, held=True),
    _Before("revenue by store name", OK),
    _Before("revenue by customer type", OK),
    # A grain phrase after a comma ends the draft's list; elsewhere the draft groups by a calendar
    # dimension that validation refuses for the measure.
    *(_Before(f"revenue by store, {phrase}", OK, held=True) for phrase in _GRAIN_PHRASES),
    *(
        _Before(shape.format(phrase), INVALID)
        for shape in (
            "revenue by {}",
            "order count by {}",
            "average order value by {}",
            "revenue by store and {}",
            "revenue by customer type and {}",
        )
        for phrase in _GRAIN_PHRASES
    ),
    *(
        _Before(f"revenue by order date{joint}{phrase}", OK if joint == ", " else INVALID)
        for joint in (", ", " and ")
        for phrase in [
            *(f"at {unit} grain" for unit in ("day", "week", "month", "quarter", "year")),
            "day level",
            "at day level",
        ]
    ),
    _Before("revenue by month level for 2017", INVALID),
    _Before("revenue in 2017 by month level", INVALID),
    _Before("revenue by store by month level", DRIFT),
    _Before("revenue by month level and store", INVALID),
    _Before("revenue by week level, store", INVALID),
    _Before("revenue by states, status", UNMATCHED, dims=(("states", "States", "store_name"),)),
    _Before(
        "revenue by store, sales",
        "PLAN_FALLBACK_SEMANTIC_DRIFT",
        dims=(("sales", "", "store_name"),),
    ),
    _Before("aov by store, sales", "VALIDATION_FAILED", dims=(("sales", "", "store_id"),)),
    _Before(
        "revenue by received, sent",
        UNMATCHED,
        dims=(("received", "Received", "store_name"), ("sent", "Sent", "store_id")),
    ),
    *(
        _Before(
            intent,
            before,
            held=intent == "monthly revenue by store",
            dims=((name, name.title(), "store_id"),),
        )
        for name in ("period", "show", "date")
        for intent, before in [
            ("monthly revenue by store", OK),
            (f"monthly revenue by store, {name}", UNMATCHED),
        ]
    ),
    _Before("revenue by taxes", OK, group_by=("dimension.tax",), dims=_TAX),
    _Before("revenue by store, taxes", "PLAN_FALLBACK_SEMANTIC_DRIFT", dims=_TAX),
    _Before("revenue by boxes", OK, group_by=("dimension.box",), dims=_BOX),
    _Before("revenue by store, boxes", "PLAN_FALLBACK_SEMANTIC_DRIFT", dims=_BOX),
    _Before("revenue by statuses", OK, group_by=("dimension.status",), dims=_STATUS),
    _Before("revenue by store, statuses", "PLAN_FALLBACK_SEMANTIC_DRIFT", dims=_STATUS),
    _Before("repair cost by incident name, incident", OK, held=True, package="incident"),
    _Before("repair cost by incident, incident name", UNMATCHED, package="incident"),
    _Before("repair cost by incident name and incident", OK, package="incident"),
    _Before("repair cost by incident and incident name", OK, package="incident"),
    _Before("repair cost by incident name", OK, package="incident"),
    _Before("repair cost by incident", OK, package="incident"),
    _Before("repair cost by incident name, incident", OK, held=True, package="revisions"),
    _Before("repair cost by incident name and incident", OK, held=True, package="revisions"),
    _Before("repair cost by repair", OK, held=True, package="repair"),
    _Before("geo cost by geo", OK, package="geo"),
]


def _case_id(case: _Before) -> str:
    return "-".join(
        [
            case.package,
            *(["fallback"] if case.fallback else []),
            *case.group_by,
            *(name for name, _, _ in case.dims),
            case.intent,
        ]
    )


def _runtime_for(
    case: _Before,
    runtime_factory: Any,
    upkeep: Callable[..., Runtime],
    monkeypatch: pytest.MonkeyPatch,
    stack: ExitStack,
) -> Runtime:
    if case.package in {"incident", "revisions", "repair", "geo"}:
        noun, measure = {"geo": ("geo", "geo"), "repair": ("repair", "repair")}.get(
            case.package, ("incident", "repair")
        )
        runtime = upkeep(noun, measure, revisions=case.package == "revisions")
    else:
        runtime = runtime_factory("jaffle_shop")
        stack.callback(runtime.close)
        if case.dims:
            runtime = stack.enter_context(_with_store_dimensions(runtime, *case.dims))
        if case.package in {"districts", "hidden_district"}:
            _with_districts(runtime, monkeypatch)
        if case.package == "hidden_district":
            hidden = SemanticPolicyConfig(
                id="policy.test.hide_store_district",
                kind="object_visibility",
                object_ids=[STORE_DISTRICT],
                action="hidden",
            )
            policies = [*runtime._config.semantic_policies, hidden]
            monkeypatch.setattr(
                runtime, "_config", replace(runtime._config, semantic_policies=policies)
            )
    if case.fallback:
        _force_fallback(runtime, monkeypatch, case.intent, "fallback")
    return runtime


def _outcome(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok" and "execute" in payload["next"].get("ready_for", []):
        return OK
    return str((payload.get("why") or {}).get("code"))


@pytest.mark.parametrize("case", _BEFORE, ids=_case_id)
def test_the_check_only_holds_a_plan_that_was_ready(
    runtime_factory: Any,
    upkeep: Callable[..., Runtime],
    monkeypatch: pytest.MonkeyPatch,
    case: _Before,
) -> None:
    partial_query = {"group_by": list(case.group_by)} if case.group_by else None
    with ExitStack() as stack:
        runtime = _runtime_for(case, runtime_factory, upkeep, monkeypatch, stack)
        after = plan_payload(runtime, intent=case.intent, partial_query=partial_query)
        with monkeypatch.context() as without_check:
            # They are the only readers of the listed groupings, with the answer-shape check
            # after them, which only holds a plan (test_plan_answer_shape.py).
            without_check.setattr(plan_module, "_dropped_grouping_why", lambda *args: None)
            without_check.setattr(plan_module, "_unasked_grouping_why", lambda *args: None)
            without_check.setattr(plan_module, "_answer_shape_why", lambda *args: None)
            before = plan_payload(runtime, intent=case.intent, partial_query=partial_query)

    # Without the checks, plan answers as it did before they existed.
    assert _outcome(before) == case.before
    # The checks never change the draft.
    assert after["best"].get("query_ir") == before["best"].get("query_ir")
    assert after["best"].get("pattern") == before["best"].get("pattern")
    if case.held:
        # They only hold a plan that was ready, and never ready one or pick another.
        assert case.before == OK
        assert after["status"] == "low_confidence"
        assert "ready_for" not in after["next"]
        assert after["why"]["code"] == case.code
    else:
        assert after["status"] == before["status"]
        assert _outcome(after) == _outcome(before)
        assert after["next"] == before["next"]
        if case.before != OK:
            assert "execute" not in after["next"].get("ready_for", [])
