"""The engine never chooses between join routes that can mean different things by hop count
or by path_preference weights.

For each (start, target) the one route resolver (``fanout.resolve_path``) takes a
``graph.path_preferences`` pin; otherwise the eligible routes are every functional route
(each hop reaches at most one row in the direction walked) and every route with a one-to-many
hop that is no longer than the shortest functional route. Eligible routes of different lengths
are refused as AMBIGUOUS_PATH, naming each route with the pin that would choose it.

Fixture: accounts, their owners, regions, memberships and invoices, on DuckDB. The routes
disagree on the data, so an answer shows which route it took:

    account -> region                  the account's branch region
    account -> owner -> region         the owner's home region
    account <- membership              memberships held on the account (one-to-many)
    account -> owner -> membership     the owner's primary membership
    invoice -> account
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler_parts.grain_recovery import _chosen_path
from semantic_rails.compiler_parts.paths import _direct_entity_key_source_expr
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import resolve_path
from semantic_rails.metadata_parts.path_coverage import _path_availability
from semantic_rails.runtime import Runtime

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
  invoice_id INTEGER, account_id INTEGER, amount INTEGER, issued_at TIMESTAMP
);
INSERT INTO invoices VALUES
  (1000, 1, 10, '2024-01-05'), (1001, 2, 20, '2024-01-06'), (1002, 3, 40, '2024-02-01'),
  (1003, 4, 80, '2024-02-10'), (1004, 5, 160, '2024-03-01'), (1005, 1, 5, '2024-03-02');
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
HOME = ["relationship.accounts_owner", "relationship.owners_home_region"]
HELD = ["relationship.memberships_account"]
PRIMARY = ["relationship.accounts_owner", "relationship.owners_primary_membership"]

_RELATIONSHIPS = {
    "accounts_branch_region": ("account", "region", "branch_region_id", "region_id"),
    "accounts_billing_region": ("account", "region", "billing_region_id", "region_id"),
    "accounts_owner": ("account", "owner", "owner_id", "owner_id"),
    "owners_home_region": ("owner", "region", "home_region_id", "region_id"),
    "memberships_account": ("membership", "account", "account_id", "account_id"),
    "owners_primary_membership": ("owner", "membership", "primary_membership_id", "membership_id"),
    "invoices_account": ("invoice", "account", "account_id", "account_id"),
}
# The two roles of a region and of a membership, and invoices.
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
    weights: dict[str, int] | None = None,
    pins: list[dict[str, Any]] | None = None,
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
              measure:
                subject_entity: self
                aggregation_entity: self
              relationship:
                traversal: [forward, reverse]
            """
        )
    )
    edges: dict[str, Any] = {}
    for name in relationships:
        source, target, via, key = _RELATIONSHIPS[name]
        edges[name] = {
            "id": f"relationship.{name}",
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [via],
            "target": [key],
        }
        if name in (weights or {}):
            edges[name]["path_preference"] = (weights or {})[name]
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


def _rows(runtime: Runtime, query: dict[str, Any], columns: list[str]) -> list[tuple]:
    out = runtime.query(query)
    return sorted(tuple(row[column] for column in columns) for row in out["rows"])


def _query(select: str, **parts: Any) -> dict[str, Any]:
    kind = "metric" if select.startswith("metric.") else "measure"
    return {"version": 1, "select": [{"expression": {kind: select}, "as": "v"}], **parts}


