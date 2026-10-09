"""A ready plan's result has the shape of the answer the question asks for.

The invariant: plan calls a draft ready only if its result holds the part each of the
question's shape words asks for. "who", "which" or "list" asks for the rows of the entity its
clause names, so the group_by needs that entity's declared key; "each" needs a row per item; a
comparison ("compared with", "vs", "up or down") needs a prior-period select; and two questions
for a value ("how many ... and how much ...") need a select of their own each, which names what
the question asks about. A time grain, a category, a name (which can repeat), another entity's
grouping or a second select never stands in. Each question below was `ok` without that part, and
so was each question an agent got by asking again without the words a hold named, as its hint
said. The check runs after every other one and only holds a plan; a hint offers to ask again
without words only when they are the "s" ending a contraction ("what's").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.planner import answer_shape, intent_holds, plan_payload
from semantic_rails.planner import plan as plan_module
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_listed_groupings import _upkeep

GAP = "PLAN_INTENT_COVERAGE_GAP"
UNMATCHED = "PLAN_UNMATCHED_TERMS"
# A Monday: last week is 2017-08-14 to 2017-08-20, last month is July 2017.
NOW = {"now": "2017-08-21"}
DROP_HINT = re.compile(r"\bask again without\b", re.IGNORECASE)
CUSTOMER_ID = "dimension.jaffle_customer_id"
CUSTOMER_NAME = "dimension.jaffle_customer_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER_NUMBER = "dimension.jaffle_order_customer_order_number"
STORE_ID = "dimension.jaffle_store_id"
STORE_NAME = "dimension.jaffle_store_name"
STORE_TAX_RATE = "dimension.jaffle_store_tax_rate"
ORDERS = {"measure": "measure.jaffle.order_count"}
REVENUE = {"measure": "measure.jaffle.revenue_usd"}
LAST_WEEK = "WHERE ordered_at >= TIMESTAMP '2017-08-14' AND ordered_at < TIMESTAMP '2017-08-21'"
COMPARED = "Orders by store name last week compared with the week ?"
STORE_REVENUE_LAST_MONTH = (
    "SELECT s.store_id, s.store_name, sum(o.order_total_cents) / 100.0 "
    "FROM jaffle_order o JOIN jaffle_store s USING (store_id) WHERE o.ordered_at >= "
    "TIMESTAMP '2017-07-01' AND o.ordered_at < TIMESTAMP '2017-08-01' GROUP BY 1, 2"
)


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


# Questions whose clauses plan can't plan alone: a part points back ("them"), names nothing, or
# lacks the window another part states. The parts hold keeps the whole question's hold
# (test_plan_question_parts.py).
PARTS_HELD = {
    "How many orders last week and who placed them?",
    "How many orders and how much revenue last week?",
    "Last week, how many orders, how many , and what was the revenue?",
}


@pytest.mark.parametrize(("question", "kind", "clause"), HELD)
def test_one_value_never_answers_a_question_asking_for_more(
    jaffle: Runtime, question: str, kind: str, clause: str
) -> None:
    payload = plan_payload(jaffle, intent=question, partial_query={"policy_context": NOW})

    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    why = payload["why"]
    if question in PARTS_HELD:
        assert why["code"] == "PLAN_PARTS_HELD"
        # The kept draft's gaps stay where a single question's are.
        assert why["details"]["gaps"] == why["details"]["question_why"]["details"]["gaps"]
        why = why["details"]["question_why"]
    assert why["code"] == GAP
    [gap] = why["details"]["gaps"]
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
        partial_query={"group_by": [CUSTOMER_ID, CUSTOMER_NAME]},
    )

    assert payload["status"] == "ok"
    assert payload["best"]["query_ir"]["group_by"] == [CUSTOMER_ID, CUSTOMER_NAME]


BOTH = "How many orders and how much revenue last week?"
BOTH_CLAUSE = '"how many", "how much"'
# Each was `ok` with more than one value, which answered a narrower question.
NARROWER = [
    # A time grain's rows, or another entity's, are not the rows of the entity the clause lists.
    ("List customers by month", {}, "list_unrealized", '"list"'),
    ("Who are our customers by store name?", {}, "list_unrealized", '"who"'),
    ("Who ordered last week by store name?", {}, "list_unrealized", '"who"'),
    ("Who ordered last week by store?", {}, "list_unrealized", '"who"'),
    # A category declares its values: its rows list none of the entity's.
    ("Who are our customers?", {"group_by": [CUSTOMER_TYPE]}, "list_unrealized", '"who"'),
    ("Who ordered last week?", {"group_by": [CUSTOMER_TYPE]}, "list_unrealized", '"who"'),
    # Only the entity's key lists its rows. A name can repeat, so customers who share one
    # would be one row; and another dimension of the entity (an order's number in its
    # customer's sequence) lists no rows of it either.
    ("List customers", {"group_by": [CUSTOMER_NAME]}, "list_unrealized", '"list"'),
    ("Who are our customers?", {"group_by": [CUSTOMER_NAME]}, "list_unrealized", '"who"'),
    ("Who ordered last week?", {"group_by": [CUSTOMER_NAME]}, "list_unrealized", '"who"'),
    ("Who ordered last week?", {"group_by": [ORDER_NUMBER]}, "list_unrealized", '"who"'),
    (
        "Which 3 stores had the most revenue in July 2017?",
        {"group_by": [STORE_NAME]},
        "list_unrealized",
        '"which"',
    ),
    ("List revenue by store name", {}, "list_unrealized", '"list"'),
    # A group_by splits one value: it compares it with nothing.
    (COMPARED, {}, "comparison_unrealized", '"compared"'),
    ("Compare revenue by store name last month", {}, "comparison_unrealized", '"compare"'),
    (
        "Orders by store last week compared with the week ?",
        {},
        "comparison_unrealized",
        '"compared"',
    ),
    ("Compare revenue by store last month", {}, "comparison_unrealized", '"compare"'),
    # Only a prior-period select is a value to compare with. A second select may spell the
    # first one again, and two values don't say what the question compares.
    (
        COMPARED,
        _selects(ORDERS, {"kind": "measure", **ORDERS}),
        "comparison_unrealized",
        '"compared"',
    ),
    (
        COMPARED,
        _selects(ORDERS, {"kind": "measure_ref", **ORDERS}),
        "comparison_unrealized",
        '"compared"',
    ),
    (COMPARED, _selects(ORDERS, REVENUE), "comparison_unrealized", '"compared"'),
    ("Food revenue vs drink revenue last month", {}, "comparison_unrealized", '"vs"'),
    ("food vs drink revenue share by month", {}, "comparison_unrealized", '"vs"'),
    ("Orders vs revenue by month", {}, "comparison_unrealized", '"vs"'),
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
    ("question", "partial", "group_by", "keys"),
    [
        ("List customers by month", {}, None, [CUSTOMER_ID]),
        ("Who are our customers by store name?", {}, [STORE_NAME], [CUSTOMER_ID]),
        # The name may sit beside the key, but never stands for it.
        ("List customers", {"group_by": [CUSTOMER_NAME]}, [CUSTOMER_NAME], [CUSTOMER_ID]),
        (
            "Which 3 stores had the most revenue in July 2017?",
            {"group_by": [STORE_NAME]},
            [STORE_NAME],
            [STORE_ID],
        ),
    ],
)
def test_a_list_hold_names_the_listed_entitys_key_dimensions(
    jaffle: Runtime,
    question: str,
    partial: dict[str, Any],
    group_by: list[str] | None,
    keys: list[str],
) -> None:
    payload = plan_payload(
        jaffle, intent=question, partial_query={**partial, "policy_context": NOW}
    )

    [gap] = _gaps(payload)
    assert gap["expected"] == {"answer": "rows", "key_dimensions": keys}
    assert payload["best"]["query_ir"].get("group_by") == group_by


@pytest.mark.parametrize(
    ("question", "group_by", "listed"),
    [
        ("Which stores had the most revenue last month?", [STORE_ID], True),
        ("Which stores had the most revenue last month?", [STORE_ID, STORE_NAME], True),
        ("Which stores had the most revenue last month?", [STORE_NAME], False),
        ("Which stores had the most revenue last month?", [STORE_TAX_RATE], False),
        ("Who ordered last week?", [CUSTOMER_ID, CUSTOMER_NAME], True),
        ("Who ordered last week?", [CUSTOMER_NAME], False),
        ("Who ordered last week?", [ORDER_NUMBER], False),
    ],
)
def test_only_the_listed_entitys_key_lists_its_rows(
    jaffle: Runtime, question: str, group_by: list[str], listed: bool
) -> None:
    """The check, read on a draft grouped as the caller states. Plan holds the store listings
    without a limit before it, too: it adds the store name and can't tell what ranks them."""

    partial = {"group_by": group_by}
    why = answer_shape._answer_shape_why(
        jaffle, question, {**_selects(REVENUE), **partial}, partial
    )

    assert (why is None) is listed
    if not listed:
        assert why is not None
        assert [gap["kind"] for gap in why["details"]["gaps"]] == ["list_unrealized"]
        payload = plan_payload(
            jaffle, intent=question, partial_query={**partial, "policy_context": NOW}
        )
        assert payload["status"] == "low_confidence"
        assert "ready_for" not in payload["next"]


