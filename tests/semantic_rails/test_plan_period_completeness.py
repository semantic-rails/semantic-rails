"""A period comparison is ready only when every period it returns has ended at the request's now.

A comparison takes no window but an end: validation refuses one beside ``last``, ``this`` or a
``start``. The check reads the draft's engine form, so with no ``time.end``, or one after
``now`` or off a bucket boundary, a draft that compares periods is held with
``PERIOD_COMPARISON_INCOMPLETE``, and its option's call ends on the last complete period. Every
other draft answers as it does without the check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
from typing import Any

import pytest
import yaml

from semantic_rails.period_completeness import compares_periods, incomplete_period_why
from semantic_rails.planner import examples as examples_module
from semantic_rails.planner import intent_holds
from semantic_rails.planner.plan import plan_payload
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import copy_package_config, opened, write_single_file_package
from tests.semantic_rails.planner_fixtures import jaffle_runtime

HELD = "PERIOD_COMPARISON_INCOMPLETE"
METRIC = "metric.sales.month_over_month_revenue_growth"
ROLE = "temporal_role.jaffle_order_time"
REVENUE = "measure.jaffle.revenue_usd"
NOW = "2018-07-15T12:00:00Z"
PRIOR_MONTH = {"kind": "prior_period", "input": REVENUE, "offset": {"unit": "month", "value": 1}}
GRANT = RequestContext(
    actor="subject",
    roles=("analyst",),
    audience="finance",
    metric_allowlist=(METRIC,),
    dimension_allowlist=(ROLE,),
)

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _ready(payload: dict[str, Any]) -> bool:
    return "execute" in (payload.get("next") or {}).get("ready_for", [])


def _assert_held(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == HELD, payload.get("why")
    assert not _ready(payload)
    (hint,) = payload["why"]["recovery_hints"]
    assert hint["kind"] == "compare_complete_periods"
    assert hint["message"].endswith(("then plan again.", "or later."))
    return payload["why"]["details"]


def _comparing(grain: str) -> list[dict[str, Any]]:
    """A prior_period select of its own, and the governed growth metric built on one."""
    prior = {
        "kind": "prior_period",
        "input": {"kind": "measure", "measure": REVENUE},
        "offset": {"unit": grain, "value": 1},
    }
    return [
        {"expression": prior, "as": "before"},
        {"expression": {"kind": "metric", "metric": METRIC}, "as": "growth"},
    ]


def _engine(select: dict[str, Any], time: dict[str, Any], now: Any, **extra: Any) -> dict:
    return {"select": [select], "time": time, "policy_context": {"now": now}, **extra}


@pytest.mark.parametrize(
    ("grain", "end", "now", "complete_end"),
    [
        # No end: the window runs to now, so the period holding now is in progress.
        ("month", None, "2024-07-15T12:00:00Z", "2024-07-01"),
        ("year", None, "2024-01-01T00:00:00Z", "2024-01-01"),
        # A date is its midnight: that day has not ended.
        ("day", "2024-07-15", "2024-07-15", None),
        ("day", "2024-07-16", "2024-07-15", "2024-07-15"),
        ("day", "2024-07-15T12:00:00", "2024-07-16T00:00:00Z", "2024-07-15"),
        # Weeks start on Monday; 2024-07-14 is a Sunday.
        ("week", "2024-07-15", "2024-07-15T00:00:00Z", None),
        ("week", "2024-07-14", "2024-07-20T00:00:00Z", "2024-07-08"),
        ("month", "2024-07-01", "2024-07-31T23:59:59Z", None),
        ("month", "2024-08-01", "2024-07-31T23:59:59Z", "2024-07-01"),
        ("quarter", "2024-07-01", "2024-08-15T00:00:00Z", None),
        ("quarter", "2024-08-01", "2024-08-15T00:00:00Z", "2024-07-01"),
        ("year", "2024-01-01", "2024-07-15T12:00:00Z", None),
    ],
)
def test_a_period_is_complete_when_its_bucket_has_ended_by_now(
    runtime: Runtime, grain: str, end: str | None, now: str, complete_end: str | None
) -> None:
    time = {"temporal_role": ROLE, "grain": grain, **({"end": end} if end else {})}
    for select in _comparing(grain):
        why = incomplete_period_why(runtime.config, _engine(select, time, now))
        if complete_end is None:
            assert why is None
        else:
            assert why is not None and why["code"] == HELD
            assert why["details"]["complete_end"] == complete_end


def test_the_clocks_zone_cuts_the_buckets(runtime: Runtime) -> None:
    """2024-07-01T02:00Z is still June 30 in New York, so June hasn't ended there."""
    config = runtime.config
    roles = [
        replace(row, timezone="America/New_York", column_timezone="UTC") if row.id == ROLE else row
        for row in config.temporal_roles
    ]
    zoned = replace(config, temporal_roles=roles)
    time = {"temporal_role": ROLE, "grain": "month", "end": "2024-07-01"}
    select = _comparing("month")[1]
    why = incomplete_period_why(zoned, _engine(select, time, "2024-07-01T02:00:00Z"))
    assert why is not None and why["details"]["incomplete_period"]["start"] == "2024-06-01"
    assert incomplete_period_why(zoned, _engine(select, time, "2024-07-01T05:00:00Z")) is None


