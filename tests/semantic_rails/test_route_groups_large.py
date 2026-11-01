"""Route groups on a package of 85 marts, each two-way: 3,580 pairs whose routes need a decision.

An account reaches a brand by 212 routes: through each of 14 activities an account has, and on
from there. They are asked as 14 groups (11 options and 3 forks of 16 or 17 routes), the same in
``execute``, ``plan``, ``inspect`` and Architect's route census, and the answer fits its budget
for every undecided pair of the package with every group listed: never ``RESULT_TOO_LARGE``.
Each fork's routes are listed as parts a package author decides it by, for the asked pair only,
and recording a part marks each pair it moves off its row.

The package takes about 20 seconds to build, once per worker. The census walks every pair one
at a time, keeping no refusal once it is checked, so memory stays flat.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from itertools import chain
from typing import Any

import pytest

from scripts.large_warehouse import ALL_MARTS, LargeWorkspace, build_large_workspace
from semantic_rails.architect_mcp import PAGE_CHARS, PAGE_ITEMS, _report_page, _with_groups
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config_parts.route_rows import walk_entities
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import package_hop_limit, package_route, route_hops
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text
from semantic_rails.query_call import route_options
from semantic_rails.request_payload import max_result_chars
from semantic_rails.route_census import (
    EXCLUDED_NOTE,
    OFF_ROW_NOTES,
    census_pairs,
    route_changes,
    unkept_route_changes,
)
from semantic_rails.route_groups import grouped_refusal, route_groups, shorter_route_asks
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PathPreferenceConfig

# The package's build (about 20 seconds here) counts toward a test's time limit, and a slower
# machine can take three times as long: each test declares its limit.
pytestmark = pytest.mark.timeout(180)

ACCOUNT, BRAND = "entity.large_account", "entity.large_brand"
ACCOUNTS_BY_BRAND = {
    "select": ["measure.large.account_count"],
    "group_by": ["dimension.large_brand_brand_name"],
}
EMPLOYEE = "entity.large_employee"
# Two fork parts, each 4 hops where its pair's shortest route is 2, and the pairs recording
# each one moves: answered off its row (``inherited``), and refused (``excluded_by_decision``).
# The first is previewed through Architect; the second's changes are read from the package as
# loaded, whose own resolutions are already cached.
OFF_ROW_PARTS = {
    (BRAND, EMPLOYEE): ("people_order_account_people_visit_employee", 24, 6),
    (ACCOUNT, EMPLOYEE): ("finance_ticket_brand_people_visit_employee", 0, 26),
}
PREVIEWED = (BRAND, EMPLOYEE)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> LargeWorkspace:
    return build_large_workspace(tmp_path_factory.mktemp("all_marts"), marts=ALL_MARTS)


@pytest.fixture(scope="module")
def runtime(workspace: LargeWorkspace) -> Runtime:
    return Runtime.from_path(str(workspace.root / workspace.package))


@pytest.fixture(scope="module")
def answer(runtime: Runtime) -> Any:
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        return adapter.call_tool("execute", {"query": ACCOUNTS_BY_BRAND, "mode": "validate"})
    finally:
        adapter.close()


def _refusal(config: Any, start: str, target: str) -> SemanticLayerError | None:
    """The pair's route refusal (``AMBIGUOUS_PATH``), or None when the package answers it."""
    try:
        package_route(config, start=start, target=target)
    except SemanticLayerError as exc:
        if exc.code == "AMBIGUOUS_PATH":
            return exc
    return None


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _asked(answer: Any, runtime: Runtime) -> list[dict[str, Any]]:
    """The account-to-brand question as ``execute`` and ``plan`` ask it: 14 groups; and the
    ``inspect`` card's count of them."""
    (error,) = answer["errors"]
    assert error["code"] == "AMBIGUOUS_PATH"
    clarification = error["details"]["clarification"]
    groups: list[dict[str, Any]] = clarification["groups"]
    assert (clarification["options_total"], clarification["groups_total"]) == (212, 14)
    assert clarification["listed"] == 11 == sum(len(g.get("options") or ()) for g in groups)
    assert sum(group["routes"] for group in groups) == 212
    forks = [group for group in groups if "decide_first" in group]
    assert sorted(group["routes"] for group in forks) == [16, 17, 17]
    assert all(group["decide_first"]["target_entity"] == BRAND for group in forks)
    # The whole question (212 ready calls) is over the budget: the groups' 11 options are shown.
    assert _size(answer) <= max_result_chars() and len(clarification["options"]) == 11
    assert clarification["note"].startswith("Only the options the groups list are shown.")
    assert len(error["message"]) <= 1_400
    # One call and a route per option, if the answer needs it: about 8,000 characters.
    template = list(shorter_route_asks(answer))[1]
    shorter = template["errors"][0]["details"]["clarification"]
    assert "call" in shorter and len(shorter["groups"]) == 14
    assert _size(shorter) <= 8_100

    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        planned = adapter.call_tool("plan", {"intent": "account count by brand name"})
        card = adapter.call_tool("inspect", {"object_id": ACCOUNT, "verbosity": "compact"})
    finally:
        adapter.close()
    assert planned["status"] == "needs_clarification" and _size(planned) <= max_result_chars()
    assert planned["why"]["code"] == "AMBIGUOUS_PATH"
    assert planned["why"]["details"]["groups"] == groups
    (brand,) = [row for row in card["card"]["reachable_entities"] if row["id"] == BRAND]
    assert brand == {"id": BRAND, "route_decision_required": True, "routes": 212, "groups": 14}
    return groups


