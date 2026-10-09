"""Relation specs, expression nodes, graph cardinality, the route policy's location and the
spec-file shapes each have one spelling; every retired spelling is refused at load."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.package_snapshot import json_fingerprint, load_package_snapshot
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import copy_package_config, write_single_file_package

# Semantic fingerprints of the bundled packages before the retired spellings were refused.
FINGERPRINTS = {
    "configs/semantic_rails/jaffle_shop": (
        "sha256:8d436d8ae0bb440f5324bae2c99fe99f5a0b11ea56069c27ba420234cc77149a"
    ),
    "configs/semantic_rails/tpch_sf1_showcase": (
        "sha256:1411788e25b3b8136edf871151cfac1ad903bbd9d1096f7b9434db38d07ed5a1"
    ),
    "comparisons/semantic_layers/semantic_rails/package": (
        "sha256:4580d1a69a70b25078c207bc3aad9ec478730725a4e300dd72a23a23ec266478"
    ),
    "tests/integration/correctness/shop": (
        "sha256:e0efc0e78506590c1fa56ae062541ad26cc07b2ce17120520c2928534aa88e42"
    ),
    "configs/examples/semantic_rails_package_starter.yml": (
        "sha256:200c1a2cb8f72112f9a6453d501724c89a06061f3f0eafbf9def0c3f9a805290"
    ),
}


@pytest.mark.parametrize(("path", "fingerprint"), FINGERPRINTS.items())
def test_bundled_packages_keep_their_semantics(path, fingerprint):
    semantic = deepcopy(load_package_snapshot(resolve_repo_path(path)).semantic)
    # Rollup exclusions are collected in a set, so their order varies between processes.
    for row in semantic.get("aggregate_relations", []):
        row["excluded_entities"] = sorted(row["excluded_entities"])
    assert json_fingerprint(semantic) == fingerprint


COLUMN = {"kind": "column", "column": "channel"}
REVENUE = {"kind": "measure", "measure": "measure.shop.revenue_usd"}


@pytest.mark.parametrize(
    ("expression", "code", "message"),
    [
        (
            {"kind": "binary", "op": "add", "left": REVENUE, "right": REVENUE},
            "INVALID_EXPRESSION_AST",
            "Write kind 'arithmetic'",
        ),
        (
            {"kind": "measure_ref", "measure": "measure.shop.revenue_usd"},
            "INVALID_EXPRESSION_AST",
            "Write kind 'measure'",
        ),
        ({"kind": "in", "left": COLUMN, "values": ["web"]}, "INVALID_EXPRESSION_KEY", "['left']"),
        (
            {"kind": "not_in", "left": COLUMN, "values": ["web"]},
            "INVALID_EXPRESSION_KEY",
            "['left']",
        ),
        (
            {
                "kind": "conversion",
                "base": REVENUE,
                "converted": REVENUE,
                "entity": "entity.shop_customer",
                "window": {"unit": "day", "value": 7},
                "matching": "first_converted_after_base",
            },
            "INVALID_EXPRESSION_KEY",
            "['matching']",
        ),
    ],
    ids=["binary", "measure_ref", "in-left", "not_in-left", "conversion-matching"],
)
def test_retired_expression_spellings_are_refused(expression, code, message):
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(expression, context="metric", path="expression")
    assert exc.value.code == code
    assert message in str(exc.value)


def _load_with(tmp_path: Path, **blocks) -> None:
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    doc.update(blocks)
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    load_package_config(str(source))


STEPS = [{"source": "shop_order"}, {"select": {"columns": {"order_id": "order_id"}}}]
RELATION = {"steps": STEPS, "output_name": "rel_recent_orders", "columns": ["order_id"]}


@pytest.mark.parametrize(
    ("relations", "message"),
    [
        ([{"id": "recent", **RELATION}], "relations must be a mapping keyed by relation"),
        (
            {"recent": {"steps": [{"kind": "source", "config": {"relation": "shop_order"}}]}},
            "unknown key 'kind'",
        ),
        ({"recent": {"steps": ["shop_order"]}}, "must be one key naming its kind"),
        (
            {"recent": {"steps": [*STEPS, {"unnest": {"column": "channel", "as": "c"}}]}},
            "write `explode:`",
        ),
        ({"recent": {**RELATION, "cte": "rel_recent"}}, "write `output_name:`"),
        ({"recent": {"steps": STEPS, "output_columns": ["order_id"]}}, "write `columns:`"),
        ({"recent": {"source": "shop_order"}}, "write it as the first step"),
        (
            {"days": {"date_spine": {"start": "2026-01-01", "end": "2026-01-03"}}},
            "write it as a step",
        ),
        (
            {"recent": {"steps": [*STEPS, {"explode": {"column": "channel"}, "as": "c"}]}},
            "step 2 has unknown key 'as'",
        ),
        (
            {"recent": {"steps": [{"source": {"relation": "shop_order", "colums": ["a"]}}]}},
            "did you mean 'columns'",
        ),
        ({"recent": {**RELATION, "colums": ["order_id"]}}, "did you mean 'columns'"),
    ],
    ids=[
        "list",
        "kind-config-step",
        "bare-string-step",
        "unnest",
        "cte",
        "output_columns",
        "source-shorthand",
        "date_spine-shorthand",
        "key-beside-step-kind",
        "unread-step-key",
        "unread-relation-key",
    ],
)
def test_retired_relation_spellings_are_refused(tmp_path, relations, message):
    with pytest.raises(SemanticLayerError, match="unknown|relation") as exc:
        _load_with(tmp_path, relations=relations)
    assert exc.value.code == "INVALID_CONFIG"
    assert message in str(exc.value)


def test_relation_steps_and_graph_route_policy_load(tmp_path):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    doc["relations"] = {
        "recent": RELATION,
        "days": {"steps": [{"date_spine": {"start": "2026-01-01", "end": "2026-01-03"}}]},
        "columns": {"steps": [{"source": "shop_order"}, {"select": {"order_id": "order_id"}}]},
    }
    doc["graph"]["path_policy"] = {"max_hops": 3}
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    config = load_package_config(str(source))
    assert [row.output_name for row in config.relations] == [
        "rel_recent_orders",
        "rel_shop_days",
        "rel_shop_columns",
    ]
    assert config.path_policy.max_hops == 3


@pytest.mark.parametrize(
    ("key", "value"), [("path_policy", {"max_hops": 3}), ("path_preferences", [])]
)
def test_route_policy_outside_graph_is_refused(tmp_path, key, value):
    with pytest.raises(SemanticLayerError, match="write it under `graph:`") as exc:
        _load_with(tmp_path, **{key: value})
    assert f"unknown key '{key}'" in str(exc.value)


@pytest.mark.parametrize("cardinality", ["N:1", "1:N", "1:1", "M:N", "Many_To_One"])
def test_symbolic_cardinality_is_refused(tmp_path, cardinality):
    package = copy_package_config(tmp_path, "jaffle_shop")
    graph = safe_load((package / "graph.yml").read_bytes())
    next(iter(graph["graph"]["relationships"].values()))["cardinality"] = cardinality
    (package / "graph.yml").write_text(yaml.safe_dump(graph, sort_keys=False))
    with pytest.raises(SemanticLayerError, match="must be one of many_to_one") as exc:
        load_package_config(str(package))
    assert f"cardinality {cardinality!r}" in str(exc.value)


def _reshape(package: Path, block: str, shape: str) -> None:
    """Rewrite one object of ``block`` into a retired file shape, in a file of its own."""
    if block == "relations":
        key, spec = "recent", {"steps": [{"source": "jaffle_order"}]}
    elif block == "models":
        path = package / "models/core/orders.yml"
        key, spec = "orders", safe_load(path.read_bytes())["model"]
        path.unlink()
    else:
        path = next(iter(sorted((package / block).rglob("*.yml"))))
        doc = safe_load(path.read_bytes())
        key, spec = next(iter(doc[block].items()))
        del doc[block][key]
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
    wrapper = {"models": "models"}.get(block, block[:-1])
    body = {wrapper: {key: spec} if block == "models" else spec} if shape == "wrapped" else spec
    target = package / block / f"{key}.yml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(yaml.safe_dump(body, sort_keys=False))


@pytest.mark.parametrize("shape", ["wrapped", "bare"])
@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("models", "must hold one model under `model:`"),
        ("metrics", "must hold a `metrics:` map"),
        ("segments", "must hold a `segments:` map"),
        ("relations", "must hold a `relations:` map"),
    ],
)
def test_retired_spec_file_shapes_are_refused(tmp_path, block, expected, shape):
    package = copy_package_config(tmp_path, "jaffle_shop")
    _reshape(package, block, shape)
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package))
    assert exc.value.code == "INVALID_CONFIG"
    assert expected in str(exc.value)