@pytest.mark.parametrize(
    "select",
    [
        {"expression": {"kind": "measure", "measure": REVENUE}, "as": "revenue"},
        {"expression": {"kind": "metric", "metric": "metric.sales.aov_usd"}, "as": "aov"},
    ],
)
def test_a_draft_that_compares_nothing_is_not_checked(
    runtime: Runtime, select: dict[str, Any]
) -> None:
    time = {"temporal_role": ROLE, "grain": "month"}
    assert incomplete_period_why(runtime.config, _engine(select, time, NOW)) is None


def test_a_clock_that_cannot_be_read_holds_a_comparison(runtime: Runtime) -> None:
    time = {"temporal_role": ROLE, "grain": "month", "end": "2018-01-01"}
    engine = _engine(_comparing("month")[1], time, "next tuesday")
    why = incomplete_period_why(runtime.config, engine)
    assert why is not None and why["code"] == HELD
    assert why["details"] == {"path": "now"}


@pytest.mark.parametrize(
    "query",
    [
        # Execution rejects each: a select that isn't a list, an unknown kind.
        {"select": {"expression": {"kind": "measure", "measure": REVENUE}}},
        {"select": [{"expression": {"kind": "prior_periods", "measure": REVENUE}}]},
        # A metric whose definition can't be read.
        {"select": [{"expression": {"kind": "metric", "metric": "metric.unknown"}}]},
    ],
)
def test_a_draft_the_check_cannot_read_counts_as_a_comparison(
    runtime: Runtime, query: dict[str, Any]
) -> None:
    assert compares_periods(runtime.config, query)
    why = incomplete_period_why(runtime.config, {**query, "policy_context": {"now": NOW}})
    assert why is not None and why["code"] == HELD


def test_a_dated_where_hold_never_asks_to_remove_the_filter(runtime: Runtime) -> None:
    role = next(row for row in runtime.config.temporal_roles if row.id == ROLE)
    time = {"temporal_role": ROLE, "grain": "month", "end": "2018-01-01"}
    where = [{"field": role.dimension, "op": "lt", "value": "2017-12-15"}]
    engine = _engine(_comparing("month")[1], time, NOW, where=where)
    why = incomplete_period_why(runtime.config, engine)
    assert why is not None and why["details"]["incomplete_period"]["start"] == "2017-12-01"
    (hint,) = why["recovery_hints"]
    assert hint["message"] == (
        "Compare complete months through November 2017: set time.end to 2017-12-01 "
        "(end-exclusive), then plan again."
    )


