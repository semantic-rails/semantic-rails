"""Relation specs, expression nodes, graph cardinality, the route policy's location and the
spec-file shapes each have one spelling; every retired spelling is refused at load."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.config_parts.shape_checks import _RELATION_RENAMED_KEYS
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.package_snapshot import json_fingerprint, load_package_snapshot
from semantic_rails.relation_pipelines import lower_relation
from semantic_rails.renderer import render_select
from semantic_rails.schema import PackageConfig
from semantic_rails.sql_ast import SqlField, SqlLiteral, SqlSelect
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


def _load_with(tmp_path: Path, **blocks) -> PackageConfig:
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    doc.update(blocks)
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    return load_package_config(str(source))


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


SOURCE = {"source": "shop_order"}
ON = [{"left": "customer_id", "right": "customer_id"}]
JOIN = {"relation": "shop_customer", "on": ON}
BRANCH = {"relation": "shop_order", "columns": {"order_id": "order_id"}}
ATTRIBUTION = {
    "base": "shop_customer",
    "attributed": "shop_order",
    "base_time": "first_ordered_at",
    "attributed_time": "ordered_at",
    "select": {"customer_id": "customer_id"},
}
# One row per retired spelling of a key a relation step's lowering reads, as (block, steps,
# retired key, kept key). Each loaded before, and the lowering read only one of the pair.
RETIRED_STEP_KEYS = [
    ("source", [{"source": {"table": "shop_order"}}], "table", "relation"),
    ("source", [{"source": {"name": "shop_order"}}], "name", "relation"),
    ("source", [{"source": {"value": "shop_order"}}], "value", "relation"),
    ("select", [SOURCE, {"select": {"value": {"order_id": "order_id"}}}], "value", "columns"),
    (
        "where",
        [SOURCE, {"where": {"predicates": ["channel = 'web'"], "where": ["order_id = 'x'"]}}],
        "where",
        "predicates",
    ),
    ("where", [SOURCE, {"where": {"value": ["channel = 'web'"]}}], "value", "predicates"),
    (
        "group_by",
        [SOURCE, {"group_by": {"group_by": {"channel": "channel"}}}],
        "group_by",
        "dimensions",
    ),
    ("group_by", [SOURCE, {"group_by": {"measures": {"n": "count"}}}], "measures", "aggregates"),
    (
        "aggregate",
        [SOURCE, {"group_by": {"aggregates": {"n": {"agg": "count"}}}}],
        "agg",
        "function",
    ),
    (
        "aggregate",
        [SOURCE, {"group_by": {"aggregates": {"n": {"function": "sum", "expression": "x"}}}}],
        "expression",
        "expr",
    ),
    ("join", [SOURCE, {"join": {"table": "shop_customer", "on": ON}}], "table", "relation"),
    (
        "join",
        [SOURCE, {"join": {**JOIN, "require_preaggregated": True}}],
        "require_preaggregated",
        "require_pre_aggregate",
    ),
    (
        "pre_aggregate side",
        [SOURCE, {"join": {**JOIN, "pre_aggregate": {"left": {"group_by": {"c": "c"}}}}}],
        "group_by",
        "dimensions",
    ),
    (
        "pre_aggregate side",
        [SOURCE, {"join": {**JOIN, "pre_aggregate": {"right": {"measures": {"n": "count"}}}}}],
        "measures",
        "aggregates",
    ),
    *(
        (kind, [SOURCE, {kind: {"table": "shop_customer", "on": ON}}], "table", "relation")
        for kind in ("semi_join", "anti_join", "exclude")
    ),
    (
        "date_lag",
        [SOURCE, {"join": {**JOIN, "on": [{**ON[0], "date_lag": {"unit": "day", "value": 7}}]}}],
        "value",
        "max",
    ),
    (
        "window",
        [SOURCE, {"window": {"columns": {"n": {"function": "count"}}}}],
        "columns",
        "windows",
    ),
    ("window spec", [SOURCE, {"window": {"n": {"kind": "row_number"}}}], "kind", "function"),
    (
        "window spec",
        [SOURCE, {"window": {"n": {"function": "sum", "expression": "x"}}}],
        "expression",
        "expr",
    ),
    (
        "order_by",
        [SOURCE, {"window": {"n": {"function": "row_number", "order_by": [{"column": "x"}]}}}],
        "column",
        "expr",
    ),
    ("explode", [SOURCE, {"explode": {"column": "channel", "alias": "c"}}], "alias", "as"),
    (
        "json_extract",
        [SOURCE, {"json_extract": {"column": "channel", "path": ["a"], "alias": "a"}}],
        "alias",
        "as",
    ),
    (
        "date_spine",
        [{"date_spine": {"start": "2026-01-01", "end": "2026-01-03", "date_column": "d"}}],
        "date_column",
        "column",
    ),
    (
        "state_as_of",
        [SOURCE, {"state_as_of": {"date_spine": "rel_days", "valid_from": "ordered_at"}}],
        "date_spine",
        "spine",
    ),
    (
        "attribution_join",
        [{"attribution_join": {**ATTRIBUTION, "base_relation": "shop_order"}}],
        "base_relation",
        "base",
    ),
    (
        "attribution_join",
        [{"attribution_join": {**ATTRIBUTION, "attributed_relation": "shop_customer"}}],
        "attributed_relation",
        "attributed",
    ),
    (
        "attribution key",
        [{"attribution_join": {**ATTRIBUTION, "keys": [{"left": "customer_id"}]}}],
        "left",
        "base",
    ),
    (
        "attribution key",
        [{"attribution_join": {**ATTRIBUTION, "keys": [{"right": "customer_id"}]}}],
        "right",
        "attributed",
    ),
    (
        "lookback",
        [{"attribution_join": {**ATTRIBUTION, "lookback": {"unit": "day", "max": 7}}}],
        "max",
        "value",
    ),
    ("union_all", [{"union_all": {"value": [BRANCH]}}], "value", "branches"),
    *(
        ("union branch", [{"union_all": [{**BRANCH, key: "shop_order"}]}], key, "relation")
        for key in ("table", "source")
    ),
    (
        "union branch",
        [{"union_all": {"branches": [{"relation": "shop_order", "select": {"a": "a"}}]}}],
        "select",
        "columns",
    ),
]


@pytest.mark.parametrize(
    ("block", "steps", "retired", "kept"),
    RETIRED_STEP_KEYS,
    ids=[f"{block}-{retired}" for block, _, retired, _ in RETIRED_STEP_KEYS],
)
def test_second_relation_step_spellings_are_refused(tmp_path, block, steps, retired, kept):
    with pytest.raises(SemanticLayerError) as exc:
        _load_with(tmp_path, relations={"recent": {"steps": steps, "columns": ["order_id"]}})
    assert exc.value.code == "INVALID_CONFIG"
    assert f"unknown key '{retired}'" in str(exc.value)
    assert f"write `{kept}:`" in str(exc.value)


def test_every_retired_relation_step_spelling_has_a_refusal_row():
    rows = {(block, retired, kept) for block, _, retired, kept in RETIRED_STEP_KEYS}
    assert rows == {
        (block, retired, kept)
        for block, renamed in _RELATION_RENAMED_KEYS.items()
        for retired, kept in renamed.items()
    }


def _relation_sql(config: PackageConfig) -> str:
    (relation,) = config.relations
    ctes, _ = lower_relation(relation, warehouse=config.package.warehouse)
    return render_select(SqlSelect(select=[SqlField(SqlLiteral(1), "one")], ctes=ctes))


def test_an_annotation_beside_a_step_kind_changes_nothing(tmp_path):
    select = {"select": {"columns": {"order_id": "order_id"}}}
    plain = _load_with(tmp_path / "plain", relations={"recent": {"steps": [SOURCE, select]}})
    noted = _load_with(
        tmp_path / "noted",
        relations={"recent": {"steps": [{**SOURCE, "_note": "x"}, {"_why": "y", **select}]}},
    )
    assert _relation_sql(noted) == _relation_sql(plain)
    assert "FROM shop_order AS src" in _relation_sql(plain)


@pytest.mark.parametrize("step", [{}, {"_note": "x"}], ids=["empty", "annotation-only"])
def test_a_step_naming_no_kind_is_refused(tmp_path, step):
    with pytest.raises(SemanticLayerError) as exc:
        _load_with(tmp_path, relations={"recent": {"steps": [SOURCE, step]}})
    assert exc.value.code == "INVALID_CONFIG"
    assert "step 1 names no kind; write each step as one key naming its kind" in str(exc.value)


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
