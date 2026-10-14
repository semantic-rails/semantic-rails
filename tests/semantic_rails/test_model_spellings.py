"""A graph relationship keeps a public id with `as:`, as every other object does; `id:` is refused."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import load_package_snapshot

ROOT = Path(__file__).resolve().parents[2]


def test_jaffle_relationship_ids() -> None:
    """Every relationship keeps its public id; the one authored with `as:` takes that id."""
    config = load_package_snapshot(ROOT / "configs/semantic_rails/jaffle_shop").config
    assert sorted(row.id for row in config.relationships) == [
        "relationship.customer_history_customer",
        "relationship.customer_history_store",
        "relationship.jaffle_order_customer_history",
        "relationship.order_items_order",
        "relationship.order_items_product",
        "relationship.order_lifecycle_customer",
        "relationship.order_lifecycle_order",
        "relationship.order_lifecycle_store",
        "relationship.orders_customer",
        "relationship.orders_store",
        "relationship.store_inventory_snapshots_store",
        "relationship.store_inventory_snapshots_time",
        "relationship.storefront_sessions_customer",
        "relationship.storefront_sessions_order",
        "relationship.storefront_sessions_store",
        "relationship.supplies_product",
    ]


def _relationship_package(root: Path, spec: dict) -> Path:
    for name, document in {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "synth",
                "namespace": "synth",
                "warehouse": "duckdb",
                "default_db": "data/synth.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "widget": {"key": ["widget_id"], "model": "widgets"},
                    "gadget": {"key": ["gadget_id"], "model": "gadgets"},
                },
                "relationships": {
                    "widgets_gadget": {
                        **spec,
                        "entities": ["widget", "gadget"],
                        "cardinality": "many_to_one",
                    }
                },
            }
        },
        "models/widgets.yml": {
            "model": {
                "id": "widgets",
                "relation": "widgets",
                "entities": {"widget": {}, "gadget": {}},
            }
        },
        "models/gadgets.yml": {
            "model": {"id": "gadgets", "relation": "gadgets", "entities": {"gadget": {}}}
        },
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(document), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param({}, "relationship.widgets_gadget", id="derived"),
        pytest.param({"as": "relationship.widget_gadget"}, "relationship.widget_gadget", id="as"),
    ],
)
def test_graph_relationship_id(tmp_path: Path, spec: dict, expected: str) -> None:
    """A graph relationship's id is derived from its key, or kept with `as:`."""
    config = load_package_snapshot(_relationship_package(tmp_path, spec)).config
    assert [row.id for row in config.relationships] == [expected]


def test_graph_relationship_id_key_is_refused(tmp_path: Path) -> None:
    package = _relationship_package(tmp_path, {"id": "relationship.widget_gadget"})
    with pytest.raises(SemanticLayerError) as exc:
        load_package_snapshot(package)
    assert exc.value.code == "INVALID_CONFIG"
    assert any(
        "graph relationship 'widgets_gadget' has unknown key 'id'" in error
        and "write `as:` only to keep a public id" in error
        for error in exc.value.details["errors"]
    )
