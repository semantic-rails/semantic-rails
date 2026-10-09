"""Which of two routes to one entity a question means is a business definition, so the engine
never guesses one.

For each (start, target) the one route resolver (``fanout.resolve_path``) takes, in order: a
``graph.path_preferences`` row for the pair; the one direct relationship from the start that
reaches at most one row (its own key); the rows of pairs its routes walk through; the only
route. Anything else is refused as AMBIGUOUS_PATH, whatever the routes' lengths, naming each
route, its meaning and the row that would record it (test_route_precedence.py covers the rows
a route inherits). So adding a route never changes an answer silently. Where the engine chose
one of two or more routes, the response carries a short note (ROUTE_RECORDED or
ROUTE_COLOCATED_KEY) with the chosen route, except at minimal verbosity.

Fixture: accounts, their owners, regions, memberships and invoices, on DuckDB. The routes
disagree on the data, so an answer shows which route it took:

    account -> region                  the account's branch region (its own key)
    account -> region                  the account's billing region (a second own key)
    account -> owner -> region         the owner's home region
    account <- membership              memberships held on the account (one-to-many)
    account -> owner -> membership     the owner's primary membership
    invoice -> account
    invoice -> region                  the region the invoice was issued in (its own key)
"""

from __future__ import annotations

import gc
import json
import os
import textwrap
import weakref
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

import semantic_rails.fanout as fanout_module
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.indexes import _PACKAGE_ANALYSIS_CACHE, get_package_analysis
from semantic_rails.compiler_parts.paths import _direct_entity_key_source_expr
from semantic_rails.config import load_package_config
from semantic_rails.diagnostics import exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import ColumnRefExpr
from semantic_rails.fanout import pass_through, resolve_path
from semantic_rails.metadata_parts.path_coverage import _path_availability
from semantic_rails.registry import Registry
from semantic_rails.route_census import census_pairs, resolve_pairs
from semantic_rails.runtime import Runtime
from semantic_rails.runtime import _route_notes as compiled_route_notes
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    PathPolicyConfig,
    PathPreferenceConfig,
    RelationshipConfig,
)

ROOT = Path(__file__).resolve().parents[2]

SEED_SQL = """
CREATE TABLE regions (region_id INTEGER, region_name VARCHAR, launched_at TIMESTAMP);
INSERT INTO regions VALUES
  (1, 'North', '2020-01-01'), (2, 'South', '2021-01-01'), (3, 'East', '2022-01-01');
CREATE TABLE owners (
  owner_id INTEGER, owner_name VARCHAR, home_region_id INTEGER, primary_membership_id INTEGER
);
INSERT INTO owners VALUES (10, 'Ann', 2, 100), (11, 'Bob', 3, 104), (12, 'Cy', 1, 103);
CREATE TABLE accounts (
  account_id INTEGER, owner_id INTEGER, branch_region_id INTEGER, billing_region_id INTEGER,
  account_kind VARCHAR, balance INTEGER
);
INSERT INTO accounts VALUES
  (1, 10, 1, 3, 'checking', 100),
  (2, 10, 1, 2, 'savings', 200),
  (3, 11, 2, 1, 'savings', 350),
  (4, 12, 3, 1, 'savings', 400),
  (5, 11, 1, 2, 'checking', 500);
CREATE TABLE memberships (membership_id INTEGER, account_id INTEGER, tier VARCHAR);
INSERT INTO memberships VALUES
  (100, 1, 'premium'), (101, 1, 'basic'), (102, 2, 'basic'),
  (103, 4, 'basic'), (104, 3, 'premium'), (105, 5, 'basic');
CREATE TABLE invoices (
  invoice_id INTEGER, account_id INTEGER, amount INTEGER, issued_at TIMESTAMP,
  issued_region_id INTEGER
);
INSERT INTO invoices VALUES
  (1000, 1, 10, '2024-01-05', 2), (1001, 2, 20, '2024-01-06', 3),
  (1002, 3, 40, '2024-02-01', 3), (1003, 4, 80, '2024-02-10', 1),
  (1004, 5, 160, '2024-03-01', 2), (1005, 1, 5, '2024-03-02', 1);
"""

ACCOUNT, OWNER, REGION = "entity.bank_account", "entity.bank_owner", "entity.bank_region"
MEMBERSHIP, INVOICE = "entity.bank_membership", "entity.bank_invoice"
BALANCE, LAUNCH_BALANCE = "measure.bank.balance", "measure.bank.launch_balance"
ACCOUNT_COUNT, REGION_COUNT = "measure.bank.account_count", "measure.bank.region_count"
AMOUNT, INVOICE_COUNT = "measure.bank.amount", "measure.bank.invoice_count"
REGION_NAME, REGION_KEY = "dimension.bank_region_name", "dimension.bank_region_id"
OWNER_NAME, TIER = "dimension.bank_owner_name", "dimension.bank_membership_tier"
ACCOUNT_KIND = "dimension.bank_account_kind"

