"""A relationship infers its cardinality only from keys.

Without an authored ``cardinality:``, a relationship is many-to-one only when its ``target:``
columns cover the target entity's key, and its source side holds one row per value only when
its ``via:`` columns cover the source entity's key. Any other target could repeat a target row
and double a measure, so loading refuses it rather than guessing; a ``via:`` on part of a
composite key is an ordinary foreign key. An authored ``cardinality:`` (or
``target_key_role:``) is the author's statement and loads as written.

Fixture: parents 1 and 2 share category x and region east, parent 3 is y/west; children 10 and
11 are x (weights 5 and 7), child 12 is y (100). Joined on category, the east children weigh
12, but every x child matches two parents, so a many-to-one reading reports 24. Order 1 (100)
has lines A, A and B, order 2 (50) one line A; the order amount over lines with sku A is 150
(orders 1 and 2), and a one-to-one reading of lines to orders reports 250.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE parents (id INTEGER, category VARCHAR, region VARCHAR);
INSERT INTO parents VALUES (1, 'x', 'east'), (2, 'x', 'east'), (3, 'y', 'west');
CREATE TABLE children (child_id INTEGER, category VARCHAR, weight INTEGER);
INSERT INTO children VALUES (10, 'x', 5), (11, 'x', 7), (12, 'y', 100);
CREATE TABLE ords (order_id INTEGER, amount INTEGER, status VARCHAR);
INSERT INTO ords VALUES (1, 100, 'open'), (2, 50, 'open');
CREATE TABLE lines (order_id INTEGER, line_no INTEGER, sku VARCHAR, qty INTEGER);
INSERT INTO lines VALUES (1, 1, 'A', 1), (1, 2, 'A', 1), (1, 3, 'B', 1), (2, 1, 'A', 1);
"""
PACKAGE = """
schema_version: 1
package: {id: nk, namespace: nk, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}}
"""
MODELS = {
    "children": """
model:
  id: children
  relation: children
  entities: {child: {}}
  measures:
    weight: {label: Weight, kind: aggregate, expr: weight, accumulation: {kind: flow}}
""",
    "parents": """
model:
  id: parents
  relation: parents
  entities: {parent: {}}
  dimensions:
    region: {label: Region, kind: categorical}
""",
    "lines": """
model:
  id: lines
  relation: lines
  entities: {line: {}}
  dimensions:
    sku: {label: SKU, kind: categorical}
  measures:
    qty: {label: Quantity, kind: aggregate, expr: qty, accumulation: {kind: flow}}
""",
    "ords": """
model:
  id: ords
  relation: ords
  entities: {ord: {}}
  dimensions:
    status: {label: Status, kind: categorical}
  measures:
    amount: {label: Amount, kind: aggregate, expr: amount, accumulation: {kind: flow}}
""",
}
ON_CATEGORY = {"entities": ["child", "parent"], "via": ["category"], "target": ["category"]}
LINE_TO_ORDER = {"entities": ["line", "ord"], "via": ["order_id"]}
WEIGHT_BY_REGION = {"measure": "measure.nk.weight", "by": "dimension.nk_parent_region"}
AMOUNT_BY_SKU = {"measure": "measure.nk.amount", "by": "dimension.nk_line_sku"}


