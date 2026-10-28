"""The route census, route changes impact reports, and Architect's decision guard.

Which route a question means is a business definition (``test_route_resolution.py``). An author
sees every entity pair that still needs one (``route_census``), a review sees every pair whose
answer a change moves (``impact_report``'s ``route_changes``), and an Architect edit that would
move an answer refuses until the author records a decision.

Fixture: the accounts, owners, regions, memberships and invoices package of
``test_route_resolution.py``, on DuckDB. Gold values come from plain SQL over its seed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

import semantic_rails.fanout as fanout_module
import semantic_rails.route_census as census_module
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import ProjectFileUpdate, ProjectTransaction
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import query_route_decisions, resolve_path
from semantic_rails.package_tools import impact_report, promote_package_report
from semantic_rails.route_census import census_pairs, route_census
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.test_role_paths import (
    CODE,
    DESTINATION,
    ORIGIN,
    ORPHAN_LEG,
    _seats_query,
)
from tests.semantic_rails.test_role_paths import (
    SEED_SQL as AIR_SEED_SQL,
)
from tests.semantic_rails.test_role_paths import (
    _write_package as _write_air_package,
)
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
    TWO_KEYS,
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


def _base_decisions(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Author the known baseline paths explicitly when testing a preservation retry."""
    return [
        _pin(change["source_entity"], change["target_entity"], change["base"]["relationship_path"])
        for change in changes
    ]


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------

# Every entity may start a query, including owners and memberships without measures.
DIAMOND_UNDECIDED = [
    (MEMBERSHIP, INVOICE),
    (OWNER, INVOICE),
    (REGION, ACCOUNT),
    (REGION, INVOICE),
    (REGION, MEMBERSHIP),
    (REGION, OWNER),
]
# Pairs with two or more routes that one lookup answers (the rung that chose it, its meaning).
DIAMOND_DEFAULTS = {
    (ACCOUNT, MEMBERSHIP): ("lookup", "the Membership of the Account's Owner"),
    (INVOICE, MEMBERSHIP): ("lookup", "the Membership of the Owner of the Invoice's Account"),
    (INVOICE, OWNER): ("lookup", "the Owner of the Invoice's Account"),
    (INVOICE, REGION): ("own_keys", "the Region of the Invoice's Account"),
    (MEMBERSHIP, OWNER): ("lookup", "the Owner of the Membership's Account"),
    (MEMBERSHIP, REGION): ("own_keys", "the Region of the Membership's Account"),
    (OWNER, ACCOUNT): ("lookup", "the Account of the Owner's Membership"),
}
# A question for each pair: a measure of the start grouped by a dimension of the target.
MEASURE = {ACCOUNT: BALANCE, INVOICE: AMOUNT, REGION: REGION_COUNT}
DIMENSION = {
    REGION: REGION_NAME,
    OWNER: OWNER_NAME,
    MEMBERSHIP: TIER,
    ACCOUNT: ACCOUNT_KIND,
    INVOICE: INVOICE_ID,
}


def _package_part(details: dict) -> dict:
    """A route refusal without what only a query has: each option's call and the hint."""
    clarification = details["clarification"]
    options = [
        {key: value for key, value in option.items() if key != "query"}
        for option in clarification["options"]
    ]
    rest = {key: value for key, value in details.items() if key != "hint"}
    return {**rest, "clarification": {**clarification, "options": options}}


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
            if start in MEASURE:
                runtime.query(_query(MEASURE[start], group_by=[DIMENSION[target]]))
            else:
                resolve_path(config, start=start, target=target)
        assert exc_info.value.code == "AMBIGUOUS_PATH"
        assert _package_part(exc_info.value.details) == _package_part(details)
        # A query's refusal also hands back each option as that query with its route.
        for option in exc_info.value.details["clarification"]["options"]:
            if start in MEASURE:
                decision = option["decision"]
                assert option["query"]["routes"] == [
                    {
                        "from": decision["source_entity"],
                        "to": decision["target_entity"],
                        "via": decision["relationship_path"],
                    }
                ]
    # Only multi-route own-key choices ask for confirmation.
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
        {
            "source_entity": MEMBERSHIP,
            "target_entity": ACCOUNT,
            "relationship_path": ["relationship.memberships_account"],
            "basis": "colocated_key",
        },
        {
            "source_entity": OWNER,
            "target_entity": MEMBERSHIP,
            "relationship_path": ["relationship.owners_primary_membership"],
            "basis": "colocated_key",
        },
        {
            "source_entity": OWNER,
            "target_entity": REGION,
            "relationship_path": ["relationship.owners_home_region"],
            "basis": "colocated_key",
        },
    ]
    # Pairs a lookup answers, each with what it reads and the routes it was chosen over.
    assert {
        pair: (row["by"], row["meaning"])
        for pair, row in zip(_pairs(census["defaults"]), census["defaults"], strict=True)
    } == DIAMOND_DEFAULTS
    for row in census["defaults"]:
        start, target = row["source_entity"], row["target_entity"]
        chosen, considered = resolve_path(config, start=start, target=target)
        assert [row["relationship_path"], *row["other_routes"]] == [chosen, *considered[1:]]
    # A pair with one route is in none of the lists.
    listed = {*undecided, *_pairs(census["assumed"]), *_pairs(census["defaults"])}
    assert {(ACCOUNT, INVOICE), (INVOICE, ACCOUNT)} <= set(census_pairs(config)) - listed


def test_census_pairs_start_and_end_at_every_reachable_entity(tmp_path):
    config = load_package_config(str(_write_package(tmp_path)))
    # An owner has no authored measure but may start a distinct-values or count query.
    with pytest.raises(SemanticLayerError) as exc_info:
        resolve_path(config, start=OWNER, target=INVOICE)
    assert exc_info.value.code == "AMBIGUOUS_PATH"
    assert {start for start, _ in census_pairs(config)} == {entity.id for entity in config.entities}
    # A child group can target an owner even when its conditions read another entity.
    no_owner_fields = replace(
        config, dimensions=[row for row in config.dimensions if row.entity != OWNER]
    )
    assert census_pairs(no_owner_fields) == census_pairs(config)
    assert _pairs(route_census(no_owner_fields)["undecided"]) == DIAMOND_UNDECIDED


def test_the_census_asks_the_resolver_once_per_pair_and_reuses_its_cache(tmp_path, monkeypatch):
    config = load_package_config(str(_write_package(tmp_path)))
    asked: list[tuple[str, str]] = []
    enumerated: list[tuple[str, str]] = []
    resolve, uncached = census_module.package_route, fanout_module._resolve_uncached

    def counted_resolve(config, *, start, target):
        asked.append((start, target))
        return resolve(config, start=start, target=target)

    def counted_uncached(config, start, target):
        enumerated.append((start, target))
        return uncached(config, start, target)

    monkeypatch.setattr(census_module, "package_route", counted_resolve)
    monkeypatch.setattr(fanout_module, "_resolve_uncached", counted_uncached)
    census = route_census(config)
    pairs = census_pairs(config)
    assert asked == pairs
    assert enumerated == pairs
    # A second census, and a query's own resolution (an answer or a refusal), read the cache.
    assert route_census(config) == census
    assert resolve_path(config, start=ACCOUNT, target=REGION)[0] == BRANCH
    assert resolve_path(config, start=INVOICE, target=REGION)[0] == INVOICE_BRANCH
    with pytest.raises(SemanticLayerError):
        resolve_path(config, start=REGION, target=INVOICE)
    assert enumerated == pairs


