"""A route is chosen only by a recorded decision or the start's own key.

For a start S and a target T, ``fanout.resolve_path`` takes, in order: a
``graph.path_preferences`` row for exactly (S, T); S's one direct key to T; the rows of
pairs a route walks through, which drop every route that walks such a pair by another part
than the row records; the only remaining route. Two or more remaining routes are refused as
AMBIGUOUS_PATH whatever their lengths, and routes the rows all drop as PATH_NOT_FOUND
(``excluded_by_decision``). Rows must agree with each other when the package loads.

Fixture: a lender on DuckDB. An account reaches its district two ways, through its branch or
through its owner (a client), and the two disagree on the data, so an answer shows which
route it took. Loans sit one level below the account, cards and transactions two levels
below (through a disposition), and the region lies beyond the district. A variant gives the
loan its own district key, which also disagrees with both account routes.

    account -> branch -> district -> region     the branch route
    account -> client -> district               the owner route
    loan -> account, disposition -> account, card -> disposition, transaction -> disposition
    loan -> district                            the loan's own district (variant)
"""

from __future__ import annotations

import contextlib
import textwrap
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails import fanout as fanout_module
from semantic_rails.compiler import _entity_determines
from semantic_rails.compiler_parts.grain_recovery import _chosen_path
from semantic_rails.compiler_parts.indexes import get_package_analysis
from semantic_rails.config import load_package_config
from semantic_rails.diagnostics import exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import eligible_path_targets, resolve_path, resolve_route
from semantic_rails.metadata_parts.path_coverage import _path_availability
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PathPreferenceConfig

SEED_SQL = """
CREATE TABLE regions (region_id INTEGER, region_name VARCHAR);
INSERT INTO regions VALUES (1, 'North'), (2, 'South');
CREATE TABLE districts (
  district_id INTEGER, district_name VARCHAR, region_id INTEGER, opened_at TIMESTAMP
);
INSERT INTO districts VALUES
  (10, 'Alpha', 1, '2020-01-01'), (11, 'Beta', 1, '2021-01-01'),
  (12, 'Gamma', 2, '2022-01-01'), (13, 'Delta', 2, '2023-01-01');
CREATE TABLE branches (branch_id INTEGER, district_id INTEGER, branch_name VARCHAR);
INSERT INTO branches VALUES (100, 10, 'Main'), (101, 12, 'Harbor');
CREATE TABLE clients (client_id INTEGER, district_id INTEGER);
INSERT INTO clients VALUES (200, 11), (201, 13), (202, 10);
CREATE TABLE accounts (
  account_id INTEGER, branch_id INTEGER, client_id INTEGER, district_id INTEGER,
  account_kind VARCHAR, balance INTEGER
);
INSERT INTO accounts VALUES
  (1, 100, 200, 12, 'checking', 100), (2, 100, 201, 11, 'checking', 200),
  (3, 101, 202, 13, 'savings', 400), (4, 101, 201, 10, 'savings', 800);
CREATE TABLE loans (
  loan_id INTEGER, account_id INTEGER, district_id INTEGER, payout_district_id INTEGER,
  amount INTEGER, granted_at TIMESTAMP
);
INSERT INTO loans VALUES
  (1000, 1, 12, 10, 10, '2024-01-05'), (1001, 2, 11, 12, 20, '2024-01-20'),
  (1002, 3, 13, 11, 40, '2024-02-03'), (1003, 4, 10, 13, 80, '2024-02-25'),
  (1004, 1, 13, 12, 160, '2024-03-10');
CREATE TABLE dispositions (disposition_id INTEGER, account_id INTEGER);
INSERT INTO dispositions VALUES (500, 1), (501, 2), (502, 3), (503, 4), (504, 1);
CREATE TABLE cards (card_id INTEGER, disposition_id INTEGER);
INSERT INTO cards VALUES (600, 500), (601, 501), (602, 502), (603, 504), (604, 503);
CREATE TABLE transactions (transaction_id INTEGER, disposition_id INTEGER, amount INTEGER);
INSERT INTO transactions VALUES
  (700, 500, 1), (701, 501, 2), (702, 502, 4), (703, 503, 8), (704, 504, 16), (705, 502, 32);
"""


def _entity(key: str) -> str:
    return f"entity.lender_{key}"


def _rel(name: str) -> str:
    return f"relationship.{name}"


ACCOUNT, LOAN, CARD, DISTRICT = map(_entity, ("account", "loan", "card", "district"))
REGION = _entity("region")
DISTRICT_NAME, DISTRICT_KEY = "dimension.lender_district_name", "dimension.lender_district_id"
REGION_NAME, ACCOUNT_KIND = "dimension.lender_region_name", "dimension.lender_account_kind"
BRANCH_NAME = "dimension.lender_branch_name"
BALANCE, LOAN_AMOUNT = "measure.lender.balance", "measure.lender.amount"
CARD_COUNT, TRANSACTION_AMOUNT = "measure.lender.card_count", "measure.lender.transaction_amount"
DISTRICT_COUNT, LOAN_COUNT = "measure.lender.district_count", "measure.lender.loan_count"

OWNER = [_rel("accounts_client"), _rel("clients_district")]
BRANCH = [_rel("accounts_branch"), _rel("branches_district")]
OWN_KEY = [_rel("loans_district")]
LOAN_ACCOUNT = [_rel("loans_account")]

_RELATIONSHIPS = {
    "districts_region": ("district", "region", "region_id", "region_id"),
    "branches_district": ("branch", "district", "district_id", "district_id"),
    "clients_district": ("client", "district", "district_id", "district_id"),
    "accounts_branch": ("account", "branch", "branch_id", "branch_id"),
    "accounts_client": ("account", "client", "client_id", "client_id"),
    "accounts_district": ("account", "district", "district_id", "district_id"),
    "loans_account": ("loan", "account", "account_id", "account_id"),
    "loans_district": ("loan", "district", "district_id", "district_id"),
    "loans_payout_district": ("loan", "district", "payout_district_id", "district_id"),
    "dispositions_account": ("disposition", "account", "account_id", "account_id"),
    "cards_disposition": ("card", "disposition", "disposition_id", "disposition_id"),
    "transactions_disposition": (
        "transaction",
        "disposition",
        "disposition_id",
        "disposition_id",
    ),
}
LENDER = (
    "districts_region",
    "branches_district",
    "clients_district",
    "accounts_branch",
    "accounts_client",
    "loans_account",
    "dispositions_account",
    "cards_disposition",
    "transactions_disposition",
)
OWN_DISTRICT = (*LENDER, "loans_district")
# A distinct count of districts by an account dimension crosses these one-to-many hops.
_ROLLUP_SAFE_REVERSE = {
    "branches_district",
    "clients_district",
    "accounts_branch",
    "accounts_client",
}


