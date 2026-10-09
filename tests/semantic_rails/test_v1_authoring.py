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

from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import validate_runtime_package
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
                "model": "widgets",
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
                "entities": {"widget": {}},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    },
                },
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count of widgets.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
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
                "model": "widgets",
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
                "model": "widgets",
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


@pytest.mark.parametrize(
    "entities_block", [None, {}, {"device": {}}, {"reading": {}, "device": {}}]
)
@pytest.mark.parametrize("with_relationship", [False, True])
def test_graph_entity_never_borrows_another_entity_key(
    tmp_path: Path, entities_block: dict | None, with_relationship: bool
) -> None:
    reading_model = {"relation": "readings"}
    if entities_block is not None:
        reading_model["entities"] = entities_block
    pkg = _write_synthetic_package(
        tmp_path / "readings",
        graph_entities={
            "reading": {"model": "readings"},
            "device": {"model": "devices", "key": "device_id"},
        },
        models={"readings": reading_model, "devices": {"relation": "devices"}},
        graph_relationships={"reading_device": {"entities": ["reading", "device"]}}
        if with_relationship
        else None,
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert "graph entity 'reading' must declare key" in str(exc.value)
    assert "model 'readings'" in str(exc.value)


@pytest.mark.parametrize("own_first", [False, True])
@pytest.mark.parametrize("key_source", ["graph", "expr"])
def test_graph_binding_selects_primary_independently_of_entity_order(
    tmp_path: Path, own_first: bool, key_source: str
) -> None:
    # The reading key comes from the graph entity, or from the model's own column for it.
    own = {"expr": "reading_id"} if key_source == "expr" else {}
    block = {"reading": own, "device": {}} if own_first else {"device": {}, "reading": own}
    model: dict[str, Any] = {"id": "readings", "relation": "readings", "entities": block}
    reading = {"model": "readings"}
    if key_source == "graph":
        reading["key"] = "reading_id"
    pkg = _write_synthetic_package(
        tmp_path / "readings",
        graph_entities={
            "reading": reading,
            "device": {"model": "devices", "key": "device_id"},
        },
        models={
            "readings": model,
            "devices": {"id": "devices", "relation": "devices", "entities": {"device": {}}},
        },
        graph_relationships={"reading_device": {"entities": ["reading", "device"]}},
    )
    config = load_package_config(str(pkg))
    entity = next(e for e in config.entities if e.id == "entity.synth_reading")
    assert entity.key == ["reading_id"]
    assert entity.foreign_keys == {"device": ["device_id"]}
    assert [(r.id, r.source_entity, r.target_entity) for r in config.relationships] == [
        ("relationship.reading_device", "entity.synth_reading", "entity.synth_device")
    ]


def test_empty_entities_block_preserves_name_binding(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "empty_entities",
        graph_entities={"reading": {"key": ["reading_id"]}},
        models={"reading": {"relation": "readings", "entities": {}}},
    )
    config = load_package_config(str(pkg))
    assert [(entity.id, entity.key, entity.table) for entity in config.entities] == [
        ("entity.synth_reading", ["reading_id"], "readings")
    ]


def test_name_binding_refuses_another_entitys_primary_key(tmp_path: Path) -> None:
    model: dict[str, Any] = {"relation": "readings", "entity": "device", "entities": {"device": {}}}
    pkg = _write_synthetic_package(
        tmp_path / "name_binding_conflict",
        graph_entities={"reading": {}, "device": {"model": "devices", "key": "device_id"}},
        models={"reading": model, "devices": {"relation": "devices"}},
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert all(f"'{name}'" in str(exc.value) for name in ("reading", "device"))
    assert "model 'reading'" in str(exc.value)


@pytest.mark.parametrize("reverse_models", [False, True])
def test_two_models_cannot_claim_an_unbound_entity(tmp_path: Path, reverse_models: bool) -> None:
    models = {
        name: {"entity": "reading", "relation": name, "entities": {"reading": {}}}
        for name in ("readings", "other_readings")
    }
    if reverse_models:
        models = dict(reversed(list(models.items())))
    pkg = _write_synthetic_package(
        tmp_path / "duplicate_claims",
        graph_entities={"reading": {"key": "reading_id"}},
        models=models,
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert all(f"'{name}'" in str(exc.value) for name in ("reading", "readings", "other_readings"))


def test_whitespace_in_graph_model_binding_preserves_relationship(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "normalized_binding",
        graph_entities={
            "reading": {"model": "readings ", "key": "reading_id"},
            "device": {"model": " devices", "key": "device_id"},
        },
        models={
            "readings": {
                "entity": "reading",
                "relation": "readings",
                "entities": {"reading": {}, "device": {}},
            },
            "devices": {"relation": "devices"},
        },
        graph_relationships={"reading_device": {"entities": ["reading", "device"]}},
    )
    config = load_package_config(str(pkg))
    assert [(r.id, r.source_entity, r.target_entity) for r in config.relationships] == [
        ("relationship.reading_device", "entity.synth_reading", "entity.synth_device")
    ]


@pytest.mark.parametrize("second_model", ["device", "sensors"])
@pytest.mark.parametrize("both_entities", [False, True])
def test_implicit_bindings_preserve_authored_entity_relations(
    tmp_path: Path, second_model: str, both_entities: bool
) -> None:
    models = {
        "reading": {"entity": "device", "relation": "devices", "entities": {"device": {}}},
        second_model: {"entity": "reading", "relation": "readings", "entities": {"reading": {}}},
    }
    if both_entities:
        for model in models.values():
            model["entities"] = {"reading": {}, "device": {}}
    pkg = _write_synthetic_package(
        tmp_path / "implicit_identity",
        graph_entities={"reading": {"key": "id"}, "device": {"key": "id"}},
        models=models,
    )
    config = load_package_config(str(pkg))
    assert {entity.id: entity.table for entity in config.entities} == {
        "entity.synth_reading": "readings",
        "entity.synth_device": "devices",
    }


def test_implicit_binding_resolves_model_name(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "implicit_resolution",
        graph_entities={"reading": {"key": "reading_id"}, "device": {"key": "device_id"}},
        models={
            "reading": {"relation": "readings", "entities": {"device": {}, "reading": {}}},
            "other": {"entity": "device", "relation": "devices", "entities": {"device": {}}},
        },
    )
    config = load_package_config(str(pkg))
    assert {entity.id: entity.table for entity in config.entities} == {
        "entity.synth_reading": "readings",
        "entity.synth_device": "devices",
    }


def test_explicit_binding_does_not_prevent_other_identity_backfill(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "mixed_bindings",
        graph_entities={
            "reading": {"model": "readings", "key": "reading_id"},
            "device": {"key": "device_id"},
        },
        models={
            "readings": {"relation": "readings", "entities": {"reading": {}}},
            "sensors": {"entity": "device", "relation": "devices", "entities": {"device": {}}},
        },
    )
    config = load_package_config(str(pkg))
    assert {entity.id: entity.table for entity in config.entities} == {
        "entity.synth_reading": "readings",
        "entity.synth_device": "devices",
    }


@pytest.mark.parametrize("explicit_binding", [False, True])
def test_final_bindings_refuse_two_entities_on_one_model(
    tmp_path: Path, explicit_binding: bool
) -> None:
    reading: dict[str, Any] = {"key": "reading_id"}
    if explicit_binding:
        reading["model"] = "readings"
    pkg = _write_synthetic_package(
        tmp_path / "final_binding_collision",
        graph_entities={"reading": reading, "readings": {"key": "other_id"}},
        models={
            "readings": {"entity": "reading", "relation": "readings", "entities": {"reading": {}}}
        },
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert "primary home of both graph entities" in str(exc.value)
    assert all(f"'{name}'" in str(exc.value) for name in ("readings", "reading"))


def test_name_fallback_cannot_select_an_explicitly_bound_entity(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "reserved_identity",
        graph_entities={"reading": {"model": "readings", "key": "reading_id"}},
        models={
            "reading": {"relation": "other_readings", "entities": {"reading": {}}},
            "readings": {"relation": "readings"},
        },
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert "model 'reading' must identify its primary entity" in str(exc.value)


@pytest.mark.parametrize("entities_block", [{}, {"device": {}}])
def test_explicit_binding_refuses_conflicting_identity_without_primary_resolution(
    tmp_path: Path, entities_block: dict
) -> None:
    model: dict[str, Any] = {"entity": "device", "relation": "readings", "entities": entities_block}
    pkg = _write_synthetic_package(
        tmp_path / "conflicting_identity",
        graph_entities={"reading": {"model": "readings", "key": "reading_id"}},
        models={"readings": model},
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert all(f"'{name}'" in str(exc.value) for name in ("readings", "reading", "device"))


def test_null_model_kind_accepts_authored_graph_relationship(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "null_kind",
        graph_entities={
            "reading": {"model": "readings", "key": "reading_id"},
            "device": {"model": "devices", "key": "device_id"},
        },
        models={
            "readings": {
                "kind": None,
                "relation": "readings",
                "entities": {"reading": {}, "device": {}},
            },
            "devices": {"kind": None, "relation": "devices"},
        },
        graph_relationships={"reading_device": {"entities": ["reading", "device"]}},
    )
    config = load_package_config(str(pkg))
    assert [(edge.id, edge.source_entity, edge.target_entity) for edge in config.relationships] == [
        ("relationship.reading_device", "entity.synth_reading", "entity.synth_device")
    ]


def test_graph_model_cannot_be_primary_for_two_entities(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "conflicting_bindings",
        graph_entities={
            "reading": {"model": "readings", "key": "reading_id"},
            "device": {"model": "readings", "key": "device_id"},
        },
        models={"readings": {"relation": "readings", "entities": {"device": {}}}},
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert all(f"'{name}'" in str(exc.value) for name in ("readings", "reading", "device"))


@pytest.mark.parametrize("customer_first", [False, True])
@pytest.mark.parametrize("identity", [None, "binding", "entity"])
def test_shared_entity_keys_require_explicit_primary_identity(
    tmp_path: Path, customer_first: bool, identity: str | None
) -> None:
    names = ["customer", "supplier"] if customer_first else ["supplier", "customer"]
    graph_entities = {name: {"key": ["id"]} for name in names}
    graph_entities["supplier"]["model"] = "supplier"
    model: dict[str, Any] = {
        "id": "parties",
        "relation": "parties",
        "entities": {name: {} for name in names},
        "dimensions": {"label": {"column": "label", "kind": "categorical"}},
        "measures": {"party_count": {"kind": "entity_count", "entity_key": ["id"]}},
    }
    if identity == "binding":
        graph_entities["customer"]["model"] = "parties"
    elif identity == "entity":
        model["entity"] = "customer"
    pkg = _write_synthetic_package(
        tmp_path / "shared_keys",
        graph_entities=graph_entities,
        models={
            "parties": model,
            "supplier": {
                "id": "supplier",
                "entity": "supplier",
                "relation": "suppliers",
                "entities": {"supplier": {}},
            },
        },
    )
    if identity is None:
        message = (
            "model 'parties' must identify its primary entity with a graph model binding "
            "(graph.entities.<entity>.model), or list an unbound entity with the model's name"
        )
        with pytest.raises(SemanticLayerError) as exc:
            load_package_config(str(pkg))
        assert exc.value.code == "INVALID_CONFIG"
        assert str(exc.value) == message
        assert any(message in error for error in validate_runtime_package(pkg))
    else:
        config = load_package_config(str(pkg))
        customer = next(
            entity for entity in config.entities if entity.id == "entity.synth_customer"
        )
        assert customer.table == "parties"
        assert customer.key == ["id"]


def test_architect_mutation_rolls_back_when_primary_remains_unidentified(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "ambiguous_edit",
        graph_entities={
            "customer": {"key": ["id"]},
            "supplier": {"key": ["id"], "model": "supplier"},
        },
        models={
            "parties": {
                "id": "parties",
                "relation": "parties",
                "entities": {"customer": {}, "supplier": {}},
            },
            "supplier": {
                "entity": "supplier",
                "relation": "suppliers",
                "entities": {"supplier": {}},
            },
        },
    )
    original = (pkg / "models" / "supplier.yml").read_bytes()
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    report = project.upsert_model(
        model_id="supplier", entity_key="supplier", relation="updated_suppliers", primary_key=["id"]
    ).report

    assert report["changes"]
    assert report["status"] == "rolled_back_after_parse_error"
    assert report["parse"]["ok"] is False
    assert any(
        error["code"] == "INVALID_CONFIG"
        and (
            "model 'parties' must identify its primary entity with a graph model binding "
            "(graph.entities.<entity>.model), or list an unbound entity with the model's name"
        )
        in error["message"]
        for error in report["parse"]["errors"]
    )
    assert (pkg / "models" / "supplier.yml").read_bytes() == original


@pytest.mark.parametrize(
    "package_path",
    [
        "configs/semantic_rails/jaffle_shop",
        "configs/semantic_rails/tpch_sf1_showcase",
        "configs/examples/semantic_rails_package_starter.yml",
    ],
)
def test_bundled_packages_resolve_primary_entities(package_path: str) -> None:
    assert load_package_config(str(REPO_ROOT / package_path)).entities


def test_model_primary_is_not_chosen_by_declaration_order(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "unresolved_primary",
        graph_entities={"widget": {"label": "Widget", "key": ["widget_id"]}},
        models={"widgets": {"relation": "widgets", "entities": {"widget": {}}}},
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert "model 'widgets' must identify its primary entity" in str(exc.value)


@pytest.mark.parametrize(
    "relationship",
    [
        {"entities": ["missing", "widget"]},
        {"entities": ["widget", "missing"]},
        {"entities": ["orphan", "widget"], "via": "widget_id"},
        {"entities": ["widget"]},
        {"entities": "widget"},
        {"from": "widget", "to": "widget"},
        {},
        None,
        "widget",
    ],
)
def test_unattachable_graph_relationship_is_refused(tmp_path: Path, relationship: Any) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "unattachable",
        graph_relationships={"unattachable_edge": relationship},
    )
    # An unbound model must not make an unknown graph entity attachable.
    _write_yaml(
        pkg / "models" / "orphan.yml",
        {
            "models": {
                "orphan": {"entity": "orphan", "relation": "orphan", "entities": {"orphan": {}}}
            }
        },
    )
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert "unattachable_edge" in str(exc.value)


def test_graph_relationships_must_be_a_mapping(tmp_path: Path) -> None:
    pkg = _write_synthetic_package(tmp_path / "invalid_relationship_block")
    graph_path = pkg / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    graph["graph"]["relationships"] = [{"entities": ["widget", "widget"]}]
    _write_yaml(graph_path, graph)
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    assert "graph.relationships must be a mapping" in str(exc.value)


def test_model_entities_block_translates_to_legacy_shape(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_entities_block"
    # Two-entity model: orders (primary) + customer (FK).
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"], "model": "orders"},
            "customer": {"label": "Customer", "key": ["customer_id"], "model": "customers"},
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "entities": {"customer": {}},
                "measures": {
                    "customer_count": {
                        "label": "Customer count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                },
            },
            "orders": {
                "id": "orders",
                "relation": "orders",
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
            "order": {"label": "Order", "key": ["order_id"], "model": "orders"},
            "customer": {"label": "Customer", "key": ["customer_id"], "model": "customers"},
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "entities": {"customer": {}},
                "measures": {
                    "customer_count": {
                        "label": "Customer count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                },
            },
            "orders": {
                "id": "orders",
                "relation": "orders",
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
            "user": {"label": "User", "key": ["user_id"], "model": "users"},
            "account": {"label": "Account", "key": ["account_id"], "model": "accounts"},
            "user_account_link": {
                "label": "User account link",
                "key": ["link_id"],
                "model": "links",
            },
        },
        models={
            "users": {
                "id": "users",
                "entity": "user",
                "relation": "user",
                "entities": {"user": {}},
                "measures": {
                    "user_count": {
                        "label": "User count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["user_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                },
            },
            "accounts": {
                "id": "accounts",
                "entity": "account",
                "relation": "account",
                "entities": {"account": {}},
                "measures": {
                    "account_count": {
                        "label": "Account count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["account_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                },
            },
            "links": {
                "id": "links",
                "relation": "user_account_link",
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


def _write_graph_relationship_package(pkg_dir: Path, *, relationship_extra: dict[str, Any]) -> None:
    """Write the shared order/customer package with explicit relationship options."""
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"], "model": "orders"},
            "customer": {"label": "Customer", "key": ["customer_id"], "model": "customers"},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "safety": "safe",
                **relationship_extra,
            },
        },
        models={
            "customers": {
                "id": "customers",
                "entity": "customer",
                "relation": "customer",
                "entities": {"customer": {}},
                "measures": {
                    "customer_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["customer_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                },
            },
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "entities": {"order": {}, "customer": {}},
                "measures": {
                    "order_count": {
                        "label": "Count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["order_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                },
            },
        },
    )


def test_graph_relationships_block_translates_bidirectional_pair(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_graph_rel"
    _write_graph_relationship_package(pkg_dir, relationship_extra={})
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    assert rel.cardinality == "N:1"


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
                "entities": {"widget": {}},
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
                "entities": {"widget": {}},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    },
                },
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
                "entities": {"widget": {}},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    },
                },
                "measures": {
                    "widget_count": {
                        "label": "Widget count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                    "premium_count": {
                        "label": "Premium count",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
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
                "value_type": "percent",
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
# Section 7: Legacy authoring forms are refused at load
# ---------------------------------------------------------------------------

_MODEL = ("models", "widgets")
_MEASURE = (*_MODEL, "measures", "widget_count")
_DELETE = object()


@pytest.mark.parametrize(
    ("file", "path", "value", "expected"),
    [
        pytest.param(
            "package.yml",
            ("package", "schema_strict"),
            True,
            ("package block has unknown key 'schema_strict'", "strict is the only profile"),
            id="schema-strict-true",
        ),
        pytest.param(
            "package.yml",
            ("package", "schema_strict"),
            False,
            ("package block has unknown key 'schema_strict'", "strict is the only profile"),
            id="schema-strict-false",
        ),
        pytest.param(
            "graph.yml",
            ("graph", "entities", "widget", "id"),
            "entity.synth_widget",
            ("graph entity 'widget' has unknown key 'id'", "write `as:`"),
            id="graph-entity-id",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "dimensions"),
            {
                "color": {
                    "id": "dimension.synth_widget_color",
                    "column": "color",
                    "kind": "categorical",
                }
            },
            ("model 'widgets' dimension 'color' has unknown key 'id'", "write `as:`"),
            id="dimension-id",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MEASURE, "id"),
            "measure.synth.widget_count",
            ("model 'widgets' measure 'widget_count' has unknown key 'id'", "write `as:`"),
            id="measure-id",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "grain"),
            ["widget_id"],
            ("model 'widgets' has unknown key 'grain'", "the entity's key keys the model's rows"),
            id="model-grain",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "joins"),
            {"other": {"to": "other", "cardinality": "N:1"}},
            ("model 'widgets' has unknown key 'joins'", "`graph.relationships` row"),
            id="model-joins",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "keys"),
            {"primary": ["widget_id"]},
            ("model 'widgets' authors 'keys.primary:' beside 'entities:'",),
            id="keys-primary-beside-entities",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "keys"),
            {"foreign": {"gadget": ["gadget_id"]}},
            ("model 'widgets' authors 'keys.foreign:'",),
            id="keys-foreign",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "entities"),
            _DELETE,
            ("model 'widgets' authors a singular 'entity:'",),
            id="singular-entity",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MODEL, "dimensions"),
            {"color": {"column": "color", "kind": "categorical", "topics": ["widgets"]}},
            ("model 'widgets' dimension 'color' has unknown key 'topics'",),
            id="dimension-topics",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MEASURE, "topics"),
            ["widgets"],
            ("measure 'widget_count' has unknown key 'topics'",),
            id="measure-topics",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MEASURE, "preferred_companion_metrics"),
            ["widgets"],
            ("measure 'widget_count' has unknown key 'preferred_companion_metrics'",),
            id="measure-companion-metrics",
        ),
        pytest.param(
            "metrics.yml",
            ("metrics", "widgets", "topics"),
            ["widgets"],
            ("metric 'widgets' has unknown key 'topics'",),
            id="metric-topics",
        ),
        pytest.param(
            "segments.yml",
            ("segments",),
            {"big_widgets": {"entity": "widget", "topics": ["widgets"]}},
            ("segment 'big_widgets' has unknown key 'topics'",),
            id="segment-topics",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MEASURE, "kind"),
            _DELETE,
            ("measure 'widget_count' has no 'kind:'",),
            id="measure-without-kind",
        ),
        pytest.param(
            "metrics.yml",
            ("metrics", "widgets", "value_type"),
            _DELETE,
            ("metric 'widgets': missing 'value_type:'",),
            id="metric-without-value-type",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MEASURE, "publish"),
            {"id": "metric.synth.widgets", "label": "Widgets"},
            ("measure 'widget_count' publish must be true or false",),
            id="measure-publish-mapping",
        ),
        pytest.param(
            "models/widgets.yml",
            (*_MEASURE, "accumulation"),
            "rolling_population",
            ("measure 'widget_count' has unknown accumulation kind 'rolling_population'",),
            id="freeform-accumulation",
        ),
    ],
)
def test_legacy_authoring_form_is_refused_at_load(
    tmp_path: Path, file: str, path: tuple[str, ...], value: Any, expected: tuple[str, ...]
) -> None:
    """Every package is read with one set of authoring rules: a legacy form is refused at
    load with an error that names it, and no package flag opts in or out."""
    pkg = _write_synthetic_package(
        tmp_path / "legacy_form",
        metrics={
            "widgets": {
                "label": "Widgets",
                "description": "Count of widgets.",
                "kind": "aggregate",
                "measure": "widget_count",
                "value_type": "count",
            },
        },
    )
    load_package_config(str(pkg))
    document = pkg / file
    raw = yaml.safe_load(document.read_text(encoding="utf-8")) if document.exists() else {}
    *parents, key = path
    block = raw
    for part in parents:
        block = block[part]
    if value is _DELETE:
        del block[key]
    else:
        block[key] = value
    _write_yaml(document, raw)
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    errors = exc.value.details["errors"]
    assert any(all(part in error for part in expected) for error in errors), errors
    assert any(all(part in error for part in expected) for error in validate_runtime_package(pkg))


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
    _write_graph_relationship_package(
        pkg_dir, relationship_extra={"allowed_directions": ["forward"]}
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
    _write_graph_relationship_package(pkg_dir, relationship_extra={})
    config = load_package_config(str(pkg_dir))
    order_id = next(e.id for e in config.entities if e.id.endswith("_order"))
    customer_id = next(e.id for e in config.entities if e.id.endswith("_customer"))
    rel = next(
        r
        for r in config.relationships
        if r.source_entity == order_id and r.target_entity == customer_id
    )
    assert rel.allowed_directions == ["forward", "reverse"]


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
            "entities": {"widget": {}},
            "times": {
                "created_at": {
                    "label": "Created at",
                    "column": "created_at",
                    "kind": "timestamp",
                    "class": "event_time",
                    "default": True,
                },
            },
            "measures": {
                "widget_count": {
                    "label": "Widget count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["widget_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                },
                "premium_count": {
                    "label": "Premium count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["widget_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
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
            },
            # Ratio whose numerator references the top-level metric above
            # (not the measure!) and whose denominator is a measure.
            "premium_share_v2": {
                "label": "Premium share v2",
                "description": "Top-level metric over measure.",
                "kind": "ratio",
                "numerator": "premium_metric",  # → top-level metric
                "denominator": "widget_count",  # → measure
                "value_type": "percent",
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
                "entities": {"widget": {}},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    },
                },
                "measures": {
                    "shared_key": {
                        "label": "Measure shared_key",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
                    },
                    "denom": {
                        "label": "Denom",
                        "description": "Count.",
                        "kind": "entity_count",
                        "entity_key": ["widget_id"],
                        "accumulation": {"kind": "event"},
                        "value_type": "count",
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
                "value_type": "percent",
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
            "entities": {"customer": {}},
            "measures": {
                "customer_count": {
                    "label": "Customer count",
                    "description": "Count.",
                    "kind": "entity_count",
                    "entity_key": ["customer_id"],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                },
            },
        },
        "orders": {
            "id": "orders",
            "relation": "orders",
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
                },
            },
        },
    }


