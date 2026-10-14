"""A name in a question ("Acme", "Globex's") is looked up in the entities' display names.

Plan reads the one row a name gives (its key in ``where``, its display in ``group_by``), asks
which when it gives several, and holds when it gives none or the lookup fails. The lookup runs
under the caller's row filters and policies, through ``execute``'s own path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.errors import SemanticLayerError
from semantic_rails.naming import last_token
from semantic_rails.planner import plan_payload
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime

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
    **RequestContext(
        actor="end-user", audience="partner", attributes={"account": "Globex"}
    ).to_policy_context(),
    **NOW,
}


def _files(policies: list[dict[str, Any]], titles: bool) -> dict[str, Any]:
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
    if titles:
        # Events named by a title: a second entity a name can find rows of.
        files["models/events.yml"]["model"]["dimensions"]["title"] = {"kind": "categorical"}
        files["graph.yml"]["graph"]["entities"]["event"]["display"] = "title"
    return files


@pytest.fixture()
def subscriptions(tmp_path: Path) -> Iterator[Callable[..., Runtime]]:
    """The neutral subscriptions package, account named by ``display: name``; ``extra`` SQL
    runs after the seed, ``policies`` are its semantic policies, and ``titles`` names events
    by a ``title`` column the extra SQL adds."""

    runtimes: list[Runtime] = []

    def build(
        extra: str = "", policies: list[dict[str, Any]] | None = None, titles: bool = False
    ) -> Runtime:
        root = tmp_path / f"subscriptions_{len(runtimes)}"
        for name, content in _files(policies or [], titles).items():
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


def _rows(runtime: Runtime, query: dict[str, Any], context: dict[str, Any] = NOW) -> list[Any]:
    identity = {key: value for key, value in context.items() if key != "now"}
    return runtime.query(query, now=NOW["now"], context=identity)["rows"]


def _alias(query: dict[str, Any]) -> str:
    [item] = query["select"]
    return last_token(item) if isinstance(item, str) else str(item["as"])


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
        # A sentence's first word, with its possessive.
        ("Globex's MRR", "Globex", "b", _MRR_ON_OCTOBER_4, [("Globex", 0.0)]),
        ("What is Acme MRR?", "Acme", "a", _MRR_ON_OCTOBER_4, [("Acme Data Co", 500.0)]),
        (
            "Show Acme MRR on 2026-10-04",
            "Acme",
            "a",
            _MRR_ON_OCTOBER_4,
            [("Acme Data Co", 500.0)],
        ),
        (
            "What is Acme Data Co MRR?",
            "Acme Data Co",
            "a",
            _MRR_ON_OCTOBER_4,
            [("Acme Data Co", 500.0)],
        ),
        (
            "What is MRR Acme Data Co?",
            "Acme Data Co",
            "a",
            _MRR_ON_OCTOBER_4,
            [("Acme Data Co", 500.0)],
        ),
        # A sentence's first word that only the sentence capitalizes starts no name.
        (
            "Did Acme have new accounts last month?",
            "Acme",
            "a",
            _NEW_ACCOUNTS_IN_SEPTEMBER,
            [("Acme Data Co", 1)],
        ),
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
    query = payload["best"]["query"]
    assert {"field": ACCOUNT_ID, "op": "is", "value": key} in query["where"]
    assert query["group_by"][-1] == ACCOUNT_NAME
    display = expected[0][0]
    assert f"'{said}' is read as Account '{display}'." in payload["assumptions"]
    actual = [(row[ACCOUNT_NAME], row[_alias(query)]) for row in _rows(runtime, query)]
    assert actual == _reference(runtime, reference.format(key=key)) == expected


def test_a_name_of_several_rows_asks_which(subscriptions: Callable[..., Runtime]) -> None:
    runtime = subscriptions("INSERT INTO accounts VALUES ('e','Acme Labs','customer');")
    payload = _plan(runtime, "How many new accounts did Acme have last month?")
    assert payload["status"] == "needs_clarification", payload.get("why")
    assert payload["next"] == {"action": "clarify"}
    [gap] = payload["why"]["details"]["gaps"]
    assert gap["kind"] == "name_ambiguous"
    matches = gap["expected"]["matches"]
    assert matches == [
        {"entity": "entity.subscriptions_account", "display": "Acme Data Co", "key": "a"},
        {"entity": "entity.subscriptions_account", "display": "Acme Labs", "key": "e"},
    ]
    assert payload["why"]["details"]["clarification"]["question"] == (
        "Which one does 'Acme' mean: Account Acme Data Co (a) or Account Acme Labs (e)?"
    )
    # No match is a call to run; the full name settles it.
    assert '"call"' not in json.dumps(payload["why"])
    full = _plan(runtime, "How many new accounts did Acme Data Co have last month?")
    assert full["status"] == "ok", full.get("why")


def test_a_capitalized_name_is_read_whole(subscriptions: Callable[..., Runtime]) -> None:
    # "Accounts" is a catalog word, but it is part of the name: no display holds "Acme
    # Accounts", so plan holds, as it does for the quoted name.
    runtime = subscriptions()
    for question in (
        "How many new accounts did Acme Accounts have last month?",
        'How many new accounts did "Acme Accounts" have last month?',
    ):
        payload = _plan(runtime, question)
        assert payload["status"] != "ok", question
        assert "execute" not in payload["next"].get("ready_for", [])
        assert "Acme Data Co" not in json.dumps(payload), question


@pytest.mark.parametrize(
    "question",
    [
        # The options would read Acme's row alone and drop Globex.
        "How many new accounts did Acme and Globex have last month?",
        # They would drop the hour.
        "How many new accounts did Acme have last month at 9am?",
    ],
)
def test_an_ambiguous_name_never_offers_a_call_that_drops_a_constraint(
    subscriptions: Callable[..., Runtime], question: str
) -> None:
    runtime = subscriptions(
        "INSERT INTO accounts VALUES ('e','Acme Labs','customer'); "
        "INSERT INTO events VALUES (7,'e','signup','2026-09-15');"
    )
    payload = _plan(runtime, question)
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert '"call"' not in json.dumps(payload.get("why") or {})


def test_a_name_of_rows_of_two_entities_asks_which(subscriptions: Callable[..., Runtime]) -> None:
    runtime = subscriptions(
        "ALTER TABLE events ADD COLUMN title VARCHAR; "
        "UPDATE events SET title = CASE event_id WHEN 6 THEN 'Acme renewal' ELSE 'Other' END;",
        titles=True,
    )
    payload = _plan(runtime, "How many new accounts did Acme have last month?")
    assert payload["status"] == "needs_clarification", payload.get("why")
    [gap] = payload["why"]["details"]["gaps"]
    assert [(row["entity"], row["display"], row["key"]) for row in gap["expected"]["matches"]] == [
        ("entity.subscriptions_account", "Acme Data Co", "a"),
        ("entity.subscriptions_event", "Acme renewal", 6),
    ]


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


def test_a_word_capitalized_by_its_sentence_alone_is_no_name(
    subscriptions: Callable[..., Runtime],
) -> None:
    runtime = subscriptions(
        "INSERT INTO accounts VALUES ('r','Roughly Speaking','customer'); "
        "INSERT INTO events VALUES (7,'r','signup','2026-09-15');"
    )
    _held_on(_plan(runtime, "Roughly how many new accounts last month?"), ["roughly"])
    named = _plan(runtime, "How many new accounts did Roughly have last month?")
    assert named["status"] == "ok", named.get("why")
    query = named["best"]["query"]
    assert (
        [(row[ACCOUNT_NAME], row[_alias(query)]) for row in _rows(runtime, query)]
        == _reference(runtime, _NEW_ACCOUNTS_IN_SEPTEMBER.format(key="r"))
        == [("Roughly Speaking", 1)]
    )


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
    # A caller the filter doesn't apply to finds Acme; the partner finds its own account.
    for name, context, expected in [
        ("Acme", NOW, [("Acme Data Co", 7)]),
        ("Globex", PARTNER, [("Globex", 2)]),
    ]:
        payload = _plan(runtime, question.format(name), context)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query"]
        sql = (
            "SELECT name, SUM(seats) FROM accounts "
            f"WHERE segment = 'customer' AND name LIKE '{name}%' GROUP BY name"
        )
        actual = [(row[ACCOUNT_NAME], row["seats"]) for row in _rows(runtime, query, context)]
        assert actual == _reference(runtime, sql) == expected


def test_a_hidden_display_is_never_searched(
    subscriptions: Callable[..., Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = {
        "id": "policy.subscriptions.hide_name",
        "kind": "object_visibility",
        "action": "hidden",
        "object_ids": [ACCOUNT_NAME],
        "audiences": ["outside"],
    }
    runtime = subscriptions(policies=[policy])
    outside = {**RequestContext(actor="end-user", audience="outside").to_policy_context(), **NOW}
    calls: list[Any] = []
    query = runtime.query
    monkeypatch.setattr(
        runtime, "query", lambda *args, **kwargs: calls.append(args) or query(*args, **kwargs)
    )
    payload = _plan(runtime, "How many new accounts did Acme have last month?", outside)
    _held_on(payload, ["acme"])
    assert calls == []
    assert "Acme Data Co" not in json.dumps(payload)


def test_a_failed_lookup_is_held(
    subscriptions: Callable[..., Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = subscriptions()

    def denied(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SemanticLayerError("POLICY_DENIED", "denied")

    monkeypatch.setattr(runtime, "query", denied)
    _held_on(_plan(runtime, "How many new accounts did Acme have last month?"), ["acme"])


@pytest.mark.parametrize(
    ("question", "detail", "reads"),
    [
        ("How many new accounts did Acme have last month?", "best", 1),
        ("How many new accounts did Acme have last month?", "full", 1),
        ("How many new accounts did Zenith have last month?", "best", 1),
        ("How many new accounts last month?", "best", 0),
        # A word the catalog declares as a value (segment "internal") is no lookup.
        ("How many new accounts did Internal accounts have last month?", "best", 0),
    ],
)
def test_plan_reads_each_name_once_and_never_an_answer(
    subscriptions: Callable[..., Runtime],
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    detail: str,
    reads: int,
) -> None:
    runtime = subscriptions()
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
    query = runtime.query

    def recorded(call: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append((call, kwargs))
        return query(call, **kwargs)

    monkeypatch.setattr(runtime, "query", recorded)
    plan_payload(runtime, intent=question, partial_query={"policy_context": NOW}, detail=detail)
    assert len(calls) == reads
    for call, kwargs in calls:
        # A select-less listing of the display and the key: no measure, so no answer.
        assert not call.get("select")
        assert call["group_by"] == [ACCOUNT_NAME, ACCOUNT_ID]
        assert call["limit"] == 6
        assert kwargs["context"] == NOW
        assert {row["field"] for row in call["where"]} == {ACCOUNT_NAME}
        assert {row["op"] for row in call["where"]} == {"contains"}


def test_a_callers_draft_with_the_row_is_not_a_lookup(
    subscriptions: Callable[..., Runtime],
) -> None:
    # The draft the lookup would make, sent by the caller: readiness credits a name only for
    # a row plan found itself, so "Acme" is still held.
    runtime = subscriptions()
    partial = {
        "policy_context": NOW,
        "where": [{"field": ACCOUNT_ID, "op": "is", "value": "a"}],
        "group_by": [ACCOUNT_NAME],
    }
    payload = plan_payload(
        runtime, intent="How many new accounts did Acme have last month?", partial_query=partial
    )
    _held_on(payload, ["acme"])