def _row(start: str, target: str, path: list[str]) -> dict[str, Any]:
    return {"source_entity": start, "target_entity": target, "relationship_path": path}


ACCOUNT_OWNER_ROW = _row(ACCOUNT, DISTRICT, OWNER)


def _write_package(
    root: Path,
    *,
    relationships: tuple[str, ...] = LENDER,
    rows: list[dict[str, Any]] | None = None,
    forward_only: tuple[str, ...] = (),
    max_hops: int | None = None,
) -> Path:
    pkg = root / "lender"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    (pkg / "package.yml").write_text(
        textwrap.dedent(
            f"""
            schema_version: 1
            package:
              id: lender
              namespace: lender
              name: lender
              description: Two routes from an account to its district.
              warehouse: duckdb
              default_db: {(root / "lender.duckdb").as_posix()}
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
            "id": _rel(name),
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [via],
            "target": [key],
        }
        if name in _ROLLUP_SAFE_REVERSE:
            edges[name]["rollup_safe"] = {"reverse": ["count_distinct"]}
        if name in forward_only:
            edges[name]["allowed_directions"] = ["forward"]
    keys = ("region", "district", "branch", "client", "account", "loan", "disposition", "card")
    graph: dict[str, Any] = {
        "entities": {
            **{
                key: {"label": key.title(), "key": [f"{key}_id"], "model": f"{key}s"}
                for key in keys
            },
            "transaction": {
                "label": "Transaction",
                "key": ["transaction_id"],
                "model": "transactions",
            },
        },
        "relationships": edges,
    }
    if rows:
        graph["path_preferences"] = rows
    if max_hops is not None:
        graph["path_policy"] = {"max_hops": max_hops}
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}, sort_keys=False))

    def model(key: str, body: dict[str, Any], table: str = "") -> None:
        spec = {"id": f"{key}s", "relation": table or f"{key}s", "entities": {key: {}}, **body}
        (pkg / "models" / f"{key}s.yml").write_text(yaml.safe_dump({"model": spec}))

    count = {"kind": "entity_count", "accumulation": {"kind": "population"}, "value_type": "count"}
    flow = {"kind": "aggregate", "accumulation": {"kind": "flow"}, "value_type": "count"}
    model("region", {"dimensions": {"name": {"column": "region_name", "kind": "categorical"}}})
    model(
        "district",
        {
            "dimensions": {"name": {"column": "district_name", "kind": "categorical"}},
            "times": {
                "opened_at": {
                    "label": "Opened at",
                    "column": "opened_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "as": "temporal_role.lender_district_opened",
                }
            },
            "measures": {
                "district_count": {**count, "label": "Districts", "entity_key": "district_id"}
            },
        },
    )
    model(
        "branch",
        {"dimensions": {"name": {"column": "branch_name", "kind": "categorical"}}},
        table="branches",
    )
    model("client", {})
    model(
        "account",
        {
            "dimensions": {"kind": {"column": "account_kind", "kind": "categorical"}},
            "measures": {"balance": {**flow, "label": "Balance", "expr": "balance"}},
        },
    )
    model(
        "loan",
        {
            "times": {
                "granted_at": {
                    "label": "Granted at",
                    "column": "granted_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "default": True,
                }
            },
            "measures": {
                "amount": {**flow, "label": "Loan amount", "expr": "amount"},
                "loan_count": {**count, "label": "Loans", "entity_key": "loan_id"},
                "amount_by_district_opening": {
                    **flow,
                    "label": "Loan amount by district opening",
                    "expr": "amount",
                    "times": ["temporal_role.lender_district_opened"],
                },
            },
        },
    )
    model("disposition", {})
    model(
        "card",
        {"measures": {"card_count": {**count, "label": "Cards", "entity_key": "card_id"}}},
    )
    model(
        "transaction",
        {"measures": {"transaction_amount": {**flow, "label": "Amount", "expr": "amount"}}},
    )
    (pkg / "metrics.yml").write_text(
        yaml.safe_dump(
            {
                "metrics": {
                    "lender.alpha_loans": {
                        "as": "metric.lender.alpha_loans",
                        "label": "Alpha loans",
                        "kind": "aggregate",
                        "value_type": "count",
                        "expression": {
                            "kind": "aggregate",
                            "measure": "amount",
                            "aggregation": "sum",
                            "filter": {
                                "all": [{"field": DISTRICT_NAME, "op": "=", "value": "Alpha"}]
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


def _gold(sql: str, seed: str = SEED_SQL) -> list[tuple]:
    con = duckdb.connect(":memory:")
    con.execute(seed)
    return sorted(tuple(row) for row in con.execute(sql).fetchall())


def _rows(out: dict[str, Any], columns: list[str]) -> list[tuple]:
    return sorted(tuple(row[column] for column in columns) for row in out["rows"])


def _query(select: str, **parts: Any) -> dict[str, Any]:
    kind = "metric" if select.startswith("metric.") else "measure"
    return {"version": 1, "select": [{"expression": {kind: select}, "as": "v"}], **parts}


def _notes(out: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (w["object_ids"][0], w["object_ids"][1]): w
        for w in out["warnings"]
        if w["code"] in {"ROUTE_COLOCATED_KEY", "ROUTE_RECORDED"}
    }


def _refusal(pkg: Path, query: dict[str, Any], code: str = "AMBIGUOUS_PATH") -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(pkg)).query(query)
    assert exc_info.value.code == code, exc_info.value
    return exc_info.value


def _routes(err: SemanticLayerError) -> list[list[str]]:
    """The routes an AMBIGUOUS_PATH refusal asks between, one per clarification option."""
    return [option["relationship_path"] for option in err.details["clarification"]["options"]]


# Each start's rows with the account they belong to, as (account_id, v).
START_ROWS = {
    "account": "SELECT account_id, balance AS v FROM accounts",
    "loan": "SELECT account_id, amount AS v FROM loans",
    "card": "SELECT dp.account_id, 1 AS v FROM cards c JOIN dispositions dp USING (disposition_id)",
    "transaction": (
        "SELECT dp.account_id, t.amount AS v FROM transactions t "
        "JOIN dispositions dp USING (disposition_id)"
    ),
}
# Each account's district by a route.
ACCOUNT_DISTRICT = {
    "owner": "JOIN clients c ON c.client_id = a.client_id JOIN districts d ON d.district_id = c.district_id",
    "branch": "JOIN branches b ON b.branch_id = a.branch_id JOIN districts d ON d.district_id = b.district_id",
}


def _by_account_route(start: str, route: str, *, region: bool = False) -> str:
    group = "r.region_name" if region else "d.district_name"
    beyond = " JOIN regions r ON r.region_id = d.region_id" if region else ""
    return (
        f"SELECT {group}, SUM(x.v) FROM ({START_ROWS[start]}) x JOIN accounts a USING (account_id) "
        f"{ACCOUNT_DISTRICT[route]}{beyond} GROUP BY 1"
    )


R1_CASES = {
    "accounts_by_district": (BALANCE, DISTRICT_NAME, "account", False),
    "loans_by_district": (LOAN_AMOUNT, DISTRICT_NAME, "loan", False),
    "cards_by_district": (CARD_COUNT, DISTRICT_NAME, "card", False),
    "transactions_by_district": (TRANSACTION_AMOUNT, DISTRICT_NAME, "transaction", False),
    "accounts_by_region": (BALANCE, REGION_NAME, "account", True),
    "loans_by_region": (LOAN_AMOUNT, REGION_NAME, "loan", True),
}


@pytest.mark.parametrize("case", R1_CASES)
def test_a_decision_holds_wherever_a_route_walks_its_pair(tmp_path, case):
    """With (account, district) recorded as the owner route, accounts, the loans one level
    below, the cards and transactions two levels below, and the region beyond the district
    all answer with the owner route's gold. Without the row each is refused."""
    measure, dimension, start, region = R1_CASES[case]
    query = _query(measure, group_by=[dimension])
    err = _refusal(_write_package(tmp_path / "none"), query)
    assert err.details["reason"] == "route_decision_required"
    out = Runtime.from_path(str(_write_package(tmp_path / "row", rows=[ACCOUNT_OWNER_ROW]))).query(
        query
    )
    gold = _gold(_by_account_route(start, "owner", region=region))
    assert _rows(out, [dimension, "v"]) == gold
    assert gold != _gold(_by_account_route(start, "branch", region=region))
    (note,) = _notes(out).values()
    assert note["code"] == "ROUTE_RECORDED"
    if start != "account" or region:
        # Inherited: the note names the row it follows.
        assert note["details"]["rows"] == [{"source_entity": ACCOUNT, "target_entity": DISTRICT}]
        assert note["message"].endswith("(recorded for Account → District)")


def test_hop_profile_reports_how_each_route_was_chosen(tmp_path):
    pkg = _write_package(tmp_path / "row", rows=[ACCOUNT_OWNER_ROW])
    runtime = Runtime.from_path(str(pkg))

    def basis(query: dict[str, Any]) -> dict[str, str]:
        targets = runtime.compile(query)["hop_profile"]["targets"]
        return {target: row["route_basis"] for target, row in targets.items()}

    assert basis(_query(BALANCE, group_by=[DISTRICT_NAME])) == {DISTRICT: "decided"}
    assert basis(_query(LOAN_AMOUNT, group_by=[DISTRICT_NAME])) == {DISTRICT: "inherited"}
    assert basis(_query(CARD_COUNT, group_by=[ACCOUNT_KIND])) == {ACCOUNT: "only_route"}
    own = Runtime.from_path(str(_write_package(tmp_path / "own", relationships=OWN_DISTRICT)))
    targets = own.compile(_query(LOAN_AMOUNT, group_by=[DISTRICT_NAME]))["hop_profile"]["targets"]
    assert targets[DISTRICT]["route_basis"] == "colocated_key"


REVERSE_GOLD = {
    route: (
        f"SELECT a.account_kind, COUNT(DISTINCT d.district_id) FROM accounts a "
        f"{ACCOUNT_DISTRICT[route]} GROUP BY 1"
    )
    for route in ACCOUNT_DISTRICT
}


def test_a_route_walked_the_other_way_follows_the_row_reversed(tmp_path):
    """District to account walks the (account, district) pair backwards: with the owner route
    recorded, the districts of each kind of account are their owners' districts."""
    query = _query(DISTRICT_COUNT, group_by=[ACCOUNT_KIND])
    _refusal(_write_package(tmp_path / "none"), query)
    pkg = _write_package(tmp_path / "row", rows=[ACCOUNT_OWNER_ROW])
    resolution = resolve_route(load_package_config(str(pkg)), start=DISTRICT, target=ACCOUNT)
    assert (resolution.basis, list(resolution.routes[0])) == ("inherited", OWNER[::-1])
    out = Runtime.from_path(str(pkg)).query(query)
    assert _rows(out, [ACCOUNT_KIND, "v"]) == _gold(REVERSE_GOLD["owner"])
    assert _gold(REVERSE_GOLD["owner"]) != _gold(REVERSE_GOLD["branch"])


def test_a_row_with_a_one_way_hop_does_not_decide_the_reverse_walk(tmp_path):
    """The client's district can't be walked back to its clients, so the owner route has no
    reverse: the row says nothing about district to account, whose only route is the branch's,
    and the row still decides account to district."""
    pkg = _write_package(tmp_path, rows=[ACCOUNT_OWNER_ROW], forward_only=("clients_district",))
    config = load_package_config(str(pkg))
    resolution = resolve_route(config, start=DISTRICT, target=ACCOUNT)
    assert (resolution.basis, list(resolution.routes[0])) == ("only_route", BRANCH[::-1])
    runtime = Runtime.from_path(str(pkg))
    out = runtime.query(_query(DISTRICT_COUNT, group_by=[ACCOUNT_KIND]))
    assert _rows(out, [ACCOUNT_KIND, "v"]) == _gold(REVERSE_GOLD["branch"])
    assert resolve_path(config, start=LOAN, target=DISTRICT)[0] == [*LOAN_ACCOUNT, *OWNER]


@pytest.mark.parametrize("weight", [None, 10], ids=["no-weight", "weight"])
def test_equal_length_routes_refuse_whatever_their_weights_and_a_row_decides(tmp_path, weight):
    """Branch and owner routes are both two hops. A relationship weight never decides: the
    package that sets one does not load; without it the pair is refused; a row decides."""
    query = _query(BALANCE, group_by=[DISTRICT_NAME])
    pkg = _write_package(tmp_path / "none")
    if weight is not None:
        graph = yaml.safe_load((pkg / "graph.yml").read_text())
        graph["graph"]["relationships"]["accounts_branch"]["path_preference"] = weight
        (pkg / "graph.yml").write_text(yaml.safe_dump(graph, sort_keys=False))
        with pytest.raises(SemanticLayerError) as exc_info:
            load_package_config(str(pkg))
        assert exc_info.value.code == "INVALID_CONFIG"
        assert "graph.path_preferences" in str(exc_info.value)
        return
    err = _refusal(pkg, query)
    assert sorted(map(len, _routes(err))) == [2, 2]
    for route in ("branch", "owner"):
        path = BRANCH if route == "branch" else OWNER
        pinned = _write_package(tmp_path / route, rows=[_row(ACCOUNT, DISTRICT, path)])
        out = Runtime.from_path(str(pinned)).query(query)
        assert _rows(out, [DISTRICT_NAME, "v"]) == _gold(_by_account_route("account", route))


# The two returned orders belong to two customers, but both were made in sessions of a third.
SHOP_SEED = """
CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR);
INSERT INTO customers VALUES (1, 'Ann'), (2, 'Bob'), (3, 'Cy');
CREATE TABLE sessions (session_id INTEGER, customer_id INTEGER);
INSERT INTO sessions VALUES (10, 3), (11, 3), (12, 1);
CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, session_id INTEGER, status VARCHAR);
INSERT INTO orders VALUES (100, 1, 10, 'returned'), (101, 2, 11, 'returned'), (102, 3, 12, 'kept');
"""
DIRECT_ORDERS = [_rel("orders_customer")]
SESSION_ORDERS = [_rel("sessions_customer"), _rel("orders_session")]


def _write_shop(root: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    pkg = root / "shop"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(SHOP_SEED)
    package = yaml.safe_load((_write_package(root / "base") / "package.yml").read_text())
    package["package"].update(
        id="shop", namespace="shop", name="shop", default_db=(root / "shop.duckdb").as_posix()
    )
    (pkg / "package.yml").write_text(yaml.safe_dump(package))
    edges = {
        name: {
            "id": _rel(name),
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [via],
            "target": [f"{target}_id"],
        }
        for name, source, target, via in (
            ("orders_customer", "order", "customer", "customer_id"),
            ("sessions_customer", "session", "customer", "customer_id"),
            ("orders_session", "order", "session", "session_id"),
        )
    }
    graph: dict[str, Any] = {
        "entities": {
            key: {"label": key.title(), "key": [f"{key}_id"], "model": f"{key}s"}
            for key in ("customer", "session", "order")
        },
        "relationships": edges,
    }
    if rows:
        graph["path_preferences"] = rows
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}, sort_keys=False))
    specs = {
        "customer": {
            "measures": {
                "customer_count": {
                    "kind": "entity_count",
                    "accumulation": {"kind": "population"},
                    "value_type": "count",
                    "label": "Customers",
                    "entity_key": "customer_id",
                }
            }
        },
        "session": {},
        "order": {"dimensions": {"status": {"kind": "categorical"}}},
    }
    for key, body in specs.items():
        spec = {"id": f"{key}s", "relation": f"{key}s", "entities": {key: {}}, **body}
        (pkg / "models" / f"{key}s.yml").write_text(yaml.safe_dump({"model": spec}))
    return pkg


