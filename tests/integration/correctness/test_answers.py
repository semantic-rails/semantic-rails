"""Frozen answers, independent references, ledger hygiene, and intent planning."""

from copy import deepcopy
from decimal import Decimal

import pytest
import yaml

from semantic_rails import package_tools, result_values
from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner import plan_payload

from . import answer_ledger
from .answer_ledger import LEDGER, comparable, encode, for_backend, load_entries, messages
from .test_correctness import CASES

ENTRIES = load_entries()
FIXTURE = yaml.safe_load(LEDGER.read_text(encoding="utf-8"))["fixture"]


def answer_template():
    return next((case_id, spec) for case_id, spec in ENTRIES if spec["expect"] == "answer")


def parameters(check):
    for case_id, spec in ENTRIES:
        if check == "reference" and spec["expect"] != "answer":
            continue
        if check == "planner" and "intent" not in spec:
            continue
        for backend in ["duckdb"] if "intent" in spec else ["duckdb", "postgres"]:
            reason = (
                spec.get("known_wrong", {}).get(check) if check in {"engine", "planner"} else None
            )
            marks = (
                [pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)]
                if reason
                else []
            )
            yield pytest.param(
                backend, case_id, for_backend(spec, backend), id=f"{backend}-{case_id}", marks=marks
            )


@pytest.mark.parametrize("backend,case_id,spec", list(parameters("engine")))
def test_engine_answer(request, backend, case_id, spec):
    runtime = request.getfixturevalue(f"{backend}_backend").runtimes[
        spec.get("variant", "utc_authored")
    ]
    report = package_tools._run_test(runtime, case_id, spec)
    assert report["ok"], report
    if spec["expect"] == "answer":
        if spec["query"].get("order_by"):
            assert comparable(runtime.query(spec["query"]), ordered=True) == comparable(
                encode(runtime, spec), ordered=True
            )
        return
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(spec["query"])
    assert raised.value.code == spec["code"]
    if spec["expect"] == "clarify":
        assert_clarification(runtime, spec, spec["query"])


def assert_clarification(runtime, spec, query):
    report = runtime.validate(query)
    assert not report["ok"], report
    error = report["errors"][0]
    assert error["code"] == spec["code"], error
    clarification = error["details"]["clarification"]
    assert clarification["question"].strip()
    assert [option["id"] for option in clarification["options"]] == spec["clarify"]["options"]
    for option in clarification["options"]:
        assert runtime.validate({**query, "where": option["where"]})["ok"], option


@pytest.mark.parametrize("backend,case_id,spec", list(parameters("reference")))
def test_reference_answer(request, backend, case_id, spec):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes[spec.get("variant", "utc_authored")]
    ordered = bool(spec["query"].get("order_by"))
    assert comparable(encode(runtime, spec, source.reference), ordered=ordered) == comparable(
        encode(runtime, spec), ordered=ordered
    ), case_id


