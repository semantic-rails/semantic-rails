"""Which, who, top N and each X answer with the rows of the entity they name, never a total."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.interop.package_writer import write_package
from semantic_rails.planner import groupings as groupings_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner.call import Call
from semantic_rails.planner.filter_checks import _filter_value_gaps
from semantic_rails.planner.patterns import metric_by_dimension_rollup as rollup_module
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from semantic_rails.visible_view import view_of

NOW = {"now": "2026-10-05T06:00:00Z"}
EVENT_CLOCK = "temporal_role.subscriptions_event_occurred_at"
DAY_CLOCK = "temporal_role.subscriptions_account_day_day"
ACCOUNT_ID = "dimension.subscriptions_account_id"
ACCOUNT_NAME = "dimension.subscriptions_account_name"
PLAN = "dimension.subscriptions_account_day_plan"
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


def _files(
    display: Any,
    account_dimensions: dict[str, Any],
    event_display: str | None,
    event_region: bool = False,
) -> dict[str, str]:
    account: dict[str, Any] = {"key": ["account_id"], "model": "accounts"}
    if display is not None:
        account["display"] = display
    event: dict[str, Any] = {"key": ["event_id"], "model": "events"}
    if event_display is not None:
        event["display"] = event_display
    graph = {
        "graph": {
            "entities": {
                "account": account,
                "event": event,
                "account_day": {"key": ["account_id", "day"], "model": "account_day"},
            },
            "relationships": {
                "event_account": {"entities": ["event", "account"], "cardinality": "many_to_one"},
                "day_account": {
                    "entities": ["account_day", "account"],
                    "cardinality": "many_to_one",
                },
            },
        }
    }
    accounts = {
        "model": {
            "id": "accounts",
            "relation": "accounts",
            "entities": {"account": {}},
            "dimensions": {
                "name": {"kind": "categorical"},
                "segment": {"kind": "categorical", "domain": ["customer", "internal"]},
                **account_dimensions,
            },
        }
    }
    events = {
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
                "kind": {"kind": "categorical", "domain": ["signup", "close", "upgrade"]},
                "reference": {"kind": "categorical", "column": "kind"},
                **(
                    {"region": {"kind": "categorical", "domain": ["north", "south"]}}
                    if event_region
                    else {}
                ),
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
    }
    account_day = {
        "model": {
            "id": "account_day",
            "relation": "account_day",
            "entities": {"account_day": {}, "account": {}},
            "times": {
                "day": {"column": "day", "kind": "date", "class": "as_of_time", "default": True}
            },
            "dimensions": {"plan": {"kind": "categorical", "domain": ["basic", "pro"]}},
            "measures": {
                "mrr_all": {
                    "kind": "aggregate",
                    "expr": "mrr",
                    "accumulation": {"kind": "stock", "snapshot": "end_of_period"},
                    "publish": False,
                }
            },
        }
    }
    customer = {"field": SEGMENT, "op": "=", "value": "customer"}
    metrics: dict[str, Any] = {
        key: {
            "label": label,
            "kind": "aggregate",
            "value_type": "count",
            "temporal_role": EVENT_CLOCK,
            "synonyms": synonyms,
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
        for key, label, kind, synonyms in [
            ("new_accounts", "New accounts", "signup", ["signups"]),
            ("closures", "Closures", "close", ["closed"]),
            ("upgrades", "Upgrades", "upgrade", ["upgraded"]),
        ]
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
    return {
        "package.yml": yaml.safe_dump(
            {
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
            }
        ),
        "graph.yml": yaml.safe_dump(graph),
        "models/accounts.yml": yaml.safe_dump(accounts),
        "models/events.yml": yaml.safe_dump(events),
        "models/account_day.yml": yaml.safe_dump(account_day),
        "metrics/accounts.yml": yaml.safe_dump({"metrics": metrics}),
    }


@pytest.fixture(scope="module")
def roots() -> dict[str, Path]:
    return {}


@pytest.fixture()
def subscriptions(
    tmp_path_factory: pytest.TempPathFactory, roots: dict[str, Path]
) -> Iterator[Callable[..., Runtime]]:
    """The neutral subscriptions package; ``display="name"`` on account unless told otherwise.
    Each variant is written once per module; each call opens a runtime of its own."""

    runtimes: list[Runtime] = []

    def build(
        display: Any = "name",
        account_dimensions: dict[str, Any] | None = None,
        event_display: str | None = None,
        extra: str = "",
        event_region: bool = False,
    ) -> Runtime:
        key = repr((display, account_dimensions, event_display, extra, event_region))
        if key not in roots:
            roots[key] = root = tmp_path_factory.mktemp("subscriptions")
            files = _files(display, account_dimensions or {}, event_display, event_region)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with duckdb.connect(str(root / "subscriptions.duckdb")) as connection:
                connection.execute(SEED + extra)
        runtime = Runtime.from_path(str(roots[key]))
        runtimes.append(runtime)
        return runtime

    yield build
    for runtime in runtimes:
        runtime.close()


def _plan(runtime: Runtime, question: str) -> dict[str, Any]:
    return plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})


def _reference(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchall()


def _ready(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    return payload["best"]["query"]


@pytest.mark.parametrize(
    ("clause", "predicate", "expected"),
    [
        ("", "", [("North Star", 1)]),
        (" where region is south", " AND e.region = 'south'", [("North Star", 1)]),
        (" where region is north", " AND e.region = 'north'", []),
        (" excluding north", " AND (e.region != 'north' OR e.region IS NULL)", [("North Star", 1)]),
    ],
)
def test_a_resolved_name_never_adds_a_value_filter(
    subscriptions: Callable[..., Runtime], clause: str, predicate: str, expected: list[Any]
) -> None:
    runtime = subscriptions(
        event_region=True,
        extra="""
        ALTER TABLE events ADD COLUMN region VARCHAR;
        UPDATE events SET region = 'south';
        UPDATE accounts SET name = 'North Star' WHERE account_id = 'a';
        UPDATE accounts SET name = 'Star Labs' WHERE account_id = 'd';
        """,
    )
    query = _ready(_plan(runtime, f"How many new accounts did North Star have last month{clause}?"))
    region = "dimension.subscriptions_event_region"
    if not clause:
        assert all(row["field"] != region for row in query.get("where", []))
    else:
        assert any(row["field"] == region for row in query["where"])
    rows = runtime.query(query, now=NOW["now"])["rows"]
    actual = [(row[ACCOUNT_NAME], row["new_accounts"]) for row in rows]
    sql = f"""
    SELECT a.name, COUNT(DISTINCT e.event_id) FROM events e JOIN accounts a USING (account_id)
    WHERE a.segment = 'customer' AND e.kind = 'signup' AND a.account_id = 'a'
      AND e.occurred_at >= DATE '2026-09-01' AND e.occurred_at < DATE '2026-10-01'{predicate}
    GROUP BY a.name
    """
    assert actual == _reference(runtime, sql) == expected


_EVENTS_LAST_WEEK = """
SELECT a.account_id, a.name, COUNT(DISTINCT e.event_id)
FROM events e JOIN accounts a USING (account_id)
WHERE a.segment = 'customer' AND e.kind = '{kind}'
  AND e.occurred_at >= DATE '2026-09-28' AND e.occurred_at < DATE '2026-10-05'