def test_fan_out_only_alternatives_refuse_and_a_row_decides(tmp_path):
    """Customers filtered by an order status: the orders a customer placed (one hop) or the
    orders made in the customer's sessions (two hops). Both fan out; neither wins by length."""
    customer, order = "entity.shop_customer", "entity.shop_order"
    query = _query(
        "measure.shop.customer_count",
        where=[{"field": "dimension.shop_order_status", "op": "=", "value": "returned"}],
    )
    err = _refusal(_write_shop(tmp_path / "none"), query)
    assert sorted(_routes(err), key=len) == [DIRECT_ORDERS, SESSION_ORDERS]
    golds = {
        tuple(DIRECT_ORDERS): (
            "SELECT COUNT(DISTINCT customer_id) FROM orders WHERE status = 'returned'"
        ),
        tuple(SESSION_ORDERS): (
            "SELECT COUNT(DISTINCT s.customer_id) FROM sessions s "
            "JOIN orders o USING (session_id) WHERE o.status = 'returned'"
        ),
    }
    for index, (path, sql) in enumerate(golds.items()):
        pkg = _write_shop(tmp_path / f"row{index}", rows=[_row(customer, order, list(path))])
        out = Runtime.from_path(str(pkg)).query(query)
        assert _rows(out, ["v"]) == _gold(sql, SHOP_SEED)
    # The data tells the routes apart: two customers placed them, one held the sessions.
    assert [_gold(sql, SHOP_SEED) for sql in golds.values()] == [[(2,)], [(1,)]]


