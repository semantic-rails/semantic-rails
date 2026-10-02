"""An ambiguous route is asked in business words, and every option it offers is usable two ways.

Which of two routes a question means is a business definition. The engine refuses with
``AMBIGUOUS_PATH`` and ``details.clarification``: the question ("Which District does the
question mean for an Account?") and, per route, its meaning from package labels, an id, and the
``graph.path_preferences`` row that decides it (``decision``). An option is applied either per
query (``route_decisions``: for the person who asked, not a default) or as a package change
(the same row in the package, which Architect ``record_route_decision`` writes).

Fixture, on DuckDB, where every pair of routes disagrees on the data:

    account -> branch -> district     the district of the account's branch
    account -> owner -> district      the account owner's home district
    account <- membership             memberships held on the account (one-to-many)
    account -> owner -> membership    the owner's primary membership
    district <- branch <- account     a child filter: accounts at the district's branches
    district <- owner <- account      a child filter: accounts of the district's residents
    flight -> airport                 by the origin or the destination airport column
"""

from __future__ import annotations

import asyncio
import textwrap
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

import semantic_rails.compiler as compiler_module
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.indexes import RouteRefusal, get_package_analysis
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import query_route_decisions
from semantic_rails.interop.package_writer import package_documents, write_package
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import build_options_payload
from semantic_rails.metadata_parts.path_coverage import _path_availability
from semantic_rails.policies import row_filters_for_context
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails import test_route_resolution as resolution

SEED_SQL = """
CREATE TABLE districts (district_id INTEGER, district_name VARCHAR, budget INTEGER);
INSERT INTO districts VALUES (1, 'North', 100), (2, 'South', 200), (3, 'East', 400);
CREATE TABLE branches (branch_id INTEGER, district_id INTEGER);
INSERT INTO branches VALUES (10, 1), (11, 2);
CREATE TABLE owners (
  owner_id INTEGER, owner_name VARCHAR, home_district_id INTEGER, primary_membership_id INTEGER
);
INSERT INTO owners VALUES (20, 'Ann', 2, 300), (21, 'Bob', 3, 301), (22, 'Cy', 1, 302);
CREATE TABLE accounts (
  account_id INTEGER, branch_id INTEGER, owner_id INTEGER, kind VARCHAR, balance INTEGER
);
INSERT INTO accounts VALUES
  (1, 10, 20, 'checking', 100), (2, 10, 21, 'savings', 200),
  (3, 11, 22, 'savings', 400), (4, 11, 20, 'checking', 800);
CREATE TABLE memberships (membership_id INTEGER, account_id INTEGER, tier VARCHAR);
INSERT INTO memberships VALUES
  (300, 1, 'gold'), (301, 2, 'basic'), (302, 3, 'gold'), (303, 1, 'basic'), (304, 4, 'basic');
CREATE TABLE airports (airport_id INTEGER, city VARCHAR);
INSERT INTO airports VALUES (1, 'Oslo'), (2, 'Rome'), (3, 'Lima');
CREATE TABLE flights (
  flight_id INTEGER, origin_airport_id INTEGER, destination_airport_id INTEGER, seats INTEGER
);
INSERT INTO flights VALUES (1, 1, 2, 100), (2, 2, 3, 150), (3, 1, 3, 80);
"""

ACCOUNT, DISTRICT, MEMBERSHIP = (
    "entity.bank_account",
    "entity.bank_district",
    "entity.bank_membership",
)
OWNER, FLIGHT, AIRPORT = "entity.bank_owner", "entity.bank_flight", "entity.bank_airport"
BRANCH_ROUTE = ["relationship.accounts_branch", "relationship.branches_district"]
OWNER_ROUTE = ["relationship.accounts_owner", "relationship.owners_home_district"]

