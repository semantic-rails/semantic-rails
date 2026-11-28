"""``semantic-rails eval`` scores a package's answers file: wrong numbers fail, misses warn."""

from __future__ import annotations

import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from semantic_rails.cli import app as cli_app
from semantic_rails.cli import eval_answers
from semantic_rails.cli.eval_answers import FAILED, MISSED, PASSED, SKIPPED, row_differences
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.conftest import copy_package_config

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "tests/integration/correctness/shop/tests/answers.yml"

REPEAT_ORDERS = {
    "kind": "query_matches_snapshot",
    "expect": "answer",
    "why": "A repeat customer has more than one lifetime order.",
    "query": {
        "select": [{"expression": "metric.sales.repeat_customer_orders", "as": "repeat_orders"}],
        "group_by": ["dimension.jaffle_store_name"],
    },
    "expected_rows": [
        {"dimension.jaffle_store_name": "Brooklyn", "repeat_orders": 21460},
        {"dimension.jaffle_store_name": "Philadelphia", "repeat_orders": 38186},
    ],
}
RANKED_STORES = {
    "kind": "query_matches_snapshot",
    "expect": "answer",
    "query": {
        "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
        "group_by": ["dimension.jaffle_store_name"],
        "order_by": [{"field": "orders", "direction": "desc"}],
    },
    "expected_rows": [
        {"dimension.jaffle_store_name": "Philadelphia", "orders": 38187},
        {"dimension.jaffle_store_name": "Brooklyn", "orders": 21465},
    ],
}
ORDERS_BY_YEAR = {
    "kind": "query_matches_snapshot",
    "expect": "answer",
    "query": {
        "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "year"},
    },
    "expected_rows": [
        {"period": "2016-01-01", "orders": 8194},
        {"period": "2017-01-01", "orders": 51458},
    ],
}
PARTNER_REVENUE = {
    "kind": "validate_fails_with_code",
    "expect": "refuse",
    "code": "POLICY_DENIED",
    "why": "External partners never see raw revenue.",
    "query": {"select": [{"expression": "measure.jaffle.revenue_usd", "as": "revenue"}]},
    "context": {"audience": "external_partner"},
}
UNGOVERNED_PROFIT = {
    "kind": "validate_fails_with_code",
    "expect": "refuse",
    "code": "OBJECT_NOT_FOUND",
    "query": {"select": [{"expression": "measure.jaffle.profit", "as": "profit"}]},
}
CLEAN = {
    "jaffle/repeat-orders-by-store": REPEAT_ORDERS,
    "jaffle/stores-ranked-by-orders": RANKED_STORES,
    "jaffle/orders-by-year": ORDERS_BY_YEAR,
    "jaffle/partner-revenue-refuses": PARTNER_REVENUE,
    "jaffle/profit-is-not-governed": UNGOVERNED_PROFIT,
}


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    return copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)


def write_answers(package: Path, tests: dict, **top) -> Path:
    path = package / "tests" / "answers.yml"
    path.write_text(yaml.safe_dump({**top, "tests": tests}, sort_keys=False), encoding="utf-8")
    return path


