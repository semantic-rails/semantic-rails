from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.acceleration import routing
from semantic_rails.acceleration.routing import ROUTING_OFF, aggregate_routing
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.indexes import rollup_dimension_entities
from semantic_rails.config import _merge_package_dir, load_package_config
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
                "schema_strict": True,
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
                        "as": "entity.demo_order",
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
                "entities": {"order": {}},
                "relation": "order_fact",
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
                        "as": "dimension.demo_store_id",
                        "column": "store_id",
                        "kind": "categorical",
                    },
                    "customer_id": {
                        "as": "dimension.demo_customer_id",
                        "column": "customer_id",
                        "kind": "categorical",
                    },
                },
                "measures": {
                    "revenue_usd": {
                        "as": "measure.demo.revenue_usd",
                        "kind": "aggregate",
                        "expr": "order_total_cents / 100.0",
                        "time": "ordered_at",
                        "rollup": "additive",
                    },
                    "order_count": {
                        "as": "measure.demo.order_count",
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


@pytest.mark.parametrize(
    ("dimension", "selected_relation", "other_relation", "selected"),
    [
        pytest.param(
            "dimension.demo_store_id",
            "order_monthly",
            "order_fact",
            ["aggregate_relation.orders_monthly"],
            id="lossless-variant",
        ),
        pytest.param(
            "dimension.demo_customer_id",
            "order_fact",
            "order_monthly",
            [],
            id="missing-dimension-fallback",
        ),
    ],
)
def test_monthly_query_variant_routing(
    tmp_path: Path, dimension, selected_relation, other_relation, selected
):
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
            "group_by": [dimension],
            "time": {
                "temporal_role": "temporal_role.demo_order_time",
                "grain": "month",
            },
        },
    )

    assert f"FROM {selected_relation}" in compiled["sql"]
    assert f"FROM {other_relation}" not in compiled["sql"]
    assert compiled["explain"].performance_plan["aggregate_routing"]["selected"] == selected