@pytest.mark.parametrize("end", [None, "2018-01-01"])
def test_a_metric_that_compares_periods_is_checked_on_every_plan_path(
    runtime: Runtime, end: str | None
) -> None:
    """The governed growth metric divides by its prior month: plan and the granted-metric
    shortcut both hold it until its window ends on a month that has ended."""

    time = {"temporal_role": ROLE, "grain": "month", **({"end": end} if end else {})}
    planned = plan_payload(
        runtime,
        intent="month over month revenue growth by month",
        partial_query={"time": {"end": end}} if end else None,
        now=NOW,
    )
    granted = plan_payload(
        runtime,
        intent="month-over-month revenue growth",
        partial_query={"policy_context": GRANT.to_policy_context(), "time": time},
        now=NOW,
    )
    assert granted["best"]["pattern"] == "granted_metric"
    for payload in (planned, granted):
        assert METRIC in str(payload["best"]["query"]["select"])
        if end:
            assert payload["status"] == "ok" and _ready(payload), payload.get("why")
        else:
            details = _assert_held(payload)
            assert details["complete_end"] == details["incomplete_period"]["start"]


def test_the_option_plans_ready_and_returns_only_ended_months(runtime: Runtime) -> None:
    intent = "month over month revenue growth by month"
    held = plan_payload(runtime, intent=intent, now=NOW)
    details = _assert_held(held)
    assert details["incomplete_period"] == {"start": "2018-07-01", "end": "2018-08-01"}
    (option,) = details["options"]
    assert option["reads"] == "every month through June 2018"
    assert option["call"] == {
        **held["best"]["query"],
        "time": {**held["best"]["query"]["time"], "end": "2018-07-01"},
    }
    again = plan_payload(
        runtime, intent=intent, partial_query={"time": {"end": "2018-07-01"}}, now=NOW
    )
    assert again["status"] == "ok" and _ready(again), again.get("why")
    rows = runtime.query(option["call"], now=NOW)["rows"]
    assert rows and all(not row["period_partial"] for row in rows)
    assert all(row["period"] < "2018-07-01" for row in rows)


@pytest.fixture
def example_runtime(runtime: Runtime) -> Iterator[Callable[[str | None], Runtime]]:
    saved = runtime._package_examples

    def _with_example(end: str | None) -> Runtime:
        time = {"temporal_role": ROLE, "grain": "month", **({"end": end} if end else {})}
        example = {
            "question": "Revenue growth on the month before",
            "query": {"select": [METRIC], "time": time},
        }
        runtime._package_examples = [("growth", example)]
        return runtime

    yield _with_example
    runtime._package_examples = saved


@pytest.mark.parametrize("end", [None, "2018-07-01"])
def test_an_authored_example_goes_through_the_same_check(
    example_runtime: Callable[[str | None], Runtime],
    monkeypatch: pytest.MonkeyPatch,
    end: str | None,
) -> None:
    """A package example's authored call answers its question only over ended periods."""

    runtime = example_runtime(end)
    question = "Revenue growth on the month before"
    payload = plan_payload(runtime, intent=question, now=NOW)
    assert payload["best"]["pattern"] == "package_example"
    assert payload["best"]["validation_ok"] is True
    if end:
        assert payload["status"] == "ok" and _ready(payload), payload.get("why")
        return
    details = _assert_held(payload)
    assert details["incomplete_period"] == {"start": "2018-07-01", "end": "2018-08-01"}
    # Nothing else holds it: without the check, the example was ready to execute.
    monkeypatch.setattr(examples_module, "_incomplete_period_why", lambda *args, **kwargs: None)
    assert _ready(plan_payload(runtime, intent=question, now=NOW))


def test_a_caller_select_that_compares_periods_is_held(runtime: Runtime) -> None:
    payload = plan_payload(
        runtime,
        intent="revenue by month",
        partial_query={
            "select": [REVENUE, {"expression": PRIOR_MONTH, "as": "month_before"}],
            "time": {"temporal_role": ROLE, "grain": "month"},
        },
        now=NOW,
    )
    assert "month_before" in str(payload["best"]["query"]["select"])
    _assert_held(payload)


