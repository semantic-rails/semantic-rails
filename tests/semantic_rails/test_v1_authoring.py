"""Synthetic-package unit tests for v1 authoring normalizations.

Each test builds a tiny synthetic package (3-5 lines per concern) and
verifies the loader produces the expected runtime shape. The mini-packages
are kept deliberately small so a regression points at one normalization,
not a tangle of features.
"""

from __future__ import annotations

import runpy
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import _load_package_source, load_package_config
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
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
        _write_yaml(package_dir / "models" / f"{model_id}.yml", {"model": model})

    if metrics:
        _write_yaml(package_dir / "metrics.yml", {"metrics": metrics})

    return package_dir


# ---------------------------------------------------------------------------
# Section 1: ID auto-derivation and `as:` escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "public_id", ["entity.synth.legacy_widget_id", "entity.othernamespace.widget"]
)
def test_as_escape_hatch_overrides_auto_derived_id(tmp_path: Path, public_id: str) -> None:
    # `as:` keeps a public id, even one outside the package's namespace.
    widget = {"as": public_id, "label": "Widget", "key": ["widget_id"], "model": "widgets"}
    pkg = _write_synthetic_package(tmp_path / "pkg_as_hatch", graph_entities={"widget": widget})
    assert [entity.id for entity in load_package_config(str(pkg)).entities] == [public_id]


# ---------------------------------------------------------------------------
# Section 2: model.entities: block authoring sugar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entities_block", "with_relationship"),
    [(None, False), ({}, True), ({"device": {}}, False), ({"reading": {}, "device": {}}, True)],
)
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
    # Nothing is homed on 'reading', so it would be row grain under its own name: taken.
    assert "graph.entities already declares 'reading'" in str(exc.value)


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


@pytest.mark.parametrize(
    ("identity", "customer_first"),
    [(None, False), (None, True), ("binding", True), ("entity", False)],
)
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
        # Listing entities homed elsewhere makes 'parties' row grain, so customer has no home.
        message = "graph entity 'customer' references unknown model 'customer'"
        with pytest.raises(SemanticLayerError) as exc:
            load_package_config(str(pkg))
        assert exc.value.code == "INVALID_CONFIG"
        assert message in str(exc.value)
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
        and "graph entity 'customer' references unknown model 'customer'" in error["message"]
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
    # 'widgets' lists only an entity it isn't home to (row grain), so 'widget' has no home.
    assert "graph entity 'widget' references unknown model 'widget'" in str(exc.value)


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
        {"model": {"entity": "orphan", "relation": "orphan", "entities": {"orphan": {}}}},
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


def _count(entity_key: str, label: str = "Count") -> dict[str, Any]:
    return {
        "label": label,
        "description": "Count.",
        "kind": "entity_count",
        "entity_key": [entity_key],
        "accumulation": {"kind": "event"},
        "value_type": "count",
    }


