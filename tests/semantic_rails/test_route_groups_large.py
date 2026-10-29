"""Route groups on a package of 85 marts, each two-way: 3,580 pairs whose routes need a decision.

An account reaches a brand by 212 routes: through each of 14 activities an account has, and on
from there. They are asked as 14 groups (11 options and 3 forks of 16 or 17 routes), the same in
``execute``, ``plan``, ``inspect`` and Architect's route census, and the answer fits its budget
for every undecided pair of the package with every group listed: never ``RESULT_TOO_LARGE``.

One test, so the package (about 20 seconds to build) and its census are made once.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from itertools import chain
from pathlib import Path
from typing import Any

import pytest

from scripts.large_warehouse import ALL_MARTS, build_large_workspace, call_tool
from semantic_rails.architect_mcp import (
    PAGE_CHARS,
    PAGE_ITEMS,
    _report_page,
    create_architect_mcp_server,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import package_hop_limit, package_route, route_hops
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text
from semantic_rails.query_call import route_options
from semantic_rails.request_payload import max_result_chars
from semantic_rails.route_census import census_pairs, resolve_pairs
from semantic_rails.route_groups import grouped_refusal, route_groups, shorter_route_asks
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PathPreferenceConfig

ACCOUNT, BRAND = "entity.large_account", "entity.large_brand"
ACCOUNTS_BY_BRAND = {
    "select": ["measure.large.account_count"],
    "group_by": ["dimension.large_brand_brand_name"],
}


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


def _fit_every_pair(runtime: Runtime, undecided: list[Any], answer: Any) -> None:
    """Each undecided pair's question in the answer ``execute`` gives (the account's refusal,
    with that pair's message and details) fits whole, or with only the options its groups
    list, or as one call and a route per option: none needs fewer groups than it has."""
    config = runtime.config
    assert len(undecided) == 3_580
    budget, steps, fork_free = max_result_chars(), [], 0
    for _pair, outcome in undecided:
        raised = SemanticLayerError("AMBIGUOUS_PATH", "", details=deepcopy(outcome.details))
        refusal = grouped_refusal(raised, config)
        details = refusal.details
        route_options(ACCOUNTS_BY_BRAND, details)
        fork_free += not any("decide_first" in g for g in details["clarification"]["groups"])
        shown = deepcopy(answer)
        shown["errors"][0].update(message=str(refusal), details=details)
        shown["error"]["message"] = str(refusal)
        tries = chain([shown], shorter_route_asks(shown))
        steps.append(next(n for n, step in enumerate(tries) if len(json_text(step)) <= budget))
    assert fork_free == 848
    assert max(steps) <= 2, "a pair's question lost a group to fit"


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


def _paged(pair: Any, groups: list[dict[str, Any]], options: list[Any]) -> None:
    """Architect's census row of the pair shows its groups and their options, pages every one
    of its 212 routes once, and each fork's routes as the parts a package author decides."""
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


def _decide_forks(runtime: Runtime, undecided: list[Any]) -> None:
    """Every fork's parts are free of the start and shorter than the hop ceiling. On each of
    the first 60 pairs asked as one option and one fork, recording the fork's first part's row
    decides the pair by exactly that route, and leaves the fork's own pair undecided."""
    config = runtime.config
    ceiling = package_hop_limit(config)
    two_groups = []
    for (start, target), outcome in undecided:
        options = outcome.details["clarification"]["options"]
        grouped = route_groups(config, start, target, options, parts=True)
        forks = [g for g in grouped["groups"] if "decide_first" in g]
        for fork in forks:
            for part in fork["decide_first"]["parts"]:
                hops = route_hops(config, fork["via"], part["relationship_path"])
                assert start not in {far for _rel, far, _reach in hops}
                assert len(part["relationship_path"]) <= ceiling - 1
        if grouped["groups_total"] == 2 and len(forks) == 1:
            two_groups.append((start, target, forks[0]))
    assert len(two_groups) >= 60
    for start, target, fork in two_groups[:60]:
        row = fork["decide_first"]["parts"][0]["decision"]
        recorded = replace(
            config, path_preferences=[*config.path_preferences, PathPreferenceConfig(**row)]
        )
        resolution = package_route(recorded, start=start, target=target)
        assert (resolution.basis, list(resolution.routes[0])) == (
            "decided",
            row["relationship_path"],
        )
        with pytest.raises(SemanticLayerError) as own_pair:
            package_route(recorded, start=fork["via"], target=target)
        assert own_pair.value.code == "AMBIGUOUS_PATH"


def test_212_routes_are_14_groups_everywhere_and_every_pair_fits_with_all_its_groups(
    tmp_path: Path,
) -> None:
    workspace = build_large_workspace(tmp_path, marts=ALL_MARTS)
    runtime = Runtime.from_path(str(workspace.root / workspace.package))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        answer = adapter.call_tool("execute", {"query": ACCOUNTS_BY_BRAND, "mode": "validate"})
    finally:
        adapter.close()
    groups = _asked(answer, runtime)

    server = create_architect_mcp_server(workspace_root=workspace.root)
    full = call_tool(
        server, "project_status", {"project_path": workspace.package, "full_report": True}
    )
    # The whole census is about 115 MB; keep the one pair.
    (pair,) = [
        row
        for row in full.pop("route_census")["undecided"]
        if (row["source_entity"], row["target_entity"]) == (ACCOUNT, BRAND)
    ]
    del full
    _paged(pair, groups, answer["errors"][0]["details"]["clarification"]["options"])
    outcomes = resolve_pairs(runtime.config, census_pairs(runtime.config))
    undecided = [(p, o) for p, o in outcomes.items() if o.refused == "AMBIGUOUS_PATH"]
    _fit_every_pair(runtime, undecided, answer)
    _decide_forks(runtime, sorted(undecided, key=lambda row: row[0]))