def test_ledger_hygiene(duckdb_backend):
    assert not messages(ENTRIES, FIXTURE, duckdb_backend.runtimes["utc_authored"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_correctness_builders_have_frozen_answers(case):
    spec = dict(ENTRIES)[f"shop/{case.name}"]
    assert spec["expect"] == "answer"
    assert spec["query"] == case.query
    assert spec["reference_sql"] == case.reference.strip()
    assert spec["variant"] == case.variant
    assert isinstance(spec["expected_rows"], list)


@pytest.mark.parametrize(
    "citation",
    [
        "tests/integration/correctness/shop/models/orders.yml#model.measures.revenue",
        "tests/integration/correctness/shop/models/orders.yml#model.dimensions.store_id",
        "tests/integration/correctness/shop/models/orders.yml#model.times.ordered_at",
        "docs/QUERY_IR_SCHEMA.md#wherefilter",
        "docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0",
        "docs/QUERY_IR_SCHEMA.md#timeblock",
        "docs/QUERY_IR_SCHEMA.md#child-groups",
    ],
)
def test_hygiene_accepts_definition_or_decision(duckdb_backend, citation):
    case_id, spec = answer_template()
    assert not messages(
        [(case_id, {**spec, "cites": [citation]})],
        FIXTURE,
        duckdb_backend.runtimes["utc_authored"],
    )


@pytest.mark.parametrize(
    "changes,fixture_changes,error",
    [
        ({"unknown": True}, {}, "unknown keys"),
        ({"tags": ["unknown"]}, {}, "unknown tags"),
        ({"cites": ["docs/QUERY_IR_SCHEMA.md#missing"]}, {}, "unresolved citation"),
        ({}, {"data_sha256": "stale"}, "stale fixture fingerprint"),
        ({"expect": "refuse"}, {}, "expect/kind mismatch"),
        ({"reference_sql": ""}, {}, "missing reference_sql"),
        (
            {"cites": ["tests/integration/correctness/conftest.py#CLOCKS"]},
            {},
            "missing declaration citation",
        ),
        (
            {"cites": ["docs/README.md#semantic-rails-docs"]},
            {},
            "missing declaration citation",
        ),
        (
            {"cites": ["tests/integration/correctness/shop/models/orders.yml#model.label"]},
            {},
            "missing declaration citation",
        ),
        (
            {"cites": ["tests/integration/correctness/shop/models/orders.yml#model"]},
            {},
            "missing declaration citation",
        ),
        ({"known_wrong": {"reference": "wrong"}}, {}, "invalid known_wrong"),
        ({"known_wrong": {"engine": " "}}, {}, "invalid known_wrong"),
        ({"variant": "unknown"}, {}, "invalid variant"),
        ({"expected_rows": None}, {}, "invalid expectation"),
        ({"expected_rows_by_backend": {"unknown": []}}, {}, "invalid backend expectation"),
        ({"expected_rows_by_backend": {"postgres": None}}, {}, "invalid backend expectation"),
        ({"partial_query": {}}, {}, "invalid partial_query"),
        ({"intent": "revenue", "partial_query": True}, {}, "invalid partial_query"),
        (
            {"expect": "refuse", "kind": "validate_fails_with_code"},
            {},
            "missing refusal code",
        ),
        (
            {"expect": "clarify", "kind": "validate_fails_with_code", "code": "AMBIGUOUS_PATH"},
            {},
            "invalid clarification options",
        ),
        ({}, {"extra": True}, "invalid fixture header"),
    ],
)
def test_hygiene_rejects_bad_entries(duckdb_backend, changes, fixture_changes, error):
    case_id, spec = answer_template()
    errors = messages(
        [(case_id, {**spec, **changes})],
        {**FIXTURE, **fixture_changes},
        duckdb_backend.runtimes["utc_authored"],
    )
    assert any(error in message for message in errors), errors


@pytest.mark.parametrize("changes", [{}, {"expected_rows": None}])
def test_reference_without_frozen_rows(duckdb_backend, changes):
    spec = dict(ENTRIES)["shop/plan-revenue-by-store"]
    source = duckdb_backend
    runtime = source.runtimes["utc_authored"]
    unfrozen = {key: value for key, value in spec.items() if key != "expected_rows"}
    assert comparable(encode(runtime, {**unfrozen, **changes}, source.reference)) == comparable(
        encode(runtime, spec)
    )


@pytest.mark.parametrize("backend,case_id,spec", list(parameters("planner")))
def test_planner_answer(request, backend, case_id, spec):
    runtime = request.getfixturevalue(f"{backend}_backend").runtimes[
        spec.get("variant", "utc_authored")
    ]
    plan = plan_payload(runtime, intent=spec["intent"], partial_query=spec.get("partial_query"))
    assert_planner_outcome(runtime, spec, plan)


def assert_planner_outcome(runtime, spec, plan):
    if spec["expect"] != "answer":
        assert plan["status"] in {"low_confidence", "unrealizable", "out_of_scope"}, plan
        assert "execute" not in plan["next"].get("ready_for", []), plan
        why = plan["why"]
        assert why["message"].strip(), plan
        error = (why.get("errors") or [why])[0]
        assert error["code"] == spec["code"], plan
        if plan["best"]:
            query = plan["best"]["query_ir"]
            report = runtime.validate(query)
            assert not report["ok"], report
            assert report["errors"][0]["code"] == spec["code"], report
        if spec["expect"] == "clarify":
            assert error["why_invalid"].strip(), error
            assert_clarification(runtime, spec, plan["best"]["query_ir"])
        return
    assert plan["status"] == "ok", plan
    assert "execute" in plan["next"].get("ready_for", []), plan
    actual = runtime.query(plan["best"]["query_ir"])
    expected = encode(runtime, spec)
    ordered = bool(spec["query"].get("order_by"))
    assert comparable(actual, positional=True, ordered=ordered) == comparable(
        expected, positional=True, ordered=ordered
    ), spec["intent"]


@pytest.mark.parametrize(
    "outcome,defect",
    [
        ("answer", "not_ready"),
        ("clarify", "execute"),
        ("clarify", "wrong_code"),
        ("clarify", "empty_question"),
        ("clarify", "wrong_options"),
        ("clarify", "invalid_option_where"),
        ("refuse", "execute"),
        ("refuse", "wrong_code"),
    ],
)
def test_planner_expectations_reject_incomplete_outcomes(
    duckdb_backend, monkeypatch, outcome, defect
):
    ids = {
        "answer": "shop/plan-revenue-by-store",
        "clarify": "shop/plan-refunds-must-say-same-refund-or-separate",
        "refuse": "shop/plan-a-supplied-rolling-average-is-refused",
    }
    spec = dict(ENTRIES)[ids[outcome]]
    runtime = duckdb_backend.runtimes["utc_authored"]
    plan = plan_payload(runtime, intent=spec["intent"], partial_query=spec.get("partial_query"))
    if defect == "not_ready":
        plan["next"] = {}
    elif defect == "execute":
        plan["next"]["ready_for"] = ["execute"]
    elif defect == "wrong_code":
        plan["why"]["errors"][0]["code"] = "INVALID_QUERY"
    else:
        validate = runtime.validate

        def incomplete(query):
            report = deepcopy(validate(query))
            if not report["ok"] and report["errors"][0]["code"] == spec["code"]:
                clarification = report["errors"][0]["details"]["clarification"]
                if defect == "empty_question":
                    clarification["question"] = " "
                elif defect == "wrong_options":
                    clarification["options"][0]["id"] = "unknown"
                else:
                    clarification["options"][0]["where"] = [
                        {"field": "dimension.shop_order_store_id", "op": "IS", "value": "x"}
                    ]
            return report

        monkeypatch.setattr(runtime, "validate", incomplete)
    with pytest.raises(AssertionError):
        assert_planner_outcome(runtime, spec, plan)


@pytest.mark.parametrize(
    "literal", ["0.12345678901234567890123456789012345678", "9007199254740993.01"]
)
def test_frozen_decimal_literals_remain_exact(tmp_path, monkeypatch, literal):
    tests = tmp_path / "tests"
    tests.mkdir()
    ledger = tests / "answers.yml"
    ledger.write_text(
        f"tests:\n  exact:\n    expected_rows: [{{value: {literal}}}]\n"
        f"    expected_rows_by_backend:\n      postgres: [{{value: {literal}}}]\n"
    )
    monkeypatch.setattr(answer_ledger, "SHOP", tmp_path)
    monkeypatch.setattr(answer_ledger, "LEDGER", ledger)
    spec = answer_ledger.load_entries()[0][1]
    for backend in ("duckdb", "postgres"):
        value = for_backend(spec, backend)["expected_rows"][0]["value"]
        assert isinstance(value, Decimal)
        assert value == Decimal(literal)


@pytest.mark.parametrize(
    "backend,expected",
    [("duckdb", "7.333333333333333"), ("postgres", "7.3333333333333333")],
)
def test_native_backend_representation_is_selected_exactly(backend, expected):
    spec = {
        "expected_rows": [{"average": Decimal("7.333333333333333")}],
        "expected_rows_by_backend": {"postgres": [{"average": Decimal("7.3333333333333333")}]},
    }
    assert for_backend(spec, backend)["expected_rows"] == [{"average": Decimal(expected)}]


def test_reference_checks_cannot_be_waived(monkeypatch):
    _, spec = answer_template()
    monkeypatch.setitem(
        globals(),
        "ENTRIES",
        [("unwaivable", {**spec, "known_wrong": {"engine": "wrong", "reference": "wrong"}})],
    )
    references = list(parameters("reference"))
    assert references
    assert all(not param.marks for param in references)


@pytest.mark.parametrize(
    "left,right,ordered,equal",
    [
        ([1, 2], [2, 1], False, True),
        ([1, 2], [2, 1], True, False),
        ([1, 1], [1], False, False),
        ([Decimal("1.001")], [Decimal("1.002")], False, False),
        (
            [Decimal("0.12345678901234567890123456789012345678")],
            [Decimal("0.12345678901234567890123456789012345679")],
            False,
            False,
        ),
    ],
)
def test_row_comparison(left, right, ordered, equal):
    a = result_values.result_rows([{"revenue": value} for value in left])
    b = result_values.result_rows([{"revenue": value} for value in right])
    assert (comparable(a, ordered=ordered) == comparable(b, ordered=ordered)) == equal