# Shipped packages whose relationships leave pairs undecided. Each needs a business decision
# recorded in the package (a follow-up), so they are pinned here to the reviewed snapshot.
SHIPPED_UNDECIDED = {
    "configs/semantic_rails/jaffle_shop": 16,
    "comparisons/semantic_layers/semantic_rails/package": 23,
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


SIGNUPS_BY_STORE_REGION = {
    "select": ["measure.shop.signup_count"],
    "group_by": ["dimension.shop_store_region"],
}


def _with_examples(pkg: Path, root: Path, **examples: dict[str, Any]) -> Path:
    """A copy of ``pkg`` under ``root`` whose examples are ``examples``' queries."""
    copy = root / pkg.name
    shutil.copytree(pkg, copy, ignore=shutil.ignore_patterns("*.duckdb"))
    (copy / "examples").mkdir(exist_ok=True)
    body = {name: {"query": query} for name, query in examples.items()}
    (copy / "examples" / "walks.yml").write_text(yaml.safe_dump({"examples": body}))
    return copy


@pytest.mark.parametrize("surface", ["census", "parse", "project_status"])
def test_resolved_crossings_are_object_warnings_only_where_a_query_walks_them(tmp_path, surface):
    pkg = ROOT / "tests/integration/correctness/shop"
    config = load_package_config(str(pkg))
    census = route_census(config)
    assert census["undecided"] == census["assumed"] == []
    # Customers and stores meet only through their orders.
    assert [(row["source_entity"], row["target_entity"]) for row in census["pass_through"]] == [
        ("entity.shop_customer", "entity.shop_customer_history"),
        ("entity.shop_customer", "entity.shop_store"),
        ("entity.shop_store", "entity.shop_customer"),
        ("entity.shop_store", "entity.shop_customer_history"),
    ]
    for crossing in census["pass_through"]:
        assert crossing["details"]["route_basis"] == "only_route"
        assert [row["entity"] for row in crossing["details"]["through"]] == ["entity.shop_order"]
        assert crossing["needed_by"] == []
    if surface == "census":
        return
    # No metric, example or test of the package walks a crossing (the answers ledger's
    # tests included): each is disclosed by the answer that reads it, never as a warning.
    report, _ = parse_config_report(PackageReference(source_path=str(pkg)))
    assert report["route_census"] == census
    assert not [row for row in report["warnings"] if row["code"] == "ROUTE_PASS_THROUGH"]
    # An example that reads the store of a customer's orders walks one.
    copy = _with_examples(pkg, tmp_path, signups_by_store_region=SIGNUPS_BY_STORE_REGION)
    if surface == "parse":
        report, _ = parse_config_report(PackageReference(source_path=str(copy)))
        walked = report["route_census"]
    else:
        server = create_architect_mcp_server(workspace_root=tmp_path)
        status = _mcp(server, "project_status", {"project_path": str(copy), "full_report": True})
        walked = status["route_census"]
        assert not any(action.startswith("Decide") for action in status["next_actions"])
        report = status["parse"]
    assert report["ok"]
    (crossing,) = [row for row in walked["pass_through"] if row["needed_by"]]
    assert (crossing["source_entity"], crossing["target_entity"]) == (
        "entity.shop_customer",
        "entity.shop_store",
    )
    assert crossing["needed_by"] == ["example.signups_by_store_region"]
    (warning,) = [row for row in report["warnings"] if row["code"] == "ROUTE_PASS_THROUGH"]
    assert warning["severity"] == "warning"
    assert warning["object_ids"] == [crossing["source_entity"], crossing["target_entity"]]
    assert warning["details"] == crossing["details"]
    assert warning["message"] == crossing["message"]


AMOUNT_BY_REGION = _query(AMOUNT, group_by=[REGION_NAME])
# Regions by account kind: any of a region's accounts, or of its owners' accounts.
REGIONS_BY_KIND = _query(REGION_COUNT, group_by=[ACCOUNT_KIND])


def test_only_the_pairs_a_query_of_the_package_walks_are_warned_about(tmp_path):
    """Six pairs have two routes, no row and no lookup default; none needs anything until a
    query walks it. An example that asks for regions by account kind walks one, and only that
    one is warned about, with the example that needs it and the rows that would decide it."""
    pkg = _write_package(tmp_path / "bank")
    report, _ = parse_config_report(PackageReference(source_path=str(pkg)))
    assert _pairs(report["route_census"]["undecided"]) == DIAMOND_UNDECIDED
    assert all(row["needed_by"] == [] for row in report["route_census"]["undecided"])
    assert "ROUTES_UNDECIDED" not in {row["code"] for row in report["warnings"]}

    copy = _with_examples(pkg, tmp_path / "walked", regions_by_kind=REGIONS_BY_KIND)
    report, _ = parse_config_report(PackageReference(source_path=str(copy)))
    undecided = report["route_census"]["undecided"]
    # The pair the example needs comes first; every other pair is still listed.
    assert sorted(_pairs(undecided)) == sorted(DIAMOND_UNDECIDED)
    assert (_pairs(undecided)[0], undecided[0]["needed_by"]) == (
        (REGION, ACCOUNT),
        ["example.regions_by_kind"],
    )
    assert all(row["needed_by"] == [] for row in undecided[1:])
    (warning,) = [row for row in report["warnings"] if row["code"] == "ROUTES_UNDECIDED"]
    rows = warning["details"].pop("first_pair_rows")
    assert warning["details"] == {
        "count": 1,
        "pairs": [
            {
                "source_entity": REGION,
                "target_entity": ACCOUNT,
                "needed_by": ["example.regions_by_kind"],
            }
        ],
        "unwalked": 5,
    }
    assert sorted(row["relationship_path"] for row in rows) == sorted(
        option["relationship_path"]
        for option in undecided[0]["details"]["clarification"]["options"]
    )
    assert "for example.regions_by_kind" in warning["message"]
    assert "The 5 other undecided pairs need nothing now" in warning["message"]


def test_a_query_past_a_fork_is_walked_to_its_end(tmp_path):
    """A query refused at one fork still needs the routes past it: the census takes the fork's
    first route for that query alone and keeps walking, so both forks are needed."""
    config = load_package_config(str(_write_package(tmp_path)))
    both = _query(
        REGION_COUNT,
        group_by=[ACCOUNT_KIND],
        where=[{"field": OWNER_NAME, "op": "is", "value": "Ann"}],
    )
    with pytest.raises(SemanticLayerError) as refused:
        Runtime.from_path(str(_write_package(tmp_path / "asked"))).query(both)
    assert refused.value.code == "AMBIGUOUS_PATH"
    walked = census_module.walked_pairs(config, [("example.both", [both], None)])
    assert walked[(REGION, ACCOUNT)] == walked[(REGION, OWNER)] == ["example.both"]
    # A metric that reads periods is walked with a time block; a test that expects a refusal
    # is not walked at all.
    queries = census_module.package_queries(config)
    assert {query_id for query_id, _calls, _now in queries} == {
        recipe.id for recipe in config.metric_recipes
    }
    assert [call.get("time") for call in queries[0][1]] == [
        None,
        *({"grain": grain} for grain in ("day", "month", "quarter", "year")),
    ]


def test_a_route_the_plan_reads_is_walked(tmp_path):
    """A group_by's own route is read from the plan, not only from what lowering records.
    Without the row that decides a customer's products, their one route goes through the
    customer's orders and their items: the examples and tests that count customers by product
    type need that crossing, and it is the one the parse report warns about for them."""
    package = copy_package_config(tmp_path, "jaffle_shop")
    graph = package / "graph.yml"
    document = yaml.safe_load(graph.read_text(encoding="utf-8"))
    rows = document["graph"]["path_preferences"]
    document["graph"]["path_preferences"] = [
        row
        for row in rows
        if (row["source_entity"], row["target_entity"]) != ("customer", "product")
    ]
    graph.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    report, _ = parse_config_report(PackageReference(source_path=str(package)))
    crossings = {
        (row["source_entity"], row["target_entity"]): row["needed_by"]
        for row in report["route_census"]["pass_through"]
    }
    assert crossings[("entity.jaffle_customer", "entity.jaffle_product")] == [
        "example.customers_who_bought_product_type",
        "example.repeat_customers_who_bought_product_type",
        "test.entity_hop_customers_by_product_type_executes",
        "test.entity_hop_customers_by_product_type_with_customer_filter_executes",
    ]
    warned = [
        tuple(row["object_ids"])
        for row in report["warnings"]
        if row["code"] == "ROUTE_PASS_THROUGH"
    ]
    assert ("entity.jaffle_customer", "entity.jaffle_product") in warned
    assert sorted(warned) == sorted(pair for pair, needed in crossings.items() if needed)


def test_a_second_parse_of_the_same_package_reuses_its_walks(tmp_path, monkeypatch):
    """An Architect write parses a package more than once: its queries are walked once per
    package source and queries, and again after either changes."""
    copy = _with_examples(_write_package(tmp_path / "bank"), tmp_path / "walked")
    walks: list[int] = []
    walk = census_module.walked_pairs

    def counted(config, queries):
        walks.append(len(queries))
        return walk(config, queries)

    monkeypatch.setattr(census_module, "walked_pairs", counted)
    monkeypatch.setattr(census_module, "_WALKS", type(census_module._WALKS)())
    ref = PackageReference(source_path=str(copy))
    first, _ = parse_config_report(ref)
    again, _ = parse_config_report(ref)
    assert len(walks) == 1 and again["route_census"] == first["route_census"]
    examples = copy / "examples" / "walks.yml"
    examples.write_text(yaml.safe_dump({"examples": {"regions": {"query": REGIONS_BY_KIND}}}))
    edited, _ = parse_config_report(ref)
    assert len(walks) == 2
    assert edited["route_census"]["undecided"][0]["needed_by"] == ["example.regions"]


def test_status_setup_and_promotion_point_at_the_pairs_a_query_needs(tmp_path):
    pkg = _write_package(tmp_path)
    server = create_architect_mcp_server(workspace_root=tmp_path)
    status = _mcp(server, "project_status", {"project_path": str(pkg), "full_report": True})
    assert _pairs(status["route_census"]["undecided"]) == DIAMOND_UNDECIDED
    assert "route_census" not in status["parse"]
    # No metric, example or test walks an undecided pair: nothing to decide now.
    assert not any(action.startswith("Decide") for action in status["next_actions"])
    summary = _mcp(server, "project_status", {"project_path": str(pkg)})["route_census"]
    assert summary["counts"]["undecided"] == len(DIAMOND_UNDECIDED)
    assert summary["counts"]["defaults"] == len(DIAMOND_DEFAULTS)
    assert summary["counts"]["undecided_needed"] == 0
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
    assert promotion["advisories"] == []

    copy = _with_examples(pkg, tmp_path / "walked", regions_by_kind=REGIONS_BY_KIND)
    server = create_architect_mcp_server(workspace_root=tmp_path / "walked")
    status = _mcp(server, "project_status", {"project_path": str(copy)})
    assert status["next_actions"][0].startswith(
        "Decide the 1 join route the package's own metrics, examples or tests need"
    )
    assert status["route_census"]["counts"]["undecided_needed"] == 1
    assert status["route_census"]["undecided"][0]["needed_by"] == ["example.regions_by_kind"]
    promotion = promote_package_report(PackageReference(source_path=str(copy)), environment="dev")
    (advisory,) = promotion["advisories"]
    assert (advisory["code"], advisory["details"]["count"]) == ("ROUTES_UNDECIDED", 1)
    assert "ROUTES_UNDECIDED" not in json.dumps(promotion["blockers"])


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------

# An invoice reaches a region through its account's branch: one route.
BASE = ("invoices_account", "accounts_branch_region", "accounts_owner")
# The owner's home region adds a second route wherever the owner lies between the two. An
# invoice keeps its account's region and owner: the one lookup along the account's own keys.
SECOND_ROUTE = (*BASE, "owners_home_region")
MOVED = [
    (OWNER, ACCOUNT),
    (OWNER, INVOICE),
    (OWNER, REGION),
    (REGION, ACCOUNT),
    (REGION, INVOICE),
    (REGION, OWNER),
]
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


def test_impact_lists_each_answer_a_second_route_moves_without_suggesting_rows(tmp_path):
    base = _write_package(tmp_path / "base", relationships=BASE)
    head = _write_package(tmp_path / "head", relationships=SECOND_ROUTE)
    report = _impact(base, head)
    assert _pairs(report["route_changes"]) == MOVED
    assert report["route_changes"][MOVED.index((REGION, ACCOUNT))] == {
        "source_entity": REGION,
        "target_entity": ACCOUNT,
        "base": {"relationship_path": BRANCH},
        "head": {"refused": "AMBIGUOUS_PATH"},
    }
    # The invoice's region keeps its answer, now chosen by default over the owner's region.
    amount = Runtime.from_path(str(head)).query(_query(AMOUNT, group_by=[REGION_NAME]))
    assert _rows(amount, [REGION_NAME, "v"]) == _gold(AMOUNT_BY_BRANCH)
    assert [(row["via"], row["basis"]) for row in amount["routes"]] == [(INVOICE_BRANCH, "default")]
    assert report["impact"]["risk"] == "high"
    diff_behavior = sum(1 for change in report["changes"] if change["behavior_change"])
    assert report["impact"]["changed_behavior_count"] == diff_behavior + len(MOVED)
    assert (
        "- Region to Account: was any of the Region's Accounts, now refused (AMBIGUOUS_PATH)"
    ) in report["markdown_summary"].splitlines()
    # The author can explicitly record the base paths to preserve the reference-SQL answers.
    assert all("keep_base" not in change for change in report["route_changes"])
    rows = _base_decisions(report["route_changes"])
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
    # With two keys from the account to the region, no lookup default answers the invoice's.
    base = _write_package(tmp_path / "base", relationships=TWO_KEYS)
    head = _write_package(
        tmp_path / "head", relationships=TWO_KEYS, pins=[_pin(INVOICE, REGION, INVOICE_HOME)]
    )
    changes = _impact(base, head)["route_changes"]
    assert _pairs(changes) == [(INVOICE, REGION), (REGION, INVOICE)]
    assert changes[0] == {
        "source_entity": INVOICE,
        "target_entity": REGION,
        "base": {"refused": "AMBIGUOUS_PATH"},
        "head": {"relationship_path": INVOICE_HOME},
    }


# ---------------------------------------------------------------------------
# Architect requires an explicit decision
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
        "as": "relationship.owners_home_region",
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
        pytest.param("owner", ["home_region_id"], [*INVOICE_ACCOUNT, OWNS, "relationship.owners_region"], id="second-route"),
        # The new key is on the start's own table: unrecorded, the pair would take it.
        pytest.param("invoice", ["issued_region_id"], ["relationship.invoices_region"], id="own-key"),
    ],
)  # fmt: skip
def test_a_relationship_that_moves_an_answer_requires_explicit_decisions(
    tmp_path, from_entity, columns, new_route
):
    project = _architect(tmp_path)
    revision, before = project.revision(), _files_and_receipts(project)
    with pytest.raises(SemanticLayerError, match="record_route_decision") as raised:
        project.upsert_relationship(from_entity=from_entity, to_entity="region", columns=columns)
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    changes = {
        (row["source_entity"], row["target_entity"]): row
        for row in raised.value.details["route_changes"]
    }
    if from_entity == "invoice":
        assert changes[(INVOICE, REGION)]["head"] == {"relationship_path": new_route}
    else:
        # The invoice keeps its account's region (a lookup default); the region's accounts don't.
        assert (INVOICE, REGION) not in changes
        assert changes[(REGION, ACCOUNT)]["head"] == {"refused": "AMBIGUOUS_PATH"}
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    assert _answers(project.project_path) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}
    for row in _base_decisions(raised.value.details["route_changes"]):
        assert project.record_route_decision(**row).report["ok"]
    report = project.upsert_relationship(
        from_entity=from_entity, to_entity="region", columns=columns
    ).report
    assert report["ok"] is True, report
    assert report["route_decisions_added"] == []
    assert report["route_changes"] == []
    assert _answers(project.project_path) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}
    assert _gold(AMOUNT_BY_BRANCH) != _gold(AMOUNT_BY_ISSUED)


