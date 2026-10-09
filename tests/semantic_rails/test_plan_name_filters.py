"""A name in a question ("Acme", "Globex's") is looked up in the entities' display names.

Plan reads the one row a name gives (its key in ``where``, its display in ``group_by``), asks
which when it gives several, and holds when it gives none or the lookup fails. The lookup runs
under the caller's row filters, and ``valid-values`` searches names the same way, in SQL.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.dialects import dialect_for_warehouse, supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.planner import plan_payload
from semantic_rails.renderer import render_expr, use_dialect
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlIdentifier, build_filter_condition

NOW = {"now": "2026-10-05T06:00:00Z"}
EVENT_CLOCK = "temporal_role.subscriptions_event_occurred_at"
DAY_CLOCK = "temporal_role.subscriptions_account_day_day"
ACCOUNT_ID = "dimension.subscriptions_account_id"
ACCOUNT_NAME = "dimension.subscriptions_account_name"
SEGMENT = "dimension.subscriptions_account_segment"
SEED = """
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a','Acme Data Co','customer'),('b','Globex','customer'),
  ('c','QA Sandbox','internal'),('d','Initech','customer');
CREATE TABLE events (event_id INTEGER, account_id VARCHAR, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1,'a','signup','2026-09-02'),(2,'b','signup','2026-09-22'),
  (3,'c','signup','2026-09-29'),(4,'d','signup','2026-09-30'),
  (5,'b','close','2026-10-01'),(6,'a','upgrade','2026-10-02');
CREATE TABLE account_day (account_id VARCHAR, day DATE, plan VARCHAR, mrr DOUBLE);
INSERT INTO account_day SELECT a.account_id, d::DATE,
  CASE WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END,
  CASE WHEN a.account_id='b' AND d >= DATE '2026-10-01' THEN 0
       WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d);