# Balance by region, through each route.
BY_BRANCH = (
    "SELECT r.region_name, SUM(a.balance) FROM accounts a "
    "JOIN regions r ON r.region_id = a.branch_region_id GROUP BY 1"
)
HOME_ROWS = (
    "SELECT r.region_name, a.balance FROM accounts a JOIN owners o USING (owner_id) "
    "JOIN regions r ON r.region_id = o.home_region_id"
)
BY_HOME = f"SELECT region_name, SUM(balance) FROM ({HOME_ROWS}) GROUP BY 1"
NORTH_BY_HOME = f"SELECT SUM(balance) FROM ({HOME_ROWS}) WHERE region_name = 'North'"
# Accounts by membership tier: memberships held, or the owner's primary membership.
TIER_HELD = (
    "SELECT m.tier, COUNT(DISTINCT a.account_id) FROM accounts a "
    "JOIN memberships m USING (account_id) GROUP BY 1"
)
TIER_PRIMARY = (
    "SELECT m.tier, COUNT(DISTINCT a.account_id) FROM accounts a JOIN owners o USING (owner_id) "
    "JOIN memberships m ON m.membership_id = o.primary_membership_id GROUP BY 1"
)


def _refusal(pkg: Path, query: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(pkg)).query(query)
    assert exc_info.value.code == "AMBIGUOUS_PATH", exc_info.value
    return exc_info.value


@pytest.mark.parametrize(
    ("query", "columns", "start", "target", "golds"),
    [
        pytest.param(
            _query(BALANCE, group_by=[REGION_NAME]),
            [REGION_NAME, "v"],
            ACCOUNT,
            REGION,
            {tuple(BRANCH): BY_BRANCH, tuple(HOME): BY_HOME},
            id="accounts-by-region",
        ),
        pytest.param(
            _query(ACCOUNT_COUNT, group_by=[TIER]),
            [TIER, "v"],
            ACCOUNT,
            MEMBERSHIP,
            {tuple(HELD): TIER_HELD, tuple(PRIMARY): TIER_PRIMARY},
            id="accounts-by-membership-tier",
        ),
    ],
)
@pytest.mark.parametrize(
    "weights",
    [
        None,
        {"accounts_owner": 1, "owners_home_region": 1, "accounts_branch_region": 500},
        {"accounts_owner": 1, "owners_primary_membership": 1, "memberships_account": 500},
    ],
    ids=["unweighted", "weights-favour-the-longer-region-route", "weights-favour-primary"],
)
def test_routes_of_different_lengths_are_refused_and_each_pin_answers_its_gold(
    tmp_path, query, columns, start, target, golds, weights
):
    err = _refusal(_write_package(tmp_path / "refused", weights=weights), query)
    assert err.details["start"] == start
    assert err.details["target"] == target
    assert sorted(map(tuple, err.details["candidates"])) == sorted(golds)
    assert "path_preferences" in err.details["hint"]
    # Each suggested pin row loads as written, and the query then answers with its route.
    for index, pin in enumerate(err.details["pins"]):
        assert pin == _pin(start, target, err.details["candidates"][index])
        runtime = Runtime.from_path(str(_write_package(tmp_path / f"pin{index}", pins=[pin])))
        gold = _gold(golds[tuple(pin["relationship_path"])])
        assert _rows(runtime, query, columns) == gold
    assert _gold(BY_BRANCH) != _gold(BY_HOME)
    assert _gold(TIER_HELD) != _gold(TIER_PRIMARY)


def test_a_parent_measure_filtered_by_a_child_is_refused_when_a_longer_lookup_exists(tmp_path):
    """Invoice amount where the membership tier is premium: the memberships held on the
    invoice's account (a one-to-many hop) or the account owner's primary membership (three
    lookups). Neither is answered by the shorter route."""
    query = _query(AMOUNT, where=[{"field": TIER, "op": "=", "value": "premium"}])
    err = _refusal(_write_package(tmp_path), query)
    assert err.details["candidates"] == [
        ["relationship.invoices_account", *HELD],
        ["relationship.invoices_account", *PRIMARY],
    ]


