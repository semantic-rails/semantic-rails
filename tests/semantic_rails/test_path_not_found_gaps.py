"""PATH_NOT_FOUND names what separates the pair and what each side could change.

A refusal with no relationship chain names the one missing link (a relationship that allows
only the other walk, a key one side holds, an entity holding both keys, or a shared identifier
that is a key of neither) and the nearest entity the start reaches. It ranks the reachable
dimensions and the measures that come closest to what was asked, and tells the caller's options
apart from the package author's. A misspelled entity gets its closest ids instead of a gap. The
diagnosis reads the caller's view only: a hidden entity is never named, and the refusal, its
code and every route stay what they were.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.compiler_parts.indexes import get_package_analysis
from semantic_rails.diagnostics import enrich_path_not_found, exception_issue
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import resolve_path
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.path_gap import closest_dimensions, closest_measures, path_gap
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    RelationshipConfig,
    SemanticPolicyConfig,
)
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.hidden_absent import absent, envelope
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

READER = RequestContext(audience="reader")


def _rel(rel_id: str, source: str, target: str, column: str, **extra: Any) -> RelationshipConfig:
    return RelationshipConfig(
        id=rel_id,
        source_entity=source,
        target_entity=target,
        source_column=column,
        target_column=column,
        cardinality="N:1",
        safety="safe",
        **extra,
    )


def _package(
    package_config_factory,
    entities: dict[str, list[str]],
    columns: dict[str, list[str]],
    relationships: list[RelationshipConfig] = (),  # type: ignore[assignment]
    hidden: list[str] = (),  # type: ignore[assignment]
) -> tuple[Any, Any]:
    """A package of ``entities`` (id -> key columns) whose dimensions read ``columns``."""
    config, path = package_config_factory("jaffle_shop")
    config = replace(
        config,
        entities=[
            EntityConfig(id=f"entity.{name}", table=name, key_columns=key, label=name.title())
            for name, key in entities.items()
        ],
        dimensions=[
            DimensionConfig(
                id=f"dimension.{name}_{column}",
                entity=f"entity.{name}",
                column=column,
                data_type="string",
                label=column.replace("_", " ").capitalize(),
            )
            for name, held in columns.items()
            for column in held
        ],
        measures=[],
        metric_recipes=[],
        segments=[],
        temporal_roles=[],
        relationships=list(relationships),
        path_preferences=[],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test.hide",
                kind="object_visibility",
                action="hidden",
                object_ids=list(hidden),
                audiences=["reader"],
            )
        ]
        if hidden
        else [],
    )
    return config, path


def _issue(
    config: Any, start: str, target: str, runnable: Any = None, **details: Any
) -> dict[str, Any]:
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start=start, target=target)
    raised.value.details.update(details)
    enriched = enrich_path_not_found(raised.value, config, runnable_measure=runnable)
    return exception_issue(enriched, stage="validate")


def _author(issue: dict[str, Any]) -> str:
    kinds = ("author_relationship", "declare_key")
    return " ".join(hint["message"] for hint in issue["recovery_hints"] if hint["kind"] in kinds)


def _audiences(issue: dict[str, Any]) -> dict[str, str]:
    return {hint["kind"]: hint["audience"] for hint in issue["recovery_hints"]}


# --- The adversarial matrix ---------------------------------------------------------------------

SCHOOL = {"student": ["student_id"], "course": ["course_id"], "enrollment": ["enrollment_id"]}
SCHOOL_COLUMNS = {
    "student": ["student_id", "student_name"],
    "course": ["course_id", "course_title"],
    "enrollment": ["enrollment_id", "student_id", "course_id"],
}


def test_a_missing_bridge_names_the_entity_holding_both_keys(package_config_factory):
    config, _ = _package(package_config_factory, SCHOOL, SCHOOL_COLUMNS)
    issue = _issue(config, "entity.student", "entity.course")
    details = issue["details"]
    assert details["missing_link"] == {
        "kind": "bridge",
        "via": "entity.enrollment",
        "entities": ["entity.student", "entity.course"],
        "columns": {"entity.student": ["student_id"], "entity.course": ["course_id"]},
    }
    assert details["nearest_entity"] == "entity.student"
    assert "'entity.enrollment' holds the keys of both" in issue["message"]
    author = next(hint for hint in issue["recovery_hints"] if hint["kind"] == "author_relationship")
    assert author["audience"] == "author"
    assert (
        "relate 'entity.enrollment' to 'entity.student' and to 'entity.course'" in author["message"]
    )


@pytest.mark.parametrize("keyed", [False, True], ids=["no-entity-keyed-by-it", "keyed-by-region"])
def test_a_many_to_many_with_no_bridge_says_the_join_needs_a_link(package_config_factory, keyed):
    entities = {
        "order": ["order_id"],
        "rep": ["rep_id"],
        **({"region": ["region_code"]} if keyed else {}),
    }
    columns = {
        "order": ["order_id", "region_code"],
        "rep": ["rep_id", "region_code", "rep_name"],
        **({"region": ["region_code"]} if keyed else {}),
    }
    config, _ = _package(package_config_factory, entities, columns)
    issue = _issue(config, "entity.order", "entity.rep")
    link = issue["details"]["missing_link"]
    assert link["kind"] == "many_to_many"
    assert link["entities"] == ["entity.order", "entity.rep"] and link["columns"] == ["region_code"]
    assert "a key of neither, so a join on it is many-to-many" in issue["message"]
    author = next(hint for hint in issue["recovery_hints"] if hint["kind"] == "author_relationship")
    if keyed:
        assert link["keyed_by"] == "entity.region"
        assert "each to 'entity.region'" in author["message"]
    else:
        assert "keyed_by" not in link
        assert "link entity with one row per pair" in author["message"]


LOYALTY = {"order": ["order_id"], "customer": ["customer_id"], "loyalty": ["loyalty_id"]}
LOYALTY_COLUMNS = {
    "order": ["order_id", "customer_id", "order_tier"],
    "customer": ["customer_id", "customer_tier"],
    "loyalty": ["loyalty_id", "customer_id", "loyalty_tier"],
}
ORDER_CUSTOMER = _rel("order_customer", "entity.order", "entity.customer", "customer_id")


@pytest.mark.parametrize("existing", [False, True], ids=["no-relationship", "one-way-relationship"])
def test_a_one_hop_miss_names_the_nearest_reachable_entity(package_config_factory, existing):
    relationships = [ORDER_CUSTOMER]
    if existing:
        # Walkable from loyalty to customer only, so never from the order side.
        relationships.append(
            _rel(
                "loyalty_customer",
                "entity.loyalty",
                "entity.customer",
                "customer_id",
                allowed_directions=["forward"],
            )
        )
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, relationships)
    issue = _issue(
        config,
        "entity.order",
        "entity.loyalty",
        unreachable_items=["dimension.loyalty_loyalty_tier"],
    )
    details = issue["details"]
    assert details["reachable_targets"] == ["entity.customer"]
    assert details["nearest_entity"] == "entity.customer"
    if existing:
        assert details["missing_link"] == {
            "kind": "direction",
            "relationship": "loyalty_customer",
            "from": "entity.customer",
            "to": "entity.loyalty",
            "allowed_directions": ["forward"],
            "fans_out": True,
        }
        assert "allows no walk from 'entity.customer'" in issue["message"]
    else:
        assert details["missing_link"] == {
            "kind": "lookup",
            "source_entity": "entity.loyalty",
            "target_entity": "entity.customer",
            "columns": ["customer_id"],
            "walk": "reverse",
        }
        assert (
            "'entity.loyalty' holds the key of 'entity.customer' (customer_id)" in issue["message"]
        )
    # The caller's stand-ins: reachable tiers, the start's own first.
    assert details["closest_dimensions"] == [
        "dimension.order_order_tier",
        "dimension.customer_customer_tier",
    ]
    audiences = _audiences(issue)
    assert audiences.pop("author_relationship") == "author"
    assert set(audiences.values()) == {"caller"}


def test_a_typo_d_entity_gets_its_closest_ids_and_no_gap(package_config_factory, jaffle_snapshot):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    issue = _issue(config, "entity.order", "entity.loyalti")
    details = issue["details"]
    assert details["unknown_entity"] == "entity.loyalti"
    assert details["closest_matches"] == ["entity.loyalty"]
    assert "missing_link" not in details and "compatible_group_by_dimensions" not in details
    assert [hint["kind"] for hint in issue["recovery_hints"]] == ["fix_entity_id"]
    assert "not an entity of this package" in issue["message"]
    # An entity filter's typo, end to end: still a PATH_NOT_FOUND, now naming the entity meant.
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    try:
        having = [{"field": "measure.jaffle.order_count", "op": "gte", "value": 2}]
        query = {
            "select": ["measure.jaffle.revenue_usd"],
            "where": [{"entity": "entity.jaffle_custmer", "having": having, "over": "all_time"}],
        }
        (error,) = runtime.validate(query)["errors"]
        assert error["code"] == "PATH_NOT_FOUND"
        assert error["details"]["closest_matches"][0] == "entity.jaffle_customer"
        assert "author" not in json.dumps(error["recovery_hints"])
    finally:
        runtime.close()


def test_a_hidden_bridge_is_never_named_to_a_caller_who_cant_see_it(
    package_config_factory, jaffle_snapshot, monkeypatch
):
    config, _ = _package(
        package_config_factory, SCHOOL, SCHOOL_COLUMNS, hidden=["entity.enrollment"]
    )
    query = {"group_by": ["dimension.student_student_name", "dimension.course_course_title"]}
    arguments = {"query": query, "mode": "validate", "verbosity": "full"}
    reader_context = context_from_policy_context(READER.to_policy_context())

    def call(package: Any, **kwargs: Any) -> dict[str, Any]:
        runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
        monkeypatch.setattr(runtime, "_config", package)
        adapter = SemanticLayerMCPAdapter(runtime)
        try:
            return adapter.call_tool("execute", arguments, **kwargs)
        finally:
            adapter.close()  # Closes its runtime too.

    reader = call(config, request_context=reader_context)
    author = call(config)
    without = call(absent(config, ["entity.enrollment"]), request_context=reader_context)
    for response in (reader, author):
        assert response["ok"] is False
        assert response["errors"][0]["code"] == "PATH_NOT_FOUND"
    assert "enrollment" not in json.dumps(reader).lower()
    assert "missing_link" not in reader["errors"][0]["details"]
    assert author["errors"][0]["details"]["missing_link"]["via"] == "entity.enrollment"
    # The reader is told exactly what a package without the hidden entity would say.
    assert envelope(reader) == envelope(without)


# --- What the diagnosis never touches -------------------------------------------------------------


def test_the_diagnosis_leaves_routes_and_their_cache_unchanged(package_config_factory):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="entity.order", target="entity.loyalty")
    analysis = get_package_analysis(config)
    cache = dict(analysis.path_cache)
    first = enrich_path_not_found(raised.value, config)
    second = enrich_path_not_found(first, config)
    assert (first.code, first.details) == (second.code, second.details)
    assert str(first) == str(second)  # The gap is said once, however often it is enriched.
    assert analysis.path_cache == cache
    assert resolve_path(config, start="entity.order", target="entity.customer")[0] == [
        "order_customer"
    ]
    with pytest.raises(SemanticLayerError) as again:
        resolve_path(config, start="entity.order", target="entity.loyalty")
    assert (again.value.code, again.value.details) == (raised.value.code, raised.value.details)


def test_a_walk_that_exists_is_never_called_missing(package_config_factory):
    # The target is reachable, only past the hop limit: no link is missing.
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    config = replace(
        config,
        relationships=[
            *config.relationships,
            _rel("loyalty_customer", "entity.loyalty", "entity.customer", "customer_id"),
        ],
    )
    assert path_gap(config, "entity.order", "entity.loyalty", ["entity.customer"]) == {}


def test_bare_ids_are_not_keys_of_each_other(package_config_factory):
    entities = {"a": ["id"], "b": ["id"]}
    config, _ = _package(package_config_factory, entities, {"a": ["id"], "b": ["id"]})
    assert path_gap(config, "entity.a", "entity.b", []) == {}


def test_closest_stand_ins_share_rare_words_with_what_was_asked(package_config_factory):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    assert closest_dimensions(
        config,
        "entity.order",
        ["entity.customer"],
        "entity.loyalty",
        ["dimension.loyalty_loyalty_tier"],
    ) == ["dimension.order_order_tier", "dimension.customer_customer_tier"]


def test_closest_measures_are_offered_as_different_measures(runtime_with_loyalty_measures):
    config = runtime_with_loyalty_measures

    def anything(_measure_id: str) -> bool:
        return True

    stand_ins = closest_measures(config, "entity.loyalty", ["measure.order_points"], anything)
    assert stand_ins == ["measure.loyalty_points"]
    # Without a way to check the caller's policies, nothing is offered.
    assert closest_measures(config, "entity.loyalty", ["measure.order_points"]) == []
    issue = _issue(
        config, "entity.order", "entity.loyalty", anything, measures=["measure.order_points"]
    )
    assert issue["details"]["closest_measures"] == ["measure.loyalty_points"]
    hint = next(hint for hint in issue["recovery_hints"] if hint["kind"] == "switch_measure")
    assert hint["audience"] == "caller"
    assert "different measure, so ask the user" in hint["message"]


@pytest.fixture()
def runtime_with_loyalty_measures(package_config_factory):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    base, _ = package_config_factory("jaffle_shop")
    template = base.measures[0]
    measures = [
        replace(template, id=measure_id, entity=entity, label=label, name=measure_id)
        for measure_id, entity, label in (
            ("measure.order_points", "entity.order", "Order points"),
            ("measure.loyalty_points", "entity.loyalty", "Loyalty points"),
            ("measure.loyalty_count", "entity.loyalty", "Loyalty members"),
            ("measure.customer_count", "entity.customer", "Customers"),
        )
    ]
    return replace(config, measures=measures)


def test_the_jaffle_refusal_names_the_membership_key_it_lacks(jaffle_snapshot):
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    try:
        query = {
            "select": ["measure.jaffle.revenue_usd"],
            "group_by": ["dimension.jaffle_membership_status"],
        }
        (error,) = runtime.validate(query)["errors"]
        details = error["details"]
        assert error["code"] == "PATH_NOT_FOUND"
        assert details["measures"] == ["measure.jaffle.revenue_usd"]
        assert details["nearest_entity"] == "entity.jaffle_customer"
        assert details["missing_link"]["kind"] == "lookup"
        assert details["missing_link"]["columns"] == ["customer_id"]
        assert "holds the key of 'entity.jaffle_customer' (customer_id)" in error["message"]
    finally:
        runtime.close()


# --- Round 2: the advice itself ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cardinality", "fans_out"), [("N:1", True), ("1:1", False), ("", None)], ids=str
)
def test_allowing_a_walk_says_whether_a_measure_repeats(
    package_config_factory, cardinality, fans_out
):
    # loyalty -> customer, walkable forward only: from customer, the walk would be its reverse.
    one_way = _rel(
        "loyalty_customer",
        "entity.loyalty",
        "entity.customer",
        "customer_id",
        allowed_directions=["forward"],
    )
    one_way = replace(one_way, cardinality=cardinality)
    config, _ = _package(
        package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER, one_way]
    )
    issue = _issue(config, "entity.order", "entity.loyalty")
    assert issue["details"]["missing_link"]["fans_out"] is fans_out
    author = _author(issue)
    if fans_out:
        assert "Each 'entity.customer' row then meets many 'entity.loyalty' rows" in author
        assert "repeats across them" in author
    elif fans_out is False:
        assert "meets one 'entity.loyalty' row, so nothing repeats" in author
    else:
        assert "if many, a measure read through 'entity.customer' repeats" in author


def test_a_bridge_says_each_row_meets_many_through_it(package_config_factory):
    config, _ = _package(package_config_factory, SCHOOL, SCHOOL_COLUMNS)
    author = _author(_issue(config, "entity.student", "entity.course"))
    assert "Each 'entity.student' row then meets many 'entity.enrollment' rows" in author
    assert "a measure read through 'entity.student' repeats across them" in author


def test_a_lookup_says_whether_it_repeats(package_config_factory):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    reverse = _author(_issue(config, "entity.order", "entity.loyalty"))
    assert "Each 'entity.customer' row then meets many 'entity.loyalty' rows" in reverse
    # The order side holds the loyalty key: a lookup, one row each.
    columns = {**LOYALTY_COLUMNS, "order": [*LOYALTY_COLUMNS["order"], "loyalty_id"]}
    config, _ = _package(package_config_factory, LOYALTY, columns, [ORDER_CUSTOMER])
    forward = _author(_issue(config, "entity.order", "entity.loyalty"))
    assert (
        "Each 'entity.order' row then meets one 'entity.loyalty' row, so nothing repeats" in forward
    )


def test_keyed_by_names_its_own_key_and_the_fan_out(package_config_factory):
    entities = {"order": ["order_id"], "rep": ["rep_id"], "region": ["region_code"]}
    columns = {
        "order": ["order_id", "country_code", "region_code"],
        "rep": ["rep_id", "country_code", "region_code"],
        "region": ["region_code"],
    }
    config, _ = _package(package_config_factory, entities, columns)
    issue = _issue(config, "entity.order", "entity.rep")
    link = issue["details"]["missing_link"]
    assert link["columns"] == ["country_code", "region_code"]
    assert (link["keyed_by"], link["keyed_by_columns"]) == ("entity.region", ["region_code"])
    author = _author(issue)
    assert "each to 'entity.region' on its key (region_code)" in author
    assert "country_code, region_code is the key" not in author
    assert "meets many 'entity.rep' rows through 'entity.region'" in author


def test_a_bare_id_is_never_another_entitys_key(package_config_factory):
    # order holds a column named id, its own surrogate; customer is keyed by a bare id.
    entities = {"order": ["order_id"], "customer": ["id"]}
    columns = {"order": ["order_id", "id"], "customer": ["id", "customer_name"]}
    config, _ = _package(package_config_factory, entities, columns)
    assert path_gap(config, "entity.order", "entity.customer", []) == {}
    issue = _issue(config, "entity.order", "entity.customer")
    assert "holds the key" not in issue["message"]
    assert " on id" not in _author(issue)


def test_closest_dimensions_are_offered_as_different_dimensions(package_config_factory):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    issue = _issue(
        config,
        "entity.order",
        "entity.loyalty",
        unreachable_items=["dimension.loyalty_loyalty_tier"],
    )
    hint = next(h for h in issue["recovery_hints"] if h["kind"] == "switch_group_by_dimension")
    assert hint["closest_dimensions"] == [
        "dimension.order_order_tier",
        "dimension.customer_customer_tier",
    ]
    assert (
        "Each is a different dimension, so ask the user before grouping by one."
        in (hint["message"])
    )


MEMBERSHIP_REVENUE = "measure.jaffle.membership_revenue_usd"


@pytest.mark.parametrize("action", ["deny", "withhold_values"])
def test_a_stand_in_the_caller_cant_run_is_never_offered(jaffle_snapshot, monkeypatch, action):
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    config = runtime.package_config
    revenue = next(m for m in config.measures if m.id == "measure.jaffle.revenue_usd")
    stand_in = replace(
        revenue,
        id=MEMBERSHIP_REVENUE,
        name="jaffle.membership_revenue_usd",
        label="Membership revenue",
        entity="entity.jaffle_customer_segment_membership",
    )
    policy = SemanticPolicyConfig(
        id="policy.test.membership_revenue",
        kind="object_access",
        action=action,
        object_ids=[MEMBERSHIP_REVENUE],
        audiences=["reader"],
        **({"config": {"max_rank": 3}} if action == "withhold_values" else {}),
    )
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(config, measures=[*config.measures, stand_in], semantic_policies=[policy]),
    )
    adapter = SemanticLayerMCPAdapter(runtime)
    query = {
        "select": ["measure.jaffle.revenue_usd"],
        "group_by": ["dimension.jaffle_membership_status"],
    }
    try:
        offered = {}
        for who, context in (("author", None), ("reader", READER.to_policy_context())):
            kwargs = {"request_context": context_from_policy_context(context)} if context else {}
            response = adapter.call_tool("execute", {"query": query, "mode": "validate"}, **kwargs)
            error = response["errors"][0]
            assert error["code"] == "PATH_NOT_FOUND"
            offered[who] = error["details"].get("closest_measures", [])
        assert offered["author"] == [MEMBERSHIP_REVENUE]
        assert MEMBERSHIP_REVENUE not in offered["reader"]
    finally:
        adapter.close()  # Closes its runtime too.


# --- Round 2: hardening -------------------------------------------------------------------------


def test_an_entity_slot_is_offered_entities_only(jaffle_snapshot):
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    try:
        having = [{"field": "measure.jaffle.order_count", "op": "gte", "value": 2}]
        query = {
            "select": ["measure.jaffle.revenue_usd"],
            "where": [
                {"entity": "dimension.jaffle_store_name", "having": having, "over": "all_time"}
            ],
        }
        (error,) = runtime.validate(query)["errors"]
        matches = error["details"]["closest_matches"]
        assert matches and all(match.startswith("entity.") for match in matches)
        assert "entity.jaffle_store" in matches
    finally:
        runtime.close()


def test_a_long_unknown_id_is_echoed_short(package_config_factory):
    config, _ = _package(package_config_factory, LOYALTY, LOYALTY_COLUMNS, [ORDER_CUSTOMER])
    target = "entity." + "x" * 5_000
    issue = _issue(config, "entity.order", target)
    assert len(issue["details"]["unknown_entity"]) <= 120
    assert len(issue["recovery_hints"][0]["message"]) < 400


def test_enriching_again_under_a_narrower_view_rebuilds_the_gap(package_config_factory):
    config, _ = _package(package_config_factory, SCHOOL, SCHOOL_COLUMNS)
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="entity.student", target="entity.course")
    broad = enrich_path_not_found(raised.value, config)
    assert broad.details["missing_link"]["via"] == "entity.enrollment"
    narrow = replace(
        config,
        entities=[e for e in config.entities if e.id != "entity.enrollment"],
        dimensions=[d for d in config.dimensions if d.entity != "entity.enrollment"],
    )
    rebuilt = enrich_path_not_found(broad, narrow)
    assert "missing_link" not in rebuilt.details
    assert "enrollment" not in str(rebuilt) and "enrollment" not in json.dumps(rebuilt.details)


def test_a_row_grain_targets_key_is_the_authors_to_declare(package_config_factory):
    entities = {"order": ["order_id"], "event": []}
    columns = {"order": ["order_id"], "event": ["event_name"]}
    config, _ = _package(package_config_factory, entities, columns)
    issue = _issue(config, "entity.order", "entity.event")
    audiences = _audiences(issue)
    assert audiences["row_grain_target"] == "caller"
    assert audiences["declare_key"] == "author"
    assert "declare a key" not in next(
        h["message"] for h in issue["recovery_hints"] if h["kind"] == "row_grain_target"
    )