def _model(
    model_id: str, entities: dict[str, Any], measures: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {"id": model_id, "entities": entities, "measures": measures, **extra}


def _order_customer_models(customer_binding: dict[str, Any]) -> dict[str, Any]:
    """Customers, and orders that declare the customer through the entities block."""
    return {
        "customers": _model(
            "customers",
            {"customer": {}},
            {"customer_count": _count("customer_id")},
            entity="customer",
            relation="customer",
        ),
        "orders": _model(
            "orders",
            {"order": {}, "customer": customer_binding},
            {"order_count": _count("order_id")},
            relation="orders",
        ),
    }


_ORDER_CUSTOMER = {
    "order": {"label": "Order", "key": ["order_id"], "model": "orders"},
    "customer": {"label": "Customer", "key": ["customer_id"], "model": "customers"},
}


@pytest.mark.parametrize(
    ("binding", "column"), [({}, "customer_id"), ({"expr": "cust_id"}, "cust_id")]
)
def test_model_entities_block_infers_the_relationship_and_key_column(
    tmp_path: Path, binding: dict[str, Any], column: str
) -> None:
    pkg = _write_synthetic_package(
        tmp_path / "pkg_entities_block",
        graph_entities=_ORDER_CUSTOMER,
        models=_order_customer_models(binding),
    )
    config = load_package_config(str(pkg))
    pairs = {(r.source_entity, r.target_entity) for r in config.relationships}
    assert ("entity.synth_order", "entity.synth_customer") in pairs
    order = next(e for e in config.entities if e.id == "entity.synth_order")
    assert order.foreign_keys["customer"] == [column]


def test_model_entities_block_bridge_false_disables_inferred_relationships(tmp_path: Path) -> None:
    graph_entities = {
        name: {"label": name, "key": [f"{key}_id"], "model": model}
        for name, key, model in (
            ("user", "user", "users"),
            ("account", "account", "accounts"),
            ("user_account_link", "link", "links"),
        )
    }
    pkg = _write_synthetic_package(
        tmp_path / "pkg_bridge_false",
        graph_entities=graph_entities,
        models={
            "users": _model(
                "users",
                {"user": {}},
                {"user_count": _count("user_id")},
                entity="user",
                relation="user",
            ),
            "accounts": _model(
                "accounts",
                {"account": {}},
                {"account_count": _count("account_id")},
                entity="account",
                relation="account",
            ),
            "links": _model(
                "links",
                {"bridge": False, "user_account_link": {}, "user": {}, "account": {}},
                {"link_count": _count("link_id")},
                relation="user_account_link",
            ),
        },
    )
    config = load_package_config(str(pkg))
    assert not [
        r for r in config.relationships if r.source_entity == "entity.synth_user_account_link"
    ]


# ---------------------------------------------------------------------------
# Section 3: graph.relationships: override block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "attribute", "expected"),
    [
        ({}, "cardinality", "N:1"),
        ({}, "allowed_directions", ["forward", "reverse"]),
        ({"allowed_directions": ["forward"]}, "allowed_directions", ["forward"]),
        # Reverse rewrite permissions stay on the authored edge, without a second edge.
        (
            {"rollup_safe": {"reverse": ["max", "min"]}},
            "rollup_safe_aggregations_reverse",
            ["max", "min"],
        ),
    ],
)
def test_graph_relationship_options_reach_the_relationship(
    tmp_path: Path, extra: dict[str, Any], attribute: str, expected: Any
) -> None:
    relationship = {
        "entities": ["order", "customer"],
        "cardinality": "many_to_one",
        "safety": "safe",
    }
    pkg = _write_synthetic_package(
        tmp_path / "pkg_graph_rel",
        graph_entities=_ORDER_CUSTOMER,
        graph_relationships={"customer_order": {**relationship, **extra}},
        models=_order_customer_models({}),
    )
    edges = [
        r
        for r in load_package_config(str(pkg)).relationships
        if (r.source_entity, r.target_entity) == ("entity.synth_order", "entity.synth_customer")
    ]
    assert [getattr(edge, attribute) for edge in edges] == [expected]


# ---------------------------------------------------------------------------
# Sections 4-6: times default flag, flat measure fields, direct metric fields
# ---------------------------------------------------------------------------


def _widget_models(**measures: dict[str, Any]) -> dict[str, Any]:
    times = {
        "created_at": {
            "label": "Created at",
            "column": "created_at",
            "kind": "timestamp",
            "class": "event_time",
            "default": True,
        },
    }
    measures = {"widget_count": _count("widget_id"), **measures}
    return {
        "widgets": _model(
            "widgets", {"widget": {}}, measures, entity="widget", relation="widget", times=times
        )
    }


def _metric(kind: str, value_type: str, **fields: Any) -> dict[str, Any]:
    return {"label": kind, "description": kind, "kind": kind, "value_type": value_type, **fields}


