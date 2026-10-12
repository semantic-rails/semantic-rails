"""Authored metric words name an answer; undeclared words never invent one."""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.planner import _base, faithfulness, plan_payload, time_windows
from semantic_rails.planner.call import Call
from semantic_rails.runtime import Runtime
from tests.semantic_rails.planner_fixtures import jaffle_runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
SEED = """
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a','Acme Data Co','customer'),('b','Globex','customer'),
 ('c','QA Sandbox','internal'),('d','Initech','customer');
CREATE TABLE events (event_id INTEGER, account_id VARCHAR, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1,'a','signup','2026-09-02'),(2,'b','signup','2026-09-22'),
 (3,'c','signup','2026-09-29'),(4,'d','signup','2026-09-30'),
 (5,'b','close','2026-10-01'),(6,'a','upgrade','2026-10-02');
CREATE VIEW event_records AS SELECT e.*, a.segment FROM events e JOIN accounts a USING (account_id);
"""
# Customer signups: one for "Globex's", two for "Globex", one for "Trader Joe's".
CLIENTS = """
ALTER TABLE events ADD COLUMN client VARCHAR;
UPDATE events SET client = CASE WHEN event_id = 1 THEN 'Globex''s' ELSE 'Globex' END;
INSERT INTO events VALUES (7,'d','signup','2026-09-30','Trader Joe''s');
CREATE OR REPLACE VIEW event_records AS
 SELECT e.*, a.segment FROM events e JOIN accounts a USING (account_id);
"""


def _alias(item: Any) -> str:
    """A select item's output column: an id's last segment, or its ``as``."""
    return item.split(".")[-1] if isinstance(item, str) else item["as"]


def _package(
    root: Path, *, synonyms: bool = False, collision: bool = False, clients: bool = False
) -> Path:
    metrics: dict[str, Any] = {}
    for key, kind, aliases in [
        ("new_accounts", "signup", ["signups", "signed up", "new signups"]),
        ("closures", "close", ["closed"]),
        ("upgrades", "upgrade", ["upgraded"]),
    ]:
        metrics[key] = {
            "label": key.replace("_", " ").title(),
            "kind": "aggregate",
            "value_type": "count",
            "temporal_role": "temporal_role.shop_event_occurred_at",
            **({"synonyms": aliases} if synonyms else {}),
            "expression": {
                "kind": "aggregate",
                "measure": "measure.shop.events_all",
                "aggregation": "count_distinct",
                "filter": {
                    "all": [
                        {"field": "dimension.shop_event_kind", "op": "=", "value": kind},
                        {"field": "dimension.shop_event_segment", "op": "=", "value": "customer"},
                    ]
                },
            },
        }
    metrics["net_new_accounts"] = {
        "label": "Net new accounts (customers)",
        "kind": "derived",
        "value_type": "count",
        "temporal_role": "temporal_role.shop_event_occurred_at",
        "expression": {
            "kind": "binary",
            "op": "subtract",
            "left": {"kind": "metric", "metric": "metric.shop.new_accounts"},
            "right": {"kind": "metric", "metric": "metric.shop.closures"},
        },
    }
    if collision:
        metrics["closures"]["synonyms"] = ["signups", "signed up"]
    dimensions: dict[str, Any] = {
        "kind": {"kind": "categorical", "domain": ["signup", "close", "upgrade"]},
        "segment": {"kind": "categorical", "domain": ["customer", "internal"]},
    }
    if clients:
        dimensions["client"] = {
            "kind": "categorical",
            "domain": ["Globex's", "Globex", "Trader Joe's"],
        }
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "shop",
                "namespace": "shop",
                "name": "shop",
                "warehouse": "duckdb",
                "default_db": "shop.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {"graph": {"entities": {"event": {"key": ["event_id"], "model": "events"}}}},
        "models/events.yml": {
            "model": {
                "id": "events",
                "relation": "event_records",
                "entities": {"event": {}},
                "times": {
                    "occurred_at": {
                        "column": "occurred_at",
                        "kind": "date",
                        "class": "event_time",
                        "default": True,
                    }
                },
                "dimensions": dimensions,
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
        "metrics/accounts.yml": {"metrics": metrics},
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(root / "shop.duckdb")) as connection:
        connection.execute(SEED)
        if clients:
            connection.execute(CLIENTS)
    return root


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory):
    """Each package variant, written once per module; a test gets a runtime of its own."""
    roots: dict[tuple[bool, bool, bool], Path] = {}

    def build(*, synonyms: bool = False, collision: bool = False, clients: bool = False) -> Path:
        key = (synonyms, collision, clients)
        if key not in roots:
            root = tmp_path_factory.mktemp("shop") / "shop"
            roots[key] = _package(root, synonyms=synonyms, collision=collision, clients=clients)
        return roots[key]

    return build


