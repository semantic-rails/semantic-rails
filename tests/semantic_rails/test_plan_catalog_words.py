"""plan isn't ready when its draft leaves out a question word that names a catalog object.

The invariant: every question word that names something in the catalog (a word of an object's
label or aliases, or of the last dotted part of its id or name outside its own namespaces) is
consumed by the draft: by the own words of an object it selects, a filter value, a time grain it
carries or a time phrase it read. A synonym, a typo, a namespace, a description, a framing word or
an object the draft doesn't select never consumes one. A draft that leaves one over dropped a grouping or answers
about another subject, so plan keeps it in ``best`` and returns ``low_confidence``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
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
    _requested_grouping_spans,
    _requested_grouping_terms,
)
from semantic_rails.planner.faithfulness import (
    unconsumed_catalog_words,
    unconsumed_unknown_words,
    unmatched_intent_terms,
)
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.runtime import Runtime

STORE = "dimension.jaffle_store_name"


@pytest.fixture()
def jaffle(runtime_factory: Any) -> Iterator[Runtime]:
    runtime = runtime_factory("jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def billing(tmp_path: Path) -> Iterator[Runtime]:
    """An invoice package: Charge amount is described as "the charges that are not discounts",
    and only Discount amount's description says "markdown"."""

    (tmp_path / "models").mkdir()
    (tmp_path / "data" / "billing_csv").mkdir(parents=True)
    (tmp_path / "data" / "billing_csv" / "invoices.csv").write_text(
        "invoice_id,issued_at,region,discount,amount\n1,2026-01-01T09:00:00,north,10,40\n",
        encoding="utf-8",
    )
    measures = {
        "discount_amount": ("Discount amount", "discount", "Total discount given, the markdown."),
        "charge_amount": ("Charge amount", "amount", "Sum of the charges that are not discounts."),
    }
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "billing",
                "namespace": "billing",
                "name": "billing",
                "warehouse": "duckdb",
                "default_db": "data/billing.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/billing_csv"},
            },
        },
        "graph.yml": {
            "graph": {"entities": {"invoice": {"key": ["invoice_id"], "model": "invoices"}}}
        },
        "models/invoices.yml": {
            "model": {
                "id": "invoices",
                "relation": "invoices",
                "entities": {"invoice": {}},
                "times": {
                    "issued_at": {"column": "issued_at", "kind": "timestamp", "default": True}
                },
                "dimensions": {"region": {"column": "region", "label": "Region"}},
                "measures": {
                    key: {
                        "label": label,
                        "description": description,
                        "kind": "aggregate",
                        "expr": column,
                        "default_agg": "sum",
                    }
                    for key, (label, column, description) in measures.items()
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


def _not_ready(payload: dict[str, Any], terms: list[str]) -> None:
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["terms"] == terms
    assert payload["best"]["query_ir"]["select"]
    assert "ready_for" not in payload["next"]


def test_a_dropped_grouping_is_not_ready(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="revenue by store, customer type and product type")

    _not_ready(payload, ["customer", "type", "product"])
    assert payload["best"]["query_ir"]["group_by"] == [STORE]
    assert payload["why"]["details"]["dropped_groupings"] == ["customer type", "product type"]
    assert payload["why"]["message"].startswith(
        "The draft drops the grouping by customer type, product type"
    )


@contextmanager
def _with_store_dimensions(jaffle: Runtime, *dimensions: tuple[str, str, str]) -> Iterator[Runtime]:
    """Jaffle with more Store dimensions, each (name, label, column), without aliases."""

    config = jaffle.config
    store = next(row for row in config.dimensions if row.id == STORE)
    added = [
        replace(
            store,
            id=f"dimension.{name}",
            name=name,
            label=label,
            aliases=[],
            description="",
            column=column,
        )
        for name, label, column in dimensions
    ]
    runtime = Runtime.from_config(
        replace(config, dimensions=[*config.dimensions, *added]), source_path=jaffle.source_path
    )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("dimensions", "intent", "grouping", "terms"),
    [
        # "status" (Membership status) is one typo from "states".
        (
            [("states", "States", "store_name")],
            "revenue by states, status",
            "dimension.states",
            ["status"],
        ),
        # "sales" is this dimension's whole name, and only a namespace elsewhere (metric.sales.*).
        ([("sales", "", "store_name")], "revenue by store, sales", STORE, ["sales"]),
        # The draft's own metric sits in that namespace: metric.sales.aov_usd, named
        # jaffle.sales_aov_usd.
        ([("sales", "", "store_id")], "aov by store, sales", STORE, ["sales"]),
        # The planner reads "sent" as a synonym of "received"; here each names its own dimension.
        (
            [("received", "Received", "store_name"), ("sent", "Sent", "store_id")],
            "revenue by received, sent",
            "dimension.received",
            ["sent"],
        ),
    ],
)
def test_one_catalog_name_never_consumes_another(
    jaffle: Runtime,
    dimensions: list[tuple[str, str, str]],
    intent: str,
    grouping: str,
    terms: list[str],
) -> None:
    with _with_store_dimensions(jaffle, *dimensions) as runtime:
        payload = plan_payload(runtime, intent=intent)

        _not_ready(payload, terms)
        assert payload["best"]["query_ir"]["group_by"] == [grouping]
        assert payload["why"]["details"]["dropped_groupings"] == terms
        assert unconsumed_catalog_words(runtime, intent, payload["best"]["query_ir"]) == terms


def test_spelling_a_selected_id_uses_its_namespace_only_there(jaffle: Runtime) -> None:
    aov_by_store = {
        "version": 2,
        "select": [{"as": "aov", "expression": {"metric": "metric.sales.aov_usd"}}],
        "group_by": [STORE],
    }
    with _with_store_dimensions(jaffle, ("sales", "", "store_id")) as runtime:
        intent = "metric.sales.aov_usd by store, sales"

        assert unconsumed_catalog_words(runtime, intent, aov_by_store) == ["sales"]
        assert (
            unconsumed_catalog_words(runtime, "metric.sales.aov_usd by store", aov_by_store) == []
        )


def test_a_time_grain_reads_its_unit_once(jaffle: Runtime) -> None:
    # The predicate's month is "in that month"; no grain groups the orders "by month".
    qualified = {
        "version": 2,
        "select": [{"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}}],
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "op": ">",
                    "value": 10,
                    "time_grain": "month",
                },
                "op": "=",
                "value": True,
            }
        ],
    }
    intent = "orders by month for customers with more than 10 orders in that month"

    assert unconsumed_catalog_words(jaffle, intent, qualified) == ["month"]
    monthly = {**qualified, "time": {"grain": "month"}}
    assert unconsumed_catalog_words(jaffle, intent, monthly) == []


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        ("revenue by order month and order year", ["year"]),
        ("revenue by order month, month", ["month"]),
        # The time block carries one clock grouping; a second one is never consumed.
        ("revenue by order month and order date", ["date"]),
        ("revenue by order year and order date", ["date"]),
        ("revenue by order date and order month", ["date", "month"]),
    ],
)
def test_a_clock_grouping_consumes_only_its_grain_once(
    jaffle: Runtime, intent: str, terms: list[str]
) -> None:
    _not_ready(plan_payload(jaffle, intent=intent), terms)


