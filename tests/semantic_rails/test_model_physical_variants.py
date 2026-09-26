from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.acceleration.routing import ROUTING_OFF, aggregate_routing
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_variant_package(package_dir: Path) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "variant_demo",
                "name": "variant_demo",
                "description": "Physical variant routing demo",
                "warehouse": "duckdb",
                "default_db": "data/example.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql"},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                }
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "id": "entity.demo_order",
                        "name": "demo.Order",
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    }
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "order_fact",
                "grain": ["order_id"],
                "default_variant": "tx",
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "dimension_id": "dimension.demo_order_ordered_at",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    }
                },
                "dimensions": {
                    "store_id": {
                        "id": "dimension.demo_store_id",
                        "column": "store_id",
                        "kind": "categorical",
                    },
                    "customer_id": {
                        "id": "dimension.demo_customer_id",
                        "column": "customer_id",
                        "kind": "categorical",
                    },
                },
                "measures": {
                    "revenue_usd": {
                        "id": "measure.demo.revenue_usd",
                        "kind": "aggregate",
                        "expr": "order_total_cents / 100.0",
                        "time": "ordered_at",
                        "rollup": "additive",
                    },
                    "order_count": {
                        "id": "measure.demo.order_count",
                        "kind": "entity_count",
                        "entity_key": "order_id",
                        "time": "ordered_at",
                        "rollup": "additive",
                    },
                },
                "variants": {
                    "tx": {
                        "relation": "order_fact",
                        "grain": {"time": "transaction", "entities": ["order"]},
                        "covers": "inherit_all",
                    },
                    "monthly": {
                        "relation": "order_monthly",
                        "grain": {"time": "month", "entities": ["order"]},
                        "time": {"role": "ordered_at", "column": "month_start"},
                        "excludes": {"entities": ["order"], "dimensions": ["customer_id"]},
                        "columns": {
                            "store_id": "store_id",
                            "revenue_usd": "revenue_usd",
                            "order_count": "order_count",
                        },
                        "eligible_time_grains": ["month", "quarter", "year"],
                        "selection": {"priority": 50},
                        "equivalence": {"kind": "exact"},
                    },
                },
            }
        },
    )


def test_model_variants_normalize_to_explicit_aggregate_relations(tmp_path: Path):
    package_dir = tmp_path / "variant_demo"
    _write_variant_package(package_dir)

    config = load_package_config(str(package_dir))

    aggregate = next(row for row in config.aggregate_relations if row.variant_id == "monthly")
    assert aggregate.model_id == "orders"
    assert aggregate.relation == "order_monthly"
    assert aggregate.temporal_role == "temporal_role.demo_order_time"
    assert aggregate.time_column == "month_start"
    assert aggregate.grain == "month"
    assert aggregate.measure_columns == {
        "measure.demo.order_count": "order_count",
        "measure.demo.revenue_usd": "revenue_usd",
    }
    assert aggregate.dimension_columns == {"dimension.demo_store_id": "store_id"}
    assert aggregate.excluded_dimensions == ["dimension.demo_customer_id"]


def test_monthly_query_routes_to_lossless_model_variant(tmp_path: Path):
    package_dir = tmp_path / "variant_demo"
    _write_variant_package(package_dir)
    config = load_package_config(str(package_dir))

    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.revenue_usd",
                        "aggregation": "sum",
                    },
                    "as": "revenue",
                }
            ],
            "group_by": ["dimension.demo_store_id"],
            "time": {
                "temporal_role": "temporal_role.demo_order_time",
                "grain": "month",
            },
        },
    )

    assert "FROM order_monthly" in compiled["sql"]
    assert "FROM order_fact" not in compiled["sql"]
    assert compiled["explain"].performance_plan["aggregate_routing"]["selected"] == [
        "aggregate_relation.orders_monthly"
    ]


def test_missing_variant_dimension_falls_back_to_raw(tmp_path: Path):
    package_dir = tmp_path / "variant_demo"
    _write_variant_package(package_dir)
    config = load_package_config(str(package_dir))

    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "kind": "aggregate",
                        "measure": "measure.demo.revenue_usd",
                        "aggregation": "sum",
                    },
                    "as": "revenue",
                }
            ],
            "group_by": ["dimension.demo_customer_id"],
            "time": {
                "temporal_role": "temporal_role.demo_order_time",
                "grain": "month",
            },
        },
    )

    assert "FROM order_fact" in compiled["sql"]
    assert "FROM order_monthly" not in compiled["sql"]
    assert compiled["explain"].performance_plan["aggregate_routing"]["selected"] == []


