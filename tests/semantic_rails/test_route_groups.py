"""A route question's options, grouped by what they mean, where an answer shows them.

``route_groups`` groups an ``AMBIGUOUS_PATH`` refusal's routes at their first divergence. The
groups are added only where a refusal leaves the engine (the runtime's answer, MCP, ``plan``,
inspect and Architect's census): the resolver's own refusal, and every reader of its options
(row-filter coverage among them), are unchanged, so no route and no policy result moves.

Fixture: departments by building. A department reaches a building through its staff (their
home building), its work orders (billed to a building, and serviced in a room in one), its
room assignments, and its projects' tasks and visits:

    department <- staff -> building
    department <- work order -> building             the billing building
    department <- work order -> room -> building     the serviced room's building
    department <- assignment -> room -> building
    department <- assignment -> room <- work order -> building
    department <- project <- task -> building
    department <- project <- visit -> building
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

import semantic_rails.route_groups as route_groups_module
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import RequestContext, context_from_policy_context
from semantic_rails.route_groups import ORDERED
from semantic_rails.row_filter_visibility import coverage
from semantic_rails.runtime import Runtime
from tests.semantic_rails import test_route_resolution as bank
from tests.semantic_rails.hidden_absent import hidden_tokens, leaks
from tests.semantic_rails.test_visible_view import READER, _hide

DEPARTMENT, BUILDING = "entity.fac_department", "entity.fac_building"
WORK_ORDER, ASSIGNMENT = "entity.fac_work_order", "entity.fac_assignment"
STAFF, PROJECT = "entity.fac_staff", "entity.fac_project"
DEPARTMENTS_BY_BUILDING = {
    "select": ["measure.fac.department_count"],
    "group_by": ["dimension.fac_building_name"],
}
# (source, target, foreign key): every relationship is many to one, walked both ways.
FACILITIES = {
    "staff_department": ("staff", "department", "department_id"),
    "staff_building": ("staff", "building", "home_building_id"),
    "work_orders_department": ("work_order", "department", "department_id"),
    "work_orders_building": ("work_order", "building", "billing_building_id"),
    "work_orders_room": ("work_order", "room", "room_id"),
    "rooms_building": ("room", "building", "building_id"),
    "assignments_department": ("assignment", "department", "department_id"),
    "assignments_room": ("assignment", "room", "room_id"),
    "projects_department": ("project", "department", "department_id"),
    "tasks_project": ("task", "project", "project_id"),
    "tasks_building": ("task", "building", "building_id"),
    "visits_project": ("visit", "project", "project_id"),
    "visits_building": ("visit", "building", "building_id"),
}


def _rel(*names: str) -> list[str]:
    return [f"relationship.{name}" for name in names]


BILLING = _rel("work_orders_department", "work_orders_building")
SERVICED = _rel("work_orders_department", "work_orders_room", "rooms_building")
ASSIGNED = _rel("assignments_department", "assignments_room", "rooms_building")
ASSIGNED_BILLING = _rel(
    "assignments_department", "assignments_room", "work_orders_room", "work_orders_building"
)
HOME = _rel("staff_department", "staff_building")
TASKS = _rel("projects_department", "tasks_project", "tasks_building")
VISITS = _rel("projects_department", "visits_project", "visits_building")


TYPES = {"opened_at": "TIMESTAMP"}


def _facilities(root: Path, relationships: dict[str, tuple[str, str, str]] = FACILITIES) -> Path:
    """A package over ``relationships``: a key and a name on each entity, a department count
    and the building name to group it by."""
    pkg = root / "fac"
    (pkg / "models").mkdir(parents=True)
    keys = sorted({end for source, target, _ in relationships.values() for end in (source, target)})
    columns: dict[str, list[str]] = {key: [f"{key}_id", "name"] for key in keys}
    columns["department"].append("opened_at")
    for source, _target, column in relationships.values():
        columns[source].append(column)
    typed = {key: [f"{c} {TYPES.get(c, 'VARCHAR')}" for c in columns[key]] for key in keys}
    seed = "".join(f"CREATE TABLE {key}s ({', '.join(typed[key])});\n" for key in keys)
    (pkg / "seed.sql").write_text(seed)
    (pkg / "package.yml").write_text(
        textwrap.dedent(
            f"""
            schema_version: 1
            package:
              id: fac
              namespace: fac
              name: fac
              description: Departments and buildings.
              warehouse: duckdb
              default_db: {(root / "fac.duckdb").as_posix()}
              seed:
                kind: sql_script
                source: seed.sql
            defaults:
              dimension:
                groupable: true
                filterable: true
              relationship:
                traversal: [forward, reverse]
            """
        )
    )
    graph = {
        "entities": {
            key: {"label": key.replace("_", " ").title(), "key": [f"{key}_id"], "model": f"{key}s"}
            for key in keys
        },
        "relationships": {
            name: {
                "as": f"relationship.{name}",
                "entities": [source, target],
                "cardinality": "many_to_one",
                "via": [column],
                "target": [f"{target}_id"],
            }
            for name, (source, target, column) in relationships.items()
        },
    }
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}, sort_keys=False))
    for key in keys:
        model: dict[str, Any] = {"id": f"{key}s", "relation": f"{key}s", "entities": {key: {}}}
        model["dimensions"] = {"name": {"column": "name", "kind": "categorical"}}
        if key == "department":
            model["times"] = {
                "opened_at": {"column": "opened_at", "kind": "timestamp", "class": "event_time"}
            }
            model["measures"] = {
                "department_count": {
                    "kind": "entity_count",
                    "entity_key": "department_id",
                    "accumulation": {"kind": "population"},
                    "value_type": "count",
                }
            }
        (pkg / "models" / f"{key}s.yml").write_text(yaml.safe_dump({"model": model}))
    return pkg


@pytest.fixture(autouse=True)
def _external_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _refusal(runtime: Runtime, call: dict[str, Any], **options: Any) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(call, **options)
    assert caught.value.code == "AMBIGUOUS_PATH", caught.value
    return caught.value


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def test_each_group_is_one_meaning_and_a_role_fork_lists_both_roles(tmp_path: Path) -> None:
    """Seven routes in four groups, by the entity each reaches first. A work order's billing
    building and its serviced room's building are two meanings: both are offered, never the
    shorter one as the work orders' meaning. An assignment's room's building is its first
    route, the longer one through a work order counted. A project's two routes tie: both are
    offered. Every route is in exactly one group, and the flat options are every route."""
    runtime = Runtime.from_path(str(_facilities(tmp_path)))
    err = _refusal(runtime, DEPARTMENTS_BY_BUILDING)
    clarification = err.details["clarification"]
    by_path = {tuple(o["relationship_path"]): o["id"] for o in clarification["options"]}
    assert set(by_path) == {
        tuple(path) for path in (BILLING, SERVICED, ASSIGNED, ASSIGNED_BILLING, HOME, TASKS, VISITS)
    }
    assert [
        (group["via"], group["routes"], group["more_routes"], group["options"])
        for group in clarification["groups"]
    ] == [
        (STAFF, 1, 0, [by_path[tuple(HOME)]]),
        (WORK_ORDER, 2, 0, [by_path[tuple(BILLING)], by_path[tuple(SERVICED)]]),
        (ASSIGNMENT, 2, 1, [by_path[tuple(ASSIGNED)]]),
        (PROJECT, 2, 0, [by_path[tuple(TASKS)], by_path[tuple(VISITS)]]),
    ]
    assert [group["id"] for group in clarification["groups"]] == [
        by_path[tuple(HOME)],
        "via_work_order",
        by_path[tuple(ASSIGNED)],
        "via_project",
    ]
    counts = {key: clarification[key] for key in ("options_total", "groups_total", "listed")}
    assert counts == {"options_total": 7, "groups_total": 4, "listed": 6}
    assert clarification["ordered"] == ORDERED
    assert str(err) == (
        f"Ambiguous path from '{DEPARTMENT}' to '{BUILDING}'. Which Building does the question "
        "mean for a Department? the Building of any of the Department's Staffs; the Building of "
        "any of the Department's Work Orders or the Building of the Room of any of the "
        "Department's Work Orders; the Building of the Room of any of the Department's "
        "Assignments (+1 other route through Assignments); the Building of any of the Tasks of "
        "any of the Department's Projects or the Building of any of the Visits of any of the "
        "Department's Projects"
    )
    assert "groups lists the distinct meanings" in err.details["hint"]
    # Every listed option is a call ready to send.
    for option in clarification["options"]:
        assert option["query"]["routes"][-1]["via"] == option["relationship_path"]


def test_plan_asks_among_the_options_the_groups_list(tmp_path: Path) -> None:
    """``plan`` asks the same groups and offers only their options, each a draft ready to send;
    the route the assignments group counts is not offered, and its message says so."""
    runtime = Runtime.from_path(str(_facilities(tmp_path)))
    groups = _refusal(runtime, DEPARTMENTS_BY_BUILDING).details["clarification"]["groups"]
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        planned = adapter.call_tool("plan", {"intent": "department count by building name"})
    finally:
        adapter.close()
    assert planned["status"] == "needs_clarification"
    why = planned["why"]
    assert why["code"] == "AMBIGUOUS_PATH" and why["details"]["groups"] == groups
    assert "unrunnable" not in why["details"]
    listed = [option for group in groups for option in group.get("options") or ()]
    assert sorted(o["id"] for o in why["details"]["options"]) == sorted(listed)
    assert (why["details"]["options_total"], why["details"]["listed"]) == (7, 6)
    assert why["message"].endswith("1 other route between them is counted in groups, not offered.")


def test_a_fork_too_big_to_list_names_what_to_decide_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group whose routes tie and are more than ``EXPAND_MAX`` has nothing to send: it is
    counted, after every group with an option, and names the part a package author decides."""
    monkeypatch.setattr(route_groups_module, "EXPAND_MAX", 1)
    runtime = Runtime.from_path(str(_facilities(tmp_path)))
    err = _refusal(runtime, DEPARTMENTS_BY_BUILDING)
    fork = err.details["clarification"]["groups"][-1]
    assert fork == {
        "id": "via_project",
        "via": PROJECT,
        "routes": 2,
        "decide_first": {"source_entity": PROJECT, "target_entity": BUILDING},
    }
    assert err.details["clarification"]["listed"] == 4
    assert str(err).endswith("; 2 routes through Projects (a package author can define which one)")