# A chain's orders: each belongs to a customer, is sold at a store, and holds its own district,
# which on this data is never its store's district.
CHAIN_SEED = """
CREATE TABLE customers (customer_id INTEGER);
INSERT INTO customers VALUES (1), (2), (3);
CREATE TABLE districts (district_id INTEGER, district_name VARCHAR);
INSERT INTO districts VALUES (100, 'North'), (101, 'South'), (102, 'East');
CREATE TABLE stores (store_id INTEGER, district_id INTEGER);
INSERT INTO stores VALUES (10, 100), (11, 101);
CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, store_id INTEGER, district_id INTEGER);
INSERT INTO orders VALUES (1000, 1, 10, 102), (1001, 2, 11, 100), (1002, 3, 10, 101), (1003, 1, 11, 101);
"""
CHAIN_CUSTOMER, CHAIN_ORDER, CHAIN_DISTRICT = (
    f"entity.chain_{key}" for key in ("customer", "order", "district")
)
CHAIN_RELATIONSHIPS = {
    "orders_customer": ("order", "customer"),
    "orders_store": ("order", "store"),
    "stores_district": ("store", "district"),
    "orders_district": ("order", "district"),
}
BY_STORE = [_rel("orders_customer"), _rel("orders_store"), _rel("stores_district")]
ORDER_OWN_DISTRICT = [_rel("orders_district")]


def _write_chain(root: Path) -> Path:
    """Customers' districts recorded as the districts of the stores they ordered at."""
    pkg = root / "chain"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(CHAIN_SEED)
    package = yaml.safe_load((_write_package(root / "base") / "package.yml").read_text())
    package["package"].update(
        id="chain", namespace="chain", name="chain", default_db=(root / "chain.duckdb").as_posix()
    )
    (pkg / "package.yml").write_text(yaml.safe_dump(package))
    edges = {
        name: {
            "id": _rel(name),
            "entities": [source, target],
            "cardinality": "many_to_one",
            "via": [f"{target}_id"],
            "target": [f"{target}_id"],
        }
        for name, (source, target) in CHAIN_RELATIONSHIPS.items()
    }
    # A count of customers grouped across their orders is read from the orders' rows.
    edges["orders_customer"]["rollup_safe"] = {"reverse": ["count_distinct"]}
    graph = {
        "entities": {
            key: {"label": key.title(), "key": [f"{key}_id"], "model": f"{key}s"}
            for key in ("customer", "order", "store", "district")
        },
        "relationships": edges,
        "path_preferences": [_row(CHAIN_CUSTOMER, CHAIN_DISTRICT, BY_STORE)],
    }
    (pkg / "graph.yml").write_text(yaml.safe_dump({"graph": graph}, sort_keys=False))
    customers = {
        "kind": "entity_count",
        "accumulation": {"kind": "population"},
        "value_type": "count",
        "label": "Customers",
        "entity_key": "customer_id",
    }
    specs = {
        "customer": {"measures": {"customer_count": customers}},
        "order": {},
        "store": {},
        "district": {"dimensions": {"name": {"column": "district_name", "kind": "categorical"}}},
    }
    for key, body in specs.items():
        spec = {"id": f"{key}s", "relation": f"{key}s", "entities": {key: {}}, **body}
        (pkg / "models" / f"{key}s.yml").write_text(yaml.safe_dump({"model": spec}))
    return pkg


