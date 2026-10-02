"""The route census, the route changes impact reports, and Architect's kept routes.

Which route a question means is a business definition (``test_route_resolution.py``). An author
sees every entity pair that still needs one (``route_census``), a review sees every pair whose
answer a change moves (``impact_report``'s ``route_changes``), and an Architect edit that would
move an answer records the earlier route in the same change instead.

Fixture: the accounts, owners, regions, memberships and invoices package of
``test_route_resolution.py``, on DuckDB. Gold values come from plain SQL over its seed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

import semantic_rails.fanout as fanout_module
import semantic_rails.route_census as census_module
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import ProjectFileUpdate, ProjectTransaction
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import resolve_path
from semantic_rails.package_tools import impact_report, promote_package_report
from semantic_rails.route_census import census_pairs, route_census
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_route_resolution import (
    _RELATIONSHIPS,
    ACCOUNT,
    ACCOUNT_KIND,
    AMOUNT,
    AMOUNT_BY_BRANCH,
    AMOUNT_BY_HOME,
    BALANCE,
    BRANCH,
    INVOICE,
    INVOICE_ACCOUNT,
    INVOICE_BRANCH,
    INVOICE_HOME,
    MEMBERSHIP,
    OWNER,
    OWNER_NAME,
    REGION,
    REGION_COUNT,
    REGION_NAME,
    ROOT,
    ROUTE_DECISIONS,
    SHIPPED_PACKAGES,
    TIER,
    _gold,
    _pin,
    _query,
    _rows,
    _write_package,
)

INVOICE_ID = "dimension.bank_invoice_id"
OWNS = "relationship.accounts_owner"


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _pairs(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(row["source_entity"], row["target_entity"]) for row in rows]


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------

# The diamond's pairs a query is refused on: every measure's entity (account, invoice, region)
# to every other entity it reaches. The owner starts no measure.
DIAMOND_UNDECIDED = [
    (ACCOUNT, MEMBERSHIP),
    (INVOICE, MEMBERSHIP),
    (INVOICE, OWNER),
    (INVOICE, REGION),
    (REGION, ACCOUNT),
    (REGION, INVOICE),
    (REGION, MEMBERSHIP),
    (REGION, OWNER),
]
# A question for each pair: a measure of the start grouped by a dimension of the target.
MEASURE = {ACCOUNT: BALANCE, INVOICE: AMOUNT, REGION: REGION_COUNT}
DIMENSION = {
    REGION: REGION_NAME,
    OWNER: OWNER_NAME,
    MEMBERSHIP: TIER,
    ACCOUNT: ACCOUNT_KIND,
    INVOICE: INVOICE_ID,
}


def test_the_census_lists_each_refused_pair_with_the_refusal_its_query_raises(tmp_path):
    pkg = _write_package(tmp_path)
    config = load_package_config(str(pkg))
    census = route_census(config)
    undecided = {
        pair: row["details"]
        for pair, row in zip(_pairs(census["undecided"]), census["undecided"], strict=True)
    }
    assert list(undecided) == DIAMOND_UNDECIDED
    runtime = Runtime.from_path(str(pkg))
    for (start, target), details in undecided.items():
        with pytest.raises(SemanticLayerError) as exc_info:
            runtime.query(_query(MEASURE[start], group_by=[DIMENSION[target]]))
        assert exc_info.value.code == "AMBIGUOUS_PATH"
        assert exc_info.value.details == details
    # The start's own key answers these two, by a rule rather than a decision: to confirm.
    assert census["assumed"] == [
        {
            "source_entity": ACCOUNT,
            "target_entity": OWNER,
            "relationship_path": [OWNS],
            "basis": "colocated_key",
        },
        {
            "source_entity": ACCOUNT,
            "target_entity": REGION,
            "relationship_path": BRANCH,
            "basis": "colocated_key",
        },
    ]
    # A pair with one route is in neither list.
    listed = {*undecided, *_pairs(census["assumed"])}
    assert {(ACCOUNT, INVOICE), (INVOICE, ACCOUNT)} <= set(census_pairs(config)) - listed


def test_census_pairs_start_at_a_measure_and_end_at_a_dimension(tmp_path):
    config = load_package_config(str(_write_package(tmp_path)))
    # The owner is refused on the invoice, but starts no measure, so no question asks it.
    with pytest.raises(SemanticLayerError) as exc_info:
        resolve_path(config, start=OWNER, target=INVOICE)
    assert exc_info.value.code == "AMBIGUOUS_PATH"
    assert OWNER not in {start for start, _ in census_pairs(config)}
    # Without a dimension the owner is no target either.
    no_owner_fields = replace(
        config, dimensions=[row for row in config.dimensions if row.entity != OWNER]
    )
    assert _pairs(route_census(no_owner_fields)["undecided"]) == [
        pair for pair in DIAMOND_UNDECIDED if pair[1] != OWNER
    ]


def test_the_census_asks_the_resolver_once_per_pair_and_reuses_its_cache(tmp_path, monkeypatch):
    config = load_package_config(str(_write_package(tmp_path)))
    asked: list[tuple[str, str]] = []
    enumerated: list[tuple[str, str]] = []
    resolve, uncached = census_module.resolve_path, fanout_module._resolve_uncached

    def counted_resolve(config, *, start, target):
        asked.append((start, target))
        return resolve(config, start=start, target=target)

    def counted_uncached(config, start, target):
        enumerated.append((start, target))
        return uncached(config, start, target)

    monkeypatch.setattr(census_module, "resolve_path", counted_resolve)
    monkeypatch.setattr(fanout_module, "_resolve_uncached", counted_uncached)
    census = route_census(config)
    pairs = census_pairs(config)
    assert asked == pairs
    assert enumerated == pairs
    # A second census, and a query's own resolution (an answer or a refusal), read the cache.
    assert route_census(config) == census
    assert resolve_path(config, start=ACCOUNT, target=REGION)[0] == BRANCH
    with pytest.raises(SemanticLayerError):
        resolve_path(config, start=INVOICE, target=REGION)
    assert enumerated == pairs


# Shipped packages whose relationships leave pairs undecided. Each needs a business decision
# recorded in the package (a follow-up), so they are pinned here to the reviewed snapshot.
SHIPPED_UNDECIDED = {
    "configs/semantic_rails/jaffle_shop": 21,
    "comparisons/semantic_layers/semantic_rails/package": 16,
}


@pytest.mark.parametrize("package", SHIPPED_PACKAGES)
def test_shipped_packages_list_only_their_reviewed_undecided_pairs(package):
    config = load_package_config(str(ROOT / package))
    pairs = set(census_pairs(config))
    refused = json.loads(ROUTE_DECISIONS.read_text(encoding="utf-8"))[package]["refused"]
    undecided = [list(pair) for pair in _pairs(route_census(config)["undecided"])]
    assert undecided == [pair for pair in refused if tuple(pair) in pairs]
    assert len(undecided) == SHIPPED_UNDECIDED.get(package, 0)


def _mcp(server, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(server.call_tool(name, arguments))[1]


def test_status_setup_and_promotion_point_at_the_undecided_pairs(tmp_path):
    pkg = _write_package(tmp_path)
    server = create_architect_mcp_server(workspace_root=tmp_path)
    status = _mcp(server, "project_status", {"project_path": str(pkg)})
    assert _pairs(status["route_census"]["undecided"]) == DIAMOND_UNDECIDED
    assert status["route_census"] == status["parse"]["route_census"]
    assert status["next_actions"][0].startswith("Decide the 8 undecided join routes")
    for result in (
        _mcp(server, "setup_project_dialog", {"package_id": "fresh"}),
        _mcp(
            server,
            "create_project",
            {"package_id": "fresh", "expected_revision": "absent", "idempotency_key": "new"},
        ),
    ):
        assert any("route_census.undecided" in action for action in result["next_actions"])
    promotion = promote_package_report(PackageReference(source_path=str(pkg)), environment="dev")
    (advisory,) = promotion["advisories"]
    assert (advisory["code"], advisory["details"]["count"]) == ("ROUTES_UNDECIDED", 8)
    assert "ROUTES_UNDECIDED" not in json.dumps(promotion["blockers"])


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------

# An invoice reaches a region through its account's branch: one route.
BASE = ("invoices_account", "accounts_branch_region", "accounts_owner")
# The owner's home region adds a second route wherever the owner lies between the two.
SECOND_ROUTE = (*BASE, "owners_home_region")
MOVED = [(INVOICE, OWNER), (INVOICE, REGION), (REGION, ACCOUNT), (REGION, INVOICE), (REGION, OWNER)]
REGIONS_BY_BRANCH_ACCOUNT = (
    "SELECT a.account_kind, COUNT(DISTINCT r.region_id) FROM regions r "
    "JOIN accounts a ON a.branch_region_id = r.region_id GROUP BY 1"
)
AMOUNT_BY_ISSUED = (
    "SELECT r.region_name, SUM(i.amount) FROM invoices i "
    "JOIN regions r ON r.region_id = i.issued_region_id GROUP BY 1"
)
# What the base answers: the amount by the account's branch region, regions by account kind.
BASE_GOLD = {"amount": AMOUNT_BY_BRANCH, "regions": REGIONS_BY_BRANCH_ACCOUNT}


def _answers(pkg: Path) -> dict[str, list[tuple]]:
    runtime = Runtime.from_path(str(pkg))
    amount = runtime.query(_query(AMOUNT, group_by=[REGION_NAME]))
    regions = runtime.query(_query(REGION_COUNT, group_by=[ACCOUNT_KIND]))
    return {
        "amount": _rows(amount, [REGION_NAME, "v"]),
        "regions": _rows(regions, [ACCOUNT_KIND, "v"]),
    }


def _impact(base: Path, head: Path) -> dict[str, Any]:
    return impact_report(PackageReference(source_path=str(head)), compare_path=str(base))


def test_impact_lists_each_answer_a_second_route_moves_with_the_row_that_keeps_it(tmp_path):
    base = _write_package(tmp_path / "base", relationships=BASE)
    head = _write_package(tmp_path / "head", relationships=SECOND_ROUTE)
    report = _impact(base, head)
    assert _pairs(report["route_changes"]) == MOVED
    assert report["route_changes"][1] == {
        "source_entity": INVOICE,
        "target_entity": REGION,
        "base": {"relationship_path": INVOICE_BRANCH},
        "head": {"refused": "AMBIGUOUS_PATH"},
        "keep_base": _pin(INVOICE, REGION, INVOICE_BRANCH),
    }
    assert report["impact"]["risk"] == "high"
    diff_behavior = sum(1 for change in report["changes"] if change["behavior_change"])
    assert report["impact"]["changed_behavior_count"] == diff_behavior + len(MOVED)
    assert (
        "- Invoice to Region: was Invoice → Account → Region, now refused (AMBIGUOUS_PATH); "
        "`keep_base` keeps the earlier route"
    ) in report["markdown_summary"].splitlines()
    # The head with every keep_base row answers exactly as the base did.
    rows = [change["keep_base"] for change in report["route_changes"]]
    kept = _write_package(tmp_path / "kept", relationships=SECOND_ROUTE, pins=rows)
    gold = {name: _gold(sql) for name, sql in BASE_GOLD.items()}
    assert _answers(base) == gold
    assert _answers(kept) == gold
    assert _impact(base, kept)["route_changes"] == []
    assert _gold(AMOUNT_BY_BRANCH) != _gold(AMOUNT_BY_HOME)


def test_impact_without_a_route_change_lists_none(tmp_path):
    base = _write_package(tmp_path / "base")
    same = _impact(base, base)
    assert (same["route_changes"], same["impact"]["risk"]) == ([], "low")
    relabelled = _write_package(tmp_path / "head", labels={"accounts_owner": "Account owner"})
    report = _impact(base, relabelled)
    assert report["route_changes"] == []
    assert "Route Changes" not in report["markdown_summary"]


def test_impact_lists_a_row_that_answers_a_pair_the_base_refused(tmp_path):
    base = _write_package(tmp_path / "base")
    head = _write_package(tmp_path / "head", pins=[_pin(INVOICE, REGION, INVOICE_HOME)])
    assert _impact(base, head)["route_changes"] == [
        {
            "source_entity": INVOICE,
            "target_entity": REGION,
            "base": {"refused": "AMBIGUOUS_PATH"},
            "head": {"relationship_path": INVOICE_HOME},
            "keep_base": None,
        }
    ]


# ---------------------------------------------------------------------------
# Architect keeps the earlier route
# ---------------------------------------------------------------------------


def _architect(tmp_path: Path, relationships: tuple[str, ...] = BASE) -> ArchitectProject:
    return ArchitectProject(
        _write_package(tmp_path, relationships=relationships), workspace_root=tmp_path
    )


def _graph(project: ArchitectProject) -> dict[str, Any]:
    return yaml.safe_load((project.project_path / "graph.yml").read_text())


def _with_home_region(project: ArchitectProject) -> dict[str, Any]:
    """graph.yml with the owner's home region added as an authored relationship."""
    graph = _graph(project)
    source, target, via, key = _RELATIONSHIPS["owners_home_region"]
    graph["graph"]["relationships"]["owners_home_region"] = {
        "id": "relationship.owners_home_region",
        "entities": [source, target],
        "cardinality": "many_to_one",
        "via": [via],
        "target": [key],
    }
    return graph


