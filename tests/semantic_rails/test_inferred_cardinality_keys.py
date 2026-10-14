"""A relationship infers its cardinality only from keys.

Without an authored ``cardinality:``, a side of a relationship is unique only when its columns
hold every column of its entity's key, and that key is declared ``primary`` (the default) or
``unique``. Columns compare as the warehouse resolves them: in any order, with extra columns
allowed, and an unquoted name in any case. A ``target:`` off the target's key could repeat a
target row and double a measure, so loading refuses it rather than guessing; a ``via:`` on part
of a composite key, or on a key declared ``natural`` or ``foreign``, is an ordinary foreign key.
An authored ``cardinality:`` (or ``target_key_role:``) is the author's statement and loads as
written.

Fixture: parents 1 and 2 share category x and region east, parent 3 is y/west; children 1 and 2
are x (weights 5 and 7), child 3 is y (100). Joined on category, the east children weigh 12,
but every x child matches two parents, so a many-to-one reading reports 24; joined child to
parent on their ids, east weighs 12 and west 100, and the parent ids over the x children sum
to 3 (parents 1 and 2) and over y to 3. Order 1 (100) has lines A, A and B, order 2 (50) one
line A; the order amount over lines with sku A is 150 (orders 1 and 2), and a one-to-one
reading of lines to orders reports 250.
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
INSERT INTO children VALUES (1, 'x', 5), (2, 'x', 7), (3, 'y', 100);
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
  dimensions:
    category: {label: Child category, kind: categorical}
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
  measures:
    parent_ids: {label: Parent ids, kind: aggregate, expr: id, accumulation: {kind: flow}}
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
KEYS: dict[str, Any] = {
    "child": ["child_id"],
    "parent": ["id"],
    "line": ["order_id", "line_no"],
    "ord": ["order_id"],
}
HOMES = {"child": "children", "parent": "parents", "line": "lines", "ord": "ords"}
ON_CATEGORY = {"entities": ["child", "parent"], "via": ["category"], "target": ["category"]}
LINE_TO_ORDER = {"entities": ["line", "ord"], "via": ["order_id"]}
WEIGHT_BY_REGION = {"measure": "measure.nk.weight", "by": "dimension.nk_parent_region"}
PARENT_IDS_BY_CATEGORY = {"measure": "measure.nk.parent_ids", "by": "dimension.nk_child_category"}
AMOUNT_BY_SKU = {"measure": "measure.nk.amount", "by": "dimension.nk_line_sku"}
QTY_BY_STATUS = {"measure": "measure.nk.qty", "by": "dimension.nk_ord_status"}
REFUSED = {"refused": "MIXED_GRAIN_INVALID"}


def _package(root: Path, relationship: dict[str, Any], **keys: Any) -> Path:
    graph = {
        "graph": {
            "entities": {
                name: {"label": name, "key": key, "model": HOMES[name]}
                for name, key in {**KEYS, **keys}.items()
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
    ("relationship", "parent_key", "message"),
    [
        (ON_CATEGORY, ["id"], "on [category], not its key [id]"),
        (ON_CATEGORY, ["id", "category"], "on [category], not its key [id, category]"),
        (
            {**ON_CATEGORY, "via": ["category", "category"], "target": ["category", "category"]},
            ["id", "category"],
            "on [category, category], not its key [id, category]",
        ),
    ],
    ids=["off_the_key", "part_of_a_composite_key", "a_repeated_column"],
)
def test_a_target_off_the_key_is_refused_without_a_cardinality(
    tmp_path: Path, relationship: dict[str, Any], parent_key: list[str], message: str
) -> None:
    root = _package(tmp_path, relationship, parent=parent_key)
    with pytest.raises(SemanticLayerError) as caught:
        load_package_config(str(root))
    assert caught.value.code == "INVALID_CONFIG"
    assert (
        f"join 'children.parent' joins parent {message}: join on the key, or declare "
        "`cardinality:` (many_to_many when the values repeat)"
    ) in str(caught.value)


@pytest.mark.parametrize(
    ("relationship", "keys", "cardinality", "ask", "totals"),
    [
        (
            {"entities": ["child", "parent"], "via": ["child_id"], "target": ["ID"]},
            {},
            "1:1",
            WEIGHT_BY_REGION,
            {"east": 12, "west": 100},
        ),
        (
            {"entities": ["line", "ord"], "via": ["order_id"], "target": ["ORDER_ID"]},
            {},
            "N:1",
            QTY_BY_STATUS,
            {"open": 4},
        ),
        (
            {"entities": ["child", "parent"], "via": ["CHILD_ID"], "target": ["id"]},
            {},
            "1:1",
            WEIGHT_BY_REGION,
            {"east": 12, "west": 100},
        ),
        (
            {
                "entities": ["child", "parent"],
                "via": ["category", "child_id"],
                "target": ["category", "id"],
            },
            {"parent": ["id", "category"]},
            "1:1",
            WEIGHT_BY_REGION,
            {"east": 12, "west": 100},
        ),
        (
            {
                "entities": ["child", "parent"],
                "via": ["child_id", "child_id"],
                "target": ["id", "id"],
            },
            {},
            "1:1",
            WEIGHT_BY_REGION,
            {"east": 12, "west": 100},
        ),
    ],
    ids=[
        "target_in_another_case",
        "many_to_one_in_another_case",
        "via_in_another_case",
        "composite_key_in_another_order",
        "a_repeated_key_column",
    ],
)
def test_a_join_on_the_key_loads_however_its_columns_are_spelled(
    tmp_path: Path,
    relationship: dict[str, Any],
    keys: dict[str, Any],
    cardinality: str,
    ask: dict[str, str],
    totals: dict[str, Any],
) -> None:
    root = _package(tmp_path, relationship, **keys)
    assert _probe(root).cardinality == cardinality
    assert _totals(root, ask) == totals


def test_columns_holding_the_whole_key_and_more_are_still_unique(tmp_path: Path) -> None:
    """Each side holds its entity's whole key, so the join is one-to-one and the parents'
    measure groups by a child's column."""
    relationship = {
        "entities": ["child", "parent"],
        "via": ["child_id", "category"],
        "target": ["id", "category"],
    }
    root = _package(tmp_path, relationship)
    probe = _probe(root)
    assert (probe.source_key_role, probe.target_key_role, probe.cardinality) == (
        "primary",
        "primary",
        "1:1",
    )
    assert _totals(root, PARENT_IDS_BY_CATEGORY) == {"x": 3, "y": 3}