@pytest.mark.parametrize(
    "intent",
    ["  revenue by store", "  top 3 stores by revenue", "revenue by store and tax & date by show"],
)
def test_grouping_spans_preserve_the_grouping_terms(intent: str) -> None:
    assert [intent.lower()[start:end] for start, end in _requested_grouping_spans(intent)] == (
        _requested_grouping_terms(intent)
    )


def test_unknown_construct_words_need_the_realized_construct(jaffle: Runtime) -> None:
    query = {"select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}}]}
    intent = "revenue at week grain"
    assert unconsumed_unknown_words(jaffle, intent, query) == ["grain"]
    assert unconsumed_unknown_words(jaffle, intent, {**query, "time": {"grain": "week"}}) == []
    assert unconsumed_unknown_words(jaffle, "distinct customers", query) == ["distinct"]
    query["select"] = [{"expression": {"measure": "measure.jaffle.ordering_customer_count"}}]
    assert unconsumed_unknown_words(jaffle, "distinct customers", query) == []


@pytest.mark.parametrize("verb", ["make", "made", "earn", "earned", "generate", "generated"])
def test_light_verbs_do_not_hide_unknown_modifiers(jaffle: Runtime, verb: str) -> None:
    assert plan_payload(jaffle, intent=f"how much revenue did we {verb}")["status"] == "ok"
    _not_ready(
        plan_payload(jaffle, intent=f"how much completed revenue did we {verb}"), ["completed"]
    )


@pytest.mark.parametrize("name", ["period", "show", "date"])
def test_a_cadence_or_request_word_never_consumes_a_dropped_catalog_name(
    jaffle: Runtime, name: str
) -> None:
    # The same framing remains valid when it names no extra catalog object.
    assert plan_payload(jaffle, intent="show monthly revenue by store")["status"] == "ok"
    with _with_store_dimensions(jaffle, (name, name.title(), "store_id")) as runtime:
        assert plan_payload(runtime, intent="monthly revenue by store")["status"] == "ok"
        payload = plan_payload(runtime, intent=f"monthly revenue by store, {name}")
        _not_ready(payload, [name])
        assert payload["best"]["query_ir"]["group_by"] == [STORE]
        for intent in (
            "revenue by store by order date",
            "monthly revenue by order date",
            "revenue by order date, at week grain",
        ):
            assert plan_payload(runtime, intent=intent)["status"] == "ok"
        query = {
            "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
        }
        assert unconsumed_catalog_words(runtime, "revenue by order date", query) == []
        query["time"]["grain"] = "month"
        assert unconsumed_catalog_words(runtime, "revenue by order date", query) == ["date"]


def test_a_prior_period_shift_never_consumes_a_grouping(jaffle: Runtime) -> None:
    query = {
        "version": 2,
        "select": [
            {
                "as": "prior_revenue",
                "expression": {
                    "kind": "prior_period",
                    "input": {"measure": "measure.jaffle.revenue_usd"},
                    "grain": "year",
                    "offset": 1,
                },
            }
        ],
        "group_by": [STORE],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }
    assert unconsumed_catalog_words(jaffle, "monthly revenue vs prior year by store", query) == []
    assert unconsumed_catalog_words(
        jaffle, "monthly revenue by store, year vs prior year", query
    ) == ["year"]


@pytest.mark.parametrize(
    ("name", "label", "plural"),
    [("tax", "Tax", "taxes"), ("box", "Box", "boxes"), ("status", "Membership status", "statuses")],
)
def test_a_plural_is_consumed_by_the_same_forms_that_recognize_it(
    jaffle: Runtime, name: str, label: str, plural: str
) -> None:
    with _with_store_dimensions(jaffle, (name, label, "store_id")) as runtime:
        selected = plan_payload(
            runtime,
            intent=f"revenue by {plural}",
            partial_query={"group_by": [f"dimension.{name}"]},
        )
        assert selected["status"] == "ok", selected.get("why")
        assert "execute" in selected["next"]["ready_for"]
        assert selected["best"]["query_ir"]["group_by"] == [f"dimension.{name}"]
        _not_ready(plan_payload(runtime, intent=f"revenue by store, {plural}"), [plural])


def test_es_does_not_invent_short_or_unrelated_catalog_names(jaffle: Runtime) -> None:
    with _with_store_dimensions(
        jaffle, ("us", "US", "store_id"), ("on", "On", "store_id")
    ) as runtime:
        payload = plan_payload(runtime, intent="revenue by store uses ones")
        assert payload["status"] == "low_confidence"
        assert "ready_for" not in payload["next"]
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert payload["why"]["details"]["gaps"][0]["kind"] == "store_grouping_unrealized"
        assert (
            unconsumed_catalog_words(runtime, payload["intent"], payload["best"]["query_ir"]) == []
        )
        assert unconsumed_unknown_words(
            runtime, payload["intent"], payload["best"]["query_ir"]
        ) == ["uses", "ones"]


@pytest.mark.parametrize(
    "count_metadata",
    [
        {"default_aggregation": "count"},
        {"default_aggregation": "count_distinct"},
        {"value_type": "count"},
        {"label": "Visitor count"},
    ],
)
def test_a_selected_count_valued_measure_reads_number_of_without_an_override(
    jaffle: Runtime, count_metadata: dict[str, str]
) -> None:
    config = jaffle.config
    original = next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd")
    visitor = replace(
        original,
        id="measure.jaffle.visitors",
        name="visitors",
        label=count_metadata.get("label", "Visitors"),
        aliases=[],
        **{key: value for key, value in count_metadata.items() if key != "label"},
    )
    runtime = Runtime.from_config(
        replace(config, measures=[*config.measures, visitor]), source_path=jaffle.source_path
    )
    query = {"version": 2, "select": [{"expression": {"measure": visitor.id}}]}
    try:
        assert unconsumed_catalog_words(runtime, "number of visitors", query) == []
        # Naming the count elsewhere in the catalog doesn't let a non-count sum consume it.
        revenue = {"version": 2, "select": [{"expression": {"measure": original.id}}]}
        assert unconsumed_catalog_words(runtime, "number of revenue", revenue) == ["number"]
        for expression in (
            {"measure": visitor.id},
            {"measure": original.id, "aggregation": "count"},
        ):
            filtered = {
                **revenue,
                "metric_filters": [{"expression": expression, "op": ">", "value": 0}],
            }
            assert unconsumed_catalog_words(runtime, "number of revenue", filtered) == ["number"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "value"),
    [
        ("revenue in Brooklyn", "brooklyn"),
        ("Brooklyn revenue", "brooklyn"),
        ("revenue for Brooklyn", "brooklyn"),
        ("revenue in Philadelphia", "philadelphia"),
    ],
)
def test_an_undeclared_filter_value_never_leaves_a_plan_ready(
    jaffle: Runtime, intent: str, value: str
) -> None:
    config = replace(
        jaffle.config,
        dimensions=[
            replace(row, value_domain="") if row.id == STORE else row
            for row in jaffle.config.dimensions
        ],
        value_domains=[row for row in jaffle.config.value_domains if STORE not in row.dimensions],
    )
    runtime = Runtime.from_config(config, source_path=jaffle.source_path)
    try:
        payload = plan_payload(runtime, intent=intent)
        _not_ready(payload, [value])
        assert payload["why"]["details"]["kind"] == "filter_values_unrealized"
        assert payload["why"]["recovery_hints"][0]["kind"] == "add_missing_condition"
        assert not payload["best"]["query_ir"].get("where")
    finally:
        runtime.close()