def test_direct_authoring_fields_resolve_package_relative_ids(tmp_path: Path) -> None:
    revenue = {
        "label": "Revenue (USD)",
        "description": "Revenue.",
        "kind": "aggregate",
        # Direct fields, not a nested `expression:` wrapper.
        "expr": "amount_usd",
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
        "value_type": "currency",
    }
    premium_metric = {"kind": "metric", "metric": "premium_metric"}
    pkg = _write_synthetic_package(
        tmp_path / "pkg_direct_fields",
        models=_widget_models(premium_count=_count("widget_id"), revenue_usd=revenue),
        metrics={
            "widget_count_metric": _metric("aggregate", "count", measure="widget_count"),
            "premium_metric": _metric("aggregate", "count", measure="premium_count"),
            # Both operands name measures; the ratio's own value_type wins over theirs.
            "premium_share": _metric(
                "ratio", "percent", numerator="premium_count", denominator="widget_count"
            ),
            # A metric numerator over a measure denominator.
            "premium_share_v2": _metric(
                "ratio", "percent", numerator="premium_metric", denominator="widget_count"
            ),
            "premium_metric_double": _metric(
                "derived",
                "count",
                expression={
                    "kind": "binary",
                    "op": "multiply",
                    "left": premium_metric,
                    "right": premium_metric,
                },
            ),
            # A bare measure key, no `metric.` prefix.
            "cumulative_revenue_usd": _metric("cumulative", "currency", measure="revenue_usd"),
        },
    )
    config = load_package_config(str(pkg))
    (widget,) = config.entities
    assert (widget.id, widget.name) == ("entity.synth_widget", "synth.Widget")
    measures = {m.id: m for m in config.measures}
    # `default: true` on the times entry replaces the model's `default_time:`.
    assert measures["measure.synth.widget_count"].compatible_temporal_roles
    assert measures["measure.synth.revenue_usd"].default_aggregation == "sum"
    metrics = {m.id: m for m in config.metric_recipes}
    assert metrics["metric.synth.widget_count_metric"].kind == "aggregate"
    share = metrics["metric.synth.premium_share"]
    assert (share.kind, share.value_type, share.expression.op) == ("ratio", "percent", "divide")
    assert (share.expression.left.measure, share.expression.right.measure) == (
        "measure.synth.premium_count",
        "measure.synth.widget_count",
    )
    mixed = metrics["metric.synth.premium_share_v2"].expression
    assert mixed.left.metric_recipe == "metric.synth.premium_metric"
    assert mixed.right.measure == "measure.synth.widget_count"
    double = metrics["metric.synth.premium_metric_double"].expression
    assert double.left.metric_recipe == double.right.metric_recipe == "metric.synth.premium_metric"
    cumulative = metrics["metric.synth.cumulative_revenue_usd"]
    assert cumulative.kind == "cumulative"
    assert cumulative.expression.input.measure == "measure.synth.revenue_usd"


# ---------------------------------------------------------------------------
# Section 7: Legacy authoring forms are refused at load
# ---------------------------------------------------------------------------

_MODEL = ("model",)
_MEASURE = (*_MODEL, "measures", "widget_count")
_DELETE = object()


_LEGACY_FORMS = [
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
        (*_MODEL, "dimensions"),
        {"color": {"column": "color", "kind": "categorical", "preferred_filter_ops": ["="]}},
        ("model 'widgets' dimension 'color' has unknown key 'preferred_filter_ops'",),
        id="dimension-preferred-filter-ops",
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
    pytest.param(
        "package.yml",
        ("defaults", "measure"),
        {"accumulation": {"kind": "stok"}},
        ("defaults.measure has unknown accumulation kind 'stok'",),
        id="defaults-accumulation-kind",
    ),
    pytest.param(
        "package.yml",
        ("defaults", "measure"),
        {"accumulation": "stok"},
        ("defaults.measure has unknown accumulation kind 'stok'",),
        id="defaults-accumulation-scalar",
    ),
]


def _one_single_file(rows: list[Any], *single_file_ids: str) -> list[Any]:
    """Each row in a package directory; the named rows again as one single-file package."""
    return [
        *(pytest.param(*row.values, "directory", id=row.id) for row in rows),
        *(
            pytest.param(*row.values, "single_file", id=f"{row.id}-single-file")
            for row in rows
            if row.id in single_file_ids
        ),
    ]