GROUP BY 1, 2 HAVING COUNT(DISTINCT e.event_id) != 0 ORDER BY 2
"""


@pytest.mark.parametrize(
    ("question", "metric", "kind", "expected"),
    [
        ("Which accounts closed last week?", "closures", "close", [("b", "Globex", 1)]),
        ("Who upgraded last week?", "upgrades", "upgrade", [("a", "Acme Data Co", 1)]),
        ("Who closed last week?", "closures", "close", [("b", "Globex", 1)]),
        ("Which accounts upgraded last week?", "upgrades", "upgrade", [("a", "Acme Data Co", 1)]),
    ],
)
def test_a_list_answers_with_the_rows_of_the_entity_it_lists(
    subscriptions: Callable[..., Runtime],
    question: str,
    metric: str,
    kind: str,
    expected: list[tuple[Any, ...]],
) -> None:
    runtime = subscriptions()
    query = _ready(_plan(runtime, question))
    assert query["group_by"] == [ACCOUNT_ID, ACCOUNT_NAME]
    # A list keeps the rows the subject counts: a known value, not 0.
    assert query["having"] == [
        {"field": metric, "op": "missing", "value": False},
        {"field": metric, "op": "not", "value": 0},
    ]
    assert query["order_by"][0] == {"field": ACCOUNT_NAME, "direction": "asc"}
    rows = runtime.query(query, now=NOW["now"])["rows"]
    actual = [(row[ACCOUNT_ID], row[ACCOUNT_NAME], row[metric]) for row in rows]
    reference = _reference(runtime, _EVENTS_LAST_WEEK.format(kind=kind))
    assert actual == reference == expected


_UPGRADES = "metric.subscriptions.upgrades"
_DROPS_UPGRADE = {"field": "dimension.subscriptions_event_kind", "op": "not", "value": "upgrade"}


@pytest.mark.parametrize(
    ("text", "query", "gap"),
    [
        # The chosen metric's name carries the value in either number.
        ("upgrades last week", {"select": [_UPGRADES]}, False),
        ("each upgrade last week", {"select": [_UPGRADES]}, False),
        # A name that doesn't spell the value never carries it.
        ("upgrades last week", {"select": ["metric.subscriptions.closures"]}, True),
        # A filter on the value's dimension that drops it still holds.
        ("upgrades last week", {"select": [_UPGRADES], "where": [_DROPS_UPGRADE]}, True),
    ],
)
def test_a_chosen_name_carries_a_value_in_either_number(
    subscriptions: Callable[..., Runtime], text: str, query: dict[str, Any], gap: bool
) -> None:
    gaps = _filter_value_gaps(subscriptions(), text, Call.from_call(query))
    assert [row.kind for row in gaps] == (["filter_values_unrealized"] if gap else [])


_MRR_ON = """
SELECT a.account_id, a.name, d.mrr FROM account_day d JOIN accounts a USING (account_id)
WHERE a.segment = 'customer' AND d.day = DATE '2026-10-04'
ORDER BY d.mrr {direction}, a.account_id LIMIT {limit}
"""


@pytest.mark.parametrize(
    ("question", "limit", "direction", "expected"),
    [
        (
            "Top 2 accounts by MRR on 2026-10-04",
            2,
            "desc",
            [("a", "Acme Data Co", 500.0), ("d", "Initech", 99.0)],
        ),
        (
            "Top 2 accounts by MRR on 2026-10-04?",
            2,
            "desc",
            [("a", "Acme Data Co", 500.0), ("d", "Initech", 99.0)],
        ),
        (
            "Which 2 accounts had the most MRR on 2026-10-04?",
            2,
            "desc",
            [("a", "Acme Data Co", 500.0), ("d", "Initech", 99.0)],
        ),
        (
            "Top two accounts by MRR on 2026-10-04",
            2,
            "desc",
            [("a", "Acme Data Co", 500.0), ("d", "Initech", 99.0)],
        ),
        (
            "Top three accounts by MRR on 2026-10-04",
            3,
            "desc",
            [("a", "Acme Data Co", 500.0), ("d", "Initech", 99.0), ("b", "Globex", 0.0)],
        ),
        ("Bottom 1 account by MRR on 2026-10-04", 1, "asc", [("b", "Globex", 0.0)]),
        ("Which account had the least MRR on 2026-10-04?", 1, "asc", [("b", "Globex", 0.0)]),
    ],
)
def test_a_ranking_keeps_its_count_direction_and_entity(
    subscriptions: Callable[..., Runtime],
    question: str,
    limit: int,
    direction: str,
    expected: list[tuple[Any, ...]],
) -> None:
    runtime = subscriptions()
    query = _ready(_plan(runtime, question))
    assert query["group_by"] == [ACCOUNT_ID, ACCOUNT_NAME]
    assert query["order_by"] == [{"field": "mrr", "direction": direction}]
    assert query["limit"] == limit
    assert "having" not in query and not any("entity" in row for row in query.get("where", []))
    rows = runtime.query(query)["rows"]
    actual = [(row[ACCOUNT_ID], row[ACCOUNT_NAME], row["mrr"]) for row in rows]
    # The internal QA Sandbox (99) is outside the metric, so no tie reaches the cut.
    reference = _reference(runtime, _MRR_ON.format(direction=direction, limit=limit))
    assert actual == reference == expected


@pytest.mark.parametrize(
    "question",
    [
        "How much MRR did each plan make on 2026-10-04?",
        "What was MRR for each plan on 2026-10-04?",
        "MRR for every plan on 2026-10-04",
    ],
)
def test_each_groups_by_the_name_it_names(
    subscriptions: Callable[..., Runtime], question: str
) -> None:
    runtime = subscriptions()
    query = _ready(_plan(runtime, question))
    assert query["group_by"] == [PLAN]
    rows = runtime.query(query)["rows"]
    reference = _reference(
        runtime,
        "SELECT d.plan, SUM(d.mrr) FROM account_day d JOIN accounts a USING (account_id) "
        "WHERE a.segment = 'customer' AND d.day = DATE '2026-10-04' GROUP BY 1 ORDER BY 1",
    )
    assert (
        [(row[PLAN], row["mrr"]) for row in rows] == reference == [("basic", 99.0), ("pro", 500.0)]
    )


# A question listing accounts and one ranking them: its reference SQL, and its value's alias.
_ACCOUNT_ROWS = {
    "Which accounts closed last week?": (_EVENTS_LAST_WEEK.format(kind="close"), "closures"),
    "Top 2 accounts by MRR on 2026-10-04": (_MRR_ON.format(direction="DESC", limit=2), "mrr"),
}


def _account_rows(runtime: Runtime, query: dict[str, Any], question: str) -> None:
    sql, alias = _ACCOUNT_ROWS[question]
    rows = runtime.query(query, now=NOW["now"])["rows"]
    assert [(row[ACCOUNT_ID], row[alias]) for row in rows] == [
        (key, value) for key, _name, value in _reference(runtime, sql)
    ]


@pytest.mark.parametrize("question", list(_ACCOUNT_ROWS))
@pytest.mark.parametrize(
    ("extra", "group_by", "assumed"),
    [
        # Two dimensions name an account: plan shows the key alone and says so.
        (
            {"account_label": {"column": "name"}, "account_tier": {"column": "segment"}},
            [ACCOUNT_ID],
            True,
        ),
        # One does: it stands beside the key, as a display would.
        (
            {"account_label": {"column": "name"}},
            [ACCOUNT_ID, "dimension.subscriptions_account_account_label"],
            False,
        ),
        # None does ("Name" names nothing in "accounts").
        ({}, [ACCOUNT_ID], True),
    ],
)
def test_without_a_display_the_key_stands_alone_with_an_assumption(
    subscriptions: Callable[..., Runtime],
    extra: dict[str, Any],
    group_by: list[str],
    assumed: bool,
    question: str,
) -> None:
    runtime = subscriptions(display=None, account_dimensions=extra)
    payload = _plan(runtime, question)
    query = _ready(payload)
    assert query["group_by"] == group_by
    line = "Account has no display name, so its rows show its key, Account Id."
    assert (line in payload.get("assumptions", [])) is assumed
    _account_rows(runtime, query, question)


def test_who_reaching_two_entities_with_a_display_asks_which(
    subscriptions: Callable[..., Runtime],
) -> None:
    runtime = subscriptions(event_display="reference")
    payload = _plan(runtime, "Who upgraded last week?")
    assert payload["status"] == "needs_clarification"
    assert payload["next"] == {"action": "clarify"}
    clarification = payload["why"]["details"]["clarification"]
    assert clarification["question"] == "Whose rows: Account, Event?"
    assert clarification["options"] == [
        {"entity": "entity.subscriptions_account", "label": "Account"},
        {"entity": "entity.subscriptions_event", "label": "Event"},
    ]
    # No option is a call to run: naming the entity settles it.
    assert "options" not in payload["why"]["details"]
    query = _ready(_plan(runtime, "Which accounts upgraded last week?"))
    assert query["group_by"] == [ACCOUNT_ID, ACCOUNT_NAME]


def test_who_keeps_the_questions_filters_and_offers_no_call(
    subscriptions: Callable[..., Runtime],
) -> None:
    # Every event is in the south: the question's north keeps no row, whoever it lists.
    runtime = subscriptions(
        event_display="reference",
        event_region=True,
        extra="ALTER TABLE events ADD COLUMN region VARCHAR; UPDATE events SET region = 'south';",
    )
    payload = _plan(runtime, "Who upgraded last week in north?")
    assert payload["status"] == "needs_clarification", payload.get("why")
    region = {"field": "dimension.subscriptions_event_region", "op": "is", "value": "north"}
    assert region in payload["best"]["query"]["where"]
    # An option that ran without the north filter would answer another question.
    assert '"call"' not in json.dumps(payload["why"])
    query = _ready(_plan(runtime, "Which accounts upgraded last week in north?"))
    assert region in query["where"]
    assert runtime.query(query, now=NOW["now"])["rows"] == []


_MRR_LISTED = """
SELECT a.account_id, a.name, SUM(d.mrr) FROM account_day d JOIN accounts a USING (account_id)
WHERE a.segment = 'customer' AND d.day = DATE '2026-10-04'
GROUP BY 1, 2 HAVING SUM(d.mrr) IS NOT NULL AND SUM(d.mrr) != 0 ORDER BY 2
"""


def test_a_list_keeps_only_rows_with_a_known_value(subscriptions: Callable[..., Runtime]) -> None:
    # Initech's MRR is unknown on the day: it had no MRR the list can show.
    runtime = subscriptions(
        extra="UPDATE account_day SET mrr = NULL WHERE account_id = 'd' AND day = '2026-10-04';"
    )
    query = _ready(_plan(runtime, "Which accounts had MRR on 2026-10-04?"))
    rows = runtime.query(query, now=NOW["now"])["rows"]
    actual = [(row[ACCOUNT_ID], row[ACCOUNT_NAME], row["mrr"]) for row in rows]
    assert actual == _reference(runtime, _MRR_LISTED) == [("a", "Acme Data Co", 500.0)]


def test_a_callers_having_beside_a_list_is_held(subscriptions: Callable[..., Runtime]) -> None:
    # The caller keeps the accounts at 0 MRR (Globex); the list's own rule would drop them.
    payload = plan_payload(
        subscriptions(),
        intent="Which accounts had MRR on 2026-10-04?",
        partial_query={
            "select": ["metric.subscriptions.mrr"],
            "having": [{"field": "mrr", "op": "is", "value": 0}],
            "policy_context": NOW,
        },
    )
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize(
    "question",
    [
        # A superlative no ranking reads is never dropped into a list.
        "Who had the most MRR on 2026-10-04?",
        # A plan is not an entity: "which plans" lists nothing.
        "Which plans had MRR on 2026-10-04?",
        # Two plans or two accounts: "the most" without a count is held.
        "Which accounts had the most MRR on 2026-10-04?",
        # Only the listed noun names a subject: listing the accounts New accounts counts would
        # answer "which accounts signed up", not "which accounts there are".
        "Which accounts are there?",
    ],
)
def test_a_list_or_ranking_plan_cannot_read_is_held(
    subscriptions: Callable[..., Runtime], question: str
) -> None:
    payload = _plan(subscriptions(), question)
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("question", list(_ACCOUNT_ROWS))
def test_a_hidden_display_is_blank_and_the_entity_stays(
    subscriptions: Callable[..., Runtime], question: str
) -> None:
    runtime = subscriptions()
    hidden = SemanticPolicyConfig(
        id="policy.hide_name", kind="object_visibility", object_ids=[ACCOUNT_NAME], action="hidden"
    )
    runtime._config = replace(
        runtime._config, semantic_policies=[*runtime._config.semantic_policies, hidden]
    )
    view = view_of(runtime._config, {})
    account = next(row for row in view.entities if row.id == "entity.subscriptions_account")
    assert account.display == ""
    assert ACCOUNT_ID in {row.id for row in view.dimensions}
    payload = _plan(runtime, question)
    query = _ready(payload)
    assert query["group_by"] == [ACCOUNT_ID]
    assert ACCOUNT_NAME not in json.dumps(payload)
    _account_rows(runtime, query, question)


def test_a_draft_by_another_entitys_column_is_held(
    subscriptions: Callable[..., Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Forcing the old reading (a dimension scored by its words, account_day's account id) is
    # held by the readiness checks, which read the entity's stand-ins on their own.
    monkeypatch.setattr(groupings_module, "_entity_grouping", lambda *args: None)
    monkeypatch.setattr(rollup_module, "_entity_grouping", lambda *args: None)
    payload = _plan(subscriptions(), "Top 2 accounts by MRR on 2026-10-04")
    assert payload["best"]["query"]["group_by"] == [
        "dimension.subscriptions_account_day_account_id"
    ]
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize(
    ("display", "extra"),
    [
        ("account_id", {"account_id": {"kind": "categorical"}}),
        ("missing", {}),
        (["name"], {}),
        ("plan", {}),
    ],
)
def test_display_must_be_a_dimension_of_the_entitys_own_model_other_than_its_key(
    subscriptions: Callable[..., Runtime], display: Any, extra: dict[str, Any]
) -> None:
    with pytest.raises(SemanticLayerError) as raised:
        subscriptions(display=display, account_dimensions=extra)
    assert raised.value.code == "INVALID_CONFIG"
    assert "graph entity 'account' display" in str(raised.value)


def test_display_loads_and_writes_back(
    subscriptions: Callable[..., Runtime], tmp_path: Path
) -> None:
    config = load_package_config(str(Path(subscriptions().source_path)))
    account = next(row for row in config.entities if row.id == "entity.subscriptions_account")
    assert account.display == ACCOUNT_NAME
    # The writer can't write a filtered metric back yet; the graph is what this reads.
    config = replace(config, metric_recipes=[])
    written = write_package(config, tmp_path / "written", namespace="subscriptions")
    graph = yaml.safe_load((written / "graph.yml").read_text(encoding="utf-8"))
    assert graph["graph"]["entities"]["account"]["display"] == "name"