def replace_in(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def run_eval(monkeypatch, capsys, package: Path, *flags: str) -> tuple[int, str]:
    argv = ["semantic-rails", "eval", "--path", str(package), *flags]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        cli_app.main()
    except SystemExit as exc:
        return int(exc.code or 0), capsys.readouterr().out
    return 0, capsys.readouterr().out


def report(package: Path, **options) -> dict:
    return eval_answers.run_answers_report(
        resolve_package_reference(package_id="", path=str(package)), **options
    )


def outcomes(result: dict) -> dict[str, str]:
    return {row["id"]: row["outcome"] for row in result["answers"]}


def test_a_clean_package_passes(monkeypatch, capsys, package):
    write_answers(package, CLEAN)
    code, out = run_eval(monkeypatch, capsys, package)
    assert code == 0, out
    assert out.strip().splitlines()[-1] == "5 answers: 5 passed. Exit 0."

    code, out = run_eval(monkeypatch, capsys, package, "--json", "--strict")
    payload = json.loads(out)
    assert code == 0
    assert payload["ok"] is True and payload["exit_code"] == 0
    assert set(outcomes(payload).values()) == {PASSED}
    assert payload["summary"]["answers_total"] == 5


def test_a_mutated_metric_filter_fails_naming_the_call_and_cells(monkeypatch, capsys, package):
    write_answers(package, CLEAN)
    metrics = package / "metrics" / "extensions" / "advanced_metrics.yml"
    replace_in(
        metrics,
        '                measure: lifetime_order_count\n              op: ">"\n              value: 1\n',
        '                measure: lifetime_order_count\n              op: ">"\n              value: 2\n',
    )
    code, out = run_eval(monkeypatch, capsys, package)
    assert code == 1, out
    assert "FAIL  jaffle/repeat-orders-by-store: wrong rows" in out
    assert '"metric.sales.repeat_customer_orders"' in out
    assert 'row {"dimension.jaffle_store_name": "Brooklyn"}: repeat_orders expected 21460' in out
    assert out.strip().splitlines()[-1] == "5 answers: 4 passed, 1 failed. Exit 1."

    result = report(package)
    failed = next(row for row in result["answers"] if row["outcome"] == FAILED)
    assert failed["id"] == "jaffle/repeat-orders-by-store"
    assert failed["query"] == REPEAT_ORDERS["query"]
    [change] = failed["details"]["changed_rows"]
    assert change["row"] == {"dimension.jaffle_store_name": "Brooklyn"}
    [cell] = change["cells"]
    assert cell["field"] == "repeat_orders" and cell["expected"] == 21460
    assert cell["actual"] < 21460


def test_an_out_of_scope_question_that_returns_a_number_fails(monkeypatch, capsys, package):
    write_answers(package, CLEAN)
    # The change drops the partner audience from the revenue denial.
    replace_in(
        package / "policies.yml",
        "    audiences: [external_partner]\n    action: deny\n",
        "    audiences: [finance_only]\n    action: deny\n",
    )
    code, out = run_eval(monkeypatch, capsys, package)
    assert code == 1, out
    assert (
        "FAIL  jaffle/partner-revenue-refuses: returned 1 row(s) for a call that must refuse "
        "with POLICY_DENIED" in out
    )
    result = report(package, strict=False)
    assert outcomes(result)["jaffle/partner-revenue-refuses"] == FAILED
    assert result["summary"][MISSED] == 0
    assert result["exit_code"] == 1


def test_a_refusal_where_rows_were_expected_is_a_miss(monkeypatch, capsys, package):
    write_answers(package, CLEAN)
    # The change renames the metric the answer asks for.
    replace_in(
        package / "metrics" / "extensions" / "advanced_metrics.yml",
        "    as: metric.sales.repeat_customer_orders\n",
        "    as: metric.sales.returning_customer_orders\n",
    )
    code, out = run_eval(monkeypatch, capsys, package)
    assert code == 2, out
    assert "MISS  jaffle/repeat-orders-by-store: refused with OBJECT_NOT_FOUND" in out
    assert out.strip().splitlines()[-1] == "5 answers: 4 passed, 1 missed. Exit 2."

    code, out = run_eval(monkeypatch, capsys, package, "--strict")
    assert code == 1, out
    assert out.strip().endswith("Exit 1 (strict).")


def test_a_miss_and_a_failure_exit_1(package):
    write_answers(
        package,
        {
            "jaffle/missing-metric": {**REPEAT_ORDERS, "query": UNGOVERNED_PROFIT["query"]},
            "jaffle/answers-anyway": {**UNGOVERNED_PROFIT, "query": RANKED_STORES["query"]},
        },
    )
    result = report(package)
    assert outcomes(result) == {"jaffle/missing-metric": MISSED, "jaffle/answers-anyway": FAILED}
    assert result["exit_code"] == 1


def test_rows_out_of_order_fail_when_the_call_orders_them(package):
    rows = list(reversed(RANKED_STORES["expected_rows"]))
    write_answers(package, {"jaffle/ranked": {**RANKED_STORES, "expected_rows": rows}})
    [row] = report(package)["answers"]
    assert row["outcome"] == FAILED
    assert row["reason"] == "the rows are in a different order"
    assert [change["position"] for change in row["details"]["changed_rows"]] == [1, 2]


def test_a_refusal_with_another_code_fails(package):
    write_answers(package, {"jaffle/wrong-code": {**PARTNER_REVENUE, "code": "FANOUT_UNSAFE"}})
    [row] = report(package)["answers"]
    assert row["outcome"] == FAILED
    assert row["reason"] == "refused with POLICY_DENIED where FANOUT_UNSAFE was expected"


def test_a_file_now_applies_to_entries_without_one(package):
    entry = {**ORDERS_BY_YEAR, "expected_rows": [{"period": "2017-01-01", "orders": 27397}]}
    entry["query"] = {**entry["query"], "time": {**entry["query"]["time"], "this": "year"}}
    write_answers(package, {"jaffle/this-year": entry}, now="2017-06-01")
    assert outcomes(report(package)) == {"jaffle/this-year": PASSED}
    write_answers(package, {"jaffle/this-year": {**entry, "now": "2017-12-31"}}, now="2017-06-01")
    assert outcomes(report(package)) == {"jaffle/this-year": FAILED}


def test_invalid_entries_fail_and_name_their_problems(package):
    write_answers(
        package,
        {
            "jaffle/no-kind": {**REPEAT_ORDERS, "kind": "validate_fails_with_code"},
            "jaffle/no-code": {key: v for key, v in PARTNER_REVENUE.items() if key != "code"},
            "jaffle/typo": {**REPEAT_ORDERS, "expected_row": []},
        },
    )
    result = report(package)
    assert set(outcomes(result).values()) == {FAILED}
    problems = {row["id"]: row["details"]["problems"] for row in result["answers"]}
    assert problems == {
        "jaffle/no-kind": ["expect: answer needs kind: query_matches_snapshot"],
        "jaffle/no-code": ["a refuse entry needs the refusal code"],
        "jaffle/typo": ["unknown keys: expected_row"],
    }


def test_a_missing_answers_file_fails(monkeypatch, capsys, package):
    code, out = run_eval(monkeypatch, capsys, package)
    assert code == 1
    assert "ERROR [INVALID_CONFIG]" in out and "no answers file" in out


def test_an_answers_file_without_entries_fails(package):
    write_answers(package, {})
    result = report(package)
    assert result["exit_code"] == 1
    assert "has no entries" in result["errors"][0]["message"]


def test_known_wrong_marks_a_failing_entry_until_it_passes(package):
    wrong = {**REPEAT_ORDERS, "expected_rows": REPEAT_ORDERS["expected_rows"][:1]}
    write_answers(
        package,
        {
            "jaffle/still-wrong": {**wrong, "known_wrong": {"engine": "Philadelphia is dropped."}},
            "jaffle/fixed": {**REPEAT_ORDERS, "known_wrong": {"engine": "Fixed since."}},
        },
    )
    result = report(package)
    assert outcomes(result) == {"jaffle/still-wrong": "known_wrong", "jaffle/fixed": FAILED}
    assert result["exit_code"] == 1


def test_two_entries_with_one_name_fail_to_load(monkeypatch, capsys, package):
    """A copied entry left with its name must not hide the first one's check."""
    wrong = {**REPEAT_ORDERS, "expected_rows": REPEAT_ORDERS["expected_rows"][:1]}
    entries = [
        yaml.safe_dump({"tests": {"jaffle/repeat-orders": spec}}, sort_keys=False)
        for spec in (wrong, REPEAT_ORDERS)
    ]
    path = package / "tests" / "answers.yml"
    path.write_text(entries[0] + entries[1].removeprefix("tests:\n"), encoding="utf-8")
    assert path.read_text(encoding="utf-8").count("jaffle/repeat-orders:") == 2

    code, out = run_eval(monkeypatch, capsys, package)
    assert code == 1, out
    assert "ERROR [INVALID_CONFIG]" in out
    assert str(path) in out and "names 'jaffle/repeat-orders' twice" in out

    with pytest.raises(SemanticLayerError, match="names 'jaffle/repeat-orders' twice"):
        eval_answers.load_answers(path)


def test_a_repeated_key_inside_an_entry_fails_to_load(tmp_path):
    path = tmp_path / "answers.yml"
    path.write_text(
        "tests:\n  a:\n    kind: query_matches_snapshot\n    expect: answer\n"
        "    query: {select: [measure.x]}\n"
        "    expected_rows: [{x: 1, x: 2}]\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticLayerError, match="names 'x' twice"):
        eval_answers.load_answers(path)


@pytest.mark.parametrize("flags", [(), ("--json",), ("--strict",), ("--json", "--strict")])
def test_known_wrong_never_excuses_a_call_that_failed_to_run(monkeypatch, capsys, package, flags):
    def warehouse_down(self, *args, **kwargs):
        raise SemanticLayerError("QUERY_EXECUTION_ERROR", "the warehouse is unavailable")

    marker = {"known_wrong": {"engine": "Philadelphia is dropped."}}
    wrong = {**REPEAT_ORDERS, "expected_rows": REPEAT_ORDERS["expected_rows"][:1], **marker}
    # The refusal validates, so eval asks the warehouse, which fails.
    answers_anyway = {**UNGOVERNED_PROFIT, "query": RANKED_STORES["query"], **marker}
    write_answers(package, {"jaffle/still-wrong": wrong, "jaffle/answers-anyway": answers_anyway})
    monkeypatch.setattr(eval_answers.Runtime, "query", warehouse_down)

    code, out = run_eval(monkeypatch, capsys, package, *flags)
    assert code == 1, out
    if "--json" in flags:
        payload = json.loads(out)
        assert payload["ok"] is False and payload["exit_code"] == 1
        assert payload["summary"][FAILED] == 2 and payload["summary"]["known_wrong"] == 0
        for row in payload["answers"]:
            assert row["outcome"] == FAILED
            assert row["run_error"] == "QUERY_EXECUTION_ERROR"
            assert row["reason"] == "the call failed: QUERY_EXECUTION_ERROR"
    else:
        assert "FAIL  jaffle/still-wrong: the call failed: QUERY_EXECUTION_ERROR" in out
        assert "FAIL  jaffle/answers-anyway: the call failed: QUERY_EXECUTION_ERROR" in out


LARGE = "dimension.jaffle_order_is_large_order"
ORDER_NUMBER = "dimension.jaffle_order_customer_order_number"
BY_DESC_ORDERS = [{"field": "orders", "direction": "desc"}]


def boolean_answer(group: str, value, expected, *, ordered: bool) -> dict:
    query = {
        "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
        "group_by": [group],
        **({"where": [{"field": group, "op": "is", "value": value}]} if value is not None else {}),
        **({"order_by": BY_DESC_ORDERS} if ordered else {}),
    }
    return {
        "kind": "query_matches_snapshot",
        "expect": "answer",
        "query": query,
        "expected_rows": expected,
    }


@pytest.mark.parametrize("ordered", [False, True], ids=["unordered", "ordered"])
def test_a_boolean_cell_matches_only_a_boolean(package, ordered):
    """Python equality has true equal 1; an answer's true/false cells stay apart from numbers."""
    first_orders = [{ORDER_NUMBER: True, "orders": 939}]
    small_orders = [{LARGE: 0, "orders": 54742}]
    split = [{LARGE: False, "orders": 54742}, {LARGE: True, "orders": 4910}]
    write_answers(
        package,
        {
            "jaffle/true-is-not-1": boolean_answer(ORDER_NUMBER, 1, first_orders, ordered=ordered),
            "jaffle/0-is-not-false": boolean_answer(LARGE, False, small_orders, ordered=ordered),
            "jaffle/true-is-true": boolean_answer(LARGE, None, split, ordered=ordered),
        },
    )
    result = report(package)
    assert outcomes(result) == {
        "jaffle/true-is-not-1": FAILED,
        "jaffle/0-is-not-false": FAILED,
        "jaffle/true-is-true": PASSED,
    }
    assert result["exit_code"] == 1
    rows = {row["id"]: row for row in result["answers"]}
    for case_id, field, expected, actual in [
        ("jaffle/true-is-not-1", ORDER_NUMBER, True, 1),
        ("jaffle/0-is-not-false", LARGE, 0, False),
    ]:
        assert rows[case_id]["reason"] == "wrong rows"
        [change] = rows[case_id]["details"]["changed_rows"]
        [cell] = change["cells"]
        assert cell == {"field": field, "expected": expected, "actual": actual}
        assert type(cell["expected"]) is type(expected) and type(cell["actual"]) is type(actual)


def test_a_harness_variant_is_skipped(package):
    write_answers(package, {"jaffle/variant": {**REPEAT_ORDERS, "variant": "local_authored"}})
    result = report(package)
    assert outcomes(result) == {"jaffle/variant": SKIPPED}
    assert result["exit_code"] == 0


def test_frozen_decimals_load_exactly(tmp_path):
    path = tmp_path / "answers.yml"
    path.write_text(
        "tests:\n  a:\n    kind: query_matches_snapshot\n    expect: answer\n"
        "    query: {select: [measure.x], where: [{field: f, op: gt, value: 1.5}]}\n"
        "    expected_rows: [{x: 0.1000000000000000055511151231257827}]\n",
        encoding="utf-8",
    )
    [(_, spec)] = eval_answers.load_answers(path)
    assert spec["expected_rows"] == [{"x": Decimal("0.1000000000000000055511151231257827")}]
    assert spec["query"]["where"][0]["value"] == 1.5


def test_every_correctness_ledger_entry_fits_the_schema():
    """The engine's own answer ledger is an answers file: eval's schema is a superset."""
    entries = eval_answers.load_answers(LEDGER)
    assert len(entries) > 300
    problems = {case_id: eval_answers.entry_problems(spec) for case_id, spec in entries}
    assert {case_id: found for case_id, found in problems.items() if found} == {}


def test_eval_agrees_with_the_correctness_ledger(tmp_path):
    """Every ledger answer the shop package states as written passes; variants are skipped."""
    package = tmp_path / "shop"
    shutil.copytree(LEDGER.parents[1], package, ignore=shutil.ignore_patterns("*.duckdb*"))
    result = report(package)
    assert result["errors"] == []
    summary = result["summary"]
    assert (summary[FAILED], summary[MISSED]) == (0, 0), [
        row for row in result["answers"] if row["outcome"] in {FAILED, MISSED}
    ]
    assert summary[PASSED] > 250
    skipped = [row for row in result["answers"] if row["outcome"] == SKIPPED]
    assert all(row["reason"].startswith("variant ") for row in skipped)


def test_eval_reads_every_ledger_key():
    from tests.integration.correctness import answer_ledger

    assert answer_ledger.KEYS <= eval_answers.KEYS
    assert answer_ledger.KINDS == eval_answers.KINDS


@pytest.mark.parametrize(
    "expected,actual,ordered,changed,missing,unexpected",
    [
        ([{"g": "a", "v": 1}], [{"g": "a", "v": 1}], False, [], [], []),
        (
            [{"g": "a", "v": 1}, {"g": "b", "v": 2}],
            [{"g": "b", "v": 2}, {"g": "a", "v": 3}],
            False,
            [{"row": {"g": "a"}, "cells": [{"field": "v", "expected": 1, "actual": 3}]}],
            [],
            [],
        ),
        ([{"g": "a", "v": 1}], [{"h": "z"}], False, [], [{"g": "a", "v": 1}], [{"h": "z"}]),
        (
            [{"g": "a", "v": True}],
            [{"g": "a", "v": 1}],
            False,
            [{"row": {"g": "a"}, "cells": [{"field": "v", "expected": True, "actual": 1}]}],
            [],
            [],
        ),
        (
            [{"v": 0}],
            [{"v": False}],
            True,
            [{"position": 1, "row": {}, "cells": [{"field": "v", "expected": 0, "actual": False}]}],
            [],
            [],
        ),
        ([{"v": False}], [{"v": False}], True, [], [], []),
        (
            [{"g": "a"}, {"g": "b"}],
            [{"g": "b"}],
            True,
            [{"position": 1, "row": {}, "cells": [{"field": "g", "expected": "a", "actual": "b"}]}],
            [{"g": "b"}],
            [],
        ),
    ],
)
def test_row_differences(expected, actual, ordered, changed, missing, unexpected):
    assert row_differences(expected, actual, ordered=ordered) == {
        "changed_rows": changed,
        "missing_rows": missing,
        "unexpected_rows": unexpected,
    }


@pytest.mark.parametrize(
    "summary,strict,code",
    [
        ({FAILED: 0, MISSED: 0}, False, 0),
        ({FAILED: 1, MISSED: 0}, False, 1),
        ({FAILED: 0, MISSED: 2}, False, 2),
        ({FAILED: 0, MISSED: 2}, True, 1),
        ({FAILED: 1, MISSED: 2}, False, 1),
    ],
)
def test_exit_codes(summary, strict, code):
    assert eval_answers.exit_code(summary, strict=strict) == code