_RELATIONSHIPS = {
    "accounts_branch": ("account", "branch", "branch_id", "branch_id"),
    "branches_district": ("branch", "district", "district_id", "district_id"),
    "accounts_owner": ("account", "owner", "owner_id", "owner_id"),
    "owners_home_district": ("owner", "district", "home_district_id", "district_id"),
    "memberships_account": ("membership", "account", "account_id", "account_id"),
    "owners_primary_membership": ("owner", "membership", "primary_membership_id", "membership_id"),
    "flights_origin": ("flight", "airport", "origin_airport_id", "airport_id"),
    "flights_destination": ("flight", "airport", "destination_airport_id", "airport_id"),
}
_MODELS: dict[str, dict[str, Any]] = {
    "district": {
        "dimensions": {"name": {"column": "district_name", "kind": "categorical"}},
        "measures": {
            "budget": {
                "kind": "aggregate",
                "label": "Budget",
                "expr": "budget",
                "accumulation": {"kind": "flow"},
                "value_type": "count",
            }
        },
    },
    "branch": {},
    "owner": {"dimensions": {"name": {"column": "owner_name", "kind": "categorical"}}},
    "account": {
        "dimensions": {"kind": {"kind": "categorical"}},
        "measures": {
            "balance": {
                "kind": "aggregate",
                "label": "Balance",
                "expr": "balance",
                "accumulation": {"kind": "flow"},
                "value_type": "count",
            },
            "account_count": {
                "kind": "entity_count",
                "label": "Accounts",
                "entity_key": "account_id",
                "accumulation": {"kind": "population"},
                "value_type": "count",
            },
        },
    },
    "membership": {"dimensions": {"tier": {"kind": "categorical"}}},
    "airport": {"dimensions": {"city": {"kind": "categorical"}}},
    "flight": {
        "measures": {
            "seats": {
                "kind": "aggregate",
                "label": "Seats",
                "expr": "seats",
                "accumulation": {"kind": "flow"},
                "value_type": "count",
            }
        }
    },
}


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _write_package(root: Path, *, decisions: list[dict[str, Any]] | None = None) -> Path:
    pkg = root / "bank"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    (pkg / "package.yml").write_text(
        textwrap.dedent(
            f"""
            schema_version: 1
            package:
              id: bank
              namespace: bank
              name: bank
              description: Routes that disagree.
              warehouse: duckdb
              default_db: {(root / "bank.duckdb").as_posix()}
              seed:
                kind: sql_script
                source: data/seed.sql
            defaults:
              dimension:
                groupable: true
                filterable: true
              measure:
                subject_entity: self
                aggregation_entity: self
              relationship:
                traversal: [forward, reverse]
            """
        )
    )
    relationships: dict[str, Any] = {}
    for name, (source, target, via, key) in _RELATIONSHIPS.items():
        relationships[name] = {
            "id": f"relationship.{name}",
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [via],
            "target": [key],
        }
    # Counting accounts by a held membership's tier needs the author's say-so.
    relationships["memberships_account"]["rollup_safe"] = {"reverse": ["count_distinct"]}
    # An airport never leads to the flights departing from it.
    relationships["flights_origin"]["allowed_directions"] = ["forward"]
    graph: dict[str, Any] = {
        "entities": {
            key: {"label": key.title(), "key": [f"{key}_id"], "model": f"{key}s"} for key in _MODELS
        },
        "relationships": relationships,
        # Two hops: the routes below and no longer ones.
        "path_policy": {"max_hops": 2},
    }
    if decisions:
        graph["path_preferences"] = decisions
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}, sort_keys=False))
    for key, body in _MODELS.items():
        spec = {"id": f"{key}s", "relation": f"{key}es" if key == "branch" else f"{key}s"}
        spec.update({"entities": {key: {}}, **body})
        (pkg / "models" / f"{key}s.yml").write_text(yaml.safe_dump({"model": spec}))
    return pkg


def _gold(sql: str) -> list[tuple]:
    con = duckdb.connect(":memory:")
    con.execute(SEED_SQL)
    return sorted(tuple(row) for row in con.execute(sql).fetchall())


def _rows(out: dict[str, Any], columns: list[str]) -> list[tuple]:
    return sorted(tuple(row[column] for column in columns) for row in out["rows"])


def _query(measure: str, **parts: Any) -> dict[str, Any]:
    return {"version": 1, "select": [{"expression": {"measure": measure}, "as": "v"}], **parts}


def _refusal(pkg: Path, query: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(pkg)).query(query)
    assert exc_info.value.code == "AMBIGUOUS_PATH", exc_info.value
    return exc_info.value


