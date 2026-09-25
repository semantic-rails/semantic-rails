from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry


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
 (7, 'c1', 's2', TIMESTAMP '2026-01-25', 5.0), (8, 'c3', 's1', TIMESTAMP '2026-04-01 02:00', 8.0)
) t(order_id, customer_id, store_id, ordered_at, amount);
CREATE TABLE order_monthly AS SELECT date_trunc('month', ordered_at) AS month_start, store_id,
 sum(amount) AS revenue, count(DISTINCT order_id) AS order_count,
 count(DISTINCT customer_id) AS buyers FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_weekly AS SELECT date_trunc('week', ordered_at) AS week_start, store_id,
 sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
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
    if overrides.get("fiscal_calendar"):  # quarters start in February
        entities["fiscal"] = {"kind": "time", "key": ["date_day"], "model": "fiscal_days"}
        day = {"column": "date_day", "kind": "date", "class": "calendar_time"}
        calendar = {"id": "fiscal_days", "relation": "fiscal_days", "calendar_id": "fiscal"}
        calendar |= {"entities": {"fiscal": {}}, "times": {"date_day": day}}
        calendar["dimensions"] = {"quarter_start": {"kind": "date"}}
        _write_yaml(package_dir / "models" / "fiscal_days.yml", {"model": calendar})
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


_PREDICATE_QUERY = _rollup_query("measure.revenue", "sum", "month")
_PREDICATE_QUERY["select"][0]["expression"]["filter"] = {"all": [{"expression": _BIG_ORDERS}]}


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
    ],
)
def test_rollup_routing_matches_base_tables(
    tmp_path: Path, rollups: tuple, query: dict, reason: str | None
):
    connection = duckdb.connect()
    connection.execute(_ROLLUP_SEED)
    answers = {}
    for name, package in {"base": ({}, [], *rollups[2:]), "rollup": rollups}.items():
        _rollup_package(tmp_path / name, *package)
        config = load_package_config(str(tmp_path / name))
        answers[name] = compile_query(config, Registry(config), query)
    rows = {name: sorted(connection.execute(c["sql"]).fetchall()) for name, c in answers.items()}

    assert rows["rollup"] == rows["base"]
    compiled = answers["rollup"]
    selected = compiled["explain"].performance_plan["aggregate_routing"]["selected"]
    assert selected == ([] if reason else [f"aggregate_relation.orders_{next(iter(rollups[0]))}"])
    rejections = compiled["logical_plan"].measure_plans[0].aggregate_relation_rejections
    assert list(rejections.values()) == ([reason] if reason else [])