@pytest.mark.parametrize(
    ("from_entity", "columns", "new_route"),
    [
        # A second route through the owner: unrecorded, the pair would be refused.
        pytest.param(
            "owner",
            ["home_region_id"],
            [*INVOICE_ACCOUNT, OWNS, "relationship.owners_region"],
            id="second-route",
        ),
        # The new key is on the start's own table: unrecorded, the pair would take it.
        pytest.param(
            "invoice", ["issued_region_id"], ["relationship.invoices_region"], id="own-key"
        ),
    ],
)
def test_a_relationship_that_moves_an_answer_records_the_earlier_route(
    tmp_path, from_entity, columns, new_route
):
    """The pin holds whichever way the resolver would have answered the new pair."""
    project = _architect(tmp_path)
    report = project.upsert_relationship(
        from_entity=from_entity, to_entity="region", columns=columns
    ).report
    assert report["ok"] is True, report
    added = dict(
        zip(
            _pairs([e["row"] for e in report["route_decisions_added"]]),
            report["route_decisions_added"],
            strict=True,
        )
    )
    assert added[(INVOICE, REGION)] == {
        "row": _pin(INVOICE, REGION, INVOICE_BRANCH),
        "new_routes": [new_route],
    }
    # The rows are in the same change, and nothing answers differently.
    assert "graph.yml" in report["changed_files"]
    assert _graph(project)["graph"]["path_preferences"] == [
        entry["row"] for entry in report["route_decisions_added"]
    ]
    assert report["route_changes"] == []
    assert _answers(project.project_path) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}
    assert _gold(AMOUNT_BY_BRANCH) != _gold(AMOUNT_BY_ISSUED)