@pytest.mark.parametrize(
    "intent", ["revenue by month", "orders by store last month", "revenue month over month"]
)
def test_a_draft_that_compares_nothing_plans_as_before(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    checked = plan_payload(runtime, intent=intent, now=NOW)
    monkeypatch.setattr(intent_holds, "incomplete_period_why", lambda *args, **kwargs: None)
    unchecked = plan_payload(runtime, intent=intent, now=NOW)
    for key in ("status", "why", "next", "best", "warnings"):
        assert checked.get(key) == unchecked.get(key), key


# ---------------------------------------------------------------- round 2: every bound counts
# A shop on a DATE clock: May reads 100, June 1 reads 10 and the rest of June 90, so complete June
# against May is 0% growth, and any cut of June reads -90%.
GROWTH_NOW = "2018-07-15T12:00:00Z"
GROWTH_CLOCK = "temporal_role.shop_order_ordered_at"
GROWTH_SEED = """
CREATE OR REPLACE TABLE shop_customer AS
SELECT 'customer_1' AS customer_id, 'new' AS customer_type, TIMESTAMP '2018-01-01 00:00:00' AS first_ordered_at;
CREATE OR REPLACE TABLE shop_order AS
SELECT * FROM (VALUES
  ('order_1', 'customer_1', 'web', 10000, DATE '2018-05-10'),
  ('order_2', 'customer_1', 'web', 1000, DATE '2018-06-01'),
  ('order_3', 'customer_1', 'web', 9000, DATE '2018-06-20')
) AS t(order_id, customer_id, channel, order_total_cents, ordered_at);
CREATE OR REPLACE TABLE shop_order_item AS
SELECT 'item_1' AS order_item_id, 'order_1' AS order_id, 'product_1' AS product_id, 1 AS quantity, 10000 AS line_total_cents;
CREATE OR REPLACE TABLE shop_product AS SELECT 'product_1' AS product_id, 'beverage' AS product_type;
"""


def _growth_package(
    target: Any, *, seed: str = GROWTH_SEED, kind: str = "date", filter_all: Any = None
) -> Runtime:
    source = write_single_file_package(target / "growth")
    (source.parent / "data" / "seed_example.sql").write_text(seed.lstrip(), "utf-8")
    doc = safe_load(source.read_bytes())
    doc["models"]["orders"]["times"]["ordered_at"]["kind"] = kind
    revenue: dict[str, Any] = {
        "kind": "aggregate",
        "measure": "measure.shop.revenue_usd",
        "aggregation": "sum",
        **({"filter": {"all": filter_all}} if filter_all else {}),
    }
    prior = {"kind": "prior_period", "input": revenue, "offset": {"unit": "month", "value": 1}}
    change = {"kind": "arithmetic", "op": "subtract", "left": revenue, "right": prior}
    doc["metrics"]["growth"] = {
        "label": "Revenue growth",
        "kind": "derived",
        "value_type": "percent",
        "expression": {"kind": "arithmetic", "op": "divide", "left": change, "right": prior},
    }
    source.write_text(yaml.safe_dump(doc, sort_keys=False), "utf-8")
    return opened(Runtime.from_path(str(source)))


@pytest.fixture(scope="module")
def growth(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    with closing(_growth_package(tmp_path_factory.mktemp("growth"))) as runtime:
        yield runtime


def _growth_call(runtime: Runtime, end: str | None, where: list[dict] | None = None) -> dict:
    role = next(row for row in runtime.config.temporal_roles if row.id == GROWTH_CLOCK)
    time = {"temporal_role": role.id, "grain": "month", **({"end": end} if end else {})}
    return {"select": ["metric.shop.growth"], "time": time, **({"where": where} if where else {})}


def _cut(runtime: Runtime, op: str, value: str) -> list[dict]:
    role = next(row for row in runtime.config.temporal_roles if row.id == GROWTH_CLOCK)
    return [{"field": role.dimension, "op": op, "value": value}]


def _plan_example(runtime: Runtime, call: dict, now: str) -> dict:
    saved = runtime._package_examples
    runtime._package_examples = [
        ("asked", {"question": "Revenue growth as authored", "query": call})
    ]
    try:
        payload = plan_payload(runtime, intent="Revenue growth as authored", now=now)
    finally:
        runtime._package_examples = saved
    assert payload["best"]["pattern"] == "package_example"
    return payload


def _growth_rows(runtime: Runtime, call: dict) -> dict[str, float | None]:
    rows = runtime.query(call, now=GROWTH_NOW)["rows"]
    return {str(row["period"])[:10]: row["growth"] for row in rows}


def test_the_fixture_reads_zero_over_complete_june_and_minus_ninety_over_a_cut_one(
    growth: Runtime,
) -> None:
    assert _growth_rows(growth, _growth_call(growth, "2018-07-01"))["2018-06-01"] == 0
    cut = _growth_call(growth, None, _cut(growth, "lt", "2018-06-15"))
    assert _growth_rows(growth, cut)["2018-06-01"] == pytest.approx(-0.9)


def test_every_clock_the_draft_reads_must_have_ended(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Orders on a New York clock, sessions on UTC: at 02:00Z on July 1, New York's June has two
    hours to go, so a comparison of order counts bucketed there is held."""

    path = copy_package_config(tmp_path_factory.mktemp("zoned"), "jaffle_shop", preseed_db=True)
    orders = path / "models" / "core" / "orders.yml"
    doc = safe_load(orders.read_bytes())
    doc["model"]["times"]["ordered_at"].update(timezone="America/New_York", column_timezone="UTC")
    orders.write_text(yaml.safe_dump(doc, sort_keys=False), "utf-8")
    prior = {"kind": "prior_period", "input": "measure.jaffle.order_count", "offset": {"unit": "month", "value": 1}}  # fmt: skip
    call = {
        "select": [
            "measure.jaffle.session_starts",
            "measure.jaffle.order_count",
            {"expression": prior, "as": "orders_month_before"},
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_session_started_at",
            "grain": "month",
            "end": "2018-07-01",
        },
    }
    with closing(opened(Runtime.from_path(str(path)))) as runtime:
        payload = _plan_example(runtime, call, "2018-07-01T02:00:00Z")
    assert payload["best"]["validation_ok"] is True
    _assert_held(payload)


def test_an_end_finer_than_a_microsecond_is_held(growth: Runtime) -> None:
    call = _growth_call(growth, "2018-06-01T00:00:00.0000001")
    details = _assert_held(_plan_example(growth, call, GROWTH_NOW))
    for option in details.get("options", []):
        assert _ready(_plan_example(growth, option["call"], GROWTH_NOW))


def test_the_option_ends_before_a_date_filter_cuts_a_period(growth: Runtime) -> None:
    call = _growth_call(growth, None, _cut(growth, "lt", "2018-06-15"))
    details = _assert_held(_plan_example(growth, call, GROWTH_NOW))
    (option,) = details["options"]
    assert option["call"]["time"]["end"] == "2018-06-01"
    assert option["call"]["where"] == call["where"]  # the caller's filter stays
    assert _ready(_plan_example(growth, option["call"], GROWTH_NOW))
    assert "2018-06-01" not in _growth_rows(growth, option["call"])


def test_a_date_filter_on_the_end_of_a_complete_window_holds_nothing(growth: Runtime) -> None:
    call = _growth_call(growth, "2018-07-01", _cut(growth, "lt", "2018-07-01"))
    payload = _plan_example(growth, call, GROWTH_NOW)
    assert payload["status"] == "ok" and _ready(payload), payload.get("why")
    assert _growth_rows(growth, call)["2018-06-01"] == 0


# ---------------------------------------------------------------- round 3: hold what can't be proved
# The same shop on a UTC TIMESTAMP clock, its last June revenue at 22:00 on June 30.
GROWTH_TIMESTAMP_SEED = GROWTH_SEED.replace("DATE '2018-05-10'", "TIMESTAMP '2018-05-10 12:00:00'")
GROWTH_TIMESTAMP_SEED = GROWTH_TIMESTAMP_SEED.replace(
    "DATE '2018-06-01'", "TIMESTAMP '2018-06-01 12:00:00'"
).replace("DATE '2018-06-20'", "TIMESTAMP '2018-06-30 22:00:00'")


def _held_with_no_option(payload: dict[str, Any]) -> dict[str, Any]:
    details = _assert_held(payload)
    assert "options" not in details, details
    return details


def _zoned_jaffle(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    """jaffle with its orders clock on New York (UTC storage); sessions stay on UTC."""
    path = copy_package_config(tmp_path_factory.mktemp("zoned"), "jaffle_shop", preseed_db=True)
    orders = path / "models" / "core" / "orders.yml"
    doc = safe_load(orders.read_bytes())
    doc["model"]["times"]["ordered_at"].update(timezone="America/New_York", column_timezone="UTC")
    orders.write_text(yaml.safe_dump(doc, sort_keys=False), "utf-8")
    return opened(Runtime.from_path(str(path)))


def test_a_comparison_in_an_entity_filter_is_held(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Customers whose growth is positive, per period: the growth reads New York's June, two
    hours from its end, though the selected sessions read UTC."""

    having = [{"field": METRIC, "op": "gt", "value": 0}]
    call = {
        "select": ["measure.jaffle.session_starts"],
        "where": [{"entity": "entity.jaffle_customer", "having": having, "over": "period"}],
        "time": {
            "temporal_role": "temporal_role.jaffle_session_started_at",
            "grain": "month",
            "end": "2018-07-01",
        },
    }
    with closing(_zoned_jaffle(tmp_path_factory)) as runtime:
        payload = _plan_example(runtime, call, "2018-07-01T02:00:00Z")
    assert payload["best"]["validation_ok"] is True
    _held_with_no_option(payload)


@pytest.mark.parametrize("end", ["2018-07-01", None])
def test_a_date_filter_inside_the_metric_is_held(
    tmp_path_factory: pytest.TempPathFactory, end: str | None
) -> None:
    """The growth metric's revenue keeps only orders before June 15: June reads -90%."""

    cut = [{"field": "dimension.shop_order_ordered_at", "op": "lt", "value": "2018-06-15"}]
    with closing(_growth_package(tmp_path_factory.mktemp("cut"), filter_all=cut)) as runtime:
        if end:
            assert _growth_rows(runtime, _growth_call(runtime, end))["2018-06-01"] == pytest.approx(
                -0.9
            )
        _held_with_no_option(_plan_example(runtime, _growth_call(runtime, end), GROWTH_NOW))


@pytest.mark.parametrize("where", [False, True], ids=["end", "where"])
def test_a_bound_with_a_utc_offset_is_held(
    tmp_path_factory: pytest.TempPathFactory, where: bool
) -> None:
    """20:00 at -04:00 is July 1 in UTC, but the SQL compares 20:00 wall time with the column,
    so the 22:00 revenue on June 30 drops out and June reads -90%."""

    bound = "2018-06-30T20:00:00-04:00"
    target = tmp_path_factory.mktemp("offset")
    with closing(_growth_package(target, seed=GROWTH_TIMESTAMP_SEED, kind="timestamp")) as runtime:
        call = (
            _growth_call(runtime, "2018-07-01", _cut(runtime, "lt", bound))
            if where
            else _growth_call(runtime, bound)
        )
        assert _growth_rows(runtime, call)["2018-06-01"] == pytest.approx(-0.9)
        _held_with_no_option(_plan_example(runtime, call, GROWTH_NOW))


def test_a_bound_with_a_comma_fraction_is_held(growth: Runtime) -> None:
    call = _growth_call(growth, "2018-06-01T00:00:00,0000001")
    _held_with_no_option(_plan_example(growth, call, GROWTH_NOW))
