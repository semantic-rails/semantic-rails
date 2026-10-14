"""Row-grain models: a model no graph entity is homed on has no key, and every stored row counts.

The shop fixture's ``shipment_scans`` (a scan log with an exact duplicate, a scan with no order and
one whose order doesn't exist) and ``order_tags`` (a link table) are row grain. Their answers are
frozen against hand SQL in the shop ledger (``shop/row-grain-*``); this file pins what loads, what
refuses and how, and what the other surfaces say.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.cli.commands import project as project_commands
from semantic_rails.config import load_package_config, load_package_snapshot
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.interop.ossie.reader import import_ossie
from semantic_rails.interop.package_writer import write_package
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import discover_payload, inspect_payload
from semantic_rails.planner import plan_payload
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime

SHOP = Path(__file__).resolve().parents[1] / "integration" / "correctness" / "shop"
SCANS = "entity.shop_shipment_scans"
SCAN_COUNT = "measure.shop.scan_count"
SCAN_TYPE = "dimension.shop_shipment_scans_scan_type"
ROW_GRAIN_MESSAGE = (
    "'Shipment scans' is row grain (no key), and this needs to tell its rows apart (a count of "
    "them as entities, a segment, a predicate, a conversion or a stock). Count its rows, sum its "
    "values, or group by its or its parents' columns; or declare a key on shipment_scans if its "
    "rows have one."
)
JOIN_INTO_SCANS = (
    "relationship.order_scans joins into 'shipment_scans', a row-grain model (no key), so one row "
    "can't be looked up there. Fix: write it from shipment_scans's side (entities: "
    "[shipment_scans, order], cardinality: many_to_one, via: [order_id]), or declare a key on "
    "shipment_scans if its rows have one. To look up what is fixed per key, model its relation "
    "again with one_per_key: true."
)


def _shop(tmp_path: Path, *edits: tuple[str, Any]) -> Path:
    """A copy of the shop fixture, each ``(file, edit)`` applied to that YAML document."""
    package = tmp_path / "shop"
    shutil.copytree(SHOP, package)
    for name, edit in edits:
        path = package / name
        path.parent.mkdir(exist_ok=True)
        document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        edit(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return package


def _scans(edit: Any) -> tuple[str, Any]:
    return "models/shipment_scans.yml", lambda document: edit(document["model"])


def _graph(edit: Any) -> tuple[str, Any]:
    return "graph.yml", lambda document: edit(document["graph"])


def _relationship(spec: dict[str, Any]) -> tuple[str, Any]:
    return _graph(lambda graph: graph.setdefault("relationships", {}).update(order_scans=spec))


def _refusal(package: Path) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    assert raised.value.code == "INVALID_CONFIG"
    return raised.value


def _runtime(package: Path) -> Runtime:
    config = load_package_config(str(package))
    return Runtime.from_config(config, source_path=str(package), package_id="shop")


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Any:
    runtime = _runtime(_shop(tmp_path_factory.mktemp("row_grain")))
    yield runtime
    runtime.close()


def test_a_model_no_graph_entity_names_has_no_key() -> None:
    config = load_package_config(str(SHOP))
    scans = next(entity for entity in config.entities if entity.id == SCANS)
    assert (scans.key_columns, scans.model, scans.label) == ([], "shipment_scans", "Shipment scans")
    assert scans.is_row_grain and scans.foreign_keys == {"order": ["order_id"]}
    assert all(measure.row_grain == [] for measure in config.measures if measure.entity == SCANS)
    for read in (lambda: scans.key, lambda: scans.primary_key):
        with pytest.raises(SemanticLayerError) as raised:
            read()
        assert raised.value.code == "ROW_GRAIN_UNSUPPORTED"
        assert str(raised.value) == ROW_GRAIN_MESSAGE
        assert raised.value.details == {"model": "shipment_scans", "entity": SCANS}
    joins = {row.id: row for row in config.relationships if row.source_entity == SCANS}
    assert [
        (row.target_entity, row.cardinality, row.source_key_role) for row in joins.values()
    ] == [("entity.shop_order", "N:1", "foreign")]
    assert not any(row.target_entity == SCANS for row in config.relationships)


@pytest.mark.parametrize(
    ("entities", "same_named", "row_grain"),
    [
        ({}, False, True),  # an empty block, and no entity of the model's name
        ({}, True, False),  # an empty block binds the entity of the model's name
        ({"shipment_scans": {}, "order": {}}, False, True),  # the model's own name, unbound
        ({"order": {}}, False, True),  # foreign keys only
    ],
)
def test_which_entities_blocks_make_a_model_row_grain(tmp_path, entities, same_named, row_grain):
    edits = [_scans(lambda model: model.update(entities=entities))]
    if same_named:
        key = {"key": ["order_id", "scanned_at"]}
        edits.append(_graph(lambda graph: graph["entities"].update(shipment_scans=key)))
    config = load_package_config(str(_shop(tmp_path, *edits)))
    scans = next(entity for entity in config.entities if entity.id == SCANS)
    assert scans.is_row_grain is row_grain
    assert scans.key_columns == ([] if row_grain else ["order_id", "scanned_at"])


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (  # a typo can't make a model row grain
            [_scans(lambda model: model.update(entities={"ordr": {}}))],
            "model 'shipment_scans' lists entities ['ordr'] that graph.entities doesn't declare",
        ),
        (
            [_scans(lambda model: model.update(entities={"bridge": False, "ordr": {}}))],
            "model 'shipment_scans' lists entities ['ordr'] that graph.entities doesn't declare",
        ),
        (  # the model's name is taken by a graph entity of another model's rows
            [
                _graph(lambda graph: graph["entities"].update(shipment_scans={"key": ["x"]})),
                _scans(lambda model: model.update(entities={"order": {}})),
            ],
            "model 'shipment_scans' lists no entity of its own, so it is row grain under a graph "
            "entity named after it, but graph.entities already declares 'shipment_scans'",
        ),
        (  # a model's name is not an entity
            [_scans(lambda model: model.update(entities={"order": {}, "orders": {}}))],
            "model 'shipment_scans' lists entities ['orders'] that graph.entities doesn't declare",
        ),
        (  # a balance reads one snapshot per series, and a series is the key minus the clock
            [
                _scans(
                    lambda model: model["measures"].update(
                        balance={
                            "kind": "aggregate",
                            "expr": "weight",
                            "accumulation": {"kind": "stock", "snapshot": "end_of_period"},
                        }
                    )
                )
            ],
            "measure 'balance' is a stock, but model 'shipment_scans' is row grain (no key)",
        ),
        (  # counting the model's rows as entities needs a key
            [_scans(lambda model: model["measures"].update(rows={"kind": "entity_count"}))],
            "entity_count measure 'rows' requires a single-column entity_key",
        ),
        (  # a composite key is joined on all of its columns, never on one
            [_scans(lambda model: model["entities"].update(account_day={"expr": "order_id"}))],
            "join 'shipment_scans.account_day' key width does not match target key",
        ),
        (  # summing a value that must not be summed needs each row's key, which it hasn't
            [_scans(lambda model: model["measures"]["heaviest_scan"].update(default_agg="sum"))],
            "measure 'heaviest_scan' is additive: false on row-grain model 'shipment_scans' (no "
            "key), so its default_agg 'sum' can't be computed",
        ),
        (  # sum is also the default aggregation
            [_scans(lambda model: model["measures"]["heaviest_scan"].pop("default_agg"))],
            "Set default_agg to one of avg, min, max",
        ),
    ],
)
def test_what_a_row_grain_model_refuses_to_load(tmp_path, edits, message):
    assert message in str(_refusal(_shop(tmp_path, *edits)))


@pytest.mark.parametrize(
    "spec",
    [
        {"entities": ["order", "shipment_scans"]},
        {"entities": ["order", "shipment_scans"], "cardinality": "one_to_many"},
        {
            "entities": ["order", "shipment_scans"],
            "cardinality": "one_to_many",
            "target": ["order_id"],
        },
    ],
)
def test_a_join_into_a_row_grain_model_names_the_fix(tmp_path, spec):
    error = _refusal(_shop(tmp_path, _relationship(spec)))
    assert str(error).endswith(JOIN_INTO_SCANS)
    assert "not its key" not in str(error)


def test_another_model_listing_a_row_grain_model_is_a_join_into_it(tmp_path):
    lists = ("models/refunds.yml", lambda doc: doc["model"]["entities"].update(shipment_scans={}))
    assert str(_refusal(_shop(tmp_path, lists))).endswith(
        "relationship.refunds_shipment_scans joins into 'shipment_scans', a row-grain model (no "
        "key), so one row can't be looked up there. Fix: write it from shipment_scans's side "
        "(entities: [shipment_scans, refund], cardinality: many_to_one, via: [refund_id]), or "
        "declare a key on shipment_scans if its rows have one. To look up what is fixed per "
        "key, model its relation again with one_per_key: true."
    )


@pytest.mark.parametrize(
    "spec",
    [  # its own lookup on a column that isn't the parent's key: possibly several rows each
        {"entities": ["shipment_scans", "order"], "via": ["scan_type"], "target": ["store_id"]},
        {"entities": ["shipment_scans", "customer"], "via": ["scan_type"], "target": ["channel"]},
    ],
)
def test_a_row_grain_lookup_on_a_non_key_column_gets_only_the_non_key_message(tmp_path, spec):
    error = _refusal(_shop(tmp_path, _relationship(spec)))
    assert f"on [{', '.join(spec['target'])}], not its key [" in str(error)
    assert "row-grain" not in str(error)


@pytest.mark.parametrize(
    "query",
    [
        {  # a segment's members are rows of its entity
            "select": [SCAN_COUNT],
            "where": [
                {
                    "entity": SCANS,
                    "having": [{"field": SCAN_COUNT, "op": "gte", "value": 1}],
                    "over": "all_time",
                }
            ],
        },  # fmt: skip
        {
            "select": [
                {
                    "as": "x",
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": SCAN_COUNT,
                        "predicates": [
                            {
                                "input": "measure.shop.scan_weight",
                                "entity": SCANS,
                                "op": "gt",
                                "value": 1,
                                "over": "all_time",
                            }
                        ],
                    },
                }
            ],
        },  # fmt: skip
        {
            "select": [
                {
                    "as": "x",
                    "expression": {
                        "kind": "distribution",
                        "function": "max",
                        "over": {
                            "kind": "entity_value",
                            "entity": SCANS,
                            "input": "measure.shop.scan_weight",
                        },
                    },
                }
            ],
        },  # fmt: skip
    ],
    ids=["entity_filter", "anchored_set", "per_row_value"],
)
def test_telling_its_rows_apart_refuses(shop, query):
    with pytest.raises(SemanticLayerError) as raised:
        shop.query(query)
    assert (raised.value.code, str(raised.value)) == ("ROW_GRAIN_UNSUPPORTED", ROW_GRAIN_MESSAGE)


def test_a_sum_of_a_non_additive_measure_names_what_it_can_be(shop):
    query = {
        "select": [
            {
                "as": "total",
                "expression": {
                    "kind": "measure",
                    "measure": "measure.shop.heaviest_scan",
                    "aggregation": "sum",
                },
            }
        ]
    }
    with pytest.raises(SemanticLayerError) as raised:
        shop.query(query)
    assert raised.value.code == "ROW_GRAIN_UNSUPPORTED"
    message = str(raised.value)
    assert "'measure.shop.heaviest_scan' is additive: false" in message
    assert "avg / min / max" in message and "sum its values" not in message
    assert raised.value.details["model"] == "shipment_scans"


def test_segments_and_conversions_on_a_row_grain_model_refuse(tmp_path):
    scans = {"as": "metric.shop.scans", "kind": "derived", "label": "Scans", "value_type": "count"}
    scans["expression"] = {"measure": SCAN_COUNT}
    conversion = {
        "as": "metric.shop.rescan_7d",
        "label": "Rescanned within 7 days",
        "kind": "conversion",
        "temporal_role": "temporal_role.shop_shipment_scans_scanned_at",
        "value_type": "percent",
        "expression": {
            "kind": "conversion",
            "entity": SCANS,
            "window": {"unit": "day", "value": 7},
            "matching_mode": "first_converted_after_base",
            "base": {"kind": "aggregate", "measure": "scanned_orders"},
            "converted": {"kind": "aggregate", "measure": "scanned_orders"},
        },
    }
    segment = {"as": "segment.shop.pickups", "label": "Pickups", "entity": SCANS}
    segment.update(basis_metric="metric.shop.scans")
    segment["membership"] = {"where": [{"field": SCAN_TYPE, "op": "is", "value": "pickup"}]}
    package = _shop(
        tmp_path,
        ("metrics/metrics.yml", lambda doc: doc["metrics"].update(scans=scans, rescan=conversion)),
        ("segments.yml", lambda doc: doc.update(segments={"pickups": segment})),
    )
    runtime = _runtime(package)
    try:
        report = runtime.segment_validate("segment.shop.pickups")
        assert [error["code"] for error in report["errors"]] == ["ROW_GRAIN_UNSUPPORTED"]
        for call in (
            lambda: runtime.segment_preview("segment.shop.pickups"),
            lambda: runtime.query(
                {
                    "select": ["metric.shop.rescan_7d"],
                    "time": {"temporal_role": conversion["temporal_role"], "grain": "year"},
                }
            ),
        ):
            with pytest.raises(SemanticLayerError) as raised:
                call()
            assert raised.value.code == "ROW_GRAIN_UNSUPPORTED"
    finally:
        runtime.close()


def test_a_not_filter_answers_and_counts_no_unknown_rows_it_cant_tell_apart(shop):
    query = {
        "select": [SCAN_COUNT],
        "where": [{"field": "dimension.shop_customer_channel", "op": "not", "value": "web"}],
    }
    result = shop.query(query)
    assert result["rows"] == [{"scan_count": 4}]
    assert "UNKNOWN_ROWS_KEPT" not in json.dumps(result, default=str)


def test_the_card_says_it_has_no_key_and_whom_it_looks_up(shop):
    card = inspect_payload(shop, object_id=SCANS)["card"]
    assert card["key"] == []
    assert card["key_note"] == (
        "No key: every stored row counts. It looks up Order; nothing can look up one of its rows."
    )
    assert card["recommended_next_actions"] == ["inspect related measures"]
    keyed = inspect_payload(shop, object_id="entity.shop_order")["card"]
    assert keyed["key"] == ["order_id"] and "key_note" not in keyed
    assert "use as grouping entity" in keyed["recommended_next_actions"]


def test_discover_offers_no_grouping_by_it(shop):
    rows = discover_payload(shop, terms="shipment scans", kinds=["entity"])["entities"]
    scans = next(row for row in rows if row["id"] == SCANS)
    assert scans["recommended_next_actions"] == ["inspect related measures"]


def test_validate_names_the_row_grain_models(tmp_path, capsys):
    args = argparse.Namespace(
        package="",
        path=str(_shop(tmp_path)),
        json=False,
        mode="parse",
        compare_path="",
        base_ref="",
    )
    project_commands.cmd_project_validate(args)
    out = capsys.readouterr().out
    assert "Row grain (no key): order_tags, room_bookings, room_rows, shipment_scans" in out
    assert "One row per key: Room" in out


def test_policies_hide_it_and_filter_its_rows(tmp_path):
    hidden = {"id": "policy.shop.no_scans", "kind": "object_visibility", "action": "hidden"}
    hidden.update(object_ids=[SCANS], roles=["analyst"])
    own = {"id": "policy.shop.own_scans", "kind": "row_filter", "dimension": SCAN_TYPE}
    own.update(attribute="scan_type", type="string", audiences=["carrier"])
    package = _shop(
        tmp_path, ("policies.yml", lambda doc: doc.update(semantic_policies=[hidden, own]))
    )
    runtime = _runtime(package)
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        analyst = context_from_policy_context({"roles": ["analyst"]})
        found = adapter.call_tool(
            "discover", {"terms": "scans", "verbosity": "full"}, request_context=analyst
        )
        assert not any(found[kind] for kind in ("measures", "dimensions", "entities"))
        refused = adapter.call_tool(
            "execute", {"query": {"select": [SCAN_COUNT]}}, request_context=analyst
        )
        assert refused["error"]["code"] == "OBJECT_NOT_FOUND"
        carrier = RequestContext(
            actor="end-user", audience="carrier", attributes={"scan_type": "pickup"}
        )
        assert runtime.query({"select": [SCAN_COUNT]}, context=carrier)["rows"] == [
            {"scan_count": 7}
        ]
        with pytest.raises(SemanticLayerError) as raised:  # a joined statement can't be filtered
            runtime.query(
                {"select": [SCAN_COUNT], "group_by": ["dimension.shop_order_store_id"]},
                context=carrier,
            )
        assert raised.value.code == "POLICY_DENIED"
    finally:
        runtime.close()


@pytest.fixture(scope="module")
def relabeled(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One-word labels, so a ranking's noun names the row-grain and composite-key entities."""
    package = _shop(
        tmp_path_factory.mktemp("relabeled"),
        _scans(lambda model: model.update(label="Scans")),
        _graph(lambda graph: graph["entities"]["account_day"].update(label="Snapshot")),
    )
    runtime = _runtime(package)
    yield runtime
    runtime.close()


