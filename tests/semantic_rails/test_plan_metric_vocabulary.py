"""Authored metric words name an answer; undeclared words never invent one."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.planner import _base, plan_payload
from semantic_rails.runtime import Runtime

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


def _package(root: Path, *, synonyms: bool = False, collision: bool = False) -> Path:
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
                "schema_strict": True,
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
                "dimensions": {
                    "kind": {"kind": "categorical", "domain": ["signup", "close", "upgrade"]},
                    "segment": {"kind": "categorical", "domain": ["customer", "internal"]},
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
        "metrics/accounts.yml": {"metrics": metrics},
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(root / "shop.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(autouse=True)
def monday(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix the lexical clock independently of the caller-clock implementation.
    class Monday(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 10, 5)

    monkeypatch.setattr(_base, "date", Monday)


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
def test_authored_names_answer_reference_sql(tmp_path: Path, question: str, metric: str, gold: Any):
    root = _package(tmp_path / "shop", synonyms=True)
    with closing(Runtime.from_path(str(root))) as runtime:
        plan = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query_ir"]
        assert query["select"][0]["expression"] == {"metric": f"metric.shop.{metric}"}
        rows = runtime.query({**query, "policy_context": NOW})["rows"]
        values = sorted((row[query["select"][0]["as"]] for row in rows), reverse=True)
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
    "question",
    [
        "How many accounts signed up last week?",
        "How many signups last week?",
        "How many accounts closed last week?",
        "How many accounts upgraded last week?",
    ],
)
def test_undeclared_inflections_stay_held(tmp_path: Path, question: str):
    with closing(Runtime.from_path(str(_package(tmp_path / "shop")))) as runtime:
        plan = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        assert plan["status"] != "ok"
        assert "ready_for" not in plan["next"]


@pytest.mark.parametrize("phrase", ["signups", "signed up"])
def test_shared_synonym_clarifies_instead_of_rank_picking(tmp_path: Path, phrase: str):
    root = _package(tmp_path / "shop", synonyms=True, collision=True)
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


@pytest.mark.parametrize("apostrophe", ["'", "’"])
@pytest.mark.parametrize(
    ("text", "expected"),
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
        ("Globex's accounts", "Globex accounts"),
        ("didn't close", "did not close"),
        ("can't close", "can not close"),
        ("won't close", "will not close"),
        ("we're here", "we are here"),
        ("we've closed", "we have closed"),
        ("we'll close", "we will close"),
        ("we'd close", "we would close"),
    ],
)
def test_contractions(text: str, expected: str, apostrophe: str) -> None:
    from semantic_rails.planner.plan import _normalize_question

    assert _normalize_question(text.replace("'", apostrophe)) == expected


def test_negation_is_normalized_before_scope_and_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantic_rails import scope

    original = scope.classify_question
    questions = []

    def classify(question):
        questions.append(question)
        return original(question)

    monkeypatch.setattr(scope, "classify_question", classify)
    with closing(Runtime.from_path(str(_package(tmp_path / "shop", synonyms=True)))) as runtime:
        plan = plan_payload(
            runtime,
            intent="Which accounts didn't close last week?",
            partial_query={"policy_context": NOW},
        )
    assert questions == ["Which accounts did not close last week?"]
    assert plan["intent"] == questions[0]
    assert plan["intent_ir"]["intent"] == questions[0]
    assert plan["status"] != "ok"


@pytest.mark.parametrize(
    "question",
    [
        "How many accounts signed last week up?",
        "How many accounts signed last week?",
    ],
)
def test_partial_or_noncontiguous_synonym_is_not_consumed(tmp_path: Path, question: str):
    with closing(Runtime.from_path(str(_package(tmp_path / "shop", synonyms=True)))) as runtime:
        plan = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        assert plan["status"] != "ok"
        assert "ready_for" not in plan["next"]


def test_phrase_consumption_records_exact_span_and_preserves_numbers(tmp_path: Path):
    from dataclasses import replace

    from semantic_rails.planner.faithfulness import _unconsumed_words, unconsumed_terms

    with closing(Runtime.from_path(str(_package(tmp_path / "shop", synonyms=True)))) as runtime:
        metric = next(
            row for row in runtime._config.metric_recipes if row.id == "metric.shop.new_accounts"
        )
        runtime._config = replace(
            runtime._config, metric_recipes=[replace(metric, aliases=["7 day signups"])]
        )
        query = {"version": 2, "select": [{"as": "n", "expression": {"metric": metric.id}}]}
        assert _unconsumed_words(runtime, "7 days signups", query) == ([], [])
        assert unconsumed_terms(runtime, "7 days signups", query) == []
        assert unconsumed_terms(runtime, "7 days unrelated signups", query) == ["7"]


def test_collision_check_also_guards_a_forced_draft(tmp_path: Path):
    from semantic_rails.planner.faithfulness import intent_subject_why
    from semantic_rails.planner.intent_ir import parse_intent

    with closing(
        Runtime.from_path(str(_package(tmp_path / "shop", synonyms=True, collision=True)))
    ) as runtime:
        question = "How many signups last week?"
        query = {
            "version": 2,
            "select": [{"as": "n", "expression": {"metric": "metric.shop.new_accounts"}}],
        }
        why = intent_subject_why(
            runtime, question=question, intent_ir=parse_intent(runtime, question), query=query
        )
        assert why is not None
        assert why["details"]["gaps"][0]["expected"]["candidates"] == [
            "metric.shop.closures",
            "metric.shop.new_accounts",
        ]


def test_unique_words_separate_a_shared_synonym(tmp_path: Path):
    with closing(
        Runtime.from_path(str(_package(tmp_path / "shop", synonyms=True, collision=True)))
    ) as runtime:
        assert [
            row.id for row in _base._shared_subjects(runtime._config, "new accounts signed up")
        ] == []
        assert (
            _base._named_metric(runtime._config, "new accounts signed up")[0].id
            == "metric.shop.new_accounts"
        )
