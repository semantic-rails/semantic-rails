"""Synthetic-package unit tests for v1 authoring normalizations.

Each test builds a tiny synthetic package (3-5 lines per concern) and
verifies the loader produces the expected runtime shape. The mini-packages
are kept deliberately small so a regression points at one normalization,
not a tangle of features.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import (
    validate_metric_expression,
    validate_runtime_package,
)
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.conftest import copy_package_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_synthetic_package(
    package_dir: Path,
    *,
    package_extra: dict[str, Any] | None = None,
    graph_entities: dict[str, Any] | None = None,
    graph_relationships: dict[str, Any] | None = None,
    models: dict[str, dict[str, Any]] | None = None,
    metrics: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Build a minimal synthetic v1 package on disk.

    Defaults: a single `widget` entity and `widgets` model with a
    timestamp/event-time `created_at` column and one count measure.
    """
    package_dir = Path(package_dir)
    package_payload = {
        "schema_version": 1,
        "package": {
            "id": package_dir.name,
            "namespace": "synth",
            "name": package_dir.name,
            "description": "synthetic test package",
            "warehouse": "duckdb",
            "default_db": "data/example.duckdb",
            "seed": {"kind": "sql_script", "source": "data/seed_example.sql"},
            **dict(package_extra or {}),
        },
        "defaults": {
            "time": {
                "timezone": "UTC",
                "default_query_axis": False,
                "supported_grains": ["day", "week", "month"],
            },
        },
    }
    _write_yaml(package_dir / "package.yml", package_payload)

    if graph_entities is None:
        graph_entities = {
            "widget": {
                "label": "Widget",
                "key": ["widget_id"],
            },
        }
    graph_payload = {"graph": {"entities": graph_entities}}
    if graph_relationships:
        graph_payload["graph"]["relationships"] = graph_relationships
    _write_yaml(package_dir / "graph.yml", graph_payload)

    if models is None:
        models = {
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count of widgets.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["widgets"],
                    },
                },
            },
        }
    for model_id, model in models.items():
        _write_yaml(package_dir / "models" / f"{model_id}.yml", {"models": {model_id: model}})

    if metrics:
        _write_yaml(package_dir / "metrics.yml", {"metrics": metrics})

    return package_dir


# ---------------------------------------------------------------------------
# Section 1: ID auto-derivation and `as:` escape hatch
# ---------------------------------------------------------------------------


