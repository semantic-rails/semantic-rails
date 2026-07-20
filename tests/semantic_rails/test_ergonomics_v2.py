from __future__ import annotations

import json
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from semantic_rails.api import AppState, Handler
from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from tests.plan_candidate_envelope import plan_candidate_envelope


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_demo_package(package_dir: Path) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": package_dir.name,
                "namespace": "demo",
                "name": "Demo",
                "warehouse": "duckdb",
                "default_db": "data/demo.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed.sql"},
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
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
                "relation": "orders",
                "grain": ["order_id"],
                "keys": {"primary": ["order_id"]},
                "dimensions": {
                    "store": {"label": "Store", "kind": "categorical"},
                },
                "times": {
                    "ordered_at": {
                        "label": "Ordered at",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "default_time": "ordered_at",
                "measures": {
                    "revenue": {
                        "label": "Revenue",
                        "expr": "revenue",
                        "accumulation": "flow",
                        "value_type": "currency",
                    },
                    "email_received": {
                        "label": "Email received",
                        "expr": "email_received",
                        "accumulation": "flow",
                    },
                    "sms_received": {
                        "label": "SMS received",
                        "expr": "sms_received",
                        "accumulation": "flow",
                    },
                    "push_received": {
                        "label": "Push received",
                        "expr": "push_received",
                        "accumulation": "flow",
                    },
                },
            }
        },
    )
    (package_dir / "data").mkdir(parents=True, exist_ok=True)
    (package_dir / "data" / "seed.sql").write_text(
        """
CREATE TABLE orders (
  order_id INTEGER,
  store VARCHAR,
  ordered_at TIMESTAMP,
  revenue DOUBLE,
  email_received INTEGER,
  sms_received INTEGER,
  push_received INTEGER
);

INSERT INTO orders VALUES
  (1, 'A', '2026-04-01 00:00:00', 10.0, 100, 10, 1),
  (2, 'B', '2026-04-02 00:00:00', 20.0, 200, 20, 2),
  (3, 'A', '2026-04-03 00:00:00', 5.0, 50, 5, 0);
""".strip(),
        encoding="utf-8",
    )


def test_schema_normalizes_to_package_config_and_auto_publishes_primitive(tmp_path: Path):
    package_dir = tmp_path / "demo"
    _write_demo_package(package_dir)

    config = load_package_config(str(package_dir))

    assert config.version == 1
    assert "entity.demo_order" in {row.id for row in config.entities}
    assert "dimension.demo_order_store" in {row.id for row in config.dimensions}
    assert "temporal_role.demo_order_ordered_at" in {row.id for row in config.temporal_roles}
    assert "measure.demo.revenue" in {row.id for row in config.measures}
    assert "metric.demo.revenue" in {row.id for row in config.metric_recipes}

    runtime = Runtime.from_path(str(package_dir))
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [{"expression": {"metric": "metric.demo.revenue"}, "as": "revenue"}],
                "group_by": ["dimension.demo_order_store"],
                "order_by": [{"field": "dimension.demo_order_store", "direction": "ASC"}],
            }
        )
    finally:
        runtime.close()

    assert result["rows"] == [
        {"dimension.demo_order_store": "A", "revenue": 15.0},
        {"dimension.demo_order_store": "B", "revenue": 20.0},
    ]


def test_compile_response_contains_sql_profile_and_output_column_lineage(tmp_path: Path):
    package_dir = tmp_path / "demo"
    _write_demo_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    try:
        # `verbosity="full"` is required to preserve `lineage` on
        # output_columns — the new compact default strips it.
        result = runtime.compile(
            {
                "version": 1,
                "select": [{"expression": {"metric": "metric.demo.revenue"}, "as": "revenue"}],
                "group_by": ["dimension.demo_order_store"],
                "sql_profile": "compact",
                "verbosity": "full",
            }
        )
    finally:
        runtime.close()

    assert result["status"] == "ok"
    assert result["sql_profile"] == "compact"
    assert result["warehouse"] == "duckdb"
    assert result["dialect"] == "duckdb"
    assert result["rendered_sql"]
    assert result["output_columns"] == [
        {
            "field": "dimension.demo_order_store",
            "semantic_id": "dimension.demo_order_store",
            "display_label": "Store",
            "sql_alias": "dimension.demo_order_store",
            "type": "string",
            "lineage": ["dimension.demo_order_store"],
        },
        {
            "field": "revenue",
            "semantic_id": "metric.demo.revenue",
            "display_label": "Revenue",
            "sql_alias": "revenue",
            "type": "currency",
            "lineage": ["metric.demo.revenue"],
        },
    ]