def test_rows_go_shortest_pair_first_and_a_dry_run_shows_them(tmp_path):
    project = _architect(tmp_path)
    revision = project.revision()
    report = project.upsert_relationship(
        from_entity="owner", to_entity="region", columns=["home_region_id"], dry_run=True
    ).report
    assert (report["status"], project.revision()) == ("preview", revision)
    rows = [entry["row"] for entry in report["route_decisions_added"]]
    assert _pairs(rows) == [
        (REGION, ACCOUNT),
        *(pair for pair in MOVED if pair != (REGION, ACCOUNT)),
    ]
    assert [len(row["relationship_path"]) for row in rows] == [1, 2, 2, 2, 2]
    (graph,) = [change for change in report["changes"] if change["path"] == "graph.yml"]
    added = [line[1:] for line in graph["diff"].splitlines()[2:] if line.startswith("+")]
    assert yaml.safe_load("\n".join(added)) == {"path_preferences": rows}


def test_a_change_that_moves_no_answer_records_nothing(tmp_path):
    project = _architect(tmp_path, relationships=("invoices_account", "accounts_branch_region"))
    graph = (project.project_path / "graph.yml").read_bytes()
    report = project.upsert_relationship(
        from_entity="account", to_entity="owner", columns=["owner_id"]
    ).report
    assert report["ok"] is True, report
    assert (report["route_decisions_added"], report["route_changes"]) == ([], [])
    assert report["changed_files"] == ["models/accounts.yml"]
    assert (project.project_path / "graph.yml").read_bytes() == graph


