"""A ready plan's result has the shape of the answer the question asks for.

The invariant: plan calls a draft ready only if its result holds the part each of the
question's shape words asks for. "who", "which" or "list" asks for the rows of the entity its
clause names, so the group_by needs one of that entity's key dimensions; "each" needs a row per
item; a comparison ("compared with", "vs", "up or down") needs a prior-period select or a second
select with another expression; and two questions for a value ("how many ... and how much ...")
need a select of their own each, which names what the question asks about. A time grain, a
category, another entity's grouping or a repeated select never stands in. Each question below
was `ok` without that part, and so was each question an agent got by asking again without the
words a hold named, as its hint said. The check runs after every other one and only holds a
plan; a hint offers to ask again without words only when they are the "s" ending a contraction
("what's").
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_listed_groupings import _upkeep

GAP = "PLAN_INTENT_COVERAGE_GAP"
UNMATCHED = "PLAN_UNMATCHED_TERMS"
# A Monday: last week is 2017-08-14 to 2017-08-20, last month is July 2017.
NOW = {"now": "2017-08-21"}
DROP_HINT = re.compile(r"\bask again without\b", re.IGNORECASE)
CUSTOMER_NAME = "dimension.jaffle_customer_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDERS = {"measure": "measure.jaffle.order_count"}
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
LAST_WEEK = "WHERE ordered_at >= TIMESTAMP '2017-08-14' AND ordered_at < TIMESTAMP '2017-08-21'"


def _selects(*expressions: dict[str, Any]) -> dict[str, Any]:
    return {
        "select": [
            {"as": f"value_{index}", "expression": expression}
            for index, expression in enumerate(expressions)
        ]
    }


@pytest.fixture(scope="module")
def jaffle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    path = copy_package_config(tmp_path_factory.mktemp("shape"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(path))
    try:
        yield opened(runtime)
    finally:
        runtime.close()


def _gaps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list((payload.get("why") or {}).get("details", {}).get("gaps", []))


def _hints(payload: dict[str, Any]) -> list[str]:
    return [str(hint["message"]) for hint in (payload.get("why") or {}).get("recovery_hints", [])]


def _without(question: str, terms: list[str]) -> str:
    """The question asked again without the words a hold named, as an agent would."""

    for term in terms:
        question = re.sub(rf"(?<![^\W_]){re.escape(term)}(?![^\W_])", "", question)
    return question


# Each was `ok` with one value. Several are what an agent asked after dropping the words a hold
# named: "Which ordered last month?" from "Which customers ordered last month?", "... compared
# with the week ?" from "... the week before?".
HELD = [
    ("Who ordered last week?", "list_unrealized", '"who"'),
    ("Who placed an order last week?", "list_unrealized", '"who"'),
    ("Who are our customers?", "list_unrealized", '"who"'),
    ("Which ordered last month?", "list_unrealized", '"which"'),
    ("Who ordered from a last week?", "list_unrealized", '"who"'),
    ("Last week, who ordered?", "list_unrealized", '"who"'),
    ("How many orders last week and who placed them?", "list_unrealized", '"who"'),
    (
        "How many orders did we get last week compared with the week ?",
        "comparison_unrealized",
        '"compared"',
    ),
    (
        "Were orders up or down last week compared with the week ?",
        "comparison_unrealized",
        '"compared", "up or down"',
    ),
    ("Were orders up or down last week?", "comparison_unrealized", '"up or down"'),
    ("How many orders did each last week?", "each_unrealized", '"each"'),
    (
        "How many orders and how much revenue last week?",
        "multiple_questions_unrealized",
        '"how many", "how much"',
    ),
    (
        "Last week, how many orders, how many , and what was the revenue?",
        "multiple_questions_unrealized",
        '"how many", "how many", "what was"',
    ),
]


@pytest.mark.parametrize(("question", "kind", "clause"), HELD)
def test_one_value_never_answers_a_question_asking_for_more(
    jaffle: Runtime, question: str, kind: str, clause: str
) -> None:
    payload = plan_payload(jaffle, intent=question, partial_query={"policy_context": NOW})

    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == GAP
    [gap] = _gaps(payload)
    assert (gap["kind"], gap["clause"]) == (kind, clause)
    assert clause in gap["message"]
    # The draft stays for inspection, unchanged: one value.
    query = payload["best"]["query_ir"]
    assert len(query["select"]) == 1
    assert not query.get("group_by")
    assert gap["actual"]["select_count"] == 1


@pytest.mark.parametrize(("question", "kind", "clause"), HELD)
def test_only_the_shape_check_holds_them(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, question: str, kind: str, clause: str
) -> None:
    """Without the check each is `ok`: no other check reads its shape words."""

    monkeypatch.setattr(plan_module, "_answer_shape_why", lambda *_args: None)

    assert plan_payload(jaffle, intent=question)["status"] == "ok"


def test_the_http_route_holds_a_who_question(jaffle: Runtime) -> None:
    route = normalize_route("/api/v1/plan")
    payload, status = SemanticHTTPService(jaffle).handle(
        "POST", route, {"intent": "Who ordered last week?"}
    )

    assert status == 200
    assert payload["status"] == "low_confidence"
    assert [gap["kind"] for gap in _gaps(payload)] == ["list_unrealized"]


def test_a_caller_group_by_gives_who_its_rows(jaffle: Runtime) -> None:
    payload = plan_payload(
        jaffle,
        intent="Who ordered last week?",
        partial_query={"group_by": [CUSTOMER_NAME]},
    )

    assert payload["status"] == "ok"
    assert payload["best"]["query_ir"]["group_by"] == [CUSTOMER_NAME]


BOTH = "How many orders and how much revenue last week?"
BOTH_CLAUSE = '"how many", "how much"'
# Each was `ok` with more than one value, which answered a narrower question.
NARROWER = [
    # A time grain's rows, or another entity's, are not the rows of the entity the clause lists.
    ("List customers by month", {}, "list_unrealized", '"list"'),
    ("Who are our customers by store?", {}, "list_unrealized", '"who"'),
    ("Who ordered last week by store?", {}, "list_unrealized", '"who"'),
    # A category declares its values: its rows list none of the entity's.
    ("Who are our customers?", {"group_by": [CUSTOMER_TYPE]}, "list_unrealized", '"who"'),
    ("Who ordered last week?", {"group_by": [CUSTOMER_TYPE]}, "list_unrealized", '"who"'),
    # A group_by splits one value: it compares it with nothing.
    (
        "Orders by store last week compared with the week ?",
        {},
        "comparison_unrealized",
        '"compared"',
    ),
    ("Compare revenue by store last month", {}, "comparison_unrealized", '"compare"'),
    # One select twice is one value, and no other select names "orders": not Tax paid, not
    # Order cost (one word of its name), nor revenue on the order clock ("Order time").
    (
        BOTH,
        _selects({**REVENUE, "aggregation": "sum"}, {**REVENUE, "aggregation": "sum"}),
        "multiple_questions_unrealized",
        BOTH_CLAUSE,
    ),
    (
        BOTH,
        _selects(REVENUE, {**REVENUE, "aggregation": "sum"}),
        "multiple_questions_unrealized",
        BOTH_CLAUSE,
    ),
    (
        BOTH,
        _selects({"measure": "measure.jaffle.tax_paid_usd"}, REVENUE),
        "multiple_questions_unrealized",
        BOTH_CLAUSE,
    ),
    (
        BOTH,
        _selects({"measure": "measure.jaffle.order_cost_usd"}, REVENUE),
        "multiple_questions_unrealized",
        BOTH_CLAUSE,
    ),
    (
        BOTH,
        _selects({**REVENUE, "temporal_role": "temporal_role.jaffle_order_time"}, REVENUE),
        "multiple_questions_unrealized",
        BOTH_CLAUSE,
    ),
]


@pytest.mark.parametrize(("question", "partial", "kind", "clause"), NARROWER)
def test_a_shape_answering_a_narrower_question_is_held(
    jaffle: Runtime, question: str, partial: dict[str, Any], kind: str, clause: str
) -> None:
    payload = plan_payload(
        jaffle, intent=question, partial_query={**partial, "policy_context": NOW}
    )

    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == GAP
    [gap] = _gaps(payload)
    assert (gap["kind"], gap["clause"]) == (kind, clause)
    assert clause in gap["message"]


@pytest.mark.parametrize(("question", "partial", "kind", "clause"), NARROWER)
def test_only_the_shape_check_holds_those(
    jaffle: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    partial: dict[str, Any],
    kind: str,
    clause: str,
) -> None:
    monkeypatch.setattr(plan_module, "_answer_shape_why", lambda *_args: None)
    payload = plan_payload(
        jaffle, intent=question, partial_query={**partial, "policy_context": NOW}
    )

    assert payload["status"] == "ok"


@pytest.mark.parametrize(
    ("question", "group_by", "keys"),
    [
        ("List customers by month", None, ["dimension.jaffle_customer_id"]),
        (
            "Who are our customers by store?",
            ["dimension.jaffle_store_name"],
            ["dimension.jaffle_customer_id"],
        ),
    ],
)
def test_a_list_hold_names_the_listed_entitys_key_dimensions(
    jaffle: Runtime, question: str, group_by: list[str] | None, keys: list[str]
) -> None:
    payload = plan_payload(jaffle, intent=question)

    [gap] = _gaps(payload)
    assert gap["expected"] == {"answer": "rows", "key_dimensions": keys}
    assert payload["best"]["query_ir"].get("group_by") == group_by


# Questions a value or rows of the asked shape answer, which stay `ok`.
STAY_OK = [
    "How many orders last week?",
    "What was revenue last month?",
    "How much revenue did we make last month?",
    "What is the average order value last month?",
    "Revenue by store last month",
    "Revenue per store last month",
    "Revenue for each store last month",
    "Show orders by store last week.",
    "Show me orders by week for the last 4 weeks.",
    "Food revenue vs drink revenue last month",
    "Monthly revenue vs prior year",
    "Orders vs revenue by month",
    "Which 3 stores had the most revenue last month?",
    "List revenue by store",
    # "who" after a noun is a relative pronoun: the total of a filtered set.
    "Revenue from customers who are new",
]


@pytest.mark.parametrize("question", STAY_OK)
def test_an_answer_of_the_asked_shape_stays_ready(jaffle: Runtime, question: str) -> None:
    payload = plan_payload(jaffle, intent=question)

    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]


def _reference(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(runtime.db_path, read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("question", "partial", "sql"),
    [
        (
            "How many orders last week?",
            {},
            f"SELECT count(DISTINCT order_id) FROM jaffle_order {LAST_WEEK}",
        ),
        (
            "Revenue for each store last month",
            {},
            "SELECT s.store_name, sum(o.order_total_cents) / 100.0 FROM jaffle_order o "
            "JOIN jaffle_store s USING (store_id) WHERE o.ordered_at >= TIMESTAMP '2017-07-01' "
            "AND o.ordered_at < TIMESTAMP '2017-08-01' GROUP BY 1 ORDER BY 1",
        ),
        # Each question for a value has a select of its own that names what it asks about.
        (
            BOTH,
            _selects(ORDERS, REVENUE),
            "SELECT count(DISTINCT order_id), sum(order_total_cents) / 100.0 FROM jaffle_order "
            f"{LAST_WEEK}",
        ),
        # The caller's group_by states the rows "who" asks for: a key dimension of an entity
        # that declares no values.
        (
            "Who ordered last week?",
            {"group_by": [CUSTOMER_NAME]},
            "SELECT c.customer_name, count(DISTINCT o.order_id) FROM jaffle_order o "
            "JOIN jaffle_customer c USING (customer_id) WHERE o.ordered_at >= TIMESTAMP "
            "'2017-08-14' AND o.ordered_at < TIMESTAMP '2017-08-21' GROUP BY 1",
        ),
        (
            "Who are our customers?",
            {"group_by": [CUSTOMER_NAME]},
            "SELECT customer_name, count(DISTINCT customer_id) FROM jaffle_customer GROUP BY 1",
        ),
    ],
)
def test_a_ready_answer_equals_its_reference(
    jaffle: Runtime, question: str, partial: dict[str, Any], sql: str
) -> None:
    payload = plan_payload(
        jaffle, intent=question, partial_query={**partial, "policy_context": NOW}
    )
    assert payload["status"] == "ok", payload.get("why")

    def plain(row: Any) -> tuple[Any, ...]:
        # The window's one bucket is no part of the answer; money compares to the cent.
        return tuple(
            round(float(value), 2) if isinstance(value, (float, Decimal)) else value
            for value in row
            if not isinstance(value, datetime)
        )

    rows = typed_rows(jaffle.query({**payload["best"]["query_ir"], "policy_context": NOW}))
    values = sorted(plain(row.values()) for row in rows)
    assert values == sorted(plain(row) for row in _reference(jaffle, sql))
    assert values


# Held because a word carries meaning: a catalog name, a grouping, a value plan can't find, or a
# number. Asking again without it changes the question, so no hint offers that.
MEANINGFUL = [
    "Which customers ordered last week?",
    "List the customers who ordered last week.",
    "How many orders did we get last week compared with the week before?",
    "Orders by store last week compared with the week before?",
    "How many orders did each store get last week?",
    "How many orders did each plan get last week?",
    "Orders between 9 and 17 on 15 March 2017",
]


@pytest.mark.parametrize("question", MEANINGFUL)
def test_no_hint_offers_to_drop_a_word_that_carries_meaning(jaffle: Runtime, question: str) -> None:
    payload = plan_payload(jaffle, intent=question)

    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == UNMATCHED
    assert payload["why"]["details"]["terms"]
    assert _hints(payload)
    assert not [hint for hint in _hints(payload) if DROP_HINT.search(hint)]


@pytest.mark.parametrize("question", MEANINGFUL[:-1])
def test_dropping_those_words_anyway_is_still_held(jaffle: Runtime, question: str) -> None:
    """An agent that drops them anyway keeps the shape words, and the shape check holds it,
    grouped or not."""

    held = plan_payload(jaffle, intent=question)
    retry = plan_payload(jaffle, intent=_without(question, held["why"]["details"]["terms"]))

    assert retry["status"] == "low_confidence"
    assert "ready_for" not in retry["next"]


@pytest.mark.parametrize(
    ("question", "status"),
    [
        ("What's revenue last month?", "ok"),
        # Asked again, plan sees that "the store's revenue" never asks to group by store.
        ("What's the store's revenue last month?", "low_confidence"),
    ],
)
def test_only_a_contraction_end_may_be_dropped(jaffle: Runtime, question: str, status: str) -> None:
    held = plan_payload(jaffle, intent=question)
    assert held["status"] == "low_confidence"
    assert held["why"]["details"] == {"terms": ["s"], "kind": "filter_values_unrealized"}
    assert [hint for hint in _hints(held) if DROP_HINT.search(hint)]

    # The retry the hint offers is the same question: every check reads it again, and a ready
    # retry is the held draft.
    retry = plan_payload(jaffle, intent=_without(question, ["s"]))
    assert retry["status"] == status
    assert retry["best"]["query_ir"] == held["best"]["query_ir"]


@pytest.mark.parametrize(
    ("question", "terms"),
    [
        ("What's revenue from blorps last month?", {"s", "blorps"}),
        # Without its "t", "can't" says the opposite.
        ("Revenue we can't collect last month", {"t"}),
    ],
)
def test_a_meaningful_word_or_tail_is_not_offered(
    jaffle: Runtime, question: str, terms: set[str]
) -> None:
    payload = plan_payload(jaffle, intent=question)

    assert payload["status"] == "low_confidence"
    assert set(payload["why"]["details"]["terms"]) == terms
    assert _hints(payload)
    assert not [hint for hint in _hints(payload) if DROP_HINT.search(hint)]


@pytest.mark.parametrize(
    ("question", "term", "tail"),
    [
        ("What's revenue?", "s", True),
        ("the store's revenue", "s", True),
        ("What’s revenue?", "s", True),
        ("revenue s", "s", False),
        ("What's revenue for s?", "s", False),
        ("revenue by plan", "plan", False),
        ("revenue", "s", False),
        # Only "s" is a tail: "t" carries a negation, and plan reads no other.
        ("Revenue we can't collect last month", "t", False),
        ("We aren’t paid", "t", False),
        ("We'll see revenue", "ll", False),
    ],
)
def test_a_contraction_end_follows_an_apostrophe_inside_a_word(
    question: str, term: str, tail: bool
) -> None:
    assert plan_module._contraction_tail(question, term) is tail


def test_a_shape_word_inside_a_declared_name_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Comparison cost" names a measure, so its "comparison" asks for no comparison."""

    question = "Comparison cost in January 2026"
    runtime = _upkeep(tmp_path / "incident", "incident", "comparison")
    try:
        payload = plan_payload(runtime, intent=question)
        monkeypatch.setattr(plan_module, "_declared_name_spans", lambda *_args: {})
        unnamed = plan_payload(runtime, intent=question)
    finally:
        runtime.close()

    assert payload["status"] == "ok", payload.get("why")
    assert payload["best"]["query_ir"]["select"] == [
        {"as": "comparison_cost", "expression": {"metric": "metric.upkeep.comparison_cost"}}
    ]
    # Read outside the name, the same word would hold the one value.
    assert [gap["kind"] for gap in _gaps(unnamed)] == ["comparison_unrealized"]
