from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.errors import SemanticLayerError
from semantic_rails.interop.package_writer import write_package
from semantic_rails.metadata import inspect_payload
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import _dimension, _group_dimensions_with_labels, _maybe_group_by
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


def test_fallback_and_pattern_use_same_label_expansion(tmp_path):
    runtime = Runtime.from_path(str(_package(tmp_path)))
    try:
        assert _choose_group_dimensions(runtime, {}, "repair cost by incident") == [KEY, LABEL]
        assert _maybe_group_by(runtime._config, "repair cost by incident") == [KEY, LABEL]
    finally:
        runtime.close()


@pytest.mark.parametrize("missing", [False, True])
def test_label_expansion_refuses_missing_or_non_groupable_composite_key(tmp_path, missing):
    config = load_package_config(str(_package(tmp_path, composite=True)))
    with pytest.raises(SemanticLayerError, match="groupable key dimension.*revision") as caught:
        replace(
            config,
            dimensions=[
                replace(row, groupable=False) if row.column == "revision" else row
                for row in config.dimensions
                if not (missing and row.column == "revision")
            ],
        )
    assert caught.value.code == "INVALID_CONFIG"
    assert "entity.shop_incident" in str(caught.value)


def test_loader_refuses_non_groupable_composite_key(tmp_path):
    path = _package(tmp_path, composite=True)
    raw = yaml.safe_load(path.read_text())
    raw["models"]["incidents"]["dimensions"]["revision"] = {"groupable": False}
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(SemanticLayerError, match="groupable key dimension.*revision") as caught:
        load_package_config(str(path))
    assert caught.value.code == "INVALID_CONFIG"
    assert "entity.shop_incident" in str(caught.value)


def test_loader_validates_synthesized_composite_key_dimensions(tmp_path):
    config = load_package_config(str(_package(tmp_path, composite=True)))
    assert _maybe_group_by(config, "repair cost by incident") == [
        KEY,
        "dimension.shop_incident_revision",
        LABEL,
    ]


