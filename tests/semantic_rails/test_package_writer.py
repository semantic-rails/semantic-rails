from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_snapshot
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.interop.package_writer import write_package
from semantic_rails.meta_contract import load_meta_contract
from semantic_rails.operational import load_operational_contract
from semantic_rails.schema import AggregateRelationConfig, RelationConfig

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = [
    ROOT / "configs/semantic_rails/jaffle_shop",
    ROOT / "configs/semantic_rails/tpch_sf1_showcase",
    ROOT / "comparisons/semantic_layers/semantic_rails/package",
    ROOT / "configs/examples/semantic_rails_package_starter.yml",
]
STARTER = load_package_snapshot(PACKAGES[-1])


@pytest.mark.parametrize("package", PACKAGES, ids=lambda path: path.stem or path.name)
def test_written_package_loads_back_identical_and_validates_alike(package, tmp_path) -> None:
    snapshot = load_package_snapshot(package)
    directory = tmp_path / snapshot.config.package.package_id
    namespace = snapshot.normalized["package"]["namespace"]
    assert write_package(snapshot.config, directory, namespace=namespace) == directory
    assert load_package_snapshot(directory).semantic == snapshot.semantic
    # The written directory passes the same validation as its source: the comparison package
    # carries one pre-existing error (a date dimension without a temporal role), kept as is.
    original = [e.replace(str(package), "<pkg>") for e in validate_runtime_package(package)]
    written = [e.replace(str(directory), "<pkg>") for e in validate_runtime_package(directory)]
    assert written == original


def test_contracts_that_measure_payloads_follow_are_written(tmp_path) -> None:
    config = STARTER.config
    operational = {"operational": {"measure": {"fields": {"owner": {"type": "string"}}}}}
    meta = {"meta": {"measure": {"fields": {"owner_team": {"type": "string"}}}}}
    config = replace(
        config,
        operational_contract=load_operational_contract(operational, path="test"),
        meta_contract=load_meta_contract(meta, path="test"),
        measures=[
            replace(config.measures[0], operational={"owner": "sales"}),
            *config.measures[1:],
        ],
    )
    directory = write_package(config, tmp_path / "shop_starter", namespace="shop")
    loaded = load_package_snapshot(directory).config
    assert (loaded.operational_contract, loaded.meta_contract) == (
        config.operational_contract,
        config.meta_contract,
    )


def _relation_pipeline(config):
    customer = replace(config.entities[0], relation_id="relation.customers")
    relation = RelationConfig(id="relation.customers", output_name="customers_rel")
    return {"entities": [customer, *config.entities[1:]], "relations": [relation]}


@pytest.mark.parametrize(
    ("changes", "refused"),
    [
        (
            lambda c: {"aggregate_relations": [AggregateRelationConfig("agg.daily", "t", "e")]},
            ["aggregate_relations agg.daily"],
        ),
        # The loader keeps one join per entity pair, so the first of two is lost.
        (
            lambda c: {"relationships": [*c.relationships, replace(c.relationships[0], id="r.2")]},
            [f"relationships {STARTER.config.relationships[0].id}"],
        ),
        (_relation_pipeline, ["entities entity.shop_customer", "relations relation.customers"]),
        # An entity id the loader can't derive from a graph key is written with `as:`, but the
        # loader derives its key dimension from the key, so that comes back under another id.
        ("as: entity.crm_customer", ["dimensions dimension.shop_customer_id"]),
    ],
)
def test_what_does_not_come_back_is_refused_by_name(changes, refused, tmp_path) -> None:
    if isinstance(changes, str):  # an authoring change, loaded the way the loader reads it
        raw = yaml.safe_load(PACKAGES[-1].read_text(encoding="utf-8"))
        raw["graph"]["entities"]["customer"].update(yaml.safe_load(changes))
        (tmp_path / "source.yml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        config = load_package_snapshot(tmp_path / "source.yml").config
    else:
        config = replace(STARTER.config, **changes(STARTER.config))
    with pytest.raises(SemanticLayerError) as caught:
        write_package(config, tmp_path / "shop_starter", namespace="shop")
    assert set(refused) <= set(caught.value.details["objects"]), caught.value.details
    assert not (tmp_path / "shop_starter").exists()


def test_a_partial_config_keeps_the_loader_defaults_when_not_exact(tmp_path) -> None:
    config = replace(
        STARTER.config, measures=[replace(m, topics=[]) for m in STARTER.config.measures]
    )
    with pytest.raises(SemanticLayerError):
        write_package(config, tmp_path / "exact", namespace="shop")
    directory = write_package(config, tmp_path / "defaults", namespace="shop", exact=False)
    assert load_package_snapshot(directory).semantic == STARTER.semantic


def test_write_package_refuses_an_existing_directory(tmp_path) -> None:
    with pytest.raises(FileExistsError):
        write_package(STARTER.config, tmp_path, namespace="shop")