def test_a_removal_reports_the_answers_it_moves_and_records_nothing(tmp_path):
    """A removed route can't be recorded: the result lists each answer the removal moves."""
    project = _architect(
        tmp_path, relationships=("invoices_account", "accounts_owner", "owners_home_region")
    )
    report = project.remove_object(kind="model", key="owners").report
    assert report["ok"] is True, report
    assert report["route_decisions_added"] == []
    assert {
        (row["source_entity"], row["target_entity"]): row["head"] for row in report["route_changes"]
    } == {
        (ACCOUNT, REGION): {"refused": "PATH_NOT_FOUND"},
        (INVOICE, REGION): {"refused": "PATH_NOT_FOUND"},
        (REGION, ACCOUNT): {"refused": "PATH_NOT_FOUND"},
        (REGION, INVOICE): {"refused": "PATH_NOT_FOUND"},
    }
    assert all(row["keep_base"] is None for row in report["route_changes"])
    assert "path_preferences" not in _graph(project)["graph"]


def test_a_change_that_records_a_pair_itself_is_left_as_written(tmp_path):
    project = _architect(tmp_path)
    graph = _with_home_region(project)
    graph["graph"]["path_preferences"] = [_pin(INVOICE, REGION, INVOICE_HOME)]
    report = project.write_file(
        relative_path="graph.yml", content=yaml.safe_dump(graph, sort_keys=False)
    ).report
    assert report["ok"] is True, report
    added = _pairs([entry["row"] for entry in report["route_decisions_added"]])
    assert added == [(REGION, ACCOUNT), (INVOICE, OWNER), (REGION, INVOICE), (REGION, OWNER)]
    assert _graph(project)["graph"]["path_preferences"][0] == _pin(INVOICE, REGION, INVOICE_HOME)
    # The change decided the pair itself, so its new answer is listed, not undone.
    (moved,) = [row for row in report["route_changes"] if row["target_entity"] == REGION]
    assert (moved["source_entity"], moved["head"]) == (INVOICE, {"relationship_path": INVOICE_HOME})
    assert _answers(project.project_path)["amount"] == _gold(AMOUNT_BY_HOME)