def test_entity_id_auto_derives_from_namespace_and_key(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(tmp_path / "pkg_id_derive")
    config = load_package_config(str(pkg))
    widget = next(e for e in config.entities if e.id.endswith("widget"))
    assert widget.id == "entity.synth_widget"
    assert widget.name == "synth.Widget"


def test_as_escape_hatch_overrides_auto_derived_id(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_as_hatch"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "widget": {
                "as": "entity.synth.legacy_widget_id",
                "label": "Widget",
                "key": ["widget_id"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    widget = next(e for e in config.entities if "widget" in e.id)
    assert widget.id == "entity.synth.legacy_widget_id"


def test_as_with_wrong_namespace_overrides_auto_derived(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_as_wrong_ns"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "widget": {
                "as": "entity.othernamespace.widget",
                "label": "Widget",
                "key": ["widget_id"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    widget = next(
        e for e in config.entities if "widget" in e.id or e.id == "entity.othernamespace.widget"
    )
    # `as:` is the escape hatch for preserving public IDs whose prefix
    # differs from the package's namespace (e.g., a measure named
    # `sales.revenue_usd` published under `metric.sales.revenue_usd` in a
    # package whose namespace is `jaffle`). Cross-namespace `as:` overrides
    # apply within a single package's YAML — the package boundary is
    # enforced by the loader (it doesn't load metrics from other packages).
    assert widget.id == "entity.othernamespace.widget"


# ---------------------------------------------------------------------------
# Section 2: model.entities: block authoring sugar
# ---------------------------------------------------------------------------


def test_model_entities_block_translates_to_legacy_shape(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_entities_block"
    # Two-entity model: orders (primary) + customer (FK).
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "grain": ["customer_id"],
                "keys": {"primary": ["customer_id"]},
                "measures": {
                    "customer_count": {
                        "label": "Customer count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["customers"],
                    },
                },
            },
            "orders": {
                "id": "orders",
                "relation": "orders",
                "grain": ["order_id"],
                "entities": {
                    "order": {},
                    "customer": {},
                },
                "measures": {
                    "order_count": {
                        "label": "Order count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["orders"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    # Inferred relationship: orders → customer
    rel_pairs = {(r.source_entity, r.target_entity) for r in config.relationships}
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    assert (order_id, customer_id) in rel_pairs


def test_model_entities_block_with_expr_renames_column(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_entities_expr"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "grain": ["customer_id"],
                "keys": {"primary": ["customer_id"]},
                "measures": {
                    "customer_count": {
                        "label": "Customer count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["c"],
                    },
                },
            },
            "orders": {
                "id": "orders",
                "relation": "orders",
                "grain": ["order_id"],
                "entities": {
                    "order": {},
                    "customer": {"expr": "cust_id"},
                },
                "measures": {
                    "order_count": {
                        "label": "Order count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["o"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    # The orders model's customer FK should bind to `cust_id`, not `customer_id`.
    orders_entity = next(e for e in config.entities if e.id.endswith("_order"))
    assert orders_entity.foreign_keys.get("customer") == ["cust_id"]


def test_model_entities_block_bridge_false_disables_inferred_relationships(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_bridge_false"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "user": {"label": "User", "key": ["user_id"]},
            "account": {"label": "Account", "key": ["account_id"]},
            "user_account_link": {"label": "User account link", "key": ["link_id"]},
        },
        models={
            "users": {
                "id": "users",
                "entity": "user",
                "relation": "user",
                "grain": ["user_id"],
                "keys": {"primary": ["user_id"]},
                "measures": {
                    "user_count": {
                        "label": "User count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["user_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["u"],
                    },
                },
            },
            "accounts": {
                "id": "accounts",
                "entity": "account",
                "relation": "account",
                "grain": ["account_id"],
                "keys": {"primary": ["account_id"]},
                "measures": {
                    "account_count": {
                        "label": "Account count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["account_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["a"],
                    },
                },
            },
            "links": {
                "id": "links",
                "relation": "user_account_link",
                "grain": ["link_id"],
                "entities": {
                    "bridge": False,
                    "user_account_link": {},
                    "user": {},
                    "account": {},
                },
                "measures": {
                    "link_count": {
                        "label": "Link count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["link_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["l"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    # No inferred relationships from links → user / account.
    rel_pairs = {(r.source_entity, r.target_entity) for r in config.relationships}
    link_entity_id = next(e.id for e in config.entities if "user_account_link" in e.id)
    target_entities = [t for s, t in rel_pairs if s == link_entity_id]
    assert not target_entities, (
        f"bridge: false should suppress inferred rels, got {target_entities}"
    )


# ---------------------------------------------------------------------------
# Section 3: graph.relationships: override block
# ---------------------------------------------------------------------------


def test_graph_relationships_block_translates_bidirectional_pair(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_graph_rel"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "rollup_safe": {
                    "forward": ["sum", "count"],
                    "reverse": [],
                },
                "safety": "safe",
            },
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "grain": ["customer_id"],
                "keys": {"primary": ["customer_id"]},
                "measures": {
                    "customer_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["c"],
                    },
                },
            },
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "grain": ["order_id"],
                "keys": {"primary": ["order_id"], "foreign": {"customer": ["customer_id"]}},
                "measures": {
                    "order_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["o"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    assert rel.cardinality == "N:1"
    assert rel.rollup_safe_aggregations == ["sum", "count"]


# ---------------------------------------------------------------------------
# Section 4: times: block — `default: true` flag
# ---------------------------------------------------------------------------


def test_times_default_flag_replaces_default_time(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_times_default"
    _write_synthetic_package(
        pkg_dir,
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    },
                },
                # No `default_time:` field — `default: true` on the times entry replaces it.
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["w"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    measure = next(m for m in config.measures if "widget_count" in m.id)
    assert measure.compatible_temporal_roles, (
        "default: true should populate compatible temporal roles"
    )


# ---------------------------------------------------------------------------
# Section 5: Un-nested measure expression: wrapper
# ---------------------------------------------------------------------------


def test_measure_unnested_expr_and_default_agg(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_unnest_measure"
    _write_synthetic_package(
        pkg_dir,
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "revenue_usd": {
                        "label": "Revenue (USD)",
                        "description": "Revenue.",
                        "kind": "aggregate",
                        # Direct fields, not a nested `expression:` wrapper.
                        "expr": "amount_usd",
                        "default_agg": "sum",
                        "accumulation": {"kind": "flow"},
                        "value_type": "currency",
                        "topics": ["r"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    revenue = next(m for m in config.measures if "revenue_usd" in m.id)
    assert revenue.default_aggregation == "sum"


# ---------------------------------------------------------------------------
# Section 6: Direct named fields per metric kind
# ---------------------------------------------------------------------------


def test_metric_kind_aggregate_direct_measure_field(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_metric_aggregate"
    _write_synthetic_package(
        pkg_dir,
        metrics={
            "widget_count_metric": {
                "label": "Widgets",
                "description": "Widget count metric.",
                "kind": "aggregate",
                "measure": "widget_count",
                "value_type": "count",
                "topics": ["w"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "widget_count_metric" in m.id)
    assert metric.kind == "aggregate"


def test_metric_kind_ratio_direct_fields(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_metric_ratio"
    _write_synthetic_package(
        pkg_dir,
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["w"],
                    },
                    "premium_count": {
                        "label": "Premium count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["p"],
                    },
                },
            },
        },
        metrics={
            "premium_share": {
                "label": "Premium share",
                "description": "Share of widgets that are premium.",
                "kind": "ratio",
                "numerator": "premium_count",
                "denominator": "widget_count",
                "null_behavior": "null_if_zero",
                "value_type": "percent",
                "topics": ["p"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_share" in m.id)
    assert metric.kind == "ratio"
    # AST translation: ratio → arithmetic divide
    expr = metric.expression
    # The compiled AST is a SemanticExpr; getattr won't fail because the
    # tree shape matters for SQL — confirm it parsed.
    assert expr is not None


# ---------------------------------------------------------------------------
# Section 7: Strict-mode validators reject legacy authoring forms
# ---------------------------------------------------------------------------


def test_strict_mode_rejects_authored_id_on_measure(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_strict_id"
    _write_synthetic_package(
        pkg_dir,
        package_extra={"schema_strict": True},
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "widget_count": {
                        "id": "measure.synth.widget_count",  # authored id — strict-rejected
                        "label": "Widget count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["w"],
                    },
                },
            },
        },
    )
    errors = validate_runtime_package(pkg_dir)
    assert any("'id:'" in e and "widget_count" in e for e in errors), (
        f"expected strict rejection of authored id on measure, got {errors}"
    )


def test_strict_mode_rejects_freeform_accumulation(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_strict_acc"
    _write_synthetic_package(
        pkg_dir,
        package_extra={"schema_strict": True},
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        # Freeform value not in {flow, stock, event, population}.
                        "accumulation": "rolling_population",
                        "value_type": "count",
                        "topics": ["w"],
                    },
                },
            },
        },
    )
    errors = validate_runtime_package(pkg_dir)
    assert any("accumulation" in e for e in errors), (
        f"expected strict rejection of freeform accumulation, got {errors}"
    )


def test_strict_mode_rejects_legacy_joins_block(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_strict_joins"
    _write_synthetic_package(
        pkg_dir,
        package_extra={"schema_strict": True},
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "joins": {  # strict-rejected
                    "other": {"to": "other", "cardinality": "N:1"},
                },
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["w"],
                    },
                },
            },
        },
    )
    errors = validate_runtime_package(pkg_dir)
    assert any("joins" in e for e in errors), (
        f"expected strict rejection of legacy joins block, got {errors}"
    )


# ---------------------------------------------------------------------------
# Section 8: schema_strict flag round-trip
# ---------------------------------------------------------------------------


def test_schema_strict_flag_threads_through(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_strict_flag"
    _write_synthetic_package(pkg_dir, package_extra={"schema_strict": True})
    config = load_package_config(str(pkg_dir))
    assert config.package.schema_strict is True


def test_schema_strict_default_is_false(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_strict_default"
    _write_synthetic_package(pkg_dir)
    config = load_package_config(str(pkg_dir))
    assert config.package.schema_strict is False


# ---------------------------------------------------------------------------
# Section 9: graph.relationships allowed_directions propagation (Bug 1 regression)
# ---------------------------------------------------------------------------


def test_graph_relationships_allowed_directions_propagates(tmp_path: Path) -> None:
    """Regression: `allowed_directions: [forward]` authored on a
    graph.relationships entry must reach RelationshipConfig.allowed_directions.
    Previously the translator passed the field through verbatim, but the
    join-spec parser reads `traversal:`, silently restoring the default
    ['forward', 'reverse'] and reopening reverse paths the author meant to
    block.
    """
    pkg_dir = tmp_path / "pkg_allowed_directions"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "allowed_directions": ["forward"],
                "safety": "safe",
            },
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "grain": ["customer_id"],
                "keys": {"primary": ["customer_id"]},
                "measures": {
                    "customer_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["c"],
                    },
                },
            },
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "grain": ["order_id"],
                "keys": {"primary": ["order_id"], "foreign": {"customer": ["customer_id"]}},
                "measures": {
                    "order_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["o"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    assert rel.allowed_directions == ["forward"], (
        f"expected ['forward'] from authored allowed_directions, got {rel.allowed_directions!r}"
    )


def test_graph_relationships_allowed_directions_defaults_when_omitted(tmp_path: Path) -> None:
    """When `allowed_directions:` is not authored, the default
    ['forward', 'reverse'] is preserved (verifies the rename is opt-in only)."""
    pkg_dir = tmp_path / "pkg_allowed_directions_default"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "safety": "safe",
            },
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "grain": ["customer_id"],
                "keys": {"primary": ["customer_id"]},
                "measures": {
                    "customer_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["c"],
                    },
                },
            },
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "grain": ["order_id"],
                "keys": {"primary": ["order_id"], "foreign": {"customer": ["customer_id"]}},
                "measures": {
                    "order_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["o"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    assert rel.allowed_directions == ["forward", "reverse"]


def test_graph_relationships_path_preference_propagates(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_path_preference"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "path_preference": 12,
                "safety": "safe",
            },
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "grain": ["customer_id"],
                "keys": {"primary": ["customer_id"]},
                "measures": {
                    "customer_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["c"],
                    },
                },
            },
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "grain": ["order_id"],
                "keys": {"primary": ["order_id"], "foreign": {"customer": ["customer_id"]}},
                "measures": {
                    "order_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["o"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    assert rel.path_preference == 12


# ---------------------------------------------------------------------------
# Section 10: Package-relative refs resolve top-level metrics, not just
# measures (Bug 2 regression)
# ---------------------------------------------------------------------------


def _widget_model_with_two_count_measures() -> dict[str, Any]:
    """Helper: a model with two entity_count measures so we can compose
    ratio/derived metrics over them."""
    return {
        "widgets": {
            "id": "widgets",
            "entity": "widget",
            "relation": "widget",
            "grain": ["widget_id"],
            "keys": {"primary": ["widget_id"]},
            "times": {
                "created_at": {
                    "label": "Created at",
                    "column": "created_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "default_query_axis": True,
                },
            },
            "default_time": "created_at",
            "measures": {
                "widget_count": {
                    "label": "Widget count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["widget_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "topics": ["w"],
                },
                "premium_count": {
                    "label": "Premium count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["widget_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "topics": ["p"],
                },
            },
        },
    }


def test_metric_ratio_numerator_resolves_top_level_metric(tmp_path: Path) -> None:
    """A ratio whose numerator is a top-level metric (not a measure) and
    whose denominator is a measure must resolve both correctly to
    fully-qualified ids — no bare strings left to fail downstream.
    """
    pkg_dir = tmp_path / "pkg_ratio_metric_numerator"
    _write_synthetic_package(
        pkg_dir,
        models=_widget_model_with_two_count_measures(),
        metrics={
            # A top-level metric over premium_count.
            "premium_metric": {
                "label": "Premium metric",
                "description": "Premium count metric.",
                "kind": "aggregate",
                "measure": "premium_count",
                "value_type": "count",
                "topics": ["p"],
            },
            # Ratio whose numerator references the top-level metric above
            # (not the measure!) and whose denominator is a measure.
            "premium_share_v2": {
                "label": "Premium share v2",
                "description": "Top-level metric over measure.",
                "kind": "ratio",
                "numerator": "premium_metric",  # → top-level metric
                "denominator": "widget_count",  # → measure
                "null_behavior": "null_if_zero",
                "value_type": "percent",
                "topics": ["p"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_share_v2" in m.id)
    expr = metric.expression
    assert expr is not None
    left = getattr(expr, "left", None)
    right = getattr(expr, "right", None)
    assert left is not None and right is not None, (
        f"expected arithmetic with left/right, got {expr!r}"
    )

    # Numerator points at a top-level metric (premium_metric exists in
    # this package) — should be a MetricRecipeRefExpr with the resolved
    # metric id.
    left_metric = getattr(left, "metric_recipe", None)
    assert left_metric == "metric.synth.premium_metric", (
        f"numerator should resolve to top-level metric, got {left!r}"
    )

    # Denominator is a measure-only key (no top-level metric exists for
    # widget_count). With auto-publish gone, the loader expands measure-
    # only ratio operands to a `kind: aggregate` AST node referencing the
    # measure directly — no synthetic metric id is produced.
    right_measure = getattr(right, "measure", None)
    assert right_measure == "measure.synth.widget_count", (
        f"denominator should resolve to a measure-aggregate node, got {right!r}"
    )


def test_metric_derived_ast_resolves_top_level_metric_ref(tmp_path: Path) -> None:
    """A derived metric whose authored AST references another top-level
    metric by local key must load cleanly with the metric ref resolved.
    """
    pkg_dir = tmp_path / "pkg_derived_metric_ref"
    _write_synthetic_package(
        pkg_dir,
        models=_widget_model_with_two_count_measures(),
        metrics={
            "premium_metric": {
                "label": "Premium metric",
                "description": "Premium count metric.",
                "kind": "aggregate",
                "measure": "premium_count",
                "value_type": "count",
                "topics": ["p"],
            },
            "premium_metric_double": {
                "label": "Premium metric (doubled)",
                "description": "Derived metric referencing another metric.",
                "kind": "derived",
                "expression": {
                    "kind": "binary",
                    "op": "multiply",
                    "left": {"kind": "metric", "metric": "premium_metric"},
                    "right": {"kind": "metric", "metric": "premium_metric"},
                },
                "value_type": "count",
                "topics": ["p"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_metric_double" in m.id)
    expr = metric.expression
    left = getattr(expr, "left", None)
    right = getattr(expr, "right", None)
    assert left is not None and right is not None
    left_metric = getattr(left, "metric_recipe", None) or getattr(left, "metric", None)
    right_metric = getattr(right, "metric_recipe", None) or getattr(right, "metric", None)
    assert left_metric == "metric.synth.premium_metric"
    assert right_metric == "metric.synth.premium_metric"


def test_metric_ref_ambiguous_when_metric_and_measure_share_key(tmp_path: Path) -> None:
    """If the same package-relative key resolves to BOTH a top-level
    metric and a measure, the loader must raise a clear ambiguity error
    rather than silently picking one.
    """
    pkg_dir = tmp_path / "pkg_ambiguous_metric_ref"
    _write_synthetic_package(
        pkg_dir,
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "shared_key": {
                        "label": "Measure shared_key",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["s"],
                    },
                    "denom": {
                        "label": "Denom",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["d"],
                    },
                },
            },
        },
        metrics={
            # Top-level metric whose local key collides with the measure
            # local key `shared_key`. Authoring this way is allowed (auto-id
            # paths produce metric.synth.shared_key for both), but any
            # package-relative ref to `shared_key` is ambiguous and must
            # error.
            "shared_key": {
                "label": "Top-level metric shared_key",
                "description": "Top-level metric.",
                "kind": "aggregate",
                "measure": "denom",
                "value_type": "count",
                "topics": ["s"],
                # Override id so the metric and the measure auto-published
                # metric do not collide on `metric.synth.shared_key`.
                "as": "metric.synth.shared_key_top",
            },
            "ambiguous_consumer": {
                "label": "Ambiguous consumer",
                "description": "Refers to shared_key — should error.",
                "kind": "ratio",
                "numerator": "shared_key",
                "denominator": "denom",
                "null_behavior": "null_if_zero",
                "value_type": "percent",
                "topics": ["a"],
            },
        },
    )
    with pytest.raises(SemanticLayerError) as excinfo:
        load_package_config(str(pkg_dir))
    msg = str(excinfo.value)
    assert "ambiguous metric reference" in msg
    assert "shared_key" in msg
    # The error must point at both candidates.
    assert "measure" in msg
    assert "metric" in msg


# ---------------------------------------------------------------------------
# Section 11: WT-C additions — additional coverage of v1 authoring shape
# ---------------------------------------------------------------------------


def _two_entity_model_skeleton() -> dict[str, Any]:
    """Helper: orders + customers with the orders model declaring both via
    the entities: block (no joins/keys.foreign authoring)."""
    return {
        "customers": {
            "id": "customers",
            "entity": "customer",
            "relation": "customer",
            "grain": ["customer_id"],
            "keys": {"primary": ["customer_id"]},
            "measures": {
                "customer_count": {
                    "label": "Customer count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["customer_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "topics": ["c"],
                },
            },
        },
        "orders": {
            "id": "orders",
            "relation": "orders",
            "grain": ["order_id"],
            "entities": {
                "order": {},
                "customer": {},
            },
            "measures": {
                "order_count": {
                    "label": "Order count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["order_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "topics": ["o"],
                },
            },
        },
    }


def test_graph_relationships_rollup_safe_both_directions(tmp_path: Path) -> None:
    """`graph.relationships:` with bidirectional `entities: [a, b]` plus
    nested `rollup_safe: { forward, reverse }` populates both directions.

    Closes the gap where the existing test only checks the forward list and
    leaves reverse empty — the loader needs to handle a non-empty reverse
    list and surface it on the resulting RelationshipConfig.
    """
    pkg_dir = tmp_path / "pkg_rollup_both_dirs"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
            "customer": {"label": "Customer", "key": ["customer_id"]},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "rollup_safe": {
                    "forward": ["sum", "count"],
                    "reverse": ["max", "min"],
                },
                "safety": "safe",
            },
        },
        models=_two_entity_model_skeleton(),
    )
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    # Forward direction (order→customer) carries the authored forward list.
    assert rel.rollup_safe_aggregations == ["sum", "count"]
    # Reverse direction (customer→order) is captured on the same edge via
    # the parallel rollup_safe_aggregations_reverse field; the loader does
    # NOT synthesize a second RelationshipConfig.
    assert rel.rollup_safe_aggregations_reverse == ["max", "min"]


def test_times_block_consolidates_temporal_role_and_dimension(tmp_path: Path) -> None:
    """The `times:` block key IS the temporal role; no separate
    `temporal_role.<id>` registration is needed and the backing dimension
    is auto-created from `column:`.

    Verifies the consolidation: one author concept (`times: { ordered_at }`)
    produces exactly one `TemporalRoleConfig` and one `DimensionConfig`,
    both keyed off the same author key, with no duplicates.
    """
    pkg_dir = tmp_path / "pkg_times_consolidation"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"]},
        },
        models={
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "grain": ["order_id"],
                "keys": {"primary": ["order_id"]},
                "times": {
                    "ordered_at": {
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "supported_grains": ["day", "week", "month"],
                        "default": True,
                    },
                },
                "measures": {
                    "order_count": {
                        "label": "Order count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                        "topics": ["o"],
                    },
                },
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    # Temporal role: exactly one, id derived from times: key.
    role_ids = [r.id for r in config.temporal_roles]
    assert role_ids == ["temporal_role.synth_order_ordered_at"], (
        f"expected single auto-derived temporal role, got {role_ids}"
    )
    # Backing dimension: exactly one matching the role's column.
    backing_dims = [d for d in config.dimensions if d.id == "dimension.synth_order_ordered_at"]
    assert len(backing_dims) == 1, (
        f"expected one auto-created backing dimension, got {[d.id for d in config.dimensions]}"
    )
    assert backing_dims[0].column == "ordered_at"


def test_metric_kind_ratio_compiles_to_arithmetic_divide(tmp_path: Path) -> None:
    """`kind: ratio` with direct `numerator:`/`denominator:` fields and no
    explicit `null_behavior:` compiles to an `ArithmeticExpr(op='divide')`
    with both sides resolved to fully qualified metric ids — verifying the
    loader produces the canonical AST shape from the v1 direct-fields
    sugar regardless of whether the optional `null_behavior` is authored.
    """
    pkg_dir = tmp_path / "pkg_ratio_arithmetic_compile"
    _write_synthetic_package(
        pkg_dir,
        models=_widget_model_with_two_count_measures(),
        metrics={
            "premium_share_default": {
                "label": "Premium share",
                "description": "No explicit null_behavior — uses loader default.",
                "kind": "ratio",
                "numerator": "premium_count",
                "denominator": "widget_count",
                # null_behavior intentionally omitted — defaults inside the
                # loader's ratio-normalization step (see config.py:360).
                "value_type": "percent",
                "topics": ["p"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_share_default" in m.id)
    expr = metric.expression
    # Direct ratio fields → arithmetic divide AST.
    assert getattr(expr, "op", None) == "divide", (
        f"ratio should compile to arithmetic divide, got {expr!r}"
    )
    # Both operands are measure-only keys (no top-level metric of those
    # names exists). With auto-publish gone, ratio measure operands compile
    # to `kind: aggregate` nodes against the measure id directly.
    left_measure = getattr(getattr(expr, "left", None), "measure", None)
    right_measure = getattr(getattr(expr, "right", None), "measure", None)
    assert left_measure == "measure.synth.premium_count", (
        f"numerator should resolve to a measure-aggregate node, got {expr.left!r}"
    )
    assert right_measure == "measure.synth.widget_count", (
        f"denominator should resolve to a measure-aggregate node, got {expr.right!r}"
    )


def test_metric_kind_cumulative_direct_measure_field_resolves_package_relative(
    tmp_path: Path,
) -> None:
    """`kind: cumulative` accepts a bare measure key (`measure: revenue_usd`)
    and the loader resolves it package-relative to the fully qualified id
    `measure.<namespace>.<key>` — no `metric.<namespace>.<key>` prefix
    required in the author surface."""
    pkg_dir = tmp_path / "pkg_cumulative_pkg_relative"
    _write_synthetic_package(
        pkg_dir,
        models={
            "widgets": {
                "id": "widgets",
                "entity": "widget",
                "relation": "widget",
                "grain": ["widget_id"],
                "keys": {"primary": ["widget_id"]},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    },
                },
                "default_time": "created_at",
                "measures": {
                    "revenue_usd": {
                        "label": "Revenue (USD)",
                        "description": "Revenue.",
                        "kind": "aggregate",
                        "expr": "amount_usd",
                        "default_agg": "sum",
                        "accumulation": {"kind": "flow"},
                        "value_type": "currency",
                        "topics": ["r"],
                    },
                },
            },
        },
        metrics={
            "cumulative_revenue_usd": {
                "label": "Cumulative revenue (USD)",
                "description": "Running total of revenue.",
                "kind": "cumulative",
                "measure": "revenue_usd",  # bare key, no metric. prefix
                "value_type": "currency",
                "topics": ["c"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "cumulative_revenue_usd" in m.id)
    assert metric.kind == "cumulative"
    # The compiled expression must resolve to the fully qualified measure id.
    inner = getattr(metric.expression, "input", None) or getattr(metric.expression, "operand", None)
    measure_id = getattr(inner, "measure", None)
    assert measure_id == "measure.synth.revenue_usd", (
        f"cumulative measure should resolve package-relative to "
        f"'measure.synth.revenue_usd', got {measure_id!r} "
        f"(expression={metric.expression!r})"
    )


def test_metric_kind_ratio_value_type_overrides_input_types(tmp_path: Path) -> None:
    """A ratio metric authored with `value_type: percent` reports percent
    on the metric record, even when both numerator and denominator are
    count-typed measures. Verifies the explicit-output-type contract from
    the v1 plan: every authored metric declares its own value_type, not
    inheriting it implicitly from the inputs.
    """
    pkg_dir = tmp_path / "pkg_ratio_value_type"
    _write_synthetic_package(
        pkg_dir,
        models=_widget_model_with_two_count_measures(),
        metrics={
            "premium_share": {
                "label": "Premium share",
                "description": "Share of widgets that are premium.",
                "kind": "ratio",
                "numerator": "premium_count",  # count
                "denominator": "widget_count",  # count
                "null_behavior": "null_if_zero",
                "value_type": "percent",  # explicit override
                "topics": ["p"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_share" in m.id)
    assert metric.value_type == "percent", (
        f"ratio metric value_type should be 'percent' even when inputs are "
        f"count-typed, got {metric.value_type!r}"
    )


def test_metric_kind_ratio_preserves_null_behavior_through_loader(tmp_path: Path) -> None:
    """Regression: `null_behavior: null_if_zero` authored on a ratio metric
    must survive the loader's binary→arithmetic conversion. Previously
    `_convert_recipe_expr` dropped the field at the conversion site, causing
    ratio metrics to lose their safety semantics silently."""
    pkg_dir = tmp_path / "pkg_ratio_null_behavior"
    _write_synthetic_package(
        pkg_dir,
        models=_widget_model_with_two_count_measures(),
        metrics={
            "premium_share_safe": {
                "label": "Premium share (safe)",
                "description": "Premium share with explicit null-if-zero handling.",
                "kind": "ratio",
                "numerator": "premium_count",
                "denominator": "widget_count",
                "null_behavior": "null_if_zero",
                "value_type": "percent",
                "topics": ["p"],
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_share_safe" in m.id)
    expr = metric.expression
    # The runtime ArithmeticExpr should carry null_behavior end-to-end.
    assert getattr(expr, "null_behavior", "") == "null_if_zero", (
        f"null_behavior should propagate through the binary→arithmetic "
        f"conversion in _convert_recipe_expr, got {expr!r}"
    )


# ----------------------------------------------------------------------
# MetricConfig.value_type — explicit authoring on every metric.
#
# In v1 the auto-publish path was removed: every authored metric must
# declare its `value_type:` directly. The legacy "inherit value_type
# from underlying measure" pass is gone, and so is the implicit
# `number` default — strict-mode validation rejects metrics without an
# explicit type.
# ----------------------------------------------------------------------


def _metric_by_id(config, metric_id):
    return next(m for m in config.metric_recipes if m.id == metric_id)


def test_authored_metric_value_type_round_trips(tmp_path):
    """Round-trip an authored value_type override — `ratio` is read back
    on the loaded MetricConfig, demonstrating the field is first-class
    on every metric, not derived from the underlying measure."""
    pkg = copy_package_config(tmp_path, "jaffle_shop")
    metrics_dir = pkg / "metrics"
    target = next(metrics_dir.rglob("*.yml"))
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "metrics" in raw
    metric_key, metric_spec = next(iter(raw["metrics"].items()))
    metric_spec["value_type"] = "ratio"
    raw["metrics"][metric_key] = metric_spec
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    config = load_package_config(str(pkg))
    metric_id = str(metric_spec.get("as") or metric_spec.get("id") or f"metric.{metric_key}")
    metric = _metric_by_id(config, metric_id)
    assert metric.value_type == "ratio"


# ----------------------------------------------------------------------
# v1 completion validator — the release-readiness script's
# ``validate_v1_completion`` walker must stay green against the repo's
# shipped packages, and ``validate_metric_expression`` must reject
# legacy freeform expression strings.
# ----------------------------------------------------------------------


def _load_validator_namespace() -> dict[str, object]:
    return runpy.run_path(str(REPO_ROOT / "scripts" / "verify_release_readiness.py"))


def test_v1_completion_validator_passes_on_repo():
    ns = _load_validator_namespace()
    errors: list[str] = []
    ns["validate_v1_completion"](errors)
    assert errors == []


def test_metric_recipe_ast_must_be_structured():
    errors: list[str] = []
    validate_metric_expression(
        "revenue / orders",
        errors,
        known_measures={"measure.demo.revenue"},
        known_metrics={"metric.demo.revenue"},
        known_temporal_roles={"temporal_role.demo.event_time"},
        path="metric_recipes[0].expression",
    )
    assert any("must be a mapping" in error for error in errors)
