"""Frozen hours by consultant period, including unmatched entries and the midnight boundary."""

import shutil
from pathlib import Path

import pytest
import yaml

from semantic_rails import package_tools
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.yaml_loader import load_named_entries

from .answer_ledger import call_arguments, comparable, encode, resolves
from .conftest import _rows, _runtime

PERIODS = Path(__file__).with_name("periods")
ANSWERS = dict(load_named_entries(PERIODS / "tests", plural_key="tests", singular_key="test"))


@pytest.fixture(scope="module")
def periods(tmp_path_factory):
    package = tmp_path_factory.mktemp("periods") / "package"
    shutil.copytree(PERIODS, package)
    runtime = _runtime(package)
    try:
        _rows(runtime, "SELECT 1")
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize("case_id", ANSWERS)
def test_period_answers(periods, case_id):
    spec = ANSWERS[case_id]
    assert all(resolves(cite) for cite in spec["cites"])
    expected = comparable(encode(periods, spec))
    assert comparable(encode(periods, spec, lambda sql: _rows(periods, sql))) == expected
    report = package_tools._run_test(periods, case_id, spec)
    assert report["ok"], report


def test_unmatched_hours_are_disclosed(periods):
    spec = ANSWERS["bounded_periods_keep_unmatched_hours"]
    result = periods.query(spec["query"], **call_arguments(spec))
    (warning,) = [row for row in result["warnings"] if row["code"] == "NULL_PRESERVING_HISTORY"]
    assert warning["details"]["measures"] == [{"null_value": 29, "output": "hours", "total": 37}]
    assert "29 of 37 hours (78%) are in the empty Period group" in warning["message"]


@pytest.mark.parametrize(
    ("with_bounded_relationship", "window_overrides", "relationship_overrides", "extra_message"),
    [
        pytest.param(False, {}, {}, "", id="single"),
        pytest.param(True, {}, {}, "", id="mixed"),
        pytest.param(False, {"valid_to": None}, {}, "", id="null_valid_to"),
        pytest.param(False, {"valid_from": ""}, {}, "", id="blank_start_only"),
        pytest.param(False, {"valid_from": None}, {}, "", id="null_start_only"),
        pytest.param(False, {"as_of": "end_of_week"}, {}, "", id="bad_as_of"),
        pytest.param(
            False, {}, {"cardinality": "many_to_many", "safety": "safe"}, "", id="safe_many_to_many"
        ),
        pytest.param(
            False,
            {"valid_until": "valid_to"},
            {},
            "It also has unknown key 'valid_until' (it takes valid_from, valid_to, as_of).",
            id="unknown_key",
        ),
    ],
)
def test_start_only_period_window_is_refused(
    tmp_path, with_bounded_relationship, window_overrides, relationship_overrides, extra_message
):
    package = tmp_path / "periods"
    shutil.copytree(PERIODS, package)
    graph_path = package / "graph.yml"
    document = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    graph = document["graph"]
    relationships = graph["relationships"]
    relationship = "entry_period"
    if with_bounded_relationship:
        graph["entities"]["other_period"] = {
            **graph["entities"]["period"],
            "model": "other_periods",
        }
        (package / "models" / "other_periods.yml").write_text(
            "model: {id: other_periods, relation: consultant_periods, entities: {other_period: {}}}\n",
            encoding="utf-8",
        )
        relationship = "entry_other_period"
        relationships[relationship] = {
            **relationships["entry_period"],
            "entities": ["entry", "other_period"],
            "via": ["consultant_id"],
            "temporal_validity": dict(relationships["entry_period"]["temporal_validity"]),
        }
    relationships[relationship].update(relationship_overrides)
    validity = relationships[relationship]["temporal_validity"]
    del validity["valid_to"]
    validity.update(window_overrides)
    graph_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package))
    error = exc.value
    assert error.code == "INVALID_CONFIG"
    assert error.details["relationship"] == f"relationship.{relationship}"
    assert error.details["missing_key"] == "valid_to"
    assert f"relationship.{relationship} temporal_validity is missing valid_to" in str(error)
    assert "match every version that had started by the time it reads the history" in str(error)
    assert "be counted once per version" in str(error)
    assert "Declare valid_to" in str(error)
    assert "A NULL value in the valid_to column means the version is still current" in str(error)
    if extra_message:
        assert str(error).endswith(extra_message)
