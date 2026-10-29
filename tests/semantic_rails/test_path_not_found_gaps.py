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
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler_parts.indexes import get_package_analysis
from semantic_rails.config import load_package_config
from semantic_rails.diagnostics import enrich_path_not_found, exception_issue
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import resolve_path
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.path_gap import closest_dimensions, path_gap
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


def _issue(config: Any, start: str, target: str, **details: Any) -> dict[str, Any]:
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start=start, target=target)
    raised.value.details.update(details)
    return exception_issue(enrich_path_not_found(raised.value, config), stage="validate")


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
        "via_keyed_by_near": False,
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


# --- Round 3: no stand-in measures --------------------------------------------------------------

LOOK_ALIKE = "measure.jaffle.membership_revenue_usd"


def test_no_refusal_offers_a_stand_in_measure(jaffle_snapshot, monkeypatch):
    # A measure on the target that only shares the asked measure's words is never offered: a
    # refusal names only the same measure on another model, once the call compiles with it in the
    # caller's own request scope (test_path_not_found_lookalikes.py).
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    config = runtime.package_config
    revenue = next(m for m in config.measures if m.id == "measure.jaffle.revenue_usd")
    look_alike = replace(
        revenue,
        id=LOOK_ALIKE,
        name="jaffle.membership_revenue_usd",
        label="Membership revenue",
        entity="entity.jaffle_customer_segment_membership",
    )
    monkeypatch.setattr(
        runtime, "_config", replace(config, measures=[*config.measures, look_alike])
    )
    adapter = SemanticLayerMCPAdapter(runtime)
    query = {
        "select": ["measure.jaffle.revenue_usd"],
        "group_by": ["dimension.jaffle_membership_status"],
    }
    try:
        response = adapter.call_tool(
            "execute", {"query": query, "mode": "validate", "verbosity": "full"}
        )
        error = response["errors"][0]
        assert error["code"] == "PATH_NOT_FOUND"
        assert "closest_measures" not in error["details"]
        assert all(hint["kind"] != "switch_measure" for hint in error["recovery_hints"])
        assert LOOK_ALIKE not in json.dumps(response)
    finally:
        adapter.close()  # Closes its runtime too.


def test_a_bridge_keyed_by_the_near_key_meets_one_row(package_config_factory):
    # One enrollment row per student: walking student -> enrollment repeats nothing.
    entities = {**SCHOOL, "enrollment": ["student_id"]}
    columns = {**SCHOOL_COLUMNS, "enrollment": ["student_id", "course_id"]}
    config, _ = _package(package_config_factory, entities, columns)
    issue = _issue(config, "entity.student", "entity.course")
    assert issue["details"]["missing_link"]["via_keyed_by_near"] is True
    author = _author(issue)
    assert (
        "Each 'entity.student' row then meets one 'entity.enrollment' row, so nothing repeats"
        in (author)
    )
    assert "meets many" not in author


def test_a_link_entity_fix_names_no_quoted_pronoun(package_config_factory):
    entities = {"order": ["order_id"], "rep": ["rep_id"]}
    columns = {"order": ["order_id", "region_code"], "rep": ["rep_id", "region_code"]}
    config, _ = _package(package_config_factory, entities, columns)
    author = _author(_issue(config, "entity.order", "entity.rep"))
    assert "'it'" not in author
    assert "meets many 'entity.rep' rows through it" in author


# --- The key columns the pair shares ------------------------------------------------------------

# A reading list's reserves: each row names a catalog item and an instructor key, while an
# instructor row is keyed by that key and the instructor's name together.
RESERVES = {
    "reserve": ["catalog_key", "instructor_key"],
    "instructor": ["instructor_key", "instructor_name"],
    "term": ["term_code"],
}
RESERVE_COLUMNS = {
    "reserve": ["catalog_key", "instructor_key", "term_code"],
    "instructor": ["instructor_key", "instructor_name", "course_name"],
    "term": ["term_code", "academic_year"],
}
RESERVE_TERM = _rel("reserve_term", "entity.reserve", "entity.term", "term_code")


def _hint(issue: dict[str, Any], kind: str) -> str:
    return next(hint["message"] for hint in issue["recovery_hints"] if hint["kind"] == kind)