@pytest.mark.parametrize(
    ("intent", "entity"), [("top 5 scans", "Scans"), ("top 2 snapshots by seats", "Snapshot")]
)
def test_ranking_rows_without_a_single_key_is_held(relabeled, intent, entity):
    plan = plan_payload(relabeled, intent=intent)
    assert plan["status"] != "ok"
    error = (plan["why"].get("errors") or [plan["why"]])[0]
    assert error["code"] == "PLAN_RANKING_NO_KEY"
    assert (
        error["message"] == f"{entity} has no single key to rank by; rank by one of its dimensions."
    )
    assert "limit" not in plan["best"]["query"]


def test_grouping_row_grain_by_its_own_column_drafts(relabeled):
    plan = plan_payload(relabeled, intent="scans by scan type")
    assert plan["status"] == "ok", plan
    assert plan["best"]["query"]["group_by"] == [SCAN_TYPE]


def test_an_authored_example_on_a_row_grain_measure_answers(tmp_path):
    example = {
        "question": "How many shipment scans were stored?",
        "query": {"select": [SCAN_COUNT]},
    }
    package = _shop(
        tmp_path, ("examples/scans.yml", lambda doc: doc.update(examples={"scans": example}))
    )
    runtime = _runtime(package)
    try:
        plan = plan_payload(runtime, intent=example["question"])
        assert plan["status"] == "ok"
        assert runtime.query(plan["best"]["query"])["rows"] == [{"scan_count": 10}]
    finally:
        runtime.close()