def _chosen_by_query(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [w["details"] for w in out["warnings"] if w["code"] == "ROUTE_CHOSEN_BY_QUERY"]


BALANCE_BY_DISTRICT = _query("measure.bank.balance", group_by=["dimension.bank_district_name"])
ACCOUNTS_BY_TIER = _query("measure.bank.account_count", group_by=["dimension.bank_membership_tier"])
SEATS_BY_CITY = _query("measure.bank.seats", group_by=["dimension.bank_airport_city"])
BUDGET_WITH_SAVINGS = _query(
    "measure.bank.budget", where=[{"field": "dimension.bank_account_kind", "value": "savings"}]
)
BY_BRANCH = (
    "SELECT d.district_name, SUM(a.balance) FROM accounts a JOIN branches b USING (branch_id) "
    "JOIN districts d USING (district_id) GROUP BY 1"
)
BY_OWNER = (
    "SELECT d.district_name, SUM(a.balance) FROM accounts a JOIN owners o USING (owner_id) "
    "JOIN districts d ON d.district_id = o.home_district_id GROUP BY 1"
)
# Each shape: the query, its result columns, the pair it asks about, and per option id the
# option's meaning, its route and the answer it gives (independent SQL).
SHAPES: dict[str, tuple[dict[str, Any], list[str], tuple[str, str], dict[str, Any]]] = {
    "diamond": (
        BALANCE_BY_DISTRICT,
        ["dimension.bank_district_name", "v"],
        (ACCOUNT, DISTRICT),
        {
            "branch_district": ("the District of the Account's Branch", BRANCH_ROUTE, BY_BRANCH),
            "owner_district": ("the District of the Account's Owner", OWNER_ROUTE, BY_OWNER),
        },
    ),
    "shorter_fan_out": (
        ACCOUNTS_BY_TIER,
        ["dimension.bank_membership_tier", "v"],
        (ACCOUNT, MEMBERSHIP),
        {
            "account_membership": (
                "any of the Account's Memberships",
                ["relationship.memberships_account"],
                "SELECT m.tier, COUNT(DISTINCT a.account_id) FROM accounts a "
                "JOIN memberships m USING (account_id) GROUP BY 1",
            ),
            "owner_membership": (
                "the Membership of the Account's Owner",
                ["relationship.accounts_owner", "relationship.owners_primary_membership"],
                "SELECT m.tier, COUNT(DISTINCT a.account_id) FROM accounts a "
                "JOIN owners o USING (owner_id) "
                "JOIN memberships m ON m.membership_id = o.primary_membership_id GROUP BY 1",
            ),
        },
    ),
    "two_keys_to_one_entity": (
        SEATS_BY_CITY,
        ["dimension.bank_airport_city", "v"],
        (FLIGHT, AIRPORT),
        {
            "destination_airport": (
                "the Flight's Airport (destination_airport_id)",
                ["relationship.flights_destination"],
                "SELECT p.city, SUM(f.seats) FROM flights f "
                "JOIN airports p ON p.airport_id = f.destination_airport_id GROUP BY 1",
            ),
            "origin_airport": (
                "the Flight's Airport (origin_airport_id)",
                ["relationship.flights_origin"],
                "SELECT p.city, SUM(f.seats) FROM flights f "
                "JOIN airports p ON p.airport_id = f.origin_airport_id GROUP BY 1",
            ),
        },
    ),
    "child_filter_diamond": (
        BUDGET_WITH_SAVINGS,
        ["v"],
        (DISTRICT, ACCOUNT),
        {
            "branch_account": (
                "any of the Accounts of any of the District's Branches",
                ["relationship.branches_district", "relationship.accounts_branch"],
                "SELECT SUM(d.budget) FROM districts d WHERE EXISTS (SELECT 1 FROM branches b "
                "JOIN accounts a USING (branch_id) WHERE b.district_id = d.district_id "
                "AND a.kind = 'savings')",
            ),
            "owner_account": (
                "any of the Accounts of any of the District's Owners",
                ["relationship.owners_home_district", "relationship.accounts_owner"],
                "SELECT SUM(d.budget) FROM districts d WHERE EXISTS (SELECT 1 FROM owners o "
                "JOIN accounts a USING (owner_id) WHERE o.home_district_id = d.district_id "
                "AND a.kind = 'savings')",
            ),
        },
    ),
}


def test_the_diamond_asks_in_business_words(tmp_path):
    err = _refusal(_write_package(tmp_path), BALANCE_BY_DISTRICT)
    assert err.details["reason"] == "route_decision_required"
    assert (err.details["start"], err.details["target"]) == (ACCOUNT, DISTRICT)
    assert {"candidates", "pins", "meanings"}.isdisjoint(err.details)
    clarification = err.details["clarification"]
    assert clarification == {
        "kind": "route",
        "apply": ["query", "package"],
        "question": "Which District does the question mean for an Account?",
        "options": [
            {
                "id": "branch_district",
                "meaning": "the District of the Account's Branch",
                "relationship_path": BRANCH_ROUTE,
                "decision": {
                    "source_entity": ACCOUNT,
                    "target_entity": DISTRICT,
                    "relationship_path": BRANCH_ROUTE,
                    "label": "the District of the Account's Branch",
                },
            },
            {
                "id": "owner_district",
                "meaning": "the District of the Account's Owner",
                "relationship_path": OWNER_ROUTE,
                "decision": {
                    "source_entity": ACCOUNT,
                    "target_entity": DISTRICT,
                    "relationship_path": OWNER_ROUTE,
                    "label": "the District of the Account's Owner",
                },
            },
        ],
    }
    # Every entity on each route, in route order: the account, its branch or owner, the district.
    for option, waypoint in zip(clarification["options"], ("Branch", "Owner"), strict=True):
        meaning = option["meaning"]
        assert meaning.index("Account") < meaning.index(waypoint) and "District" in meaning
    assert all(meaning in str(err) for meaning in (o["meaning"] for o in clarification["options"]))


@pytest.mark.parametrize("shape", SHAPES)
def test_every_option_answers_with_its_route_per_query_or_in_the_package(tmp_path, shape):
    """The invariant: whatever option is chosen, its decision answers with that route, sent
    with the query or recorded in the package."""
    query, columns, (start, target), options = SHAPES[shape]
    err = _refusal(_write_package(tmp_path / "refused"), query)
    clarification = err.details["clarification"]
    assert (err.details["start"], err.details["target"]) == (start, target)
    assert [option["id"] for option in clarification["options"]] == sorted(options)
    runtime = Runtime.from_path(str(_write_package(tmp_path / "per_query")))
    golds = set()
    for option in clarification["options"]:
        meaning, route, gold = options[option["id"]]
        assert (option["meaning"], option["relationship_path"]) == (meaning, route)
        assert option["decision"]["label"] == meaning
        golds.add(tuple(_gold(gold)))
        per_query = runtime.query({**query, "route_decisions": [option["decision"]]})
        assert _rows(per_query, columns) == _gold(gold)
        (chosen,) = _chosen_by_query(per_query)
        assert chosen["row"] == {key: option["decision"][key] for key in chosen["row"]}
        assert chosen["replaced"] == "undecided"
        recorded = _write_package(tmp_path / option["id"], decisions=[option["decision"]])
        in_package = Runtime.from_path(str(recorded)).query(query)
        assert _rows(in_package, columns) == _gold(gold)
        assert _chosen_by_query(in_package) == []
    assert len(golds) == len(options)  # the routes disagree


def test_a_query_row_overrides_the_package_default_for_that_query_only(tmp_path):
    branch = SHAPES["diamond"][3]["branch_district"]
    pkg = _write_package(
        tmp_path,
        decisions=[
            {"source_entity": "account", "target_entity": "district", "relationship_path": [
                "accounts_branch", "branches_district"
            ], "label": branch[0]}
        ],
    )  # fmt: skip
    runtime = Runtime.from_path(str(pkg))
    columns = ["dimension.bank_district_name", "v"]
    owner_row = {
        "source_entity": ACCOUNT,
        "target_entity": DISTRICT,
        "relationship_path": OWNER_ROUTE,
    }
    out = runtime.query({**BALANCE_BY_DISTRICT, "route_decisions": [owner_row]})
    assert _rows(out, columns) == _gold(BY_OWNER)
    assert _chosen_by_query(out) == [{"row": owner_row, "replaced": "decided"}]
    out = runtime.query(BALANCE_BY_DISTRICT)
    assert _rows(out, columns) == _gold(BY_BRANCH)
    assert _chosen_by_query(out) == []
    # The query row answers its own compile; the cache never serves it to another query.
    out = runtime.query({**BALANCE_BY_DISTRICT, "route_decisions": [owner_row]})
    assert _rows(out, columns) == _gold(BY_OWNER)
    package_profile = runtime.compile(BALANCE_BY_DISTRICT)["hop_profile"]["targets"][DISTRICT]
    assert (package_profile["path"], package_profile["route_label"]) == (BRANCH_ROUTE, branch[0])
    query_profile = runtime.compile({**BALANCE_BY_DISTRICT, "route_decisions": [owner_row]})
    assert query_profile["hop_profile"]["targets"][DISTRICT]["path"] == OWNER_ROUTE
    assert "route_label" not in query_profile["hop_profile"]["targets"][DISTRICT]


def test_the_package_path_cache_never_holds_a_query_row(tmp_path):
    config = load_package_config(str(_write_package(tmp_path)))
    owner_row = {
        "source_entity": ACCOUNT,
        "target_entity": DISTRICT,
        "relationship_path": OWNER_ROUTE,
    }
    with_row = compile_query(
        config, Registry(config), {**BALANCE_BY_DISTRICT, "route_decisions": [owner_row]}
    )
    assert with_row["route_decisions"] == [{**owner_row, "replaced": "undecided"}]
    cached = get_package_analysis(config).path_cache[(ACCOUNT, DISTRICT)]
    assert isinstance(cached, RouteRefusal) and cached.code == "AMBIGUOUS_PATH"
    with pytest.raises(SemanticLayerError) as exc_info:
        compile_query(config, Registry(config), BALANCE_BY_DISTRICT)
    assert exc_info.value.code == "AMBIGUOUS_PATH"


DIAMOND_ROW = {
    "source_entity": ACCOUNT,
    "target_entity": DISTRICT,
    "relationship_path": OWNER_ROUTE,
}


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        pytest.param({"source_entity": ACCOUNT}, None, id="not-a-list"),
        pytest.param(
            [{**DIAMOND_ROW, "relationship_path": []}], "malformed_route_decision", id="empty-path"
        ),
        pytest.param([{**DIAMOND_ROW, "weight": 1}], "malformed_route_decision", id="unknown-key"),
        pytest.param(
            [{**DIAMOND_ROW, "relationship_path": ["relationship.accounts_branch"]}],
            "invalid_route_decision",
            id="off-route",
        ),
        pytest.param(
            [{**DIAMOND_ROW, "relationship_path": ["relationship.nope"]}],
            "invalid_route_decision",
            id="unknown-relationship",
        ),
        pytest.param(
            [{**DIAMOND_ROW, "source_entity": "entity.bank_nope"}],
            "invalid_route_decision",
            id="unknown-entity",
        ),
        pytest.param(
            [DIAMOND_ROW, {**DIAMOND_ROW, "relationship_path": BRANCH_ROUTE}],
            "duplicate_route_decision",
            id="two-rows-one-pair",
        ),
        pytest.param(
            [
                DIAMOND_ROW,
                {
                    "source_entity": FLIGHT,
                    "target_entity": AIRPORT,
                    "relationship_path": ["relationship.flights_origin"],
                },
            ],
            "route_decision_unused",
            id="pair-never-walked",
        ),  # fmt: skip
    ],
)
def test_a_bad_route_decision_is_invalid_query(tmp_path, rows, reason):
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(_write_package(tmp_path))).query(
            {**BALANCE_BY_DISTRICT, "route_decisions": rows}
        )
    assert exc_info.value.code == "INVALID_QUERY", exc_info.value
    if reason is not None:
        assert exc_info.value.details["reason"] == reason
        assert exc_info.value.details["path"].startswith("route_decisions[")


