"""Validation names each group of models an agent can read for one another, and asks which one is
canonical.

A campus package keeps rooms beside their history, a keyless space survey beside its snapshots,
and floors beside the space survey, each counting distinct buildings. An agent asked "how many
rooms" or "how many buildings" can answer from any of them, each for another population, and only
the author knows which one a question means. ``validate_project`` (parse) and ``project_status``
name each group once (``LOOKALIKE_MODELS``), from the YAML alone. A table and the table that holds
its key (invoices and their payments), two calendars, and two tables' row counts are never a
group.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.architect_mcp import _guidance_payload, create_architect_mcp_server
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.lookalike_models import _same_rows_pairs, lookalike_groups
from semantic_rails.path_gap import _Grains

ROOT = Path(__file__).resolve().parents[2]
ROOM = "entity.campus_room"
ROOM_HIST = "entity.campus_room_hist"
FLOOR = "entity.campus_floor"
SPACE_DETAIL = "entity.campus_space_detail"
SPACE_DETAIL_HIST = "entity.campus_space_detail_hist"
FLOOR_BUILDINGS = "measure.campus.floors_distinct_building_id"
SPACE_BUILDINGS = "measure.campus.space_detail_distinct_building_id"


def _count(label: str, key: str) -> dict[str, Any]:
    return {
        "label": label,
        "kind": "entity_count",
        "entity_key": key,
        "accumulation": {"kind": "population"},
        "value_type": "count",
    }


def _rows(label: str) -> dict[str, Any]:
    """A table's row count, as a package mirrored from warehouse tables names it."""
    return {
        "label": label,
        "kind": "aggregate",
        "expr": "1",
        "default_agg": "sum",
        "accumulation": {"kind": "flow"},
        "value_type": "count",
    }


def _model(model_id: str, entities: dict[str, Any], **parts: Any) -> dict[str, Any]:
    return {"model": {"id": model_id, "relation": model_id, "entities": entities, **parts}}


def _categorical(*columns: str) -> dict[str, Any]:
    return {column: {"kind": "categorical"} for column in columns}


def _files() -> dict[str, Any]:
    valid_from = {"valid_from": {"column": "valid_from", "kind": "date"}}
    snapshot = {"snapshot_date": {"column": "snapshot_date", "kind": "date"}}
    return {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "campus",
                "namespace": "campus",
                "name": "Campus",
                "description": "Rooms, floors and billing",
                "warehouse": "duckdb",
                "default_db": "campus.duckdb",
                "seed": {"kind": "external"},
            },
            "defaults": {"time": {"timezone": "UTC"}},
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "room": {"key": ["room_id"], "model": "rooms"},
                    # One row per version of a room, from the date it took effect.
                    "room_hist": {"key": ["room_id", "valid_from"], "model": "rooms_hist"},
                    "floor": {"key": ["floor_id"], "model": "floors"},
                    "invoice": {"key": ["invoice_id"], "model": "invoices"},
                    "payment": {"key": ["payment_id"], "model": "payments"},
                    "time": {"kind": "time", "key": ["date_day"], "model": "calendar"},
                    "daily_use": {"key": ["date_day"], "model": "daily_use"},
                },
                "relationships": {},
            }
        },
        "models/rooms.yml": _model(
            "rooms",
            {"room": {}},
            description="One row per room in use.",
            dimensions=_categorical("room_name", "building_id"),
            measures={"rooms_count": _count("Rooms", "room_id")},
        ),
        "models/rooms_hist.yml": _model(
            "rooms_hist",
            {"room_hist": {}},
            description="One row per version of a room, past and present.",
            times=valid_from,
            dimensions=_categorical("room_name", "building_id"),
            measures={"rooms_hist_count": _count("Rooms", "room_id")},
        ),
        "models/floors.yml": _model(
            "floors",
            {"floor": {}},
            dimensions=_categorical("floor_name", "building_id"),
            measures={"floors_distinct_building_id": _count("Distinct buildings", "building_id")},
        ),
        # A keyless space survey and its snapshots, which read every column of it and a date.
        "models/space_detail.yml": _model(
            "space_detail",
            {},
            dimensions=_categorical("room_number", "building_id"),
            measures={
                "space_detail_count": _rows("Space detail rows"),
                "space_detail_distinct_building_id": _count("Distinct buildings", "building_id"),
            },
        ),
        "models/space_detail_hist.yml": _model(
            "space_detail_hist",
            {},
            times=snapshot,
            dimensions=_categorical("room_number", "building_id"),
        ),
        # Invoices and their payments: each payment holds the invoice it pays.
        "models/invoices.yml": _model(
            "invoices",
            {"invoice": {}},
            dimensions=_categorical("customer_name"),
            measures={"invoices_count": _count("Count", "invoice_id")},
        ),
        "models/payments.yml": _model(
            "payments",
            {"payment": {}},
            dimensions=_categorical("invoice_id", "method"),
            measures={"payments_count": _count("Count", "payment_id")},
        ),
        # Keyless tables that share only a row count.
        "models/cash_sales.yml": _model(
            "cash_sales",
            {},
            dimensions=_categorical("register"),
            measures={"cash_sales_count": _rows("Count")},
        ),
        "models/refunds.yml": _model(
            "refunds",
            {},
            dimensions=_categorical("reason"),
            measures={"refunds_count": _rows("Count")},
        ),
        "metrics/rooms.yml": {
            "metrics": {
                "rooms": {
                    "kind": "aggregate",
                    "measure": "rooms_count",
                    "value_type": "count",
                    "description": "Rooms in use.",
                }
            }
        },
        # The calendar, and a daily rollup keyed by the same day.
        "models/calendar.yml": _model(
            "calendar",
            {"time": {}},
            calendar_id="default",
            times={"date_day": {"column": "date_day", "kind": "date", "class": "calendar_time"}},
        ),
        "models/daily_use.yml": _model(
            "daily_use",
            {"daily_use": {}},
            dimensions=_categorical("weekday"),
            measures={"daily_use_count": _count("Days", "date_day")},
        ),
    }