def test_a_view_that_hides_a_groups_first_route_offers_the_next_one(tmp_path: Path) -> None:
    """Groups are made from the routes the caller sees: with the room-to-building relationship
    hidden, the assignment group's one visible route, the longer one through a work order, is
    its option, and nothing names what is hidden."""
    pkg = _facilities(tmp_path)
    config = _hide(load_package_config(str(pkg)), "relationship.rooms_building")
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out = adapter.call_tool(
            "execute",
            {"query": DEPARTMENTS_BY_BUILDING, "verbosity": "full"},
            request_context=context_from_policy_context(READER),
        )
    finally:
        adapter.close()
    error = out["errors"][0]
    assert error["code"] == "AMBIGUOUS_PATH", out
    clarification = error["details"]["clarification"]
    by_id = {option["id"]: option["relationship_path"] for option in clarification["options"]}
    assert [
        (group["via"], [by_id[option] for option in group["options"]])
        for group in clarification["groups"]
    ] == [
        (STAFF, [HOME]),
        (WORK_ORDER, [BILLING]),
        (PROJECT, [TASKS, VISITS]),
        (ASSIGNMENT, [ASSIGNED_BILLING]),
    ]
    hidden = frozenset({"relationship.rooms_building"})
    assert not leaks(out, hidden_tokens(config, hidden), request=DEPARTMENTS_BY_BUILDING)