def test_the_writer_keeps_a_row_grain_model(tmp_path):
    source = tmp_path / "source" / "scans"
    documents = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "scans",
                "namespace": "scans",
                "default_db": "data/scans.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {"graph": {"entities": {"order": {"key": ["order_id"], "model": "orders"}}}},
        "models/orders.yml": {
            "model": {"id": "orders", "relation": "orders", "entities": {"order": {}}}
        },
        "models/shipment_scans.yml": yaml.safe_load(
            (SHOP / "models/shipment_scans.yml").read_text()
        ),
    }
    for name, document in documents.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    snapshot = load_package_snapshot(source)
    written = write_package(snapshot.config, tmp_path / "written" / "scans", namespace="scans")
    assert load_package_snapshot(written).semantic == snapshot.semantic
    assert (
        "shipment_scans"
        not in yaml.safe_load((written / "graph.yml").read_text())["graph"]["entities"]
    )


def test_ossie_import_reports_the_datasets_it_drops(tmp_path):
    document = tmp_path / "shop.yaml"
    key = {"dialects": [{"dialect": "ANSI_SQL", "expression": "order_id"}]}
    document.write_text(
        yaml.safe_dump(
            {
                "version": "0.2.0",
                "name": "shop",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "shop.orders",
                        "primary_key": ["order_id"],
                        "fields": [{"name": "order_id", "dimension": {}, "expression": key}],
                    },
                    {"name": "scans", "source": "shop.scans"},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = import_ossie(document, tmp_path / "imported")
    dropped = {row["construct"]: row["ids"] for row in report["warnings"]}
    assert dropped["datasets without a primary key or defined by a query"] == ["scans"]


def test_kind_fact_is_refused_with_the_two_ways_to_write_it(tmp_path):
    fact = _scans(lambda model: model.update(kind="fact", time_entity="default_calendar"))
    message = "give the model an entity keyed by its time column, or leave it row grain"
    assert message in str(_refusal(_shop(tmp_path, fact)))


def test_a_hidden_key_dimension_never_shrinks_a_composite_key(tmp_path):
    # A row-grain model's per-parent values over a two-column parent key, for a caller who
    # can't see the parent's account_id dimension: three parents, never merged into one.
    readings = {
        "id": "readings",
        "relation": "readings",
        "entities": {"account_day": {"expr": ["account_id", "snapshot_day"]}},
        "dimensions": {
            "account_id": {
                "column": "account_id",
                "name": "Reading owner",
                "label": "Reading owner",
            }
        },
        "measures": {"reading_amount": {"kind": "aggregate", "expr": "amount"}},
    }
    hide = {"id": "policy.shop.hide_id", "kind": "object_visibility", "action": "hidden"}
    hide.update(object_ids=["dimension.shop_account_day_account_id"], roles=["support"])
    package = _shop(
        tmp_path,
        ("models/readings.yml", lambda doc: doc.update(model=readings)),
        (
            "models/account_days.yml",
            lambda doc: doc["model"]["dimensions"].update(account_id={"label": "account_id"}),
        ),
        ("policies.yml", lambda doc: doc.update(semantic_policies=[hide])),
    )
    seed = package / "data" / "seed.sql"
    seed.write_text(
        seed.read_text(encoding="utf-8")
        + "\nCREATE TABLE readings (account_id INTEGER, snapshot_day DATE, amount INTEGER);\n"
        "INSERT INTO readings VALUES (1, DATE '2024-01-01', 1), (2, DATE '2024-01-01', 10),\n"
        "  (3, DATE '2024-01-01', 100);\n",
        encoding="utf-8",
    )
    over = {"kind": "entity_value", "entity": "entity.shop_account_day"}
    over["input"] = "measure.shop.reading_amount"
    median = {"kind": "distribution", "function": "median", "over": over}
    runtime = _runtime(package)
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        for roles in ([], ["support"]):
            result = adapter.call_tool(
                "execute",
                {"query": {"select": [{"as": "median", "expression": median}]}},
                request_context=context_from_policy_context({"roles": roles}),
            )
            if roles and not result["ok"]:
                continue  # a refusal is fine: the key needs a dimension the caller can't see
            assert result["ok"], result
            assert float(result["rows"][0]["median"]) == 10, (roles, result["rows"])
    finally:
        runtime.close()