@pytest.mark.parametrize("decided", [BRANCH_ROUTE, OWNER_ROUTE])
def test_a_route_decision_is_refused_under_a_row_policy_on_any_route(tmp_path, decided):
    """A row filter on the owner refuses a query row for the pair even when the row takes
    the branch route: the route choice must never step around the filter."""
    config = load_package_config(str(_write_package(tmp_path)))
    policy = {"dimension": "dimension.bank_owner_name", "attribute": "owner", "type": "string"}
    config = replace(
        config,
        semantic_policies=[SemanticPolicyConfig(id="policy.bank.owner", kind="row_filter", config=policy)],
    )  # fmt: skip
    context = RequestContext(attributes={"owner": "Ann"}).to_policy_context()
    row = {**DIAMOND_ROW, "relationship_path": decided}
    with pytest.raises(SemanticLayerError) as exc_info:
        compile_query(
            config,
            Registry(config),
            {**BALANCE_BY_DISTRICT, "route_decisions": [row]},
            row_filters=row_filters_for_context(config, context),
        )
    assert exc_info.value.code == "POLICY_DENIED"
    assert exc_info.value.details["reason"] == "route_override_under_row_policy"
    assert exc_info.value.details["entities"] == [OWNER]
    assert exc_info.value.details["policy_ids"] == ["policy.bank.owner"]