def _write(parent: Path, files: dict[str, Any]) -> Path:
    """The package under ``parent/campus``: its directory is named for its id."""
    root = parent / "campus"
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def campus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write(tmp_path_factory.mktemp("campus"), _files())


def _lookalikes(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in report["warnings"] if row["code"] == "LOOKALIKE_MODELS"]


def _mcp(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(server.call_tool(name, arguments))[1]


def test_each_group_of_lookalike_models_is_named_once_with_the_question(campus):
    report, _ = parse_config_report(PackageReference(source_path=str(campus)))
    assert report["ok"]
    warnings = _lookalikes(report)
    assert [(row["details"]["kind"], row["details"]["entities"]) for row in warnings] == [
        ("same_rows", [ROOM, ROOM_HIST]),
        ("same_rows", [SPACE_DETAIL, SPACE_DETAIL_HIST]),
        ("same_count", [FLOOR, SPACE_DETAIL]),
    ]
    rooms, survey, buildings = warnings
    assert rooms["severity"] == "warning"
    assert rooms["message"] == (
        f"{campus}: {ROOM} and {ROOM_HIST} hold the same rows (keyed by room_id; {ROOM_HIST} "
        "adds valid_from: one row per version), so an agent can answer one question from any "
        "of them. Which is canonical? Say what each one's rows are in its model's description, "
        "and remove a copy no question needs."
    )
    assert rooms["details"]["keys"] == {ROOM: ["room_id"], ROOM_HIST: ["room_id", "valid_from"]}
    assert survey["message"].startswith(
        f"{campus}: {SPACE_DETAIL} and {SPACE_DETAIL_HIST} hold the same rows (neither has a "
        f"key, and {SPACE_DETAIL_HIST} reads every column {SPACE_DETAIL} does), so"
    )
    assert buildings["message"] == (
        f"{campus}: {FLOOR} and {SPACE_DETAIL} each count distinct building_id under one label "
        "(Distinct buildings) over rows that aren't the same things, so an agent can answer "
        "one question from any of them. Which is canonical? Label each count for the rows it "
        "counts, or set publish: false on those no question needs."
    )
    assert buildings["details"] == {
        "kind": "same_count",
        "entities": [FLOOR, SPACE_DETAIL],
        "measures": {FLOOR: [FLOOR_BUILDINGS], SPACE_DETAIL: [SPACE_BUILDINGS]},
        "unit": "distinct building_id",
        "label": "Distinct buildings",
    }


@pytest.mark.parametrize(
    ("floors", "space_detail", "grouped"),
    [
        # The author labels one count for the rows it counts: that answers the question.
        (
            ("floors_distinct_building_id", "Buildings with a floor"),
            ("space_detail_distinct_building_id", "Distinct buildings"),
            False,
        ),
        # Labels mirrored from each table still say nothing of the rows.
        (
            ("floors_distinct_building_id", "Distinct buildings (FLOORS)"),
            ("space_detail_distinct_building_id", "Distinct buildings (SPACE_DETAIL)"),
            True,
        ),
        # A new key alone leaves the label each card shows.
        (
            ("buildings_with_a_floor", "Distinct buildings"),
            ("space_detail_distinct_building_id", "Distinct buildings"),
            True,
        ),
    ],
)
def test_a_count_labelled_for_its_rows_leaves_its_group(tmp_path, floors, space_detail, grouped):
    files = _files()
    for path, (key, label) in (
        ("models/floors.yml", floors),
        ("models/space_detail.yml", space_detail),
    ):
        measures = files[path]["model"]["measures"]
        old = next(name for name in measures if name.endswith("distinct_building_id"))
        measures[key] = {**measures.pop(old), "label": label}
    config = load_package_config(str(_write(tmp_path, files)))
    kinds = [(group.kind, group.entities) for group in lookalike_groups(config)]
    assert (("same_count", (FLOOR, SPACE_DETAIL)) in kinds) is grouped
    assert [kind for kind in kinds if kind[0] == "same_rows"] == [
        ("same_rows", (ROOM, ROOM_HIST)),
        ("same_rows", (SPACE_DETAIL, SPACE_DETAIL_HIST)),
    ]


def test_a_table_and_the_table_holding_its_key_are_never_lookalikes(campus):
    """Payments hold each invoice's key, a calendar and a daily rollup share the day, and two
    keyless tables share only a row count: none is a group, and neither are their counts."""
    named = {
        entity
        for group in lookalike_groups(load_package_config(str(campus)))
        for entity in group.entities
    }
    assert named == {ROOM, ROOM_HIST, FLOOR, SPACE_DETAIL, SPACE_DETAIL_HIST}


def test_a_group_of_three_names_what_each_adds_and_counts_the_rest(tmp_path):
    files = _files()
    files["graph.yml"]["graph"]["entities"]["room_audit"] = {
        "key": ["room_id", "audited_at"],
        "model": "rooms_audit",
    }
    files["models/rooms_audit.yml"] = _model(
        "rooms_audit",
        {"room_audit": {}},
        times={"audited_at": {"column": "audited_at", "kind": "date"}},
        dimensions=_categorical("room_name"),
    )
    report, _ = parse_config_report(PackageReference(source_path=str(_write(tmp_path, files))))
    (rooms, *_) = _lookalikes(report)
    audit = "entity.campus_room_audit"
    assert rooms["details"]["entities"] == [ROOM, audit, ROOM_HIST]
    assert (
        f"{ROOM}, {audit} and {ROOM_HIST} hold the same rows (keyed by room_id; {audit} adds "
        f"audited_at; {ROOM_HIST} adds valid_from: one row per version)"
    ) in rooms["message"]


def test_the_message_names_five_models_and_the_details_all(tmp_path):
    files = _files()
    for index in range(6):
        model_id = f"rooms_copy_{index}"
        files["graph.yml"]["graph"]["entities"][model_id] = {"key": ["room_id"], "model": model_id}
        files[f"models/{model_id}.yml"] = _model(
            model_id, {model_id: {}}, dimensions=_categorical("room_name")
        )
    report, _ = parse_config_report(PackageReference(source_path=str(_write(tmp_path, files))))
    (rooms, *_) = _lookalikes(report)
    assert len(rooms["details"]["entities"]) == 8
    assert "and 3 more hold the same rows (keyed by room_id; " in rooms["message"]


@pytest.mark.parametrize(
    "package",
    [
        "tests/integration/correctness/shop",
        "tests/integration/correctness/teams",
        "configs/semantic_rails/jaffle_shop",
        "campus",
    ],
)
def test_the_indexed_pairs_are_every_pair_the_rule_joins(package, request):
    """Pairs are found through each entity's first key (or column), never every pair compared:
    the same pairs as comparing all of them."""
    path = request.getfixturevalue("campus") if package == "campus" else ROOT / package
    config = load_package_config(str(path))
    grains = _Grains(config)
    entities = {row.id: row for row in config.entities if row.kind != "time"}
    every = {
        (left, right)
        for left, right in itertools.combinations(sorted(entities), 2)
        if grains.same_things(left, right)
    }
    assert every
    assert _same_rows_pairs(grains, entities) == every


def test_project_status_asks_for_the_canonical_models_and_the_summary_shows_one(campus, tmp_path):
    server = create_architect_mcp_server(workspace_root=campus)
    status = _mcp(server, "project_status", {"project_path": str(campus)})
    assert status["next_actions"][0] == (
        "Name the canonical model of each of the 3 groups of look-alike models in parse.warnings "
        "(code LOOKALIKE_MODELS; page: parse.warnings lists them all): an agent can answer one "
        "question from any model of a group."
    )
    assert status["parse"]["warning_counts"]["LOOKALIKE_MODELS"] == 3
    summary = _mcp(server, "validate_project", {"project_path": str(campus)})
    (shown,) = [row for row in summary["warnings"] if row["code"] == "LOOKALIKE_MODELS"]
    assert shown["message"].startswith(f"{campus}: {ROOM} and {ROOM_HIST} hold the same rows")
    whole = _mcp(server, "validate_project", {"project_path": str(campus), "full_report": True})
    assert _lookalikes(whole) == _lookalikes(parse_config_report(PackageReference(str(campus)))[0])
    # A package without look-alikes gets no such action.
    files = {
        name: body
        for name, body in _files().items()
        if not name.startswith(("models/rooms_hist", "models/space_detail", "models/floors"))
    }
    entities = files["graph.yml"]["graph"]["entities"]
    for name in ("room_hist", "floor"):
        entities.pop(name)
    plain = _write(tmp_path / "plain", files)
    status = _mcp(
        create_architect_mcp_server(workspace_root=plain),
        "project_status",
        {"project_path": str(plain)},
    )
    assert not any("look-alike" in action for action in status["next_actions"])


def test_the_architect_guidance_says_who_decides():
    (principle,) = [
        line for line in _guidance_payload()["principles"] if "LOOKALIKE_MODELS" in line
    ]
    assert "decide which is canonical" in principle
    assert "never from the names alone" in principle