@pytest.mark.parametrize(
    ("file", "path", "value", "expected", "layout"),
    _one_single_file(_LEGACY_FORMS, "measure-id", "segment-topics"),
)
def test_legacy_authoring_form_is_refused_at_load(
    tmp_path: Path,
    file: str,
    path: tuple[str, ...],
    value: Any,
    expected: tuple[str, ...],
    layout: str,
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
    if layout == "single_file":
        merged = _load_package_source(str(pkg))
        pkg = pkg / "package.yml"
        _write_yaml(pkg, merged)
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    errors = exc.value.details["errors"]
    assert any(all(part in error for part in expected) for error in errors), errors
    assert any(all(part in error for part in expected) for error in validate_runtime_package(pkg))


def test_measure_inherits_defaults_accumulation_kind(tmp_path: Path) -> None:
    """A measure with no `accumulation:` of its own takes `defaults.measure.accumulation`."""
    pkg = _write_synthetic_package(
        tmp_path / "defaults_stock",
        models={
            "widgets": {
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
                    "balance_usd": {
                        "label": "Balance (USD)",
                        "description": "Balance.",
                        "kind": "aggregate",
                        "expr": "balance_usd",
                        "value_type": "currency",
                    },
                },
            },
        },
    )
    document = pkg / "package.yml"
    raw = yaml.safe_load(document.read_text(encoding="utf-8"))
    raw["defaults"]["measure"] = {"accumulation": {"kind": "stock"}}
    _write_yaml(document, raw)
    config = load_package_config(str(pkg))
    balance = next(m for m in config.measures if "balance_usd" in m.id)
    assert balance.measure_class == "semi_additive"
    assert balance.default_aggregation == "last_value"


# ---------------------------------------------------------------------------
# Section 8: A measure's accumulation states the kind and snapshot it means
# ---------------------------------------------------------------------------

_STOCK_SOP = {"kind": "stock", "snapshot": "start_of_period"}
_BALANCES = [
    ("a", date(2026, 1, 3), 10),
    ("a", date(2026, 1, 20), 15),
    ("a", date(2026, 2, 2), 20),
    ("a", date(2026, 2, 27), 25),
    ("b", date(2026, 1, 5), 100),
    ("b", date(2026, 1, 31), 90),
    ("b", date(2026, 2, 1), 80),
    ("b", date(2026, 2, 15), 70),
]


def _write_balance_package(
    package_dir: Path,
    *,
    default: Any = _DELETE,
    accumulation: Any = _DELETE,
    layout: str = "directory",
) -> Path:
    """Daily balances of two accounts. ``default`` is `defaults.measure.accumulation` and
    ``accumulation`` the balance measure's own; ``_DELETE`` leaves either unauthored."""
    measure: dict[str, Any] = {"kind": "aggregate", "expr": "balance", "value_type": "currency"}
    if accumulation is not _DELETE:
        measure["accumulation"] = accumulation
    pkg = _write_synthetic_package(
        package_dir,
        package_extra={"seed": {"kind": "external"}},
        graph_entities={"account_day": {"key": ["account_id", "date_day"], "model": "balances"}},
        models={
            "balances": {
                "relation": "balances",
                "entities": {"account_day": {}},
                "times": {
                    "date_day": {
                        "column": "date_day",
                        "kind": "date",
                        "class": "as_of_time",
                        "default": True,
                    },
                },
                "measures": {"balance_usd": measure},
            },
        },
    )
    if default is not _DELETE:
        raw = yaml.safe_load((pkg / "package.yml").read_text(encoding="utf-8"))
        raw["defaults"]["measure"] = {"accumulation": default}
        _write_yaml(pkg / "package.yml", raw)
    if layout == "single_file":
        merged = _load_package_source(str(pkg))
        pkg = pkg / "package.yml"
        _write_yaml(pkg, merged)
    return pkg