@pytest.mark.parametrize(
    ("dry_run", "grouped", "role"),
    [
        (False, True, "destination"),
        (True, False, "destination"),
        (False, False, "origin"),
        (True, True, "origin"),
    ],
    ids=["keep-route-group-by-key", "keep-route-preview", "switch-route", "switch-route-preview"],
)
def test_adding_a_role_cannot_silently_change_an_unmatched_key(tmp_path, dry_run, grouped, role):
    pkg = _write_air_package(tmp_path, explicit=("destination",), extra_seed=ORPHAN_LEG)
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    query = _seats_query(
        **(
            {"group_by": [CODE]}
            if grouped
            # A plain-SQL reference, where a sum of no rows is NULL: the query scope.
            else {
                "where": [{"field": CODE, "op": "is", "value": "SFO"}],
                "observation_scope": "query",
            }
        )
    )

    def answer():
        runtime = Runtime.from_path(str(pkg))
        try:
            columns = [CODE, "seats"] if grouped else ["seats"]
            return {
                tuple(row[column] for column in columns) for row in runtime.query(query)["rows"]
            }
        finally:
            runtime.close()

    with duckdb.connect(":memory:") as connection:
        connection.execute(AIR_SEED_SQL + ORPHAN_LEG)
        base_sql = (
            "SELECT destination_code, SUM(seats) FROM legs GROUP BY destination_code"
            if grouped
            else "SELECT SUM(seats) FROM legs WHERE destination_code = 'SFO'"
        )
        base_gold = set(connection.execute(base_sql).fetchall())
        assert (("SFO", 7) if grouped else (7,)) in base_gold
        assert answer() == base_gold

        graph = _graph(project)
        graph["graph"]["relationships"]["legs_origin_airport"] = {
            "as": ORIGIN,
            "entities": ["leg", "airport"],
            "cardinality": "many_to_one",
            "via": ["origin_code"],
            "target": ["airport_code"],
        }
        revision, before = project.revision(), _files_and_receipts(project)
        with pytest.raises(SemanticLayerError, match="record_route_decision") as raised:
            project.write_files(
                [{"path": "graph.yml", "content": yaml.safe_dump(graph)}], dry_run=dry_run
            )
        assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
        pair = ("entity.air_leg", "entity.air_airport")
        assert pair in _pairs(raised.value.details["route_changes"])
        assert "rows" not in raised.value.details
        assert project.revision() == revision
        assert _files_and_receipts(project) == before
        assert answer() == base_gold

        # The author explicitly selects lookup semantics for the existing or new role.
        graph["graph"]["path_preferences"] = [
            _pin(*pair, [DESTINATION if role == "destination" else ORIGIN]),
            _pin(*reversed(pair), [DESTINATION if role == "destination" else ORIGIN]),
        ]
        report = project.write_files(
            [{"path": "graph.yml", "content": yaml.safe_dump(graph)}]
        ).report
        assert report["ok"] is True, report
        assert report["route_decisions_added"] == []
        select = "a.airport_code, SUM(l.seats)" if grouped else "SUM(l.seats)"
        tail = "GROUP BY a.airport_code" if grouped else "WHERE a.airport_code = 'SFO'"
        gold = set(
            connection.execute(
                f"SELECT {select} FROM legs l LEFT JOIN airports a "
                f"ON a.airport_code = l.{role}_code {tail}"
            ).fetchall()
        )
        assert answer() == gold