def test_the_child_filter_guard_asks_instead_of_refusing_as_mixed_grain(tmp_path, monkeypatch):
    """The bypass: a resolver that hands a child filter several routes (as the start's own
    key would) still gets a route question, never an answer by one of them."""
    config = load_package_config(str(_write_package(tmp_path)))
    resolve = compiler_module.resolve_path
    branch = SHAPES["child_filter_diamond"][3]["branch_account"][1]
    owner = SHAPES["child_filter_diamond"][3]["owner_account"][1]

    def several_routes(config, *, start, target):
        if (start, target) == (DISTRICT, ACCOUNT):
            return list(branch), [list(branch), list(owner)]
        return resolve(config, start=start, target=target)

    monkeypatch.setattr(compiler_module, "resolve_path", several_routes)
    with pytest.raises(SemanticLayerError) as exc_info:
        compile_query(config, Registry(config), BUDGET_WITH_SAVINGS)
    assert exc_info.value.code == "AMBIGUOUS_PATH"
    options = exc_info.value.details["clarification"]["options"]
    assert [option["relationship_path"] for option in options] == [branch, owner]


# --- record_route_decision ---------------------------------------------------------------


def _project(tmp_path: Path) -> ArchitectProject:
    return ArchitectProject(_write_package(tmp_path), workspace_root=tmp_path)


