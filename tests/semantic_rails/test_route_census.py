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

# Every entity may start a query, including owners and memberships without measures.
DIAMOND_UNDECIDED = [
    (ACCOUNT, MEMBERSHIP),
    (INVOICE, MEMBERSHIP),
    (INVOICE, OWNER),
    (INVOICE, REGION),
    (MEMBERSHIP, INVOICE),
    (MEMBERSHIP, OWNER),
    (MEMBERSHIP, REGION),
    (OWNER, ACCOUNT),
    (OWNER, INVOICE),
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
            if start in MEASURE:
                runtime.query(_query(MEASURE[start], group_by=[DIMENSION[target]]))
            else:
                resolve_path(config, start=start, target=target)
        assert exc_info.value.code == "AMBIGUOUS_PATH"
        assert exc_info.value.details == details
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
    # A pair with one route is in neither list.
    listed = {*undecided, *_pairs(census["assumed"])}
    assert {(ACCOUNT, INVOICE), (INVOICE, ACCOUNT)} <= set(census_pairs(config)) - listed


def test_census_pairs_start_at_every_entity_and_end_at_a_dimension(tmp_path):
    config = load_package_config(str(_write_package(tmp_path)))
    # An owner has no authored measure but may start a distinct-values or count query.
    with pytest.raises(SemanticLayerError) as exc_info:
        resolve_path(config, start=OWNER, target=INVOICE)
    assert exc_info.value.code == "AMBIGUOUS_PATH"
    assert {start for start, _ in census_pairs(config)} == {entity.id for entity in config.entities}
    # Without a dimension the owner is no target either.
    no_owner_fields = replace(
        config, dimensions=[row for row in config.dimensions if row.entity != OWNER]
    )
    assert _pairs(route_census(no_owner_fields)["undecided"]) == [
        pair for pair in DIAMOND_UNDECIDED if pair[1] != OWNER
    ]


def test_the_census_asks_the_resolver_once_per_multi_route_pair_and_reuses_its_cache(
    tmp_path, monkeypatch
):
    config = load_package_config(str(_write_package(tmp_path)))
    asked: list[tuple[str, str]] = []
    enumerated: list[tuple[str, str]] = []
    resolve, uncached = census_module.resolve_route, fanout_module._resolve_uncached

    def counted_resolve(config, *, start, target):
        asked.append((start, target))
        return resolve(config, start=start, target=target)

    def counted_uncached(config, start, target):
        enumerated.append((start, target))
        return uncached(config, start, target)

    monkeypatch.setattr(census_module, "resolve_route", counted_resolve)
    monkeypatch.setattr(fanout_module, "_resolve_uncached", counted_uncached)
    census = route_census(config)
    graph = census_module.get_package_analysis(config).graph
    pairs = [
        pair
        for pair in census_pairs(config)
        if fanout_module._has_multiple_routes(graph, *pair, fanout_module.package_hop_limit(config))
    ]
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
    "configs/semantic_rails/jaffle_shop": 18,
    "comparisons/semantic_layers/semantic_rails/package": 25,
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
    assert "route_census" not in status["parse"]
    assert status["next_actions"][0].startswith("Decide the 13 undecided join routes")
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
    assert (advisory["code"], advisory["details"]["count"]) == ("ROUTES_UNDECIDED", 13)
    assert "ROUTES_UNDECIDED" not in json.dumps(promotion["blockers"])


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------

# An invoice reaches a region through its account's branch: one route.
BASE = ("invoices_account", "accounts_branch_region", "accounts_owner")
# The owner's home region adds a second route wherever the owner lies between the two.
SECOND_ROUTE = (*BASE, "owners_home_region")
MOVED = [
    (INVOICE, OWNER),
    (INVOICE, REGION),
    (OWNER, ACCOUNT),
    (OWNER, INVOICE),
    (OWNER, REGION),
    (REGION, ACCOUNT),
    (REGION, INVOICE),
    (REGION, OWNER),
]
KEPT = [(OWNER, ACCOUNT), (REGION, ACCOUNT), (OWNER, REGION)]
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
        "- Invoice to Region: was the Region of the Invoice's Account, now refused (AMBIGUOUS_PATH); "
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
    changes = _impact(base, head)["route_changes"]
    assert _pairs(changes) == [(INVOICE, REGION), (REGION, INVOICE)]
    assert changes[0] == {
        "source_entity": INVOICE,
        "target_entity": REGION,
        "base": {"refused": "AMBIGUOUS_PATH"},
        "head": {"relationship_path": INVOICE_HOME},
        "keep_base": None,
    }


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
    if from_entity == "invoice":
        assert added[(INVOICE, REGION)] == {
            "row": _pin(INVOICE, REGION, INVOICE_BRANCH),
            "new_routes": [new_route],
        }
    else:
        assert added[(OWNER, REGION)] == {
            "row": _pin(OWNER, REGION, [OWNS, *BRANCH]),
            "new_routes": [["relationship.owners_region"]],
        }
        assert (INVOICE, REGION) not in added  # inherits the shorter rows

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
    assert _pairs(rows) == KEPT
    assert [len(row["relationship_path"]) for row in rows] == [1, 1, 2]
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
    assert all(row["keep_base"] is None for row in report["route_changes"])
    assert "path_preferences" not in _graph(project)["graph"]


def test_record_route_decision_is_deliberate_and_reports_inherited_pairs(tmp_path):
    project = _architect(tmp_path, relationships=SECOND_ROUTE)
    report = project.record_route_decision(**_pin(INVOICE, REGION, INVOICE_HOME)).report
    assert report["ok"] is True
    assert report["route_decisions_added"] == []
    assert _pairs(report["route_changes"]) == [
        (INVOICE, OWNER),
        (INVOICE, REGION),
        (OWNER, INVOICE),
        (REGION, INVOICE),
    ]
    assert all(row["base"] == {"refused": "AMBIGUOUS_PATH"} for row in report["route_changes"])
    out = Runtime.from_path(str(project.project_path)).query(_query(AMOUNT, group_by=[REGION_NAME]))
    assert _rows(out, [REGION_NAME, "v"]) == _gold(AMOUNT_BY_HOME)


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
    assert _pairs([entry["row"] for entry in outcome.report["route_decisions_added"]]) == KEPT
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
                "id": f"relationship.{name}",
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
        (pkg / "models" / f"{key}.yml").write_text(yaml.safe_dump({"model": model}))
    return pkg


def test_queries_without_authored_measures_keep_their_gold(tmp_path):
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
    distinct = {"version": 1, "select": [], "group_by": [b_name, c_name]}
    count = {
        "version": 1,
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
    report = project.upsert_relationship(from_entity="b", to_entity="c", columns=["c_id"]).report
    assert ("entity.small_b", "entity.small_c") in _pairs(
        [entry["row"] for entry in report["route_decisions_added"]]
    )
    assert report["route_changes"] == []
    assert answers() == ([("B", "North")], [("North", 1)])


def test_a_single_many_to_one_key_never_needs_confirmation(tmp_path):
    config = load_package_config(str(_write_package(tmp_path, relationships=("invoices_account",))))
    assert route_census(config) == {"undecided": [], "assumed": []}


def test_lowering_the_hop_ceiling_commits_without_undoing_the_cut(tmp_path):
    project = _architect(tmp_path)
    graph = _graph(project)
    graph["graph"]["path_policy"] = {"max_hops": 1}
    report = project.write_file(relative_path="graph.yml", content=yaml.safe_dump(graph)).report
    assert report["ok"] is True
    assert report["route_decisions_added"] == []
    assert len(report["route_changes"]) == 6
    assert all(
        row["head"] == {"refused": "PATH_NOT_FOUND"} and row["keep_base"] is None
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


@pytest.mark.parametrize("decide", [False, True])
def test_a_single_route_swap_requires_the_pairs_own_row(tmp_path, decide):
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
        "id": "relationship.accounts_billing_region",
        "via": ["billing_region_id"],
    }
    if decide:
        graph["graph"]["path_preferences"] = [
            _pin(ACCOUNT, REGION, ["relationship.accounts_billing_region"]),
        ]
    revision, before = project.revision(), _files_and_receipts(project)
    content = yaml.safe_dump(graph)
    if decide:
        report = project.write_file(relative_path="graph.yml", content=content).report
        assert report["ok"] is True
        assert report["route_decisions_added"] == []
        assert _pairs(report["route_changes"]) == [(ACCOUNT, REGION)]
        assert project.revision() != revision
    else:
        with pytest.raises(SemanticLayerError, match="nothing was written") as raised:
            project.write_file(relative_path="graph.yml", content=content)
        assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
        assert _pairs(raised.value.details["route_changes"]) == [(ACCOUNT, REGION)]
        assert project.revision() == revision
        assert _files_and_receipts(project) == before


def test_a_removal_reports_a_refused_pair_becoming_the_only_route(tmp_path):
    project = _architect(tmp_path, relationships=SECOND_ROUTE)
    report = project.remove_object(kind="model", key="owners").report
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


@pytest.mark.parametrize("layout", ["top-level", "graph", "inline"])
def test_one_shorter_row_keeps_both_answers_and_both_writers_follow_the_loader(tmp_path, layout):
    pkg = _loan_package(tmp_path)
    package = yaml.safe_load((pkg / "package.yml").read_text())
    graph = yaml.safe_load((pkg / "graph.yml").read_text())
    if layout == "top-level":
        package["path_preferences"] = []
        (pkg / "package.yml").write_text(yaml.safe_dump(package))
    elif layout == "inline":
        (pkg / "package.yml").write_text(yaml.safe_dump({**package, **graph}))
        (pkg / "graph.yml").unlink()
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    gold = {"account": [("North", 100)], "loan": [("North", 250)]}
    assert _loan_answers(project) == gold
    relative = "package.yml" if layout == "inline" else "graph.yml"
    document = yaml.safe_load((pkg / relative).read_text())
    document["graph"]["relationships"]["owner_district"] = {
        "id": "relationship.owner_district",
        "entities": ["owner", "district"],
        "via": ["district_id"],
        "target": ["id"],
        "cardinality": "many_to_one",
        "allowed_directions": ["forward"],
    }
    report = project.write_file(
        relative_path=relative,
        content="# authored comment\n" + yaml.safe_dump(document),
        # The loader accepts inline graphs; directory lint still requires graph.yml.
        validate_after=layout != "inline",
    ).report
    assert report["ok"] is True
    assert [entry["row"] for entry in report["route_decisions_added"]] == [
        _pin(SMALL_ACCOUNT, DISTRICT, SMALL_BRANCH)
    ]
    assert _pairs(report["route_changes"]) == [("entity.small_owner", DISTRICT)]
    assert report["route_changes"][0]["base"] == {"refused": "PATH_NOT_FOUND"}
    destination = "graph.yml" if layout == "graph" else "package.yml"
    holder = yaml.safe_load((pkg / destination).read_text())
    if layout != "top-level":
        holder = holder["graph"]
    assert holder["path_preferences"] == [_pin(SMALL_ACCOUNT, DISTRICT, SMALL_BRANCH)]
    assert _loan_answers(project) == gold
    if layout != "top-level":
        assert "# authored comment" not in (pkg / destination).read_text()
    changed = project.record_route_decision(
        **_pin(SMALL_ACCOUNT, DISTRICT, SMALL_HOME), validate_after=layout != "inline"
    ).report
    assert changed["route_decisions_added"] == []
    assert changed["changed_files"] == [destination]
    assert _pairs(changed["route_changes"]) == [(SMALL_ACCOUNT, DISTRICT), (LOAN, DISTRICT)]
    assert _loan_answers(project) == {"account": [("South", 100)], "loan": [("South", 250)]}


def test_an_edited_row_that_moves_an_inherited_pair_refuses_a_disagreeing_keep_row(tmp_path):
    project = ArchitectProject(_loan_package(tmp_path), workspace_root=tmp_path)
    project.upsert_relationship(from_entity="owner", to_entity="district", columns=["district_id"])
    revision, before = project.revision(), _files_and_receipts(project)
    graph = _graph(project)
    graph["graph"]["path_preferences"] = [_pin(SMALL_ACCOUNT, DISTRICT, SMALL_HOME)]
    with pytest.raises(SemanticLayerError) as raised:
        project.write_file(relative_path="graph.yml", content=yaml.safe_dump(graph))
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert raised.value.details["conflicts_with"] == [_pin(SMALL_ACCOUNT, DISTRICT, SMALL_HOME)]
    assert raised.value.details["row"] == _pin(
        LOAN, DISTRICT, ["relationship.loan_account", *SMALL_BRANCH]
    )
    assert project.revision() == revision
    assert _files_and_receipts(project) == before


def test_a_keep_row_reload_failure_refuses_before_writing(tmp_path, monkeypatch):
    import semantic_rails.architect_transactions as transactions

    project = _architect(tmp_path)
    revision, before = project.revision(), _files_and_receipts(project)
    load = transactions.load_package_snapshot
    calls = 0

    def fail_final(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SemanticLayerError("INVALID_CONFIG", "added rows fail to load")
        return load(path)

    monkeypatch.setattr(transactions, "load_package_snapshot", fail_final)
    with pytest.raises(SemanticLayerError) as raised:
        project.write_file(
            relative_path="graph.yml", content=yaml.safe_dump(_with_home_region(project))
        )
    assert raised.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert "added rows fail to load" in str(raised.value)
    assert project.revision() == revision
    assert _files_and_receipts(project) == before


def test_deleting_a_pairs_row_never_silently_switches_its_answer(tmp_path):
    project = ArchitectProject(
        _write_package(
            tmp_path, relationships=SECOND_ROUTE, pins=[_pin(OWNER, REGION, [OWNS, *BRANCH])]
        ),
        workspace_root=tmp_path,
    )
    graph = _graph(project)
    del graph["graph"]["path_preferences"]
    report = project.write_file(relative_path="graph.yml", content=yaml.safe_dump(graph)).report
    assert report["ok"] is True
    assert report["route_changes"] == []
    assert _pin(OWNER, REGION, [OWNS, *BRANCH]) in [
        entry["row"] for entry in report["route_decisions_added"]
    ]
    config = load_package_config(str(project.project_path))
    assert resolve_path(config, start=OWNER, target=REGION)[0] == [OWNS, *BRANCH]
