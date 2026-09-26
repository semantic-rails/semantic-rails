from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from semantic_rails.config import load_package_snapshot
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.interop.package_writer import package_documents, write_package
from semantic_rails.package_snapshot import semantic_payload
from semantic_rails.schema import AggregateRelationConfig

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = [
    ROOT / "configs/semantic_rails/jaffle_shop",
    ROOT / "configs/semantic_rails/tpch_sf1_showcase",
    ROOT / "comparisons/semantic_layers/semantic_rails/package",
    ROOT / "configs/examples/semantic_rails_package_starter.yml",
]


def _written(package: Path, tmp_path: Path):
    snapshot = load_package_snapshot(package)
    namespace = snapshot.normalized["package"]["namespace"]
    documents, unwritten = package_documents(snapshot.config, namespace=namespace)
    directory = write_package(documents, tmp_path / snapshot.config.package.package_id)
    return snapshot, directory, unwritten


@pytest.mark.parametrize("package", PACKAGES, ids=lambda path: path.stem or path.name)
def test_written_package_loads_back_identical_and_validates_alike(package, tmp_path) -> None:
    snapshot, directory, unwritten = _written(package, tmp_path)
    assert unwritten == {}
    assert semantic_payload(load_package_snapshot(directory).config) == snapshot.semantic
    # The written directory passes the same validation as its source: the comparison package
    # carries one pre-existing error (a date dimension without a temporal role), kept as is.
    original = [e.replace(str(package), "<pkg>") for e in validate_runtime_package(package)]
    written = [e.replace(str(directory), "<pkg>") for e in validate_runtime_package(directory)]
    assert written == original


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            lambda c: {"aggregate_relations": [AggregateRelationConfig("agg.daily", "t", "e")]},
            {"aggregate_relations": ["agg.daily"]},
        ),
        # The loader keeps one join per entity pair, so a second one can't be written.
        (
            lambda c: {"relationships": [*c.relationships, replace(c.relationships[0], id="r.2")]},
            {"relationships": ["r.2"]},
        ),
        # An entity built by a relation pipeline isn't written, nor is anything on it.
        (
            lambda c: {
                "entities": [replace(c.entities[0], relation_id="relation.x"), *c.entities[1:]]
            },
            {
                "entities": ["entity.shop_customer"],
                "dimensions": [
                    "dimension.shop_customer_customer_type",
                    "dimension.shop_customer_id",
                    "dimension.shop_customer_first_ordered_at",
                ],
                "relationships": ["relationship.orders_customer"],
            },
        ),
    ],
)
def test_objects_the_writer_cannot_write_are_named(changes, expected) -> None:
    config = load_package_snapshot(PACKAGES[-1]).config
    _documents, unwritten = package_documents(replace(config, **changes(config)), namespace="shop")
    assert unwritten == expected


def test_write_package_refuses_an_existing_directory(tmp_path) -> None:
    with pytest.raises(FileExistsError):
        write_package({"package.yml": {}}, tmp_path)
