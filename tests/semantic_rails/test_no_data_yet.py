"""Fail-safe coverage marker decisions and per-measure attribution."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.empty_groups import sql_nodes
from semantic_rails.registry import Registry
from semantic_rails.runtime import _no_data_yet_warnings
from semantic_rails.sql_ast import SqlIdentifier

ROLE = "temporal_role.jaffle_order_time"
REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"


@pytest.fixture
def total(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [{"expression": {"measure": REVENUE}, "as": "value"}],
        "time": {"temporal_role": ROLE, "start": "2026-09-21", "end": "2026-09-28"},
    }
    return compile_query(config, Registry(config), query), query


@pytest.mark.parametrize(
    "loaded_from,loaded_to,start,expected",
    [
        (-1, -1, "2026-09-21", "before_window"),
        (-1, 0, "2026-09-21", None),
        (-1, 1, "2026-09-21", None),
        (1, 1, "2026-09-21", None),
        (1, None, "2026-09-21", None),
        (None, None, "2026-09-21", "undated"),
        (-1, -1, "", None),
    ],
)
def test_window_total_markers_only_disclose_proven_edges(
    total,
    monkeypatch,
    loaded_from,
    loaded_to,
    start,
    expected,
):
    compiled, query = total
    query["time"]["start"] = start
    compiled["logical_plan"].time["start"] = start
    compiled["explain"].normalized_query["time"]["start"] = start
    monkeypatch.setattr(
        runtime_module,
        "_compiled_cte_row",
        lambda *a: {
            "coverage_1__loaded_from": loaded_from,
            "coverage_1__loaded_to": loaded_to,
        },
    )
    warnings = _no_data_yet_warnings(None, compiled, [], query)
    assert bool(warnings) is (expected is not None)
    if expected:
        assert warnings[0]["details"]["measures"] == [
            {
                "id": REVENUE,
                "edge": start if expected == "before_window" else None,
                "edge_source": None if expected == "undated" else expected,
            }
        ]


def test_relative_window_total_names_the_normalized_start(total, monkeypatch):
    compiled, query = total
    query["time"] = {"temporal_role": ROLE, "range": {"last": 1, "unit": "week"}}
    monkeypatch.setattr(
        runtime_module,
        "_compiled_cte_row",
        lambda *a: {
            "coverage_1__loaded_from": -1,
            "coverage_1__loaded_to": -1,
        },
    )
    warning = _no_data_yet_warnings(None, compiled, [], query)[0]
    assert warning["details"]["measures"][0]["edge"] == "2026-09-21"


@pytest.mark.parametrize("rows,excluded", [([{"value": 1}], ()), ([{"value": None}], ("value",))])
def test_no_extra_read_for_values_or_withheld_outputs(total, monkeypatch, rows, excluded):
    compiled, query = total
    calls = []
    monkeypatch.setattr(runtime_module, "_compiled_cte_row", lambda *a: calls.append(a))
    assert _no_data_yet_warnings(None, compiled, rows, query, excluded_outputs=excluded) == []
    assert calls == []


@pytest.mark.parametrize("window_total", [False, True])
def test_one_warning_attributes_each_measure_to_its_own_coverage(
    package_config_factory,
    monkeypatch,
    window_total,
):
    config, _ = package_config_factory("jaffle_shop")
    time = {"temporal_role": ROLE, "start": "2026-09-21", "end": "2026-09-28"}
    if not window_total:
        time.update(grain="week", fill=True)
    query = {
        "select": [
            {"expression": {"measure": m}, "as": alias}
            for m, alias in [(REVENUE, "money"), (ORDERS, "count")]
        ],
        "time": time,
    }
    compiled = compile_query(config, Registry(config), query)
    # Hold lowering's two independently observed leaves at different coverage results.
    coverage = next(c for c in compiled["sql_ast"].ctes if c.name == "coverage_1")
    compiled["sql_ast"].ctes.append(replace(coverage, name="coverage_2"))
    guard = next(c.query for c in compiled["sql_ast"].ctes if c.name == "guarded_base")
    field = next(f for f in guard.select if f.alias == "m2")
    for node in sql_nodes(field.expression):
        if isinstance(node, SqlIdentifier) and node.parts[0] == "coverage_1":
            node.parts[0] = "coverage_2"
    monkeypatch.setattr(
        runtime_module,
        "_compiled_cte_row",
        lambda *a: {
            "coverage_1__loaded_from": -1 if window_total else datetime(2026, 9, 7),
            "coverage_1__loaded_to": -1 if window_total else datetime(2026, 9, 7),
            "coverage_2__loaded_from": None,
            "coverage_2__loaded_to": None,
        },
    )
    rows = (
        []
        if window_total
        else [{f"{ROLE}__week": datetime(2026, 9, 21), "money": None, "count": None}]
    )
    warnings = _no_data_yet_warnings(None, compiled, rows, query)
    assert len(warnings) == 1
    assert warnings[0]["details"]["outputs"] == ["money", "count"]
    assert warnings[0]["details"]["measures"] == [
        {
            "id": REVENUE,
            "edge": "2026-09-21" if window_total else "2026-09-07",
            "edge_source": "before_window" if window_total else "last_bucket",
        },
        {"id": ORDERS, "edge": None, "edge_source": None},
    ]


@pytest.mark.parametrize(
    "grain,edges,buckets,expected",
    [
        ("week", [datetime(2024, 9, 16)], [], None),
        ("day", [datetime(2024, 9, 21)], [], None),
        ("hour", [datetime(2024, 9, 21, 10)], [datetime(2024, 9, 21, 11)], "2024-09-21T10:00:00"),
        ("hour", [datetime(2024, 9, 21, 10)], [datetime(2024, 9, 21, 10)], None),
        ("day", [date(2024, 9, 21)], [date(2024, 9, 22)], "2024-09-21"),
        ("week", [datetime(2024, 9, 16)], [datetime(2024, 9, 23)], "2024-09-16"),
        (
            "hour",
            [datetime(2024, 9, 21, 10, 0, 0, 123456, tzinfo=UTC)],
            [datetime(2024, 9, 21, 7, tzinfo=timezone(timedelta(hours=-4)))],
            "2024-09-21T10:00:00.123456+00:00",
        ),
        ("day", [date(2024, 9, 21)], [datetime(2024, 9, 22)], None),
        ("day", [datetime(2024, 9, 21)], [date(2024, 9, 22)], None),
        ("hour", [datetime(2024, 9, 21, 10, tzinfo=UTC)], [datetime(2024, 9, 21, 11)], None),
        ("hour", [datetime(2024, 9, 21, 10)], [datetime(2024, 9, 21, 11, tzinfo=UTC)], None),
        ("day", ["2024-09-21"], ["2024-09-22"], None),
        ("day", [date(2024, 9, 21)], [None], None),
        ("day", [date(2024, 9, 21)], [date(2024, 9, 22), "2024-09-23"], None),
        ("day", [date(2024, 9, 20), datetime(2024, 9, 21)], [date(2024, 9, 22)], None),
        (
            "hour",
            [datetime(2024, 9, 21, 9), datetime(2024, 9, 21, 10, tzinfo=UTC)],
            [datetime(2024, 9, 21, 11)],
            None,
        ),
        ("day", [None], [], "undated"),
    ],
)
def test_series_compares_only_compatible_sql_bucket_keys(
    package_config_factory, monkeypatch, grain, edges, buckets, expected
):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        temporal_roles=[
            replace(role, supported_grains=[*role.supported_grains, "hour"])
            if role.id == ROLE
            else role
            for role in config.temporal_roles
        ],
    )
    query = {
        "select": [{"expression": {"measure": REVENUE}, "as": "value"}],
        "time": {
            "temporal_role": ROLE,
            "grain": grain,
            "fill": grain != "hour",
            "start": "2024-09-22",
            "end": "2024-09-23",
        },
    }
    if grain == "hour":
        query["select"][0]["expression"].update(
            kind="aggregate",
            filter={
                "all": [
                    {
                        "field": "dimension.jaffle_order_customer_order_number",
                        "op": "!=",
                        "value": 9999,
                    }
                ]
            },
        )
    compiled = compile_query(config, Registry(config), query)
    coverage = next(c for c in compiled["sql_ast"].ctes if c.name == "coverage_1")
    for index in range(2, len(edges) + 1):
        compiled["sql_ast"].ctes.append(replace(coverage, name=f"coverage_{index}"))
    monkeypatch.setattr(
        runtime_module,
        "_guard_probe_names",
        lambda *a: (
            {"m1": {f"coverage_{i}" for i in range(1, len(edges) + 1)}},
            {"value": {f"coverage_{i}" for i in range(1, len(edges) + 1)}},
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "_compiled_cte_row",
        lambda *a: {f"coverage_{i}__loaded_to": edge for i, edge in enumerate(edges, 1)},
    )
    rows = [{f"{ROLE}__{grain}": bucket, "value": None} for bucket in buckets]
    warnings = _no_data_yet_warnings(None, compiled, rows, query)
    assert bool(warnings) is (expected is not None)
    if expected:
        assert warnings[0]["details"]["measures"] == [
            {
                "id": REVENUE,
                "edge": None if expected == "undated" else expected,
                "edge_source": None if expected == "undated" else "last_bucket",
            }
        ]