def test_a_longer_one_to_many_route_leaves_the_functional_route_alone(tmp_path):
    """Account to owner is one lookup; through the branch region or a membership it fans out.
    Only the lookup is eligible: it answers, with no refusal and no warning."""
    runtime = Runtime.from_path(str(_write_package(tmp_path)))
    out = runtime.query(_query(BALANCE, group_by=[OWNER_NAME]))
    gold = _gold(
        "SELECT o.owner_name, SUM(a.balance) FROM accounts a JOIN owners o USING (owner_id) "
        "GROUP BY 1"
    )
    assert sorted((row[OWNER_NAME], row["v"]) for row in out["rows"]) == gold
    assert not [w for w in out["warnings"] if w["code"] == "PATH_ALTERNATES_UNPINNED"]
    _, candidates = resolve_path(runtime.config, start=ACCOUNT, target=OWNER)
    assert len(candidates) == 3  # the two fan-out routes were considered, not chosen


@pytest.mark.parametrize(
    ("weights", "gold"),
    [
        (
            {"accounts_billing_region": 50},
            "SELECT r.region_name, SUM(a.balance) FROM accounts a "
            "JOIN regions r ON r.region_id = a.billing_region_id GROUP BY 1",
        ),
        ({"accounts_branch_region": 50}, BY_BRANCH),
        (None, None),
    ],
    ids=["billing-weighted", "branch-weighted", "equal-sums"],
)
def test_equal_length_routes_keep_the_weight_tie_break(tmp_path, weights, gold):
    pkg = _write_package(
        tmp_path,
        relationships=("accounts_branch_region", "accounts_billing_region", "accounts_owner"),
        weights=weights,
    )
    query = _query(BALANCE, group_by=[REGION_NAME])
    if gold is None:
        err = _refusal(pkg, query)
        assert len(err.details["candidates"]) == 2
        return
    assert _rows(Runtime.from_path(str(pkg)), query, [REGION_NAME, "v"]) == _gold(gold)


@pytest.mark.parametrize(
    "weights",
    [None, {"accounts_branch_region": 500, "accounts_owner": 1, "owners_home_region": 1}],
    ids=["unweighted", "weighted"],
)
def test_fan_out_only_routes_keep_the_shortest_and_always_warn(tmp_path, weights):
    """Every route from a region to an account fans out, so today's rule stands (the
    shortest, the branch accounts) and the warning is there whatever the weights say."""
    runtime = Runtime.from_path(str(_write_package(tmp_path, weights=weights)))
    out = runtime.query(_query(REGION_COUNT, group_by=[ACCOUNT_KIND]))
    gold = _gold(
        "SELECT a.account_kind, COUNT(DISTINCT r.region_id) FROM regions r "
        "JOIN accounts a ON a.branch_region_id = r.region_id GROUP BY 1"
    )
    assert sorted((row[ACCOUNT_KIND], row["v"]) for row in out["rows"]) == gold
    warnings = [w for w in out["warnings"] if w["code"] == "PATH_ALTERNATES_UNPINNED"]
    assert [w["details"]["chosen_path"] for w in warnings] == [BRANCH]


def test_discovery_grain_recovery_and_compile_report_the_pinned_route(tmp_path):
    pkg = _write_package(tmp_path, pins=[_pin("account", "region", HOME)])
    config = load_package_config(str(pkg))
    assert _path_availability(config, ACCOUNT, REGION)["path"] == HOME
    assert _chosen_path(config, start=ACCOUNT, target=REGION) == HOME
    compiled = Runtime.from_path(str(pkg)).compile(_query(BALANCE, group_by=[REGION_NAME]))
    assert compiled["hop_profile"]["targets"][REGION]["path"] == HOME