_MISREAD_ACCUMULATIONS = [
    pytest.param(
        _DELETE,
        {"kind": "stock", "snapshot": "start_of_peroid"},
        ("measure 'balance_usd'", "unknown accumulation snapshot 'start_of_peroid'"),
        id="snapshot-typo",
    ),
    pytest.param(
        _DELETE,
        {"kind": "stock", "snapshot": None},
        ("measure 'balance_usd'", "unknown accumulation snapshot ''"),
        id="snapshot-empty",
    ),
    pytest.param(
        _DELETE,
        {"snapshot": "start_of_period"},
        ("measure 'balance_usd'", "without 'kind: stock'"),
        id="snapshot-without-kind",
    ),
    pytest.param(
        _DELETE,
        {"kind": "flow", "snapshot": "end_of_period"},
        ("measure 'balance_usd'", "without 'kind: stock'"),
        id="snapshot-on-flow",
    ),
    pytest.param(
        {"kind": "stock"},
        {"snapshot": "start_of_period"},
        ("measure 'balance_usd'", "without 'kind: stock'"),
        id="override-drops-default-kind",
    ),
    pytest.param(
        "stock",
        {},
        ("measure 'balance_usd'", "names no 'kind:'"),
        id="override-names-no-kind",
    ),
    pytest.param(
        {"kind": "stock"},
        None,
        ("measure 'balance_usd'", "names no 'kind:'"),
        id="override-null",
    ),
    pytest.param(
        _STOCK_SOP,
        {"kind": "stock"},
        ("measure 'balance_usd'", "names no 'snapshot:'", "'start_of_period'"),
        id="override-drops-default-snapshot",
    ),
    pytest.param(
        _STOCK_SOP,
        "stock",
        ("measure 'balance_usd'", "names no 'snapshot:'", "'start_of_period'"),
        id="scalar-override-drops-default-snapshot",
    ),
    pytest.param(
        {"kind": "stock", "snapshot": "start_of_peroid"},
        _DELETE,
        ("defaults.measure", "unknown accumulation snapshot 'start_of_peroid'"),
        id="defaults-snapshot-typo",
    ),
    pytest.param(
        {"snapshot": "start_of_period"},
        _DELETE,
        ("defaults.measure", "without 'kind: stock'"),
        id="defaults-snapshot-without-kind",
    ),
    pytest.param(
        {"kind": "flow", "snapshot": "start_of_period"},
        _DELETE,
        ("defaults.measure", "without 'kind: stock'"),
        id="defaults-snapshot-on-flow",
    ),
]


@pytest.mark.parametrize(
    ("default", "accumulation", "expected", "layout"),
    _one_single_file(_MISREAD_ACCUMULATIONS, "override-drops-default-snapshot"),
)
def test_accumulation_read_as_another_is_refused_at_load(
    tmp_path: Path, default: Any, accumulation: Any, expected: tuple[str, ...], layout: str
) -> None:
    """A measure's `accumulation:` replaces `defaults.measure.accumulation` whole, and the
    loader reads an unknown snapshot as end_of_period and a snapshot without `kind: stock`
    as a sum. A value that doesn't state what it means is refused instead of read as another."""
    pkg = _write_balance_package(
        tmp_path / "balances", default=default, accumulation=accumulation, layout=layout
    )
    expected = (*expected, "replaces `defaults.measure.accumulation` whole")
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(pkg))
    assert exc.value.code == "INVALID_CONFIG"
    errors = exc.value.details["errors"]
    assert any(all(part in error for part in expected) for error in errors), errors
    assert any(all(part in error for part in expected) for error in validate_runtime_package(pkg))


_WHOLE_ACCUMULATIONS = [
    pytest.param(_DELETE, _STOCK_SOP, "first_value", id="complete"),
    pytest.param(_STOCK_SOP, _DELETE, "first_value", id="inherits-default"),
    pytest.param(
        _STOCK_SOP,
        {"kind": "stock", "snapshot": " End_Of_Period "},
        "last_value",
        id="complete-override",
    ),
    pytest.param(_STOCK_SOP, "flow", "sum", id="override-names-kind"),
]


@pytest.mark.parametrize(
    ("default", "accumulation", "aggregation", "layout"),
    _one_single_file(_WHOLE_ACCUMULATIONS, "inherits-default"),
)
def test_accumulation_stated_whole_loads(
    tmp_path: Path, default: Any, accumulation: Any, aggregation: str, layout: str
) -> None:
    pkg = _write_balance_package(
        tmp_path / "balances", default=default, accumulation=accumulation, layout=layout
    )
    balance = next(m for m in load_package_config(str(pkg)).measures if "balance_usd" in m.id)
    assert balance.default_aggregation == aggregation
    assert not [
        error
        for error in validate_runtime_package(pkg)
        if "measure 'balance_usd'" in error or "defaults.measure" in error
    ]