"""
# The caller sees only Globex: a partner reading its own account.
PARTNER = {
    **RequestContext(actor="end-user", audience="partner", attributes={"account": "Globex"})
    .to_policy_context(),
    **NOW,
}


def _files(policies: list[dict[str, Any]]) -> dict[str, Any]:
    customer = {"field": SEGMENT, "op": "=", "value": "customer"}
    metrics: dict[str, Any] = {
        key: {
            "label": label,
            "kind": "aggregate",
            "value_type": "count",
            "temporal_role": EVENT_CLOCK,
            "expression": {
                "kind": "aggregate",
                "measure": "measure.subscriptions.events_all",
                "aggregation": "count_distinct",
                "filter": {
                    "all": [
                        {"field": "dimension.subscriptions_event_kind", "op": "=", "value": kind},
                        customer,
                    ]
                },
            },
        }
        for key, label, kind in [("new_accounts", "New accounts", "signup")]
    }
    metrics["mrr"] = {
        "label": "MRR (USD)",
        "kind": "semi_additive",
        "value_type": "number",
        "temporal_role": DAY_CLOCK,
        "expression": {
            "kind": "semi_additive",
            "measure": "measure.subscriptions.mrr_all",
            "filter": {"all": [customer]},
        },
    }
    files: dict[str, Any] = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "subscriptions",
                "namespace": "subscriptions",
                "name": "Subscriptions",
                "description": "Account activity",
                "warehouse": "duckdb",
                "default_db": "subscriptions.duckdb",
                "seed": {"kind": "external"},
            },
            "defaults": {"time": {"timezone": "UTC"}},
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "account": {"key": ["account_id"], "model": "accounts", "display": "name"},
                    "event": {"key": ["event_id"], "model": "events"},
                    "account_day": {"key": ["account_id", "day"], "model": "account_day"},
                },
                "relationships": {
                    "event_account": {
                        "entities": ["event", "account"],
                        "cardinality": "many_to_one",
                    },
                    "day_account": {
                        "entities": ["account_day", "account"],
                        "cardinality": "many_to_one",
                    },
                },
            }
        },
        "models/accounts.yml": {
            "model": {
                "id": "accounts",
                "relation": "accounts",
                "entities": {"account": {}},
                "dimensions": {
                    "name": {"kind": "categorical"},
                    "segment": {"kind": "categorical", "domain": ["customer", "internal"]},
                },
            }
        },
        "models/events.yml": {
            "model": {
                "id": "events",
                "relation": "events",
                "entities": {"event": {}, "account": {}},
                "times": {
                    "occurred_at": {
                        "column": "occurred_at",
                        "kind": "date",
                        "class": "event_time",
                        "default": True,
                    }
                },
                "dimensions": {
                    "kind": {"kind": "categorical", "domain": ["signup", "close", "upgrade"]}
                },
                "measures": {
                    "events_all": {
                        "kind": "entity_count",
                        "entity_key": "event_id",
                        "value_type": "count",
                        "publish": False,
                    }
                },
            }
        },
        "models/account_day.yml": {
            "model": {
                "id": "account_day",
                "relation": "account_day",
                "entities": {"account_day": {}, "account": {}},
                "times": {
                    "day": {
                        "column": "day",
                        "kind": "date",
                        "class": "as_of_time",
                        "default": True,
                    }
                },
                "measures": {
                    "mrr_all": {
                        "kind": "aggregate",
                        "expr": "mrr",
                        "accumulation": {"kind": "stock", "snapshot": "end_of_period"},
                        "publish": False,
                    }
                },
            }
        },
        "metrics/accounts.yml": {"metrics": metrics},
    }
    if policies:
        # A row filter answers only a query reading its one relation: seats are the accounts'.
        files["models/accounts.yml"]["model"]["measures"] = {
            "seats_all": {
                "kind": "aggregate",
                "expr": "seats",
                "value_type": "count",
                "publish": False,
            }
        }
        metrics["seats"] = {
            "label": "Seats",
            "kind": "aggregate",
            "value_type": "count",
            "expression": {
                "kind": "aggregate",
                "measure": "measure.subscriptions.seats_all",
                "aggregation": "sum",
                "filter": {"all": [customer]},
            },
        }
        files["policies.yml"] = {"semantic_policies": policies}
    return files


@pytest.fixture()
def subscriptions(tmp_path: Path) -> Iterator[Callable[..., Runtime]]:
    """The neutral subscriptions package, account named by ``display: name``; ``extra`` SQL
    runs after the seed, and ``policies`` are its semantic policies."""

    runtimes: list[Runtime] = []

    def build(extra: str = "", policies: list[dict[str, Any]] | None = None) -> Runtime:
        root = tmp_path / f"subscriptions_{len(runtimes)}"
        for name, content in _files(policies or []).items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(content), encoding="utf-8")
        with duckdb.connect(str(root / "subscriptions.duckdb")) as connection:
            connection.execute(SEED + extra)
        runtime = Runtime.from_path(str(root))
        runtimes.append(runtime)
        return runtime

    yield build
    for runtime in runtimes:
        runtime.close()


def _plan(runtime: Runtime, question: str, context: dict[str, Any] = NOW) -> dict[str, Any]:
    return plan_payload(runtime, intent=question, partial_query={"policy_context": context})


def _reference(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchall()


def _held_on(payload: dict[str, Any], terms: list[str]) -> None:
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"] == {"terms": terms, "kind": "filter_values_unrealized"}
    assert "execute" not in payload["next"].get("ready_for", [])


_NEW_ACCOUNTS_IN_SEPTEMBER = """
SELECT a.name, COUNT(DISTINCT e.event_id) FROM events e JOIN accounts a USING (account_id)
WHERE e.kind = 'signup' AND a.segment = 'customer' AND a.account_id = '{key}'
  AND e.occurred_at >= DATE '2026-09-01' AND e.occurred_at < DATE '2026-10-01'