def _graph_rows(project: ArchitectProject) -> list[dict[str, Any]]:
    graph = yaml.safe_load((project.project_path / "graph.yml").read_text())["graph"]
    return list(graph.get("path_preferences", []))


def test_record_route_decision_previews_the_row_then_makes_the_query_answer(tmp_path):
    project = _project(tmp_path)
    before = (project.project_path / "graph.yml").read_bytes()
    err = _refusal(project.project_path, BALANCE_BY_DISTRICT)
    owner = err.details["clarification"]["options"][1]
    decision = owner["decision"]

    preview = project.record_route_decision(**decision, dry_run=True).report
    assert preview["status"] == "preview" and preview["ok"] is True, preview
    assert (project.project_path / "graph.yml").read_bytes() == before
    (change,) = preview["changes"]
    lines = change["diff"].splitlines()[2:]  # past the ---/+++ header
    assert not [line for line in lines if line.startswith("-")]
    assert yaml.safe_load(change["proposed_content"]) == {
        "graph": {**yaml.safe_load(before)["graph"], "path_preferences": [decision]}
    }
    assert preview["replaced"] is None
    assert preview["summary"] == (
        "For an Account, 'District' now means the District of the Account's Owner. "
        "Other meanings: the District of the Account's Branch."
    )

    committed = project.record_route_decision(**decision).report
    assert (committed["status"], committed["changed_files"]) == ("recorded", ["graph.yml"])
    assert _graph_rows(project) == [decision]
    out = Runtime.from_path(str(project.project_path)).query(BALANCE_BY_DISTRICT)
    assert _rows(out, ["dimension.bank_district_name", "v"]) == _gold(BY_OWNER)


def test_record_route_decision_replaces_the_pair_row_and_reports_it(tmp_path):
    project = _project(tmp_path)
    first = {"source_entity": "account", "target_entity": "district",
             "relationship_path": ["accounts_branch", "branches_district"]}  # fmt: skip
    project.record_route_decision(**first)
    replaced = project.record_route_decision(**DIAMOND_ROW, label="home district").report
    assert replaced["replaced"] == first
    assert _graph_rows(project) == [{**DIAMOND_ROW, "label": "home district"}]
    assert replaced["summary"].startswith("For an Account, 'District' now means home district.")
    config = load_package_config(str(project.project_path))
    assert config.path_preferences[0].label == "home district"


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({**DIAMOND_ROW, "relationship_path": ["relationship.accounts_branch"]}, id="off-route"),
        pytest.param({**DIAMOND_ROW, "relationship_path": ["relationship.nope"]}, id="unknown-relationship"),
        pytest.param({**DIAMOND_ROW, "target_entity": "entity.bank_nope"}, id="unknown-entity"),
        pytest.param(
            {**DIAMOND_ROW, "relationship_path": ["relationship.branches_district"]}, id="broken-chain"
        ),
        pytest.param(
            {"source_entity": AIRPORT, "target_entity": FLIGHT,
             "relationship_path": ["relationship.flights_origin"]},
            id="disallowed-direction",
        ),
    ],
)  # fmt: skip
def test_record_route_decision_refuses_a_path_that_is_not_a_route_and_writes_nothing(tmp_path, row):
    project = _project(tmp_path)
    revision = project.revision()
    with pytest.raises(SemanticLayerError) as exc_info:
        project.record_route_decision(**row)
    assert exc_info.value.code == "INVALID_CONFIG"
    assert exc_info.value.details["reason"] == "invalid_route_decision"
    assert project.revision() == revision
    assert _graph_rows(project) == []


