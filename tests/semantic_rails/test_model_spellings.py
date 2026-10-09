"""One spelling per key: the bundled packages keep their meaning, and `as:` keeps a public id."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import load_package_snapshot
from tests.semantic_rails.conftest import copy_package_config

ROOT = Path(__file__).resolve().parents[2]

# Each bundled package's semantic fingerprint. Rewriting a package to the one current spelling
# changes its authored bytes, never its meaning, so these hold across every spelling migration.
BUNDLED_FINGERPRINTS = {
    "configs/semantic_rails/jaffle_shop": (
        "sha256:4c9c3c67ae11859916c86019cf76af49d977cdbc8ee90344b6d1d81a3b037860"
    ),
    "configs/semantic_rails/tpch_sf1_showcase": (
        "sha256:1411788e25b3b8136edf871151cfac1ad903bbd9d1096f7b9434db38d07ed5a1"
    ),
    "comparisons/semantic_layers/semantic_rails/package": (
        "sha256:a2b5c027a17f2e09c81de1c04947685c251446da628ceee33eff125d9a6c25c5"
    ),
    "tests/integration/correctness/shop": (
        "sha256:e0efc0e78506590c1fa56ae062541ad26cc07b2ce17120520c2928534aa88e42"
    ),
    "configs/examples/semantic_rails_package_starter.yml": (
        "sha256:200c1a2cb8f72112f9a6453d501724c89a06061f3f0eafbf9def0c3f9a805290"
    ),
}


@pytest.mark.parametrize(("package", "fingerprint"), BUNDLED_FINGERPRINTS.items())
def test_bundled_package_meaning_is_unchanged(package: str, fingerprint: str) -> None:
    assert load_package_snapshot(ROOT / package).semantic_fingerprint == fingerprint


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


@pytest.mark.parametrize(
    ("keys", "refused"),
    [
        pytest.param({"primary": ["date_day", "store_id"]}, "", id="primary"),
        pytest.param(
            {"foreign": {"store": ["store_id"]}},
            "model 'daily_metrics' keys has unknown key 'foreign'",
            id="foreign",
        ),
    ],
)
def test_a_fact_model_keys_its_rows_with_keys_primary(
    tmp_path: Path, keys: dict, refused: str
) -> None:
    """A fact model has no entity, so `keys.primary` stays its row key; nothing else in `keys:`."""
    package = copy_package_config(tmp_path, "jaffle_shop")
    path = package / "models" / "core" / "daily_metrics.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["model"]["keys"] = keys
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    if refused:
        with pytest.raises(SemanticLayerError) as exc:
            load_package_snapshot(package)
        assert any(refused in error for error in exc.value.details["errors"])
        return
    config = load_package_snapshot(package).config
    assert {
        tuple(measure.row_grain)
        for measure in config.measures
        if measure.source_relation == "jaffle_daily_metric_rollup"
    } == {("date_day", "store_id")}