def test_compile_response_summarizes_qualified_metric_rollup(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # `verbosity="full"` keeps `semantic_summary`; the new compact
        # default drops it.
        result = runtime.compile(
            {
                "version": 2,
                "select": [
                    {
                        "as": "revenue_usd_from_qualified_customers",
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                            "predicates": [
                                {
                                    "measure": "measure.jaffle.order_count",
                                    "entity": "entity.jaffle_customer",
                                    "op": ">=",
                                    "value": 10,
                                }
                            ],
                        },
                    }
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
                "verbosity": "full",
            }
        )
    finally:
        runtime.close()

    summary = result["semantic_summary"]
    assert summary["rollup_dimensions"] == ["dimension.jaffle_store_name"]
    assert summary["qualified_metrics"][0]["target_measure"] == "measure.jaffle.revenue_usd"
    assert (
        summary["qualified_metrics"][0]["predicates"][0]["predicate_metric"]
        == "measure.jaffle.order_count"
    )
    assert summary["qualified_metrics"][0]["predicates"][0]["predicate_grain"] == "month"
    assert summary["sql_complexity"]["cte_count"] >= 3


def test_sql_render_profiles_change_rendered_shape_without_changing_metadata(tmp_path: Path):
    package_dir = tmp_path / "demo"
    _write_demo_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    query = {
        "version": 1,
        "select": [{"expression": {"metric": "metric.demo.revenue"}, "as": "revenue"}],
        "group_by": ["dimension.demo_order_store"],
    }
    try:
        audit = runtime.compile({**query, "sql_profile": "audit"})
        compact = runtime.compile({**query, "sql_profile": "compact"})
        debug = runtime.compile({**query, "sql_profile": "debug"})
    finally:
        runtime.close()

    assert audit["rendered_sql"].count("\n") > compact["rendered_sql"].count("\n")
    assert "\n" not in compact["rendered_sql"]
    assert compact["output_columns"] == audit["output_columns"]
    assert debug["sql_profile"] == "debug"
    assert debug["output_columns"] == audit["output_columns"]


def test_http_compile_route_is_compile_only(runtime_factory, package_config_factory, monkeypatch):
    runtime = runtime_factory("jaffle_shop")
    _, alt_config_path = package_config_factory("jaffle_shop")
    monkeypatch.setattr(
        "semantic_rails.config.list_package_paths", lambda: {"jaffle_shop": str(alt_config_path)}
    )
    state = AppState("jaffle_shop")
    state.runtime.close()
    state.runtime = runtime

    class _Handler(Handler):
        pass

    _Handler.state = state
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(
            {
                "query": {
                    "version": 1,
                    "select": [
                        {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                    ],
                }
            }
        ).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/compile",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        state.runtime.close()

    assert response["ok"] is True
    assert response["status"] == "ok"
    assert response["rendered_sql"]
    assert "rows" not in response
    assert response["output_columns"][0]["field"] == "orders"


def test_jaffle_shop_advanced_examples_execute_hard_questions(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        ratio = runtime.query(
            {
                "version": 2,
                "select": [
                    {
                        "as": "pct_revenue_from_session_customers",
                        "expression": {
                            "kind": "ratio",
                            "numerator": {
                                "kind": "scoped_aggregate",
                                "measure": "measure.jaffle.revenue_usd",
                                "aggregation": "sum",
                                "predicates": [
                                    {
                                        "measure": "measure.jaffle.session_starts",
                                        "entity": "entity.jaffle_customer",
                                        "op": ">",
                                        "value": 0,
                                    }
                                ],
                            },
                            "denominator": {
                                "kind": "scoped_aggregate",
                                "measure": "measure.jaffle.revenue_usd",
                                "aggregation": "sum",
                            },
                        },
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "start": "2016-09-01",
                    "end": "2016-10-01",
                },
            }
        )
        p80 = runtime.query(
            {
                "version": 2,
                "select": [
                    {
                        "as": "p80_customer_revenue",
                        "expression": {
                            "kind": "distribution",
                            "function": "percentile",
                            "p": 0.8,
                            "over": {
                                "kind": "entity_value",
                                "entity": "entity.jaffle_customer",
                                "input": {
                                    "measure": "measure.jaffle.revenue_usd",
                                    "aggregation": "sum",
                                },
                                "where": [{"kind": "value_filter", "op": ">", "value": 0}],
                            },
                        },
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "start": "2016-09-01",
                    "end": "2017-01-01",
                },
            }
        )
    finally:
        runtime.close()

    assert ratio["rows"][0]["pct_revenue_from_session_customers"] > 0
    assert "PredicateSet" in {row["kind"] for row in ratio["explain"]["semantic_dag"]["nodes"]}
    assert p80["row_count"] == 4
    assert "DistributionAggregate" in {
        row["kind"] for row in p80["explain"]["semantic_dag"]["nodes"]
    }


def test_plan_v2_composes_runtime_arithmetic_from_package_metadata(tmp_path: Path):
    package_dir = tmp_path / "demo"
    _write_demo_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    try:
        result = plan_candidate_envelope(
            runtime, intent="sum email sms and push messages by region", limit=1
        )
    finally:
        runtime.close()

    query = result["candidates"][0]["candidate_ir"]
    assert result["candidate_envelope_version"] == 1
    assert result["interpreted_intent"]["pattern"] == "runtime_metric_arithmetic"
    assert query["select"][0]["expression"]["kind"] == "arithmetic"
    assert "metric.demo.email_received" in json.dumps(query)
    assert "metric.demo.sms_received" in json.dumps(query)
    assert "metric.demo.push_received" in json.dumps(query)
    assert result["candidates"][0]["validation"]["ok"] is True