def _chain_query(column: str) -> dict[str, Any]:
    return _query("measure.chain.customer_count", group_by=[f"dimension.chain_district_{column}"])


def _chain_gold(column: str, route: str) -> list[tuple]:
    join = {
        "store": "JOIN stores s USING (store_id) JOIN districts d ON d.district_id = s.district_id",
        "own": "JOIN districts d ON d.district_id = o.district_id",
    }[route]
    return _gold(
        f"SELECT d.district_{column}, COUNT(DISTINCT o.customer_id) FROM orders o {join} GROUP BY 1",
        CHAIN_SEED,
    )


def _sql_joins(sql: str, route: list[str]) -> bool:
    """Whether ``sql`` joins every relationship of ``route`` on that relationship's columns."""
    for rel_id in route:
        source, target = CHAIN_RELATIONSHIPS[rel_id.removeprefix("relationship.")]
        sides = (f"{source}s.{target}_id", f"{target}s.{target}_id")
        if f"{sides[0]} = {sides[1]}" not in sql and f"{sides[1]} = {sides[0]}" not in sql:
            return False
    return True


def test_a_rewrite_notes_only_the_route_its_sql_reads(tmp_path):
    """Customers by district are counted from their orders' rows, along the recorded store
    route. The order holds its own district too, and its own pair (order, district) resolves
    to it; resolved first in the same process, that route still never shows up as the query's:
    every note names joins the SQL makes, and the answer is the store route's gold."""
    runtime = Runtime.from_path(str(_write_chain(tmp_path)))
    # The runtime's own configuration (``runtime.config`` is a copy), so its queries see this.
    resolution = resolve_route(runtime._config, start=CHAIN_ORDER, target=CHAIN_DISTRICT)
    assert (resolution.basis, list(resolution.routes[0])) == ("colocated_key", ORDER_OWN_DISTRICT)
    query = _chain_query("name")
    out = runtime.query(query)
    sql = runtime.compile(query)["explain"]["rendered_sql"]
    assert "FROM orders" in sql and "orders.district_id" not in sql
    assert _rows(out, ["dimension.chain_district_name", "v"]) == _chain_gold("name", "store")
    assert _chain_gold("name", "store") != _chain_gold("name", "own")
    notes = _notes(out)
    assert list(notes) == [(CHAIN_CUSTOMER, CHAIN_DISTRICT)]
    assert notes[(CHAIN_CUSTOMER, CHAIN_DISTRICT)]["details"] == {"route": BY_STORE}
    assert all(_sql_joins(sql, note["details"]["route"]) for note in notes.values())


def test_a_querys_route_notes_do_not_depend_on_what_ran_before(tmp_path):
    """The same query's notes on a fresh runtime, and on one where every pair has been resolved
    and other queries have run."""
    pkg = _write_chain(tmp_path)
    fresh = _notes(Runtime.from_path(str(pkg)).query(_chain_query("name")))
    runtime = Runtime.from_path(str(pkg))
    entities = [entity.id for entity in runtime._config.entities]
    for start in entities:
        for target in entities:
            if start != target:
                with contextlib.suppress(SemanticLayerError):
                    resolve_route(runtime._config, start=start, target=target)
    runtime.query(_chain_query("id"))
    assert _notes(runtime.query(_chain_query("name"))) == fresh


def test_the_anchors_own_key_never_answers_for_the_roots_route(tmp_path):
    """Customers by district key: the orders' rows hold a district key of their own, but the
    customers' recorded route reaches the district through the store. The rewrite does not read
    the order's column: the query answers with the store route's gold, and no note names the
    order's own key."""
    runtime = Runtime.from_path(str(_write_chain(tmp_path)))
    query = _chain_query("id")
    out = runtime.query(query)
    sql = runtime.compile(query)["explain"]["rendered_sql"]
    assert "orders.district_id" not in sql
    assert _rows(out, ["dimension.chain_district_id", "v"]) == _chain_gold("id", "store")
    assert _chain_gold("id", "store") != _chain_gold("id", "own")
    assert [note["details"]["route"] for note in _notes(out).values()] == [BY_STORE]


OWN_KEY_GOLD = (
    "SELECT d.district_name, SUM(l.amount) FROM loans l "
    "JOIN districts d ON d.district_id = l.district_id GROUP BY 1"
)


def test_the_starts_own_key_answers_and_lists_each_other_route_with_its_row(tmp_path):
    """The loan holds its own district. With no rows, and even with the account's district
    recorded as the owner route, "loan amount by district" reads the loan's own district and
    says so; the other routes come with the row that would make each the default. A card,
    with no district of its own, follows the account row."""
    query = _query(LOAN_AMOUNT, group_by=[DISTRICT_NAME])
    account_routes = [[*LOAN_ACCOUNT, *BRANCH], [*LOAN_ACCOUNT, *OWNER]]
    for name, rows, details in (
        (
            "none",
            None,
            {"alternatives": [_row(LOAN, DISTRICT, path) for path in account_routes]},
        ),
        # A row for the branch route would disagree with the account row, so only the
        # owner route is offered, and the branch route names the row it disagrees with.
        (
            "row",
            [ACCOUNT_OWNER_ROW],
            {
                "alternatives": [_row(LOAN, DISTRICT, account_routes[1])],
                "conflicts_with": [
                    {"relationship_path": account_routes[0], "rows": [ACCOUNT_OWNER_ROW]}
                ],
            },
        ),
    ):
        pkg = _write_package(tmp_path / name, relationships=OWN_DISTRICT, rows=rows)
        out = Runtime.from_path(str(pkg)).query(query)
        assert _rows(out, [DISTRICT_NAME, "v"]) == _gold(OWN_KEY_GOLD)
        note = _notes(out)[(LOAN, DISTRICT)]
        assert (note["code"], note["severity"]) == ("ROUTE_COLOCATED_KEY", "info")
        assert note["details"] == {"route": OWN_KEY, **details}
    assert _gold(OWN_KEY_GOLD) != _gold(_by_account_route("loan", "owner"))
    cards = Runtime.from_path(str(pkg)).query(_query(CARD_COUNT, group_by=[DISTRICT_NAME]))
    assert _rows(cards, [DISTRICT_NAME, "v"]) == _gold(_by_account_route("card", "owner"))