GROUP BY a.name
"""
_MRR_ON_OCTOBER_4 = """
SELECT a.name, SUM(d.mrr) FROM account_day d JOIN accounts a USING (account_id)
WHERE a.segment = 'customer' AND a.account_id = '{key}' AND d.day = DATE '2026-10-04'
GROUP BY a.name
"""


@pytest.mark.parametrize(
    ("question", "said", "key", "reference", "expected"),
    [
        (
            "How many new accounts did Acme have last month?",
            "Acme",
            "a",
            _NEW_ACCOUNTS_IN_SEPTEMBER,
            [("Acme Data Co", 1)],
        ),
        # Quoted, all capitals, and the whole name: the same row.
        (
            "How many new accounts did 'acme' have last month?",
            "acme",
            "a",
            _NEW_ACCOUNTS_IN_SEPTEMBER,
            [("Acme Data Co", 1)],
        ),
        (
            "How many new accounts did ACME have last month?",
            "ACME",
            "a",
            _NEW_ACCOUNTS_IN_SEPTEMBER,
            [("Acme Data Co", 1)],
        ),
        (
            "New accounts for Acme Data Co last month",
            "Acme Data Co",
            "a",
            _NEW_ACCOUNTS_IN_SEPTEMBER,
            [("Acme Data Co", 1)],
        ),
        # A balance: read on the last complete day, 2026-10-04, when Globex paid nothing.
        ("What's Globex's MRR?", "Globex", "b", _MRR_ON_OCTOBER_4, [("Globex", 0.0)]),
        # With or without the entity's own word.
        ("What is the MRR of account Globex?", "Globex", "b", _MRR_ON_OCTOBER_4, [("Globex", 0.0)]),
        ("MRR of the Globex account", "Globex", "b", _MRR_ON_OCTOBER_4, [("Globex", 0.0)]),
    ],
)
def test_a_name_reads_the_one_row_it_gives(
    subscriptions: Callable[..., Runtime],
    question: str,
    said: str,
    key: str,
    reference: str,
    expected: list[tuple[Any, ...]],
) -> None:
    runtime = subscriptions()
    payload = _plan(runtime, question)
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    assert {"field": ACCOUNT_ID, "op": "=", "value": key} in query["where"]
    assert query["group_by"][-1] == ACCOUNT_NAME
    display = expected[0][0]
    assert f"'{said}' is read as Account '{display}'." in payload["assumptions"]
    rows = runtime.query(query)["rows"]
    value = next(item["as"] for item in query["select"])
    actual = [(row[ACCOUNT_NAME], row[value]) for row in rows]
    assert actual == _reference(runtime, reference.format(key=key)) == expected


def test_a_name_of_several_rows_asks_which(subscriptions: Callable[..., Runtime]) -> None:
    runtime = subscriptions("INSERT INTO accounts VALUES ('e','Acme Labs','customer');")
    payload = _plan(runtime, "How many new accounts did Acme have last month?")
    assert payload["status"] == "needs_clarification", payload.get("why")
    assert payload["next"] == {"action": "clarify"}
    [gap] = payload["why"]["details"]["gaps"]
    assert gap["kind"] == "name_ambiguous"
    assert [(row["display"], row["key"], row["where"]) for row in gap["expected"]["matches"]] == [
        ("Acme Data Co", "a", {"field": ACCOUNT_ID, "op": "=", "value": "a"}),
        ("Acme Labs", "e", {"field": ACCOUNT_ID, "op": "=", "value": "e"}),
    ]
    assert payload["why"]["details"]["clarification"]["question"] == (
        "Which one does 'Acme' mean: Account Acme Data Co (a) or Account Acme Labs (e)?"
    )


@pytest.mark.parametrize(
    ("question", "terms"),
    [
        ("How many new accounts did Zenith have last month?", ["zenith"]),
        # A plural is another word, and a name is never a lowercase word outside quotes.
        ("How many new accounts did Acmes have last month?", ["acmes"]),
        ("How many new accounts did acme have last month?", ["acme"]),
        # Two rows of one entity: one filter can't read both.
        ("How many new accounts did Acme and Globex have last month?", ["acme", "globex"]),
    ],
)
def test_a_name_without_one_row_is_held(
    subscriptions: Callable[..., Runtime], question: str, terms: list[str]
) -> None:
    _held_on(_plan(subscriptions(), question), terms)


@pytest.mark.parametrize(("others", "ready"), [(4, True), (5, False)])
def test_a_full_lookup_never_makes_one_match(
    subscriptions: Callable[..., Runtime], others: int, ready: bool
) -> None:
    # "Acmeco" holds "acme" but not as a word. Five of them fill the six-row read with Acme
    # Data Co, so a seventh account holding "Acme" could hide past it: plan holds.
    names = ",".join(f"('x{index}','Acmeco {index}','customer')" for index in range(others))
    payload = _plan(
        subscriptions(f"INSERT INTO accounts VALUES {names};"),
        "How many new accounts did Acme have last month?",
    )
    if ready:
        assert payload["status"] == "ok", payload.get("why")
    else:
        _held_on(payload, ["acme"])


def test_a_row_filter_hides_the_name_and_plan_never_shows_it(
    subscriptions: Callable[..., Runtime],
) -> None:
    policy = {
        "id": "policy.subscriptions.own_account",
        "kind": "row_filter",
        "dimension": ACCOUNT_NAME,
        "attribute": "account",
        "audiences": ["partner"],
    }
    runtime = subscriptions(
        "ALTER TABLE accounts ADD COLUMN seats INTEGER; "
        "UPDATE accounts SET seats = CASE account_id WHEN 'a' THEN 7 ELSE 2 END;",
        policies=[policy],
    )
    question = "How many seats does {} have?"
    hidden = _plan(runtime, question.format("Acme"), PARTNER)
    _held_on(hidden, ["acme"])
    assert "Acme Data Co" not in json.dumps(hidden)
    # A caller the filter doesn't apply to finds it; the partner finds its own account.
    for name, context, expected in [("Acme", NOW, ("Acme Data Co", 7)), ("Globex", PARTNER, None)]:
        payload = _plan(runtime, question.format(name), context)
        assert payload["status"] == "ok", payload.get("why")
        rows = runtime.query({**payload["best"]["query_ir"], "policy_context": context})["rows"]
        sql = f"SELECT name, SUM(seats) FROM accounts WHERE name = '{expected[0] if expected else name}' GROUP BY name"
        assert [(row[ACCOUNT_NAME], row["seats"]) for row in rows] == _reference(runtime, sql)


def test_a_failed_lookup_is_held(
    subscriptions: Callable[..., Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = subscriptions()

    def denied(payload: dict[str, Any]) -> dict[str, Any]:
        raise SemanticLayerError("POLICY_DENIED", "denied")

    monkeypatch.setattr(runtime, "query", denied)
    _held_on(_plan(runtime, "How many new accounts did Acme have last month?"), ["acme"])


def test_a_callers_draft_with_the_row_is_not_a_lookup(subscriptions: Callable[..., Runtime]) -> None:
    # The draft the lookup would make, sent by the caller: readiness credits a name only for
    # a row plan found itself, so "Acme" is still held.
    runtime = subscriptions()
    partial = {
        "policy_context": NOW,
        "where": [{"field": ACCOUNT_ID, "op": "=", "value": "a"}],
        "group_by": [ACCOUNT_NAME],
    }
    payload = plan_payload(
        runtime, intent="How many new accounts did Acme have last month?", partial_query=partial
    )
    _held_on(payload, ["acme"])


def test_valid_values_searches_names_in_the_warehouse(
    subscriptions: Callable[..., Runtime],
) -> None:
    # 150 names with events sort before "Zeta Corp": the first 100 values never held it.
    runtime = subscriptions(
        "INSERT INTO accounts SELECT 'n' || i, 'Account ' || lpad(i::VARCHAR, 3, '0'), "
        "'customer' FROM range(150) t(i); "
        "INSERT INTO accounts VALUES ('z','Zeta Corp','customer'); "
        "INSERT INTO events SELECT 100 + row_number() OVER (ORDER BY account_id), account_id, "
        "'signup', DATE '2026-09-10' FROM accounts WHERE account_id LIKE 'n%' OR account_id = 'z';"
    )
    query = {"select": [{"expression": {"metric": "metric.subscriptions.new_accounts"}}]}
    for search in ("zeta", "ZETA corp"):
        payload = valid_values_payload(
            runtime,
            dimension_id=ACCOUNT_NAME,
            query=query,
            search=search,
            allow_live_query=True,
        )
        assert [row["value"] for row in payload["values"]] == ["Zeta Corp"], search
        assert payload["query_state"]["where"][-1] == {
            "field": ACCOUNT_NAME,
            "op": "ILIKE",
            "value": search.lower().replace(" ", "%").join("%%"),
        }


@pytest.mark.parametrize("warehouse", supported_warehouses())
@pytest.mark.parametrize(("op", "like"), [("ILIKE", "LIKE"), ("NOT ILIKE", "NOT LIKE")])
def test_ilike_is_like_on_lowercase_on_every_warehouse(warehouse: str, op: str, like: str) -> None:
    with use_dialect(dialect_for_warehouse(warehouse)):
        sql = render_expr(build_filter_condition(SqlIdentifier(["name"]), op, "%Acme%"))
    assert sql.replace('"', "").replace("`", "") == f"LOWER(name) {like} '%acme%'"


def test_ilike_filters_rows_in_any_case(subscriptions: Callable[..., Runtime]) -> None:
    runtime = subscriptions()
    result = runtime.query(
        {
            "version": 1,
            "select": [],
            "group_by": [ACCOUNT_NAME],
            "where": [{"field": ACCOUNT_NAME, "op": "ILIKE", "value": "%ACME%"}],
        }
    )
    assert [row[ACCOUNT_NAME] for row in result["rows"]] == ["Acme Data Co"]