def test_a_change_that_moves_no_answer_records_nothing(tmp_path):
    project = _architect(tmp_path, relationships=("invoices_account", "accounts_branch_region"))
    graph = (project.project_path / "graph.yml").read_bytes()
    report = project.upsert_relationship(
        from_entity="account", to_entity="owner", columns=["owner_id"]
    ).report
    assert report["ok"] is True, report
    assert report["route_decisions_added"] == []
    assert len(report["route_changes"]) == 6
    assert all(row["base"] == {"refused": "PATH_NOT_FOUND"} for row in report["route_changes"])
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
    assert all("keep_base" not in row for row in report["route_changes"])
    assert "path_preferences" not in _graph(project)["graph"]


def _invoice_amounts(pkg: Path) -> list[tuple]:
    runtime = Runtime.from_path(str(pkg))
    try:
        return _rows(runtime.query(_query(AMOUNT, group_by=[REGION_NAME])), [REGION_NAME, "v"])
    finally:
        runtime.close()


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "preview"])
def test_removing_an_issued_region_key_refuses_a_switch_to_branch_region(tmp_path, dry_run):
    project = _architect(tmp_path, relationships=("invoices_account", "accounts_branch_region"))
    invoice = project.project_path / "models/invoices.yml"
    model = yaml.safe_load(invoice.read_text())
    model["model"]["entities"]["region"] = {"expr": "issued_region_id"}
    invoice.write_text(yaml.safe_dump(model))
    issued = _gold(AMOUNT_BY_ISSUED)
    branch = _gold(AMOUNT_BY_BRANCH)
    assert dict(issued) == {"North": 85, "South": 170, "East": 60}
    assert dict(branch) == {"North": 195, "South": 40, "East": 80}
    assert _invoice_amounts(project.project_path) == issued
    revision, before = project.revision(), _files_and_receipts(project)

    with pytest.raises(SemanticLayerError, match="nothing was written") as raised:
        project.remove_object(kind="relationship", key="region", model="invoices", dry_run=dry_run)
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert "keep_existing_routes" not in str(raised.value)
    changes = {
        (row["source_entity"], row["target_entity"]): row
        for row in raised.value.details["route_changes"]
    }
    assert changes[(INVOICE, REGION)]["head"] == {"relationship_path": INVOICE_BRANCH}
    assert "keep_base" not in changes[(INVOICE, REGION)]
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    assert _invoice_amounts(project.project_path) == issued

    # The author may deliberately select branch region, then remove the issued-region key.
    assert project.record_route_decision(**_pin(INVOICE, REGION, INVOICE_BRANCH)).report["ok"]
    assert _invoice_amounts(project.project_path) == branch
    report = project.remove_object(kind="relationship", key="region", model="invoices").report
    assert report["ok"] is True, report
    assert report["route_decisions_added"] == []
    assert _invoice_amounts(project.project_path) == branch


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "preview"])
def test_removing_an_issued_region_key_refuses_a_switch_to_the_lookup_default(tmp_path, dry_run):
    """Without the invoice's own key, its region is the one lookup along the account's own keys
    (the branch region, over the owner's home region): a default is an answer, so the removal
    that would switch to it is refused like any other silent switch."""
    project = _architect(tmp_path, relationships=SECOND_ROUTE)
    invoice = project.project_path / "models/invoices.yml"
    model = yaml.safe_load(invoice.read_text())
    model["model"]["entities"]["region"] = {"expr": "issued_region_id"}
    invoice.write_text(yaml.safe_dump(model))
    revision, before = project.revision(), _files_and_receipts(project)
    with pytest.raises(SemanticLayerError, match="nothing was written") as raised:
        project.remove_object(kind="relationship", key="region", model="invoices", dry_run=dry_run)
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    changes = {
        (row["source_entity"], row["target_entity"]): row
        for row in raised.value.details["route_changes"]
    }
    assert changes[(INVOICE, REGION)]["head"] == {"relationship_path": INVOICE_BRANCH}
    assert (project.revision(), _files_and_receipts(project)) == (revision, before)
    assert _invoice_amounts(project.project_path) == _gold(AMOUNT_BY_ISSUED)


