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
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.architect_mcp import _guidance_payload, create_architect_mcp_server
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.interop.package_writer import write_package
from semantic_rails.lookalike_models import _same_rows_pairs, lookalike_groups
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.path_gap import _Grains
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.visible_view import build_view, hidden_object_ids
from tests.semantic_rails.hidden_absent import visibility_policy, with_policies

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
        f"{campus}: models rooms and rooms_hist hold the same rows (keyed by room_id; rooms_hist "
        "adds valid_from: one row per version), so an agent can answer one question from any "
        "of them. Which is canonical? List the others in its model's prefer_over, and say in "
        "each model's description what its rows are. When two of them are different things, "
        "list one in the other's distinct_from."
    )
    assert rooms["details"]["models"] == ["rooms", "rooms_hist"]
    assert rooms["details"]["keys"] == {ROOM: ["room_id"], ROOM_HIST: ["room_id", "valid_from"]}
    assert survey["message"].startswith(
        f"{campus}: models space_detail and space_detail_hist hold the same rows (neither has a "
        "key, and space_detail_hist reads every column space_detail does), so"
    )
    assert buildings["message"] == (
        f"{campus}: models floors and space_detail each count distinct building_id under one "
        "label (Distinct buildings) over rows that aren't the same things, so an agent can "
        "answer one question from any of them. Which is canonical? When one model answers "
        "every question the others do, list them in its prefer_over; else label each count for "
        "the rows it counts, or set publish: false on those no question needs."
    )
    assert buildings["details"] == {
        "kind": "same_count",
        "models": ["floors", "space_detail"],
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
        "models rooms, rooms_audit and rooms_hist hold the same rows (keyed by room_id; "
        "rooms_audit adds audited_at; rooms_hist adds valid_from: one row per version)"
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
        "(code LOOKALIKE_MODELS; page: parse.warnings lists them all), and list the others in "
        "its prefer_over (upsert_model): an agent can answer one question from any model of a "
        "group."
    )
    assert status["parse"]["warning_counts"]["LOOKALIKE_MODELS"] == 3
    summary = _mcp(server, "validate_project", {"project_path": str(campus)})
    (shown,) = [row for row in summary["warnings"] if row["code"] == "LOOKALIKE_MODELS"]
    assert shown["message"].startswith(f"{campus}: models rooms and rooms_hist hold the same rows")
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
    assert "prefer_over" in principle


def _with_prefer(files: dict[str, Any], **prefer: list[str]) -> dict[str, Any]:
    """``files`` with each named model's ``prefer_over``."""
    for name, body in files.items():
        model = body.get("model") if name.startswith("models/") else None
        if model and model["id"] in prefer:
            model["prefer_over"] = prefer[model["id"]]
    return files


def _rooms_audit(files: dict[str, Any]) -> dict[str, Any]:
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
    return files


def test_a_declared_canonical_model_clears_its_group_and_only_its_group(tmp_path):
    files = _with_prefer(_files(), rooms=["rooms_hist"], floors=["space_detail"])
    root = _write(tmp_path, files)
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert report["ok"]
    assert [row["details"]["models"] for row in _lookalikes(report)] == [
        ["space_detail", "space_detail_hist"]
    ]
    config = load_package_config(str(root))
    rows = {row.id: row for row in config.entities}
    assert rows[ROOM].prefer_over == [ROOM_HIST]
    assert rows[FLOOR].prefer_over == [SPACE_DETAIL]
    assert (rows[ROOM].model_id, rows[SPACE_DETAIL].model_id) == ("rooms", "space_detail")
    assert [group.canonical for group in lookalike_groups(config)] == [ROOM, "", FLOOR]
    status = _mcp(
        create_architect_mcp_server(workspace_root=root),
        "project_status",
        {"project_path": str(root)},
    )
    assert status["next_actions"][0].startswith(
        "Name the canonical model of each of the 1 group of look-alike models"
    )


def test_a_group_is_decided_only_when_one_model_reaches_every_other(tmp_path):
    """rooms over rooms_hist leaves rooms_audit undecided; rooms_hist over rooms_audit then
    decides the group through rooms_hist, and so does rooms listing both."""
    for prefer, warned in (
        ({"rooms": ["rooms_hist"]}, True),
        ({"rooms": ["rooms_hist"], "rooms_hist": ["rooms_audit"]}, False),
        ({"rooms": ["rooms_hist", "rooms_audit"]}, False),
        ({"rooms_hist": ["rooms_audit"], "rooms_audit": ["rooms"]}, False),
        ({"rooms": ["rooms_hist"], "rooms_audit": ["rooms_hist"]}, True),
    ):
        root = _write(tmp_path / "_".join(prefer), _with_prefer(_rooms_audit(_files()), **prefer))
        report, _ = parse_config_report(PackageReference(source_path=str(root)))
        models = [row["details"]["models"] for row in _lookalikes(report)]
        assert (["rooms", "rooms_audit", "rooms_hist"] in models) is warned, prefer