def test_a_declared_filter_value_matches_reference_sql(jaffle: Runtime) -> None:
    payload = plan_payload(jaffle, intent="revenue in Brooklyn")
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    [select] = query["select"]
    assert select["expression"]["measure"] == "measure.jaffle.revenue_usd"
    [row] = jaffle.query(query)["rows"]
    # Revenue declares order_total_cents / 100.0, summed over orders.
    connection = duckdb.connect(jaffle.db_path, read_only=True)
    try:
        reference = connection.execute(
            "SELECT SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
            "JOIN jaffle_store s ON o.store_id = s.store_id WHERE s.store_name = 'Brooklyn'"
        ).fetchone()[0]
        total = connection.execute(
            "SELECT SUM(order_total_cents / 100.0) FROM jaffle_order"
        ).fetchone()[0]
    finally:
        connection.close()
    assert row[select["as"]] == pytest.approx(reference)
    assert reference != pytest.approx(total)


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        ("revenue decile by store", ["decile"]),
        ("What is completed revenue by month?", ["completed"]),
        # The intent parse records these as "received", "message" and "account".
        ("revenue from sent messages", ["sent", "messages"]),
        ("revenue for accounts", ["accounts"]),
    ],
)
def test_an_unknown_modifier_is_not_ready(jaffle: Runtime, intent: str, terms: list[str]) -> None:
    payload = plan_payload(jaffle, intent=intent)
    _not_ready(payload, terms)
    assert payload["why"]["details"]["kind"] == "filter_values_unrealized"