def test_record_route_decision_refuses_a_stale_revision(tmp_path):
    project = _project(tmp_path)
    stale = project.revision()
    project.record_route_decision(**DIAMOND_ROW, expected_revision=stale, idempotency_key="one")
    with pytest.raises(SemanticLayerError) as exc_info:
        project.record_route_decision(
            **{**DIAMOND_ROW, "relationship_path": BRANCH_ROUTE},
            expected_revision=stale,
            idempotency_key="two",
        )
    assert exc_info.value.code == "CONFIG_CONFLICT"
    assert exc_info.value.details["conflict_kind"] == "stale_revision"
    assert _graph_rows(project) == [DIAMOND_ROW]


def test_a_row_label_round_trips_through_the_package_writer(tmp_path):
    label = "the District of the Account's Owner"
    config = load_package_config(
        str(_write_package(tmp_path / "source", decisions=[{**DIAMOND_ROW, "label": label}]))
    )
    assert config.path_preferences[0].label == label
    documents = package_documents(config, namespace="bank")
    assert documents["graph.yml"]["graph"]["path_preferences"][0]["label"] == label
    written = write_package(config, tmp_path / "written", namespace="bank")
    reloaded = load_package_config(str(written))
    assert reloaded.path_preferences == config.path_preferences
    assert package_documents(reloaded, namespace="bank") == documents


# --- MCP ---------------------------------------------------------------------------------


def test_the_query_mcp_keeps_the_clarification_and_discloses_the_query_row(tmp_path):
    pkg = _write_package(tmp_path)
    expected = _refusal(pkg, BALANCE_BY_DISTRICT).details["clarification"]
    adapter = SemanticLayerMCPAdapter(Runtime.from_path(str(pkg)))
    try:
        refused = adapter.call_tool("execute", {"query": BALANCE_BY_DISTRICT})
        (error,) = refused["errors"]
        assert error["code"] == "AMBIGUOUS_PATH"
        assert error["details"]["clarification"] == expected
        decision = expected["options"][1]["decision"]
        answered = adapter.call_tool(
            "execute", {"query": {**BALANCE_BY_DISTRICT, "route_decisions": [decision]}}
        )
        assert not answered.get("errors"), answered
        assert [w["code"] for w in answered["warnings"]] == ["ROUTE_CHOSEN_BY_QUERY"]
    finally:
        adapter.close()


def test_the_architect_mcp_records_a_route_decision(tmp_path):
    pkg = _write_package(tmp_path)
    decision = _refusal(pkg, BALANCE_BY_DISTRICT).details["clarification"]["options"][0]["decision"]
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def session_calls():
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            result = await session.call_tool(
                "record_route_decision",
                {
                    "project_path": "bank",
                    **decision,
                    "expected_revision": ArchitectProject(pkg, workspace_root=tmp_path).revision(),
                    "idempotency_key": "decide",
                },
            )
            return tools, dict(result.structuredContent or {})

    tools, report = asyncio.run(session_calls())
    assert "record_route_decision" in tools
    assert (report["status"], report["changed_files"]) == ("recorded", ["graph.yml"]), report
    assert report["summary"].startswith("For an Account, 'District' now means the District of the")
    out = Runtime.from_path(str(pkg)).query(BALANCE_BY_DISTRICT)
    assert _rows(out, ["dimension.bank_district_name", "v"]) == _gold(BY_BRANCH)


# --- The bypass guard: every entry point asks, and follows a query row --------------------

