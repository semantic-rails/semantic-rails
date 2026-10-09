"""A named window states its window, clock and scope, and a balance answers its closing day.

The rows themselves are frozen in ``shop/tests/answers.yml`` (the quiet week, the empty
middle month, the days after the last order, the ratio with no denominator, the balance's
closing day); these tests check what the answer says about them.
"""

from dataclasses import replace

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

from .conftest import _runtime

ORDERS = "measure.shop.order_count"
SEATS = "measure.shop.seats"
ORDER_CLOCK = "temporal_role.shop_order_ordered_at"
SEAT_CLOCK = "temporal_role.shop_account_day_snapshot_day"
SEAT_DAY = "dimension.shop_account_day_snapshot_day"


@pytest.fixture
def shop(duckdb_backend) -> Runtime:
    return duckdb_backend.runtimes["utc_authored"]


def test_days_after_the_last_order_say_no_data_yet(shop: Runtime) -> None:
    call = {"select": [ORDERS], "time": {"this": "month", "grain": "day"}}
    result = shop.query(call, now="2024-08-03T12:00:00Z")
    assert [(r["period"], r["period_partial"], r["order_count"]) for r in result["rows"]] == [
        ("2024-08-01", False, 1),
        ("2024-08-02", False, None),
        ("2024-08-03", True, None),
    ]
    assert result["window"] == {
        "start": "2024-08-01",
        "end": "2024-08-03T12:00:00",
        "now": "2024-08-03T12:00:00Z",
        "partial": True,
        "data_through": "2024-08-01",
    }
    assert result["clock"] == ORDER_CLOCK
    assert result["observation_scope"] == "dataset"
    assert {"code": "PARTIAL_PERIOD", "message": "August 2024 so far (3 of 31 days)"} in result[
        "assumptions"
    ]
    (warning,) = [w for w in result["warnings"] if w["code"] == "NO_DATA_YET"]
    assert warning["details"]["measures"] == [
        {"id": ORDERS, "edge": "2024-08-01", "edge_source": "last_bucket"}
    ]
    # The output columns list the flag right after the period.
    fields = [column["field"] for column in result["output_columns"]]
    assert fields[:2] == ["period", "period_partial"]


def test_a_quiet_week_inside_the_data_reads_zero_without_a_warning(shop: Runtime) -> None:
    call = {"select": [ORDERS], "time": {"last": {"unit": "week", "value": 1}}}
    result = shop.query(call, now="2024-05-10T00:00:00Z")
    assert result["rows"] == [{"order_count": 0}]
    assert result["window"]["data_through"] == "2024-05-06"
    assert result["window"]["partial"] is False
    assert not [w for w in result["warnings"] if w["code"].startswith("NO_DATA")]


def test_validate_and_compile_state_the_window_without_running(shop: Runtime) -> None:
    call = {"select": [ORDERS], "time": {"last": {"unit": "week", "value": 1}}}
    for answer in (
        shop.validate(call, now="2024-05-10T00:00:00Z"),
        shop.compile(call, now="2024-05-10T00:00:00Z"),
    ):
        assert answer["window"] == {
            "start": "2024-04-29",
            "end": "2024-05-06",
            "now": "2024-05-10T00:00:00Z",
            "partial": False,
        }
        assert answer["clock"] == ORDER_CLOCK
        assert answer["observation_scope"] == "dataset"


def test_an_answer_without_time_still_names_its_clock_and_scope(shop: Runtime) -> None:
    result = shop.query({"select": [ORDERS]})
    assert "window" not in result
    assert result["clock"] == ORDER_CLOCK
    assert result["observation_scope"] == "dataset"


def test_a_balance_right_now_is_its_last_closing_day(shop: Runtime) -> None:
    """'Right now' on a balance is the last complete day: account 1 and 3 on January 15."""
    call = {"select": [SEATS], "time": {"last": {"unit": "day", "value": 1}, "grain": "day"}}
    result = shop.query(call, now="2024-01-16T09:00:00Z")
    assert result["rows"] == [{"period": "2024-01-15", "period_partial": False, "seats": 220}]
    assert result["clock"] == SEAT_CLOCK


@pytest.fixture
def per_day_balances(packages) -> Runtime:
    """The shop with a policy: seats are only read per snapshot day."""
    base = _runtime(packages["utc_authored"])
    config, source_path = base.config, base.source_path
    base.close()
    policy = SemanticPolicyConfig(
        id="policy.shop.seats_per_day",
        kind="metric_constraint",
        object_ids=[SEATS],
        config={"required_group_by": [SEAT_DAY]},
    )
    runtime = Runtime.from_config(
        replace(config, semantic_policies=[*config.semantic_policies, policy]),
        source_path=source_path,
    )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("time", "group_by", "allowed"),
    [
        ({"last": {"unit": "day", "value": 1}, "grain": "day"}, [], True),
        ({"start": "2024-01-01", "end": "2024-01-08", "grain": "day"}, [], True),
        ({}, [SEAT_DAY], True),
        ({"start": "2024-01-01", "end": "2024-01-22", "grain": "week"}, [], False),
        ({"start": "2024-01-01", "end": "2024-01-22"}, [], False),
    ],
    ids=["right-now", "daily-series", "grouped-by-the-day", "weekly", "window-total"],
)
def test_a_day_grain_meets_a_required_snapshot_day(per_day_balances, time, group_by, allowed):
    """The clock's own date is the period at day grain; a coarser grain is not that day."""
    call = {"select": [SEATS], "group_by": group_by}
    if time:
        call["time"] = {"temporal_role": SEAT_CLOCK, **time}
    if allowed:
        assert per_day_balances.query(call, now="2024-01-16T09:00:00Z")["rows"]
        return
    with pytest.raises(SemanticLayerError) as refused:
        per_day_balances.query(call, now="2024-01-16T09:00:00Z")
    assert refused.value.code == "POLICY_DENIED"


@pytest.mark.parametrize(
    ("window", "path"),
    [
        ({"last": {"unit": "week", "value": 2}}, "time.last"),
        ({"this": "month"}, "time.this"),
        ({"start": "2024-04-01", "end": "2024-05-06"}, "time.start"),
    ],
)
def test_a_comparison_beside_a_named_window_asks_for_the_series(shop: Runtime, window, path):
    prior = {
        "kind": "prior_period",
        "input": "measure.shop.revenue",
        "offset": {"unit": "week", "value": 1},
    }
    call = {
        "select": [{"expression": prior, "as": "week_before"}],
        "time": {"temporal_role": ORDER_CLOCK, "grain": "week", **window},
    }
    (error,) = shop.validate(call, now="2024-05-10T00:00:00Z")["errors"]
    assert error["code"] == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    assert error["details"]["path"] == path
    assert "comparison beside a named window is not supported yet" in error["message"]
    (hint,) = error["recovery_hints"]
    assert hint["kind"] == "ask_for_a_series" and "time.start" not in hint["message"]
    series = {**call, "time": hint["patch"]["replace"]["time"]}
    assert series["time"] == {"temporal_role": ORDER_CLOCK, "grain": "week"}
    assert shop.validate(series, now="2024-05-10T00:00:00Z")["ok"] is True