_ROLLUP_SEED = """
CREATE TABLE order_fact AS SELECT * FROM (VALUES
 (1, 'c1', 's1', TIMESTAMP '2026-01-10', 10.0), (2, 'c1', 's2', TIMESTAMP '2026-02-10', 20.0),
 (3, 'c2', 's1', TIMESTAMP '2026-03-30', 30.0), (4, 'c1', 's1', TIMESTAMP '2026-03-31', 40.0),
 (5, 'c3', 's2', TIMESTAMP '2026-04-02', 50.0), (6, 'c2', 's1', TIMESTAMP '2026-01-20', 60.0),
 (7, 'c1', 's2', TIMESTAMP '2026-01-25', 5.0), (8, 'c3', 's1', TIMESTAMP '2026-04-01 02:00', 8.0),
 (9, 'c2', 's2', TIMESTAMP '2026-03-31 12:00', 7.0), (10, 'c3', 's1', TIMESTAMP '2026-01-01 00:30', 3.0),
 (11, 'c2', 's2', TIMESTAMP '2026-03-31 23:30', 4.0), (12, 'c1', 's1', TIMESTAMP '2026-04-01 00:30', 2.0)
) t(order_id, customer_id, store_id, ordered_at, amount);
CREATE TABLE order_monthly AS SELECT date_trunc('month', ordered_at) AS month_start, store_id,
 sum(amount) AS revenue, count(DISTINCT order_id) AS order_count,
 count(DISTINCT customer_id) AS buyers FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_weekly AS SELECT date_trunc('week', ordered_at) AS week_start, store_id,
 sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_hourly AS SELECT date_trunc('hour', ordered_at) AS hour_start, store_id,
 sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_minutely AS SELECT date_trunc('minute', ordered_at) AS minute_start, store_id,
 sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_days AS SELECT ordered_at::DATE AS date_day, store_id FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_days_monthly AS SELECT date_trunc('month', date_day) AS month_start, store_id,
 count(DISTINCT date_day) AS days FROM order_days GROUP BY 1, 2;
CREATE TABLE fiscal_days AS SELECT d::DATE AS date_day,
 (date_trunc('quarter', d - INTERVAL 1 MONTH) + INTERVAL 1 MONTH)::DATE AS quarter_start
 FROM range(TIMESTAMP '2025-11-01', TIMESTAMP '2026-08-01', INTERVAL 1 DAY) t(d);
CREATE TABLE order_monthly_s1 AS SELECT date_trunc('month', ordered_at) AS month_start,
 store_id, sum(amount) AS revenue FROM order_fact WHERE store_id = 's1' GROUP BY 1, 2;
"""
_ROLLUP_COLUMNS = {"store_id": "store_id", "revenue": "revenue"}
_MONTHLY = {
    "relation": "order_monthly",
    "grain": {"time": "month", "entities": []},
    "time": {"role": "ordered_at", "column": "month_start"},
    "excludes": {"entities": ["order"], "dimensions": ["customer_id"]},
    "columns": {**_ROLLUP_COLUMNS, "order_count": "order_count", "buyers": "buyers"},
    "eligible_time_grains": ["month", "quarter", "year"],
    "equivalence": {"kind": "exact"},
}
_WEEKLY = {
    **_MONTHLY,
    "relation": "order_weekly",
    "grain": {"time": "week", "entities": []},
    "time": {"role": "ordered_at", "column": "week_start"},
    "columns": _ROLLUP_COLUMNS,
}
_WEEKLY.pop("eligible_time_grains")  # the loader default applies
_HOURLY = {
    **_WEEKLY,
    "relation": "order_hourly",
    "grain": {"time": "hour", "entities": []},
    "time": {"role": "ordered_at", "column": "hour_start"},
}
_MINUTELY = {
    **_HOURLY,
    "relation": "order_minutely",
    "grain": {"time": "minute", "entities": []},
    "time": {"role": "ordered_at", "column": "minute_start"},
}
_S1_ONLY = {
    "id": "aggregate_relation.s1_only",
    "relation": "order_monthly_s1",
    "source_entity": "entity.order",
    "temporal_role": "temporal_role.t",
    "time_column": "month_start",
    "grain": "month",
    "measures": {"measure.revenue": {"column": "revenue", "rollup": "additive"}},
    "dimensions": {"dimension.store_id": {"column": "store_id"}},
    "filters": {"store_id": "s1"},
    "equivalence_kind": "exact",
}
# Days with orders, by store; a day with sales in both stores is in both stores' rows.
_DAYS_MONTHLY = {
    "id": "aggregate_relation.days_monthly",
    "relation": "order_days_monthly",
    "source_entity": "entity.p_fiscal",
    "temporal_role": "temporal_role.day",
    "time_column": "month_start",
    "grain": "month",
    "measures": {"measure.days": {"column": "days", "rollup": "additive"}},
    "dimensions": {"dimension.day_store_id": {"column": "store_id"}},
    "equivalence_kind": "exact",
}
_BIG_ORDERS = {
    "kind": "metric_predicate",
    "entity": "entity.order",
    "input": {"kind": "aggregate", "measure": "measure.revenue", "aggregation": "sum"},
    "op": ">",
    "value": 25,
    "scope_mode": "contextual",
}


