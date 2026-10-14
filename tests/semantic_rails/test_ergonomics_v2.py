from __future__ import annotations

import json
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
import yaml

from semantic_rails.api import AppState, Handler
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from tests.plan_candidate_envelope import plan_candidate_envelope
from tests.semantic_rails.conftest import copy_package_config, opened


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
                "entities": {"order": {}},
                "relation": "orders",
                "dimensions": {
                    "store": {"label": "Store", "kind": "categorical"},
                },
                "times": {
                    "ordered_at": {
                        "label": "Ordered at",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    }
                },
                "measures": {
                    "revenue": {
                        "kind": "aggregate",
                        "label": "Revenue",
                        "expr": "revenue",
                        "accumulation": "flow",
                        "value_type": "currency",
                    },
                    "email_received": {
                        "kind": "aggregate",
                        "label": "Email received",
                        "expr": "email_received",
                        "accumulation": "flow",
                    },
                    "sms_received": {
                        "kind": "aggregate",
                        "label": "SMS received",
                        "expr": "sms_received",
                        "accumulation": "flow",
                    },
                    "push_received": {
                        "kind": "aggregate",
                        "label": "Push received",
                        "expr": "push_received",
                        "accumulation": "flow",
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                key: {
                    "value_type": ("currency" if key == "revenue" else "number"),
                    "kind": "aggregate",
                    "measure": key,
                }
                for key in ("revenue", "email_received", "sms_received", "push_received")
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


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """A package written from scratch: one entity, a store dimension and four measures."""
    package_dir = tmp_path_factory.mktemp("package") / "demo"
    _write_demo_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    yield opened(runtime)
    runtime.close()


@pytest.fixture(scope="module")
def jaffle(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(path))
    yield opened(runtime)
    runtime.close()


BY_STORE = {"select": ["metric.demo.revenue"], "group_by": ["dimension.demo_order_store"]}


def test_schema_normalizes_to_package_config_and_answers_its_metric(demo):
    config = demo.package_config
    assert config.version == 1
    assert "entity.demo_order" in {row.id for row in config.entities}
    assert "dimension.demo_order_store" in {row.id for row in config.dimensions}
    assert "temporal_role.demo_order_ordered_at" in {row.id for row in config.temporal_roles}
    assert "measure.demo.revenue" in {row.id for row in config.measures}
    assert "metric.demo.revenue" in {row.id for row in config.metric_recipes}
    order = [{"field": "dimension.demo_order_store", "direction": "asc"}]
    assert demo.query({**BY_STORE, "order_by": order})["rows"] == [
        {"dimension.demo_order_store": "A", "revenue": 15.0},
        {"dimension.demo_order_store": "B", "revenue": 20.0},
    ]


def test_compile_reports_its_sql_profile_and_output_column_lineage(demo):
    # `verbosity="full"` keeps `lineage` on output_columns.
    audit, compact, debug = (
        demo.compile(BY_STORE, verbosity="full", sql_profile=profile)
        for profile in ("audit", "compact", "debug")
    )
    assert compact["status"] == "ok"
    assert (compact["sql_profile"], debug["sql_profile"]) == ("compact", "debug")
    assert (compact["warehouse"], compact["dialect"]) == ("duckdb", "duckdb")
    assert compact["output_columns"] == [
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
    # A render profile changes the SQL's shape, never the metadata.
    assert compact["rendered_sql"] and "\n" not in compact["rendered_sql"]
    assert audit["rendered_sql"].count("\n") > 0
    assert audit["output_columns"] == compact["output_columns"] == debug["output_columns"]


def test_compile_response_summarizes_qualified_metric_rollup(jaffle):
    # `verbosity="full"` keeps `semantic_summary`; the compact default drops it.
    result = jaffle.compile(
        {
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
                                "op": "gte",
                                "value": 10,
                                "over": "period",
                            }
                        ],
                    },
                }
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
        verbosity="full",
    )

    summary = result["semantic_summary"]
    assert summary["rollup_dimensions"] == ["dimension.jaffle_store_name"]
    assert summary["qualified_metrics"][0]["target_measure"] == "measure.jaffle.revenue_usd"
    assert (
        summary["qualified_metrics"][0]["predicates"][0]["predicate_metric"]
        == "measure.jaffle.order_count"
    )
    assert summary["qualified_metrics"][0]["predicates"][0]["predicate_grain"] == "month"
    assert summary["sql_complexity"]["cte_count"] >= 3


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
                    "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
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


def test_jaffle_shop_advanced_examples_execute_hard_questions(jaffle):
    ratio = jaffle.query(
        {
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
                                    "op": "gt",
                                    "value": 0,
                                    "over": "period",
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
    p80 = jaffle.query(
        {
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
                                "kind": "measure",
                                "measure": "measure.jaffle.revenue_usd",
                                "aggregation": "sum",
                            },
                            "where": [{"kind": "value_filter", "op": "gt", "value": 0}],
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

    assert ratio["rows"][0]["pct_revenue_from_session_customers"] > 0
    assert "PredicateSet" in {row["kind"] for row in ratio["explain"]["semantic_dag"]["nodes"]}
    assert p80["row_count"] == 4
    assert "DistributionAggregate" in {
        row["kind"] for row in p80["explain"]["semantic_dag"]["nodes"]
    }


def test_plan_v2_composes_runtime_arithmetic_from_package_metadata(demo):
    result = plan_candidate_envelope(
        demo, intent="sum email sms and push received by store", limit=1
    )
    # The package has no region, and the draft drops that grouping: not ready.
    held = plan_payload(demo, intent="sum email sms and push messages by region")

    assert held["status"] == "low_confidence"
    assert "ready_for" not in held["next"]
    assert held["why"]["details"]["terms"] == ["messages", "region"]
    assert not held["best"]["query"].get("group_by")
    query = result["candidates"][0]["candidate_ir"]
    assert result["candidate_envelope_version"] == 1
    assert result["interpreted_intent"]["pattern"] == "runtime_metric_arithmetic"
    assert query["select"][0]["expression"]["kind"] == "arithmetic"
    assert "metric.demo.email_received" in json.dumps(query)
    assert "metric.demo.sms_received" in json.dumps(query)
    assert "metric.demo.push_received" in json.dumps(query)
    assert result["candidates"][0]["validation"]["ok"] is True