def test_a_shared_key_that_isnt_unique_on_the_far_side_says_so(package_config_factory):
    # "Reserves by instructor name": the key the two share is part of the instructor's key, so
    # one reserve row can match every instructor row with that key.
    config, _ = _package(package_config_factory, RESERVES, RESERVE_COLUMNS, [RESERVE_TERM])
    asked = ["dimension.instructor_instructor_name"]
    issue = _issue(config, "entity.reserve", "entity.instructor", unreachable_items=asked)
    details = issue["details"]
    assert "missing_link" not in details
    assert details["shared_key_columns"] == ["instructor_key"]
    assert details["shared_key_unique_on_target"] is False
    assert issue["message"].endswith(
        ": 'entity.reserve' and 'entity.instructor' share instructor_key, but not the whole key "
        "of 'entity.instructor', so one 'entity.reserve' row can match many 'entity.instructor' "
        "rows on it."
    )
    split = _hint(issue, "drop_or_split")
    assert "Key columns they share: instructor_key, not the whole key of 'entity.instructor'" in (
        split
    )
    assert "counts a 'entity.reserve' row once per 'entity.instructor' row it matches" in split
    author = _author(issue)
    # Which relationship joins them depends on rows the engine doesn't read: the hint says both.
    assert (
        "is `cardinality: many_to_one` when no two 'entity.instructor' rows hold the same "
        "instructor_key" in author
    )
    assert "Otherwise it is `cardinality: many_to_many`" in author
    assert "refused with MIXED_GRAIN_INVALID" in author
    assert "should be reachable" not in author
    assert _audiences(issue)["drop_or_split"] == "caller"


def test_a_pair_sharing_no_key_says_so_and_where_a_relationship_goes(package_config_factory):
    # "Terms by instructor name": neither holds a column of the other's key.
    config, _ = _package(package_config_factory, RESERVES, RESERVE_COLUMNS, [RESERVE_TERM])
    asked = ["dimension.instructor_instructor_name"]
    issue = _issue(config, "entity.term", "entity.instructor", unreachable_items=asked)
    assert issue["details"]["shared_key_columns"] == []
    assert "shared_key_unique_on_target" not in issue["details"]
    assert issue["message"].endswith(": 'entity.term' and 'entity.instructor' share no key column.")
    assert "share no key column, so their separate results have no key to be matched on" in (
        _hint(issue, "drop_or_split")
    )
    author = _author(issue)
    assert "an entity 'entity.term' reaches must hold its key" in author
    assert "`entities:` block" in author and "graph.relationships row in graph.yml" in author


def test_a_shared_whole_key_matches_one_row_each(package_config_factory):
    # Orders hold the store's whole key: the split's results match one store row per order.
    entities = {"order": ["order_id"], "store": ["store_id"]}
    columns = {"order": ["order_id", "store_id"], "store": ["store_id", "store_name"]}
    config, _ = _package(package_config_factory, entities, columns)
    issue = _issue(config, "entity.order", "entity.store")
    assert issue["details"]["shared_key_columns"] == ["store_id"]
    assert issue["details"]["shared_key_unique_on_target"] is True
    # The missing link says it once; the shared key adds nothing to the message.
    assert issue["message"].count("store_id") == 1
    assert issue["details"]["missing_link"]["kind"] == "lookup"
    assert (
        "Key columns they share: store_id; each 'entity.order' row matches at most one "
        "'entity.store' row on it." in _hint(issue, "isolated_source_entity")
    )


def test_a_row_grain_target_sharing_a_key_column_says_it_has_no_key(package_config_factory):
    entities = {"room": ["room_id"], "room_rows": []}
    columns = {"room": ["room_id"], "room_rows": ["room_id", "department"]}
    config, _ = _package(package_config_factory, entities, columns)
    issue = _issue(config, "entity.room", "entity.room_rows")
    assert issue["details"]["shared_key_columns"] == ["room_id"]
    assert issue["details"]["shared_key_unique_on_target"] is False
    assert (
        "but 'entity.room_rows' has no key, so one 'entity.room' row can match many"
        in (issue["message"])
    )
    assert all(hint["kind"] != "author_relationship" for hint in issue["recovery_hints"])


def test_a_bare_id_on_both_sides_is_no_shared_key(package_config_factory):
    entities = {"a": ["id"], "b": ["id"]}
    config, _ = _package(package_config_factory, entities, {"a": ["id"], "b": ["id"]})
    issue = _issue(config, "entity.a", "entity.b")
    assert issue["details"]["shared_key_columns"] == []
    assert "share no key column" in issue["message"]


def test_enriching_again_states_the_shared_key_once(package_config_factory):
    config, _ = _package(package_config_factory, RESERVES, RESERVE_COLUMNS, [RESERVE_TERM])
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="entity.reserve", target="entity.instructor")
    once = enrich_path_not_found(raised.value, config)
    twice = enrich_path_not_found(once, config)
    assert str(twice) == str(once)
    assert str(once).count("share instructor_key") == 1
    assert twice.details == once.details


