"""plan isn't ready when its draft leaves out a question word that names a catalog object.

The invariant: every question word that names something in the catalog (a word of an object's
id, name, label or aliases) is consumed by the draft, by the names of an object it uses, a filter
value, a time phrase it read, or as a framing word. A description never consumes a word. A draft
that leaves one over dropped a grouping or answers about another subject, so plan keeps it in
``best`` and returns ``low_confidence``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import RuntimeCompositionDraft
from semantic_rails.planner.faithfulness import unconsumed_catalog_words, unmatched_intent_terms
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


@pytest.mark.parametrize(
    ("name", "label", "intent", "grouping", "terms"),
    [
        ("states", "States", "revenue by states, status", "dimension.states", ["status"]),
        ("sales", "", "revenue by store, sales", STORE, ["sales"]),
    ],
)
def test_exact_catalog_names_survive_typos_and_other_objects_namespaces(
    jaffle: Runtime, name: str, label: str, intent: str, grouping: str, terms: list[str]
) -> None:
    config = jaffle.config
    store = next(row for row in config.dimensions if row.id == STORE)
    dimension = replace(
        store, id=f"dimension.{name}", name=name, label=label, aliases=[], description=""
    )
    runtime = Runtime.from_config(
        replace(config, dimensions=[*config.dimensions, dimension]), source_path=jaffle.source_path
    )
    try:
        payload = plan_payload(runtime, intent=intent)

        _not_ready(payload, terms)
        assert payload["best"]["query_ir"]["group_by"] == [grouping]
        assert payload["why"]["details"]["dropped_groupings"] == terms
        assert unconsumed_catalog_words(runtime, intent, payload["best"]["query_ir"]) == terms
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        # A governed metric's name: Cumulative revenue.
        ("revenue with cumulative", ["cumulative"]),
        # Revenue isn't the customer count.
        ("how many customers ordered in 2017", ["customers"]),
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


def test_a_word_only_a_description_holds_stays_a_warning(billing: Runtime) -> None:
    payload = plan_payload(billing, intent="discount markdown by region")

    assert payload["status"] == "ok", payload.get("why")
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
    _not_ready(fallback, ["type"])


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