def _fits(config: Any, raised: SemanticLayerError, answer: Any, budget: int) -> tuple[int, bool]:
    """The first step at which the pair's question, in the answer ``execute`` gives (the
    account's refusal, with that pair's message and details), fits ``budget``: 0 whole, 1 the
    options its groups list, 2 one call and a route per option, 3 or more fewer groups; and
    whether its groups have no fork."""
    refusal = grouped_refusal(raised, config)
    details = refusal.details
    route_options(ACCOUNTS_BY_BRAND, details)
    fork_free = not any("decide_first" in g for g in details["clarification"]["groups"])
    shown = deepcopy(answer)
    shown["errors"][0].update(message=str(refusal), details=details)
    shown["error"]["message"] = str(refusal)
    tries = chain([shown], shorter_route_asks(shown))
    return next(n for n, step in enumerate(tries) if len(json_text(step)) <= budget), fork_free


def _without_parts(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**g, "decide_first": {k: v for k, v in g["decide_first"].items() if k != "parts"}}
        if "decide_first" in g
        else g
        for g in groups
    ]


def _walk(report: Any, path: str) -> list[Any]:
    """Every item of the list at ``path``, page by page, each page under its budget."""
    seen: list[Any] = []
    offset = 0
    while True:
        page = _report_page(report, path, offset, PAGE_ITEMS, {})
        assert _size(page) <= PAGE_CHARS
        seen += page["items"]
        if not page["has_more"]:
            return seen
        offset = page["next_offset"]


def _paged(config: Any, groups: list[dict[str, Any]], options: list[Any]) -> None:
    """Architect's census row of the pair, as ``project_status`` makes it, shows its groups and
    their options, pages every one of its 212 routes once, and each fork's routes as the parts
    a package author decides."""
    raised = _refusal(config, ACCOUNT, BRAND)
    assert raised is not None
    ends = {"source_entity": ACCOUNT, "target_entity": BRAND}
    pair = _with_groups(config, {**ends, "details": raised.details, "needed_by": []})
    whole = pair["details"]["clarification"]
    assert _without_parts(whole["groups"]) == groups and len(whole["options"]) == 212
    report = {"route_census": {"undecided": [pair]}}

    page = _report_page(report, "route_census.undecided", 0, PAGE_ITEMS, {})
    assert _size(page) <= PAGE_CHARS
    shown = page["items"][0]["details"]["clarification"]
    assert shown["groups"] == groups
    assert [o["id"] for o in shown["options"]] == [o["id"] for o in options]
    path = "route_census.undecided.0.details.clarification"
    assert shown["options_note"].startswith(f"the 11 options its groups list, of 212 for {ACCOUNT}")
    assert page["pages"][f"{path}.options"]["total_count"] == 212
    assert _walk(report, f"{path}.options") == whole["options"]

    decisions = {o["id"]: o["decision"] for o in whole["options"]}
    forks = [(n, g) for n, g in enumerate(whole["groups"]) if "decide_first" in g]
    assert len(forks) == 3
    for index, fork in forks:
        where = f"{path}.groups.{index}.decide_first.parts"
        assert page["pages"][where] == {"total_count": fork["routes"], "next_offset": 0}
        parts = _walk(report, where)
        assert parts == fork["decide_first"]["parts"] and len(parts) == fork["routes"]
        # Each part is its route from the fork's entity on, and records the asked pair's row.
        for part in parts:
            row = decisions[part["id"]]
            assert part["decision"] == row and row["source_entity"] == ACCOUNT
            assert (
                row["relationship_path"][-len(part["relationship_path"]) :]
                == (part["relationship_path"])
            )


def test_212_routes_are_asked_as_14_groups_everywhere(runtime: Runtime, answer: Any) -> None:
    groups = _asked(answer, runtime)
    _paged(runtime.config, groups, answer["errors"][0]["details"]["clarification"]["options"])


