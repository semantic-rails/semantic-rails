"""Additional grouping spellings can only hold a plan, never change its answer."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import (
    RuntimeCompositionDraft,
    _listed_grouping_terms,
    _named_grouping_spans,
    _named_grouping_terms,
)
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.runtime import Runtime
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_unasked_groupings import _CASES
from tests.semantic_rails.test_plan_value_lists import _force_fallback

STORE = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
STORE_ID = "dimension.retail_store_id"
STORE_NAME = "dimension.retail_store_name"
STORE_LABEL = "dimension.retail_store_label"
ID_FILTER = {"field": STORE_ID, "op": "IN", "value": ["a", "b", "c"]}
NAME_FILTER = {"field": STORE_NAME, "op": "IN", "value": ["Central", "Harbor"]}
JAFFLE_FILTER = {"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}


@pytest.fixture()
def jaffle(runtime_factory: Any) -> Iterator[Runtime]:
    runtime = runtime_factory("jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def retail(tmp_path: Path) -> Iterator[Runtime]:
    (tmp_path / "models").mkdir()
    (tmp_path / "data" / "csv").mkdir(parents=True)
    (tmp_path / "data" / "csv" / "stores.csv").write_text(
        "store_id,store_name,store_label,reported_at,revenue\n"
        "a,Central,East,2026-01-01T09:00:00,10\n"
        "b,Central,West,2026-01-02T09:00:00,25\n"
        "c,Harbor,South,2026-02-01T09:00:00,7\n",
        encoding="utf-8",
    )
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "retail",
                "namespace": "retail",
                "name": "Retail",
                "warehouse": "duckdb",
                "default_db": "data/retail.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/csv"},
            },
        },
        "graph.yml": {"graph": {"entities": {"store": {"key": ["store_id"], "model": "stores"}}}},
        "models/stores.yml": {
            "model": {
                "id": "stores",
                "relation": "stores",
                "entities": {"store": {}},
                "times": {
                    "reported_at": {"column": "reported_at", "kind": "timestamp", "default": True}
                },
                "dimensions": {
                    "store_id": {
                        "as": STORE_ID,
                        "label": "Store id",
                        "synonyms": ["Store key", "Store number", "Store code"],
                    },
                    "store_name": {"as": STORE_NAME, "label": "Store name"},
                    "store_label": {"as": STORE_LABEL, "label": "Store label"},
                },
                "measures": {
                    "revenue": {
                        "kind": "aggregate",
                        "expr": "revenue",
                        "label": "Revenue",
                        "default_agg": "sum",
                    }
                },
            }
        },
    }
    for name, body in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


_PHRASINGS = [
    "by, {noun}",
    "by\t{noun}",
    "by\n{noun}",
    "per {noun}",
    "for each {noun}",
    "each {noun}",
    "every {noun}",
    "at {noun} level",
    "at the {noun} level",
    "{noun} level",
    "at {noun} grain",
    "{noun} grain",
    "grouped by\t{noun}",
    "group by\n{noun}",
]


@pytest.mark.parametrize("phrasing", _PHRASINGS)
def test_each_spelling_records_the_exact_named_term(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, phrasing: str
) -> None:
    question = "revenue " + phrasing.format(noun="customer type")
    spans = _named_grouping_spans(question, jaffle._config)
    assert [question[start:end] for start, end in spans] == ["customer type"]
    assert _named_grouping_terms(question, jaffle._config) == ["customer type"]
    _compare_base(jaffle, monkeypatch, question)


@pytest.mark.parametrize(
    ("question", "terms"),
    [
        ("revenue by, store name, customer type", ["store name", "customer type"]),
        ("revenue by\tstore name and\ncustomer type", ["store name", "customer type"]),
        ("revenue per store name & customer type", ["store name", "customer type"]),
        (
            "revenue for each store name, last month and customer type",
            ["store name", "customer type"],
        ),
        ("revenue by store name; by customer type", ["store name", "customer type"]),
        ("revenue by store name per month", ["store name", "month"]),
        ("highest 5 stores by revenue", ["stores"]),
        ("lowest five stores by revenue", ["stores"]),
        ("top 5 stores, customers by revenue", ["stores", "customers"]),
        ("revenue by, store name, nonsense, customer type", ["store name"]),
        ("revenue at mystery level", ["mystery"]),
        ("revenue per mystery", ["mystery"]),
        ("revenue byproduct", []),
        ("revenue for each of the last 3 months", []),
        ("revenue at the level", []),
    ],
)
def test_clause_grammar_keeps_lists_windows_and_ranked_nouns(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, question: str, terms: list[str]
) -> None:
    spans = _named_grouping_spans(question, jaffle._config)
    assert [" ".join(question[start:end].split()) for start, end in spans] == terms
    legacy = _listed_grouping_terms(question, jaffle._config)
    assert _named_grouping_terms(question, jaffle._config)[: len(legacy)] == legacy
    _compare_base(jaffle, monkeypatch, question)


def _compare_base(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    partial: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    after = plan_payload(runtime, intent=question, partial_query=partial)
    with monkeypatch.context() as base:
        base.setattr(plan_module, "_named_grouping_terms", _listed_grouping_terms)
        before = plan_payload(runtime, intent=question, partial_query=partial)
    # Both paths generate exactly the same draft. Only readiness may change.
    assert after["best"] == before["best"]
    if before["status"] == "ok" and after["status"] != "ok":
        assert after["status"] == "low_confidence"
        assert after["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert "execute" not in after["next"].get("ready_for", [])
    else:
        assert after["status"] == before["status"]
        if before["status"] == "ok":
            assert after.get("why") == before.get("why")
            assert after["next"] == before["next"]
        else:
            assert "execute" not in after["next"].get("ready_for", [])
    return before, after


@pytest.mark.parametrize("path", ["pattern", "fallback"])
@pytest.mark.parametrize("phrasing", _PHRASINGS)
def test_a_filter_never_stands_in_for_a_dropped_grouping(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, phrasing: str
) -> None:
    question = "revenue " + phrasing.format(noun="customer type")
    filters = [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}]

    def force(grouped: bool) -> None:
        draft = RuntimeCompositionDraft(
            query={
                "version": 2,
                "select": [
                    {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
                ],
                "group_by": [CUSTOMER_TYPE] if grouped else [],
                "where": filters,
            },
            resolved=[],
            rationale=[],
            interpreted_intent={},
        )
        monkeypatch.setattr(
            plan_module,
            "compose",
            lambda runtime, text: CompositionResult(
                intent_ir=parse_intent(runtime, text),
                draft=draft if path == "pattern" else None,
                pattern="test" if path == "pattern" else "",
            ),
        )
        monkeypatch.setattr(
            plan_module, "fallback_drafts", lambda *args, **kwargs: [(draft, "catalog_fallback")]
        )

    force(True)
    before, complete = _compare_base(jaffle, monkeypatch, question, {"group_by": [CUSTOMER_TYPE]})
    assert complete["status"] == before["status"]
    if "grain" in phrasing:
        # A non-clock grain phrase is already held by lexical coverage. The
        # refusal-only reader must preserve that hold, even for a complete IR.
        assert complete["status"] == "low_confidence"
    else:
        assert complete["status"] == "ok", complete.get("why")
        assert "execute" in complete["next"]["ready_for"]
    query = complete["best"]["query_ir"]
    assert plan_module._dropped_grouping_why(jaffle, question, query) is None
    actual = typed_rows(jaffle.query(query))
    assert len(actual) == 2
    assert actual == typed_rows(jaffle.query(before["best"]["query_ir"]))
    jaffle.close()
    with duckdb.connect(jaffle.db_path, read_only=True) as connection:
        reference = dict(
            connection.execute(
                "SELECT c.customer_type, SUM(o.order_total_cents / 100.0) "
                "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
                "JOIN jaffle_store s USING (store_id) WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
                "AND c.customer_type IN ('new', 'repeat') GROUP BY 1"
            ).fetchall()
        )
    assert {row[CUSTOMER_TYPE]: row["revenue_usd"] for row in actual} == pytest.approx(reference)

    force(False)
    _, dropped = _compare_base(jaffle, monkeypatch, question)
    assert dropped["best"]["validation_ok"] is True
    assert dropped["status"] == "low_confidence"
    assert dropped["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    why = plan_module._dropped_grouping_why(jaffle, question, dropped["best"]["query_ir"])
    assert why is not None
    assert why["details"]["dropped_groupings"] == ["customer type"]
    if "grain" not in phrasing:
        assert dropped["why"]["details"]["dropped_groupings"] == ["customer type"]
    assert "execute" not in dropped["next"].get("ready_for", [])


@pytest.mark.parametrize(
    "clause",
    [
        "by, store name and store id",
        "by\tstore name, store id",
        "by\nstore name & store id",
        "per store name and store id",
        "for each store name and store id",
        "each store name, store id",
        "every store name and store id",
        "by store name; by store id",
    ],
)
def test_a_second_named_term_cannot_be_discharged_by_a_filter(
    retail: Runtime, monkeypatch: pytest.MonkeyPatch, clause: str
) -> None:
    question = "revenue " + clause
    partial = {"where": [NAME_FILTER, ID_FILTER], "group_by": [STORE_NAME, STORE_ID]}
    before, complete = _compare_base(retail, monkeypatch, question, partial)
    assert before["status"] == complete["status"] == "ok", complete.get("why")
    query = complete["best"]["query_ir"]
    assert plan_module._dropped_grouping_why(retail, question, query) is None
    actual = typed_rows(retail.query(query))
    alias = query["select"][0]["as"]
    assert sorted((row[STORE_ID], row[STORE_NAME], row[alias]) for row in actual) == [
        ("a", "Central", 10),
        ("b", "Central", 25),
        ("c", "Harbor", 7),
    ]
    retail.close()
    with duckdb.connect(retail.db_path, read_only=True) as connection:
        reference = connection.execute(
            "SELECT store_id, store_name, SUM(revenue) FROM stores GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
    assert sorted((row[STORE_ID], row[STORE_NAME], row[alias]) for row in actual) == reference
    incomplete = {**query, "group_by": [STORE_NAME]}
    why = plan_module._dropped_grouping_why(retail, question, incomplete)
    assert why is not None
    assert why["code"] == "PLAN_UNMATCHED_TERMS"
    assert why["details"]["dropped_groupings"] == ["store id"]


# Existing temporal, ranking, qualification and store questions remain holds or
# keep exactly the same executable draft; the added reader cannot grant readiness.
@pytest.mark.parametrize("question", [case.intent for case in _CASES])
def test_existing_question_outcomes_only_gain_holds(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    _compare_base(jaffle, monkeypatch, question)


_STORE_CASES = [
    ("revenue by store id", [ID_FILTER]),
    ("revenue by store key", []),
    ("revenue by store number", []),
    ("revenue by store code", []),
    ("revenue by store name", [NAME_FILTER]),
    (
        "revenue by store label",
        [{"field": STORE_LABEL, "op": "IN", "value": ["East", "West", "South"]}],
    ),
    ("revenue by store", []),
    ("revenue by stores", [ID_FILTER]),
    ("revenue by, store name and store id", [NAME_FILTER, ID_FILTER]),
    ("revenue at the store id level", [ID_FILTER]),
    ("revenue by the store name", [NAME_FILTER]),
    ("revenue by store name per month", [NAME_FILTER]),
    ("revenue by store name sorted by revenue", [NAME_FILTER]),
    ("revenue by store mystery", [ID_FILTER]),
    ("revenue by store id name", []),
    ("revenue by store id and store name", [ID_FILTER, NAME_FILTER]),
    *[
        ("revenue " + form.format(noun=noun), [ID_FILTER if noun == "store id" else NAME_FILTER])
        for form in _PHRASINGS
        for noun in ["store id", "store name"]
    ],
]


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(("question", "filters"), _STORE_CASES)
def test_store_question_outcomes_only_gain_holds(
    retail: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    question: str,
    filters: list[dict[str, Any]],
) -> None:
    _force_fallback(retail, monkeypatch, question, path)
    _compare_base(retail, monkeypatch, question, {"where": filters})


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    "question",
    [
        *("revenue " + form.format(noun="store name") for form in _PHRASINGS),
        "revenue by the store name",
        "revenue by store name per month",
        "revenue by store name sorted by revenue",
        "revenue by customer type, store name",
        "revenue by customer type; by store name",
    ],
)
def test_filtered_store_controls_only_gain_holds(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, question: str
) -> None:
    _force_fallback(jaffle, monkeypatch, question, path)
    _compare_base(jaffle, monkeypatch, question, {"where": [JAFFLE_FILTER]})


@pytest.mark.parametrize("term", ["store id", "store key", "store number", "store code"])
@pytest.mark.parametrize("change", ["ambiguous", "missing"])
def test_store_key_catalog_controls_only_gain_holds(
    retail: Runtime, monkeypatch: pytest.MonkeyPatch, term: str, change: str
) -> None:
    dimensions = [
        dim for dim in retail._config.dimensions if change != "missing" or dim.id != STORE_ID
    ]
    if change == "ambiguous":
        dimensions = [
            replace(dim, aliases=[*(dim.aliases or []), term]) if dim.id == STORE_NAME else dim
            for dim in dimensions
        ]
    retail._config = replace(retail._config, dimensions=dimensions)
    before, after = _compare_base(retail, monkeypatch, f"revenue by {term}")
    if change == "missing":
        assert before["status"] == after["status"] == "low_confidence"


@pytest.mark.parametrize("separator", [" and ", " & ", ", "])
def test_complete_suffix_list_matches_reference_sql(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    question = f"revenue at customer type{separator}store name level"
    partial = {
        "group_by": [STORE, CUSTOMER_TYPE],
        "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}],
    }
    before, complete = _compare_base(jaffle, monkeypatch, question, partial)
    assert before["status"] == complete["status"] == "ok", complete.get("why")
    assert "execute" in complete["next"]["ready_for"]
    query = complete["best"]["query_ir"]
    with duckdb.connect(":memory:") as connection:
        seed_path = str(jaffle.db_path).replace("'", "''")
        connection.execute(f"ATTACH '{seed_path}' AS seed (READ_ONLY)")
        for table in ["jaffle_order", "jaffle_customer", "jaffle_store"]:
            connection.execute(f"CREATE TABLE {table} AS SELECT * FROM seed.{table}")
        reference = connection.execute(
            "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
            "JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
            "AND c.customer_type IN ('new', 'repeat') GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        cursor = connection.execute(jaffle.compile(query)["rendered_sql"])
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        actual = [(row[STORE], row[CUSTOMER_TYPE], row[query["select"][0]["as"]]) for row in rows]
    assert len(actual) == len(reference) == 4
    assert sorted(actual) == reference
    assert [row[2] for row in reference] == pytest.approx([90.48, 259334.37, 6.36, 486461.82])
    assert _named_grouping_terms(question, jaffle._config) == ["customer type", "store name"]


@pytest.mark.parametrize("separator", [" and ", " & ", ", "])
@pytest.mark.parametrize("missing", [STORE, CUSTOMER_TYPE])
def test_suffix_list_holds_when_either_grouping_is_dropped(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, separator: str, missing: str
) -> None:
    question = f"revenue at customer type{separator}store name level"
    filters = [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}]
    _, native = _compare_base(jaffle, monkeypatch, question, {"where": filters})
    assert "execute" not in native["next"].get("ready_for", [])
    draft = RuntimeCompositionDraft(
        query={
            "version": 2,
            "select": [
                {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
            ],
            "group_by": [item for item in [STORE, CUSTOMER_TYPE] if item != missing],
            "where": filters,
        },
        resolved=[],
        rationale=[],
        interpreted_intent={},
    )
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, text: CompositionResult(intent_ir=parse_intent(runtime, text), draft=draft),
    )
    _, dropped = _compare_base(jaffle, monkeypatch, question)
    assert dropped["best"]["validation_ok"] is True
    assert dropped["status"] == "low_confidence"
    assert dropped["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert dropped["why"]["details"]["dropped_groupings"] == [
        "store name" if missing == STORE else "customer type"
    ]
    assert "execute" not in dropped["next"].get("ready_for", [])


def test_repeated_description_of_one_grouping_stays_ready(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, complete = _compare_base(
        jaffle, monkeypatch, "revenue at store name level for each store"
    )
    assert before["status"] == complete["status"] == "ok", complete.get("why")
    assert "execute" in complete["next"]["ready_for"]
    assert typed_rows(jaffle.query(complete["best"]["query_ir"])) == typed_rows(
        jaffle.query(before["best"]["query_ir"])
    )
    _, dropped = _compare_base(
        jaffle,
        monkeypatch,
        "revenue by store name and customer type",
        {"group_by": [STORE], "where": [JAFFLE_FILTER]},
    )
    assert "execute" not in dropped["next"].get("ready_for", [])


@pytest.mark.parametrize(
    ("noun", "dimension"), [("severity level", STORE_ID), ("level", STORE_NAME)]
)
def test_level_in_a_dimension_name_stays_ready(
    retail: Runtime, monkeypatch: pytest.MonkeyPatch, noun: str, dimension: str
) -> None:
    retail._config = replace(
        retail._config,
        dimensions=[
            replace(row, label="Severity level", name="severity_level", aliases=[])
            if row.id == STORE_ID
            else replace(row, label="Level", name="level", aliases=[])
            if row.id == STORE_NAME
            else row
            for row in retail._config.dimensions
        ],
        measures=[
            replace(
                row,
                label="Count",
                name="count",
                default_aggregation="count",
                allowed_aggregations=["count"],
            )
            for row in retail._config.measures
        ],
    )
    before, complete = _compare_base(
        retail, monkeypatch, f"count by {noun}", {"group_by": [dimension]}
    )
    assert before["status"] == complete["status"] == "ok", complete.get("why")
    assert "execute" in complete["next"]["ready_for"]
    assert typed_rows(retail.query(complete["best"]["query_ir"])) == typed_rows(
        retail.query(before["best"]["query_ir"])
    )


@pytest.mark.parametrize(
    ("question", "terms"),
    [
        ("revenue by store name and store name", ["store name", "store name"]),
        ("revenue per mystery each mystery", ["mystery", "mystery"]),
        ("revenue per store name each store", ["store name", "store"]),
    ],
)
def test_legacy_unknown_and_ambiguous_obligations_are_not_deduplicated(
    retail: Runtime, monkeypatch: pytest.MonkeyPatch, question: str, terms: list[str]
) -> None:
    assert _named_grouping_terms(question, retail._config) == terms
    _, held = _compare_base(retail, monkeypatch, question, {"group_by": [STORE_NAME]})
    assert "execute" not in held["next"].get("ready_for", [])


def test_a_grouping_marker_inside_a_metric_name_preserves_the_complete_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.semantic_rails.test_runtime_and_metadata import _write_generic_planning_package

    _write_generic_planning_package(tmp_path)
    runtime = Runtime.from_path(str(tmp_path))
    try:
        before, after = _compare_base(
            runtime, monkeypatch, "sends per account by product and send type for current month"
        )
        assert before["status"] == after["status"] == "ok", after.get("why")
        assert "execute" in after["next"]["ready_for"]
        assert _named_grouping_terms(
            "sends per account by product and send type for current month", runtime._config
        ) == ["product", "send type"]
    finally:
        runtime.close()