def _rollup_package(
    package_dir: Path, variants: dict, aggregate_relations: list, overrides: dict | None = None
) -> None:
    overrides = overrides or {}
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "p",
                "name": "p",
                "warehouse": "duckdb",
                "default_db": "x.duckdb",
                "seed": {"kind": "external"},
                "schema_strict": bool(overrides.get("fact_days")),
            },
            "defaults": {"time": {"timezone": "UTC", "default_query_axis": False}},
            **({"aggregate_relations": aggregate_relations} if aggregate_relations else {}),
        },
    )
    order = {
        "id": "entity.order",
        "key": ["order_id"],
        "model": "orders",
        **overrides.get("entity", {}),
    }
    entities = {"order": order}
    if overrides.get("fiscal_calendar") or overrides.get("fact_days"):  # quarters start in Feb
        entities["fiscal"] = {"kind": "time", "key": ["date_day"], "model": "fiscal_days"}
        day = {"column": "date_day", "kind": "date", "class": "calendar_time"}
        calendar = {"id": "fiscal_days", "relation": "fiscal_days", "calendar_id": "fiscal"}
        calendar |= {"entities": {"fiscal": {}}, "times": {"date_day": day}}
        calendar["dimensions"] = {"quarter_start": {"kind": "date"}}
        _write_yaml(package_dir / "models" / "fiscal_days.yml", {"model": calendar})
    if overrides.get("fact_days"):  # a fact model whose rows repeat a day across stores
        day = {"id": "temporal_role.day", "column": "date_day", "kind": "date", "default": True}
        fact = {"id": "order_days", "kind": "fact", "relation": "order_days"}
        fact |= {"time_entity": "fiscal", "time_column": "date_day"}
        fact["times"] = {"date_day": {**day, "class": "event_time"}}
        fact["dimensions"] = {"store_id": {"id": "dimension.day_store_id", "kind": "categorical"}}
        fact["measures"] = {
            "days": {"id": "measure.days", "kind": "entity_count", "time": "date_day"}
        }
        _write_yaml(package_dir / "models" / "order_days.yml", {"model": fact})
    _write_yaml(package_dir / "graph.yml", {"graph": {"entities": entities}})
    dims = {
        key: {"id": f"dimension.{key}", "column": key, "kind": "categorical"}
        for key in ("store_id", "customer_id")
    }
    time = {"id": "temporal_role.t", "dimension_id": "dimension.ordered_at", "column": "ordered_at"}
    time.update(overrides.get("time", {}))
    measure = {"time": "ordered_at", "rollup": "additive"}
    model = {
        "id": "orders",
        "entity": "order",
        "relation": "order_fact",
        "grain": ["order_id"],
        "default_variant": "tx",
        "times": {
            "ordered_at": {**time, "kind": "timestamp", "class": "event_time", "default": True}
        },
        "dimensions": dims,
        "measures": {
            "revenue": {"id": "measure.revenue", "kind": "aggregate", "expr": "amount", **measure},
            "order_count": {
                "id": "measure.order_count",
                "kind": "entity_count",
                "entity_key": "order_id",
                **measure,
            },
            "buyers": {
                "id": "measure.buyers",
                "kind": "entity_count",
                "entity_key": "customer_id",
                **measure,
            },
        },
        "variants": {
            "tx": {
                "relation": "order_fact",
                "grain": {"time": "transaction", "entities": ["order"]},
                "covers": "inherit_all",
            },
            **variants,
        },
    }
    _write_yaml(package_dir / "models" / "orders.yml", {"model": model})