def test_an_author_may_declare_models_no_rule_groups(tmp_path):
    """Rooms and the keyless space survey read different columns: no rule groups them, but the
    author knows the survey's rows are rooms too."""
    root = _write(tmp_path, _with_prefer(_files(), rooms=["space_detail"]))
    config = load_package_config(str(root))
    assert {row.id: row.prefer_over for row in config.entities}[ROOM] == [SPACE_DETAIL]


def test_a_written_package_keeps_prefer_over(tmp_path):
    """The package writer names models by their entity keys: the list follows them."""
    root = _write(tmp_path, _with_prefer(_files(), rooms=["rooms_hist", "space_detail"]))
    out = write_package(load_package_config(str(root)), tmp_path / "out/campus", namespace="campus")
    rows = {row.id: row.prefer_over for row in load_package_config(str(out)).entities}
    assert rows[ROOM] == [ROOM_HIST, SPACE_DETAIL]
    assert yaml.safe_load((out / "models/room.yml").read_text())["model"]["prefer_over"] == [
        "room_hist",
        "space_detail",
    ]


def test_a_caller_never_sees_a_hidden_model_in_prefer_over(tmp_path):
    """Hiding a listed look-alike drops it from the list, never the model that lists it."""
    root = _write(tmp_path, _with_prefer(_files(), rooms=["rooms_hist", "space_detail"]))
    config = load_package_config(str(root))
    policy = with_policies(config, visibility_policy("hidden", ROOM_HIST))
    hidden = hidden_object_ids(policy, roles=["support"])
    assert ROOM_HIST in hidden
    assert ROOM not in hidden
    view = build_view(config, hidden)
    assert {row.id: row.prefer_over for row in view.entities}[ROOM] == [SPACE_DETAIL]
    assert f'"{ROOM_HIST}"' not in json.dumps([asdict(row) for row in view.entities])


@pytest.mark.parametrize(
    ("prefer", "message"),
    [
        (
            {"rooms": ["rooms_hst"]},
            "model 'rooms' prefer_over names 'rooms_hst', which is no model of the package "
            "(did you mean 'rooms_hist'?)",
        ),
        (
            {"rooms": ["rooms"]},
            "model 'rooms' prefer_over names the model itself: list the other models whose "
            "questions it answers instead",
        ),
        (
            {"rooms": ["rooms_hist"], "rooms_hist": ["floors"], "floors": ["rooms"]},
            "prefer_over runs in a circle (floors -> rooms -> rooms_hist -> floors): one model of "
            "a group is the canonical one, so drop one of these from the other's prefer_over",
        ),
        ({"rooms": "rooms_hist"}, "model 'rooms' prefer_over must be a list of model ids"),
        ({"rooms": ["rooms_hist", ""]}, "model 'rooms' prefer_over must be a list of model ids"),
    ],
)
def test_a_prefer_over_the_package_cannot_follow_is_refused(tmp_path, prefer, message):
    root = _write(tmp_path, _with_prefer(_files(), **prefer))
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert not report["ok"]
    assert [row["code"] for row in report["errors"]] == ["INVALID_CONFIG"]
    assert report["errors"][0]["message"].endswith(
        f"failed to load package config: {root}: {message}"
    )


def test_upsert_model_writes_keeps_and_removes_prefer_over(campus, tmp_path):
    root = _write(tmp_path, _files())
    server = create_architect_mcp_server(workspace_root=root)
    arguments = {
        "project_path": str(root),
        "model_id": "rooms",
        "entity_key": "room",
        "relation": "rooms",
        "primary_key": ["room_id"],
    }

    def write(revision: str, key: str, **changes: Any) -> dict[str, Any]:
        result = _mcp(
            server,
            "upsert_model",
            {**arguments, "expected_revision": revision, "idempotency_key": key, **changes},
        )
        assert result["status"] == "upserted", result
        return result

    def declared() -> Any:
        return yaml.safe_load((root / "models/rooms.yml").read_text())["model"].get("prefer_over")

    revision = _mcp(server, "project_status", {"project_path": str(root)})["revision"]
    revision = write(revision, "prefer", prefer_over=["rooms_hist"])["revision"]
    assert declared() == ["rooms_hist"]
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert ["rooms", "rooms_hist"] not in [row["details"]["models"] for row in _lookalikes(report)]
    # A replace keeps it; [] removes it.
    measure = _files()["models/rooms.yml"]["model"]["measures"]
    revision = write(revision, "replace", replace=True, measures=measure)["revision"]
    assert declared() == ["rooms_hist"]
    write(revision, "remove", prefer_over=[])
    assert declared() is None


