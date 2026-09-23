"""Claims the comparison pack publishes are generated from its own output check."""

from __future__ import annotations

import copy
import importlib.util
import itertools
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2] / "comparisons" / "semantic_layers" / "shared" / "scripts"
)


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load("validate_output_consistency")
generator = _load("generate_comparison_contracts")

LAYERS = generator.LAYER_ORDER
SHARED = [f"q0{n}_shared" for n in range(1, 8)]
TARGETED = [f"q{n:02d}_targeted" for n in range(8, 17)]


def _question(qid: str, status: str = "matched") -> dict[str, Any]:
    return {
        "question_id": qid,
        "slice": "shared" if qid in SHARED else "semantic_rails_targeted",
        "comparison_status": status,
        "comparable_layers": list(LAYERS),
        "layer_statuses": dict.fromkeys(LAYERS, "native"),
    }


def _report(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in ("matched", "mismatched", "not_comparable")}
    by_slice: dict[str, dict[str, Any]] = {}
    for item in items:
        counts[item["comparison_status"]] += 1
        entry = by_slice.setdefault(
            item["slice"],
            {"questions": 0, **dict.fromkeys(counts, 0), "mismatched_questions": []},
        )
        entry["questions"] += 1
        entry[item["comparison_status"]] += 1
        if item["comparison_status"] == "mismatched":
            entry["mismatched_questions"].append(item["question_id"])
    return {"summary": counts, "summary_by_slice": by_slice, "questions": items}


def _claims(items: list[dict[str, Any]], labels: dict[str, str] | None = None) -> list[str]:
    questions = [{"id": item["question_id"], "title": item["question_id"]} for item in items]
    slice_ids: dict[str, list[str]] = {}
    for item in items:
        slice_ids.setdefault(item["slice"], []).append(item["question_id"])
    layers = [
        {
            "label": generator.LAYER_META[layer]["label"],
            "questions": [
                {"question_id": qid, "support_status": (labels or {}).get(layer, "native")}
                for qid in generator.PRECOMPUTED_COLUMN_QUESTIONS
            ],
        }
        for layer in LAYERS
    ]
    return generator.claim_findings(_report(items), questions, slice_ids, layers)


def test_agreement_groups_agree_pairwise_despite_non_transitive_tolerance() -> None:
    def rows(value: str) -> list[dict[str, Any]]:
        return [{"orders": Decimal(value)}]

    groups = validator._agreement_groups(
        {
            "semantic_rails": rows("10"),
            "metricflow": rows("1"),
            "cube": rows("1.0000009"),
            "malloy": rows("0.9999991"),
        }
    )
    by_layer = {
        "semantic_rails": "10",
        "metricflow": "1",
        "cube": "1.0000009",
        "malloy": "0.9999991",
    }
    for group in groups:
        for left, right in itertools.combinations(group, 2):
            assert validator._rows_equal(rows(by_layer[left]), rows(by_layer[right]))[0], group
    assert sorted(map(sorted, groups)) == [["cube", "metricflow"], ["malloy"], ["semantic_rails"]]


def test_headline_reports_mismatches_and_who_disagrees() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    for item in (items[6], items[15]):
        item["comparison_status"] = "mismatched"
        item["agreement_groups"] = [["semantic_rails"], [layer for layer in LAYERS[1:]]]
    claims = _claims(items)
    assert claims[0].startswith("14 of 16 questions return matching normalized outputs")
    assert "2 do not match: q07" in claims[0]
    assert claims[1] == (
        "On q07 and q16: MetricFlow, Cube, Malloy, Snowflake Semantic Views and KtX agree "
        "with each other; Semantic Rails differs."
    )
    assert "Shared questions (q01-q07): 6 of 7 match." in claims[2]
    assert "Semantic-Rails-targeted questions (q08-q16): 8 of 9 match." in claims[2]


def test_headline_when_everything_matches() -> None:
    claims = _claims([_question(qid) for qid in SHARED + TARGETED])
    assert claims[0] == "All 16 questions return matching normalized outputs across all 6 layers."
    assert not any(claim.startswith("On ") for claim in claims)


def test_unsupported_layer_is_named_and_empty_slices_are_skipped() -> None:
    items = [_question(qid) for qid in SHARED]
    unsupported = copy.deepcopy(items[2])
    unsupported["layer_statuses"]["malloy"] = "unsupported"
    unsupported["comparable_layers"].remove("malloy")
    items[2] = unsupported
    claims = _claims(items)
    assert "across the layers that executed them" in claims[0]
    assert "Malloy did not execute q03." in claims
    assert not any("were chosen to exercise features" in claim for claim in claims)


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (["q01_a", "q02_b", "q03_c"], "q01-q03"),
        (["q03_c", "q01_a", "q04_d", "q05_e"], "q01, q03-q05"),
        (["q07_a"], "q07"),
    ],
)
def test_id_range_compacts_runs(ids: list[str], expected: str) -> None:
    assert generator.id_range(ids) == expected


def test_status_totals_keep_unknown_labels() -> None:
    totals = generator.status_totals(["native", "native", "other"])
    assert totals["native"] == 2 and totals["other"] == 1 and totals["unsupported"] == 0


def test_q11_q12_sentence_follows_the_labels() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    uniform = next(claim for claim in _claims(items) if "q11 and q12" in claim)
    assert "every layer gets the same label there" in uniform
    assert "yet the labels differ" not in uniform
    mixed = next(
        claim
        for claim in _claims(items, {"semantic_rails": "native", "metricflow": "precomputed"})
        if "q11 and q12" in claim
    )
    assert "yet the labels differ" in mixed
    assert "precomputed for MetricFlow" in mixed


def test_unknown_status_gets_a_note_instead_of_crashing() -> None:
    assert generator.default_note("partial") == "Labeled partial."


def test_size_blocks_carry_no_question_or_label_counts() -> None:
    for layer in LAYERS:
        for block in generator.layer_scale(layer).values():
            assert set(block) == {"models", "files", "loc", "relationships"}
