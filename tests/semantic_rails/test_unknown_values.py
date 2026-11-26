"""A dimension's declared unknown spellings read as NULL everywhere the dimension is read.

The source writes an unknown country as '' or 'XX' as well as NULL. Declaring
``unknown_values: ['', 'XX']`` makes grouping, ``missing``, ``not`` and the unknown-rows note
treat all three alike. Gold values come from plain SQL over the seed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE downloads (download_id INTEGER, country VARCHAR, installs INTEGER);
INSERT INTO downloads VALUES
  (1, 'US', 10), (2, 'DE', 4), (3, '', 2), (4, 'XX', 1), (5, NULL, 3), (6, 'US', 5);
"""
COUNTRY = "dimension.dl_download_country"
INSTALLS = "measure.dl.installs"
KNOWN = "CASE WHEN country IN ('', 'XX') THEN NULL ELSE country END"


def _package(
    root: Path,
    country: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    others: dict[str, Any] | None = None,
) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "dl", "namespace": "dl", "name": "dl", "description": "Downloads",
                    "warehouse": "duckdb", "default_db": "dl.duckdb", "seed": {"kind": "external"}},
        **({"defaults": defaults} if defaults else {}),
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "download": {"key": ["download_id"], "model": "downloads"},
    }}})  # fmt: skip
    put("models/downloads.yml", {"model": {
        "id": "downloads", "relation": "downloads", "entities": {"download": {}},
        "dimensions": {"country": {"kind": "categorical", **country}, **(others or {})},
        "measures": {"installs": {"kind": "aggregate", "expr": "installs", "value_type": "count"}},
    }})  # fmt: skip
    with duckdb.connect(str(root / "dl.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory):
    root = _package(tmp_path_factory.mktemp("dl"), {"unknown_values": ["", "XX"]})
    engine = Runtime.from_path(str(root))
    try:
        engine._get_adapter()  # the module fixture owns its connection across tests
        yield engine
    finally:
        engine.close()


def _gold(sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return connection.execute(sql).fetchall()


def _rows(result: dict[str, Any]) -> set[tuple[Any, ...]]:
    return {(row.get(COUNTRY), int(row["installs"])) for row in result["rows"]}


def test_grouping_puts_every_unknown_spelling_in_one_unknown_group(runtime):
    result = runtime.query({"select": [INSTALLS], "group_by": [COUNTRY]})
    gold = _gold(f"SELECT {KNOWN}, SUM(installs) FROM downloads GROUP BY 1")
    assert _rows(result) == set(gold) == {("US", 15), ("DE", 4), (None, 6)}


@pytest.mark.parametrize(("missing", "test"), [(True, "IS NULL"), (False, "IS NOT NULL")])
def test_missing_reads_the_declared_spellings_as_unknown(runtime, missing, test):
    result = runtime.query(
        {"select": [INSTALLS], "where": [{"field": COUNTRY, "op": "missing", "value": missing}]}
    )
    [gold] = _gold(f"SELECT SUM(installs) FROM downloads WHERE {KNOWN} {test}")
    assert [int(row["installs"]) for row in result["rows"]] == [gold[0]]


def test_not_keeps_every_unknown_row_and_counts_them(runtime):
    result = runtime.query(
        {"select": [INSTALLS], "where": [{"field": COUNTRY, "op": "not", "value": "US"}]}
    )
    [gold] = _gold(
        f"SELECT SUM(installs) FROM downloads WHERE ({KNOWN}) <> 'US' OR ({KNOWN}) IS NULL"
    )
    assert [int(row["installs"]) for row in result["rows"]] == [gold[0]] == [10]
    [warning] = [row for row in result["warnings"] if row["code"] == "UNKNOWN_ROWS_KEPT"]
    [[kept]] = _gold(f"SELECT COUNT(*) FROM downloads WHERE {KNOWN} IS NULL")
    assert warning["details"] == {"field": COUNTRY, "rows": kept, "values": [None, "", "XX"]}


@pytest.mark.parametrize(("op", "missing"), [("is", "true"), ("not", "false")])
def test_an_unknown_spelling_in_is_or_not_is_refused_with_missing(runtime, op, missing):
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(
            {"select": [INSTALLS], "where": [{"field": COUNTRY, "op": op, "value": "XX"}]}
        )
    error = raised.value
    assert error.code == "INVALID_QUERY"
    assert error.details["path"] == "where[0].value"
    assert f'"op": "missing", "value": {missing}' in error.details["recovery_hints"][0]["message"]


@pytest.mark.parametrize(
    ("country", "defaults", "message"),
    [
        ({"unknown_values": ""}, None, "non-empty list"),
        ({"unknown_values": []}, None, "non-empty list"),
        ({"unknown_values": ["", None]}, None, "non-empty list"),
        ({}, {"dimension": {"unknown_values": [""]}}, "never as a package default"),
    ],
)
def test_unknown_values_is_a_list_declared_per_dimension(tmp_path, country, defaults, message):
    _package(tmp_path, country, defaults)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(tmp_path))
    assert message in str(raised.value) + str(raised.value.details)


# A reference code in one column, "Ref Code": two downloads record an unknown code as 'UNKNOWN'
# and two leave it NULL. DuckDB reads "Ref Code" and "REF CODE" as that one column.
REFERENCES = """
ALTER TABLE downloads ADD COLUMN "Ref Code" VARCHAR;
UPDATE downloads SET "Ref Code" = CASE download_id
  WHEN 1 THEN 'R1' WHEN 2 THEN 'UNKNOWN' WHEN 4 THEN 'UNKNOWN' WHEN 6 THEN 'R6' END;
"""
SENTINEL = {"unknown_values": ["UNKNOWN"]}


def _references(root: Path, spellings: dict[str, dict[str, Any]]) -> Runtime:
    """The downloads package with one dimension over each spelling of the reference column."""
    others = {
        f"ref_{index}": {"kind": "categorical", "column": column, **declared}
        for index, (column, declared) in enumerate(spellings.items())
    }
    _package(root, {}, others=others)
    model = root / "models" / "downloads.yml"
    doc = yaml.safe_load(model.read_text(encoding="utf-8"))
    # A declared measure that reads the column in its other spelling, through a CASE.
    when = {
        "kind": "comparison",
        "op": "missing",
        "left": {"kind": "column", "column": "REF CODE"},
        "right": {"kind": "literal", "value": True},
    }
    doc["model"]["measures"]["unreferenced_installs"] = {
        "kind": "aggregate", "expr": {"kind": "case", "whens": [
            {"when": when, "then": {"kind": "column", "column": "installs"}}],
            "else": {"kind": "literal", "value": 0}},
    }  # fmt: skip
    model.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(root / "dl.duckdb")) as connection:
        connection.execute(REFERENCES)
    return Runtime.from_path(str(root))


def _installs_where(column: str, op: str, value: Any) -> dict[str, Any]:
    ref = {"kind": "column", "entity": "entity.dl_download", "column": column}
    condition = {
        "kind": "comparison",
        "op": op,
        "left": ref,
        "right": {"kind": "literal", "value": value},
    }
    value_ref = {"kind": "column", "entity": "entity.dl_download", "column": "installs"}
    expression = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "value": value_ref,
        "condition": condition,
    }
    return {"select": [{"expression": expression, "as": "total"}]}