ROOM_AUDIT = "entity.campus_room_audit"
HISTORY_OF_ROOM = f"history copy of {ROOM}"


def _discover(
    root: Path, terms: str, *, config: Any = None, caller: Any = None, **more: Any
) -> Any:
    """The MCP discover response's buckets, without the per-call fields."""
    runtime = (
        Runtime.from_config(config, source_path=str(root))
        if config is not None
        else Runtime.from_path(str(root))
    )
    try:
        response = SemanticLayerMCPAdapter(runtime).call_tool(
            "discover",
            {"terms": terms, **more},
            request_context=context_from_policy_context(caller) if caller else None,
        )
    finally:
        runtime.close()
    body = dict(response.get("structuredContent", response))
    for key in ("request_id", "timing_ms"):
        body.pop(key, None)
    return body


def _ids(body: Any, bucket: str) -> list[str]:
    return [row["id"] for row in body.get(bucket, [])]


def _marks(body: Any) -> dict[str, str]:
    return {
        row["id"]: row["look_alike"]
        for rows in body.values()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and "look_alike" in row
    }


def test_discover_lists_the_canonical_models_rows_first_and_marks_its_history(tmp_path):
    plain = _discover(_write(tmp_path / "plain", _files()), "rooms")
    root = _write(tmp_path / "declared", _with_prefer(_files(), rooms=["rooms_hist"]))
    body = _discover(root, "rooms")
    # The same rows in every bucket; the room's dimensions now come before its history's, in
    # the places those rows held, and the survey's keep theirs.
    assert {
        bucket: sorted(_ids(body, bucket)) for bucket in body if isinstance(body[bucket], list)
    } == {
        bucket: sorted(_ids(plain, bucket)) for bucket in plain if isinstance(plain[bucket], list)
    }
    dimensions = _ids(body, "dimensions")
    assert _ids(plain, "dimensions") != dimensions
    room = [
        index for index, row in enumerate(dimensions) if row.startswith("dimension.campus_room_")
    ]
    hist = [index for index, row in enumerate(dimensions) if "campus_room_hist_" in row]
    room = [index for index in room if index not in hist]
    assert max(room) < min(hist)
    assert [row for row in dimensions if "space_detail" in row] == [
        row for row in _ids(plain, "dimensions") if "space_detail" in row
    ]
    assert [dimensions.index(row) for row in dimensions if "space_detail" in row] == [
        _ids(plain, "dimensions").index(row) for row in dimensions if "space_detail" in row
    ]
    assert _ids(body, "entities") == [ROOM, ROOM_HIST]

    # The history's measure and entity cards say whose history it is, and in full cards its
    # dimensions too; the room's cards carry nothing.
    def history_rows(body: Any, buckets: tuple[str, ...]) -> dict[str, str]:
        return {
            row: HISTORY_OF_ROOM
            for bucket in buckets
            for row in _ids(body, bucket)
            if "room_hist" in row or "rooms_hist" in row
        }

    marks = _marks(body)
    assert marks == history_rows(body, ("measures", "entities"))
    assert "measure.campus.rooms_hist_count" in marks
    full = _discover(root, "rooms", verbosity="full")
    assert _marks(full) == history_rows(full, ("measures", "dimensions", "entities"))
    assert "dimension.campus_room_hist_valid_from" in _marks(full)
    assert not _marks(plain)


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_every_verbosity_carries_the_mark(tmp_path, verbosity):
    root = _write(tmp_path, _with_prefer(_files(), rooms=["rooms_hist"]))
    body = _discover(root, "rooms", verbosity=verbosity)
    assert _marks(body)[ROOM_HIST] == HISTORY_OF_ROOM
    assert _marks(body)["measure.campus.rooms_hist_count"] == HISTORY_OF_ROOM