def _package(root: Path, relationship: dict[str, Any], parent_key: list[str]) -> Path:
    graph = {
        "graph": {
            "entities": {
                "child": {"label": "Child", "key": ["child_id"], "model": "children"},
                "parent": {"label": "Parent", "key": parent_key, "model": "parents"},
                "line": {"label": "Line", "key": ["order_id", "line_no"], "model": "lines"},
                "ord": {"label": "Order", "key": ["order_id"], "model": "ords"},
            },
            "relationships": {"probe": {"id": "relationship.probe", **relationship}},
        }
    }
    files = {
        "package.yml": PACKAGE,
        "data/seed.sql": SEED,
        "graph.yml": yaml.safe_dump(graph),
        **{f"models/{name}.yml": text for name, text in MODELS.items()},
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def _probe(root: Path) -> Any:
    config = load_package_config(str(root))
    return next(rel for rel in config.relationships if rel.id == "relationship.probe")


def _totals(root: Path, ask: dict[str, str]) -> dict[str, Any]:
    """The measure grouped by the dimension, or the refusal's code."""
    runtime = Runtime.from_path(str(root))
    try:
        result = runtime.query({"select": [ask["measure"]], "group_by": [ask["by"]]})
    except SemanticLayerError as exc:
        return {"refused": exc.code}
    finally:
        runtime.close()
    return {
        row[ask["by"]]: next(v for k, v in row.items() if k != ask["by"]) for row in result["rows"]
    }


@pytest.mark.parametrize(
    ("parent_key", "key_text"),
    [(["id"], "[id]"), (["id", "category"], "[id, category]")],
    ids=["off_the_key", "part_of_a_composite_key"],
)
def test_a_target_off_the_key_is_refused_without_a_cardinality(
    tmp_path: Path, parent_key: list[str], key_text: str
) -> None:
    root = _package(tmp_path, ON_CATEGORY, parent_key)
    with pytest.raises(SemanticLayerError) as caught:
        load_package_config(str(root))
    assert caught.value.code == "INVALID_CONFIG"
    assert (
        f"join 'children.parent' joins parent on [category], not its key {key_text}: join on "
        "the key, or declare `cardinality:` (many_to_many when the values repeat)"
    ) in str(caught.value)


def test_an_authored_many_to_many_off_the_key_loads_and_never_doubles(tmp_path: Path) -> None:
    root = _package(tmp_path, {**ON_CATEGORY, "cardinality": "many_to_many"}, ["id"])
    relationship = _probe(root)
    assert (relationship.cardinality, relationship.safety) == ("M:N", "requires_rewrite")
    assert _totals(root, WEIGHT_BY_REGION) == {"refused": "MIXED_GRAIN_INVALID"}


@pytest.mark.parametrize(
    "authored",
    [{"cardinality": "many_to_one"}, {"target_key_role": "unique"}],
    ids=["cardinality", "target_key_role"],
)
def test_an_authored_many_to_one_off_the_key_loads_as_written(
    tmp_path: Path, authored: dict[str, str]
) -> None:
    """An alternate key the author vouches for, like a coupon's code."""
    relationship = _probe(_package(tmp_path, {**ON_CATEGORY, **authored}, ["id"]))
    assert (relationship.cardinality, relationship.safety) == ("N:1", "safe")


def test_a_join_on_the_declared_key_is_trusted(tmp_path: Path) -> None:
    """Joining on the declared key infers many-to-one; whether the data holds to it is the
    package's key test, not the loader's."""
    relationship = _probe(
        _package(tmp_path, {"entities": ["child", "parent"], "via": ["category"]}, ["category"])
    )
    assert relationship.target_columns == ["category"]
    assert (relationship.cardinality, relationship.safety) == ("N:1", "safe")


def test_a_via_on_part_of_a_composite_source_key_is_a_foreign_key(tmp_path: Path) -> None:
    root = _package(tmp_path, LINE_TO_ORDER, ["id"])
    relationship = _probe(root)
    assert (relationship.source_key_role, relationship.cardinality) == ("foreign", "N:1")
    # Orders over their lines is a one-to-many hop: refused here, never 250 for sku A.
    assert _totals(root, AMOUNT_BY_SKU) == {"refused": "MIXED_GRAIN_INVALID"}
    assert _totals(root, {"measure": "measure.nk.qty", "by": "dimension.nk_ord_status"}) == {
        "open": 4
    }


def test_a_via_covering_both_keys_stays_one_to_one(tmp_path: Path) -> None:
    relationship = _probe(
        _package(tmp_path, {"entities": ["child", "parent"], "via": ["child_id"]}, ["id"])
    )
    assert (relationship.source_key_role, relationship.cardinality) == ("primary", "1:1")