def _gold_references(where: str) -> int:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED + REFERENCES)
        [[total]] = connection.execute(
            f"SELECT SUM(installs) FROM downloads WHERE {where}"
        ).fetchall()
    return int(total)


@pytest.mark.parametrize("spelling", ["Ref Code", "REF CODE"])
@pytest.mark.parametrize(
    ("op", "value", "message"),
    [
        ("missing", True, "whose dimensions declare different unknown values"),
        ("is", "R1", "whose dimensions declare different unknown values"),
        ("contains", "R", "is the column of several dimensions"),
    ],
)
def test_a_column_read_whose_spellings_declare_different_unknown_values_is_refused(
    tmp_path, spelling, op, value, message
):
    # Without the refusal each spelling read only its own dimension: missing summed 10 in the
    # sentinel's spelling and 5 in the other, two answers for one column.
    runtime = _references(tmp_path, {"Ref Code": SENTINEL, "REF CODE": {}})
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.query(_installs_where(spelling, op, value))
    finally:
        runtime.close()
    error = raised.value
    assert error.code == "INVALID_QUERY"
    assert message in str(error)
    assert sorted(error.details["dimensions"]) == [
        "dimension.dl_download_ref_0",
        "dimension.dl_download_ref_1",
    ]
    assert '"op": "' + op + '"' in error.details["recovery_hints"][0]["message"]


def test_a_declared_measure_reading_a_spelling_whose_dimensions_disagree_is_refused(tmp_path):
    # The call has no inline column to check: the compiler refuses the measure's own read.
    runtime = _references(tmp_path, {"Ref Code": SENTINEL, "REF CODE": {}})
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.query({"select": ["measure.dl.unreferenced_installs"]})
    finally:
        runtime.close()
    error = raised.value
    assert error.code == "INVALID_QUERY"
    assert str(error).startswith(
        "measure.dl.unreferenced_installs.left: 'missing' reads entity.dl_download.REF CODE, "
        "whose dimensions declare different unknown values"
    )
    assert error.details["dimensions"] == [
        "dimension.dl_download_ref_0",
        "dimension.dl_download_ref_1",
    ]


@pytest.mark.parametrize(
    ("declared", "where"),
    [
        (SENTINEL, '"Ref Code" IS NULL OR "Ref Code" = \'UNKNOWN\''),
        ({}, '"Ref Code" IS NULL'),
    ],
)
def test_spellings_whose_dimensions_agree_give_one_answer(tmp_path, declared, where):
    runtime = _references(tmp_path, {"Ref Code": declared, "REF CODE": declared})
    try:
        totals = {
            spelling: [
                int(row["total"])
                for row in runtime.query(_installs_where(spelling, "missing", True))["rows"]
            ]
            for spelling in ["Ref Code", "REF CODE"]
        }
        [declared_row] = runtime.query({"select": ["measure.dl.unreferenced_installs"]})["rows"]
    finally:
        runtime.close()
    gold = _gold_references(where)
    assert totals == {"Ref Code": [gold], "REF CODE": [gold]}
    assert int(declared_row["unreferenced_installs"]) == gold


def test_one_dimension_over_its_exact_spelling_reads_its_unknown_values(tmp_path):
    runtime = _references(tmp_path, {"Ref Code": SENTINEL})
    try:
        missing = runtime.query(_installs_where("Ref Code", "missing", True))["rows"]
        contains = runtime.query(_installs_where("Ref Code", "contains", "R"))["rows"]
    finally:
        runtime.close()
    assert (
        [int(row["total"]) for row in missing]
        == [_gold_references('"Ref Code" IS NULL OR "Ref Code" = \'UNKNOWN\'')]
        == [10]
    )
    assert (
        [int(row["total"]) for row in contains]
        == [_gold_references("\"Ref Code\" LIKE '%R%' AND \"Ref Code\" <> 'UNKNOWN'")]
        == [15]
    )