BILLING_ROWS = resolution.ROUTE_ROWS["branch"].replace("branch_region_id", "billing_region_id")
ENTRY_ROUTES = {
    tuple(resolution.BRANCH): resolution.ROUTE_ROWS["branch"],
    tuple(resolution.BILLING): BILLING_ROWS,
    tuple(resolution.HOME): resolution.ROUTE_ROWS["home"],
}


def _entry_package(root: Path, pins: list[dict[str, Any]] | None = None) -> Path:
    """The route resolver's own fixture with two direct keys to the region (no route is
    chosen), within two hops so every route is one a query can answer by."""
    pkg = resolution._write_package(root, relationships=resolution.TWO_KEYS, pins=pins)
    graph = yaml.safe_load((pkg / "graph.yml").read_text())
    graph["graph"]["path_policy"] = {"max_hops": 2}
    (pkg / "graph.yml").write_text(yaml.safe_dump(graph, sort_keys=False))
    return pkg


def _entry_gold(entry: str, route: list[str]) -> list[tuple]:
    sql = resolution._entry_gold(entry, "branch").replace(
        resolution.ROUTE_ROWS["branch"], ENTRY_ROUTES[tuple(route)]
    )
    return resolution._gold(sql)


@pytest.mark.parametrize("entry", resolution.ENTRY_POINTS)
def test_every_entry_point_asks_and_every_option_answers_both_ways(tmp_path, entry):
    query, columns = resolution.ENTRY_POINTS[entry]
    err = resolution._refusal(_entry_package(tmp_path / "refused"), query)
    start, target = err.details["start"], err.details["target"]
    options = err.details["clarification"]["options"]
    assert len(options) >= 2 and len({option["id"] for option in options}) == len(options)
    runtime = Runtime.from_path(str(_entry_package(tmp_path / "per_query")))
    for index, option in enumerate(options):
        route, decision = option["relationship_path"], option["decision"]
        assert (decision["source_entity"], decision["target_entity"]) == (start, target)
        recorded = Runtime.from_path(str(_entry_package(tmp_path / f"o{index}", pins=[decision])))
        per_query = {**query, "route_decisions": [decision]}
        if columns is None:  # the conversion: its SQL reads the chosen route's key only
            key, other = (
                ("billing", "branch")
                if route[-1].endswith("billing_region")
                else ("branch", "billing")
            )
            for sql in (
                recorded.compile(query)["explain"]["rendered_sql"],
                runtime.compile(per_query)["explain"]["rendered_sql"],
            ):
                assert f"{key}_region_id" in sql and f"{other}_region_id" not in sql
            continue
        gold = _entry_gold(entry, route)
        out = runtime.query(per_query)
        assert resolution._rows(out, columns) == gold
        assert [row["row"]["relationship_path"] for row in _chosen_by_query(out)] == [route]
        assert resolution._rows(recorded.query(query), columns) == gold


def test_discovery_follows_a_query_row(tmp_path):
    config = load_package_config(str(_write_package(tmp_path)))
    availability = _path_availability(config, ACCOUNT, DISTRICT)
    assert (availability["available"], availability["error_code"]) == (False, "AMBIGUOUS_PATH")
    assert (
        availability["details"]["clarification"]["options"][1]["relationship_path"] == OWNER_ROUTE
    )
    with query_route_decisions({(ACCOUNT, DISTRICT): OWNER_ROUTE}):
        assert _path_availability(config, ACCOUNT, DISTRICT)["path"] == OWNER_ROUTE
    assert _path_availability(config, ACCOUNT, DISTRICT)["available"] is False
    runtime = Runtime.from_path(str(_write_package(tmp_path / "options")))
    partial = {"version": 1, "select": BALANCE_BY_DISTRICT["select"]}
    for routes, available in (([], False), ([DIAMOND_ROW], True)):
        payload = build_options_payload(
            runtime,
            partial_query={**partial, "route_decisions": routes},
            step="group_by",
            verbosity="full",
            limit=50,
        )
        (row,) = [
            row
            for bucket in ("recommended", "available", "blocked")
            for row in payload[bucket]
            if row["id"] == "dimension.bank_district_name"
        ]
        assert row["available"] is available
        if available:  # the next query keeps the person's choice
            assert row["query_patch"]["route_decisions"] == [DIAMOND_ROW]
        else:
            assert row["blocked_reason"] == "route decision required"
