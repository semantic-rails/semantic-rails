"""Completed period comparisons keep both dated values and the governed subject."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.ast import normalize_query
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.answer_shape import _answer_shape_why
from semantic_rails.planner.faithfulness import intent_faithfulness_why
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.patterns.period_pair import completed_period_pair
from semantic_rails.planner.time_windows import _time_window
from semantic_rails.planner.unasked_groupings import _unasked_grouping_why
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_metric_vocabulary import SEED, _package
from tests.semantic_rails.test_plan_time_reference import subscriptions  # noqa: F401, F811

NOW = {"now": "2026-10-05T06:00:00Z"}
QUESTION = "How many new accounts last week compared with the week before?"
# Counts settle to zero only inside loaded coverage. These witnesses lie outside every
# compared window, preserving its values while establishing coverage on both sides.
COVERAGE_SEED = """
INSERT INTO events VALUES (7, 'a', 'close', '2026-08-01'),
  (8, 'a', 'signup', '2026-07-01'), (9, 'a', 'signup', '2026-10-05'),
  (10, 'a', 'close', '2026-10-05'), (11, 'a', 'signup', '2023-12-31'),
  (12, 'a', 'close', '2023-12-31');
"""


@pytest.fixture()
def accounts(tmp_path: Path) -> Iterator[Runtime]:
    root = _package(tmp_path, synonyms=True)
    path = root / "models/events.yml"
    doc = yaml.safe_load(path.read_text())
    doc["model"]["dimensions"]["plan_before"] = {
        "column": "segment",
        "label": "Plan before",
        "kind": "categorical",
    }
    path.write_text(yaml.safe_dump(doc))
    with duckdb.connect(str(root / "shop.duckdb")) as connection:
        connection.execute(COVERAGE_SEED)
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def _draft() -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"as": "value", "expression": {"metric": "metric.shop.new_accounts"}}],
        "time": {
            "temporal_role": "temporal_role.shop_event_occurred_at",
            "grain": "week",
            "range": {"last": {"unit": "week", "value": 2}},
            "fill": True,
        },
        "order_by": [{"field": "time", "direction": "ASC"}],
        "policy_context": NOW,
    }


@pytest.mark.parametrize(
    ("question", "subject", "unit", "start", "end", "expected"),
    [
        (QUESTION, "new_accounts", "week", "2026-09-21", "2026-10-05", [1, 1]),
        (
            "Were closures up or down last week compared with the week before?",
            "closures",
            "week",
            "2026-09-21",
            "2026-10-05",
            [0, 1],
        ),
        (
            "new accounts last week vs the previous week",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "new accounts last week compared to the prior week",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "new accounts last week versus the week prior",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "new accounts last week against the week before",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "New accounts by week last week vs the week before",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "new accounts last week, up or down",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "signups last week vs the week before",
            "new_accounts",
            "week",
            "2026-09-21",
            "2026-10-05",
            [1, 1],
        ),
        (
            "new accounts last month vs the month before",
            "new_accounts",
            "month",
            "2026-08-01",
            "2026-10-01",
            [0, 3],
        ),
        (
            "new accounts yesterday vs the day before",
            "new_accounts",
            "day",
            "2026-10-03",
            "2026-10-05",
            [0, 0],
        ),
        (
            "new accounts last day vs the previous day",
            "new_accounts",
            "day",
            "2026-10-03",
            "2026-10-05",
            [0, 0],
        ),
        (
            "new accounts last quarter vs the quarter before",
            "new_accounts",
            "quarter",
            "2026-04-01",
            "2026-10-01",
            [0, 4],
        ),
        (
            "new accounts last year vs the prior year",
            "new_accounts",
            "year",
            "2024-01-01",
            "2026-01-01",
            [0, 0],
        ),
    ],
)
def test_completed_pairs_match_independent_sql(
    accounts: Runtime,
    question: str,
    subject: str,
    unit: str,
    start: str,
    end: str,
    expected: list[int],
) -> None:
    result = plan_payload(accounts, intent=question, partial_query={"policy_context": NOW})
    assert result["status"] == "ok", result.get("why")
    assert result["best"]["pattern"] == "period_pair"
    assert result["next"]["ready_for"] == ["execute"]
    query = result["best"]["query_ir"]
    assert query["select"][0]["expression"] == {"metric": f"metric.shop.{subject}"}
    time = normalize_query(query, config=accounts._config).time
    assert time and (time.start, time.end) == (start, end)
    assert query["order_by"] == [{"field": "time", "direction": "ASC"}]
    rows = accounts.query(query)["rows"]
    kind = "signup" if subject == "new_accounts" else "close"
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        connection.execute(COVERAGE_SEED)
        reference = connection.execute(f"""
            WITH periods AS (
                SELECT unnest(generate_series(DATE '{start}', DATE '{end}' - INTERVAL '1 {unit}',
                                              INTERVAL '1 {unit}')) AS bucket
            )
            SELECT CAST(p.bucket AS DATE), COUNT(DISTINCT e.event_id)
            FROM periods p LEFT JOIN event_records e
              ON e.occurred_at >= p.bucket AND e.occurred_at < p.bucket + INTERVAL '1 {unit}'
              AND e.kind = '{kind}' AND e.segment = 'customer'
            GROUP BY p.bucket ORDER BY p.bucket
        """).fetchall()
    values = [row[query["select"][0]["as"]] for row in rows]
    assert values == [row[1] for row in reference] == expected
    clock = f"{query['time']['temporal_role']}__{unit}"
    assert [str(row[clock])[:10] for row in rows] == [str(row[0]) for row in reference]
    span = result["best"]["interpreted_intent"]["span"]
    window = _time_window(question, policy_context=NOW)
    assert window.spans == (tuple(span),)
    assert window.bounds == {"range": {"last": {"unit": unit, "value": 2}}}


@pytest.mark.parametrize(
    "question",
    [
        "new accounts last week vs the same week last year",
        "new accounts September vs August 2026",
        "new accounts last week vs the month before",
        "new accounts this week compared with last week",
        "new accounts this month vs the month before",
        "new accounts last week vs the week before last",
        "new accounts last week change from the week before",
        "New accounts by day last week vs the week before",
    ],
)
def test_other_comparisons_stay_held(accounts: Runtime, question: str) -> None:
    assert completed_period_pair(question) is None
    result = plan_payload(accounts, intent=question, partial_query={"policy_context": NOW})
    assert result["status"] == "low_confidence", result
    assert not result["next"].get("ready_for")
    unchanged = {
        "new accounts last week vs the same week last year": "TIME_WINDOW_UNRESOLVED",
        "new accounts September vs August 2026": "TIME_WINDOW_UNRESOLVED",
        "new accounts last week vs the week before last": "PLAN_UNMATCHED_TERMS",
        "new accounts last week change from the week before": "PLAN_UNMATCHED_TERMS",
    }
    if question in unchanged:
        assert result["why"]["code"] == unchanged[question]
        return
    assert result["why"]["code"] == "VALIDATION_FAILED"
    [error] = result["why"]["errors"]
    assert error["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "completed" in error["message"]


def test_stock_pair_stays_held(subscriptions: Runtime) -> None:  # noqa: F811
    result = plan_payload(
        subscriptions,
        intent="MRR last week vs the week before",
        partial_query={"policy_context": NOW},
    )
    assert result["status"] == "low_confidence"
    assert not result["next"].get("ready_for")
    assert result["why"]["code"] == "VALIDATION_FAILED"
    assert result["why"]["errors"][0]["code"] == "PLAN_INTENT_COVERAGE_GAP"


@pytest.mark.parametrize("fill", [False, None], ids=["disabled", "missing"])
def test_order_pair_requires_fill(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, fill: bool | None
) -> None:
    runtime = runtime_factory("jaffle_shop")
    question = "How many orders did we get last week compared with the week before?"
    context = {"now": "2017-08-21"}
    planned = plan_payload(runtime, intent=question, partial_query={"policy_context": context})
    assert planned["status"] == "ok", planned.get("why")
    query = deepcopy(planned["best"]["query_ir"])
    query["policy_context"] = context
    if fill is None:
        del query["time"]["fill"]
    else:
        query["time"]["fill"] = fill
        patched = plan_payload(
            runtime,
            intent=question,
            partial_query={"policy_context": context, "time": {"fill": fill}},
        )
        assert patched["status"] != "ok", patched
        assert "execute" not in patched["next"].get("ready_for", [])
    assert completed_period_pair(question, runtime=runtime, query=query) is None
    draft = RuntimeCompositionDraft(query=query, resolved=[], rationale=[], interpreted_intent={})
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda *_args: SimpleNamespace(
            draft=draft, pattern="period_pair", intent_ir=parse_intent(runtime, question)
        ),
    )
    held = plan_payload(runtime, intent=question, partial_query={"policy_context": context})
    assert held["status"] != "ok", held
    assert "execute" not in held["next"].get("ready_for", [])
    assert held["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"


def test_completed_order_pair_fills_an_empty_week(tmp_path: Path) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    database = package / "jaffle_shop.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("""
            DELETE FROM jaffle_order
            WHERE ordered_at >= DATE '2017-08-07' AND ordered_at < DATE '2017-08-21';
            UPDATE jaffle_order SET ordered_at = DATE '2017-08-14'
            WHERE order_id = (SELECT MIN(order_id) FROM jaffle_order);
        """)
        reference = connection.execute("""
            WITH periods(bucket) AS (VALUES (DATE '2017-08-07'), (DATE '2017-08-14'))
            SELECT p.bucket, COUNT(DISTINCT o.order_id) FROM periods p
            LEFT JOIN jaffle_order o ON o.ordered_at >= p.bucket
              AND o.ordered_at < p.bucket + INTERVAL '1 week'
            GROUP BY p.bucket ORDER BY p.bucket
        """).fetchall()
    runtime = Runtime.from_path(str(package))
    try:
        planned = plan_payload(
            runtime,
            intent="How many orders did we get last week compared with the week before?",
            partial_query={"policy_context": {"now": "2017-08-21"}},
        )
        assert planned["status"] == "ok", planned.get("why")
        assert planned["next"]["ready_for"] == ["execute"]
        query = planned["best"]["query_ir"]
        rows = typed_rows(runtime.query(query))
        clock = f"{query['time']['temporal_role']}__week"
        alias = query["select"][0]["as"]
        actual = [(str(row[clock])[:10], row[alias]) for row in rows]
        assert actual == [(str(bucket), count) for bucket, count in reference] == [
            ("2017-08-07", 0),
            ("2017-08-14", 1),
        ]
    finally:
        runtime.close()


def test_change_from_last_month_keeps_the_time_window_and_hold(runtime_factory: Any) -> None:
    runtime = runtime_factory("jaffle_shop")
    planned = plan_payload(
        runtime,
        intent="How did revenue change from last month?",
        partial_query={"policy_context": {"now": "2017-08-21"}},
    )
    assert planned["status"] == "low_confidence", planned
    assert planned["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert "execute" not in planned["next"].get("ready_for", [])
    assert planned["best"]["query_ir"]["time"]["range"] == {
        "last": {"unit": "month", "value": 1}
    }


@pytest.mark.parametrize(
    ("question", "patch"),
    [
        ("new accounts last week vs the same week last year", {}),
        ("new accounts this week compared with last week", {}),
        ("new accounts last week vs LAST week", {}),
        (QUESTION, {"time": {"range": {"last": {"unit": "week", "value": 3}}}}),
        (QUESTION, {"time": {"grain": "day"}}),
        (QUESTION, {"group_by": ["dimension.shop_event_kind"]}),
        (QUESTION, {"limit": 1}),
        (QUESTION, {"order_by": [{"field": "time", "direction": "DESC"}]}),
        (QUESTION, {"time": {"start": "2026-09-01"}}),
    ],
)
def test_hand_authored_bypasses_keep_the_comparison_guard(
    accounts: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    patch: dict[str, Any],
) -> None:
    query = deepcopy(_draft())
    for key, value in patch.items():
        query[key] = {**query[key], **value} if key == "time" else value
    grouping_why = _unasked_grouping_why(accounts, question, query)
    if question == QUESTION:
        assert grouping_why and grouping_why["code"] == "PLAN_UNASKED_GROUPING"
    else:
        # These words already name a grain on main; the comparison guard still holds.
        assert grouping_why is None
    why = _answer_shape_why(accounts, question, query)
    assert why and why["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "comparison_unrealized" in {gap["kind"] for gap in why["details"]["gaps"]}
    draft = RuntimeCompositionDraft(query=query, resolved=[], rationale=[], interpreted_intent={})
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda *_args: type(
            "Result",
            (),
            {
                "draft": draft,
                "pattern": "period_pair",
                "intent_ir": parse_intent(accounts, question),
            },
        )(),
    )
    result = plan_payload(accounts, intent=question, partial_query={"policy_context": NOW})
    assert result["status"] != "ok"
    assert not result["next"].get("ready_for")


def test_previous_week_faithfulness_uses_the_same_validator(accounts: Runtime) -> None:
    question = "new accounts last week vs the previous week"
    query = _draft()
    assert (
        intent_faithfulness_why(
            accounts, question=question, query=query, intent_ir=parse_intent(accounts, question)
        )
        is None
    )
    query["limit"] = 1
    why = intent_faithfulness_why(
        accounts, question=question, query=query, intent_ir=parse_intent(accounts, question)
    )
    assert why and "prior_period_comparison_unrealized" in {
        gap["kind"] for gap in why["details"]["gaps"]
    }


def test_two_rows_without_a_comparison_keeps_main_status(accounts: Runtime) -> None:
    query = _draft()
    assert _answer_shape_why(accounts, "new accounts last 2 weeks", query) is None
    result = plan_payload(
        accounts, intent="new accounts last 2 weeks", partial_query={"policy_context": NOW}
    )
    assert result["status"] == "low_confidence"
    assert result["why"]["code"] == "PLAN_UNASKED_GROUPING"


def test_day_after_is_not_a_consumed_comparison(accounts: Runtime) -> None:
    result = plan_payload(
        accounts, intent="new accounts the day after", partial_query={"policy_context": NOW}
    )
    assert result["status"] != "ok"
    assert not result["next"].get("ready_for")


def test_bounded_inline_shift_preserves_the_window(accounts: Runtime) -> None:
    from semantic_rails.planner.orchestrator import compose

    result = compose(accounts, "signups last 2 months vs last year", policy_context=NOW)
    assert result.pattern == "inline_period_shift"
    assert result.draft
    assert result.draft.query["time"]["range"] == {"last": {"unit": "month", "value": 2}}
    assert result.draft.query["select"][0]["expression"] == {"metric": "metric.shop.new_accounts"}
    plan = plan_payload(
        accounts, intent="signups last 2 months vs last year", partial_query={"policy_context": NOW}
    )
    assert plan["status"] != "ok"
    assert not plan["next"].get("ready_for")


def test_inline_comparison_selects_a_metric_synonym_first(accounts: Runtime) -> None:
    from semantic_rails.planner.patterns.inline_comparison import _resolve_side

    row = _resolve_side(accounts._config, "signups", prefer_share_metric=False)
    assert row and row[0].id == "metric.shop.new_accounts" and not row[1]
