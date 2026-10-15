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


def _package(root: Path, country: dict[str, Any], defaults: dict[str, Any] | None = None) -> Path:
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
        "dimensions": {"country": {"kind": "categorical", **country}},
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