BRANCH = ["relationship.accounts_branch_region"]
BILLING = ["relationship.accounts_billing_region"]
HOME = ["relationship.accounts_owner", "relationship.owners_home_region"]
HELD = ["relationship.memberships_account"]
PRIMARY = ["relationship.accounts_owner", "relationship.owners_primary_membership"]
INVOICE_ACCOUNT = ["relationship.invoices_account"]

_RELATIONSHIPS = {
    "accounts_branch_region": ("account", "region", "branch_region_id", "region_id"),
    "accounts_billing_region": ("account", "region", "billing_region_id", "region_id"),
    "accounts_owner": ("account", "owner", "owner_id", "owner_id"),
    "owners_home_region": ("owner", "region", "home_region_id", "region_id"),
    "memberships_account": ("membership", "account", "account_id", "account_id"),
    "owners_primary_membership": ("owner", "membership", "primary_membership_id", "membership_id"),
    "invoices_account": ("invoice", "account", "account_id", "account_id"),
    "invoices_issued_region": ("invoice", "region", "issued_region_id", "region_id"),
}
# The branch and home roles of a region, the two roles of a membership, and invoices.
DIAMOND = (
    "accounts_branch_region",
    "accounts_owner",
    "owners_home_region",
    "memberships_account",
    "owners_primary_membership",
    "invoices_account",
)
# Count distinct across a one-to-many hop needs the author's say-so.
_ROLLUP_SAFE_REVERSE = {"accounts_branch_region", "memberships_account"}


def _pin(start: str, target: str, path: list[str]) -> dict[str, Any]:
    return {"source_entity": start, "target_entity": target, "relationship_path": path}


