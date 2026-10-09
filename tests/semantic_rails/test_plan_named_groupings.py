"""Additional grouping spellings can only hold a plan, never change its answer."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.expressions import AggregateExpr
from semantic_rails.planner import grouping_checks, plan_payload
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.grouping_checks import _named_grouping_spans, _named_grouping_terms
from semantic_rails.planner.groupings import _listed_grouping_terms
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_plan_unasked_groupings import _CASES
from tests.semantic_rails.test_plan_value_lists import _force_fallback

STORE = "dimension.jaffle_store_name"
STORE_ID_JAFFLE = "dimension.jaffle_store_id"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
REVENUE = "measure.jaffle.revenue_usd"
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

# Plurals, separators and a repeated "at" don't change which groupings the list names.
_SUFFIX_LISTS = [
    f"revenue at customer type{separator}store name {word}"
    for word in ["level", "levels", "grain", "grains"]
    for separator in [" and ", " & ", ", ", " and at ", ", at ", " & at "]
]


@pytest.mark.parametrize("phrasing", _PHRASINGS)
def test_each_spelling_records_the_exact_named_term(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, phrasing: str
) -> None:
    question = "revenue " + phrasing.format(noun="customer type")
    # A level or grain word isn't read as a clause: it makes every named grouping required.
    level = "level" in phrasing or "grain" in phrasing
    named = [] if level else ["customer type"]
    spans = _named_grouping_spans(question, jaffle._config)
    assert [question[start:end] for start, end in spans] == named
    assert _named_grouping_terms(question, jaffle._config) == named
    unmet = grouping_checks._level_groupings_unmet(jaffle._config, question, {"group_by": []})
    assert unmet == (["customer type"] if level else [])
    assert (
        grouping_checks._level_groupings_unmet(
            jaffle._config, question, {"group_by": [CUSTOMER_TYPE]}
        )
        == []
    )
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
        ("revenue at mystery level", []),
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
        base.setattr(grouping_checks, "_named_grouping_terms", _listed_grouping_terms)
        base.setattr(grouping_checks, "_level_groupings_unmet", lambda *args: [])
        base.setattr(grouping_checks, "_named_groupings_unmet", lambda *args: ([], []))
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


def _compare_declared_name_base(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    partial: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    after = plan_payload(runtime, intent=question, partial_query=partial)
    with monkeypatch.context() as base:
        # Reproduce the base without the name obligation, retaining its whitespace-only
        # level/grain reader and all its listed, unknown-noun and other readiness checks.
        base.setattr(grouping_checks, "_named_groupings_unmet", lambda *args: ([], []))
        before = plan_payload(runtime, intent=question, partial_query=partial)
    assert after["best"] == before["best"]
    if before["status"] == "ok" and after["status"] != "ok":
        assert after["status"] == "low_confidence"
        assert after["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert "execute" not in after["next"].get("ready_for", [])
    else:
        assert after["status"] == before["status"]
        assert after["next"] == before["next"]
        if before["status"] == "ok":
            assert after.get("why") == before.get("why")
    return before, after


@pytest.mark.parametrize("kind", ["dimension", "entity"])
@pytest.mark.parametrize("source", ["name", "label", "aliases"])
@pytest.mark.parametrize(
    "term",
    [
        "client_category",
        "client category",
        "CLIENT_CATEGORY",
        "_client_category",
        "client_category_",
    ],
)
def test_declared_grouping_names_preserve_underscore_space_and_case_spans(
    jaffle: Runtime, kind: str, source: str, term: str
) -> None:
    rows = jaffle._config.dimensions if kind == "dimension" else jaffle._config.entities
    row = next(row for row in rows if row.id == CUSTOMER_TYPE) if kind == "dimension" else rows[0]
    spelling = term.lower().replace(" ", "_")
    changed = replace(
        row,
        **{source: [spelling] if source == "aliases" else spelling},
    )
    config = replace(
        jaffle._config,
        **{"dimensions" if kind == "dimension" else "entities": [changed]},
    )
    question = f"revenue at {term} and store name level"
    spans = grouping_checks._declared_name_spans(config, question.lower(), underscores=True)
    assert [
        question[low:high] for (low, high), named in spans.items() if (kind, changed) in named
    ] == [term]
    unmet, _ = grouping_checks._named_groupings_unmet(config, question, {"group_by": []})
    assert term.lower() in unmet


@pytest.mark.parametrize("kind", ["dimension", "entity"])
@pytest.mark.parametrize("source", ["name", "label", "aliases"])
@pytest.mark.parametrize("term", ["_client_category", "client_category_"])
def test_boundary_underscores_do_not_hide_a_space_joined_declared_name(
    jaffle: Runtime, kind: str, source: str, term: str
) -> None:
    rows = jaffle._config.dimensions if kind == "dimension" else jaffle._config.entities
    row = next(row for row in rows if row.id == CUSTOMER_TYPE) if kind == "dimension" else rows[0]
    changed = replace(
        row, **{source: ["client category"] if source == "aliases" else "client category"}
    )
    config = replace(
        jaffle._config,
        **{"dimensions" if kind == "dimension" else "entities": [changed]},
    )
    question = f"revenue at {term} and store name level"
    spans = grouping_checks._declared_name_spans(config, question, underscores=True)
    assert [
        question[low:high] for (low, high), named in spans.items() if (kind, changed) in named
    ] == ["client_category"]


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("group_by", [None, [STORE], [STORE, CUSTOMER_TYPE]])
@pytest.mark.parametrize(
    "question",
    [
        "revenue at customer_type and store name level",
        "revenue at CUSTOMER_TYPE and STORE_NAME levels",
        "revenue at customer type and store name level",
        "revenue at customer_type and store_name grain",
        "revenue at store name and customer_type level",
        "revenue at store name level",
        "revenue by store name",
        "revenue at mystery level",
    ],
)
def test_declared_name_variants_only_add_holds_against_immediate_base(
    jaffle: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    group_by: list[str] | None,
    question: str,
) -> None:
    _force_fallback(jaffle, monkeypatch, question, path)
    partial = {
        "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}],
        **({"group_by": group_by} if group_by is not None else {}),
    }
    before, after = _compare_declared_name_base(jaffle, monkeypatch, question, partial)
    if question == "revenue at customer_type and store name level":
        if group_by != [STORE, CUSTOMER_TYPE]:
            assert after["status"] == "low_confidence"
            assert "customer_type" in after["why"]["details"]["dropped_groupings"]
            if before["best"]["query_ir"].get("group_by") == [STORE]:
                assert before["status"] == "ok"
                assert after["why"]["details"]["dropped_groupings"] == ["customer_type"]
        else:
            assert before["status"] == after["status"] == "ok"
            query = after["best"]["query_ir"]
            reference = _in_memory_reference(
                jaffle,
                "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
                "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
                "JOIN jaffle_store s USING (store_id) "
                "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
                "AND c.customer_type IN ('new', 'repeat') GROUP BY 1, 2 ORDER BY 1, 2",
            )
            assert len(reference) == 4
            assert sorted(_in_memory_rows(jaffle, query, [STORE, CUSTOMER_TYPE])) == reference


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("source", ["name", "label", "aliases"])
@pytest.mark.parametrize("term", ["_customer_type", "customer_type_"])
@pytest.mark.parametrize("group_by", [None, [STORE], [STORE, CUSTOMER_TYPE]])
def test_boundary_underscore_names_only_add_holds(
    jaffle: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    source: str,
    term: str,
    group_by: list[str] | None,
) -> None:
    monkeypatch.setattr(
        jaffle,
        "_config",
        replace(
            jaffle._config,
            dimensions=[
                replace(row, **{source: [term] if source == "aliases" else term})
                if row.id == CUSTOMER_TYPE
                else row
                for row in jaffle._config.dimensions
            ],
        ),
    )
    question = f"revenue at {term} and store name level"
    _force_fallback(jaffle, monkeypatch, question, path)
    partial = {
        "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}],
        **({"group_by": group_by} if group_by is not None else {}),
    }
    before, after = _compare_declared_name_base(jaffle, monkeypatch, question, partial)
    if group_by != [STORE, CUSTOMER_TYPE]:
        assert after["status"] == "low_confidence"
        assert term in after["why"]["details"]["dropped_groupings"]
        if before["best"]["query_ir"].get("group_by") == [STORE]:
            assert before["status"] == "ok"
            assert after["why"]["details"]["dropped_groupings"] == [term]
        assert "execute" not in after["next"].get("ready_for", [])
    else:
        assert before["status"] == after["status"] == "ok"
        query = after["best"]["query_ir"]
        reference = _in_memory_reference(
            jaffle,
            "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
            "JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
            "AND c.customer_type IN ('new', 'repeat') GROUP BY 1, 2 ORDER BY 1, 2",
        )
        expected = {
            ("Brooklyn", "new"): 90.48,
            ("Brooklyn", "repeat"): 259334.37,
            ("Philadelphia", "new"): 6.36,
            ("Philadelphia", "repeat"): 486461.82,
        }
        assert len(reference) == len(expected) == 4
        assert {(store, kind): value for store, kind, value in reference} == pytest.approx(expected)
        assert sorted(_in_memory_rows(jaffle, query, [STORE, CUSTOMER_TYPE])) == reference


# The words around a grouping name never decide whether a value inside it may filter.
_VALUE_IN_NAME_QUESTIONS = [
    "revenue at {alias} and store name level",
    "revenue by {alias} and store name",
    "revenue for each {alias} and store name",
    "revenue per {alias} and store name",
    "{alias} and store name revenue",
]

# (phrasing, path, caller group_by length) that other readiness checks hold even without it.
_HELD_WITHOUT_IT = {
    # Without the store shortcut, these drafts also omit the requested Store name.
    ("revenue at {alias} and store name level", "primary", None),
    ("revenue for each {alias} and store name", "primary", None),
    ("revenue at {alias} and store name level", "fallback", None),
    ("revenue for each {alias} and store name", "fallback", None),
    ("revenue per {alias} and store name", "fallback", None),
    *(("revenue per {alias} and store name", "primary", group_by) for group_by in (None, 1, 2)),
}


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("group_by", [None, [STORE], [STORE, CUSTOMER_TYPE]])
@pytest.mark.parametrize("alias", ["_new_type", "new_type", "new_type_", "new type"])
@pytest.mark.parametrize("phrasing", _VALUE_IN_NAME_QUESTIONS)
def test_a_value_inside_a_declared_grouping_name_only_adds_a_hold(
    jaffle: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    group_by: list[str] | None,
    alias: str,
    phrasing: str,
) -> None:
    monkeypatch.setattr(
        jaffle,
        "_config",
        replace(
            jaffle._config,
            dimensions=[
                replace(row, aliases=[*(row.aliases or []), alias])
                if row.id == CUSTOMER_TYPE
                else row
                for row in jaffle._config.dimensions
            ],
        ),
    )
    question = phrasing.format(alias=alias)
    _force_fallback(jaffle, monkeypatch, question, path)
    partial = {
        "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}],
        **({"group_by": group_by} if group_by is not None else {}),
    }
    after = plan_payload(jaffle, intent=question, partial_query=partial)
    with monkeypatch.context() as base:
        # Keep the whitespace level reader and every other readiness check.
        base.setattr(grouping_checks, "_named_groupings_unmet", lambda *args: ([], []))
        before = plan_payload(jaffle, intent=question, partial_query=partial)
    assert after["best"] == before["best"]
    query = after["best"]["query_ir"]
    assert {"field": CUSTOMER_TYPE, "op": "=", "value": "new"} in query["where"]
    held = (phrasing, path, group_by and len(group_by)) in _HELD_WITHOUT_IT
    assert before["status"] == ("low_confidence" if held else "ok")
    assert before["status"] == "ok" or after["status"] == before["status"]
    assert after["status"] == "low_confidence", after.get("why")
    assert "execute" not in after["next"].get("ready_for", [])
    collision = [{"term": alias, "field": CUSTOMER_TYPE, "value": "new"}]
    if before["status"] == "ok":
        # A draft without the store grouping drops that named grouping as well.
        unmet = [alias, *([] if STORE in query.get("group_by", []) else ["store name"])]
        assert after["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert after["why"]["details"]["dropped_groupings"] == unmet
        assert after["why"]["details"]["filter_inside_grouping"] == collision
    # Even a caller-supplied complete grouping cannot authorize the narrowed answer, and the
    # hold names the filter to confirm or remove, not a grouping to add.
    unmet, inside = grouping_checks._named_groupings_unmet(jaffle._config, question, query)
    assert alias in unmet
    assert inside == collision
    why = grouping_checks._dropped_grouping_why(jaffle, question, query, partial)
    assert why is not None
    assert why["code"] == "PLAN_UNMATCHED_TERMS"
    assert alias in why["details"]["dropped_groupings"]
    assert why["details"]["filter_inside_grouping"] == collision
    assert f"'new' of {CUSTOMER_TYPE} inside {alias!r}" in why["message"]
    assert not any(
        alias in sentence
        for sentence in why["message"].split(". ")
        if sentence.startswith("The draft drops the grouping")
    )
    hint = why["recovery_hints"][0]["message"]
    assert "filter_inside_grouping entry, confirm with the user" in hint
    if {STORE, CUSTOMER_TYPE} <= set(query.get("group_by") or []):
        assert "drops the grouping" not in why["message"]
        assert not hint.startswith("Find a dimension")


@pytest.mark.parametrize(
    ("question", "where"),
    [
        # A value stated outside every grouping name is the question's own filter.
        (
            "revenue by customer type and store name for new customers",
            [{"field": CUSTOMER_TYPE, "op": "=", "value": "new"}],
        ),
        # A filter that doesn't carry the value, or keeps it out, brings nothing in.
        ("revenue by _new_type and store name", [JAFFLE_FILTER]),
        (
            "revenue by _new_type and store name",
            [{"field": CUSTOMER_TYPE, "op": "IN", "value": ["repeat"]}],
        ),
        (
            "revenue by _new_type and store name",
            [{"field": CUSTOMER_TYPE, "op": "!=", "value": "new"}],
        ),
    ],
)
def test_a_value_outside_grouping_names_adds_no_hold(
    jaffle: Runtime, question: str, where: list[dict[str, Any]]
) -> None:
    config = replace(
        jaffle._config,
        dimensions=[
            replace(row, aliases=[*(row.aliases or []), "_new_type"])
            if row.id == CUSTOMER_TYPE
            else row
            for row in jaffle._config.dimensions
        ],
    )
    query = {"group_by": [STORE, CUSTOMER_TYPE], "where": where}
    assert grouping_checks._named_groupings_unmet(config, question, query) == ([], [])


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("alias", ["_customer_type", "customer_type", "customer type"])
@pytest.mark.parametrize("phrasing", _VALUE_IN_NAME_QUESTIONS)
def test_a_fully_grouped_alias_without_a_value_collision_matches_reference_sql(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, alias: str, phrasing: str
) -> None:
    question = phrasing.format(alias=alias)
    monkeypatch.setattr(
        jaffle,
        "_config",
        replace(
            jaffle._config,
            dimensions=[
                replace(row, aliases=[*(row.aliases or []), alias])
                if row.id == CUSTOMER_TYPE
                else row
                for row in jaffle._config.dimensions
            ],
        ),
    )
    _force_fallback(jaffle, monkeypatch, question, path)
    partial = {
        "group_by": [STORE, CUSTOMER_TYPE],
        "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}],
    }
    before, after = _compare_declared_name_base(jaffle, monkeypatch, question, partial)
    assert grouping_checks._named_groupings_unmet(
        jaffle._config, question, after["best"]["query_ir"]
    ) == ([], [])
    listed = phrasing.startswith(("revenue by ", "revenue for each ", "revenue per "))
    if (alias == "_customer_type" and listed) or (
        phrasing.startswith("revenue per ") and path == "primary"
    ):
        # Held before this check existed: the listed-grouping reader doesn't read a boundary
        # underscore, and "per" drifts from the primary draft. Only a hold could be added.
        assert before["status"] == after["status"] == "low_confidence"
        return
    assert before["status"] == after["status"] == "ok", after.get("why")
    assert "execute" in after["next"]["ready_for"]
    reference = _in_memory_reference(
        jaffle,
        "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
        "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
        "JOIN jaffle_store s USING (store_id) "
        "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
        "AND c.customer_type IN ('new', 'repeat') GROUP BY 1, 2 ORDER BY 1, 2",
    )
    assert len(reference) == 4
    assert [row[2] for row in reference] == pytest.approx([90.48, 259334.37, 6.36, 486461.82])
    assert (
        sorted(_in_memory_rows(jaffle, after["best"]["query_ir"], [STORE, CUSTOMER_TYPE]))
        == reference
    )


# The primary draft reads "per" with a filter on the value inside the name, which another
# check holds as drift with or without the obligation.
_HELD_WITHOUT_OBLIGATION = {("revenue per {alias} and store name", "primary")}


def _value_word_in_measure_label(runtime: Runtime) -> Any:
    """Customer type with the alias `_new_type`, and revenue labelled with that alias's value
    word, so value inference brings in no Customer type filter."""

    return replace(
        runtime._config,
        dimensions=[
            replace(row, aliases=[*(row.aliases or []), "_new_type"])
            if row.id == CUSTOMER_TYPE
            else row
            for row in runtime._config.dimensions
        ],
        measures=[
            replace(row, label="Revenue (new and repeat types)") if row.id == REVENUE else row
            for row in runtime._config.measures
        ],
    )


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("grouped", [False, True], ids=["metric-filter-only", "grouped"])
def test_a_metric_filter_cannot_pin_another_selections_named_grouping(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, grouped: bool
) -> None:
    config = _value_word_in_measure_label(jaffle)
    cohort = MetricConfig(
        id="metric.sales.cohort_revenue",
        kind="aggregate",
        label="Cohort revenue",
        expression=AggregateExpr(
            REVENUE,
            "sum",
            filter={"all": [{"field": CUSTOMER_TYPE, "op": "=", "value": "new"}]},
        ),
    )
    monkeypatch.setattr(
        jaffle, "_config", replace(config, metric_recipes=[*config.metric_recipes, cohort])
    )
    question = "revenue at _new_type and store name level"
    _force_fallback(jaffle, monkeypatch, question, path)
    partial = {
        "select": [
            {"as": "revenue_usd", "expression": {"measure": REVENUE}},
            {"as": "cohort_revenue", "expression": {"metric": cohort.id}},
        ],
        "group_by": [STORE, CUSTOMER_TYPE] if grouped else [STORE],
        "where": [JAFFLE_FILTER],
    }
    before, after = _compare_declared_name_base(jaffle, monkeypatch, question, partial)
    query = after["best"]["query_ir"]
    assert query["select"] == partial["select"]
    assert query["group_by"] == partial["group_by"]
    assert query["where"] == partial["where"]
    assert before["status"] == "ok"
    if not grouped:
        assert after["status"] == "low_confidence"
        assert after["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert after["why"]["details"]["dropped_groupings"] == ["_new_type"]
        assert "execute" not in after["next"].get("ready_for", [])
        return
    assert after["status"] == "ok", after.get("why")
    assert "execute" in after["next"]["ready_for"]
    reference = _in_memory_reference(
        jaffle,
        "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0), "
        "SUM(CASE WHEN c.customer_type = 'new' THEN o.order_total_cents / 100.0 ELSE 0 END) "
        "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
        "JOIN jaffle_store s USING (store_id) "
        "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1, 2 ORDER BY 1, 2",
    )
    columns, rows = _in_memory(jaffle, jaffle.compile(query)["rendered_sql"])
    actual = [
        tuple(
            row[columns.index(item)]
            for item in [STORE, CUSTOMER_TYPE, "revenue_usd", "cohort_revenue"]
        )
        for row in rows
    ]
    assert len(actual) == len(reference) == 4
    assert sorted(actual) == reference
    assert [row[2] for row in reference] == pytest.approx([90.48, 259334.37, 6.36, 486461.82])


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("phrasing", _VALUE_IN_NAME_QUESTIONS)
def test_a_named_grouping_without_a_filter_keeps_its_obligation(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, phrasing: str
) -> None:
    monkeypatch.setattr(jaffle, "_config", _value_word_in_measure_label(jaffle))
    question = phrasing.format(alias="_new_type")
    _force_fallback(jaffle, monkeypatch, question, path)
    partial = {"group_by": [STORE], "where": [JAFFLE_FILTER]}
    before, after = _compare_declared_name_base(jaffle, monkeypatch, question, partial)
    query = after["best"]["query_ir"]
    unmet, inside = grouping_checks._named_groupings_unmet(jaffle._config, question, query)
    assert unmet == ["_new_type"]
    assert after["status"] == "low_confidence"
    assert "execute" not in after["next"].get("ready_for", [])
    if (phrasing, path) in _HELD_WITHOUT_OBLIGATION:
        assert before["status"] == "low_confidence"
        return
    # Neither a value word inside the name nor the measure label's words discharge it.
    assert query["group_by"] == [STORE]
    assert not any(row["field"] == CUSTOMER_TYPE for row in query["where"])
    assert inside == []
    assert before["status"] == "ok"
    assert after["why"]["details"]["dropped_groupings"] == ["_new_type"]


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("phrasing", _VALUE_IN_NAME_QUESTIONS)
@pytest.mark.parametrize(
    ("partial", "sql", "expected"),
    [
        (
            {"group_by": [STORE, CUSTOMER_TYPE], "where": [JAFFLE_FILTER]},
            "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
            "JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1, 2 ORDER BY 1, 2",
            {
                ("Brooklyn", "new"): 90.48,
                ("Brooklyn", "repeat"): 259334.37,
                ("Philadelphia", "new"): 6.36,
                ("Philadelphia", "repeat"): 486461.82,
            },
        ),
        (
            {
                "group_by": [STORE],
                "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "=", "value": "repeat"}],
            },
            "SELECT s.store_name, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
            "JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
            "AND c.customer_type = 'repeat' GROUP BY 1 ORDER BY 1",
            {("Brooklyn",): 259334.37, ("Philadelphia",): 486461.82},
        ),
    ],
    ids=["grouped", "pinned"],
)
def test_a_grouped_or_pinned_name_keeps_its_answer(
    jaffle: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    phrasing: str,
    partial: dict[str, Any],
    sql: str,
    expected: dict[tuple[str, ...], float],
) -> None:
    monkeypatch.setattr(jaffle, "_config", _value_word_in_measure_label(jaffle))
    question = phrasing.format(alias="_new_type")
    _force_fallback(jaffle, monkeypatch, question, path)
    before, after = _compare_declared_name_base(jaffle, monkeypatch, question, partial)
    if (phrasing, path) in _HELD_WITHOUT_OBLIGATION:
        assert before["status"] == after["status"] == "low_confidence"
        return
    query = after["best"]["query_ir"]
    assert grouping_checks._named_groupings_unmet(jaffle._config, question, query) == ([], [])
    assert before["status"] == after["status"] == "ok", after.get("why")
    assert "execute" in after["next"]["ready_for"]
    reference = _in_memory_reference(jaffle, sql)
    assert {tuple(row[:-1]): row[-1] for row in reference} == pytest.approx(expected)
    dimensions = partial["group_by"]
    assert sorted(_in_memory_rows(jaffle, query, dimensions)) == reference


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
                "version": 1,
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
    assert grouping_checks._dropped_grouping_why(jaffle, question, query) is None
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
    why = grouping_checks._dropped_grouping_why(jaffle, question, dropped["best"]["query_ir"])
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
    assert grouping_checks._dropped_grouping_why(retail, question, query) is None
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
    why = grouping_checks._dropped_grouping_why(retail, question, incomplete)
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
        *_SUFFIX_LISTS,
        "revenue last month customer type level",
        "revenue, customer type level",
        "revenue and orders store level",
        "revenue at store name level for customer type new",
        "revenue at store level for customer types new and repeat",
        "revenue at store name level for each store",
        "revenue by store name for each store",
        "revenue at region level",
        "revenue at the level",
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


@pytest.mark.parametrize("question", _SUFFIX_LISTS)
def test_complete_suffix_list_matches_reference_sql(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    partial = {
        "group_by": [STORE, CUSTOMER_TYPE],
        "where": [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}],
    }
    before, complete = _compare_base(jaffle, monkeypatch, question, partial)
    if "grain" in question:
        # A non-clock grain phrase is already held by lexical coverage, and stays held.
        assert before["status"] == complete["status"] == "low_confidence"
    else:
        assert before["status"] == complete["status"] == "ok", complete.get("why")
        assert "execute" in complete["next"]["ready_for"]
    query = complete["best"]["query_ir"]
    assert grouping_checks._dropped_grouping_why(jaffle, question, query) is None
    reference = _in_memory_reference(
        jaffle,
        "SELECT s.store_name, c.customer_type, SUM(o.order_total_cents / 100.0) "
        "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
        "JOIN jaffle_store s USING (store_id) "
        "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
        "AND c.customer_type IN ('new', 'repeat') GROUP BY 1, 2 ORDER BY 1, 2",
    )
    actual = _in_memory_rows(jaffle, query, [STORE, CUSTOMER_TYPE])
    assert len(actual) == len(reference) == 4
    assert sorted(actual) == reference
    assert [row[2] for row in reference] == pytest.approx([90.48, 259334.37, 6.36, 486461.82])
    assert grouping_checks._level_groupings_unmet(jaffle._config, question, query) == []


@pytest.mark.parametrize("question", _SUFFIX_LISTS)
@pytest.mark.parametrize("missing", [STORE, CUSTOMER_TYPE])
def test_suffix_list_holds_when_either_grouping_is_dropped(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, question: str, missing: str
) -> None:
    filters = [JAFFLE_FILTER, {"field": CUSTOMER_TYPE, "op": "IN", "value": ["new", "repeat"]}]
    _, native = _compare_base(jaffle, monkeypatch, question, {"where": filters})
    assert "execute" not in native["next"].get("ready_for", [])
    draft = RuntimeCompositionDraft(
        query={
            "version": 1,
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
    name = "store name" if missing == STORE else "customer type"
    why = grouping_checks._dropped_grouping_why(jaffle, question, dropped["best"]["query_ir"])
    assert why is not None
    assert why["details"]["dropped_groupings"] == [name]
    if "grain" not in question:
        assert dropped["why"]["details"]["dropped_groupings"] == [name]
    assert "execute" not in dropped["next"].get("ready_for", [])


@pytest.mark.parametrize("prefix", ["show ", "total ", "what is "])
def test_level_in_a_measure_name_asks_for_no_level(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    config = replace(
        jaffle._config,
        measures=[
            replace(row, label="Stock level", aliases=["Stock level"])
            if row.id == "measure.jaffle.revenue_usd"
            else row
            for row in jaffle._config.measures
        ],
    )
    runtime = Runtime.from_config(config, source_path=jaffle.source_path)
    try:
        question = f"{prefix}stock level by store name"
        before, complete = _compare_base(runtime, monkeypatch, question)
        assert before["status"] == complete["status"] == "ok", complete.get("why")
        assert "execute" in complete["next"]["ready_for"]
        query = complete["best"]["query_ir"]
        assert query["group_by"] == [STORE]
        assert query["select"][0]["expression"]["measure"] == "measure.jaffle.revenue_usd"
        assert grouping_checks._level_groupings_unmet(runtime._config, question, query) == []
    finally:
        runtime.close()


_BOTH_MEASURES = [
    {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}},
    {"as": "order_count", "expression": {"measure": "measure.jaffle.order_count"}},
]


@pytest.mark.parametrize(
    ("question", "partial", "name"),
    [
        ("revenue last month customer type level", {"group_by": [CUSTOMER_TYPE]}, "customer type"),
        ("revenue, customer type level", {"group_by": [CUSTOMER_TYPE]}, "customer type"),
        (
            "revenue and orders store level",
            {"group_by": [STORE], "select": _BOTH_MEASURES},
            "store",
        ),
    ],
)
def test_measure_words_before_a_level_add_no_grouping(
    jaffle: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    partial: dict[str, Any],
    name: str,
) -> None:
    before, complete = _compare_base(jaffle, monkeypatch, question, partial)
    assert before["status"] == complete["status"] == "ok", complete.get("why")
    assert "execute" in complete["next"]["ready_for"]
    query = complete["best"]["query_ir"]
    assert query["group_by"] == partial["group_by"]
    assert grouping_checks._level_groupings_unmet(jaffle._config, question, query) == []
    assert grouping_checks._level_groupings_unmet(
        jaffle._config, question, {**query, "group_by": []}
    ) == [name]


@pytest.mark.parametrize(
    ("pin", "ready"),
    [
        ({"op": "=", "value": "new"}, True),
        ({"op": "IN", "value": ["new"]}, True),
        ({"op": "IN", "value": ["new", "repeat"]}, False),
    ],
)
def test_only_a_one_value_filter_stands_in_for_a_named_grouping(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch, pin: dict[str, Any], ready: bool
) -> None:
    question = "revenue at store name level for customer type new"
    draft = RuntimeCompositionDraft(
        query={
            "version": 1,
            "select": [
                {"as": "revenue_usd", "expression": {"measure": "measure.jaffle.revenue_usd"}}
            ],
            "group_by": [STORE],
            "where": [{"field": CUSTOMER_TYPE, **pin}],
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
    before, after = _compare_base(jaffle, monkeypatch, question, {"group_by": [STORE]})
    assert before["status"] == "ok"
    query = after["best"]["query_ir"]
    if not ready:
        # The draft adds repeat customers to the answer the question limits to new ones.
        assert after["status"] == "low_confidence"
        assert after["why"]["details"]["dropped_groupings"] == ["customer type"]
        assert "execute" not in after["next"].get("ready_for", [])
        return
    assert after["status"] == "ok", after.get("why")
    assert "execute" in after["next"]["ready_for"]
    assert sorted(_in_memory_rows(jaffle, query, [STORE])) == _in_memory_reference(
        jaffle,
        "SELECT s.store_name, SUM(o.order_total_cents / 100.0) "
        "FROM jaffle_order o JOIN jaffle_customer c USING (customer_id) "
        "JOIN jaffle_store s USING (store_id) "
        "WHERE c.customer_type = 'new' GROUP BY 1 ORDER BY 1",
    )


ORDER_WEEKS = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "week"}
OPENED_MONTHS = {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"}


@pytest.mark.parametrize(
    ("question", "group_by", "time", "unmet"),
    [
        # No level word outside a declared name: nothing is read.
        ("revenue by store name", [], None, []),
        # An entity takes a stand-in: its key, or its one dimension that names it.
        ("revenue at store level", [STORE], None, []),
        ("revenue at store level", [STORE_ID_JAFFLE], None, []),
        ("revenue at store level", [CUSTOMER_TYPE], None, ["store"]),
        ("revenue at customer level", [CUSTOMER_TYPE], None, ["customer"]),
        ("revenue at customer level", ["dimension.jaffle_customer_id"], None, []),
        # Every named grouping counts, wherever the question names it.
        ("revenue at store name level for each customer type", [STORE], None, ["customer type"]),
        # A clock phrase is the time block's, with the names inside it, but only the query's
        # own clock, and never across a comma.
        ("revenue at month level", [], None, []),
        ("revenue by order date, at week grain", [], ORDER_WEEKS, []),
        ("revenue by order date, at week grain", [], None, ["order"]),
        ("revenue at month, store level", [], OPENED_MONTHS, ["store"]),
        # An unknown noun is unmet.
        ("revenue at region level", [], None, ["region"]),
        ("revenue at mystery levels", [], None, ["mystery"]),
        ("revenue at the level", [], None, ["revenue"]),
        ("level of revenue", [], None, ["level"]),
        # A plural that isn't a declared name holds rather than guesses.
        ("revenue at customer types level", [CUSTOMER_TYPE], None, ["customer", "types"]),
    ],
)
def test_level_words_need_every_named_grouping(
    jaffle: Runtime,
    question: str,
    group_by: list[str],
    time: dict[str, str] | None,
    unmet: list[str],
) -> None:
    query = {"group_by": group_by, **({"time": time} if time else {})}
    assert grouping_checks._level_groupings_unmet(jaffle._config, question, query) == unmet


def _in_memory_reference(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    return _in_memory(runtime, sql)[1]


def _in_memory_rows(
    runtime: Runtime, query: dict[str, Any], dimensions: list[str]
) -> list[tuple[Any, ...]]:
    columns, rows = _in_memory(runtime, runtime.compile(query)["rendered_sql"])
    alias = query["select"][0]["as"]
    return [tuple(row[columns.index(item)] for item in [*dimensions, alias]) for row in rows]


def _in_memory(runtime: Runtime, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run SQL on in-memory copies of the order, customer and store tables."""

    with duckdb.connect(":memory:") as connection:
        seed_path = str(runtime.db_path).replace("'", "''")
        connection.execute(f"ATTACH '{seed_path}' AS seed (READ_ONLY)")
        for table in ["jaffle_order", "jaffle_customer", "jaffle_store"]:
            connection.execute(f"CREATE TABLE {table} AS SELECT * FROM seed.{table}")
        cursor = connection.execute(sql)
        return [column[0] for column in cursor.description], cursor.fetchall()


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