@pytest.mark.parametrize("term", ["incident", "incident code"])
def test_label_sorting_first_does_not_replace_entity_identity(tmp_path, term):
    path = _package(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["models"]["incidents"]["dimensions"]["name"]["label"] = "Incident code"
    raw["models"]["incidents"]["dimensions"]["incident_id"] = {
        "as": KEY,
        "kind": "id",
        "label": "Incident id",
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    runtime = Runtime.from_path(str(path))
    try:
        intent = f"repair cost by {term}"
        expected = [KEY, LABEL] if term == "incident" else [LABEL]
        assert _dimension(runtime._config, ["incident"]).id == LABEL
        assert _maybe_group_by(runtime._config, intent) == expected
        assert _choose_group_dimensions(runtime, {}, intent) == expected
        plan = plan_payload(runtime, intent=intent)
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query_ir"]
        assert query["group_by"] == expected
        rows = typed_rows(runtime.query(query))
        columns = "incident_id, incident_name" if term == "incident" else "incident_name"
        with duckdb.connect(":memory:") as reference:
            expected_rows = reference.execute(
                f"SELECT {columns}, SUM(repair_cost) FROM read_csv_auto(?) GROUP BY {columns}",
                [str(tmp_path / "data/incidents.csv")],
            ).fetchall()
        measure_alias = query["select"][0]["as"]
        assert sorted(tuple(row[key] for key in [*expected, measure_alias]) for row in rows) == (
            sorted(expected_rows)
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "expected", "columns", "expected_rows"),
    [
        *[
            (
                f"repair cost by {terms}",
                [KEY, LABEL],
                "incident_id, incident_name",
                [(1, "Leak", 10), (2, "Leak", 20)],
            )
            for terms in ["store", "store id", "store name and store", "store and store name"]
        ],
        ("repair cost by store name", [LABEL], "incident_name", [("Leak", 30)]),
    ],
)
def test_store_grouping_keeps_identity_unless_label_is_requested(
    tmp_path, intent, expected, columns, expected_rows
):
    path = _package(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["graph"]["entities"]["incident"]["label"] = "Store"
    raw["models"]["incidents"]["dimensions"]["name"]["label"] = "Store name"
    raw["models"]["incidents"]["dimensions"]["incident_id"] = {
        "as": KEY,
        "kind": "id",
        "label": "Store id",
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    runtime = Runtime.from_path(str(path))
    try:
        plan = plan_payload(runtime, intent=intent)
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query_ir"]
        assert query["group_by"] == expected
        rows = typed_rows(runtime.query(query))
        with duckdb.connect(":memory:") as reference:
            reference.execute(
                "CREATE TABLE incidents AS SELECT * FROM read_csv_auto(?)",
                [str(tmp_path / "data/incidents.csv")],
            )
            assert (
                sorted(
                    reference.execute(
                        f"SELECT {columns}, SUM(repair_cost) FROM incidents GROUP BY {columns}"
                    ).fetchall()
                )
                == expected_rows
            )
        measure_alias = query["select"][0]["as"]
        assert sorted(tuple(row[key] for key in [*expected, measure_alias]) for row in rows) == (
            expected_rows
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("entity_label", ["Geo", "Region", "Geography"])
@pytest.mark.parametrize("explicit_label", [False, True], ids=["entity", "display-dimension"])
def test_geography_grouping_keeps_identity_unless_label_is_requested(
    tmp_path, entity_label, explicit_label
):
    path = _package(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["graph"]["entities"]["incident"]["label"] = entity_label
    raw["models"]["incidents"]["dimensions"]["name"]["label"] = "Geo code"
    raw["models"]["incidents"]["dimensions"]["incident_id"] = {
        "as": KEY,
        "kind": "id",
        "label": "Geo id",
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    term = "geo code" if explicit_label else entity_label.lower()
    expected = [LABEL] if explicit_label else [KEY, LABEL]
    columns = "incident_name" if explicit_label else "incident_id, incident_name"
    expected_rows = [("Leak", 30)] if explicit_label else [(1, "Leak", 10), (2, "Leak", 20)]
    runtime = Runtime.from_path(str(path))
    try:
        plan = plan_payload(runtime, intent=f"repair cost by {term}")
        query = plan["best"]["query_ir"]
        assert query["group_by"] == expected
        rows = typed_rows(runtime.query(query))
        with duckdb.connect(":memory:") as reference:
            reference.execute(
                "CREATE TABLE incidents AS SELECT * FROM read_csv_auto(?)",
                [str(tmp_path / "data/incidents.csv")],
            )
            assert (
                sorted(
                    reference.execute(
                        f"SELECT {columns}, SUM(repair_cost) FROM incidents GROUP BY {columns}"
                    ).fetchall()
                )
                == expected_rows
            )
        measure_alias = query["select"][0]["as"]
        assert sorted(tuple(row[key] for key in [*expected, measure_alias]) for row in rows) == (
            expected_rows
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("intent", ["revenue by store", "top stores by revenue"])
def test_package_without_entity_labels_keeps_store_grouping(intent):
    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    assert not any(row.label_dimension for row in config.entities)
    assert _maybe_group_by(config, intent) == ["dimension.jaffle_store_name"]


def test_categorical_composite_key_keeps_distinct_entities(tmp_path):
    path = _package(tmp_path, composite=True)
    data = tmp_path / "data/incidents.csv"
    data.write_text(data.read_text().replace("incident_id", "code"))
    raw = yaml.safe_load(path.read_text())
    raw["graph"]["entities"]["incident"]["key"] = ["code", "revision"]
    raw["models"]["incidents"]["dimensions"]["code"] = {
        "kind": "categorical",
        "label": "Incident id",
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    runtime = Runtime.from_path(str(path))
    try:
        expected = ["dimension.shop_incident_code", "dimension.shop_incident_revision", LABEL]
        intent = "repair cost by incident"
        assert _maybe_group_by(runtime._config, intent) == expected
        assert _choose_group_dimensions(runtime, {}, intent) == expected
        # A grouping without a term must also expand categorical key membership.
        assert _group_dimensions_with_labels(runtime._config, [(expected[0], "")]) == expected
        plan = plan_payload(runtime, intent=intent)
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query_ir"]
        assert query["group_by"] == expected
        rows = typed_rows(runtime.query(query))
        with duckdb.connect(":memory:") as reference:
            expected_rows = reference.execute(
                "SELECT code, revision, incident_name, SUM(repair_cost) "
                "FROM read_csv_auto(?) GROUP BY code, revision, incident_name",
                [str(data)],
            ).fetchall()
        measure_alias = query["select"][0]["as"]
        assert sorted(tuple(row[key] for key in [*expected, measure_alias]) for row in rows) == (
            sorted(expected_rows)
        )
        assert len(rows) == 2
        assert sorted(row[measure_alias] for row in rows) == [10, 20]
    finally:
        runtime.close()


def test_non_groupable_key_alias_does_not_hide_groupable_key(tmp_path):
    path = _package(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["models"]["incidents"]["dimensions"] = {
        "filter_id": {"column": "incident_id", "kind": "id", "groupable": False},
        "incident_id": {"as": KEY, "kind": "id", "label": "Incident id"},
        **raw["models"]["incidents"]["dimensions"],
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    runtime = Runtime.from_path(str(path))
    try:
        intent = "repair cost by incident id"
        assert _maybe_group_by(runtime._config, intent) == [KEY, LABEL]
        assert _choose_group_dimensions(runtime, {}, intent) == [KEY, LABEL]
        plan = plan_payload(runtime, intent=intent)
        assert plan["status"] == "ok", plan.get("why")
        assert plan["best"]["query_ir"]["group_by"] == [KEY, LABEL]
    finally:
        runtime.close()


@pytest.mark.parametrize("field", ["name", "label", "aliases"])
def test_exact_entity_names_override_selected_label_dimension(tmp_path, field):
    config = load_package_config(str(_package(tmp_path)))
    name = ["repair subject"] if field == "aliases" else "repair subject"
    config = replace(config, entities=[replace(config.entities[0], **{field: name})])
    assert _group_dimensions_with_labels(config, [(LABEL, "repair subject")]) == [KEY, LABEL]
    assert _group_dimensions_with_labels(config, [(LABEL, "other repair subject")]) == [LABEL]
    assert _maybe_group_by(config, "repair cost by repair subject") == [KEY, LABEL]


def test_entity_grouping_places_key_before_previously_named_label(tmp_path):
    runtime = Runtime.from_path(str(_package(tmp_path)))
    try:
        intent = "repair cost by incident name and incident"
        assert _maybe_group_by(runtime._config, intent) == [KEY, LABEL]
        assert _choose_group_dimensions(runtime, {}, intent) == [KEY, LABEL]
        plan = plan_payload(runtime, intent=intent)
        assert plan["status"] == "ok", plan.get("why")
        assert plan["best"]["query_ir"]["group_by"] == [KEY, LABEL]
    finally:
        runtime.close()


def test_ambiguous_entity_term_keeps_selected_label_dimension(tmp_path):
    config = load_package_config(str(_package(tmp_path)))
    other = replace(config.entities[0], id="entity.shop_other", label_dimension="dimension.other")
    label = next(row for row in config.dimensions if row.id == LABEL)
    key = next(row for row in config.dimensions if row.id == KEY)
    config = replace(
        config,
        entities=[*config.entities, other],
        dimensions=[
            *config.dimensions,
            replace(label, id="dimension.other", entity=other.id),
            replace(key, id="dimension.other_id", entity=other.id),
        ],
    )
    assert _group_dimensions_with_labels(config, [(LABEL, "incident")]) == [LABEL]


@pytest.mark.parametrize("chosen_column", ["incident_id", "revision"])
def test_expansion_preserves_selected_key_dimension_in_canonical_order(tmp_path, chosen_column):
    config = load_package_config(str(_package(tmp_path, composite=True)))
    keys = [row for row in config.dimensions if row.column in ("incident_id", "revision")]
    alternatives = [replace(row, id=f"{row.id}_alternate") for row in keys]
    config = replace(config, dimensions=[*alternatives, *config.dimensions])
    chosen = next(row.id for row in keys if row.column == chosen_column)
    expected = [
        next(row.id for row in keys if row.column == column)
        if column == chosen_column
        else next(row.id for row in alternatives if row.column == column)
        for column in ("incident_id", "revision")
    ]
    assert _group_dimensions_with_labels(config, [(chosen, "")]) == [*expected, LABEL]


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


@pytest.mark.parametrize("verbosity", ["full", "minimal"])
def test_inspect_does_not_report_declared_label_as_missing(tmp_path, verbosity):
    runtime = Runtime.from_path(str(_package(tmp_path)))
    try:
        card = inspect_payload(runtime, object_id="entity.shop_incident", verbosity=verbosity)[
            "card"
        ]
        assert "label_status" not in card
        assert card["label_dimension"] == LABEL
    finally:
        runtime.close()


@pytest.mark.parametrize("verbosity", ["full", "minimal"])
@pytest.mark.parametrize("roles", [[], ["sales"]])
def test_inspect_omits_role_hidden_entity_label(tmp_path, verbosity, roles):
    path = _package(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["semantic_policies"] = [
        {
            "id": "policy.shop.hidden_incident_name",
            "kind": "object_visibility",
            "object_ids": [LABEL],
            "roles": ["sales"],
            "action": "hidden",
        }
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    runtime = Runtime.from_path(str(path))
    try:
        card = inspect_payload(
            runtime,
            object_id="entity.shop_incident",
            verbosity=verbosity,
            partial_query={"policy_context": {"roles": roles}},
        )["card"]
        assert "label_status" not in card
        if roles:
            assert "label_dimension" not in card
            assert LABEL not in str(card)
        else:
            assert card["label_dimension"] == LABEL
    finally:
        runtime.close()


def test_inspect_time_entity_has_no_label_status(tmp_path):
    path = _package(tmp_path, label="")
    raw = yaml.safe_load(path.read_text())
    raw["graph"]["entities"]["incident"]["kind"] = "time"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    runtime = Runtime.from_path(str(path))
    try:
        card = inspect_payload(runtime, object_id="entity.shop_incident")["card"]
        assert "label_status" not in card
    finally:
        runtime.close()