def test_every_undecided_pair_fits_its_answer_with_all_its_groups(
    runtime: Runtime, answer: Any
) -> None:
    """All 3,580 undecided pairs, one at a time: each fits at the second step or before, and
    848 of them have no fork."""
    config, budget = runtime.config, max_result_chars()
    undecided = fork_free = latest = 0
    for start, target in census_pairs(config):
        raised = _refusal(config, start, target)
        if raised is None:
            continue
        undecided += 1
        step, no_fork = _fits(config, raised, answer, budget)
        fork_free += no_fork
        assert step <= 2, f"{start} to {target} lost a group to fit (step {step})"
        latest = max(latest, step)
    assert (undecided, fork_free, latest) == (3_580, 848, 2)


def test_a_package_author_decides_each_fork_by_one_of_its_parts(runtime: Runtime) -> None:
    """Every fork's parts, on every undecided pair, are free of the start and shorter than the
    hop ceiling. On each of the first 60 pairs asked as one option and one fork, recording the
    fork's first part's row decides the pair by exactly that route, and leaves the fork's own
    pair undecided."""
    config = runtime.config
    ceiling = package_hop_limit(config)
    two_groups = []
    for start, target in census_pairs(config):
        raised = _refusal(config, start, target)
        if raised is None:
            continue
        options = raised.details["clarification"]["options"]
        grouped = route_groups(config, start, target, options, parts=True)
        forks = [g for g in grouped["groups"] if "decide_first" in g]
        for fork in forks:
            for part in fork["decide_first"]["parts"]:
                hops = route_hops(config, fork["via"], part["relationship_path"])
                assert start not in {far for _rel, far, _reach in hops}
                assert len(part["relationship_path"]) <= ceiling - 1
        if grouped["groups_total"] == 2 and len(forks) == 1:
            two_groups.append(
                (start, target, forks[0]["via"], forks[0]["decide_first"]["parts"][0])
            )
    assert len(two_groups) == 233
    for start, target, via, part in sorted(two_groups, key=lambda row: row[:2])[:60]:
        row = part["decision"]
        recorded = replace(
            config, path_preferences=[*config.path_preferences, PathPreferenceConfig(**row)]
        )
        resolution = package_route(recorded, start=start, target=target)
        assert (resolution.basis, list(resolution.routes[0])) == (
            "decided",
            row["relationship_path"],
        )
        with pytest.raises(SemanticLayerError) as own_pair:
            package_route(recorded, start=via, target=target)
        assert own_pair.value.code == "AMBIGUOUS_PATH"


@pytest.mark.parametrize("pair", list(OFF_ROW_PARTS), ids=lambda pair: pair[0].split("_")[-1])
def test_a_part_longer_than_its_pairs_shortest_route_marks_each_pair_it_moves_off_its_row(
    workspace: LargeWorkspace, runtime: Runtime, pair: tuple[str, str]
) -> None:
    """Recording such a part moves the asked pair and its reverse along its row, unmarked, and
    other pairs off it: ``route_changes`` (in the ``record_route_decision`` preview) marks each
    one answered by the only route its exclusion left, a route not through the asked pair, and
    each one refused."""
    config, (start, target) = runtime.config, pair
    part_id, answered, refused = OFF_ROW_PARTS[pair]
    raised = _refusal(config, start, target)
    assert raised is not None
    options = raised.details["clarification"]["options"]
    groups = route_groups(config, start, target, options, parts=True)["groups"]
    (part,) = [
        part
        for group in groups
        if "decide_first" in group
        for part in group["decide_first"]["parts"]
        if part["id"] == part_id
    ]
    if pair == PREVIEWED:
        root, package = workspace.root, workspace.package
        project = ArchitectProject(root / package, workspace_root=root)
        preview = project.record_route_decision(
            **part["decision"], validate_after=False, dry_run=True
        )
        changes = preview.report["route_changes"]
    else:
        row = PathPreferenceConfig(**part["decision"])
        recorded = replace(config, path_preferences=[*config.path_preferences, row])
        changes = route_changes(config, recorded)
        # Written by hand, not as a decision, the row is refused for the pairs that answered,
        # each noted the same way.
        unkept = unkept_route_changes(config, recorded)
        assert unkept == [c for c in changes if "note" in c]
        assert len(unkept) == refused and {c["note"] for c in unkept} == {EXCLUDED_NOTE}
    unmarked = {(c["source_entity"], c["target_entity"]) for c in changes if "note" not in c}
    assert unmarked == {(start, target), (target, start)}
    notes = Counter(change.get("note") for change in changes)
    assert (notes[OFF_ROW_NOTES["inherited"]], notes[EXCLUDED_NOTE]) == (answered, refused)
    relationships = {rel.id: rel for rel in config.relationships}
    for change in changes:
        if change.get("note") == EXCLUDED_NOTE:
            assert change["head"] == {"refused": "PATH_NOT_FOUND"}
        elif change.get("note"):
            path = change["head"]["relationship_path"]
            walked = walk_entities(relationships, change["source_entity"], path)
            assert not {start, target} <= set(walked)