def _open(packages, **flags: bool):
    return closing(Runtime.from_path(str(packages(**flags))))


jaffle = pytest.fixture(scope="module", name="jaffle")(jaffle_runtime)


@pytest.fixture(autouse=True)
def monday(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix the lexical clock independently of the caller-clock implementation.
    class Monday(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 10, 5)

    monkeypatch.setattr(time_windows, "date", Monday)


@pytest.mark.parametrize(
    ("question", "metric", "gold"),
    [
        ("How many accounts signed up last week?", "new_accounts", 1),
        ("How many signups last week?", "new_accounts", 1),
        ("What's the number of new accounts last week?", "new_accounts", 1),
        ("What’s the number of new accounts last week?", "new_accounts", 1),
        ("Net new accounts by month", "net_new_accounts", [3, -1]),
    ],
)
def test_authored_names_answer_reference_sql(packages, question: str, metric: str, gold: Any):
    root = packages(synonyms="sign" in question)
    with closing(Runtime.from_path(str(root))) as runtime:
        plan = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query"]
        assert Call.from_call(query).select[0].ref == f"metric.shop.{metric}"
        rows = runtime.query(query, now=NOW["now"])["rows"]
        values = sorted((row[_alias(query["select"][0])] for row in rows), reverse=True)
        with duckdb.connect(str(root / "shop.duckdb"), read_only=True) as connection:
            if metric == "net_new_accounts":
                sql = """SELECT date_trunc('month', occurred_at),
                    COUNT(DISTINCT CASE WHEN kind='signup' THEN event_id END)
                    - COUNT(DISTINCT CASE WHEN kind='close' THEN event_id END)
                    FROM event_records WHERE segment='customer' GROUP BY 1 ORDER BY 1"""
                reference = [row[1] for row in connection.execute(sql).fetchall()]
            else:
                kind = {"new_accounts": "signup", "closures": "close", "upgrades": "upgrade"}[
                    metric
                ]
                reference = [
                    connection.execute(
                        """SELECT COUNT(DISTINCT event_id)
                    FROM event_records WHERE segment='customer' AND kind=?
                    AND occurred_at >= DATE '2026-09-28' AND occurred_at < DATE '2026-10-05'""",
                        [kind],
                    ).fetchone()[0]
                ]
        assert values == reference == (gold if isinstance(gold, list) else [gold])


@pytest.mark.parametrize(
    ("question", "synonyms"),
    [
        ("How many accounts signed up last week?", False),
        ("How many signups last week?", False),
        ("How many accounts closed last week?", False),
        ("How many accounts upgraded last week?", False),
        # A partial or non-contiguous synonym is not consumed.
        ("How many accounts signed last week up?", True),
        ("How many accounts signed last week?", True),
    ],
)
def test_undeclared_inflections_stay_held(packages, question: str, synonyms: bool):
    with _open(packages, synonyms=synonyms) as runtime:
        plan = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        assert plan["status"] != "ok"
        assert "ready_for" not in plan["next"]


@pytest.mark.parametrize("phrase", ["signups", "signed up"])
def test_shared_synonym_clarifies_instead_of_rank_picking(packages, phrase: str):
    root = packages(synonyms=True, collision=True)
    with closing(Runtime.from_path(str(root))) as runtime:
        plan = plan_payload(
            runtime, intent=f"How many {phrase} last week?", partial_query={"policy_context": NOW}
        )
        assert plan["status"] == "needs_clarification", plan
        assert "ready_for" not in plan["next"]
        assert "metric.shop.closures" in str(plan["why"])
        assert "metric.shop.new_accounts" in str(plan["why"])
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    collisions = [row for row in report["warnings"] if row["code"] == "SEMANTIC_TERM_COLLISION"]
    assert len(collisions) == 2, collisions  # once per shared phrase


def test_aliases_is_still_an_unknown_metric_key(tmp_path: Path):
    root = _package(tmp_path / "shop")
    path = root / "metrics/accounts.yml"
    doc = yaml.safe_load(path.read_text())
    doc["metrics"]["new_accounts"]["aliases"] = ["signups"]
    path.write_text(yaml.safe_dump(doc))
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert not report["ok"]
    assert "aliases" in str(report["errors"])


@pytest.mark.parametrize(
    ("text", "expected", "apostrophe"),
    [
        (text, expected, ("'", "’")[index % 2])
        for index, (text, expected) in enumerate(
            [
                ("What's here?", "What is here?"),
                ("Who's there?", "Who is there?"),
                ("Where's it?", "Where is it?"),
                ("When's it?", "When is it?"),
                ("How's it?", "How is it?"),
                ("It's that", "It is that"),
                ("That's it", "That is it"),
                ("There's it", "There is it"),
                ("Here's it", "Here is it"),
                ("Globex's accounts", "Globex's accounts"),
                ("didn't close", "did not close"),
                ("can't close", "can not close"),
                ("won't close", "will not close"),
                ("we're here", "we are here"),
                ("we've closed", "we have closed"),
                ("we'll close", "we will close"),
                ("we'd close", "we would close"),
            ]
        )
    ],
)
def test_contractions(text: str, expected: str, apostrophe: str) -> None:
    from semantic_rails.planner.plan import _normalize_question

    assert _normalize_question(text.replace("'", apostrophe)) == expected.replace("'", apostrophe)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sales for \"Who's Next\", who's up", 'sales for "Who\'s Next", who is up'),
        ("sales for “Who’s Next”, who’s up", "sales for “Who’s Next”, who is up"),
        ("sales for 'Who's Next', who's up", "sales for 'Who's Next', who is up"),
        ("sales for ‘Who’s Next’, who’s up", "sales for ‘Who’s Next’, who is up"),
        ("sales at It's Sugar, it's up", "sales at It's Sugar, it is up"),
        ("sales at it’s sugar", "sales at it’s sugar"),
        ("didn't close at We'll Bake", "did not close at We'll Bake"),
    ],
)
def test_quoted_text_and_declared_phrases_stay_as_typed(text: str, expected: str) -> None:
    from semantic_rails.planner.plan import _normalize_question

    assert _normalize_question(text, ["It's Sugar", "We'll Bake"]) == expected


