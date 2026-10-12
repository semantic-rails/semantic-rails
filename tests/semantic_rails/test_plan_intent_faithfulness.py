"""Fail-closed checks for intent clauses that validate cannot prove."""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.planner_fixtures import jaffle_runtime
from tests.semantic_rails.result_helpers import assert_plan_held, disable_planner_patterns

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _gap_kinds(payload: dict) -> set[str]:
    why = payload.get("why") or {}
    details = why.get("details") or {}
    return {str(row.get("kind")) for row in details.get("gaps") or []}


@pytest.mark.parametrize(
    ("intent", "expected_gap"),
    [
        (
            "Top 3 customers by revenue within each store",
            "partitioned_ranking_unrealized",
        ),
        (
            "Revenue by store for customers with at least 4 orders in the last 90 days "
            "compared with prior year",
            "prior_period_comparison_unrealized",
        ),
        (
            "Revenue and order count by store last quarter",
            "multiple_subjects_unrealized",
        ),
        # The draft drops Brooklyn, but not the other product types.
        (
            "Food or drink orders by store excluding Brooklyn",
            "filter_values_unrealized",
        ),
    ],
)
def test_validating_but_unfaithful_complex_plans_fail_closed(
    runtime, monkeypatch: pytest.MonkeyPatch, intent: str, expected_gap: str
) -> None:
    monkeypatch.setattr(runtime, "_config", runtime._config)  # restored after the test
    disable_planner_patterns(runtime, "conjoined_metrics")
    payload = plan_payload(runtime, intent=intent)

    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert expected_gap in _gap_kinds(payload)
    assert payload["best"]["validation_ok"] is True
    assert "ready_for" not in payload["next"]
    assert payload["why"]["recovery_hints"]