def test_removing_an_issued_region_key_may_leave_the_pair_ambiguous(tmp_path):
    # Two keys from the account to the region: no one lookup is left once the key goes.
    project = _architect(tmp_path, relationships=(*SECOND_ROUTE, "accounts_billing_region"))
    invoice = project.project_path / "models/invoices.yml"
    model = yaml.safe_load(invoice.read_text())
    model["model"]["entities"]["region"] = {"expr": "issued_region_id"}
    invoice.write_text(yaml.safe_dump(model))
    assert _invoice_amounts(project.project_path) == _gold(AMOUNT_BY_ISSUED)

    report = project.remove_object(kind="relationship", key="region", model="invoices").report
    assert report["ok"] is True, report
    changes = {(row["source_entity"], row["target_entity"]): row for row in report["route_changes"]}
    assert changes[(INVOICE, REGION)]["head"] == {"refused": "AMBIGUOUS_PATH"}
    with pytest.raises(SemanticLayerError) as raised:
        _invoice_amounts(project.project_path)
    assert raised.value.code == "AMBIGUOUS_PATH"


def test_record_route_decision_is_deliberate_and_reports_inherited_pairs(tmp_path):
    project = _architect(tmp_path, relationships=SECOND_ROUTE)
    report = project.record_route_decision(**_pin(INVOICE, REGION, INVOICE_HOME)).report
    assert report["ok"] is True
    assert report["route_decisions_added"] == []
    assert _pairs(report["route_changes"]) == [
        (INVOICE, REGION),
        (OWNER, INVOICE),
        (REGION, INVOICE),
    ]
    # The row overrides the invoice's lookup default (its account's branch region) and settles
    # the two pairs whose routes walk through it.
    base = {_pairs([row])[0]: row["base"] for row in report["route_changes"]}
    assert base.pop((INVOICE, REGION)) == {"relationship_path": INVOICE_BRANCH}
    assert set(map(str, base.values())) == {str({"refused": "AMBIGUOUS_PATH"})}
    out = Runtime.from_path(str(project.project_path)).query(_query(AMOUNT, group_by=[REGION_NAME]))
    assert _rows(out, [REGION_NAME, "v"]) == _gold(AMOUNT_BY_HOME)


def test_raw_transactions_refuse_until_the_change_records_each_affected_pair(tmp_path):
    """Direct file updates pass through the same guard as Architect operations."""
    project = _architect(tmp_path)
    update = ProjectFileUpdate(
        "graph.yml", yaml.safe_dump(_with_home_region(project), sort_keys=False).encode()
    )
    transaction = ProjectTransaction(project.project_path, workspace_root=tmp_path)
    revision = project.revision()
    before = _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as exc_info:
        transaction.apply(
            [update], expected_revision=revision, idempotency_key="raw-1", intent={"raw": 1}
        )
    assert exc_info.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert _pairs(exc_info.value.details["route_changes"]) == MOVED
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    graph = _with_home_region(project)
    graph["graph"]["path_preferences"] = _base_decisions(exc_info.value.details["route_changes"])
    update = ProjectFileUpdate("graph.yml", yaml.safe_dump(graph).encode())
    outcome = transaction.apply(
        [update], expected_revision=revision, idempotency_key="raw-2", intent={"raw": 2}
    )
    assert outcome.report["route_decisions_added"] == []
    assert _answers(project.project_path) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}


def _small_package(tmp_path, entities, relationships, seed):
    """A small real package; relationships are (source, target, columns, cardinality, directions)."""
    pkg = tmp_path / "small"
    (pkg / "models").mkdir(parents=True)
    (pkg / "data").mkdir()
    (pkg / "data/seed.sql").write_text(seed)
    (pkg / "package.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "package": {
                    "id": "small",
                    "namespace": "small",
                    "warehouse": "duckdb",
                    "default_db": "data/test.duckdb",
                    "seed": {"kind": "sql_script", "source": "data/seed.sql"},
                },
                "defaults": {"dimension": {"groupable": True, "filterable": True}},
            }
        )
    )
    graph = {
        "entities": {key: {"key": ["id"], "model": key} for key in entities},
        "relationships": {
            name: {
                "as": f"relationship.{name}",
                "entities": [source, target],
                "via": [column],
                "target": ["id"],
                "cardinality": cardinality,
                "allowed_directions": directions,
            }
            for name, (source, target, column, cardinality, directions) in relationships.items()
        },
    }
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}))
    metrics = {}
    for key in entities:
        model = {
            "id": key,
            "relation": key,
            "entities": {key: {}},
            "dimensions": {"name": {"column": "name", "kind": "categorical"}},
        }
        if key in {"a", "account", "loan"}:
            model["measures"] = {
                f"{key}_amount": {
                    "kind": "aggregate",
                    "expr": "amount",
                    "accumulation": {"kind": "flow"},
                    "value_type": "count",
                }
            }
            metrics[f"{key}_amount"] = {
                "kind": "aggregate",
                "label": f"{key.title()} Amount",
                "measure": f"measure.small.{key}_amount",
                "aggregation": "sum",
                "value_type": "count",
            }
        (pkg / "models" / f"{key}.yml").write_text(yaml.safe_dump({"model": model}))
    (pkg / "metrics.yml").write_text(yaml.safe_dump({"metrics": metrics}))
    return pkg


@pytest.mark.parametrize("cardinality", ["one_to_many", "many_to_one"])
def test_a_second_route_to_a_dimensionless_child_requires_a_decision(tmp_path, cardinality):
    pkg = _small_package(
        tmp_path,
        ["a", "b", "c", "d"],
        {
            "ab": ("a", "b", "id", "one_to_many", ["forward"]),
            "bc": ("b", "c", "id", "one_to_many", ["forward"]),
            "cd": ("c", "d", "d_id", "many_to_one", ["forward"]),
        },
        """
        CREATE TABLE a(id INT, name VARCHAR, c_id INT, amount INT);
        INSERT INTO a VALUES (1, 'A1', 2, 100), (2, 'A2', 1, 200);
        CREATE TABLE b(id INT, name VARCHAR, a_id INT);
        INSERT INTO b VALUES (10, 'B1', 1), (20, 'B2', 2);
        CREATE TABLE c(id INT, b_id INT, a_id INT, d_id INT);
        INSERT INTO c VALUES (1, 10, 2, 1), (2, 20, 1, 2);
        CREATE TABLE d(id INT, name VARCHAR);
        INSERT INTO d VALUES (1, 'yes'), (2, 'no');
        """,
    )
    child_model = {"model": {"id": "c", "relation": "c", "entities": {"c": {"expr": "id"}}}}
    (pkg / "models/c.yml").write_text(yaml.safe_dump(child_model))
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    graph = _graph(project)
    del graph["graph"]["entities"]["c"]["key"]
    graph["graph"]["relationships"]["ab"]["target"] = ["a_id"]
    graph["graph"]["relationships"]["bc"]["target"] = ["b_id"]
    (pkg / "graph.yml").write_text(yaml.safe_dump(graph))
    base = load_package_config(str(pkg))
    assert not any(dimension.entity == "entity.small_c" for dimension in base.dimensions)
    query = _query(
        "measure.small.a_amount",
        where=[
            {
                "entity": "entity.small_c",
                "any": [{"field": "dimension.small_d_name", "op": "is", "value": "yes"}],
            }
        ],
    )

    def answer():
        return _rows(Runtime.from_path(str(pkg)).query(query), ["v"])

    gold = answer()
    assert gold == [(100,)]
    relationship = {
        "as": "relationship.ac",
        "entities": ["a", "c"],
        "cardinality": cardinality,
        "allowed_directions": ["forward"],
        "via": ["c_id"] if cardinality == "many_to_one" else ["id"],
        "target": ["id"] if cardinality == "many_to_one" else ["a_id"],
    }
    graph["graph"]["relationships"]["ac"] = relationship
    content = yaml.safe_dump(graph)
    transaction = ProjectTransaction(pkg, workspace_root=tmp_path)
    with transaction.virtual_project([ProjectFileUpdate("graph.yml", content.encode())]) as staged:
        head = load_package_config(str(staged))
    changes = census_module.route_changes(base, head)
    census = route_census(head)
    # Directory lint requires graph keys; the loader accepts the model's key.
    pair = ("entity.small_a", "entity.small_c")
    assert pair in _pairs(changes)
    assert pair in _pairs(
        census["undecided"] if cardinality == "one_to_many" else census["assumed"]
    )
    revision, before = project.revision(), _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as raised:
        project.write_files([{"path": "graph.yml", "content": content}], validate_after=False)
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert "rows" not in raised.value.details
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    assert answer() == gold
    graph["graph"]["path_preferences"] = _base_decisions(raised.value.details["route_changes"])
    report = project.write_files(
        [{"path": "graph.yml", "content": yaml.safe_dump(graph)}], validate_after=False
    ).report
    assert report["ok"] is True, report
    assert report["route_decisions_added"] == []
    assert report["route_changes"] == []
    assert pair not in _pairs(route_census(load_package_config(str(pkg)))["undecided"])
    assert answer() == gold


