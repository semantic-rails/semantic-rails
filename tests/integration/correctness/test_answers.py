"""Frozen answers, independent references, ledger hygiene, and intent planning."""

from decimal import Decimal

import pytest
import yaml

from semantic_rails import package_tools, result_values
from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner import plan_payload

from .answer_ledger import LEDGER, comparable, encode, load_entries, messages

ENTRIES = load_entries()
FIXTURE = yaml.safe_load(LEDGER.read_text(encoding="utf-8"))["fixture"]


def parameters(check):
    for case_id, spec in ENTRIES:
        if check in {"reference", "planner"} and spec["expect"] != "answer":
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
            yield pytest.param(backend, case_id, spec, id=f"{backend}-{case_id}", marks=marks)


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
        clarification = runtime.validate(spec["query"])["errors"][0]["details"]["clarification"]
        assert clarification["question"].strip()
        assert [option["id"] for option in clarification["options"]] == spec["clarify"]["options"]
        for option in clarification["options"]:
            assert runtime.validate({**spec["query"], "where": option["where"]})["ok"]


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
        ({"known_wrong": {"reference": "wrong"}}, {}, "invalid known_wrong"),
        ({"known_wrong": {"engine": " "}}, {}, "invalid known_wrong"),
        ({"variant": "unknown"}, {}, "invalid variant"),
        ({"expected_rows": None}, {}, "invalid expectation"),
        ({}, {"extra": True}, "invalid fixture header"),
    ],
)
def test_hygiene_rejects_bad_entries(duckdb_backend, changes, fixture_changes, error):
    case_id, spec = ENTRIES[0]
    errors = messages(
        [(case_id, {**spec, **changes})],
        {**FIXTURE, **fixture_changes},
        duckdb_backend.runtimes["utc_authored"],
    )
    assert any(error in message for message in errors), errors


@pytest.mark.parametrize("backend,case_id,spec", list(parameters("planner")))
def test_planner_answer(request, backend, case_id, spec):
    runtime = request.getfixturevalue(f"{backend}_backend").runtimes[
        spec.get("variant", "utc_authored")
    ]
    plan = plan_payload(runtime, intent=spec["intent"])
    assert plan["status"] == "ok", plan
    assert "execute" in plan["next"]["ready_for"], plan
    actual = runtime.query(plan["best"]["query_ir"])
    expected = encode(runtime, spec)
    ordered = bool(spec["query"].get("order_by"))
    assert comparable(actual, positional=True, ordered=ordered) == comparable(
        expected, positional=True, ordered=ordered
    ), case_id


@pytest.mark.parametrize(
    "left,right,ordered,equal",
    [
        ([1, 2], [2, 1], False, True),
        ([1, 2], [2, 1], True, False),
        ([1, 1], [1], False, False),
        ([Decimal("1.001")], [Decimal("1.002")], False, False),
    ],
)
def test_row_comparison(left, right, ordered, equal):
    a = result_values.result_rows([{"revenue": value} for value in left])
    b = result_values.result_rows([{"revenue": value} for value in right])
    assert (comparable(a, ordered=ordered) == comparable(b, ordered=ordered)) == equal