def test_conjoined_store_quarter_answers_with_the_keyed_store(runtime) -> None:
    # "store" names the Store entity, so the conjoined subjects group by its key and name.
    now = {"now": "2017-04-15"}
    payload = plan_payload(
        runtime,
        intent="Revenue and order count by store last quarter",
        partial_query={"policy_context": now},
    )
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert query["group_by"] == ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
    rows = runtime.query(query, now=now["now"])["rows"]
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        reference = connection.execute(
            "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0), "
            "COUNT(DISTINCT o.order_id) FROM jaffle_order o JOIN jaffle_store s USING "
            "(store_id) WHERE o.ordered_at >= TIMESTAMP '2017-01-01' AND o.ordered_at < "
            "TIMESTAMP '2017-04-01' GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
    actual = [
        (
            row["dimension.jaffle_store_id"],
            row["dimension.jaffle_store_name"],
            round(float(row["revenue_usd"]), 2),
            row["order_count"],
        )
        for row in rows
    ]
    assert reference
    assert actual == [
        (key, name, round(float(revenue), 2), count) for key, name, revenue, count in reference
    ]


@pytest.mark.parametrize(
    ("intent", "pattern", "status", "code", "named"),
    [
        # A comparison with the prior year asks which reading to run first.
        (
            "revenue vs prior year by store",
            "inline_period_shift",
            "needs_clarification",
            "PERIOD_COMPARISON",
            None,
        ),
        ("orders by store and month", "metric_by_dimension_rollup", "ok", None, None),
        ("orders by store in Brooklyn", "metric_by_dimension_rollup", "ok", None, None),
        # "For stores that have ..." qualifies the stores; the draft also splits by store.
        (
            "Give me the 28D adoption funnel from signup to Send for stores that have an "
            "order rate of over 90% grouped by month",
            "filtered_adoption_funnel",
            "low_confidence",
            "PLAN_UNASKED_GROUPING",
            ["Store name"],
        ),
        # One total per store and no prior-period select: the answer-shape check holds it.
        (
            "food revenue share vs drink revenue share by store",
            "inline_comparison",
            "low_confidence",
            "PLAN_INTENT_COVERAGE_GAP",
            ["comparison_unrealized"],
        ),
        (
            "revenue vs order count by store name last quarter",
            "inline_comparison",
            "low_confidence",
            "PLAN_INTENT_COVERAGE_GAP",
            ["comparison_unrealized"],
        ),
    ],
)
def test_faithfulness_gate_preserves_realized_and_supported_shapes(
    runtime,
    intent: str,
    pattern: str,
    status: str,
    code: str | None,
    named: list[str] | None,
) -> None:
    payload = plan_payload(runtime, intent=intent)

    assert payload["best"]["pattern"] == pattern
    assert payload["status"] == status, payload.get("why")
    if status == "ok":
        assert payload["next"]["ready_for"] == ["execute"]
        if "Brooklyn" in intent:
            # A positive value filter is not mistaken for a negation.
            assert payload["best"]["query"]["where"] == [
                {"field": "dimension.jaffle_store_name", "op": "is", "value": "Brooklyn"}
            ]
        return
    # The faithfulness gate keeps the shape; another check holds it and names why.
    assert payload["why"]["code"] == code
    assert "ready_for" not in payload["next"]
    if code == "PLAN_UNASKED_GROUPING":
        assert payload["why"]["details"]["unasked_groupings"] == named
    elif code == "PLAN_INTENT_COVERAGE_GAP":
        assert sorted(_gap_kinds(payload)) == named


def test_qualifier_on_another_clock_is_not_offered_as_ready(runtime) -> None:
    # The stores' qualifier reads all their rows; the snapshot still has no as-of day.
    payload = plan_payload(
        runtime,
        intent="Give me the sum of active menu snapshot at the store dimension for stores "
        "that have done more than one order and more than one session grouped by month",
    )

    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "stock_as_of_unrealized" in _gap_kinds(payload)
    assert "ready_for" not in payload["next"]


def test_a_lifetime_qualifier_reads_every_row_of_the_entity(runtime) -> None:
    # "Lifetime orders" is the customer's count over all time, whatever the orders' months.
    now = "2017-09-15"
    payload = plan_payload(
        runtime, intent="monthly orders for customers with at least 10 lifetime orders", now=now
    )
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    [qualifier] = query["where"]
    assert (qualifier["entity"], qualifier["over"]) == ("entity.jaffle_customer", "all_time")
    rows = runtime.query(query, now=now)["rows"]
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        reference = connection.execute(
            "SELECT DATE_TRUNC('month', o.ordered_at), COUNT(DISTINCT o.order_id) "
            "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
            "WHERE c.lifetime_order_count >= 10 GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert reference
    assert [(str(row["period"])[:10], row["order_count"]) for row in rows] == [
        (str(month)[:10], count) for month, count in reference
    ]


@pytest.fixture(scope="module")
def trailing_window_runtime(tmp_path_factory):
    """A snapshot stock labelled with its own trailing window, like a vendor's 14-day uniques."""
    import duckdb

    package = tmp_path_factory.mktemp("subject_window") / "f4win"
    (package / "models").mkdir(parents=True)
    (package / "metrics").mkdir()
    (package / "data").mkdir()
    (package / "package.yml").write_text(
        "schema_version: 1\npackage: {id: f4win, namespace: f4win, name: f4win, "
        "warehouse: duckdb, default_db: data/f4win.duckdb, seed: {kind: external}, "
        "environments: [development]}\n"
    )
    (package / "graph.yml").write_text(
        "graph:\n  entities:\n    repo_snapshot: {key: [repo, snapshot_date], "
        "model: repo_snapshots, allowed_as_root: true}\n"
    )
    (package / "models" / "repo_snapshots.yml").write_text(
        "model:\n  id: repo_snapshots\n  relation: repo_snapshot\n  entities: {repo_snapshot: {}}\n"
        "  times:\n    snapshot_date: {column: snapshot_date, kind: date, class: as_of_time, "
        "default: true}\n"
        "  measures:\n    visitors_14d: {label: Unique visitors (14 days), kind: aggregate, "
        "expr: visitors_14d, accumulation: {kind: stock, snapshot: end_of_period}, "
        "value_type: count}\n"
    )
    (package / "metrics" / "metrics.yml").write_text(
        # The metric's label leaves the span out; only its id and its measure state it.
        "metrics:\n  unique_visitors_14d: {label: Unique visitors, "
        "description: Distinct visitors in the trailing 14-day window as of the snapshot., "
        "kind: semi_additive, measure: visitors_14d, value_type: count, "
        "temporal_role: temporal_role.f4win_repo_snapshot_snapshot_date}\n"
    )
    connection = duckdb.connect(str(package / "data" / "f4win.duckdb"))
    connection.execute(
        "create table repo_snapshot as select * from (values "
        "('a', date '2026-09-21', 4), ('a', date '2026-09-22', 4)) "
        "t(repo, snapshot_date, visitors_14d)"
    )
    connection.close()
    runtime = Runtime.from_path(str(package))
    try:
        yield opened(runtime)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "status"),
    [
        # A 14-day count bucketed or bounded by week is not the week's unique visitors;
        # each row covers its bucket, however long the whole window is.
        ("unique visitors by week", "low_confidence"),
        ("unique visitors last week", "low_confidence"),
        ("unique visitors in the last two weeks by week", "low_confidence"),
        ("unique visitors in the last 2 weeks by week", "low_confidence"),
        ("unique visitors in September 2026 by day", "low_confidence"),
        # A window with no bucket: each value covers the window (7 and 30 days, never 14).
        ("unique visitors this week", "low_confidence"),
        ("unique visitors in September 2026", "low_confidence"),
        # Nothing the question says turns the check off: these rows are 14-day counts too.
        ("unique visitors over 14 days by week", "low_confidence"),
        ("rolling unique visitors by week", "low_confidence"),
        ("unique visitors trailing 7 days", "low_confidence"),
        # No period asked for: plan reads the stock on the last complete day, and a 14-day
        # count as of that day is still not that day's count.
        ("how many unique visitors", "low_confidence"),
    ],
)
def test_a_subject_with_its_own_window_is_flagged_for_another_period(
    trailing_window_runtime, intent: str, status: str
) -> None:
    payload = plan_payload(trailing_window_runtime, intent=intent)
    assert payload["status"] == status, payload.get("why")
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert "subject_window_mismatch" in _gap_kinds(payload)
    if intent == "how many unique visitors":
        assert payload["best"]["query"]["time"]["grain"] == "day"