@pytest.mark.parametrize(
    "question",
    ['new accounts for "Globex\'s"', "new accounts for Globex's", "new accounts for ‘Globex’s’"],
)
def test_apostrophe_value_is_never_answered_as_another_value(packages, question: str):
    root = packages(clients=True)
    with closing(Runtime.from_path(str(root))) as runtime:
        plan = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        assert plan["intent"] == question
        if plan["status"] != "ok":
            assert "ready_for" not in plan["next"]
            return
        query = plan["best"]["query"]
        assert [(row["field"], row["value"]) for row in query["where"]] == [
            ("dimension.shop_event_client", "Globex's")
        ]
        rows = runtime.query(query, now=NOW["now"])["rows"]
    with duckdb.connect(str(root / "shop.duckdb"), read_only=True) as connection:
        reference = connection.execute(
            """SELECT COUNT(DISTINCT event_id) FROM event_records
            WHERE kind='signup' AND segment='customer' AND client='Globex''s'"""
        ).fetchone()[0]
    assert [row["new_accounts"] for row in rows] == [reference] == [1]


def test_declared_value_with_a_possessive_still_matches(packages):
    root = packages(clients=True)
    with closing(Runtime.from_path(str(root))) as runtime:
        plan = plan_payload(
            runtime, intent="new accounts for Trader Joe's", partial_query={"policy_context": NOW}
        )
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query"]
        assert query["where"] == [
            {"field": "dimension.shop_event_client", "op": "is", "value": "Trader Joe's"}
        ]
        rows = runtime.query(query, now=NOW["now"])["rows"]
    with duckdb.connect(str(root / "shop.duckdb"), read_only=True) as connection:
        reference = connection.execute(
            """SELECT COUNT(DISTINCT event_id) FROM event_records
            WHERE kind='signup' AND segment='customer' AND client='Trader Joe''s'"""
        ).fetchone()[0]
    assert [row["new_accounts"] for row in rows] == [reference] == [1]