def _write_package(
    root: Path,
    *,
    relationships: tuple[str, ...] = DIAMOND,
    pins: list[dict[str, Any]] | None = None,
    labels: dict[str, str] | None = None,
    extra: dict[str, dict[str, Any]] | None = None,
) -> Path:
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
              description: Two roles of a region and of a membership.
              warehouse: duckdb
              default_db: {(root / "bank.duckdb").as_posix()}
              seed:
                kind: sql_script
                source: data/seed.sql
            defaults:
              dimension:
                groupable: true
                filterable: true
              relationship:
                traversal: [forward, reverse]
            """
        )
    )
    edges: dict[str, Any] = {}
    for name in relationships:
        source, target, via, key = _RELATIONSHIPS[name]
        edges[name] = {
            "as": f"relationship.{name}",
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [via],
            "target": [key],
            **({"label": labels[name]} if name in (labels or {}) else {}),
            **(extra or {}).get(name, {}),
        }
        if name in _ROLLUP_SAFE_REVERSE:
            edges[name]["rollup_safe"] = {"reverse": ["count_distinct"]}
    graph: dict[str, Any] = {
        "entities": {
            key: {"label": key.title(), "key": [f"{key}_id"], "model": f"{key}s"}
            for key in ("region", "owner", "account", "membership", "invoice")
        },
        "relationships": edges,
    }
    if pins:
        graph["path_preferences"] = pins
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}, sort_keys=False))

    def model(key: str, body: dict[str, Any]) -> None:
        spec = {"id": f"{key}s", "relation": f"{key}s", "entities": {key: {}}, **body}
        (pkg / "models" / f"{key}s.yml").write_text(yaml.safe_dump({"model": spec}))

    count = {"kind": "entity_count", "accumulation": {"kind": "population"}, "value_type": "count"}
    flow = {"kind": "aggregate", "accumulation": {"kind": "flow"}, "value_type": "count"}
    model(
        "region",
        {
            "dimensions": {"name": {"column": "region_name", "kind": "categorical"}},
            "times": {
                "launched_at": {
                    "label": "Launched at",
                    "column": "launched_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "as": "temporal_role.bank_region_launched",
                }
            },
            "measures": {"region_count": {**count, "label": "Regions", "entity_key": "region_id"}},
        },
    )
    model("owner", {"dimensions": {"name": {"column": "owner_name", "kind": "categorical"}}})
    model(
        "account",
        {
            "dimensions": {"kind": {"column": "account_kind", "kind": "categorical"}},
            "measures": {
                "balance": {**flow, "label": "Balance", "expr": "balance"},
                "launch_balance": {
                    **flow,
                    "label": "Balance by region launch",
                    "expr": "balance",
                    "times": ["temporal_role.bank_region_launched"],
                },
                "account_count": {**count, "label": "Accounts", "entity_key": "account_id"},
            },
        },
    )
    model("membership", {"dimensions": {"tier": {"kind": "categorical"}}})
    model(
        "invoice",
        {
            "times": {
                "issued_at": {
                    "label": "Issued at",
                    "column": "issued_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "default": True,
                }
            },
            "measures": {
                "amount": {**flow, "label": "Amount", "expr": "amount"},
                "invoice_count": {**count, "label": "Invoices", "entity_key": "invoice_id"},
            },
        },
    )
    (pkg / "metrics.yml").write_text(
        yaml.safe_dump(
            {
                "metrics": {
                    "bank.north_balance": {
                        "as": "metric.bank.north_balance",
                        "label": "North balance",
                        "kind": "aggregate",
                        "value_type": "count",
                        "expression": {
                            "kind": "aggregate",
                            "measure": "balance",
                            "aggregation": "sum",
                            "filter": {
                                "all": [{"field": REGION_NAME, "op": "=", "value": "North"}]
                            },
                        },
                    }
                }
            }
        )
    )
    return pkg


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _gold(sql: str) -> list[tuple]:
    con = duckdb.connect(":memory:")
    con.execute(SEED_SQL)
    return sorted(tuple(row) for row in con.execute(sql).fetchall())


def _rows(out: dict[str, Any], columns: list[str]) -> list[tuple]:
    return sorted(tuple(row[column] for column in columns) for row in out["rows"])


def _query(select: str, **parts: Any) -> dict[str, Any]:
    kind = "metric" if select.startswith("metric.") else "measure"
    return {"version": 1, "select": [{"expression": {kind: select}, "as": "v"}], **parts}


def _route_notes(out: dict[str, Any]) -> dict[tuple[str, str], tuple[str, list[str]]]:
    """Each route note in a response, by its (start, target): its code and the chosen route."""
    return {
        (w["object_ids"][0], w["object_ids"][1]): (w["code"], w["details"]["route"])
        for w in out["warnings"]
        if w["code"] in {"ROUTE_COLOCATED_KEY", "ROUTE_RECORDED"}
    }


def _refusal(pkg: Path, query: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(pkg)).query(query)
    assert exc_info.value.code == "AMBIGUOUS_PATH", exc_info.value
    return exc_info.value


# The balance rows of each account with the region each route gives it.
ROUTE_ROWS = {
    "branch": (
        "SELECT r.region_id, r.region_name, r.launched_at, a.balance FROM accounts a "
        "JOIN regions r ON r.region_id = a.branch_region_id"
    ),
    "home": (
        "SELECT r.region_id, r.region_name, r.launched_at, a.balance FROM accounts a "
        "JOIN owners o USING (owner_id) JOIN regions r ON r.region_id = o.home_region_id"
    ),
}
BY_BRANCH = f"SELECT region_name, SUM(balance) FROM ({ROUTE_ROWS['branch']}) GROUP BY 1"
BY_HOME = f"SELECT region_name, SUM(balance) FROM ({ROUTE_ROWS['home']}) GROUP BY 1"
# Accounts by membership tier: memberships held, or the owner's primary membership.
TIER_HELD = (
    "SELECT m.tier, COUNT(DISTINCT a.account_id) FROM accounts a "
    "JOIN memberships m USING (account_id) GROUP BY 1"
)
TIER_PRIMARY = (
    "SELECT m.tier, COUNT(DISTINCT a.account_id) FROM accounts a JOIN owners o USING (owner_id) "
    "JOIN memberships m ON m.membership_id = o.primary_membership_id GROUP BY 1"
)
# Invoice amount by region: through the account's branch, or its owner's home.
AMOUNT_BY_BRANCH = (
    "SELECT r.region_name, SUM(i.amount) FROM invoices i JOIN accounts a USING (account_id) "
    "JOIN regions r ON r.region_id = a.branch_region_id GROUP BY 1"
)
AMOUNT_BY_HOME = (
    "SELECT r.region_name, SUM(i.amount) FROM invoices i JOIN accounts a USING (account_id) "
    "JOIN owners o USING (owner_id) JOIN regions r ON r.region_id = o.home_region_id GROUP BY 1"
)
INVOICE_HOME = [*INVOICE_ACCOUNT, *HOME]
INVOICE_BRANCH = [*INVOICE_ACCOUNT, *BRANCH]


def test_the_direct_key_and_a_recorded_route_each_answer_with_a_short_note(tmp_path):
    """Account to region: the account's own branch key, or its owner's home region. The direct
    key answers; with the owner route recorded, the owner's answer comes back. Either way the
    response names the chosen route in one short note, and nothing more."""
    query = _query(BALANCE, group_by=[REGION_NAME])
    direct = _write_package(tmp_path / "direct")
    out = Runtime.from_path(str(direct)).query(query)
    assert _rows(out, [REGION_NAME, "v"]) == _gold(BY_BRANCH)
    assert _route_notes(out) == {(ACCOUNT, REGION): ("ROUTE_COLOCATED_KEY", BRANCH)}
    (note,) = [w for w in out["warnings"] if w["code"] == "ROUTE_COLOCATED_KEY"]
    assert (note["severity"], note["message"]) == ("info", "the Account's Region (own key)")
    # Each other route comes with the row that would make it the default.
    _, routes = resolve_path(load_package_config(str(direct)), start=ACCOUNT, target=REGION)
    assert note["details"] == {
        "route": BRANCH,
        "alternatives": [_pin(ACCOUNT, REGION, path) for path in routes[1:]],
    }
    assert _pin(ACCOUNT, REGION, HOME) in note["details"]["alternatives"]

    pinned = _write_package(tmp_path / "home", pins=[_pin("account", "region", HOME)])
    out = Runtime.from_path(str(pinned)).query(query)
    assert _rows(out, [REGION_NAME, "v"]) == _gold(BY_HOME)
    assert _route_notes(out) == {(ACCOUNT, REGION): ("ROUTE_RECORDED", HOME)}
    (note,) = [w for w in out["warnings"] if w["code"] == "ROUTE_RECORDED"]
    assert note["message"] == "the Region of the Account's Owner (recorded route)"
    assert _gold(BY_BRANCH) != _gold(BY_HOME)


@pytest.mark.parametrize(
    ("relationships", "pins", "verbosity"),
    [
        pytest.param(DIAMOND, None, "minimal", id="minimal-response"),
        pytest.param(("accounts_branch_region",), None, "compact", id="one-route"),
        pytest.param(
            ("accounts_branch_region",),
            [_pin("account", "region", BRANCH)],
            "compact",
            id="one-route-recorded",
        ),
    ],
)
def test_no_route_note_where_nothing_was_chosen_or_in_the_minimal_response(
    tmp_path, relationships, pins, verbosity
):
    """A pair with one route had nothing to choose, recorded or not; and the minimal response
    (the MCP default) carries no route note at all."""
    pkg = _write_package(tmp_path, relationships=relationships, pins=pins)
    query = {**_query(BALANCE, group_by=[REGION_NAME]), "verbosity": verbosity}
    out = Runtime.from_path(str(pkg)).query(query)
    assert _rows(out, [REGION_NAME, "v"]) == _gold(BY_BRANCH)
    assert _route_notes(out) == {}


def test_pinned_dense_graph_compiles_and_notes_use_bounded_work(tmp_path, monkeypatch):
    """A pin bypasses candidate enumeration even when hundreds of thousands of routes fit
    the ceiling. The note scans edges a bounded number of times and caches only a boolean.
    """
    base = load_package_config(str(_write_package(tmp_path)))
    entities = [EntityConfig(id=f"e{i}", table=f"e{i}", primary_key="id") for i in range(11)]
    relationships = [
        RelationshipConfig(
            id=f"r{i}_{j}",
            source_entity=f"e{i}",
            target_entity=f"e{j}",
            source_column=f"e{j}_id",
            target_column="id",
            cardinality="N:1",
            safety="safe",
            rollup_safe_aggregations_reverse=["count_distinct"],
        )
        for i in range(11)
        for j in range(i + 1, 11)
    ]
    config = replace(
        base,
        entities=entities,
        relationships=relationships,
        dimensions=[
            DimensionConfig(id="dimension.e0_id", entity="e0", column="id", data_type="id"),
            DimensionConfig(id="dimension.e10_id", entity="e10", column="id", data_type="id"),
        ],
        temporal_roles=[],
        measures=[
            replace(
                base.measures[0],
                id="measure.e10_count",
                entity="e10",
                row_grain=["dimension.e10_id"],
                expr=ColumnRefExpr("id"),
                measure_class="event_count",
                default_aggregation="count_distinct",
                allowed_aggregations=["count_distinct"],
                compatible_temporal_roles=[],
            )
        ],
        metric_recipes=[],
        path_preferences=[PathPreferenceConfig("e10", "e0", relationship_path=["r0_10"])],
        path_policy=PathPolicyConfig(max_hops=8),
    )
    analysis = get_package_analysis(config)
    work = {"enumerations": 0, "edges": 0}

    class CountedEdges(list):
        def __iter__(self):
            for edge in super().__iter__():
                work["edges"] += 1
                yield edge

    analysis.graph = {node: CountedEdges(edges) for node, edges in analysis.graph.items()}

    def count_enumeration(*_args, **_kwargs):
        work["enumerations"] += 1
        pytest.fail("a pinned compile or its notes must not enumerate candidates")

    monkeypatch.setattr(fanout_module, "enumerate_paths", count_enumeration)
    query = _query("measure.e10_count", group_by=["dimension.e0_id"], verbosity="compact")
    compiled = compile_query(config, Registry(config), query)
    notes = compiled_route_notes(config, compiled, query)
    assert [(note["code"], note["details"]) for note in notes] == [
        ("ROUTE_RECORDED", {"route": ["r0_10"]})
    ]
    assert work["enumerations"] == 0
    assert 0 < work["edges"] <= (config.path_policy.max_hops + 1) * 2 * len(relationships)
    assert analysis.path_cache == {}
    assert analysis.route_note_cache == {("e10", "e0"): True}
    first_work = dict(work)
    assert compiled_route_notes(config, compiled, query) == notes
    assert work == first_work


@pytest.mark.parametrize("hop_limit", [1, 2, 3])
@pytest.mark.parametrize("pinned_path", [BRANCH, HOME])
def test_recorded_route_notes_obey_the_hop_ceiling(tmp_path, monkeypatch, hop_limit, pinned_path):
    config = load_package_config(
        str(_write_package(tmp_path, pins=[_pin("account", "region", pinned_path)]))
    )
    config = replace(config, path_policy=PathPolicyConfig(max_hops=hop_limit))
    chosen, candidates = resolve_path(config, start=ACCOUNT, target=REGION)
    assert chosen == pinned_path

    def fail_enumeration(*_args, **_kwargs):
        pytest.fail("notes must not enumerate paths even when alternatives exceed the ceiling")

    monkeypatch.setattr(fanout_module, "enumerate_paths", fail_enumeration)
    assert candidates == [pinned_path]
    assert fanout_module.route_basis(config, ACCOUNT, REGION) == "decided"
    noted = fanout_module.route_note(config, ACCOUNT, REGION, pinned_path)
    assert (noted is not None) == (hop_limit >= 2)
    assert get_package_analysis(config).path_cache == {}


@pytest.mark.parametrize(
    "graph",
    [
        {"a": [("b", "ab")], "b": [("a", "ab"), ("c", "bc")]},
        {"a": [("b", "ab"), ("b", "role_ab")], "b": [("c", "bc")]},
        {"a": [("b", "ab"), ("c", "ac")], "b": [("c", "bc")]},
        {"a": [("b", "ab")], "c": [("b", "cb")]},
        {"a": [("b", "ab")], "b": [("c", "bc"), ("d", "bd")], "d": [("c", "dc")]},
    ],
    ids=["cycle", "parallel-roles", "diamond", "disallowed-direction", "shared-prefix"],
)
@pytest.mark.parametrize("hop_limit", [1, 2, 3])
def test_bounded_route_multiplicity_matches_simple_paths(graph, hop_limit):
    expected = len(fanout_module.enumerate_paths(graph, "a", "c", hop_limit)) > 1
    assert fanout_module._has_multiple_routes(graph, "a", "c", hop_limit) == expected


@pytest.mark.parametrize(
    ("relationships", "query", "columns", "start", "target", "golds"),
    [
        pytest.param(
            DIAMOND,
            _query(AMOUNT, group_by=[REGION_NAME]),
            [REGION_NAME, "v"],
            INVOICE,
            REGION,
            {tuple(INVOICE_BRANCH): AMOUNT_BY_BRANCH, tuple(INVOICE_HOME): AMOUNT_BY_HOME},
            id="different-lengths",
        ),
        pytest.param(
            ("invoices_account", "accounts_branch_region", "accounts_billing_region"),
            _query(AMOUNT, group_by=[REGION_NAME]),
            [REGION_NAME, "v"],
            INVOICE,
            REGION,
            {
                tuple(INVOICE_BRANCH): AMOUNT_BY_BRANCH,
                tuple([*INVOICE_ACCOUNT, *BILLING]): AMOUNT_BY_BRANCH.replace(
                    "branch_region_id", "billing_region_id"
                ),
            },
            id="equal-lengths",
        ),
        pytest.param(
            DIAMOND,
            _query(REGION_COUNT, group_by=[ACCOUNT_KIND]),
            [ACCOUNT_KIND, "v"],
            REGION,
            ACCOUNT,
            {
                tuple(BRANCH): (
                    "SELECT a.account_kind, COUNT(DISTINCT r.region_id) FROM regions r "
                    "JOIN accounts a ON a.branch_region_id = r.region_id GROUP BY 1"
                ),
                tuple(reversed(HOME)): (
                    "SELECT a.account_kind, COUNT(DISTINCT r.region_id) FROM regions r "
                    "JOIN owners o ON o.home_region_id = r.region_id "
                    "JOIN accounts a USING (owner_id) GROUP BY 1"
                ),
            },
            id="fan-out-only",
        ),
    ],
)
def test_routes_without_one_direct_key_are_refused_and_each_pin_answers_its_gold(
    tmp_path, relationships, query, columns, start, target, golds
):
    pkg = _write_package(tmp_path / "refused", relationships=relationships)
    err = _refusal(pkg, query)
    assert err.details["reason"] == "route_decision_required"
    assert (err.details["start"], err.details["target"]) == (start, target)
    options = err.details["clarification"]["options"]
    assert set(golds) <= {tuple(option["relationship_path"]) for option in options}
    assert len({option["meaning"] for option in options}) == len(options)
    assert "business definition" in err.details["hint"]
    # Each route's row loads as written, and the query then answers with that route.
    for index, option in enumerate(options):
        path, pin = option["relationship_path"], option["decision"]
        assert pin == {**_pin(start, target, path), "label": option["meaning"]}
        if tuple(path) not in golds:
            continue
        pinned = _write_package(tmp_path / f"pin{index}", relationships=relationships, pins=[pin])
        out = Runtime.from_path(str(pinned)).query(query)
        assert _rows(out, columns) == _gold(golds[tuple(path)])
        assert _route_notes(out)[(start, target)] == ("ROUTE_RECORDED", path)
    assert len({tuple(_gold(sql)) for sql in golds.values()}) == len(golds)


def test_the_refusal_reads_the_routes_by_their_labels(tmp_path):
    pkg = _write_package(
        tmp_path,
        labels={"owners_home_region": "Home region", "accounts_branch_region": "Branch region"},
    )
    err = _refusal(pkg, _query(AMOUNT, group_by=[REGION_NAME]))
    clarification = err.details["clarification"]
    assert clarification["question"] == "Which Region does the question mean for an Invoice?"
    # One relationship per entity pair: each hop is named by the entity it reaches.
    assert [(option["id"], option["meaning"]) for option in clarification["options"]] == [
        ("account_region", "the Region of the Invoice's Account"),
        ("account_owner_region", "the Region of the Owner of the Invoice's Account"),
        (
            "account_membership_owner_region",
            "the Region of any of the Owners of any of the Memberships of the Invoice's Account",
        ),
    ]
    # Parallel roles are told apart by their own label, else by their foreign-key columns.
    err = _refusal(
        _write_package(
            tmp_path / "roles",
            relationships=tuple(_RELATIONSHIPS)[:2],
            labels={"accounts_branch_region": "Branch region"},
        ),
        _query(BALANCE, group_by=[REGION_NAME]),
    )
    assert [
        (option["id"], option["meaning"]) for option in err.details["clarification"]["options"]
    ] == [
        ("billing_region", "the Account's Region (billing_region_id)"),
        ("branch_region", "the Account's Branch region"),
    ]


@pytest.mark.parametrize(
    ("before", "added", "query", "columns", "gold"),
    [
        pytest.param(
            ("invoices_account", "accounts_branch_region"),
            ("accounts_owner", "owners_home_region"),
            _query(AMOUNT, group_by=[REGION_NAME]),
            [REGION_NAME, "v"],
            None,
            id="one-route-pair-refuses",
        ),
        pytest.param(
            ("accounts_branch_region",),
            ("accounts_owner", "owners_home_region"),
            _query(BALANCE, group_by=[REGION_NAME]),
            [REGION_NAME, "v"],
            BY_BRANCH,
            id="direct-key-pair-discloses",
        ),
    ],
)
def test_adding_a_route_never_changes_an_answer_silently(
    tmp_path, before, added, query, columns, gold
):
    """A pair with one route answers by it. Give it a longer route: a pair with no direct key
    is refused from then on, and a pair whose one route is its own key keeps the answer and
    now notes that its own key was chosen."""
    out = Runtime.from_path(str(_write_package(tmp_path / "before", relationships=before))).query(
        query
    )
    assert _route_notes(out) == {}
    after = _write_package(tmp_path / "after", relationships=(*before, *added))
    if gold is None:
        assert _rows(out, columns) == _gold(AMOUNT_BY_BRANCH)
        _refusal(after, query)
        return
    assert _rows(out, columns) == _gold(gold)
    out = Runtime.from_path(str(after)).query(query)
    assert _rows(out, columns) == _gold(gold)
    assert _route_notes(out) == {(ACCOUNT, REGION): ("ROUTE_COLOCATED_KEY", BRANCH)}


_METRIC_PREDICATE = {
    "kind": "metric_predicate",
    "entity": REGION,
    "scope_mode": "entity_only",
    "input": {"measure": BALANCE},
    "op": ">=",
    "value": 500,
}
_CONVERSION = {
    "version": 1,
    "select": [
        {
            "as": "v",
            "expression": {
                "kind": "conversion",
                "entity": REGION,
                "window": {"unit": "day", "value": 60},
                "matching_mode": "first_converted_after_base",
                "base": {"kind": "aggregate", "measure": INVOICE_COUNT},
                "converted": {"kind": "aggregate", "measure": INVOICE_COUNT},
            },
        }
    ],
}
# Every way a query reaches the region from an account (or an invoice), with its columns;
# None where the answer isn't a plain table (checked by its SQL).
ENTRY_POINTS = {
    "group_by": (_query(BALANCE, group_by=[REGION_NAME]), [REGION_NAME, "v"]),
    "where": (_query(BALANCE, where=[{"field": REGION_NAME, "op": "=", "value": "North"}]), ["v"]),
    "measure_filter": (_query("metric.bank.north_balance"), ["v"]),
    "metric_predicate": (
        _query(
            BALANCE, metric_filters=[{"expression": _METRIC_PREDICATE, "op": "=", "value": True}]
        ),
        ["v"],
    ),
    "time_role": (
        _query(LAUNCH_BALANCE, time={"temporal_role": "temporal_role.bank_region_launched"}),
        ["v"],
    ),
    "direct_key_read": (_query(BALANCE, group_by=[REGION_KEY]), [REGION_KEY, "v"]),
    "conversion": (_CONVERSION, None),
}


def _entry_gold(entry: str, route: str) -> str:
    rows = ROUTE_ROWS[route]
    return {
        "group_by": f"SELECT region_name, SUM(balance) FROM ({rows}) GROUP BY 1",
        "where": f"SELECT SUM(balance) FROM ({rows}) WHERE region_name = 'North'",
        "measure_filter": f"SELECT SUM(balance) FROM ({rows}) WHERE region_name = 'North'",
        "metric_predicate": (
            f"SELECT SUM(balance) FROM ({rows}) WHERE region_id IN "
            f"(SELECT region_id FROM ({rows}) GROUP BY 1 HAVING SUM(balance) >= 500)"
        ),
        "time_role": f"SELECT SUM(balance) FROM ({rows}) GROUP BY launched_at",
        "direct_key_read": f"SELECT region_id, SUM(balance) FROM ({rows}) GROUP BY 1",
    }[entry]


# Two direct keys to the region (branch and billing) and the owner's home: no route is chosen.
TWO_KEYS = (*DIAMOND, "accounts_billing_region")


def test_the_direct_key_read_and_discovery_follow_the_resolver(tmp_path):
    refused = load_package_config(str(_write_package(tmp_path / "refused", relationships=TWO_KEYS)))
    assert _direct_entity_key_source_expr(ACCOUNT, REGION, "region_id", refused) is None
    availability = _path_availability(refused, ACCOUNT, REGION)
    assert (availability["available"], availability["error_code"]) == (False, "AMBIGUOUS_PATH")
    direct = load_package_config(str(_write_package(tmp_path / "direct")))
    expr = _direct_entity_key_source_expr(ACCOUNT, REGION, "region_id", direct)
    assert expr is not None and expr.parts[-1] == "branch_region_id"
    assert _path_availability(direct, ACCOUNT, REGION)["path"] == BRANCH
    home = load_package_config(
        str(_write_package(tmp_path / "home", pins=[_pin("account", "region", HOME)]))
    )
    assert _direct_entity_key_source_expr(ACCOUNT, REGION, "region_id", home) is None
    assert _path_availability(home, ACCOUNT, REGION)["path"] == HOME


def test_a_refusal_is_cached_and_its_recovery_hint_carries_the_rows(tmp_path, monkeypatch):
    config = load_package_config(str(_write_package(tmp_path)))
    with pytest.raises(SemanticLayerError) as first:
        resolve_path(config, start=INVOICE, target=REGION)

    def fail_enumerate_paths(*_args, **_kwargs):
        raise AssertionError("a refused pair should not enumerate the graph again")

    monkeypatch.setattr(fanout_module, "enumerate_paths", fail_enumerate_paths)
    with pytest.raises(SemanticLayerError) as second:
        resolve_path(config, start=INVOICE, target=REGION)
    assert (second.value.code, second.value.details) == (first.value.code, first.value.details)
    second.value.details["clarification"]["options"].clear()  # a copy, not the cached refusal
    with pytest.raises(SemanticLayerError) as third:
        resolve_path(config, start=INVOICE, target=REGION)
    assert third.value.details == first.value.details
    issue = exception_issue(first.value, stage="compile")
    (hint,) = issue["recovery_hints"]
    assert "clarification" not in hint
    assert issue["details"]["clarification"] == first.value.details["clarification"]


@pytest.mark.parametrize(
    ("relationships", "start", "target", "code"),
    [
        pytest.param(DIAMOND, INVOICE, REGION, "AMBIGUOUS_PATH", id="ambiguous"),
        pytest.param(("accounts_branch_region",), ACCOUNT, INVOICE, "PATH_NOT_FOUND", id="no-path"),
    ],
)
def test_a_cached_refusal_lets_a_discarded_package_be_collected(
    tmp_path, relationships, start, target, code
):
    """The cache keeps a refusal as plain data, never the raised exception, whose traceback
    would hold the package configuration and keep its cache entry alive forever."""
    config = load_package_config(str(_write_package(tmp_path, relationships=relationships)))
    for _ in range(2):  # a miss, then a hit
        with pytest.raises(SemanticLayerError) as exc_info:
            resolve_path(config, start=start, target=target)
        assert exc_info.value.code == code
    del exc_info
    collected, key = weakref.ref(config), id(config)
    del config
    gc.collect()
    assert collected() is None
    assert key not in _PACKAGE_ANALYSIS_CACHE


@pytest.mark.parametrize("form", ["graph_relationship", "model_join"])
def test_a_package_that_weights_a_relationship_is_refused_at_load(tmp_path, form):
    """The relationship weight is gone: the package fails to load, naming the relationship and
    the key, rather than quietly answering by another route."""
    if form == "graph_relationship":
        pkg = _write_package(tmp_path, extra={"accounts_owner": {"path_preference": 10}})
        relationship = "graph relationship 'accounts_owner'"
    else:
        pkg = _write_package(tmp_path)
        path = pkg / "graph.yml"
        spec = yaml.safe_load(path.read_text())
        spec["graph"]["relationships"]["invoices_region"] = {
            "entities": ["invoice", "region"],
            "via": ["issued_region_id"],
            "path_preference": 10,
        }
        path.write_text(yaml.safe_dump(spec))
        relationship = "graph relationship 'invoices_region'"
    with pytest.raises(SemanticLayerError) as exc_info:
        load_package_config(str(pkg))
    assert exc_info.value.code == "INVALID_CONFIG"
    assert f"{relationship} has unknown key 'path_preference'" in str(exc_info.value)


SHIPPED_PACKAGES = [
    "configs/semantic_rails/jaffle_shop",
    "configs/semantic_rails/tpch_sf1_showcase",
    "comparisons/semantic_layers/semantic_rails/package",
    "tests/integration/correctness/shop",
    "configs/examples/semantic_rails_package_starter.yml",
]
ROUTE_DECISIONS = Path(__file__).parent / "fixtures" / "route_decisions.json"


def _route_decisions(package: str) -> dict[str, list[list[str]]]:
    """Every entity pair that is refused (no row, or every route excluded by rows), every pair
    answered by its direct key over another route, and every pair answered by rows recorded
    for pairs its routes walk through."""
    config = load_package_config(str(ROOT / package))
    found: dict[str, list[list[str]]] = {
        "refused": [],
        "excluded_by_decision": [],
        "direct_key": [],
        "inherited": [],
    }
    for start in config.entities:
        for target in config.entities:
            if start.id == target.id:
                continue
            pair = [start.id, target.id]
            try:
                resolution = fanout_module.resolve_route(config, start=start.id, target=target.id)
            except SemanticLayerError as exc:
                if exc.code == "AMBIGUOUS_PATH":
                    found["refused"].append(pair)
                elif exc.details.get("reason") == "excluded_by_decision":
                    found["excluded_by_decision"].append(pair)
                continue
            if resolution.basis == "colocated_key" and len(resolution.routes) > 1:
                found["direct_key"].append(pair)
            elif resolution.basis == "inherited":
                found["inherited"].append(pair)
    return {key: sorted(pairs) for key, pairs in found.items()}


@pytest.mark.parametrize("package", SHIPPED_PACKAGES)
def test_shipped_route_decisions_match_the_reviewed_snapshot(package):
    """A relationship or row edit that changes which pairs are refused, answered by a direct
    key, or answered by an inherited row fails here, so a new route can't change an answer
    unreviewed. Review the pairs, record each pair a question needs in graph.path_preferences,
    then update the snapshot."""
    snapshot = json.loads(ROUTE_DECISIONS.read_text(encoding="utf-8"))
    current = _route_decisions(package)
    assert current == snapshot[package], json.dumps({package: current}, indent=2)


ROUTE_RESOLUTIONS = Path(__file__).parent / "fixtures" / "route_resolutions.json"


def _route_resolutions(package: str) -> dict[str, str]:
    """Every census pair's package resolution: the rung that chose its route and the route,
    or the code it is refused with."""
    config = load_package_config(str(ROOT / package))
    return {
        f"{start} -> {target}": (
            f"refused {outcome.refused}"
            if outcome.refused
            else f"{outcome.basis}: {', '.join(outcome.path)}"
        )
        for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items()
    }


@pytest.mark.parametrize("package", SHIPPED_PACKAGES[:4])
def test_shipped_route_resolutions_match_the_reviewed_snapshot(package):
    """Which route answers each pair a bundled package can be asked about, or how it is
    refused. A change that moves any pair fails here: review the moves, then update the
    snapshot (``SR_UPDATE_SNAPSHOTS=1``)."""
    snapshot = json.loads(ROUTE_RESOLUTIONS.read_text(encoding="utf-8"))
    current = _route_resolutions(package)
    if os.environ.get("SR_UPDATE_SNAPSHOTS") == "1":
        snapshot[package] = current
        ROUTE_RESOLUTIONS.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    assert current == snapshot[package], json.dumps({package: current}, indent=2)


SNAPSHOT = "entity.jaffle_store_inventory_snapshot"
CALENDAR = "entity.jaffle_time"


def test_bundled_routes_through_undeclared_rows_are_exactly_these():
    """The answered pairs whose route enters another table by one key and leaves to another
    parent, without a link-table declaration, a history or a recorded row walking it: each
    gets ROUTE_PASS_THROUGH when a query reads it."""
    found = {}
    for package in SHIPPED_PACKAGES[:4]:
        config = load_package_config(str(ROOT / package))
        for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items():
            crossings = [] if outcome.refused else pass_through(config, start, outcome.path)
            if crossings:
                found[(start, target)] = [row["entity"] for row in crossings]
    assert found == {
        **{
            pair: [SNAPSHOT]
            for pair in [
                ("entity.jaffle_customer", CALENDAR),
                ("entity.jaffle_customer_history", CALENDAR),
                ("entity.jaffle_item", CALENDAR),
                ("entity.jaffle_store", CALENDAR),
                (CALENDAR, "entity.jaffle_store"),
            ]
        },
        ("entity.shop_customer", "entity.shop_customer_history"): ["entity.shop_order"],
    }
    shop = load_package_config(str(ROOT / "tests/integration/correctness/shop"))
    path = resolve_path(shop, start="entity.shop_customer", target="entity.shop_customer_history")
    assert fanout_module.route_reading(shop, "entity.shop_customer", path[0]) == (
        "the Customer history valid at the time of any of the Customer's Orders"
    )