def test_an_honored_inclusion_clause_consumes_its_marker(jaffle: Runtime) -> None:
    query = {
        "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}}],
        "where": [
            {"field": STORE, "op": "!=", "value": "Brooklyn"},
            {"field": STORE, "op": "!=", "value": "New Orleans"},
            {"field": STORE, "op": "=", "value": "Philadelphia"},
        ],
    }
    intent = "revenue excluding Brooklyn; without New Orleans, including Philadelphia"
    assert "including" not in unconsumed_unknown_words(jaffle, intent, query)
    query["where"].pop()
    assert "including" in unconsumed_unknown_words(jaffle, intent, query)


@pytest.mark.parametrize(
    ("intent", "aggregation", "terms"),
    [
        ("number of orders by customer name", "count_distinct", ["number"]),
        # Only a count reads "number of"; "number" also names Customer order number.
        ("number of revenue by customer name", "sum", ["number"]),
        # And a count reads only "number of", never the name's own "number".
        ("orders by customer order number", "count_distinct", ["number"]),
    ],
)
def test_a_count_reads_only_its_number_of(
    jaffle: Runtime, intent: str, aggregation: str, terms: list[str]
) -> None:
    orders = {
        "measure": "measure.jaffle.revenue_usd"
        if aggregation == "sum"
        else "measure.jaffle.order_count",
        "aggregation": aggregation,
    }
    by_name = {
        "version": 2,
        "select": [{"as": "orders", "expression": orders}],
        "group_by": ["dimension.jaffle_customer_name"],
    }

    assert unconsumed_catalog_words(jaffle, intent, by_name) == terms


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        # A governed metric's name: Cumulative revenue.
        ("revenue with cumulative", ["cumulative"]),
        # Revenue isn't the customer count.
        ("how many customers ordered in 2017", ["customers"]),
        # Revenue's Order entity isn't an object the draft selects, so it consumes no word: not a
        # dropped grouping by order, nor "orders", which names the Orders measure.
        ("revenue by store, order", ["order"]),
        ("revenue from orders", ["orders"]),
        # A framing word that names an object (Calendar day's "date", Order time) counts too: only
        # a time grain the draft carries reads it, and these drafts carry none.
        ("revenue by store, date", ["date"]),
        ("orders by store, time", ["time"]),
        # A plural the planner doesn't fold still names Membership status.
        ("revenue by store, statuses", ["statuses"]),
    ],
)
def test_a_word_naming_an_object_the_draft_does_not_use_is_not_ready(
    jaffle: Runtime, intent: str, terms: list[str]
) -> None:
    _not_ready(plan_payload(jaffle, intent=intent), terms)