_ROLLUP_SEED = """
CREATE TABLE order_fact AS SELECT * FROM (VALUES
 (1, 'c1', 's1', TIMESTAMP '2026-01-10', 10.0), (2, 'c1', 's2', TIMESTAMP '2026-02-10', 20.0),
 (3, 'c2', 's1', TIMESTAMP '2026-03-30', 30.0), (4, 'c1', 's1', TIMESTAMP '2026-03-31', 40.0),
 (5, 'c3', 's2', TIMESTAMP '2026-04-02', 50.0), (6, 'c2', 's1', TIMESTAMP '2026-01-20', 60.0),
 (7, 'c1', 's2', TIMESTAMP '2026-01-25', 5.0), (8, 'c3', 's1', TIMESTAMP '2026-04-01 02:00', 8.0),
 (9, 'c2', 's2', TIMESTAMP '2026-03-31 12:00', 7.0), (10, 'c3', 's1', TIMESTAMP '2026-01-01 00:30', 3.0),
 (11, 'c2', 's2', TIMESTAMP '2026-03-31 23:30', 4.0), (12, 'c1', 's1', TIMESTAMP '2026-04-01 00:30', 2.0),
 (13, 'c9', 's1', TIMESTAMP '2026-02-15', 9.0)
) t(order_id, customer_id, store_id, ordered_at, amount);
CREATE TABLE order_monthly AS SELECT date_trunc('month', ordered_at) AS month_start, store_id,
 sum(amount) AS revenue, count(DISTINCT order_id) AS order_count,
 count(DISTINCT customer_id) AS buyers, min(amount) AS min_amount, max(amount) AS max_amount
 FROM order_fact GROUP BY 1, 2;
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
CREATE TABLE customers AS SELECT * FROM (VALUES ('c1', 'east', 1), ('c2', 'west', 2),
 ('c3', 'east', 3)) t(customer_id, region, weight);
ALTER TABLE order_fact ADD COLUMN ship_to_id VARCHAR;
UPDATE order_fact SET ship_to_id = CASE customer_id WHEN 'c1' THEN 'c2' ELSE 'c1' END;
CREATE TABLE order_region_monthly AS SELECT date_trunc('month', ordered_at) AS month_start,
 region, sum(amount) AS revenue FROM order_fact JOIN customers USING (customer_id) GROUP BY 1, 2;
CREATE TABLE order_region_left_monthly AS SELECT date_trunc('month', ordered_at) AS month_start,
 region, sum(amount) AS revenue FROM order_fact LEFT JOIN customers USING (customer_id)
 GROUP BY 1, 2;
CREATE TABLE order_ship_to_monthly AS SELECT date_trunc('month', ordered_at) AS month_start,
 ship_to_id AS customer_key, sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_buyer_monthly AS SELECT date_trunc('month', ordered_at) AS month_start,
 customer_id AS customer_key, sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
CREATE TABLE order_lines AS SELECT * FROM (VALUES (1, 1, 'a'), (2, 1, 'b'), (3, 3, 'a'),
 (4, 5, 'b')) t(line_id, order_id, product);
CREATE TABLE order_product_monthly AS SELECT date_trunc('month', ordered_at) AS month_start,
 product, sum(amount) AS revenue FROM order_fact JOIN order_lines USING (order_id) GROUP BY 1, 2;
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
_S1_ONLY = {**_MONTHLY, "filters": {"store_id": "s1"}}
# Days with orders, by store; a day with sales in both stores is in both stores' rows.
_DAYS_MONTHLY = {
    "id": "aggregate_relation.days_monthly",
    "relation": "order_days_monthly",
    "grain": {"time": "month", "entities": []},
    "time": {"role": "date_day", "column": "month_start"},
    "columns": {"days": {"column": "days", "rollup": "additive"}, "store_id": "store_id"},
}
_SHIP_TO, _BUYER = "relationship.orders_ship_to", "relationship.orders_customer"
_LINE_ORDER = "relationship.order_lines_order"
_BIG_ORDERS = {
    "kind": "metric_predicate",
    "entity": "entity.order",
    "input": {"kind": "aggregate", "measure": "measure.revenue", "aggregation": "sum"},
    "op": ">",
    "value": 25,
    "scope_mode": "contextual",
}


def _rollup_package(package_dir: Path, variants: dict, overrides: dict | None = None) -> None:
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
                "schema_strict": True,
            },
            "defaults": {"time": {"timezone": "UTC"}},
        },
    )
    order = {
        "as": "entity.order",
        "key": ["order_id"],
        "model": "orders",
        **overrides.get("entity", {}),
    }
    entities = {"order": order}
    if overrides.get("fiscal_calendar") or overrides.get("fact_days"):  # quarters start in Feb
        entities["fiscal"] = {"kind": "time", "key": ["date_day"], "model": "fiscal_days"}
        day = {"column": "date_day", "kind": "date", "class": "calendar_time"}
        # A fact model's days bucket on the default calendar; fiscal ones are refused.
        calendar_id = "fiscal" if overrides.get("fiscal_calendar") else "default"
        calendar = {"id": "fiscal_days", "relation": "fiscal_days", "calendar_id": calendar_id}
        calendar |= {"entities": {"fiscal": {}}, "times": {"date_day": day}}
        calendar["dimensions"] = {"quarter_start": {"kind": "date"}}
        _write_yaml(package_dir / "models" / "fiscal_days.yml", {"model": calendar})
    if overrides.get("fact_days"):  # a fact model whose rows repeat a day across stores
        day = {"id": "temporal_role.day", "column": "date_day", "kind": "date", "default": True}
        fact = {"id": "order_days", "kind": "fact", "relation": "order_days"}
        fact |= {"time_entity": "fiscal", "time_column": "date_day"}
        fact["times"] = {"date_day": {**day, "class": "event_time"}}
        fact["dimensions"] = {"store_id": {"as": "dimension.day_store_id", "kind": "categorical"}}
        fact["measures"] = {
            "days": {"as": "measure.days", "kind": "entity_count", "time": "date_day"}
        }
        fact["measures"]["days"].update(overrides.get("day_measure", {}))
        fact["variants"] = variants
        _write_yaml(package_dir / "models" / "order_days.yml", {"model": fact})
    graph: dict = {"entities": entities, "relationships": {}}
    if overrides.get("lines"):  # an order has many lines
        entities["line"] = {"as": "entity.line", "key": ["line_id"], "model": "order_lines"}
        product = {"as": "dimension.product", "column": "product", "kind": "categorical"}
        lines = {
            "id": "order_lines",
            "entities": {"line": {}, "order": {}},
            "relation": "order_lines",
        }
        lines["dimensions"] = {"product": product}
        graph["relationships"]["lines_order"] = {
            "id": _LINE_ORDER,
            "entities": ["line", "order"],
            "cardinality": "many_to_one",
        }
        _write_yaml(package_dir / "models" / "order_lines.yml", {"model": lines})
    if "ship_to" in overrides:  # orders reach customers by the buyer, or also by the ship-to
        entities["customer"] = {
            "as": "entity.customer",
            "key": ["customer_id"],
            "model": "customers",
        }
        region = {"as": "dimension.region", "column": "region", "kind": "categorical"}
        customers = {"id": "customers", "entities": {"customer": {}}, "relation": "customers"}
        customers["dimensions"] = {"region": region}
        _write_yaml(package_dir / "models" / "customers.yml", {"model": customers})
        if overrides["ship_to"] is not None:  # None: the buyer relationship only
            route = [_SHIP_TO] if overrides["ship_to"] else [_BUYER]
            pin = {"source_entity": "order", "target_entity": "customer"}
            graph["path_preferences"] = [pin | {"relationship_path": route}]
        graph["relationships"].update(
            {
                key: {
                    "id": f"relationship.orders_{key}",
                    "entities": ["order", "customer"],
                    "via": [key + "_id"],
                    "cardinality": "many_to_one",
                }
                for key in ("customer", "ship_to")[: 1 if overrides.get("ship_to") is None else 2]
            }
        )
    _write_yaml(package_dir / "graph.yml", {"graph": graph})
    dims = {
        key: {"as": f"dimension.{key}", "column": key, "kind": "categorical"}
        for key in ("store_id", "customer_id")
    }
    time = {"id": "temporal_role.t", "dimension_id": "dimension.ordered_at", "column": "ordered_at"}
    time.update(overrides.get("time", {}))
    measure = {"time": "ordered_at", "rollup": "additive"}
    stock = {"accumulation": {"kind": "stock", "snapshot": "end_of_period"}}
    model = {
        "id": "orders",
        "entities": {"order": {}, **({"customer": {}} if "ship_to" in overrides else {})},
        "relation": "order_fact",
        "times": {
            "ordered_at": {**time, "kind": "timestamp", "class": "event_time", "default": True}
        },
        "dimensions": dims,
        "measures": {
            "revenue": {"as": "measure.revenue", "kind": "aggregate", "expr": "amount", **measure},
            "order_count": {
                "as": "measure.order_count",
                "kind": "entity_count",
                "entity_key": "order_id",
                **measure,
            },
            "buyers": {
                "as": "measure.buyers",
                "kind": "entity_count",
                "entity_key": "customer_id",
                **measure,
            },
            "balance": {"as": "measure.balance", "kind": "aggregate", "expr": "amount", **measure}
            | stock,
            **(
                {
                    "weight": {
                        "as": "measure.weight",
                        "kind": "aggregate",
                        "expr": "customers.weight",
                    }
                }
                if "ship_to" in overrides
                else {}
            ),
        },
        "variants": {} if overrides.get("fact_days") else variants,
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
_SUM_AND_DISTRIBUTION = _rollup_query("measure.revenue", "sum", "quarter")
_SUM_AND_DISTRIBUTION["select"].append({**_DISTRIBUTION["select"][0], "as": "d"})
_DAYS_QUERY = {
    **_rollup_query("measure.days", "count_distinct", "quarter"),
    "time": {"temporal_role": "temporal_role.day", "grain": "quarter"},
}


_MONTHLY_ONLY = ({"monthly": _MONTHLY},)
_BUYERS, _REVENUE = "measure.buyers", "measure.revenue"
_STORE = "dimension.store_id"


def _monthly(**columns) -> tuple:
    return ({"monthly": {**_MONTHLY, "columns": {**_MONTHLY["columns"], **columns}}},)


def _grouped(query: dict, *dims: str, where: list | None = None) -> dict:
    return {**query, "group_by": list(dims), **({"where": where} if where else {})}


_DISTINCT_BUYERS = _monthly(buyers={"column": "buyers", "holds": "count_distinct"})
_BUYERS_MONTH = _rollup_query(_BUYERS, "count_distinct", "month")
_REGION = {
    "id": "aggregate_relation.region",
    "relation": "order_region_monthly",
    "grain": {"time": "month", "entities": []},
    "time": {"role": "ordered_at", "column": "month_start"},
    "excludes": {
        "dimensions": ["store_id", "customer_id"],
        "measures": ["order_count", "buyers", "balance", "weight"],
    },
    "columns": {
        "revenue": {"column": "revenue", "rollup": "additive"},
        "dimension.region": {"column": "region", "path": [_BUYER]},
    },
}
_NO_PATH = {**_REGION, "columns": {**_REGION["columns"], "dimension.region": {"column": "region"}}}
_NO_ROLE = {
    **_MONTHLY,
    "id": "aggregate_relation.no_role",
    "time": {"role": "", "column": "month_start"},
    "excludes": {
        **_MONTHLY["excludes"],
        "measures": ["order_count", "buyers", "balance", "weight"],
    },
    "columns": {"store_id": "store_id", "revenue": {"column": "revenue", "rollup": "additive"}},
}
_UNDECLARED_COLUMNS = {"store_id": "store_id", "revenue": {"column": "revenue", "rollup": ""}}
_BY_REGION = _grouped(_rollup_query(_REVENUE, "sum", "month"), "dimension.region")
_CUSTOMER_KEY = "dimension.p_customer_id"  # the customer entity's key, read from a foreign key
_BUYER_KEY = {
    **_REGION,
    "id": "aggregate_relation.buyer",
    "relation": "order_buyer_monthly",
    "columns": {
        "revenue": "revenue",
        "dimension.p_customer_id": {"column": "customer_key", "path": [_BUYER]},
    },
}
_PRODUCT = {
    **_REGION,
    "id": "aggregate_relation.product",
    "relation": "order_product_monthly",
    "columns": {
        "revenue": "revenue",
        "dimension.product": {"column": "product", "path": [_LINE_ORDER]},
    },
}
_SHIP_TO_KEY = {
    **_REGION,
    "id": "aggregate_relation.ship_to",
    "relation": "order_ship_to_monthly",
    "columns": {
        "revenue": "revenue",
        _CUSTOMER_KEY: {"column": "customer_key", "path": [_SHIP_TO]},
    },
}


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
            ({"monthly": _MONTHLY}, {"entity": {"key": ["customer_id", "order_id"]}}),
            _rollup_query(_BUYERS, "count_distinct", "quarter"),
            "aggregation_not_reaggregable",
            id="distinct-buyers-composite-key",
        ),
        pytest.param(
            # A related entity's key cannot make monthly distinct-buyer counts safe to re-sum into quarterly totals.
            ({"monthly": _MONTHLY}, {"ship_to": None}),
            _rollup_query(_BUYERS, "count_distinct", "quarter"),
            "aggregation_not_reaggregable",
            id="distinct-entity-key-not-row-grain",
        ),
        pytest.param(
            (
                {"monthly": _MONTHLY},
                {"time": {"timezone": "America/New_York", "column_timezone": "UTC"}},
            ),
            _rollup_query(_REVENUE, "sum", "month"),
            "timezone_mismatch",
            id="role-converts-timezone",
        ),
        pytest.param(
            ({"weekly": _WEEKLY},),
            _rollup_query(_REVENUE, "sum", "month"),
            "unsupported_query_grain",
            id="weekly-rollup-default-grains",
        ),
        pytest.param(
            ({"weekly": {**_WEEKLY, "eligible_time_grains": ["week", "month"]}},),
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
            ({"days_monthly": _DAYS_MONTHLY}, {"fact_days": True}),
            _DAYS_QUERY,
            "aggregation_not_reaggregable",
            id="distinct-fact-model-row-key",
        ),
        pytest.param(
            (
                {"days_monthly": _DAYS_MONTHLY},
                {"fact_days": True, "day_measure": {"kind": "aggregate", "expr": "1"}},
            ),
            {
                **_DAYS_QUERY,
                "select": _rollup_query("measure.days", "sum", "quarter")["select"],
            },
            None,
            id="fact-model-additive",
        ),
        pytest.param(_MONTHLY_ONLY, _NO_TIME_QUERY, "missing_query_time_grain", id="no-time"),
        pytest.param(
            ({"hourly": _HOURLY},),
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
        # Rollup columns declare what they hold (`holds:`); each pair of what a column holds and
        # what the query asks for must re-aggregate exactly.
        pytest.param(
            _monthly(revenue={"column": "max_amount", "holds": "max"}),
            _rollup_query(_REVENUE, "max", "quarter"),
            None,
            id="max",
        ),
        pytest.param(
            _monthly(revenue={"column": "min_amount", "holds": "min"}),
            _grouped(_rollup_query(_REVENUE, "min", "year"), _STORE),
            None,
            id="min-by-store",
        ),
        pytest.param(
            _monthly(revenue={"column": "max_amount", "holds": "max"}),
            _rollup_query(_REVENUE, "sum", "quarter"),
            "aggregation_not_reaggregable",
            id="sum-from-a-max-column",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "max", "quarter"),
            "aggregation_not_reaggregable",
            id="max-from-a-sum-column",
        ),
        pytest.param(
            _monthly(revenue={"column": "max_amount", "holds": "max", "aggregation": "sum"}),
            _rollup_query(_REVENUE, "max", "quarter"),
            "unsupported_rollup_aggregation",
            id="max-column-re-added",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "avg", "quarter"),
            "unsupported_query_aggregation",
            id="avg",
        ),
        pytest.param(
            _monthly(balance="revenue"),
            _rollup_query("measure.balance", "sum", "quarter"),
            "aggregation_not_reaggregable",
            id="stock-measure",  # the base path sums each order's last value per quarter
        ),
        pytest.param(
            _monthly(balance={"column": "revenue", "holds": "sum"}),
            _rollup_query("measure.balance", "sum", "quarter"),
            "aggregation_not_reaggregable",
            id="stock-measure-declared-sum",
        ),
        # A distinct count of a key other than the row key answers only one rollup row per
        # output row: the rollup's grain, and every rollup dimension grouped or pinned by `=`.
        pytest.param(
            _DISTINCT_BUYERS,
            _grouped(_BUYERS_MONTH, _STORE),
            None,
            id="distinct-by-every-dimension",
        ),
        pytest.param(
            _DISTINCT_BUYERS,
            _grouped(_BUYERS_MONTH, where=[{"field": _STORE, "op": "=", "value": "s1"}]),
            None,
            id="distinct-dimension-pinned",
        ),
        pytest.param(
            _DISTINCT_BUYERS,
            _with_filter(
                _rollup_query(_BUYERS, "count_distinct", "month"),
                {"field": _STORE, "op": "=", "value": "s2"},
            ),
            None,
            id="distinct-dimension-pinned-by-measure-filter",
        ),
        pytest.param(
            _DISTINCT_BUYERS,
            _grouped(_BUYERS_MONTH, where=[{"field": _STORE, "op": "in", "value": ["s1", "s2"]}]),
            "aggregation_not_reaggregable",
            id="distinct-dimension-in-list",
        ),
        pytest.param(
            _DISTINCT_BUYERS,
            _grouped(_BUYERS_MONTH, where=[{"field": _STORE, "op": ">=", "value": "s1"}]),
            "aggregation_not_reaggregable",
            id="distinct-dimension-range",
        ),
        pytest.param(
            _DISTINCT_BUYERS,
            _BUYERS_MONTH,
            "aggregation_not_reaggregable",
            id="distinct-dimension-not-grouped",
        ),
        pytest.param(
            _DISTINCT_BUYERS,
            _grouped(_rollup_query(_BUYERS, "count_distinct", "quarter"), _STORE),
            "aggregation_not_reaggregable",
            id="distinct-coarser-grain",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _grouped(_BUYERS_MONTH, _STORE),
            "aggregation_not_reaggregable",
            id="distinct-without-holds",
        ),
        pytest.param(
            (
                {
                    "monthly": {
                        **_DISTINCT_BUYERS[0]["monthly"],
                        "grain": {"time": "month", "entities": ["order"]},
                    }
                },
            ),
            _grouped(_BUYERS_MONTH, _STORE),
            "aggregation_not_reaggregable",
            id="distinct-entity-grain-not-a-dimension",  # rows per order can't be grouped
        ),
        # A column pre-joined from another model routes only along the query's join path.
        pytest.param(
            ({"region": _REGION}, {"ship_to": False}), _BY_REGION, None, id="pre-joined-path"
        ),
        pytest.param(
            ({"region": _REGION}, {"ship_to": False}),
            _rollup_query(_REVENUE, "sum", "month"),
            "join_path_mismatch",
            id="pre-joined-column-unused",  # its inner join left out order 13 (no such customer)
        ),
        pytest.param(
            ({"no_role": _NO_ROLE}, {"ship_to": False}),
            _rollup_query(_REVENUE, "sum", "month"),
            "temporal_role_mismatch",
            id="rollup-without-a-time-role",
        ),
        pytest.param(
            ({"region": _REGION}, {"ship_to": True}),
            _BY_REGION,
            "join_path_mismatch",
            id="pre-joined-other-path",  # the query joins customers by the ship-to
        ),
        pytest.param(
            ({"no_path": _NO_PATH}, {"ship_to": False}),
            _BY_REGION,
            "join_path_mismatch",
            id="pre-joined-path-undeclared",
        ),
        pytest.param(
            ({"region": _REGION}, {"ship_to": False}),
            _grouped(
                _rollup_query(_REVENUE, "sum", "month"),
                where=[{"field": "dimension.region", "op": "=", "value": "east"}],
            ),
            None,
            id="pre-joined-path-filter",
        ),
        pytest.param(
            ({"ship_to_key": _SHIP_TO_KEY}, {"ship_to": False}),
            _grouped(_rollup_query(_REVENUE, "sum", "month"), _CUSTOMER_KEY),
            "join_path_mismatch",
            id="foreign-key-with-two-relationships",  # the base reads the buyer's key
        ),
        pytest.param(
            ({"buyer_key": _BUYER_KEY}, {"ship_to": None}),
            _grouped(_rollup_query(_REVENUE, "sum", "month"), _CUSTOMER_KEY),
            None,
            id="foreign-key-with-one-relationship",
        ),
        pytest.param(
            ({"product": _PRODUCT}, {"lines": True}),
            _rollup_query(_REVENUE, "sum", "month"),
            "join_path_mismatch",
            id="rollup-pre-joined-one-to-many",  # each order's revenue once per line
        ),
        pytest.param(
            ({"product": _PRODUCT}, {"lines": True}),
            _grouped(
                _rollup_query(_REVENUE, "sum", "month"),
                where=[{"field": "dimension.product", "op": "=", "value": "a"}],
            ),
            "one_to_many_hop",
            id="rollup-pre-joined-one-to-many-filter",  # the base counts each order once
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, {"ship_to": None}),
            _rollup_query("measure.weight", "sum", "month"),
            "join_path_mismatch",
            id="measure-read-from-another-model",
        ),
        # Reason codes that predate the rollup guards.
        pytest.param(
            ({"monthly": {**_MONTHLY, "equivalence": {"kind": "approximate"}}},),
            _rollup_query(_REVENUE, "sum", "quarter"),
            "non_exact_equivalence",
            id="approximate-rollup",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _grouped(_rollup_query(_REVENUE, "sum", "month"), "dimension.customer_id"),
            "missing_dimension",
            id="excluded-dimension",
        ),
        pytest.param(
            ({"monthly": {**_MONTHLY, "eligible_time_grains": ["day", "month"]}},),
            _rollup_query(_REVENUE, "sum", "day"),
            "aggregate_grain_too_coarse",
            id="day-from-a-month-rollup",
        ),
        pytest.param(
            (
                {
                    "monthly": {
                        **_MONTHLY,
                        "excludes": {**_MONTHLY["excludes"], "measures": ["revenue"]},
                    }
                },
            ),
            _rollup_query(_REVENUE, "sum", "quarter"),
            "missing_measure",
            id="excluded-measure",
        ),
        pytest.param(
            ({"no_role": {**_NO_ROLE, "time": _MONTHLY["time"], "columns": _UNDECLARED_COLUMNS}},),
            _rollup_query(_REVENUE, "sum", "quarter"),
            "unsupported_rollup",
            id="column-without-rollup-or-holds",
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _rollup_query(_REVENUE, "sum", "month", start="2026-02-01T00:00:00.0000000"),
            None,
            id="bound-with-zero-digits-past-microseconds",
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
            ({"weekly": _WEEKLY},), _rollup_query(_REVENUE, "sum", "week"), None, id="week"
        ),
        pytest.param(
            ({"hourly": _HOURLY},),
            _rollup_query(_REVENUE, "sum", "day", start="2026-01-01", end="2026-04-01"),
            None,
            id="hourly-day-bounds",
        ),
        pytest.param(
            ({"minutely": _MINUTELY},),
            _rollup_query(_REVENUE, "sum", "day", start="2026-01-01", end="2026-04-01"),
            None,
            id="minutely-day-bounds",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, {"time": {"timezone": "America/New_York"}}),
            _rollup_query(_REVENUE, "sum", "month"),
            None,
            id="role-zone-without-conversion",
        ),
        pytest.param(
            ({"monthly": _MONTHLY}, {"time": {"timezone": "UTC", "column_timezone": "UTC"}}),
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
    variant_id, variant = next(iter(rollups[0].items()))
    relation = variant.get("id", f"aggregate_relation.orders_{variant_id}")

    assert routing["selected"] == ([] if reason else [relation])
    assert _decisions(routing) == {f"leaf_1:{relation}": reason or "selected"}


def test_a_non_default_calendar_refuses_before_routing(tmp_path: Path) -> None:
    query = _rollup_query(_REVENUE, "sum", "quarter", calendar_id="fiscal", fill=True)
    with pytest.raises(SemanticLayerError) as refused:
        _routed_answers(tmp_path, ({"monthly": _MONTHLY}, {"fiscal_calendar": True}), query)

    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    assert refused.value.details["reason"] == "calendar_not_supported_yet"


@pytest.mark.parametrize(
    ("rollups", "query", "decisions"),
    [
        pytest.param(
            ({"monthly": _MONTHLY, "spare": {**_MONTHLY, "selection": {"priority": -1}}},),
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
            {"leaf_1:aggregate_relation.orders_monthly": "eligible:lowered_separately"},
            id="distribution-lowered-separately",  # its branch doesn't read the rollup
        ),
        pytest.param(
            _MONTHLY_ONLY,
            _SUM_AND_DISTRIBUTION,
            {"leaf_1:aggregate_relation.orders_monthly": "unknown:lowered_separately"},
            id="distribution-beside-a-routed-sum",  # the sum's branch reads the rollup
        ),
    ],
)
def test_routing_report_lists_each_rollup_per_leaf(
    tmp_path: Path, rollups: tuple, query: dict, decisions: dict
):
    assert _decisions(_routed_answers(tmp_path, rollups, query)) == decisions


@pytest.mark.parametrize(
    ("columns", "error"),
    [
        pytest.param(
            {"revenue": {"column": "max_amount", "hold": "max"}}, "unknown keys", id="typo"
        ),
        pytest.param({"revenue": {"column": "revenue", "holds": "avg"}}, "holds 'avg'", id="value"),
        pytest.param(
            {"revenue": {"column": "revenue", "holds": "count_distinct"}},
            "holds 'count_distinct'",
            id="not-allowed-for-the-measure",
        ),
    ],
)
def test_rollup_bindings_are_checked(tmp_path: Path, columns: dict, error: str):
    _rollup_package(tmp_path / "p", *_monthly(**columns))
    with pytest.raises(SemanticLayerError, match=error):
        load_package_config(str(tmp_path / "p"))
    unknown_path = {"dimension.region": {"column": "region", "path": ["relationship.nope"]}}
    _rollup_package(
        tmp_path / "q",
        {"region": {**_REGION, "columns": {"revenue": "revenue", **unknown_path}}},
        {"ship_to": False},
    )
    with pytest.raises(SemanticLayerError, match="unknown relationships"):
        load_package_config(str(tmp_path / "q"))


@pytest.mark.parametrize("single_file", [False, True], ids=["directory", "single-file"])
@pytest.mark.parametrize(
    ("location", "fields", "error"),
    [
        ("package", {"aggregate_relations": []}, "declare rollups under the model's `variants:`"),
        ("package", {"aggregate_relations": None}, "declare rollups under the model's `variants:`"),
        ("model", {"default_variant": "tx"}, "default_variant"),
        ("variant", {"time_grain": "month"}, "time_grain"),
        ("variant", {"time_column": "month_start"}, "time_column"),
        ("variant", {"temporal_role": "temporal_role.t"}, "temporal_role"),
        ("variant", {"grain": "month"}, "grain must be a mapping"),
        ("variant", {"grain": {"time": "transaction"}, "time_column": "bad"}, "time_column"),
        ("variant", {"covers": "inherit_all"}, "covers"),
        ("variant", {"selection": {"prefer_for_grains": ["month"]}}, "prefer_for_grains"),
        ("variant", {"equivalence": {"baseline": "tx"}}, "baseline"),
        ("variant", {"filters": _S1_ONLY["filters"]}, "filters"),
    ],
)
def test_noncanonical_rollups_are_refused(tmp_path: Path, single_file, location, fields, error):
    package = tmp_path / "p"
    _rollup_package(package, {"monthly": _MONTHLY})
    raw = _merge_package_dir(str(package))
    target = {
        "package": raw,
        "model": raw["models"]["orders"],
        "variant": raw["models"]["orders"]["variants"]["monthly"],
    }[location]
    target.update(fields)
    if single_file:
        source = tmp_path / "p.yml"
        _write_yaml(source, raw)
    else:
        source = package
        _write_yaml(
            package / "package.yml", {key: value for key, value in raw.items() if key != "models"}
        )
        _write_yaml(package / "models" / "orders.yml", {"model": raw["models"]["orders"]})
    with pytest.raises(SemanticLayerError, match=re.escape(error)) as exc:
        load_package_config(str(source))
    assert exc.value.code == "INVALID_CONFIG"
    if "default_variant" in fields:
        assert "model 'orders'" in str(exc.value)
        assert "['default_variant']" in str(exc.value)


def test_routing_report_caps_its_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(routing, "MAX_CANDIDATES", 1)
    rollups = ({"monthly": _MONTHLY, "spare": {**_MONTHLY, "selection": {"priority": -1}}},)
    report = _routed_answers(tmp_path, rollups, _rollup_query(_REVENUE, "sum", "quarter"))
    assert len(report["candidates"]) == 1 and report["candidates_omitted"] == 1


@pytest.mark.parametrize("fanout_path", [True, False], ids=["declared-fanout", "foreign-key-only"])
def test_foreign_key_rollup_checks_its_declared_path(tmp_path: Path, fanout_path: bool):
    path = [_LINE_ORDER, "relationship.lines_customer"] if fanout_path else []
    binding = {"column": "customer_key", **({"path": path} if path else {})}
    variant = {**_BUYER_KEY, "columns": {"revenue": "revenue", _CUSTOMER_KEY: binding}}
    package = tmp_path / "p"
    _rollup_package(package, {"buyer": variant}, {"lines": True, "ship_to": None})
    source = package / "models" / "order_lines.yml"
    lines = yaml.safe_load(source.read_text())
    lines["model"]["entities"]["customer"] = {}
    _write_yaml(source, lines)
    graph_path = package / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["relationships"]["lines_customer"] = {
        "id": "relationship.lines_customer",
        "entities": ["line", "customer"],
        "cardinality": "many_to_one",
    }
    _write_yaml(graph_path, graph)
    config = load_package_config(str(package))
    query = _rollup_query(_REVENUE, "sum", "month")
    compiled = compile_query(config, Registry(config), query)
    with aggregate_routing(False):
        off = compile_query(config, Registry(config), query)

    with duckdb.connect() as connection:
        connection.execute(_ROLLUP_SEED)
        connection.execute("INSERT INTO customers VALUES ('c9', 'east', 9)")
        connection.execute("""
            CREATE OR REPLACE TABLE order_lines AS
            SELECT order_id * 2 + n AS line_id, order_id, customer_id, 'a' AS product
            FROM order_fact CROSS JOIN range(2) t(n)
        """)
        if fanout_path:
            connection.execute("""
                CREATE OR REPLACE TABLE order_buyer_monthly AS
                SELECT date_trunc('month', o.ordered_at) AS month_start,
                    c.customer_id AS customer_key, SUM(o.amount) AS revenue
                FROM order_fact o JOIN order_lines l ON l.order_id = o.order_id
                JOIN customers c ON c.customer_id = l.customer_id GROUP BY 1, 2
            """)
        reference = connection.execute("""
            SELECT date_trunc('month', ordered_at), SUM(amount)
            FROM order_fact GROUP BY 1 ORDER BY 1
        """).fetchall()
        assert [row[1] for row in reference] == [78, 29, 81, 60]
        assert sorted(connection.execute(compiled["sql"]).fetchall()) == reference
        assert sorted(connection.execute(off["sql"]).fetchall()) == reference

    relation_id = variant["id"]
    leaf = compiled["logical_plan"].measure_plans[0]
    assert leaf.aggregate_relation_rejections == (
        {relation_id: "join_path_mismatch"} if fanout_path else {}
    )
    report = compiled["explain"].performance_plan["aggregate_routing"]
    assert report["selected"] == ([] if fanout_path else [relation_id])


def _routed_answers(tmp_path: Path, rollups: tuple, query: dict) -> dict:
    """Check that the package without its rollups, with them, and with routing off all return
    the same rows; return the routing report with them."""
    connection = duckdb.connect()
    connection.execute(_ROLLUP_SEED)
    compiled = {}
    for name, package in {"base": ({}, *rollups[1:]), "rollup": rollups}.items():
        _rollup_package(tmp_path / name, *package)
        config = load_package_config(str(tmp_path / name))
        compiled[name] = compile_query(config, Registry(config), query)
    with aggregate_routing(False):
        compiled["off"] = compile_query(config, Registry(config), query)
    rows = {
        name: sorted(connection.execute(c["sql"]).fetchall(), key=str)
        for name, c in compiled.items()
    }

    assert rows["rollup"] == rows["off"]
    # A dimension a rollup holds from another model keeps its inner join in this package (see
    # test_lookup_joins), so that routing never changes an answer; the package without the
    # rollup has no such dimension to keep, and keeps the rows the lookup found no match for.
    if not any(
        rollup_dimension_entities(config, row.source_entity) - {row.source_entity}
        for row in config.aggregate_relations
    ):
        assert rows["base"] == rows["off"]
    tables = {row.id: row.relation for row in config.aggregate_relations}

    def read(sql: str) -> set[str]:
        return {table for table in tables.values() if re.search(rf"\b{table}\b", sql)}

    off = compiled["off"]["explain"].performance_plan["aggregate_routing"]
    assert off["selected"] == [] and read(compiled["off"]["sql"]) == set()
    assert {row["reason"] for row in off["candidates"]} == {ROUTING_OFF}
    routing = compiled["rollup"]["explain"].performance_plan["aggregate_routing"]
    decided: dict[str, set[str]] = {"selected": set(), "unknown": set()}
    for row in routing["candidates"]:
        decided.setdefault(row["decision"], set()).add(row["relation_id"])
    # The report agrees with the SQL: `selected` is exactly what it reads, separately compiled
    # branches included; a leaf's row says `selected` only for what that leaf reads, and a
    # rollup some branch reads is `unknown` for a leaf lowered separately.
    selected = set(routing["selected"])
    assert {tables[id_] for id_ in selected} == read(compiled["rollup"]["sql"])
    assert decided["selected"] <= selected
    assert selected - decided["selected"] <= decided["unknown"] <= selected
    return routing


def _decisions(routing: dict) -> dict[str, str]:
    return {
        f"{row['leaf_id']}:{row['relation_id']}": (
            f"{row['decision']}:{row['reason']}"
            if row["reason"] == "lowered_separately"
            else row["reason"] or row["decision"]
        )
        for row in routing["candidates"]
    }


def test_routing_switch_applies_on_a_warm_compile_cache(tmp_path: Path, monkeypatch):
    _rollup_package(tmp_path / "p", {"monthly": _MONTHLY})
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
    with runtime.request_scope():  # a request in flight doesn't block the switch
        runtime.set_aggregate_routing(True)

    monkeypatch.setenv("SEMANTIC_RAILS_AGGREGATE_ROUTING", "OFF")
    assert compile_once(Runtime.from_path(str(tmp_path / "p"))) == ([], False)
    monkeypatch.setenv("SEMANTIC_RAILS_AGGREGATE_ROUTING", "no")
    with pytest.raises(SemanticLayerError, match="must be 'on' or 'off'"):
        Runtime.from_path(str(tmp_path / "p"))