def test_a_row_for_the_exact_pair_beats_the_starts_own_key(tmp_path):
    loan_owner = [*LOAN_ACCOUNT, *OWNER]
    pkg = _write_package(
        tmp_path, relationships=OWN_DISTRICT, rows=[_row(LOAN, DISTRICT, loan_owner)]
    )
    out = Runtime.from_path(str(pkg)).query(_query(LOAN_AMOUNT, group_by=[DISTRICT_NAME]))
    assert _rows(out, [DISTRICT_NAME, "v"]) == _gold(_by_account_route("loan", "owner"))
    note = _notes(out)[(LOAN, DISTRICT)]
    assert (note["code"], note["details"]) == ("ROUTE_RECORDED", {"route": loan_owner})


def test_two_own_keys_are_not_one_and_an_inherited_row_decides(tmp_path):
    """A loan's district and its payout district are two keys to one entity: refused. The
    row for district -> loan, walked back, names the loan's district."""
    two_keys = (*OWN_DISTRICT, "loans_payout_district")
    query = _query(LOAN_AMOUNT, group_by=[DISTRICT_NAME])
    err = _refusal(_write_package(tmp_path / "none", relationships=two_keys), query)
    assert [OWN_KEY, [_rel("loans_payout_district")]] == _routes(err)[:2]
    pkg = _write_package(
        tmp_path / "row", relationships=two_keys, rows=[_row(DISTRICT, LOAN, OWN_KEY)]
    )
    resolution = resolve_route(load_package_config(str(pkg)), start=LOAN, target=DISTRICT)
    assert (resolution.basis, list(resolution.routes[0])) == ("inherited", OWN_KEY)
    out = Runtime.from_path(str(pkg)).query(query)
    assert _rows(out, [DISTRICT_NAME, "v"]) == _gold(OWN_KEY_GOLD)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [ACCOUNT_OWNER_ROW, _row(LOAN, DISTRICT, [*LOAN_ACCOUNT, *BRANCH])],
            id="a-longer-row-walks-the-pair-differently",
        ),
        pytest.param(
            [ACCOUNT_OWNER_ROW, _row(DISTRICT, ACCOUNT, BRANCH[::-1])],
            id="the-reverse-pair-records-another-route",
        ),
        pytest.param([ACCOUNT_OWNER_ROW, _row(ACCOUNT, DISTRICT, BRANCH)], id="one-pair-twice"),
    ],
)
def test_rows_that_disagree_are_refused_at_load_naming_both(tmp_path, rows):
    with pytest.raises(SemanticLayerError) as exc_info:
        load_package_config(str(_write_package(tmp_path, rows=rows)))
    err = exc_info.value
    assert err.code == "INVALID_CONFIG"
    assert sorted(map(str, err.details["rows"])) == sorted(map(str, rows))


def test_rows_that_agree_load_and_a_single_route_pair_may_be_recorded(tmp_path):
    rows = [
        ACCOUNT_OWNER_ROW,
        _row(LOAN, DISTRICT, [*LOAN_ACCOUNT, *OWNER]),
        _row(DISTRICT, REGION, [_rel("districts_region")]),
    ]
    config = load_package_config(str(_write_package(tmp_path, rows=rows)))
    assert resolve_route(config, start=DISTRICT, target=REGION).basis == "decided"


def test_rows_built_in_code_must_agree_too(tmp_path):
    """A configuration built in code never meets the YAML loader, but the package analysis runs
    the same check: two rows that disagree are refused when the query binds, before any SQL
    runs, naming both."""
    pkg = _write_package(tmp_path, rows=[ACCOUNT_OWNER_ROW])
    config = load_package_config(str(pkg))
    loan_branch = PathPreferenceConfig(LOAN, DISTRICT, [*LOAN_ACCOUNT, *BRANCH])
    built = replace(config, path_preferences=[*config.path_preferences, loan_branch])
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_config(built, source_path=str(pkg)).query(
            _query(LOAN_AMOUNT, group_by=[DISTRICT_NAME])
        )
    assert exc_info.value.code == "INVALID_CONFIG"
    assert exc_info.value.details["rows"] == [
        ACCOUNT_OWNER_ROW,
        _row(LOAN, DISTRICT, loan_branch.relationship_path),
    ]


LOAN_BRANCH, LOAN_OWNER = [*LOAN_ACCOUNT, *BRANCH], [*LOAN_ACCOUNT, *OWNER]
LOAN_REGION_BY_OWN_KEY = _row(LOAN, REGION, [*OWN_KEY, _rel("districts_region")])
LOAN_REGION_BY_OWNER = _row(LOAN, REGION, [*LOAN_OWNER, _rel("districts_region")])
# A package where the engine suggests rows for district by a measure: its relationships and
# rows, the measure, the routes whose rows it offers, and each route whose row would not load
# with the rows that row would disagree with. A loan amount is noted (the loan's own key) with
# the other routes as alternatives; an account balance is refused with a pin for each route.
SUGGESTIONS = {
    "own_key": (OWN_DISTRICT, [], LOAN_AMOUNT, [LOAN_BRANCH, LOAN_OWNER], []),
    "own_key_and_account_row": (
        OWN_DISTRICT,
        [ACCOUNT_OWNER_ROW],
        LOAN_AMOUNT,
        [LOAN_OWNER],
        [(LOAN_BRANCH, [ACCOUNT_OWNER_ROW])],
    ),
    # The loan's region recorded through its own district walks (loan, district) that way.
    "own_key_and_loan_region_row": (
        OWN_DISTRICT,
        [LOAN_REGION_BY_OWN_KEY],
        LOAN_AMOUNT,
        [],
        [(LOAN_BRANCH, [LOAN_REGION_BY_OWN_KEY]), (LOAN_OWNER, [LOAN_REGION_BY_OWN_KEY])],
    ),
    # The loan's region recorded through the account's owner walks (account, district) that way.
    "refusal_and_loan_region_row": (
        LENDER,
        [LOAN_REGION_BY_OWNER],
        BALANCE,
        [OWNER],
        [(BRANCH, [LOAN_REGION_BY_OWNER])],
    ),
}