def test_a_description_that_negates_the_word_does_not_answer_it(billing: Runtime) -> None:
    payload = plan_payload(billing, intent="discounts granted by region")

    # The draft takes the measure whose description holds "discounts", not the one named for them.
    [select] = payload["best"]["query_ir"]["select"]
    assert select["expression"]["measure"] == "measure.billing.charge_amount"
    _not_ready(payload, ["discounts"])
    # The warning names it too: a description no longer accounts for a word.
    assert payload["warnings"][0]["details"]["terms"] == ["discounts", "granted"]
    charges = {"version": 2, "select": [select]}
    assert unmatched_intent_terms(billing, "discounts granted", charges) == [
        "discounts",
        "granted",
    ]
    assert unconsumed_catalog_words(billing, "discounts granted", charges) == ["discounts"]


def test_an_unknown_word_only_a_description_holds_is_not_ready(billing: Runtime) -> None:
    payload = plan_payload(billing, intent="discount markdown by region")

    _not_ready(payload, ["markdown"])
    assert payload["why"]["details"]["kind"] == "filter_values_unrealized"
    [select] = payload["best"]["query_ir"]["select"]
    assert select["expression"]["measure"] == "measure.billing.discount_amount"
    assert payload["warnings"][0]["details"]["terms"] == ["markdown"]


def test_every_draft_goes_through_the_one_gate(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another pattern's draft.
    comparison = plan_payload(
        jaffle, intent="food revenue vs drink revenue by store, customer type"
    )
    assert comparison["best"]["pattern"] == "inline_comparison"
    _not_ready(comparison, ["customer", "type"])

    # The catalog fallback's draft, for a question no pattern realizes.
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, intent: CompositionResult(
            intent_ir=parse_intent(runtime, intent), draft=None, pattern=""
        ),
    )
    fallback = plan_payload(jaffle, intent="orders by store, customer type")
    assert fallback["best"]["pattern"] == "catalog_fallback"
    # The history grouping needs query time. Validation refuses the primary;
    # the validating delivered-orders alternative changes its target and grouping.
    assert fallback["status"] == "low_confidence"
    assert fallback["why"]["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    assert fallback["best"]["validation_ok"] is False
    assert "ready_for" not in fallback["next"]

    # Force an executable catalog draft past validation: it must still pass the
    # same catalog-word gate as a named pattern, rather than become ready.
    [draft, *_] = plan_module.fallback_drafts(jaffle, intent="orders by store, customer type")
    valid_draft = replace(draft[0], query={**draft[0].query, "group_by": [STORE]})
    monkeypatch.setattr(
        plan_module, "fallback_drafts", lambda *args, **kwargs: [(valid_draft, draft[1])]
    )
    gated = plan_payload(jaffle, intent="orders by store, customer type")
    assert gated["best"]["validation_ok"] is True
    _not_ready(gated, ["customer", "type"])
    unknown = plan_payload(jaffle, intent="orders decile by store")
    assert unknown["best"]["validation_ok"] is True
    _not_ready(unknown, ["decile"])


def test_a_long_question_is_read_to_its_last_word(
    jaffle: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # More distinct words than the warning reads, then a grouping the draft drops.
    filler = " ".join(f"zq{a}{b}" for a in "abcdefghijklm" for b in "abcdefghijklmnopqrstuvwxyz")
    intent = f"{filler} revenue by store, customer type"
    draft = RuntimeCompositionDraft(
        query={
            "version": 2,
            "select": [{"as": "revenue", "expression": {"measure": "measure.jaffle.revenue_usd"}}],
            "group_by": [STORE],
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

    assert "customer" not in unmatched_intent_terms(jaffle, intent, draft.query)
    _not_ready(plan_payload(jaffle, intent=intent), ["customer", "type"])