def _load_balances(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("create table balances (account_id varchar, date_day date, balance integer)")
    connection.executemany("insert into balances values (?, ?, ?)", _BALANCES)


def test_start_of_period_stock_sums_each_series_opening_balance(tmp_path: Path) -> None:
    """The complete start-of-period stock reads each account's first balance in the month,
    then adds up the accounts: the reference SQL's rows."""
    pkg = _write_balance_package(tmp_path / "balances", accumulation=_STOCK_SOP)
    (pkg / "data").mkdir()
    connection = duckdb.connect(str(pkg / "data" / "example.duckdb"))
    try:
        _load_balances(connection)
    finally:
        connection.close()
    reference_db = duckdb.connect()
    try:
        _load_balances(reference_db)
        reference = reference_db.execute(
            "with opening as (select date_trunc('month', date_day)::date as period, balance, "
            "row_number() over (partition by account_id, date_trunc('month', date_day) "
            "order by date_day) as rn from balances) "
            "select period, sum(balance) from opening where rn = 1 group by period order by period"
        ).fetchall()
    finally:
        reference_db.close()
    assert reference == [(date(2026, 1, 1), 110), (date(2026, 2, 1), 100)]

    runtime = Runtime.from_path(str(pkg))
    try:
        balance = next(m.id for m in runtime.config.measures if "balance_usd" in m.id)
        role = next(r.id for r in runtime.config.temporal_roles)
        result = runtime.query(
            {
                "select": [{"expression": balance, "as": "v"}],
                "time": {"temporal_role": role, "grain": "month"},
            }
        )
    finally:
        runtime.close()
    assert result["ok"], result.get("errors")
    rows = sorted((date.fromisoformat(str(row["period"])[:10]), row["v"]) for row in result["rows"])
    assert rows == reference


def test_metric_ref_ambiguous_when_metric_and_measure_share_key(tmp_path: Path) -> None:
    # `shared_key` names both a measure and a metric: a reference to it is refused, not guessed.
    pkg = _write_synthetic_package(
        tmp_path / "pkg_ambiguous_metric_ref",
        models=_widget_models(shared_key=_count("widget_id"), denom=_count("widget_id")),
        metrics={
            "shared_key": {
                **_metric("aggregate", "count", measure="denom"),
                "as": "metric.synth.shared_key_top",
            },
            "ambiguous_consumer": _metric(
                "ratio", "percent", numerator="shared_key", denominator="denom"
            ),
        },
    )
    with pytest.raises(SemanticLayerError) as excinfo:
        load_package_config(str(pkg))
    assert excinfo.value.code == "INVALID_CONFIG"
    assert "ambiguous metric reference" in str(excinfo.value)
    assert "shared_key" in str(excinfo.value)


def test_times_block_consolidates_temporal_role_and_dimension(tmp_path: Path) -> None:
    # The `times:` key is the temporal role; its backing dimension comes from `column:`.
    times = {
        "ordered_at": {
            "label": "Order time",
            "column": "ordered_at",
            "kind": "timestamp",
            "class": "event_time",
            "supported_grains": ["day", "week", "month"],
            "default": True,
        },
    }
    pkg = _write_synthetic_package(
        tmp_path / "pkg_times_consolidation",
        graph_entities={"order": _ORDER_CUSTOMER["order"]},
        models={
            "orders": _model(
                "orders",
                {"order": {}},
                {"order_count": _count("order_id")},
                entity="order",
                relation="orders",
                times=times,
            )
        },
    )
    config = load_package_config(str(pkg))
    assert [r.id for r in config.temporal_roles] == ["temporal_role.synth_order_ordered_at"]
    backing = [d for d in config.dimensions if d.id == "dimension.synth_order_ordered_at"]
    assert [d.column for d in backing] == ["ordered_at"]


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