def test_queries_without_authored_measures_require_explicit_decisions(tmp_path):
    pkg = _small_package(
        tmp_path,
        ["a", "b", "c"],
        {
            "ab": ("a", "b", "b_id", "one_to_one", ["forward", "reverse"]),
            "ac": ("a", "c", "c_id", "one_to_one", ["forward", "reverse"]),
        },
        """
        CREATE TABLE a(id INT, name VARCHAR, b_id INT, c_id INT, amount INT);
        INSERT INTO a VALUES (1, 'A', 10, 1, 100);
        CREATE TABLE b(id INT, name VARCHAR, c_id INT);
        INSERT INTO b VALUES (10, 'B', 2);
        CREATE TABLE c(id INT, name VARCHAR);
        INSERT INTO c VALUES (1, 'North'), (2, 'South');
    """,
    )
    b_name, c_name = "dimension.small_b_name", "dimension.small_c_name"
    distinct = {"select": [], "group_by": [b_name, c_name]}
    count = {
        "select": [
            {
                "as": "v",
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": {
                        "kind": "comparison",
                        "op": ">",
                        "left": {"kind": "column", "entity": "entity.small_b", "column": "id"},
                        "right": {"kind": "literal", "value": 0},
                    },
                },
            }
        ],
        "group_by": [c_name],
    }

    def answers():
        runtime = Runtime.from_path(str(pkg))
        return (
            _rows(runtime.query(distinct), [b_name, c_name]),
            _rows(runtime.query(count), [c_name, "v"]),
        )

    assert answers() == ([("B", "North")], [("North", 1)])
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    revision, before = project.revision(), _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(from_entity="b", to_entity="c", columns=["c_id"])
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert ("entity.small_b", "entity.small_c") in _pairs(raised.value.details["route_changes"])
    assert "rows" not in raised.value.details
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    assert answers() == ([("B", "North")], [("North", 1)])
    for row in _base_decisions(raised.value.details["route_changes"]):
        project.record_route_decision(**row)
    report = project.upsert_relationship(from_entity="b", to_entity="c", columns=["c_id"]).report
    assert report["route_decisions_added"] == []
    assert report["route_changes"] == []
    assert answers() == ([("B", "North")], [("North", 1)])


def test_a_single_many_to_one_key_never_needs_confirmation(tmp_path):
    config = load_package_config(str(_write_package(tmp_path, relationships=("invoices_account",))))
    assert route_census(config) == {
        "undecided": [],
        "assumed": [],
        "defaults": [],
        "pass_through": [],
    }


def test_lowering_the_hop_ceiling_commits_without_undoing_the_cut(tmp_path):
    project = _architect(tmp_path)
    graph = _graph(project)
    graph["graph"]["path_policy"] = {"max_hops": 1}
    report = project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph)}]).report
    assert report["ok"] is True
    assert report["route_decisions_added"] == []
    assert len(report["route_changes"]) == 6
    assert all(
        row["head"] == {"refused": "PATH_NOT_FOUND"} and "keep_base" not in row
        for row in report["route_changes"]
    )
    assert _graph(project)["graph"]["path_policy"] == {"max_hops": 1}


def _files_and_receipts(project):
    # Lock creation is allowed; source files and idempotency receipts must not change.
    paths = [
        *project.project_path.rglob("*"),
        *(project.workspace_root / ".semantic-rails/architect-transactions").rglob("*"),
    ]
    return {
        str(path.relative_to(project.workspace_root)): path.read_bytes()
        for path in paths
        if path.is_file()
    }


def test_a_single_route_swap_requires_the_pairs_own_row(tmp_path):
    """With the pair's row the swap is written; without it, it is refused (the batch test below)."""
    project = ArchitectProject(
        _write_package(
            tmp_path,
            relationships=("accounts_branch_region",),
            extra={"accounts_branch_region": {"allowed_directions": ["forward"]}},
        ),
        workspace_root=tmp_path,
    )
    graph = _graph(project)
    relationship = graph["graph"]["relationships"].pop("accounts_branch_region")
    graph["graph"]["relationships"]["accounts_billing_region"] = {
        **relationship,
        "as": "relationship.accounts_billing_region",
        "via": ["billing_region_id"],
    }
    graph["graph"]["path_preferences"] = [
        _pin(ACCOUNT, REGION, ["relationship.accounts_billing_region"]),
    ]
    revision = project.revision()
    report = project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph)}]).report
    assert report["ok"] is True
    assert report["route_decisions_added"] == []
    assert _pairs(report["route_changes"]) == [(ACCOUNT, REGION)]
    assert project.revision() != revision


def test_a_removal_reports_a_refused_pair_becoming_the_only_route(tmp_path):
    # Two keys from the account to the region; the billing key goes, the branch key stays.
    project = _architect(
        tmp_path,
        relationships=("invoices_account", "accounts_branch_region", "accounts_billing_region"),
    )
    graph = _graph(project)
    graph["graph"]["relationships"].pop("accounts_billing_region")
    report = project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph)}]).report
    assert report["ok"] is True
    changes = {(row["source_entity"], row["target_entity"]): row for row in report["route_changes"]}
    assert changes[(INVOICE, REGION)]["base"] == {"refused": "AMBIGUOUS_PATH"}
    assert changes[(INVOICE, REGION)]["head"] == {"relationship_path": INVOICE_BRANCH}
    assert report["route_decisions_added"] == []


@pytest.mark.parametrize("failure", ["staging", "base-load", "staged-load"])
def test_unexpected_staging_failures_write_no_files_revision_or_receipts(
    tmp_path, monkeypatch, failure
):
    import semantic_rails.architect_transactions as transactions

    project = _architect(tmp_path)
    revision, before = project.revision(), _files_and_receipts(project)
    update = ProjectFileUpdate("graph.yml", yaml.safe_dump(_with_home_region(project)).encode())
    if failure == "staging":

        def fail(*args, **kwargs):
            raise PermissionError("staging unavailable")

        monkeypatch.setattr(ProjectTransaction, "virtual_project", fail)
        error = PermissionError
    else:
        load = transactions.load_package_snapshot
        calls = 0

        def fail(path):
            nonlocal calls
            calls += 1
            if calls == (1 if failure == "base-load" else 2):
                raise RuntimeError("unexpected loader failure")
            return load(path)

        monkeypatch.setattr(transactions, "load_package_snapshot", fail)
        error = RuntimeError
    with pytest.raises(error):
        ProjectTransaction(project.project_path, workspace_root=tmp_path).apply(
            [update],
            expected_revision=revision,
            idempotency_key="failure",
            intent={"failure": failure},
        )
    assert project.revision() == revision
    assert _files_and_receipts(project) == before


