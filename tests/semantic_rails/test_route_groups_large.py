"""Route groups on a package of 85 marts, each two-way: 3,580 pairs whose routes need a decision.

An account reaches a brand by 212 routes: through each of 14 activities an account has, and on
from there. They are asked as 14 groups (11 options and 3 forks of 16 or 17 routes), the same in
``execute``, ``plan``, ``inspect`` and Architect's route census, and the answer fits its budget
for every undecided pair of the package with every group listed: never ``RESULT_TOO_LARGE``.

The package takes about 20 seconds to build, once per worker. The census walks every pair one
at a time, keeping no refusal once it is checked, so memory stays flat.
"""

from __future__ import annotations

import json
from copy import deepcopy
from itertools import chain
from typing import Any

import pytest

from scripts.large_warehouse import ALL_MARTS, LargeWorkspace, build_large_workspace
from semantic_rails.architect_mcp import PAGE_CHARS, PAGE_ITEMS, _report_page, _with_groups
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import package_route
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text
from semantic_rails.query_call import route_options
from semantic_rails.request_payload import max_result_chars
from semantic_rails.route_census import census_pairs
from semantic_rails.route_groups import grouped_refusal, shorter_route_asks
from semantic_rails.runtime import Runtime

# The package's build (about 20 seconds here) counts toward a test's time limit, and a slower
# machine can take three times as long: each test declares its limit.
pytestmark = pytest.mark.timeout(180)

ACCOUNT, BRAND = "entity.large_account", "entity.large_brand"
ACCOUNTS_BY_BRAND = {
    "select": ["measure.large.account_count"],
    "group_by": ["dimension.large_brand_brand_name"],
}


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


def _paged(config: Any, groups: list[dict[str, Any]], options: list[Any]) -> None:
    """Architect's census row of the pair, as ``project_status`` makes it, shows its groups and
    their options, and pages every one of its 212 routes once."""
    raised = _refusal(config, ACCOUNT, BRAND)
    assert raised is not None
    ends = {"source_entity": ACCOUNT, "target_entity": BRAND}
    pair = _with_groups(config, {**ends, "details": raised.details, "needed_by": []})
    whole = pair["details"]["clarification"]
    assert whole["groups"] == groups and len(whole["options"]) == 212
    report = {"route_census": {"undecided": [pair]}}

    page = _report_page(report, "route_census.undecided", 0, PAGE_ITEMS, {})
    assert _size(page) <= PAGE_CHARS
    shown = page["items"][0]["details"]["clarification"]
    assert shown["groups"] == groups
    assert [o["id"] for o in shown["options"]] == [o["id"] for o in options]
    path = "route_census.undecided.0.details.clarification.options"
    assert shown["options_note"].startswith(f"the 11 options its groups list, of 212 for {ACCOUNT}")
    assert page["pages"][path]["total_count"] == 212

    seen: list[Any] = []
    offset = 0
    while True:
        listed = _report_page(report, path, offset, PAGE_ITEMS, {})
        assert _size(listed) <= PAGE_CHARS
        seen += listed["items"]
        if not listed["has_more"]:
            break
        offset = listed["next_offset"]
    assert seen == whole["options"]


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