def test_a_look_alike_that_is_no_history_reads_prefer(tmp_path):
    """The survey isn't keyed by the room's key, so it's marked only as the one to read less;
    a chain of lists reaches the audit table through the history, after both."""
    files = _with_prefer(
        _rooms_audit(_files()), rooms=["rooms_hist", "space_detail"], rooms_hist=["rooms_audit"]
    )
    body = _discover(_write(tmp_path, files), "rooms", verbosity="full")
    assert _ids(body, "entities")[:3] == [ROOM, ROOM_HIST, ROOM_AUDIT]
    marks = _marks(body)
    assert marks[ROOM_HIST] == marks[ROOM_AUDIT] == HISTORY_OF_ROOM
    assert {mark for row, mark in marks.items() if "space_detail" in row and "hist" not in row} == {
        f"prefer {ROOM}"
    }


@pytest.mark.parametrize("hidden", [ROOM, ROOM_HIST])
def test_a_caller_who_cant_see_a_model_gets_discover_as_if_nothing_were_declared(tmp_path, hidden):
    """Hiding the canonical model drops it from the history's list in the caller's view, so
    nothing is marked or moved; hiding the history leaves the room nothing to stand before."""
    caller = {"roles": ["support"]}
    bodies = []
    for name, prefer in (("plain", {}), ("declared", {"rooms": ["rooms_hist"]})):
        root = _write(tmp_path / name, _with_prefer(_files(), **prefer))
        config = with_policies(load_package_config(str(root)), visibility_policy("hidden", hidden))
        bodies.append(_discover(root, "rooms", config=config, caller=caller))
    plain, declared = bodies
    assert declared == plain
    assert not _marks(declared)
    assert f'"{hidden}"' not in json.dumps(declared)


def test_plan_and_execute_read_the_declared_package_as_before(tmp_path):
    calls = (
        ("plan", {"intent": "how many rooms by building"}),
        (
            "execute",
            {
                "query": {
                    "select": ["measure.campus.rooms_hist_count"],
                    "group_by": ["dimension.campus_room_hist_building_id"],
                },
                "mode": "sql",
            },
        ),
    )
    answers = []
    for name, prefer in (("plain", {}), ("declared", {"rooms": ["rooms_hist"]})):
        runtime = Runtime.from_path(str(_write(tmp_path / name, _with_prefer(_files(), **prefer))))
        try:
            adapter = SemanticLayerMCPAdapter(runtime)
            rows = []
            for tool, arguments in calls:
                body = dict(adapter.call_tool(tool, arguments, request_context=None))
                body = dict(body.get("structuredContent", body))
                for key in ("request_id", "timing_ms"):
                    body.pop(key, None)
                rows.append(json.dumps(body, sort_keys=True, default=str).replace(name, "<pkg>"))
            answers.append(rows)
        finally:
            runtime.close()
    assert answers[0] == answers[1]


# --- distinct_from: models that only look alike ---------------------------------------------


def _with_distinct(files: dict[str, Any], **distinct: list[str]) -> dict[str, Any]:
    """``files`` with each named model's ``distinct_from``."""
    for name, body in files.items():
        model = body.get("model") if name.startswith("models/") else None
        if model and model["id"] in distinct:
            model["distinct_from"] = distinct[model["id"]]
    return files


@pytest.mark.parametrize(
    ("declared", "loaded"),
    [
        ({"rooms": ["rooms_hist"]}, {ROOM: [ROOM_HIST]}),
        ({"rooms_hist": ["rooms"]}, {ROOM_HIST: [ROOM]}),
    ],
)
def test_models_set_apart_by_either_one_are_in_no_group(tmp_path, declared, loaded):
    """The author has looked: rooms_hist holds room inspections, not versions of rooms. Whichever
    of the two lists the other, they are in no group, and their counts of rooms under one label
    form no count group either; the audit table stays grouped with the rooms."""
    root = _write(tmp_path / "pair", _with_distinct(_files(), **declared))
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert report["ok"]
    assert [row["details"]["models"] for row in _lookalikes(report)] == [
        ["space_detail", "space_detail_hist"],
        ["floors", "space_detail"],
    ]
    root = _write(tmp_path / "three", _with_distinct(_rooms_audit(_files()), **declared))
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert [row["details"]["models"] for row in _lookalikes(report)][0] == ["rooms", "rooms_audit"]
    rows = load_package_config(str(root)).entities
    assert {row.id: row.distinct_from for row in rows if row.distinct_from} == loaded