def _dated(config: Any, *columns: str, on: str = "") -> Any:
    """``config`` with every dimension reading one of ``columns`` typed as a date (on the
    entity ``on`` only, when given)."""
    return replace(
        config,
        dimensions=[
            replace(row, data_type="date")
            if row.column in columns and row.entity == (on or row.entity)
            else row
            for row in config.dimensions
        ],
    )


def test_a_load_date_on_every_table_is_no_shared_key(package_config_factory):
    # Two histories keyed by their own key and the date the warehouse loaded each row, and a
    # rooms table that reads the same load date. The date dates the rows and identifies nothing
    # the tables share, so a separate result of each has no key to be matched on.
    entities = {
        "building_hist": ["building_key", "load_date"],
        "floor_hist": ["floor_key", "load_date"],
        "room": ["room_id"],
    }
    columns = {
        "building_hist": ["building_key", "load_date", "building_name"],
        "floor_hist": ["floor_key", "load_date", "floor_name"],
        "room": ["room_id", "load_date"],
    }
    config, _ = _package(package_config_factory, entities, columns)
    for start, target in (
        ("building_hist", "floor_hist"),
        ("floor_hist", "room"),
        ("room", "building_hist"),
    ):
        issue = _issue(_dated(config, "load_date"), f"entity.{start}", f"entity.{target}")
        assert issue["details"]["shared_key_columns"] == [], (start, target)
        assert issue["message"].endswith("share no key column."), (start, target)
        assert "many_to_many" not in _author(issue)
    # Either side's date type says so, whichever end the call starts from.
    one_side = _dated(config, "load_date", on="entity.building_hist")
    for start, target in (("room", "building_hist"), ("building_hist", "room")):
        issue = _issue(one_side, f"entity.{start}", f"entity.{target}")
        assert issue["details"]["shared_key_columns"] == [], (start, target)
    # The same column read as text would be stated as a key they share.
    issue = _issue(config, "entity.building_hist", "entity.floor_hist")
    assert issue["details"]["shared_key_columns"] == ["load_date"]


def test_a_date_beside_an_identifier_or_as_a_calendars_key_is_shared(package_config_factory):
    # Daily store totals keyed by store and day: an order holds both, so it matches one row. A
    # calendar keyed by its date: an order's day matches one calendar row.
    entities = {
        "order": ["order_id"],
        "store_day": ["store_id", "date_day"],
        "calendar": ["date_day"],
    }
    columns = {
        "order": ["order_id", "store_id", "date_day"],
        "store_day": ["store_id", "date_day", "visits"],
        "calendar": ["date_day", "fiscal_week"],
    }
    config, _ = _package(package_config_factory, entities, columns)
    config = _dated(config, "date_day")
    for target, shared in (("store_day", ["date_day", "store_id"]), ("calendar", ["date_day"])):
        details = _issue(config, "entity.order", f"entity.{target}")["details"]
        assert details["shared_key_columns"] == shared, target
        assert details["shared_key_unique_on_target"] is True, target


def _library(
    root: Path, key: str, relationship: dict[str, Any] | None, target_key: str = ""
) -> Runtime:
    """Reserves keyed by a catalog item and an instructor key, and instructors keyed by that
    key (spelled ``target_key`` when given) and the instructor's name, related by
    ``relationship`` when given."""
    target_key = target_key or key
    count = {"kind": "entity_count", "accumulation": {"kind": "population"}, "value_type": "count"}
    files: dict[str, Any] = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "library",
                "namespace": "library",
                "name": "Library",
                "description": "Course reserves",
                "warehouse": "duckdb",
                "default_db": "library.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "models/reserves.yml": {
            "model": {
                "id": "reserves",
                "relation": "reserves",
                "entities": {"reserve": {}},
                "measures": {"reserves": {**count, "label": "Reserves", "entity_key": key}},
            }
        },
        "models/instructors.yml": {
            "model": {
                "id": "instructors",
                "relation": "instructors",
                "entities": {"instructor": {}},
                "dimensions": {"course_name": {"kind": "categorical"}},
            }
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "reserve": {"key": ["catalog_key", key], "model": "reserves"},
                    "instructor": {"key": [target_key, "instructor_name"], "model": "instructors"},
                },
                "relationships": {"reserve_instructor": relationship} if relationship else {},
            }
        },
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
    with duckdb.connect(str(root / "library.duckdb")) as connection:
        connection.execute(
            f"CREATE TABLE reserves (catalog_key VARCHAR, {key} VARCHAR);"
            f"CREATE TABLE instructors ({target_key} VARCHAR, instructor_name VARCHAR, "
            "course_name VARCHAR);"
        )
    load_package_config(str(root))
    return Runtime.from_path(str(root))


