"""plan never answers with fewer groupings than the question lists.

The invariant: every grouping the question lists that isn't a clock term ("by month", "by order
date") or a declared value has a dimension in the draft's group_by, or plan doesn't call the draft
ready. A comma separates groupings as "and" does, so "by incident name, incident" lists two. Two
incidents can share a name; grouping by the name alone would add their costs into one row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import generators, plan_payload
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner._base import (
    RuntimeCompositionDraft,
    _requested_grouping_spans,
    _requested_grouping_terms,
)
from semantic_rails.planner.faithfulness import unconsumed_catalog_words
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.runtime import Runtime
from tests.semantic_rails.result_helpers import typed_rows

INCIDENT_ID = "dimension.upkeep_incident_incident_id"
INCIDENT_NAME = "dimension.upkeep_incident_incident_name"
STORE = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"


def _upkeep(path: Path, noun: str, measure: str) -> Runtime:
    """An entity ``noun`` labelled ``noun.title()``, with dimensions "<Noun> id" and "<Noun>
    name", a measure "<Measure> cost" summing ``<measure>_cost``, and two rows that share a name:
    (1, "Leak", 10) and (2, "Leak", 20)."""

    title = noun.title()
    (path / "models").mkdir(parents=True)
    (path / "data" / "csv").mkdir(parents=True)
    (path / "data" / "csv" / f"{noun}s.csv").write_text(
        f"{noun}_id,{noun}_name,reported_at,{measure}_cost\n"
        "1,Leak,2026-01-01T09:00:00,10\n"
        "2,Leak,2026-01-02T09:00:00,20\n",
        encoding="utf-8",
    )
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "upkeep",
                "namespace": "upkeep",
                "name": "upkeep",
                "warehouse": "duckdb",
                "default_db": "data/upkeep.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/csv"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {noun: {"key": [f"{noun}_id"], "model": f"{noun}s", "label": title}}
            }
        },
        f"models/{noun}s.yml": {
            "model": {
                "id": f"{noun}s",
                "relation": f"{noun}s",
                "entities": {noun: {}},
                "times": {
                    "reported_at": {"column": "reported_at", "kind": "timestamp", "default": True}
                },
                "dimensions": {
                    f"{noun}_id": {"column": f"{noun}_id", "label": f"{title} id"},
                    f"{noun}_name": {"column": f"{noun}_name", "label": f"{title} name"},
                },
                "measures": {
                    f"{measure}_cost": {
                        "label": f"{measure.title()} cost",
                        "kind": "aggregate",
                        "expr": f"{measure}_cost",
                        "default_agg": "sum",
                    }
                },
            }
        },
    }
    for name, body in files.items():
        (path / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return Runtime.from_path(str(path))


@pytest.fixture()
def upkeep(tmp_path: Path) -> Iterator[Callable[[str, str], Runtime]]:
    opened: list[Runtime] = []

    def build(noun: str, measure: str) -> Runtime:
        opened.append(_upkeep(tmp_path / noun, noun, measure))
        return opened[-1]

    try:
        yield build
    finally:
        for runtime in opened:
            runtime.close()


@pytest.fixture()
def jaffle(runtime_factory: Any) -> Iterator[Runtime]:
    runtime = runtime_factory("jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


def _incident_reference() -> list[tuple[int, str, float]]:
    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TABLE incidents AS SELECT * FROM (VALUES "
            "(1, 'Leak', 10), (2, 'Leak', 20)) AS t(incident_id, incident_name, repair_cost)"
        )
        rows = connection.execute(
            "SELECT incident_id, incident_name, SUM(repair_cost) FROM incidents "
            "GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
    finally:
        connection.close()
    return [(int(key), str(name), float(cost)) for key, name, cost in rows]


@pytest.mark.parametrize(
    ("intent", "group_by"),
    [
        ("repair cost by incident name, incident", [INCIDENT_NAME, INCIDENT_ID]),
        ("repair cost by incident, incident name", [INCIDENT_ID, INCIDENT_NAME]),
        ("repair cost by incident name and incident", [INCIDENT_NAME, INCIDENT_ID]),
    ],
)
def test_two_listed_groupings_never_add_up_one_name(
    upkeep: Callable[[str, str], Runtime], intent: str, group_by: list[str]
) -> None:
    runtime = upkeep("incident", "repair")
    payload = plan_payload(runtime, intent=intent)

    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    assert query["group_by"] == group_by
    rows = [
        (int(row[INCIDENT_ID]), str(row[INCIDENT_NAME]), float(row["repair_cost"]))
        for row in typed_rows(runtime.query(query))
    ]
    assert sorted(rows) == _incident_reference()


def test_a_draft_that_drops_a_listed_grouping_is_not_ready(
    upkeep: Callable[[str, str], Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = upkeep("incident", "repair")
    intent = "repair cost by incident name, incident"
    # Every word of the question is consumed: "incident" by Incident name's own words. Only the
    # count of groupings shows that the draft drops one.
    draft = RuntimeCompositionDraft(
        query={
            "version": 2,
            "select": [
                {"as": "repair_cost", "expression": {"metric": "metric.upkeep.repair_cost"}}
            ],
            "group_by": [INCIDENT_NAME],
        },
        resolved=[],
        rationale=[],
        interpreted_intent={},
    )
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, text: CompositionResult(
            intent_ir=parse_intent(runtime, text), draft=draft, pattern="test"
        ),
    )
    assert unconsumed_catalog_words(runtime, intent, draft.query) == []

    payload = plan_payload(runtime, intent=intent)

    assert payload["status"] == "low_confidence"
    assert payload["best"]["validation_ok"] is True
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"] == {
        "terms": ["incident name", "incident"],
        "dropped_groupings": ["incident name", "incident"],
    }
    # Run anyway, it would add both incidents into one row.
    assert typed_rows(runtime.query(draft.query)) == [{INCIDENT_NAME: "Leak", "repair_cost": 30}]


def test_a_grouping_named_by_the_measure_word_is_not_dropped(
    upkeep: Callable[[str, str], Runtime],
) -> None:
    runtime = upkeep("repair", "repair")
    payload = plan_payload(runtime, intent="repair cost by repair")

    # The draft reads "repair" as the measure's word and groups by nothing: one total, 30.
    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["repair"]
    assert not payload["best"]["query_ir"].get("group_by")


def test_a_label_only_grouping_stays_ready(upkeep: Callable[[str, str], Runtime]) -> None:
    runtime = upkeep("incident", "repair")
    payload = plan_payload(runtime, intent="repair cost by incident name")

    # The question asks for the name only, so one row per name is its answer.
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert query["group_by"] == [INCIDENT_NAME]
    assert typed_rows(runtime.query(query)) == [{INCIDENT_NAME: "Leak", "repair_cost": 30}]


def test_a_geo_entity_keeps_its_plan(upkeep: Callable[[str, str], Runtime]) -> None:
    runtime = upkeep("geo", "geo")
    payload = plan_payload(runtime, intent="geo cost by geo")

    assert payload["status"] == "ok", payload.get("why")
    assert payload["best"]["query_ir"] == {
        "version": 2,
        "select": [{"as": "geo_cost", "expression": {"metric": "metric.upkeep.geo_cost"}}],
        "group_by": ["dimension.upkeep_geo_geo_id"],
        "order_by": [{"field": "dimension.upkeep_geo_geo_id", "direction": "ASC"}],
    }


@pytest.mark.parametrize(
    ("intent", "group_by", "grain"),
    [("revenue by store", [STORE], None), ("revenue by month", None, "month")],
)
def test_a_single_grouping_stays_ready(
    jaffle: Runtime, intent: str, group_by: list[str] | None, grain: str | None
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert query.get("group_by") == group_by
    assert (query.get("time") or {}).get("grain") == grain


def test_a_comma_lists_groupings_as_and_does(jaffle: Runtime) -> None:
    comma = plan_payload(jaffle, intent="revenue by store, customer type")
    conjunction = plan_payload(jaffle, intent="revenue by store and customer type")

    assert comma["best"]["query_ir"] == conjunction["best"]["query_ir"]
    assert comma["status"] == conjunction["status"] == "ok", comma.get("why")
    query = comma["best"]["query_ir"]
    assert query["group_by"] == [STORE, CUSTOMER_TYPE]
    [select] = query["select"]
    rows = sorted(
        (str(row[STORE]), str(row[CUSTOMER_TYPE]), float(row[select["as"]]))
        for row in typed_rows(jaffle.query(query))
    )
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
            "JOIN jaffle_customer c ON o.customer_id = c.customer_id "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    finally:
        connection.close()
    assert [row[:2] for row in rows] == [(str(store), str(kind)) for store, kind, _ in reference]
    assert [row[2] for row in rows] == pytest.approx([float(total) for *_, total in reference])


@pytest.mark.parametrize("parse", [_requested_grouping_terms, generators._requested_grouping_terms])
@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        (
            "revenue by store, customer type, and product type",
            ["store", "customer type", "product type"],
        ),
        (
            "revenue by store, customer type and product type",
            ["store", "customer type", "product type"],
        ),
        ("revenue by store,customer type", ["store", "customer type"]),
        ("revenue by store; customer type", ["store"]),
        ("revenue by store, customer type for 2017", ["store", "customer type"]),
    ],
)
def test_a_comma_separates_grouping_terms(
    parse: Callable[[str], list[str]], intent: str, terms: list[str]
) -> None:
    assert parse(intent) == terms
    assert [intent[start:end] for start, end in _requested_grouping_spans(intent)] == (
        _requested_grouping_terms(intent)
    )