@pytest.mark.parametrize(
    ("load_number", "error"), [(1, yaml.YAMLError), (2, ValueError)], ids=["base", "staged"]
)
def test_invalid_loader_input_reaches_the_parse_gate_and_rolls_back(
    tmp_path, monkeypatch, load_number, error
):
    import semantic_rails.architect_transactions as transactions

    project = _architect(tmp_path)
    package = project.project_path / "package.yml"
    assert project.write_files(
        [{"path": "package.yml", "content": package.read_text() + "# previous write\n"}]
    ).report["ok"]
    revision, before = project.revision(), _files_and_receipts(project)
    load = transactions.load_package_snapshot
    calls = 0

    def fail(path):
        nonlocal calls
        calls += 1
        if calls == load_number:
            raise error("invalid loader input")
        return load(path)

    monkeypatch.setattr(transactions, "load_package_snapshot", fail)
    transaction = ProjectTransaction(project.project_path, workspace_root=tmp_path)
    report = transaction.apply(
        [ProjectFileUpdate("package.yml", b"schema_version: [\n")],
        expected_revision=revision,
        idempotency_key="invalid-input",
        intent={"invalid": True},
    ).report
    assert (report["ok"], report["status"], report["rolled_back"]) == (
        False,
        "rolled_back_after_parse_error",
        True,
    )
    assert report["parse"]["ok"] is False
    assert report["errors"]
    assert report["revision"] == project.revision() == revision
    after = _files_and_receipts(project)
    assert {path: after[path] for path in before} == before
    # The parse gate preserves existing receipts and records only this failed attempt.
    receipt = str(transaction._receipt_path("invalid-input").relative_to(tmp_path))
    assert after.keys() - before.keys() == {receipt}
    assert json.loads(after[receipt])["report"]["status"] == "rolled_back_after_parse_error"


@pytest.mark.parametrize(
    ("repair", "validate_after"),
    [(False, True), (True, True), (True, False)],
    ids=["preview", "repair-then-preview", "unvalidated-repair-then-preview"],
)
def test_an_architect_write_can_repair_invalid_yaml_and_preview_invalid_input(
    tmp_path, repair, validate_after
):
    project = _architect(tmp_path)
    package = project.project_path / "package.yml"
    valid = package.read_text()
    if repair:
        package.write_text("schema_version: [\n")
        report = project.write_files(
            [{"path": "package.yml", "content": valid}], validate_after=validate_after
        ).report
        assert (report["ok"], report["status"]) == (True, "written")
        assert package.read_text() == valid
        load_package_config(str(project.project_path))
    revision, before = project.revision(), _files_and_receipts(project)
    preview = project.write_files(
        [{"path": "package.yml", "content": "schema_version: [\n"}], dry_run=True
    ).report
    assert (preview["ok"], preview["status"]) == (False, "preview_invalid")
    assert preview["parse"]["ok"] is False
    assert project.revision() == revision
    assert _files_and_receipts(project) == before


def test_an_invalid_intermediate_write_cannot_erase_the_branch_region_baseline(tmp_path):
    project = _architect(tmp_path, relationships=("invoices_account", "accounts_branch_region"))
    branch = _gold(AMOUNT_BY_BRANCH)
    assert _invoice_amounts(project.project_path) == branch
    graph = _graph(project)
    source, target, via, key = _RELATIONSHIPS["invoices_issued_region"]
    relationships = graph["graph"]["relationships"]
    relationships["invoices_issued_region"] = {
        "as": "relationship.invoices_issued_region",
        "entities": [source, target],
        "cardinality": "many_to_one",
        "via": [via],
        "target": [key],
    }
    relationships["unknown_entity"] = {
        "entities": ["account", "unknown"],
        "cardinality": "many_to_one",
        "via": ["owner_id"],
        "target": ["owner_id"],
    }
    revision, before = project.revision(), _files_and_receipts(project)
    with pytest.raises(SemanticLayerError, match="nothing was written") as raised:
        project.write_files(
            [{"path": "graph.yml", "content": yaml.safe_dump(graph)}], validate_after=False
        )
    assert raised.value.code == "INVALID_CONFIG"
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    assert _invoice_amounts(project.project_path) == branch

    # Retrying only the repaired input still compares against the valid branch-region base.
    del relationships["unknown_entity"]
    with pytest.raises(SemanticLayerError) as raised:
        project.write_files(
            [{"path": "graph.yml", "content": yaml.safe_dump(graph)}], validate_after=False
        )
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    assert _invoice_amounts(project.project_path) == branch
    graph["graph"]["path_preferences"] = _base_decisions(raised.value.details["route_changes"])
    report = project.write_files(
        [{"path": "graph.yml", "content": yaml.safe_dump(graph)}], validate_after=False
    ).report
    assert report["ok"] is True, report
    assert report["route_decisions_added"] == []
    assert report["route_changes"] == []
    assert _invoice_amounts(project.project_path) == branch
    assert branch != _gold(AMOUNT_BY_ISSUED)


DISTRICT = "entity.small_district"
SMALL_ACCOUNT, LOAN = "entity.small_account", "entity.small_loan"
SMALL_BRANCH = ["relationship.account_branch", "relationship.branch_district"]
SMALL_HOME = ["relationship.account_owner", "relationship.owner_district"]


def _loan_package(tmp_path):
    return _small_package(
        tmp_path,
        ["account", "branch", "district", "owner", "loan"],
        {
            "account_branch": ("account", "branch", "branch_id", "many_to_one", ["forward"]),
            "branch_district": ("branch", "district", "district_id", "many_to_one", ["forward"]),
            "account_owner": ("account", "owner", "owner_id", "many_to_one", ["forward"]),
            "loan_account": ("loan", "account", "account_id", "many_to_one", ["forward"]),
        },
        """
        CREATE TABLE account(id INT, name VARCHAR, branch_id INT, owner_id INT, amount INT);
        INSERT INTO account VALUES (1, 'A', 10, 20, 100);
        CREATE TABLE branch(id INT, name VARCHAR, district_id INT);
        INSERT INTO branch VALUES (10, 'Branch', 1);
        CREATE TABLE district(id INT, name VARCHAR);
        INSERT INTO district VALUES (1, 'North'), (2, 'South');
        CREATE TABLE owner(id INT, name VARCHAR, district_id INT);
        INSERT INTO owner VALUES (20, 'Owner', 2);
        CREATE TABLE loan(id INT, name VARCHAR, account_id INT, amount INT);
        INSERT INTO loan VALUES (30, 'Loan', 1, 250);
    """,
    )


def _loan_answers(project):
    runtime = Runtime.from_path(str(project.project_path))
    return {
        key: _rows(
            runtime.query(
                _query(f"measure.small.{key}_amount", group_by=["dimension.small_district_name"])
            ),
            ["dimension.small_district_name", "v"],
        )
        for key in ("account", "loan")
    }