def _rollup_query(measure: str, aggregation: str, grain: str, **time) -> dict:
    return {
        "version": 1,
        "select": [
            {
                "expression": {"kind": "aggregate", "measure": measure, "aggregation": aggregation},
                "as": "v",
            }
        ],
        "time": {"temporal_role": "temporal_role.t", "grain": grain, **time},
    }


def _with_filter(query: dict, clause: dict) -> dict:
    query["select"][0]["expression"]["filter"] = {"all": [clause]}
    return query


_PREDICATE_QUERY = _with_filter(
    _rollup_query("measure.revenue", "sum", "month"), {"expression": _BIG_ORDERS}
)
_NO_TIME_QUERY = _rollup_query("measure.revenue", "sum", "month")
del _NO_TIME_QUERY["time"]
_TWO_LEAVES = _rollup_query("measure.revenue", "sum", "quarter")
_TWO_LEAVES["select"].append(
    {**_rollup_query("measure.buyers", "count_distinct", "quarter")["select"][0], "as": "b"}
)
_DISTRIBUTION = _rollup_query("measure.revenue", "sum", "quarter")
_DISTRIBUTION["select"][0]["expression"] = {
    "kind": "distribution",
    "function": "avg",
    "over": {
        "kind": "entity_value",
        "entity": "entity.order",
        "input": {"measure": "measure.revenue"},
    },
}
_DAYS_QUERY = {
    **_rollup_query("measure.days", "count_distinct", "quarter"),
    "time": {"temporal_role": "temporal_role.day", "grain": "quarter"},
}


_MONTHLY_ONLY = ({"monthly": _MONTHLY}, [])
_BUYERS, _REVENUE = "measure.buyers", "measure.revenue"