@pytest.mark.parametrize(
    ("lists", "message"),
    [
        (
            {"distinct_from": {"rooms": ["rooms_hst"]}},
            "model 'rooms' distinct_from names 'rooms_hst', which is no model of the package "
            "(did you mean 'rooms_hist'?)",
        ),
        (
            {"distinct_from": {"rooms": ["rooms"]}},
            "model 'rooms' distinct_from names the model itself: list the other models whose "
            "rows are different things",
        ),
        (
            {"distinct_from": {"rooms": "rooms_hist"}},
            "model 'rooms' distinct_from must be a list of model ids",
        ),
        (
            {"prefer_over": {"rooms": ["rooms_hist"]}, "distinct_from": {"rooms": ["rooms_hist"]}},
            "model 'rooms' names 'rooms_hist' in both prefer_over and distinct_from: a model "
            "answers only for its look-alikes, so drop one of them",
        ),
        (
            {"prefer_over": {"rooms": ["rooms_hist"]}, "distinct_from": {"rooms_hist": ["rooms"]}},
            "model 'rooms' prefer_over names 'rooms_hist', and model 'rooms_hist' distinct_from "
            "names 'rooms': a model answers only for its look-alikes, so drop one of them",
        ),
    ],
)
def test_a_distinct_from_the_package_cannot_follow_is_refused(tmp_path, lists, message):
    files = _with_prefer(_files(), **lists.get("prefer_over", {}))
    root = _write(tmp_path, _with_distinct(files, **lists["distinct_from"]))
    report, _ = parse_config_report(PackageReference(source_path=str(root)))
    assert not report["ok"]
    assert [row["code"] for row in report["errors"]] == ["INVALID_CONFIG"]
    assert report["errors"][0]["message"].endswith(
        f"failed to load package config: {root}: {message}"
    )


def test_a_written_package_keeps_distinct_from_and_its_fingerprint_records_it(tmp_path):
    plain = load_package_snapshot(str(_write(tmp_path / "plain", _files())))
    root = _write(tmp_path / "declared", _with_distinct(_files(), rooms=["rooms_hist"]))
    assert load_package_snapshot(str(root)).semantic_fingerprint != plain.semantic_fingerprint
    out = write_package(load_package_config(str(root)), tmp_path / "out/campus", namespace="campus")
    rows = {row.id: row.distinct_from for row in load_package_config(str(out)).entities}
    assert rows[ROOM] == [ROOM_HIST]
    assert yaml.safe_load((out / "models/room.yml").read_text())["model"]["distinct_from"] == [
        "room_hist"
    ]


def test_a_caller_never_sees_a_hidden_model_in_distinct_from(tmp_path):
    """Hiding a model drops it from the list, never the model that lists it."""
    root = _write(tmp_path, _with_distinct(_files(), rooms=["rooms_hist", "space_detail"]))
    config = load_package_config(str(root))
    hidden = hidden_object_ids(
        with_policies(config, visibility_policy("hidden", ROOM_HIST)), roles=["support"]
    )
    assert ROOM not in hidden
    view = build_view(config, hidden)
    assert {row.id: row.distinct_from for row in view.entities}[ROOM] == [SPACE_DETAIL]
    assert f'"{ROOM_HIST}"' not in json.dumps([asdict(row) for row in view.entities])


def test_discover_marks_no_model_its_canonical_one_is_set_apart_from(tmp_path):
    """rooms answers for its history, and the history for the audit table; but rooms says the
    audit table holds different things, so the audit table carries no mark naming rooms."""
    files = _with_prefer(_rooms_audit(_files()), rooms=["rooms_hist"], rooms_hist=["rooms_audit"])
    body = _discover(_write(tmp_path, _with_distinct(files, rooms=["rooms_audit"])), "rooms")
    marks = _marks(body)
    assert marks[ROOM_HIST] == HISTORY_OF_ROOM
    assert ROOM_AUDIT in _ids(body, "entities")
    assert not [row for row in marks if "audit" in row]


def test_an_architect_replace_keeps_distinct_from(tmp_path):
    root = _write(tmp_path, _with_distinct(_files(), rooms=["rooms_hist"]))
    server = create_architect_mcp_server(workspace_root=root)
    result = _mcp(
        server,
        "upsert_model",
        {
            "project_path": str(root),
            "model_id": "rooms",
            "entity_key": "room",
            "relation": "rooms",
            "primary_key": ["room_id"],
            "replace": True,
            "measures": _files()["models/rooms.yml"]["model"]["measures"],
            "expected_revision": _mcp(server, "project_status", {"project_path": str(root)})[
                "revision"
            ],
            "idempotency_key": "replace",
        },
    )
    assert result["status"] == "upserted", result
    assert "distinct_from" not in json.dumps(result.get("dropped_fields"))
    model = yaml.safe_load((root / "models/rooms.yml").read_text())["model"]
    assert model["distinct_from"] == ["rooms_hist"]