REGION_FILTER = {
    "id": "policy.bank.region",
    "kind": "row_filter",
    "dimension": bank.REGION_NAME,
    "attribute": "region",
    "audiences": ["regional"],
}


def test_two_keys_one_hop_down_stay_two_options_and_the_row_filter_still_refuses(
    tmp_path: Path,
) -> None:
    """Invoice amount by region: an invoice's account holds two region keys (its branch and
    its billing region). The shared hop to the account is set aside, so the two keys are two
    groups of one option each, named by their columns. Under a region row filter the invoice
    model's route to the region is undecided, so it is unsupported: a caller limited to one
    region is refused invoices, never shown every region's. Grouping at the answer's edge
    leaves that coverage exactly as the resolver's flat options make it."""
    pkg = bank._write_package(
        tmp_path,
        relationships=("invoices_account", "accounts_branch_region", "accounts_billing_region"),
    )
    (pkg / "policies.yml").write_text(yaml.safe_dump({"semantic_policies": [REGION_FILTER]}))
    config = load_package_config(str(pkg))
    assert coverage(config, bank.REGION).unsupported[bank.INVOICE] == (
        "its route to the filtered model is undecided"
    )
    runtime = Runtime.from_path(str(pkg))
    err = _refusal(runtime, bank._query(bank.AMOUNT, group_by=[bank.REGION_NAME]))
    clarification = err.details["clarification"]
    meanings = {option["id"]: option["meaning"] for option in clarification["options"]}
    assert [(g["via"], g["routes"], g["more_routes"]) for g in clarification["groups"]] == [
        (bank.REGION, 1, 0),
        (bank.REGION, 1, 0),
    ]
    assert [meanings[group["options"][0]] for group in clarification["groups"]] == [
        "the Region (billing_region_id) of the Invoice's Account",
        "the Region (branch_region_id) of the Invoice's Account",
    ]
    west = RequestContext(audience="regional", attributes={"region": "North"})
    with pytest.raises(SemanticLayerError) as caught:
        runtime.query(bank._query(bank.AMOUNT), context=west)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["reason"] == "row_filter_coverage_unsupported"


