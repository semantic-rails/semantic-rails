"""Role-local range resolution on normalization, nested compilation and cache hits."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from semantic_rails import ast
from semantic_rails.compiler import _compile_query_sql_ast, _predicate_window_filters
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata import _query_state
from semantic_rails.metadata_parts.valid_values import _query_state as values_query_state
from semantic_rails.runtime import Runtime

ROLE = "temporal_role.jaffle_order_time"


@pytest.fixture
def config():
    original = load_package_config("configs/semantic_rails/jaffle_shop")
    return replace(
        original,
        temporal_roles=[replace(r, timezone="America/New_York") for r in original.temporal_roles],
    )


def _query(**time):
    return {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "n"}],
        "time": {
            "temporal_role": ROLE,
            "grain": "month",
            "range": {"last": {"unit": "month", "value": 1}},
            **time,
        },
    }


@pytest.mark.parametrize(
    "now",
    [
        "2024-07-01T02:00:00Z",
        "2024-06-30T22:00:00-04:00",
        datetime(2024, 7, 1, 2, tzinfo=UTC),
        "2024-06-30",
        date(2024, 6, 30),
        datetime(2024, 6, 30, 22),
    ],
)
@pytest.mark.parametrize("normalize", [ast.normalize_query, ast.normalize_partial_query])
def test_range_normalization_preserves_the_instant_until_the_role_is_known(config, now, normalize):
    query = {**_query(), "policy_context": {"now": now}}
    time = normalize(query, config=config).time
    assert (time.start, time.end) == ("2024-05-01", "2024-06-01")


@pytest.mark.parametrize(
    "timezone,end",
    [("UTC", "2024-07-01"), ("America/New_York", "2024-06-01"), ("Asia/Tokyo", "2024-07-01")],
)
def test_default_now_uses_the_roles_date(monkeypatch, config, timezone, end):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 7, 1, 2, tzinfo=UTC)

    monkeypatch.setattr(ast, "datetime", FixedDatetime)
    config = replace(
        config, temporal_roles=[replace(r, timezone=timezone) for r in config.temporal_roles]
    )
    assert ast.normalize_query(_query(), config=config).time.end == end


def test_utc_default_keeps_planner_resolution_consistent():
    last = {"last": {"unit": "month", "value": 1}}
    for instant in ("2024-07-01T02:00:00Z", "2024-06-30T22:00:00-04:00"):
        assert ast._relative_range_bounds(last, policy_context={"now": instant}) == {
            "start": "2024-06-01",
            "end": "2024-07-01",
        }


@pytest.mark.parametrize("read", [_query_state, values_query_state])
def test_query_state_reports_the_same_local_bounds(config, read):
    state = read({**_query(), "policy_context": {"now": "2024-07-01T02:00:00Z"}}, config)
    assert state["normalized_query"]["time"]["end"] == "2024-06-01"


def test_cache_changes_at_local_midnight_within_one_utc_day(monkeypatch, config):
    class MovingDatetime(datetime):
        hour = 3

        @classmethod
        def now(cls, tz=None):
            return cls(2024, 7, 1, cls.hour, tzinfo=UTC)

    monkeypatch.setattr(ast, "datetime", MovingDatetime)
    runtime = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        first = runtime._compile(_query(), policy_context={})
        MovingDatetime.hour = 4
        second = runtime._compile(_query(), policy_context={})
        again = runtime._compile(_query(), policy_context={})
        assert first["logical_plan"].time["end"] == "2024-06-01"
        assert second["logical_plan"].time["end"] == "2024-07-01"
        assert second["compile_stats"]["cache_hit"] is False
        assert again["compile_stats"]["cache_hit"] is True
    finally:
        runtime.close()


@pytest.mark.parametrize("unit", ["week", "month", "quarter", "year"])
def test_nested_compilation_refuses_nondefault_relative_periods(config, unit):
    query = _query(calendar_id="fiscal", range={"last": {"unit": unit, "value": 9}})
    with pytest.raises(SemanticLayerError) as caught:
        _compile_query_sql_ast(config, query)
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["path"] == "time.range.last.unit"


def test_cache_reuses_the_date_already_resolved_by_binding(monkeypatch, config):
    class MovingDatetime(datetime):
        hour = 3

        @classmethod
        def now(cls, tz=None):
            return cls(2024, 7, 1, cls.hour, tzinfo=UTC)

    monkeypatch.setattr(ast, "datetime", MovingDatetime)
    runtime = Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        binding = runtime._bind(_query(), {})
        MovingDatetime.hour = 4
        first = runtime._compile(_query(), policy_context={}, binding=binding)
        second = runtime._compile(_query(), policy_context={}, binding=runtime._bind(_query(), {}))
        assert first["logical_plan"].time["end"] == "2024-06-01"
        assert second["logical_plan"].time["end"] == "2024-07-01"
        assert second["compile_stats"]["cache_hit"] is False
    finally:
        runtime.close()


def test_day_ranges_remain_available_on_nondefault_calendars(config):
    query = {
        **_query(calendar_id="fiscal", range={"last": {"unit": "day", "value": 1}}),
        "policy_context": {"now": "2024-07-01T02:00:00Z"},
    }
    time = ast.normalize_query(query, config=config).time
    assert (time.start, time.end) == ("2024-06-29", "2024-06-30")


def test_role_bound_to_nondefault_calendar_cannot_bypass_the_period_guard(config):
    role = next(r for r in config.temporal_roles if r.id == ROLE)
    entity = next(d.entity for d in config.dimensions if d.id == role.dimension)
    config = replace(
        config,
        entities=[
            replace(e, calendar_id="fiscal") if e.id == entity else e for e in config.entities
        ],
    )
    with pytest.raises(SemanticLayerError) as caught:
        ast.normalize_query(_query(), config=config)
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.details["calendar_id"] == "fiscal"


def test_converted_date_predicate_window_preserves_logical_field_filters(config):
    role = next(r for r in config.temporal_roles if r.id == ROLE)
    config = replace(
        config,
        dimensions=[
            replace(d, data_type="date") if d.id == role.dimension else d for d in config.dimensions
        ],
        temporal_roles=[
            replace(r, column_timezone="UTC") if r.id == ROLE else r for r in config.temporal_roles
        ],
    )
    assert _predicate_window_filters(
        {"temporal_role": ROLE, "start": "2024-06-30", "end": "2024-07-01T23:00:00"}, config
    ) == [
        {"field": role.dimension, "op": ">=", "value": "2024-06-30"},
        {"field": role.dimension, "op": "<=", "value": "2024-07-01"},
    ]