@pytest.mark.parametrize("key", ["instructor_key", "INSTRUCTOR_KEY"])
def test_both_relationships_the_author_hint_offers_load_as_written(tmp_path, key):
    # "Reserves by course": the hint's two rows, written as it spells them, both load. The
    # many-to-one row answers the call; the many-to-many one is refused, as the hint says.
    by_course = {
        "select": ["measure.library.reserves"],
        "group_by": ["dimension.library_instructor_course_name"],
    }
    for_math = {
        "select": ["measure.library.reserves"],
        "where": [
            {"field": "dimension.library_instructor_course_name", "op": "is", "value": "Math"}
        ],
    }
    runtime = _library(tmp_path / "refused", key, None)
    (error,) = runtime.validate(by_course)["errors"]
    runtime.close()
    assert error["code"] == "PATH_NOT_FOUND"
    assert error["details"]["shared_key_columns"] == [key]
    snippets = re.findall(r"`((?:via|target): \[[^\]]*\]|cardinality: [a-z_]+)`", _author(error))
    assert snippets == [
        f"via: [{key}]",
        f"target: [{key}]",
        "cardinality: many_to_one",
        "cardinality: many_to_many",
    ]
    via, target, *cardinalities = (yaml.safe_load(snippet) for snippet in snippets)
    answers = {}
    for cardinality in cardinalities:
        row = {"entities": ["reserve", "instructor"], **via, **target, **cardinality}
        runtime = _library(tmp_path / cardinality["cardinality"], key, row)
        answers[cardinality["cardinality"]] = [
            [error["code"] for error in runtime.validate(call).get("errors") or []]
            for call in (by_course, for_math)
        ]
        runtime.close()
    assert answers == {
        "many_to_one": [[], []],
        "many_to_many": [["MIXED_GRAIN_INVALID"], ["MIXED_GRAIN_INVALID"]],
    }


@pytest.mark.parametrize(
    ("start_key", "target_key"),
    [("instructor_key", "INSTRUCTOR_KEY"), ("INSTRUCTOR_KEY", "instructor_key")],
)
def test_the_hint_spells_each_side_as_its_own_key_says(tmp_path, start_key, target_key):
    # The two keys spell the shared column with different capitals: the hint's `via:` follows
    # the reserves' key and `target:` the instructors', so both rows it offers load.
    by_course = {
        "select": ["measure.library.reserves"],
        "group_by": ["dimension.library_instructor_course_name"],
    }
    runtime = _library(tmp_path / "refused", start_key, None, target_key)
    (error,) = runtime.validate(by_course)["errors"]
    runtime.close()
    assert error["code"] == "PATH_NOT_FOUND"
    assert error["details"]["shared_key_columns"] == [target_key]
    snippets = re.findall(r"`((?:via|target): \[[^\]]*\]|cardinality: [a-z_]+)`", _author(error))
    assert snippets == [
        f"via: [{start_key}]",
        f"target: [{target_key}]",
        "cardinality: many_to_one",
        "cardinality: many_to_many",
    ]
    via, target, *cardinalities = (yaml.safe_load(snippet) for snippet in snippets)
    answers = {}
    for cardinality in cardinalities:
        row = {"entities": ["reserve", "instructor"], **via, **target, **cardinality}
        runtime = _library(tmp_path / cardinality["cardinality"], start_key, row, target_key)
        answers[cardinality["cardinality"]] = [
            issue["code"] for issue in runtime.validate(by_course).get("errors") or []
        ]
        runtime.close()
    assert answers == {"many_to_one": [], "many_to_many": ["MIXED_GRAIN_INVALID"]}
    assert error["details"]["shared_key_columns_on_start"] == [start_key]


def test_a_shared_key_spelled_alike_on_both_sides_names_one_spelling(tmp_path):
    runtime = _library(tmp_path / "refused", "instructor_key", None)
    (error,) = runtime.validate(
        {
            "select": ["measure.library.reserves"],
            "group_by": ["dimension.library_instructor_course_name"],
        }
    )["errors"]
    runtime.close()
    assert error["details"]["shared_key_columns"] == ["instructor_key"]
    assert "shared_key_columns_on_start" not in error["details"]
