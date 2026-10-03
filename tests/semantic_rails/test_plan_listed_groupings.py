"""Every listed grouping has its own matching dimension before plan is ready.

The invariant: every grouping the question lists that isn't a clock term ("by month", "by order
date") or a declared value has a dimension in the draft's group_by, or plan doesn't call the draft
ready. A comma separates groupings as "and" does, so "by incident name, incident" lists two. Two
incidents can share a name; grouping by the name alone would add their costs into one row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
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
from semantic_rails.schema import DimensionConfig
from tests.semantic_rails.result_helpers import typed_rows

INCIDENT_ID = "dimension.upkeep_incident_incident_id"
INCIDENT_NAME = "dimension.upkeep_incident_incident_name"
STORE = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER_TIME = "temporal_role.jaffle_order_time"


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
    # one-to-one correspondence shows that the draft drops one.
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
        "terms": ["incident"],
        "dropped_groupings": ["incident"],
    }
    # Run anyway, it would add both incidents into one row.
    assert typed_rows(runtime.query(draft.query)) == [{INCIDENT_NAME: "Leak", "repair_cost": 30}]


def test_an_unrelated_dimension_never_masks_a_dropped_order_grouping(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="order count by customer type, order for Brooklyn store")
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            "SELECT c.customer_type, o.order_id, COUNT(DISTINCT o.order_id) "
            "FROM jaffle_order o "
            "JOIN jaffle_customer c ON o.customer_id = c.customer_id "
            "JOIN jaffle_store s ON o.store_id = s.store_id "
            "WHERE s.store_name = 'Brooklyn' GROUP BY 1, 2"
        ).fetchall()
    finally:
        connection.close()
    assert len(reference) == 21_465
    assert {row[2] for row in reference} == {1}
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["order"]
    assert payload["best"]["query_ir"]["group_by"] == [STORE, CUSTOMER_TYPE]
    assert len(typed_rows(jaffle.query(payload["best"]["query_ir"]))) == 2


def test_a_forced_store_grouping_cannot_stand_in_for_order(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = "order count by customer type, order for Brooklyn store"
    draft = RuntimeCompositionDraft(
        query={
            "version": 2,
            "select": [
                {"as": "order_count", "expression": {"measure": "measure.jaffle.order_count"}}
            ],
            "group_by": [STORE, CUSTOMER_TYPE],
            "where": [{"field": STORE, "op": "=", "value": "Brooklyn"}],
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
    assert unconsumed_catalog_words(jaffle, intent, draft.query) == []
    payload = plan_payload(jaffle, intent=intent)
    assert payload["best"]["validation_ok"] is True
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload["next"].get("ready_for", [])
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["dropped_groupings"] == ["order"]


@pytest.mark.parametrize(
    ("intent", "status", "groups", "window"),
    [
        ("revenue by store, last month", "ok", [STORE], "month"),
        ("revenue by month, last year", "ok", [], "year"),
        ("revenue by store, sorted by revenue", "low_confidence", [STORE], None),
    ],
)
def test_a_comma_before_a_trailing_clause_keeps_the_original_plan(
    jaffle: Runtime, intent: str, status: str, groups: list[str], window: str | None
) -> None:
    payload = plan_payload(jaffle, intent=intent)
    assert payload["status"] == status, payload.get("why")
    query = payload["best"]["query_ir"]
    assert query.get("group_by", []) == groups
    # The grouping clause ends at the comma, including for a held sort request.
    assert _requested_grouping_terms(intent, config=jaffle._config) == [
        "month" if "by month" in intent else "store"
    ]
    if window is None:
        assert not query.get("time")
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert payload["why"]["details"]["terms"] == ["sorted"]
        return
    assert "execute" in payload["next"]["ready_for"]
    assert query["time"] == {
        "temporal_role": ORDER_TIME,
        "grain": "month",
        "range": {"last": {"unit": window, "value": 1}},
    }
    rows = typed_rows(jaffle.query({**query, "policy_context": {"now": "2017-04-15"}}))
    fields = [*groups, f"{ORDER_TIME}__month"]
    group_sql = "s.store_name, " if groups else ""
    start, end = ("2017-03-01", "2017-04-01") if window == "month" else ("2016-01-01", "2017-01-01")
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            f"SELECT {group_sql}DATE_TRUNC('month', o.ordered_at), "
            "SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
            "JOIN jaffle_store s ON o.store_id = s.store_id "
            "WHERE o.ordered_at >= ? AND o.ordered_at < ? "
            + ("GROUP BY 1, 2 ORDER BY 1, 2" if groups else "GROUP BY 1 ORDER BY 1"),
            [start, end],
        ).fetchall()
    finally:
        connection.close()
    assert reference
    actual = sorted(
        tuple(row[field] for field in fields) + (float(row["revenue_usd"]),) for row in rows
    )
    assert [row[:-1] for row in actual] == [row[:-1] for row in reference]
    assert [row[-1] for row in actual] == pytest.approx([float(row[-1]) for row in reference])


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


_UNITS = ("day", "week", "month", "quarter", "year")
_GRAIN_PHRASES = [
    (phrase.format(unit), unit)
    for phrase in ("{} level", "at {} grain", "at {} level")
    for unit in _UNITS
]
# Each measure's output name, and its reference SQL over jaffle_order.
_REVENUE = ("revenue_usd", "SUM(o.order_total_cents / 100.0)")
_ORDERS = ("order_count", "COUNT(DISTINCT o.order_id)")
_AOV = ("aov_usd", "SUM(o.order_total_cents / 100.0) / COUNT(DISTINCT o.order_id)")
_YEAR_2017 = {"start": "2017-01-01", "end": "2018-01-01"}


@pytest.mark.parametrize(
    ("intent", "group_by", "measure", "grain", "window"),
    [
        *(
            (shape.format(phrase), group_by, measure, unit, {})
            for shape, group_by, measure in [
                ("revenue by {}", [], _REVENUE),
                ("order count by {}", [], _ORDERS),
                ("average order value by {}", [], _AOV),
                ("revenue by store and {}", [STORE], _REVENUE),
                ("revenue by store, {}", [STORE], _REVENUE),
                ("revenue by customer type and {}", [CUSTOMER_TYPE], _REVENUE),
            ]
            for phrase, unit in _GRAIN_PHRASES
        ),
        # "Order date" names the order clock, at the grain the phrase sets.
        *(
            (f"revenue by order date{joint}{phrase}", [], _REVENUE, unit, {})
            for joint in (", ", " and ")
            for phrase, unit in [
                *((f"at {unit} grain", unit) for unit in _UNITS),
                ("day level", "day"),
                ("at day level", "day"),
            ]
        ),
        ("revenue by month level for 2017", [], _REVENUE, "month", _YEAR_2017),
        ("revenue in 2017 by month level", [], _REVENUE, "month", _YEAR_2017),
        ("revenue by store by month level", [STORE], _REVENUE, "month", {}),
        ("revenue by month level and store", [STORE], _REVENUE, "month", {}),
        ("revenue by week level, store", [STORE], _REVENUE, "week", {}),
    ],
)
def test_a_grain_phrase_is_the_clocks_grain(
    jaffle: Runtime,
    intent: str,
    group_by: list[str],
    measure: tuple[str, str],
    grain: str,
    window: dict[str, str],
) -> None:
    payload = plan_payload(jaffle, intent=intent)

    # "At week grain" or "month level" names no grouping of its own: the time block buckets the
    # measure's own clock, Order time, at that grain.
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    query = payload["best"]["query_ir"]
    assert query.get("group_by", []) == group_by
    assert query["time"] == {"temporal_role": ORDER_TIME, "grain": grain, **window}
    alias, total = measure
    assert [select["as"] for select in query["select"]] == [alias]
    bucket = f"{ORDER_TIME}__{grain}"
    rows = sorted(
        (*(str(row[dim]) for dim in group_by), str(row[bucket])[:10], float(row[alias]))
        for row in typed_rows(jaffle.query(query))
    )
    columns = [{STORE: "s.store_name", CUSTOMER_TYPE: "c.customer_type"}[dim] for dim in group_by]
    where = "WHERE o.ordered_at >= ? AND o.ordered_at < ?" if window else ""
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            f"SELECT {''.join(f'{column}, ' for column in columns)}"
            f"CAST(DATE_TRUNC('{grain}', o.ordered_at) AS DATE), {total} "
            "FROM jaffle_order o LEFT JOIN jaffle_store s ON o.store_id = s.store_id "
            f"LEFT JOIN jaffle_customer c ON o.customer_id = c.customer_id {where} GROUP BY ALL",
            [window["start"], window["end"]] if window else [],
        ).fetchall()
    finally:
        connection.close()
    expected = sorted((*(str(key) for key in row[:-1]), float(row[-1])) for row in reference)
    assert rows
    assert [row[:-1] for row in rows] == [row[:-1] for row in expected]
    assert [row[-1] for row in rows] == pytest.approx([row[-1] for row in expected])


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
    jaffle: Runtime, parse: Callable[[str], list[str]], intent: str, terms: list[str]
) -> None:
    actual = (
        _requested_grouping_terms(intent, config=jaffle._config)
        if parse is _requested_grouping_terms
        else parse(intent)
    )
    assert actual == terms
    assert [
        intent[start:end] for start, end in _requested_grouping_spans(intent, config=jaffle._config)
    ] == _requested_grouping_terms(intent, config=jaffle._config)


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        ("revenue by store, the customer type", ["store", "the customer type"]),
        ("revenue by store, order date", ["store", "order date"]),
        ("revenue by store, at week grain", ["store", "at week grain"]),
        ("revenue by store, 2017", ["store"]),
        ("revenue by store, nonsense, customer type", ["store"]),
        ("revenue by store, last month and customer type", ["store"]),
    ],
)
def test_only_a_named_grouping_continues_past_a_comma(
    jaffle: Runtime, intent: str, terms: list[str]
) -> None:
    assert _requested_grouping_terms(intent, config=jaffle._config) == terms
    assert [
        intent[start:end] for start, end in _requested_grouping_spans(intent, config=jaffle._config)
    ] == terms


@pytest.mark.parametrize(
    ("term", "changes", "matched"),
    [
        ("the case", {"label": "Case"}, True),
        ("case", {"id": "dimension.case"}, True),
        ("case", {"name": "support.case"}, True),
        ("case", {"aliases": ["Case"]}, True),
        ("case", {"label": "Showcase"}, False),
        ("case number", {"label": "Case"}, False),
        ("case", {"description": "Case"}, False),
        ("case", {"topics": ["Case"]}, False),
        ("purchase", {"label": "Order"}, False),
        ("sent", {"label": "Received"}, False),
    ],
)
def test_grouping_correspondence_uses_only_declared_name_words(
    upkeep: Callable[[str, str], Runtime], term: str, changes: dict[str, Any], matched: bool
) -> None:
    runtime = upkeep("incident", "repair")
    dimension = replace(
        DimensionConfig(
            "dimension.neutral", runtime._config.entities[0].id, "incident_name", "string"
        ),
        **changes,
    )
    runtime._config = replace(runtime._config, dimensions=[dimension])
    query = {"group_by": [dimension.id]}
    why = plan_module._dropped_grouping_why(runtime, f"repair cost by {term}", query)
    assert (why is None) is matched
    assert _requested_grouping_terms(
        f"repair cost by incident, {term}", config=runtime._config
    ) == (["incident", term] if matched else ["incident"])
    if why:
        assert why["code"] == "PLAN_UNMATCHED_TERMS"
        assert why["details"]["dropped_groupings"] == [term]


@pytest.mark.parametrize("column", ["incident_id", "incident_name"])
def test_an_entity_name_matches_its_declared_key_only(
    upkeep: Callable[[str, str], Runtime], column: str
) -> None:
    runtime = upkeep("incident", "repair")
    dimension = DimensionConfig(
        "dimension.reference", runtime._config.entities[0].id, column, "string"
    )
    runtime._config = replace(runtime._config, dimensions=[dimension])
    why = plan_module._dropped_grouping_why(
        runtime, "repair cost by incident", {"group_by": [dimension.id]}
    )
    assert (why is None) is (column == "incident_id")


def test_repeating_one_dimension_never_satisfies_two_listed_groupings(
    upkeep: Callable[[str, str], Runtime],
) -> None:
    runtime = upkeep("incident", "repair")
    why = plan_module._dropped_grouping_why(
        runtime,
        "repair cost by incident name, incident",
        {"group_by": [INCIDENT_NAME, INCIDENT_NAME, "dimension.unknown"]},
    )
    assert why is not None
    assert why["details"]["dropped_groupings"] == ["incident"]