def test_hidden_metric_words_never_reach_relevance(tmp_path: Path):
    from dataclasses import replace

    from semantic_rails.catalog_search import CatalogSearchIndex
    from semantic_rails.metadata import discover_payload
    from semantic_rails.schema import SemanticPolicyConfig

    root = _package(tmp_path / "shop", synonyms=True)
    path = root / "metrics/accounts.yml"
    doc = yaml.safe_load(path.read_text())
    doc["metrics"]["closures"]["synonyms"] = ["aabankruptcy"]
    doc["metrics"]["upgrades"]["synonyms"] = ["aaupgrade"]
    path.write_text(yaml.safe_dump(doc))
    hidden = "metric.shop.closures"
    with closing(Runtime.from_path(str(root))) as runtime:
        config = runtime._config
        owned = CatalogSearchIndex.from_config(config).catalog_tokens - (
            CatalogSearchIndex.from_config(
                replace(config, metric_recipes=[r for r in config.metric_recipes if r.id != hidden])
            ).catalog_tokens
        )
        assert {"aabankruptcy", "closures"} <= owned
        runtime._config = replace(
            config,
            semantic_policies=[
                SemanticPolicyConfig(
                    id="policy.hide_closures",
                    kind="object_visibility",
                    object_ids=[hidden],
                    action="hidden",
                )
            ],
        )
        for question in ["weather zebras", "aabankruptcy zebras"]:
            for payload in (
                plan_payload(runtime, intent=question, partial_query={"policy_context": NOW}),
                discover_payload(runtime, terms=question, enforce_scope=True),
            ):
                assert payload.get("status", "out_of_scope") == "out_of_scope", payload
                assert "catalog_token_sample" in str(payload)
                said = set(question.split())
                words = set(re.findall(r"[a-z0-9]+", json.dumps(payload).lower())) - said
                assert not words & owned
        # A visible synonym still counts toward relevance.
        plan = plan_payload(
            runtime, intent="aaupgrade zebras", partial_query={"policy_context": NOW}
        )
        assert plan["status"] != "out_of_scope", plan


@pytest.mark.parametrize(
    "question",
    ["session to order conversion rate by store", "session to order conversion rate"],
)
def test_label_shared_without_parenthetical_clarifies(jaffle, question: str):
    plan = plan_payload(jaffle, intent=question)
    assert plan["status"] == "needs_clarification"
    assert "ready_for" not in plan["next"]
    assert plan["why"]["details"]["gaps"][0]["expected"]["candidates"] == [
        "metric.sales.session_to_order_conversion_rate_7d",
        "metric.sales.session_to_order_conversion_rate_7d_same_store",
    ]


@pytest.mark.parametrize(
    ("question", "subject", "status"),
    [
        ("rolling 7-day revenue", "measure.jaffle.rolling_7d_revenue_usd", "low_confidence"),
        ("revenue MTD", "measure.jaffle.revenue_mtd_usd", "ok"),
    ],
)
def test_label_without_parenthetical_never_selects(
    jaffle, question: str, subject: str, status: str
):
    plan = plan_payload(jaffle, intent=question)
    assert plan["status"] == status, plan
    if status == "low_confidence":
        assert "ready_for" not in plan["next"], plan
        assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP", plan
        assert "subject_window_mismatch" in [
            gap["kind"] for gap in plan["why"]["details"]["gaps"]
        ], plan
    else:
        assert "execute" in plan["next"]["ready_for"], plan
    assert Call.from_call(plan["best"]["query"]).subjects == [subject]


def test_negation_is_normalized_before_scope_and_parsing(packages, monkeypatch: pytest.MonkeyPatch):
    from semantic_rails import scope

    original = scope.classify_question
    questions = []

    def classify(question):
        questions.append(question)
        return original(question)

    monkeypatch.setattr(scope, "classify_question", classify)
    with _open(packages, synonyms=True) as runtime:
        plan = plan_payload(
            runtime,
            intent="Which accounts didn't close last week?",
            partial_query={"policy_context": NOW},
        )
    assert questions == ["Which accounts did not close last week?"]
    assert plan["intent"] == questions[0]
    assert plan["intent_ir"]["intent"] == questions[0]
    assert plan["status"] != "ok"