@pytest.mark.parametrize(
    ("rollups", "query", "reason"),
    [
        # Wrong answers on DuckDB before the guards; each must now run on the base tables.
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_BUYERS, "count_distinct", "quarter"),
            "aggregation_not_reaggregable",
            id="distinct-buyers-month-to-quarter",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_BUYERS, "count_distinct", "month"),
            "aggregation_not_reaggregable",
            id="distinct-buyers-across-stores",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, [], {"entity": {"key": ["customer_id", "order_id"]}}),
            _rollup_query(_BUYERS, "count_distinct", "quarter"),
            "aggregation_not_reaggregable",
            id="distinct-buyers-composite-key",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, [], {"entity": {"key": ["customer_id"]}}),
            _rollup_query(_BUYERS, "count_distinct", "quarter"),
            "aggregation_not_reaggregable",
            id="distinct-entity-key-not-row-grain",
        ),
        pytest.param(
            (
                {"monthly": _MONTHLY},
                [],
                {"time": {"timezone": "America/New_York", "column_timezone": "UTC"}},
            ),
            _rollup_query(_REVENUE, "sum", "month"),
            "timezone_mismatch",
            id="role-converts-timezone",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, [], {"fiscal_calendar": True}),
            _rollup_query(_REVENUE, "sum", "quarter", calendar_id="fiscal", fill=True),
            "calendar_mismatch",
            id="non-default-calendar",
        ),
        pytest.param(
            ({"weekly": _WEEKLY}, []),
            _rollup_query(_REVENUE, "sum", "month"),
            "unsupported_query_grain",
            id="weekly-rollup-default-grains",
        ),
        pytest.param(
            ({"weekly": {**_WEEKLY, "eligible_time_grains": ["week", "month"]}}, []),
            _rollup_query(_REVENUE, "sum", "month"),
            "non_nesting_grain",
            id="weekly-rollup-declares-month",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "sum", "month", start="2026-01-15", end="2026-03-31"),
            "time_bounds_not_aligned",
            id="bounds-not-on-month-boundaries",
        ),
        pytest.param(
            ({}, [_S1_ONLY]),
            _rollup_query(_REVENUE, "sum", "month"),
            "rollup_filter_not_implied",
            id="rollup-own-filter",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _PREDICATE_QUERY,
            "metric_predicate_filter",
            id="measure-metric-predicate",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            {
                **_rollup_query(_REVENUE, "sum", "month"),
                "metric_filters": [{"expression": _BIG_ORDERS, "op": "=", "value": True}],
            },
            "metric_predicate_filter",
            id="query-metric-predicate",
        ),
        pytest.param(
            ({}, [_DAYS_MONTHLY], {"fact_days": True}),
            _DAYS_QUERY,
            "aggregation_not_reaggregable",
            id="distinct-fact-model-row-key",
        ),
        pytest.param(_MONTHLY_ONLY, _NO_TIME_QUERY, "missing_query_time_grain", id="no-time"),
        pytest.param(
            ({"hourly": _HOURLY}, []),
            _rollup_query(_REVENUE, "sum", "day", start="2026-01-10T05:00:00"),
            "time_bounds_not_aligned",
            id="hourly-bound-inside-a-day",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "sum", "month", start="2026-02-01T00:00:00.0000001"),
            "time_bounds_not_aligned",
            id="bound-past-microseconds",
        ),
        # Exact rollup answers that must keep routing.
        pytest.param(_MONTHLY_ONLY, _rollup_query(_REVENUE, "sum", "quarter"), None, id="sum"),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query("measure.order_count", "count_distinct", "quarter"),
            None,
            id="distinct-fact-key",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "sum", "quarter", start="2026-02-01", end="2026-04-01"),
            None,
            id="aligned-bounds",
        ),
        pytest.param(
            ({"weekly": _WEEKLY}, []), _rollup_query(_REVENUE, "sum", "week"), None, id="week"
        ),
        pytest.param(
            ({"hourly": _HOURLY}, []),
            _rollup_query(_REVENUE, "sum", "day", start="2026-01-01", end="2026-04-01"),
            None,
            id="hourly-day-bounds",
        ),
        pytest.param(
            ({"minutely": _MINUTELY}, []),
            _rollup_query(_REVENUE, "sum", "day", start="2026-01-01", end="2026-04-01"),
            None,
            id="minutely-day-bounds",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, [], {"time": {"timezone": "America/New_York"}}),
            _rollup_query(_REVENUE, "sum", "month"),
            None,
            id="role-zone-without-conversion",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, [], {"time": {"timezone": "UTC", "column_timezone": "UTC"}}),
            _rollup_query(_REVENUE, "sum", "month"),
            None,
            id="role-same-zones",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "sum", "quarter", calendar_id="default"),
            None,
            id="default-calendar",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _with_filter(
                _rollup_query(_REVENUE, "sum", "month"),
                {"field": "dimension.store_id", "op": "=", "value": "s1"},
            ),
            None,
            id="measure-dimension-filter",
        ),
    ],
)
def test_rollup_routing_matches_base_tables(
    tmp_path: Path, rollups: tuple, query: dict, reason: str | None
):
    routing = _routed_answers(tmp_path, rollups, query)
    explicit = [row["id"] for row in rollups[1]]
    relation = (explicit or [f"aggregate_relation.orders_{next(iter(rollups[0]))}"])[0]

    assert routing["selected"] == ([] if reason else [relation])
    assert _decisions(routing) == {f"leaf_1:{relation}": reason or "selected"}