def test_every_write_keeps_routes_and_a_row_that_does_not_take_effect_refuses(
    tmp_path, monkeypatch
):
    """The rule lives in the transaction every write passes through: file updates handed to it
    directly get the same rows, and a row that would not take effect refuses the change with
    nothing written."""
    project = _architect(tmp_path)
    update = ProjectFileUpdate(
        "graph.yml", yaml.safe_dump(_with_home_region(project), sort_keys=False).encode()
    )
    transaction = ProjectTransaction(project.project_path, workspace_root=tmp_path)
    revision = project.revision()
    with monkeypatch.context() as patch:
        patch.setattr(ProjectTransaction, "_route_rows_update", lambda self, updates, rows: update)
        with pytest.raises(SemanticLayerError) as exc_info:
            transaction.apply(
                [update], expected_revision=revision, idempotency_key="raw-1", intent={"raw": 1}
            )
    assert exc_info.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert _pairs(exc_info.value.details["route_changes"]) == MOVED
    assert project.revision() == revision
    outcome = transaction.apply(
        [update], expected_revision=revision, idempotency_key="raw-2", intent={"raw": 2}
    )
    assert _pairs([entry["row"] for entry in outcome.report["route_decisions_added"]]) == [
        (REGION, ACCOUNT),
        *(pair for pair in MOVED if pair != (REGION, ACCOUNT)),
    ]
    assert _answers(project.project_path) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}