def _hub(root: Path, activities: int) -> Path:
    """Departments and buildings related by ``activities`` tables, each holding both keys."""
    relationships: dict[str, tuple[str, str, str]] = {}
    for index in range(activities):
        key = f"activity_{index:02d}"
        relationships[f"{key}s_department"] = (key, "department", "department_id")
        relationships[f"{key}s_building"] = (key, "building", "building_id")
    return _facilities(root, relationships)


# The timing a response reports varies in length from call to call.
SLACK = 20


def _execute(adapter: SemanticLayerMCPAdapter, call: dict[str, Any], **args: Any) -> Any:
    return adapter.call_tool("execute", {"query": call, **args})


def test_a_route_question_over_its_budget_is_shortened_never_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A department reached through 60 activities: 60 groups. An answer that doesn't fit its
    budget keeps its question, shortened step by step down to the question and its counts, and
    says what it left out: never RESULT_TOO_LARGE, and the bigger the budget, the more groups
    it shows. Plan asks the same question within the default budget."""
    adapter = SemanticLayerMCPAdapter(Runtime.from_path(str(_hub(tmp_path, 60))))
    try:
        shown = []
        for budget in (32_000, 12_000, 6_000, 2_000):
            monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", str(budget))
            for mode in ("run", "validate"):
                out = _execute(adapter, DEPARTMENTS_BY_BUILDING, mode=mode)
                assert _size(out) <= budget
                (error,) = out["errors"]
                assert error["code"] == "AMBIGUOUS_PATH", out
                clarification = error["details"]["clarification"]
                assert clarification["question"] == (
                    "Which Building does the question mean for a Department?"
                )
                assert (clarification["options_total"], clarification["groups_total"]) == (60, 60)
                assert "project_status" in clarification["note"]
                groups = clarification.get("groups") or []
                assert {o["id"] for o in clarification.get("options") or []} == {
                    option for group in groups for option in group["options"]
                }
            shown.append(len(groups))
        assert shown[0] > shown[1] > shown[2] > shown[3] == 0
        monkeypatch.delenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS")
        planned = adapter.call_tool("plan", {"intent": "department count by building name"})
        assert planned["status"] == "needs_clarification", planned
        assert planned["why"]["code"] == "AMBIGUOUS_PATH"
        assert 0 < len(planned["why"]["details"]["groups"]) <= 60
    finally:
        adapter.close()


def test_each_step_is_taken_only_when_the_one_before_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole question when it fits; then only the options the groups list (the route the
    assignments group counts is left out); then one call, and per option the route to append
    to its routes."""
    adapter = SemanticLayerMCPAdapter(Runtime.from_path(str(_facilities(tmp_path))))
    try:
        whole = _execute(adapter, DEPARTMENTS_BY_BUILDING, mode="validate")
        clarification = whole["errors"][0]["details"]["clarification"]
        whole_options = clarification["options"]
        assert "note" not in clarification and len(whole_options) == 7
        monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", str(_size(whole) - SLACK))
        listed = _execute(adapter, DEPARTMENTS_BY_BUILDING, mode="validate")
        clarification = listed["errors"][0]["details"]["clarification"]
        assert clarification["note"].startswith("Only the options the groups list are shown.")
        assert len(clarification["options"]) == 6 and "call" not in clarification
        assert ASSIGNED_BILLING not in [o["relationship_path"] for o in clarification["options"]]
        monkeypatch.setenv("SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS", str(_size(listed) - SLACK))
        template = _execute(adapter, DEPARTMENTS_BY_BUILDING, mode="validate")
        clarification = template["errors"][0]["details"]["clarification"]
        assert clarification["call"] == DEPARTMENTS_BY_BUILDING
        assert clarification["note"].endswith("the route's via is the option's relationship_path.")
        paths = {o["id"]: o["relationship_path"] for o in whole_options}
        for option in clarification["options"]:
            assert "query" not in option and "relationship_path" not in option
            assert option["route"]["via"] == paths[option["id"]]
        assert len(clarification["groups"]) == 4
    finally:
        adapter.close()
