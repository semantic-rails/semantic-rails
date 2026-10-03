from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.interop.package_writer import write_package
from semantic_rails.metadata import inspect_payload
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import _maybe_group_by
from semantic_rails.planner.generators import _choose_group_dimensions
from semantic_rails.runtime import Runtime
from tests.semantic_rails.result_helpers import typed_rows

KEY = "dimension.shop_incident_id"
LABEL = "dimension.shop_incident_name"


def _package(tmp_path: Path, *, label: str = LABEL, composite: bool = False) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/incidents.csv").write_text(
        "incident_id,revision,incident_name,repair_cost,reported_at\n1,1,Leak,10,2026-01-01\n"
        f"{1 if composite else 2},2,Leak,20,2026-01-02\n",
        encoding="utf-8",
    )
    entity = {
        "key": ["incident_id", "revision"] if composite else ["incident_id"],
        "model": "incidents",
    }
    if label:
        entity["label_dimension"] = label
    raw = {
        "schema_version": 1,
        "package": {
            "id": "shop",
            "namespace": "shop",
            "name": "Shop",
            "warehouse": "duckdb",
            "default_db": "shop.duckdb",
            "seed": {"kind": "csv_dir_duckdb", "source": "data"},
        },
        "graph": {"entities": {"incident": entity}},
        "models": {
            "incidents": {
                "relation": "incidents",
                "entities": {"incident": {}},
                "dimensions": {
                    "name": {
                        "column": "incident_name",
                        "kind": "categorical",
                        "label": "Incident name",
                    },
                },
                "times": {"reported_at": {"kind": "date", "default_query_axis": True}},
                "measures": {
                    "repair_cost": {
                        "label": "Repair cost",
                        "kind": "aggregate",
                        "expr": "repair_cost",
                        "default_agg": "sum",
                    }
                },
            }
        },
    }
    path = tmp_path / "package.yml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("label", "change", "reason"),
    [
        ("dimension.unknown", "", "unknown dimension"),
        (KEY, "", "non-id dimension"),
        (KEY, "categorical-key", "non-id dimension"),
        (LABEL, "non-groupable", "groupable"),
        ("dimension.shop_location_name", "other-entity", "same entity"),
        (LABEL, "id", "non-id dimension"),
    ],
)
def test_invalid_label_declaration_names_entity_and_reason(tmp_path, label, change, reason):
    path = _package(tmp_path, label=label)
    raw = yaml.safe_load(path.read_text())
    dimensions = raw["models"]["incidents"]["dimensions"]
    if change == "non-groupable":
        dimensions["name"]["groupable"] = False
    elif change == "categorical-key":
        dimensions["incident_id"] = {"as": KEY, "kind": "categorical"}
    elif change == "id":
        dimensions["name"]["kind"] = "id"
    elif change == "other-entity":
        raw["graph"]["entities"]["location"] = {"key": "location_id", "model": "locations"}
        raw["models"]["locations"] = {
            "relation": "locations",
            "entities": {"location": {}},
            "dimensions": {"name": {"kind": "categorical"}},
        }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(SemanticLayerError) as caught:
        load_package_config(str(path))
    assert caught.value.code == "INVALID_CONFIG"
    assert "entity.shop_incident" in str(caught.value)
    assert reason in str(caught.value)


def test_label_round_trips_through_package_writer(tmp_path):
    config = load_package_config(str(_package(tmp_path)))
    assert config.entities[0].label_dimension == LABEL
    directory = write_package(config, tmp_path / "written", namespace="shop")
    graph = yaml.safe_load((directory / "graph.yml").read_text())
    assert graph["graph"]["entities"]["incident"]["label_dimension"] == LABEL
    assert load_package_config(str(directory)).entities == config.entities


def test_programmatic_config_cannot_bypass_label_validation(tmp_path):
    config = load_package_config(str(_package(tmp_path)))
    with pytest.raises(SemanticLayerError, match="unknown dimension") as caught:
        replace(config, entities=[replace(config.entities[0], label_dimension="dimension.unknown")])
    assert caught.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize(
    ("label", "intent", "composite", "expected"),
    [
        (LABEL, "repair cost by incident", False, [KEY, LABEL]),
        ("", "repair cost by incident", False, [KEY]),
        (LABEL, "repair cost by incident name", False, [LABEL]),
        (LABEL, "repair cost by incident", True, [KEY, "dimension.shop_incident_revision", LABEL]),
    ],
)
def test_planner_keeps_entity_identity_with_declared_label(
    tmp_path, label, intent, composite, expected
):
    runtime = Runtime.from_path(str(_package(tmp_path, label=label, composite=composite)))
    try:
        plan = plan_payload(runtime, intent=intent)
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query_ir"]
        assert query["group_by"] == expected
        result = runtime.query(query)
        rows = typed_rows(result)
        assert all(all(column in row for column in expected) for row in rows)
        if expected == [LABEL]:
            assert len(rows) == 1
        else:
            assert len(rows) == 2
            assert {row[KEY] for row in rows} == ({1} if composite else {1, 2})
        if LABEL in expected:
            assert {row[LABEL] for row in rows} == {"Leak"}
    finally:
        runtime.close()


def test_explicit_dimension_shortcut_and_pattern_use_same_label_expansion(tmp_path):
    runtime = Runtime.from_path(str(_package(tmp_path)))
    try:
        assert _choose_group_dimensions(runtime, {}, "", chosen_group_dim=KEY) == [KEY, LABEL]
        assert _choose_group_dimensions(runtime, {}, "", chosen_group_dim=LABEL) == [LABEL]
        assert _choose_group_dimensions(runtime, {}, "repair cost by incident") == [KEY, LABEL]
        assert _maybe_group_by(runtime._config, "repair cost by incident") == [KEY, LABEL]
    finally:
        runtime.close()


@pytest.mark.parametrize("missing", [False, True])
def test_label_expansion_refuses_missing_or_non_groupable_composite_key(tmp_path, missing):
    config = load_package_config(str(_package(tmp_path, composite=True)))
    config = replace(
        config,
        dimensions=[
            replace(row, groupable=False) if row.column == "revision" else row
            for row in config.dimensions
            if not (missing and row.column == "revision")
        ],
    )
    with pytest.raises(SemanticLayerError, match="groupable key dimension.*revision") as caught:
        _choose_group_dimensions(SimpleNamespace(_config=config), {}, "", chosen_group_dim=KEY)
    assert caught.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("verbosity", ["full", "minimal"])
def test_inspect_explains_undeclared_entity_label(tmp_path, verbosity):
    runtime = Runtime.from_path(str(_package(tmp_path, label="")))
    try:
        card = inspect_payload(runtime, object_id="entity.shop_incident", verbosity=verbosity)[
            "card"
        ]
        assert card["label_status"] == "no label declared; incident_id identifies it"
    finally:
        runtime.close()


def test_inspect_does_not_report_declared_label_as_missing(tmp_path):
    runtime = Runtime.from_path(str(_package(tmp_path)))
    try:
        card = inspect_payload(runtime, object_id="entity.shop_incident")["card"]
        assert "label_status" not in card
    finally:
        runtime.close()