@pytest.mark.parametrize("role", ["natural", "foreign"])
def test_a_source_key_declared_non_unique_is_never_promoted(tmp_path: Path, role: str) -> None:
    """A via on a key declared without uniqueness is a foreign key: the order amount over lines
    is refused, never 250 for sku A."""
    root = _package(tmp_path, LINE_TO_ORDER, line={"columns": ["order_id"], "role": role})
    relationship = _probe(root)
    assert (relationship.source_key_role, relationship.cardinality) == ("foreign", "N:1")
    assert _totals(root, AMOUNT_BY_SKU) == REFUSED
    assert _totals(root, QTY_BY_STATUS) == {"open": 4}


def test_an_authored_many_to_many_off_the_key_loads_and_never_doubles(tmp_path: Path) -> None:
    root = _package(tmp_path, {**ON_CATEGORY, "cardinality": "many_to_many"})
    relationship = _probe(root)
    assert (relationship.cardinality, relationship.safety) == ("M:N", "requires_rewrite")
    assert _totals(root, WEIGHT_BY_REGION) == REFUSED


@pytest.mark.parametrize(
    "authored",
    [{"cardinality": "many_to_one"}, {"target_key_role": "unique"}],
    ids=["cardinality", "target_key_role"],
)
def test_an_authored_many_to_one_off_the_key_loads_as_written(
    tmp_path: Path, authored: dict[str, str]
) -> None:
    """An alternate key the author vouches for, like a coupon's code."""
    relationship = _probe(_package(tmp_path, {**ON_CATEGORY, **authored}))
    assert (relationship.cardinality, relationship.safety) == ("N:1", "safe")


def test_a_join_on_the_declared_key_is_trusted(tmp_path: Path) -> None:
    """Joining on the declared key infers many-to-one; whether the data holds to it is the
    package's key test, not the loader's."""
    relationship = _probe(
        _package(
            tmp_path, {"entities": ["child", "parent"], "via": ["category"]}, parent=["category"]
        )
    )
    assert relationship.target_columns == ["category"]
    assert (relationship.cardinality, relationship.safety) == ("N:1", "safe")


def test_a_via_on_part_of_a_composite_source_key_is_a_foreign_key(tmp_path: Path) -> None:
    root = _package(tmp_path, LINE_TO_ORDER)
    relationship = _probe(root)
    assert (relationship.source_key_role, relationship.cardinality) == ("foreign", "N:1")
    # Orders over their lines is a one-to-many hop: refused here, never 250 for sku A.
    assert _totals(root, AMOUNT_BY_SKU) == REFUSED
    assert _totals(root, QTY_BY_STATUS) == {"open": 4}


def test_a_via_covering_both_keys_stays_one_to_one(tmp_path: Path) -> None:
    relationship = _probe(
        _package(tmp_path, {"entities": ["child", "parent"], "via": ["child_id"]})
    )
    assert (relationship.source_key_role, relationship.cardinality) == ("primary", "1:1")