def test_graph_relationships_rollup_safe_reverse(tmp_path: Path) -> None:
    """Reverse rewrite permissions stay on the authored edge, without a second edge."""
    pkg_dir = tmp_path / "pkg_rollup_both_dirs"
    _write_synthetic_package(
        pkg_dir,
        graph_entities={
            "order": {"label": "Order", "key": ["order_id"], "model": "orders"},
            "customer": {"label": "Customer", "key": ["customer_id"], "model": "customers"},
        },
        graph_relationships={
            "customer_order": {
                "entities": ["order", "customer"],
                "cardinality": "many_to_one",
                "rollup_safe": {
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
            "order": {"label": "Order", "key": ["order_id"], "model": "orders"},
        },
        models={
            "orders": {
                "id": "orders",
                "entity": "order",
                "relation": "orders",
                "entities": {"order": {}},
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
    """`kind: ratio` with direct `numerator:`/`denominator:` fields compiles to an
    `ArithmeticExpr(op='divide')` with both sides resolved to fully qualified
    metric ids — verifying the loader produces the canonical AST shape from the
    v1 direct-fields sugar.
    """
    pkg_dir = tmp_path / "pkg_ratio_arithmetic_compile"
    _write_synthetic_package(
        pkg_dir,
        models=_widget_model_with_two_count_measures(),
        metrics={
            "premium_share_default": {
                "label": "Premium share",
                "description": "Premium share of widgets.",
                "kind": "ratio",
                "numerator": "premium_count",
                "denominator": "widget_count",
                "value_type": "percent",
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
                "entities": {"widget": {}},
                "times": {
                    "created_at": {
                        "label": "Created at",
                        "column": "created_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    },
                },
                "measures": {
                    "revenue_usd": {
                        "label": "Revenue (USD)",
                        "description": "Revenue.",
                        "kind": "aggregate",
                        "expr": "amount_usd",
                        "default_agg": "sum",
                        "accumulation": {"kind": "flow"},
                        "value_type": "currency",
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
                "value_type": "percent",  # explicit override
            },
        },
    )
    config = load_package_config(str(pkg_dir))
    metric = next(m for m in config.metric_recipes if "premium_share" in m.id)
    assert metric.value_type == "percent", (
        f"ratio metric value_type should be 'percent' even when inputs are "
        f"count-typed, got {metric.value_type!r}"
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
# shipped packages.
# ----------------------------------------------------------------------


def _load_validator_namespace() -> dict[str, object]:
    return runpy.run_path(str(REPO_ROOT / "scripts" / "verify_release_readiness.py"))


def test_v1_completion_validator_passes_on_repo():
    ns = _load_validator_namespace()
    errors: list[str] = []
    ns["validate_v1_completion"](errors)
    assert errors == []