@pytest.mark.parametrize("case", SUGGESTIONS)
def test_every_suggested_row_loads_and_answers_by_its_route(tmp_path, case):
    """Every row the engine suggests, an own-key note's alternative or a refusal's pin, loads
    beside the package's rows and answers with its route's gold. A route whose row would not
    load is not offered: it stays among the routes, in ``details.conflicts_with`` with the rows
    it disagrees with, and adding its row fails to load naming exactly those rows."""
    relationships, rows, measure, offered, conflicts = SUGGESTIONS[case]
    start = LOAN if measure == LOAN_AMOUNT else ACCOUNT
    query = _query(measure, group_by=[DISTRICT_NAME])
    pkg = _write_package(tmp_path / "package", relationships=relationships, rows=rows)
    if start == LOAN:
        details = _notes(Runtime.from_path(str(pkg)).query(query))[(LOAN, DISTRICT)]["details"]
        suggested, routes = details["alternatives"], [LOAN_BRANCH, LOAN_OWNER]
    else:
        options = _refusal(pkg, query).details["clarification"]["options"]
        suggested = [
            _row(start, DISTRICT, option["relationship_path"])
            for option in options
            if "conflicts_with" not in option
        ]
        routes = [option["relationship_path"] for option in options]
        details = {
            "conflicts_with": [
                {"relationship_path": option["relationship_path"], "rows": option["conflicts_with"]}
                for option in options
                if "conflicts_with" in option
            ]
        }
    assert suggested == [_row(start, DISTRICT, path) for path in offered]
    expected = [{"relationship_path": path, "rows": named} for path, named in conflicts]
    assert details.get("conflicts_with", []) == expected
    assert sorted(routes) == sorted([*offered, *(path for path, _ in conflicts)])
    for index, row in enumerate(suggested):
        added = _write_package(
            tmp_path / f"offered{index}", relationships=relationships, rows=[*rows, row]
        )
        out = Runtime.from_path(str(added)).query(query)
        route = "branch" if row["relationship_path"][-2:] == BRANCH else "owner"
        gold = _by_account_route("loan" if start == LOAN else "account", route)
        assert _rows(out, [DISTRICT_NAME, "v"]) == _gold(gold)
    for index, conflict in enumerate(expected):
        row = _row(start, DISTRICT, conflict["relationship_path"])
        added = _write_package(
            tmp_path / f"conflict{index}", relationships=relationships, rows=[*rows, row]
        )
        with pytest.raises(SemanticLayerError) as exc_info:
            load_package_config(str(added))
        assert exc_info.value.code == "INVALID_CONFIG"
        assert exc_info.value.details["rows"] == [*conflict["rows"], row]


def test_every_route_excluded_by_rows_is_refused_naming_the_rows(tmp_path):
    """The account holds its own district, but the owner route is recorded for the pair.
    Within two hops a loan reaches its district only through the account's own key, which the
    row rules out: refused, naming the row. Three hops admit the owner route."""
    relationships = (*LENDER, "accounts_district")
    query = _query(LOAN_AMOUNT, group_by=[DISTRICT_NAME])
    pkg = _write_package(
        tmp_path / "two", relationships=relationships, rows=[ACCOUNT_OWNER_ROW], max_hops=2
    )
    err = _refusal(pkg, query, code="PATH_NOT_FOUND")
    assert err.details["reason"] == "excluded_by_decision"
    assert err.details["rows"] == [ACCOUNT_OWNER_ROW]
    (hint,) = exception_issue(err, stage="compile")["recovery_hints"]
    assert (hint["kind"], hint["rows"]) == ("follow_recorded_routes", [ACCOUNT_OWNER_ROW])
    pkg = _write_package(
        tmp_path / "three", relationships=relationships, rows=[ACCOUNT_OWNER_ROW], max_hops=3
    )
    out = Runtime.from_path(str(pkg)).query(query)
    assert _rows(out, [DISTRICT_NAME, "v"]) == _gold(_by_account_route("loan", "owner"))


_METRIC_PREDICATE = {
    "kind": "metric_predicate",
    "entity": DISTRICT,
    "scope_mode": "entity_only",
    "input": {"measure": LOAN_AMOUNT},
    "op": ">=",
    "value": 100,
}
_CONVERSION = {
    "version": 2,
    "select": [
        {
            "as": "v",
            "expression": {
                "kind": "conversion",
                "entity": DISTRICT,
                "window": {"unit": "day", "value": 60},
                "matching_mode": "first_converted_after_base",
                "base": {"kind": "aggregate", "measure": LOAN_COUNT},
                "converted": {"kind": "aggregate", "measure": LOAN_COUNT},
            },
        }
    ],
}
# Every way a query reaches the district from a loan, with its columns; None where the answer
# isn't a plain table (checked by its SQL).
ENTRY_POINTS = {
    "group_by": (_query(LOAN_AMOUNT, group_by=[DISTRICT_NAME]), [DISTRICT_NAME, "v"]),
    "where": (
        _query(LOAN_AMOUNT, where=[{"field": DISTRICT_NAME, "op": "=", "value": "Alpha"}]),
        ["v"],
    ),
    "measure_filter": (_query("metric.lender.alpha_loans"), ["v"]),
    "metric_predicate": (
        _query(
            LOAN_AMOUNT,
            metric_filters=[{"expression": _METRIC_PREDICATE, "op": "=", "value": True}],
        ),
        ["v"],
    ),
    "time_role": (
        _query(
            "measure.lender.amount_by_district_opening",
            time={"temporal_role": "temporal_role.lender_district_opened"},
        ),
        ["v"],
    ),
    "direct_key_read": (_query(LOAN_AMOUNT, group_by=[DISTRICT_KEY]), [DISTRICT_KEY, "v"]),
    "conversion": (_CONVERSION, None),
}
LOAN_DISTRICT_ROWS = {
    "owner": (
        "SELECT d.district_id, d.district_name, d.opened_at, l.amount FROM loans l "
        f"JOIN accounts a USING (account_id) {ACCOUNT_DISTRICT['owner']}"
    ),
    "own": (
        "SELECT d.district_id, d.district_name, d.opened_at, l.amount FROM loans l "
        "JOIN districts d ON d.district_id = l.district_id"
    ),
}