def test_rolling_metrics_are_not_checked(runtime) -> None:
    # A rolling metric is read by day as a matter of course; its window is follow-up work.
    payload = plan_payload(runtime, intent="rolling revenue by day")
    assert "subject_window_mismatch" not in _gap_kinds(payload)


@pytest.mark.parametrize(
    ("time", "flagged"),
    [
        # No bucket: each value covers the whole window.
        ({"start": "2026-09-01", "end": "2026-10-01"}, True),
        ({"start": "2026-09-21", "end": "2026-09-28"}, True),
        ({"start": "2026-09-08", "end": "2026-09-22"}, False),
        ({"last": {"unit": "week", "value": 2}}, False),
        # A bucket wins over the window it sits in.
        ({"grain": "week", "start": "2026-09-08", "end": "2026-09-22"}, True),
    ],
)
def test_the_period_is_the_bucket_else_the_whole_window(
    trailing_window_runtime, time: dict, flagged: bool
) -> None:
    from semantic_rails.planner.call import Call
    from semantic_rails.planner.time_checks import _subject_window_gaps

    role = "temporal_role.f4win_repo_snapshot_snapshot_date"
    query = {
        "select": [{"expression": "metric.f4win.unique_visitors_14d", "as": "v"}],
        "time": {"temporal_role": role, **time},
    }
    gaps = _subject_window_gaps(trailing_window_runtime.config, Call.from_call(query))
    assert bool(gaps) is flagged