@pytest.mark.parametrize("layout", ["graph", "inline"])
def test_an_explicit_shorter_row_keeps_both_answers_and_follows_the_loader(tmp_path, layout):
    pkg = _loan_package(tmp_path)
    package = yaml.safe_load((pkg / "package.yml").read_text())
    graph = yaml.safe_load((pkg / "graph.yml").read_text())
    if layout == "inline":
        (pkg / "package.yml").write_text(yaml.safe_dump({**package, **graph}))
        (pkg / "graph.yml").unlink()
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    gold = {"account": [("North", 100)], "loan": [("North", 250)]}
    assert _loan_answers(project) == gold
    project.record_route_decision(
        **_pin(SMALL_ACCOUNT, DISTRICT, SMALL_BRANCH), validate_after=layout != "inline"
    )
    relative = "package.yml" if layout == "inline" else "graph.yml"
    document = yaml.safe_load((pkg / relative).read_text())
    document["graph"]["relationships"]["owner_district"] = {
        "as": "relationship.owner_district",
        "entities": ["owner", "district"],
        "via": ["district_id"],
        "target": ["id"],
        "cardinality": "many_to_one",
        "allowed_directions": ["forward"],
    }
    report = project.write_files(
        [{"path": relative, "content": "# authored comment\n" + yaml.safe_dump(document)}],
        # The loader accepts inline graphs; directory lint still requires graph.yml.
        validate_after=layout != "inline",
    ).report
    assert report["ok"] is True
    assert report["route_decisions_added"] == []
    assert _pairs(report["route_changes"]) == [("entity.small_owner", DISTRICT)]
    assert report["route_changes"][0]["base"] == {"refused": "PATH_NOT_FOUND"}
    destination = "graph.yml" if layout == "graph" else "package.yml"
    holder = yaml.safe_load((pkg / destination).read_text())["graph"]
    assert holder["path_preferences"] == [_pin(SMALL_ACCOUNT, DISTRICT, SMALL_BRANCH)]
    assert _loan_answers(project) == gold
    changed = project.record_route_decision(
        **_pin(SMALL_ACCOUNT, DISTRICT, SMALL_HOME), validate_after=layout != "inline"
    ).report
    assert changed["route_decisions_added"] == []
    assert changed["changed_files"] == [destination]
    assert _pairs(changed["route_changes"]) == [(SMALL_ACCOUNT, DISTRICT), (LOAN, DISTRICT)]
    assert _loan_answers(project) == {"account": [("South", 100)], "loan": [("South", 250)]}


def test_an_edited_row_that_moves_an_inherited_pair_requires_its_own_decision(tmp_path):
    project = ArchitectProject(_loan_package(tmp_path), workspace_root=tmp_path)
    project.record_route_decision(**_pin(SMALL_ACCOUNT, DISTRICT, SMALL_BRANCH))
    graph = _graph(project)
    graph["graph"]["relationships"]["owner_district"] = {
        "as": "relationship.owner_district",
        "entities": ["owner", "district"],
        "via": ["district_id"],
        "target": ["id"],
        "cardinality": "many_to_one",
        "allowed_directions": ["forward"],
    }
    assert project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph)}]).report[
        "ok"
    ]
    revision, before = project.revision(), _files_and_receipts(project)
    graph = _graph(project)
    graph["graph"]["path_preferences"] = [_pin(SMALL_ACCOUNT, DISTRICT, SMALL_HOME)]
    with pytest.raises(SemanticLayerError) as raised:
        project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph)}])
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert _pairs(raised.value.details["route_changes"]) == [(LOAN, DISTRICT)]
    assert "rows" not in raised.value.details
    assert "keep_base" not in raised.value.details["route_changes"][0]
    assert "graph.path_preferences" in str(raised.value)
    for field in ("source_entity", "target_entity", "relationship_path"):
        assert field in str(raised.value)
    assert project.revision() == revision
    assert _files_and_receipts(project) == before


@pytest.mark.parametrize("override_account", [False, True], ids=["loan-only", "account-and-loan"])
def test_package_census_impact_and_guard_ignore_query_route_overrides(tmp_path, override_account):
    base = _loan_package(tmp_path / "base")
    head = _loan_package(tmp_path / "head")
    for pkg, route in ((base, SMALL_BRANCH), (head, SMALL_HOME)):
        graph = yaml.safe_load((pkg / "graph.yml").read_text())
        graph["graph"]["relationships"]["owner_district"] = {
            "as": "relationship.owner_district",
            "entities": ["owner", "district"],
            "via": ["district_id"],
            "target": ["id"],
            "cardinality": "many_to_one",
            "allowed_directions": ["forward"],
        }
        graph["graph"]["path_preferences"] = [_pin(SMALL_ACCOUNT, DISTRICT, route)]
        (pkg / "graph.yml").write_text(yaml.safe_dump(graph))
    config = load_package_config(str(base))
    ambiguous = replace(config, path_preferences=[])
    expected_census = route_census(ambiguous)
    assert (LOAN, DISTRICT) in _pairs(expected_census["undecided"])
    expected_decided_census = route_census(config)
    expected_impact = _impact(base, head)
    assert _pairs(expected_impact["route_changes"]) == [(SMALL_ACCOUNT, DISTRICT), (LOAN, DISTRICT)]
    transaction = ProjectTransaction(base, workspace_root=tmp_path)
    updates = (ProjectFileUpdate("graph.yml", (head / "graph.yml").read_bytes()),)
    expected_report = transaction._guard_routes(updates, guard=False, validate_after=True)
    with pytest.raises(SemanticLayerError) as baseline:
        transaction._guard_routes(updates, guard=True, validate_after=True)
    assert baseline.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert _pairs(baseline.value.details["route_changes"]) == [(LOAN, DISTRICT)]

    overrides = {(LOAN, DISTRICT): ["relationship.loan_account", *SMALL_BRANCH]}
    if override_account:
        overrides[(SMALL_ACCOUNT, DISTRICT)] = SMALL_BRANCH
    with query_route_decisions(overrides):
        # The query override is active, including for an otherwise ambiguous pair.
        assert (
            resolve_path(ambiguous, start=LOAN, target=DISTRICT)[0] == overrides[(LOAN, DISTRICT)]
        )
        assert route_census(ambiguous) == expected_census
        assert route_census(config) == expected_decided_census
        assert _impact(base, head) == expected_impact
        assert (
            transaction._guard_routes(updates, guard=False, validate_after=True) == expected_report
        )
        with pytest.raises(SemanticLayerError) as active:
            transaction._guard_routes(updates, guard=True, validate_after=True)
        assert active.value.code == baseline.value.code
        assert active.value.details == baseline.value.details


def test_deleting_a_pairs_row_never_silently_switches_its_answer(tmp_path):
    project = ArchitectProject(
        _write_package(
            tmp_path, relationships=SECOND_ROUTE, pins=[_pin(OWNER, REGION, [OWNS, *BRANCH])]
        ),
        workspace_root=tmp_path,
    )
    graph = _graph(project)
    del graph["graph"]["path_preferences"]
    revision, before = project.revision(), _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as raised:
        project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph)}])
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert "rows" not in raised.value.details
    assert (OWNER, REGION) in _pairs(raised.value.details["route_changes"])
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
    config = load_package_config(str(project.project_path))
    assert resolve_path(config, start=OWNER, target=REGION)[0] == [OWNS, *BRANCH]


@pytest.mark.parametrize("dry_run", [False, True])
def test_batch_route_swap_refuses_every_file_without_recorded_decision(tmp_path, dry_run):
    project = ArchitectProject(
        _write_package(
            tmp_path,
            relationships=("accounts_branch_region",),
            extra={"accounts_branch_region": {"allowed_directions": ["forward"]}},
        ),
        workspace_root=tmp_path,
    )
    graph = _graph(project)
    relationship = graph["graph"]["relationships"].pop("accounts_branch_region")
    graph["graph"]["relationships"]["accounts_billing_region"] = {
        **relationship,
        "as": "relationship.accounts_billing_region",
        "via": ["billing_region_id"],
    }
    revision, before = project.revision(), _files_and_receipts(project)

    with pytest.raises(SemanticLayerError, match="nothing was written") as raised:
        project.write_files(
            [
                {"path": "graph.yml", "content": yaml.safe_dump(graph)},
                {"path": "notes.md", "content": "new route\n"},
            ],
            dry_run=dry_run,
        )

    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert _pairs(raised.value.details["route_changes"]) == [(ACCOUNT, REGION)]
    assert project.revision() == revision
    assert _files_and_receipts(project) == before