_METRIC_PREDICATE = {
    "kind": "metric_predicate",
    "entity": REGION,
    "scope_mode": "entity_only",
    "input": {"measure": BALANCE},
    "op": ">=",
    "value": 500,
}
_CONVERSION = {
    "version": 2,
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
# Every way a query reaches the region from an account (or an invoice), with the home-region
# gold for the pinned package; None where the answer isn't a plain table (checked by its SQL).
ENTRY_POINTS = {
    "group_by": (_query(BALANCE, group_by=[REGION_NAME]), [REGION_NAME, "v"], BY_HOME),
    "where": (
        _query(BALANCE, where=[{"field": REGION_NAME, "op": "=", "value": "North"}]),
        ["v"],
        NORTH_BY_HOME,
    ),
    "measure_filter": (
        _query("metric.bank.north_balance"),
        ["v"],
        NORTH_BY_HOME,
    ),
    "metric_predicate": (
        _query(
            BALANCE, metric_filters=[{"expression": _METRIC_PREDICATE, "op": "=", "value": True}]
        ),
        ["v"],
        "SELECT SUM(a.balance) FROM accounts a JOIN owners o USING (owner_id) "
        "WHERE o.home_region_id IN (SELECT o2.home_region_id FROM accounts a2 "
        "JOIN owners o2 USING (owner_id) GROUP BY 1 HAVING SUM(a2.balance) >= 500)",
    ),
    "time_role": (
        _query(LAUNCH_BALANCE, time={"temporal_role": "temporal_role.bank_region_launched"}),
        ["v"],
        f"SELECT SUM(balance) FROM ({HOME_ROWS}) GROUP BY region_name",
    ),
    "direct_key_read": (
        _query(BALANCE, group_by=[REGION_KEY]),
        [REGION_KEY, "v"],
        "SELECT o.home_region_id, SUM(a.balance) FROM accounts a JOIN owners o USING (owner_id) "
        "GROUP BY 1",
    ),
    "conversion": (_CONVERSION, None, None),
}
_PINS = [
    _pin("account", "region", HOME),
    _pin("invoice", "region", ["relationship.invoices_account", *HOME]),
]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_every_entry_point_refuses_unpinned_and_follows_the_pin(tmp_path, entry):
    """The bypass guard: no code path reaches the region by the branch route on its own."""
    query, columns, gold = ENTRY_POINTS[entry]
    _refusal(_write_package(tmp_path / "refused"), query)
    runtime = Runtime.from_path(str(_write_package(tmp_path / "pinned", pins=_PINS)))
    if gold is not None:
        assert _rows(runtime, query, columns) == _gold(gold)
    sql = runtime.compile(query)["explain"]["rendered_sql"]
    assert "owners" in sql  # the home route, through the owner
    assert "branch_region_id" not in sql


def test_the_direct_key_read_and_discovery_follow_the_resolver(tmp_path):
    refused = load_package_config(str(_write_package(tmp_path / "refused")))
    assert _direct_entity_key_source_expr(ACCOUNT, REGION, "region_id", refused) is None
    availability = _path_availability(refused, ACCOUNT, REGION)
    assert (availability["available"], availability["error_code"]) == (False, "AMBIGUOUS_PATH")
    branch = load_package_config(
        str(_write_package(tmp_path / "branch", pins=[_pin("account", "region", BRANCH)]))
    )
    expr = _direct_entity_key_source_expr(ACCOUNT, REGION, "region_id", branch)
    assert expr is not None and expr.parts[-1] == "branch_region_id"
    assert _path_availability(branch, ACCOUNT, REGION)["path"] == BRANCH


SHIPPED_PACKAGES = [
    "configs/semantic_rails/jaffle_shop",
    "configs/semantic_rails/tpch_sf1_showcase",
    "comparisons/semantic_layers/semantic_rails/package",
    "tests/integration/correctness/shop",
    "configs/examples/semantic_rails_package_starter.yml",
]


@pytest.mark.parametrize("package", SHIPPED_PACKAGES)
def test_no_shipped_pair_is_refused_for_routes_of_different_lengths(package):
    """A relationship edit that adds a second role must pin it: every pair resolves, or is an
    equal-length tie refused as before."""
    config = load_package_config(str(ROOT / package))
    for start in config.entities:
        for target in config.entities:
            if start.id == target.id:
                continue
            try:
                resolve_path(config, start=start.id, target=target.id)
            except SemanticLayerError as exc:
                if exc.code == "PATH_NOT_FOUND":
                    continue
                assert exc.code == "AMBIGUOUS_PATH"
                lengths = {len(path) for path in exc.details["candidates"]}
                assert len(lengths) == 1, (start.id, target.id, exc.details["candidates"])
