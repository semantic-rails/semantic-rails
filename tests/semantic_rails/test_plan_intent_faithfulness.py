"""Fail-closed checks for intent clauses that validate cannot prove."""

from __future__ import annotations

import pytest

from semantic_rails.planner import plan_payload
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.result_helpers import assert_plan_held


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
        (
            "Food or drink orders by store excluding Brooklyn",
            "negation_reversed",
        ),
    ],
)
def test_validating_but_unfaithful_complex_plans_fail_closed(
    runtime_factory,
    intent: str,
    expected_gap: str,
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()

    if intent == "Revenue and order count by store last quarter":
        assert_plan_held(payload, "PLAN_FALLBACK_SEMANTIC_DRIFT")
        return
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert expected_gap in _gap_kinds(payload)
    assert payload["best"]["validation_ok"] is True
    assert "ready_for" not in payload["next"]
    assert payload["why"]["recovery_hints"]


@pytest.mark.parametrize(
    ("intent", "pattern", "unasked"),
    [
        # The draft compares each month with the same month a year before: the question asks
        # for no month split.
        ("revenue vs prior year by store", "inline_period_shift", ["month"]),
        ("orders by store and month", "metric_by_dimension_rollup", []),
        ("orders by store in Brooklyn", "metric_by_dimension_rollup", []),
        # "For stores that have ..." qualifies the stores; the draft also splits by store.
        (
            "Give me the 28D adoption funnel from signup to Send for stores that have an "
            "order rate of over 90% grouped by month",
            "filtered_adoption_funnel",
            ["Store name"],
        ),
        # The comparison buckets by month, which the question never asks for.
        (
            "food revenue share vs drink revenue share by store",
            "inline_comparison",
            ["month"],
        ),
    ],
)
def test_faithfulness_gate_preserves_realized_and_supported_shapes(
    runtime_factory,
    intent: str,
    pattern: str,
    unasked: list[str],
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()

    holds = {
        "revenue vs prior year by store": "PLAN_UNMATCHED_TERMS",
        "revenue vs order count by store last quarter": "PLAN_FALLBACK_SEMANTIC_DRIFT",
        "orders by store and month": "PLAN_UNMATCHED_TERMS",
        "orders by store in Brooklyn": "PLAN_FALLBACK_SEMANTIC_DRIFT",
        "food revenue share vs drink revenue share by store": "PLAN_UNMATCHED_TERMS",
    }
    if intent in holds:
        assert_plan_held(payload, holds[intent])
        return
    assert payload["best"]["pattern"] == pattern
    if unasked:
        # The faithfulness gate keeps the shape; a grouping the question never asks for holds it.
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_UNASKED_GROUPING"
        assert payload["why"]["details"]["unasked_groupings"] == unasked
        assert "ready_for" not in payload["next"]
        return
    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]


def test_faithfulness_gate_keeps_a_comparison_with_no_prior_period(runtime_factory) -> None:
    """The gate keeps the comparison's shape; with no prior-period select, the answer-shape
    check holds it."""

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="revenue vs order count by store name last quarter")
    finally:
        runtime.close()

    assert payload["best"]["pattern"] == "inline_comparison"
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert _gap_kinds(payload) == {"comparison_unrealized"}
    assert "ready_for" not in payload["next"]


@pytest.mark.parametrize(
    "intent",
    [
        "Give me the sum of active menu snapshot at the store dimension for stores "
        "that have done more than one order and more than one session grouped by month",
        "monthly orders for customers with at least 10 lifetime orders",
    ],
)
def test_qualifier_on_another_clock_is_not_offered_as_ready(runtime_factory, intent: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()

    assert payload["status"] == "low_confidence"
    assert payload["best"]["validation_ok"] is False
    assert "ready_for" not in payload["next"]


def test_positive_value_filter_is_not_mistaken_for_negation(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="orders by store in Brooklyn")
    finally:
        runtime.close()

    assert_plan_held(payload, "PLAN_FALLBACK_SEMANTIC_DRIFT")
    assert payload["best"]["query_ir"]["where"] == [
        {"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}
    ]


@pytest.fixture(scope="module")
def trailing_window_runtime(tmp_path_factory):
    """A snapshot stock labelled with its own trailing window, like a vendor's 14-day uniques."""
    import duckdb

    from semantic_rails.runtime import Runtime

    package = tmp_path_factory.mktemp("subject_window") / "f4win"
    (package / "models").mkdir(parents=True)
    (package / "metrics").mkdir()
    (package / "data").mkdir()
    (package / "package.yml").write_text(
        "schema_version: 1\npackage: {id: f4win, namespace: f4win, name: f4win, "
        "warehouse: duckdb, default_db: data/f4win.duckdb, seed: {kind: external}, "
        "schema_strict: true, environments: [development]}\n"
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
        # No period asked for: the stock still needs one as-of day per row.
        ("how many unique visitors", "low_confidence"),
    ],
)
def test_a_subject_with_its_own_window_is_flagged_for_another_period(
    trailing_window_runtime, intent: str, status: str
) -> None:
    payload = plan_payload(trailing_window_runtime, intent=intent)
    assert payload["status"] == status, payload.get("why")
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert ("subject_window_mismatch" in _gap_kinds(payload)) == (
        intent != "how many unique visitors"
    )
    if intent == "how many unique visitors":
        assert "stock_as_of_unrealized" in _gap_kinds(payload)


def test_rolling_metrics_are_not_checked(runtime_factory) -> None:
    # A rolling metric is read by day as a matter of course; its window is follow-up work.
    payload = plan_payload(runtime_factory("jaffle_shop"), intent="rolling revenue by day")
    assert "subject_window_mismatch" not in _gap_kinds(payload)


@pytest.mark.parametrize(
    ("time", "flagged"),
    [
        # No bucket: each value covers the whole window.
        ({"start": "2026-09-01", "end": "2026-10-01"}, True),
        ({"start": "2026-09-21", "end": "2026-09-28"}, True),
        ({"start": "2026-09-08", "end": "2026-09-22"}, False),
        ({"range": {"last": {"unit": "week", "value": 2}}}, False),
        # A bucket wins over the window it sits in.
        ({"grain": "week", "start": "2026-09-08", "end": "2026-09-22"}, True),
    ],
)
def test_the_period_is_the_bucket_else_the_whole_window(
    trailing_window_runtime, time: dict, flagged: bool
) -> None:
    from semantic_rails.planner.time_checks import _subject_window_gaps

    role = "temporal_role.f4win_repo_snapshot_snapshot_date"
    query = {
        "select": [{"expression": {"metric": "metric.f4win.unique_visitors_14d"}, "as": "v"}],
        "time": {"temporal_role": role, **time},
    }
    gaps = _subject_window_gaps(trailing_window_runtime.config, query)
    assert bool(gaps) is flagged