def _entry_gold(entry: str, route: str) -> str:
    rows = LOAN_DISTRICT_ROWS[route]
    return {
        "group_by": f"SELECT district_name, SUM(amount) FROM ({rows}) GROUP BY 1",
        "where": f"SELECT SUM(amount) FROM ({rows}) WHERE district_name = 'Alpha'",
        "measure_filter": f"SELECT SUM(amount) FROM ({rows}) WHERE district_name = 'Alpha'",
        "metric_predicate": (
            f"SELECT SUM(amount) FROM ({rows}) WHERE district_id IN "
            f"(SELECT district_id FROM ({rows}) GROUP BY 1 HAVING SUM(amount) >= 100)"
        ),
        "time_role": f"SELECT SUM(amount) FROM ({rows}) GROUP BY opened_at",
        "direct_key_read": f"SELECT district_id, SUM(amount) FROM ({rows}) GROUP BY 1",
    }[entry]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_every_entry_point_follows_an_inherited_row(tmp_path, entry):
    """The bypass guard with an inherited row: unrecorded, every way a query reaches the
    district from a loan refuses; with (account, district) recorded as the owner route, each
    follows it and never reads the branch route."""
    query, columns = ENTRY_POINTS[entry]
    err = _refusal(_write_package(tmp_path / "none"), query)
    assert err.details["target"] == DISTRICT
    runtime = Runtime.from_path(str(_write_package(tmp_path / "row", rows=[ACCOUNT_OWNER_ROW])))
    if columns is not None:
        out = runtime.query(query)
        assert _rows(out, columns) == _gold(_entry_gold(entry, "owner"))
        assert _gold(_entry_gold(entry, "owner")) != _gold(_entry_gold(entry, "own"))
        assert _notes(out)[(LOAN, DISTRICT)]["details"]["route"] == [*LOAN_ACCOUNT, *OWNER]
    sql = runtime.compile(query)["explain"]["rendered_sql"]
    assert "clients" in sql and "branches" not in sql


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_every_entry_point_reads_the_starts_own_key_over_an_inherited_row(tmp_path, entry):
    """The bypass guard with a co-located key: the loan's own district answers every entry
    point, even with the account's district recorded, and each says so."""
    query, columns = ENTRY_POINTS[entry]
    pkg = _write_package(tmp_path, relationships=OWN_DISTRICT, rows=[ACCOUNT_OWNER_ROW])
    runtime = Runtime.from_path(str(pkg))
    out = runtime.query(query)
    if columns is not None:
        assert _rows(out, columns) == _gold(_entry_gold(entry, "own"))
    assert _notes(out)[(LOAN, DISTRICT)]["code"] == "ROUTE_COLOCATED_KEY"
    sql = runtime.compile(query)["explain"]["rendered_sql"]
    assert "clients" not in sql and "branches" not in sql


def test_discovery_grain_recovery_and_entity_determination_follow_the_ladder(tmp_path):
    inherited = load_package_config(str(_write_package(tmp_path / "row", rows=[ACCOUNT_OWNER_ROW])))
    own = load_package_config(
        str(_write_package(tmp_path / "own", relationships=OWN_DISTRICT, rows=[ACCOUNT_OWNER_ROW]))
    )
    refused = load_package_config(str(_write_package(tmp_path / "none")))
    for config, route in ((inherited, [*LOAN_ACCOUNT, *OWNER]), (own, OWN_KEY)):
        assert _path_availability(config, LOAN, DISTRICT)["path"] == route
        assert _chosen_path(config, start=LOAN, target=DISTRICT) == route
        # A loan determines its district by the chosen route, however it was chosen.
        assert _entity_determines(
            config=config,
            source_entity=LOAN,
            target_entity=DISTRICT,
            time_bound_relationships=set(),
        )
    availability = _path_availability(refused, LOAN, DISTRICT)
    assert (availability["available"], availability["error_code"]) == (False, "AMBIGUOUS_PATH")
    assert _chosen_path(refused, start=LOAN, target=DISTRICT) is None
    assert not _entity_determines(
        config=refused, source_entity=LOAN, target_entity=DISTRICT, time_bound_relationships=set()
    )


def test_a_child_filter_through_an_inherited_route_counts_each_loan_once(tmp_path):
    """Loans whose owner's district has a branch named Main: a lookup to the owner's district,
    then one-to-many to its branches, reached only through rows (the account's branch is a
    second route, which the account -> branch row rules out). The filter lowers to EXISTS."""
    rows = [
        ACCOUNT_OWNER_ROW,
        _row(ACCOUNT, _entity("branch"), [*OWNER, _rel("branches_district")]),
    ]
    pkg = _write_package(tmp_path, rows=rows)
    resolution = resolve_route(load_package_config(str(pkg)), start=LOAN, target=_entity("branch"))
    assert resolution.basis == "inherited"
    query = _query(LOAN_AMOUNT, where=[{"field": BRANCH_NAME, "op": "=", "value": "Main"}])
    runtime = Runtime.from_path(str(pkg))
    out = runtime.query(query)
    gold = (
        "SELECT SUM(l.amount) FROM loans l JOIN accounts a USING (account_id) "
        "JOIN clients c ON c.client_id = a.client_id WHERE EXISTS (SELECT 1 FROM branches b "
        "WHERE b.district_id = c.district_id AND b.branch_name = 'Main')"
    )
    assert _rows(out, ["v"]) == _gold(gold)
    assert _gold(gold) != _gold(
        "SELECT SUM(l.amount) FROM loans l JOIN accounts a USING (account_id) "
        "JOIN branches b ON b.branch_id = a.branch_id WHERE b.branch_name = 'Main'"
    )
    assert "EXISTS" in runtime.compile(query)["explain"]["rendered_sql"]


@pytest.mark.parametrize(
    ("relationships", "rows", "forward_only", "max_hops", "start"),
    [
        (LENDER, [ACCOUNT_OWNER_ROW], (), 4, LOAN),
        (LENDER, [ACCOUNT_OWNER_ROW], (), 4, DISTRICT),
        (LENDER, [ACCOUNT_OWNER_ROW], ("clients_district",), 4, DISTRICT),
        (OWN_DISTRICT, [ACCOUNT_OWNER_ROW], (), 4, LOAN),
        ((*LENDER, "accounts_district"), [ACCOUNT_OWNER_ROW], (), 2, LOAN),
        (
            (*OWN_DISTRICT, "loans_payout_district"),
            [_row(DISTRICT, LOAN, OWN_KEY)],
            (),
            4,
            LOAN,
        ),
    ],
    ids=["inherited", "reverse", "one-way", "own-key", "excluded", "two-own-keys"],
)
def test_path_hints_agree_with_the_inherited_route_ladder(
    tmp_path, monkeypatch, relationships, rows, forward_only, max_hops, start
):
    config = load_package_config(
        str(
            _write_package(
                tmp_path,
                relationships=relationships,
                rows=rows,
                forward_only=forward_only,
                max_hops=max_hops,
            )
        )
    )
    expected = []
    for entity in sorted(config.entities, key=lambda entity: entity.id):
        if entity.id == start:
            continue
        try:
            resolve_path(config, start=start, target=entity.id)
        except SemanticLayerError:
            continue
        expected.append(entity.id)
    analysis = get_package_analysis(config)
    analysis.path_cache.clear()

    def reject_full_resolution(*args, **kwargs):
        pytest.fail("hint eligibility must not enumerate or render full route envelopes")

    monkeypatch.setattr(fanout_module, "enumerate_paths", reject_full_resolution)
    monkeypatch.setattr(fanout_module, "resolve_route", reject_full_resolution)
    monkeypatch.setattr(fanout_module, "_route_decision_required", reject_full_resolution)
    assert eligible_path_targets(config, start=start) == expected
    assert not analysis.path_cache
    assert not analysis.route_note_cache