@pytest.mark.parametrize(
    ("rollups", "query", "decisions"),
    [
        pytest.param(
            ({"monthly": _MONTHLY}, [_S1_ONLY]),
            _rollup_query(_REVENUE, "sum", "quarter"),
            {
                "leaf_1:aggregate_relation.orders_monthly": "selected",
                "leaf_1:aggregate_relation.s1_only": "rollup_filter_not_implied",
            },
            id="filtered-rollup-beside-an-exact-one",
        ),
        pytest.param(
            ({"monthly": _MONTHLY, "spare": {**_MONTHLY, "selection": {"priority": -1}}}, []),
            _rollup_query(_REVENUE, "sum", "quarter"),
            {
                "leaf_1:aggregate_relation.orders_monthly": "selected",
                "leaf_1:aggregate_relation.orders_spare": "eligible",
            },
            id="lower-priority-rollup",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _TWO_LEAVES,
            {
                "leaf_1:aggregate_relation.orders_monthly": "selected",
                "leaf_2:aggregate_relation.orders_monthly": "aggregation_not_reaggregable",
            },
            id="one-leaf-routed-one-on-base-tables",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _DISTRIBUTION,
            {"leaf_1:aggregate_relation.orders_monthly": "query_shape_not_routed"},
            id="distribution-runs-on-base-tables",
        ),
    ],
)
def test_routing_report_lists_each_rollup_per_leaf(
    tmp_path: Path, rollups: tuple, query: dict, decisions: dict
):
    assert _decisions(_routed_answers(tmp_path, rollups, query)) == decisions


def _routed_answers(tmp_path: Path, rollups: tuple, query: dict) -> dict:
    """Check that the package without its rollups, with them, and with routing off all return
    the same rows; return the routing report with them."""
    connection = duckdb.connect()
    connection.execute(_ROLLUP_SEED)
    compiled = {}
    for name, package in {"base": ({}, [], *rollups[2:]), "rollup": rollups}.items():
        _rollup_package(tmp_path / name, *package)
        config = load_package_config(str(tmp_path / name))
        compiled[name] = compile_query(config, Registry(config), query)
    with aggregate_routing(False):
        compiled["off"] = compile_query(config, Registry(config), query)
    rows = {name: sorted(connection.execute(c["sql"]).fetchall()) for name, c in compiled.items()}

    assert rows["rollup"] == rows["base"] == rows["off"]
    off = compiled["off"]["explain"].performance_plan["aggregate_routing"]
    assert off["selected"] == []
    assert {row["reason"] for row in off["candidates"]} == {ROUTING_OFF}
    routing = compiled["rollup"]["explain"].performance_plan["aggregate_routing"]
    reported = {
        row["relation_id"] for row in routing["candidates"] if row["decision"] == "selected"
    }
    assert reported == set(routing["selected"])  # the report agrees with the scans
    return routing


def _decisions(routing: dict) -> dict[str, str]:
    return {
        f"{row['leaf_id']}:{row['relation_id']}": row["reason"] or row["decision"]
        for row in routing["candidates"]
    }


def test_routing_switch_applies_on_a_warm_compile_cache(tmp_path: Path, monkeypatch):
    _rollup_package(tmp_path / "p", {"monthly": _MONTHLY}, [])
    runtime = Runtime.from_path(str(tmp_path / "p"))
    payload = {**_rollup_query(_REVENUE, "sum", "quarter"), "verbosity": "full"}

    def compile_once(runtime: Runtime) -> tuple[list[str], bool]:
        compiled = runtime.compile(payload)
        routing = compiled["performance_plan"]["aggregate_routing"]
        return routing["selected"], compiled["compile_stats"]["cache_hit"]

    routed = ["aggregate_relation.orders_monthly"]
    assert compile_once(runtime) == (routed, False)
    assert compile_once(runtime) == (routed, True)
    runtime.set_aggregate_routing(False)
    assert compile_once(runtime) == ([], False)
    runtime.set_aggregate_routing(True)
    assert compile_once(runtime) == (routed, True)
    with pytest.raises(TypeError):
        runtime.set_aggregate_routing("off")  # type: ignore[arg-type]

    monkeypatch.setenv("SEMANTIC_RAILS_AGGREGATE_ROUTING", "OFF")
    assert compile_once(Runtime.from_path(str(tmp_path / "p"))) == ([], False)
    monkeypatch.setenv("SEMANTIC_RAILS_AGGREGATE_ROUTING", "no")
    with pytest.raises(SemanticLayerError, match="must be 'on' or 'off'"):
        Runtime.from_path(str(tmp_path / "p"))