def test_phrase_consumption_records_exact_span_and_preserves_numbers(packages):
    from dataclasses import replace

    from semantic_rails.planner.consumed_spans import unconsumed_terms
    from semantic_rails.planner.unmatched_words import _unconsumed_words

    with _open(packages, synonyms=True) as runtime:
        metric = next(
            row for row in runtime._config.metric_recipes if row.id == "metric.shop.new_accounts"
        )
        runtime._config = replace(
            runtime._config, metric_recipes=[replace(metric, aliases=["7 day signups"])]
        )
        query = Call.from_call({"select": [{"as": "n", "expression": metric.id}]})
        assert _unconsumed_words(runtime, "7 days signups", query) == ([], [])
        assert unconsumed_terms(runtime, "7 days signups", query) == []
        assert unconsumed_terms(runtime, "7 days unrelated signups", query) == ["7"]


def test_collision_check_also_guards_a_forced_draft(packages):
    from semantic_rails.planner.faithfulness import intent_subject_why
    from semantic_rails.planner.intent_ir import parse_intent

    with _open(packages, synonyms=True, collision=True) as runtime:
        question = "How many signups last week?"
        query = Call.from_call({"select": [{"as": "n", "expression": "metric.shop.new_accounts"}]})
        why = intent_subject_why(
            runtime, question=question, intent_ir=parse_intent(runtime, question), query=query
        )
        assert why is not None
        assert why["details"]["gaps"][0]["expected"]["candidates"] == [
            "metric.shop.closures",
            "metric.shop.new_accounts",
        ]


def test_unique_words_separate_a_shared_synonym(packages):
    with _open(packages, synonyms=True, collision=True) as runtime:
        assert [
            row.id
            for row in faithfulness._shared_subjects(runtime._config, "new accounts signed up")
        ] == []
        assert (
            _base._named_metric(runtime._config, "new accounts signed up")[0].id
            == "metric.shop.new_accounts"
        )


def test_metric_synonym_lists_round_trip_from_yaml(packages):
    from semantic_rails.config import load_package_config

    config = load_package_config(str(packages(synonyms=True)))
    names = {row.id: row.aliases for row in config.metric_recipes}
    assert names["metric.shop.new_accounts"] == ["signups", "signed up", "new signups"]
    assert names["metric.shop.closures"] == ["closed"]
    assert names["metric.shop.upgrades"] == ["upgraded"]


def test_metric_synonym_colliding_with_a_measure_clarifies(packages):
    from dataclasses import replace

    with _open(packages, synonyms=True) as runtime:
        measure = runtime._config.measures[0]
        runtime._config = replace(
            runtime._config, measures=[replace(measure, label="Signups", publish=True)]
        )
        plan = plan_payload(
            runtime, intent="How many signups last week?", partial_query={"policy_context": NOW}
        )
        assert plan["status"] == "needs_clarification"
        assert measure.id in str(plan["why"])
        assert "metric.shop.new_accounts" in str(plan["why"])


def test_whole_metric_label_collision_clarifies(packages):
    from dataclasses import replace

    with _open(packages) as runtime:
        runtime._config = replace(
            runtime._config,
            metric_recipes=[
                replace(row, label="Account total") for row in runtime._config.metric_recipes[:2]
            ],
        )
        plan = plan_payload(runtime, intent="Account total", partial_query={"policy_context": NOW})
        assert plan["status"] == "needs_clarification"


def test_collision_options_include_only_visible_metrics(packages):
    from dataclasses import replace

    from semantic_rails.schema import SemanticPolicyConfig

    with _open(packages, synonyms=True, collision=True) as runtime:
        hidden = "metric.shop.closures"
        runtime._config = replace(
            runtime._config,
            metric_recipes=[
                replace(row, aliases=["signups"]) if row.id == "metric.shop.upgrades" else row
                for row in runtime._config.metric_recipes
            ],
            semantic_policies=[
                SemanticPolicyConfig(
                    id="policy.hide_closures",
                    kind="object_visibility",
                    object_ids=[hidden],
                    action="hidden",
                )
            ],
        )
        plan = plan_payload(
            runtime, intent="How many signups last week?", partial_query={"policy_context": NOW}
        )
        assert plan["status"] == "needs_clarification"
        assert hidden not in str(plan)
        assert plan["why"]["details"]["gaps"][0]["expected"]["candidates"] == [
            "metric.shop.new_accounts",
            "metric.shop.upgrades",
        ]