def test_a_category_that_declares_no_values_lists_no_rows(
    runtime_factory: Callable[[str], Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without its declared values, a customer's type still groups customers into types."""

    runtime = runtime_factory("jaffle_shop")
    try:
        config = runtime._config
        dimensions = [
            replace(row, value_domain="") if row.id == CUSTOMER_TYPE else row
            for row in config.dimensions
        ]
        monkeypatch.setattr(runtime, "_config", replace(config, dimensions=dimensions))
        partial = {"group_by": [CUSTOMER_TYPE]}
        payload = plan_payload(runtime, intent="Who are our customers?", partial_query=partial)
        monkeypatch.setattr(plan_module, "_answer_shape_why", lambda *_args: None)
        unchecked = plan_payload(runtime, intent="Who are our customers?", partial_query=partial)
    finally:
        runtime.close()

    assert payload["status"] == "low_confidence"
    [gap] = _gaps(payload)
    assert (gap["kind"], gap["expected"]) == (
        "list_unrealized",
        {"answer": "rows", "key_dimensions": [CUSTOMER_ID]},
    )
    assert payload["best"]["query_ir"]["group_by"] == [CUSTOMER_TYPE]
    # Only the shape check holds it.
    assert unchecked["status"] == "ok"


# Questions a value or rows of the asked shape answer, which stay `ok`.
STAY_OK = [
    "How many orders last week?",
    "What was revenue last month?",
    "How much revenue did we make last month?",
    "What is the average order value last month?",
    "Revenue by store name last month",
    "Revenue for each store last month",
    "Show orders by store name last week.",
    "Show me orders by week for the last 4 weeks.",
    # A prior-period select is a value to compare with.
    "Monthly revenue vs prior year",
    # "who" after a noun is a relative pronoun: the total of a filtered set.
    "Revenue from customers who are new",
]


@pytest.mark.parametrize("question", STAY_OK)
def test_an_answer_of_the_asked_shape_stays_ready(jaffle: Runtime, question: str) -> None:
    partial = {
        "Revenue for each store last month": {"group_by": [STORE_NAME]},
        # A comparison is ready only over months that have ended
        # (test_plan_period_completeness.py).
        "Monthly revenue vs prior year": {"time": {"end": "2018-01-01"}},
    }.get(question, {})
    payload = plan_payload(jaffle, intent=question, partial_query=partial)

    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]


@pytest.mark.parametrize(
    ("question", "code"),
    [
        ("Who are our customers by store?", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
        ("Revenue per store last month", "PLAN_FALLBACK_SEMANTIC_DRIFT"),
        ("Orders by store last week compared with the week before?", UNMATCHED),
        ("What's the store's revenue last month?", UNMATCHED),
    ],
)
def test_store_shortcut_removal_holds_before_the_shape_check(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, question: str, code: str
) -> None:
    payload = plan_payload(jaffle, intent=question, partial_query={"policy_context": NOW})
    monkeypatch.setattr(plan_module, "_answer_shape_why", lambda *_args: None)
    unchecked = plan_payload(jaffle, intent=question, partial_query={"policy_context": NOW})

    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == code
    assert payload == unchecked
    assert not [hint for hint in _hints(payload) if DROP_HINT.search(hint)]


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
            {"group_by": [STORE_NAME]},
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
        # The caller's group_by states the rows "who" asks for: the customer's key, with the
        # name beside it.
        (
            "Who ordered last week?",
            {"group_by": [CUSTOMER_ID, CUSTOMER_NAME]},
            "SELECT c.customer_id, c.customer_name, count(DISTINCT o.order_id) "
            "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) WHERE o.ordered_at "
            ">= TIMESTAMP '2017-08-14' AND o.ordered_at < TIMESTAMP '2017-08-21' GROUP BY 1, 2",
        ),
        # The stores' key lists the stores a ranking asks for.
        (
            "Which 3 stores had the most revenue in July 2017?",
            {"group_by": [STORE_ID, STORE_NAME]},
            "SELECT s.store_id, s.store_name, sum(o.order_total_cents) / 100.0 "
            "FROM jaffle_order o JOIN jaffle_store s USING (store_id) WHERE o.ordered_at >= "
            "TIMESTAMP '2017-07-01' AND o.ordered_at < TIMESTAMP '2017-08-01' GROUP BY 1, 2 "
            "ORDER BY 3 DESC LIMIT 3",
        ),
        # "what's" reads as "what is" before any check.
        (
            "What's revenue last month?",
            {},
            "SELECT sum(order_total_cents) / 100.0 FROM jaffle_order WHERE ordered_at >= "
            "TIMESTAMP '2017-07-01' AND ordered_at < TIMESTAMP '2017-08-01'",
        ),
        # "store" names the Store entity: its key, with its one naming dimension beside it.
        *(
            (question, {}, STORE_REVENUE_LAST_MONTH)
            for question in ("Revenue by store last month", "Revenue for each store last month")
        ),
        (
            "Which 3 stores had the most revenue in July 2017?",
            {},
            f"{STORE_REVENUE_LAST_MONTH} ORDER BY 3 DESC LIMIT 3",
        ),
        (
            "List revenue by store",
            {},
            "SELECT s.store_id, s.store_name, sum(o.order_total_cents) / 100.0 "
            "FROM jaffle_order o JOIN jaffle_store s USING (store_id) GROUP BY 1, 2",
        ),
        *(
            (
                question,
                {},
                "SELECT s.store_id, s.store_name, count(DISTINCT o.order_id) FROM jaffle_order o "
                f"JOIN jaffle_store s USING (store_id) {LAST_WEEK.replace('ordered_at', 'o.ordered_at')} "
                "GROUP BY 1, 2",
            )
            for question in (
                "Show orders by store last week.",
                "How many orders did each store get last week?",
            )
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


@pytest.mark.parametrize("question", ["List customers", "Who are our customers?"])
def test_a_listing_by_key_keeps_customers_who_share_a_name(jaffle: Runtime, question: str) -> None:
    payload = plan_payload(
        jaffle,
        intent=question,
        partial_query={"group_by": [CUSTOMER_ID, CUSTOMER_NAME], "policy_context": NOW},
    )
    assert payload["status"] == "ok", payload.get("why")

    rows = typed_rows(jaffle.query({**payload["best"]["query_ir"], "policy_context": NOW}))
    listed = sorted((row[CUSTOMER_ID], row[CUSTOMER_NAME]) for row in rows)
    assert listed == sorted(
        _reference(jaffle, "SELECT customer_id, customer_name FROM jaffle_customer")
    )
    assert {row["customer_count"] for row in rows} == {1}
    # Some customers share a name: listed by name alone, they would be one row.
    assert len({name for _key, name in listed}) < len(listed)


# Held because a word carries meaning: a catalog name, a grouping, a value plan can't find, or a
# number. Asking again without it changes the question, so no hint offers that.
MEANINGFUL = [
    "Which customers ordered last week?",
    "List the customers who ordered last week.",
    "How many orders did we get last week compared with the week before?",
    "Orders by store name last week compared with the week before?",
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


@pytest.mark.parametrize("question", ["What's revenue last month?", "What’s revenue last month?"])
def test_a_contraction_is_expanded_before_any_check(jaffle: Runtime, question: str) -> None:
    # Plan reads "what's" as "what is" before every check, so its "s" is never an unmatched
    # word, and the draft is the one asking again without the "s" gets.
    plan = plan_payload(jaffle, intent=question)
    assert plan["intent"] == "What is revenue last month?"
    assert plan["status"] == "ok", plan.get("why")
    assert plan["next"]["ready_for"] == ["execute"]

    retry = plan_payload(jaffle, intent=_without(question, ["s"]))
    assert retry["status"] == "ok"
    assert retry["best"]["query_ir"] == plan["best"]["query_ir"]


@pytest.mark.parametrize(
    ("question", "code", "named"),
    [
        # "what's" reads as "what is"; "blorps" still carries meaning.
        ("What's revenue from blorps last month?", UNMATCHED, {"blorps"}),
        # "can't" reads as "can not": a negation the draft lacks, never a tail to drop.
        ("Revenue we can't collect last month", GAP, {"negation_unrealized"}),
    ],
)
def test_a_meaningful_word_or_tail_is_not_offered(
    jaffle: Runtime, question: str, code: str, named: set[str]
) -> None:
    payload = plan_payload(jaffle, intent=question)

    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == code
    # The words the hold names, or the kinds of the gaps it reports.
    details = payload["why"]["details"]
    assert set(details.get("terms") or [gap["kind"] for gap in _gaps(payload)]) == named
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
    assert intent_holds._contraction_tail(question, term) is tail


def test_a_shape_word_inside_a_declared_name_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Comparison cost" names a measure, so its "comparison" asks for no comparison."""

    question = "Comparison cost in January 2026"
    runtime = _upkeep(tmp_path / "incident", "incident", "comparison")
    try:
        payload = plan_payload(runtime, intent=question)
        monkeypatch.setattr(answer_shape, "_declared_name_spans", lambda *_args, **_kwargs: {})
        unnamed = plan_payload(runtime, intent=question)
    finally:
        runtime.close()

    assert payload["status"] == "ok", payload.get("why")
    assert payload["best"]["query_ir"]["select"] == [
        {"as": "comparison_cost", "expression": {"metric": "metric.upkeep.comparison_cost"}}
    ]
    # Read outside the name, the same word would hold the one value.
    assert [gap["kind"] for gap in _gaps(unnamed)] == ["comparison_unrealized"]
