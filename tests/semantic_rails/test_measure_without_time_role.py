"""A measure with no time role, asked for by a date at a time grain, is refused, never a crash.

A claim measure on a model whose open date is a time role but not the model's default, with no
``times:`` on the measure, has no clock. Asking it for ``time: {temporal_role, grain: month}``
used to fail with a bare ``KeyError`` (an ``INTERNAL_ERROR`` at the transports). The rule: a
query with ``time`` never binds a measure that has no clock; it is refused with
``INCOMPATIBLE_TEMPORAL_ROLE`` and a hint to declare one. Once the measure has a clock, the
answer is checked against independent SQL over the same table.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.ast import normalize_query
from semantic_rails.compiler_parts.grain_recovery import _enrichment_unsafe
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE claims AS SELECT * FROM (VALUES
  (1, DATE '2024-01-05', 100.0),
  (2, DATE '2024-01-20', 50.0),
  (3, DATE '2024-02-10', 70.0),
  (4, DATE '2024-04-02', 30.0),
  (5, DATE '2024-08-15', 20.0)
) AS t(claim_id, opened_on, claim_amount);
"""
PACKAGE = """
schema_version: 1
package: {id: ins, namespace: ins, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}, schema_strict: true}
"""
CLAIMS = """
model:
  id: claims
  label: Claims
  relation: claims
  entities: {claim: {}}
  times:
    opened_on: {label: Opened, column: opened_on, kind: date, class: event_time,
      supported_grains: [day, week, month, quarter, year]%(default)s}
  dimensions:
    opened_on_date: {label: Open date, kind: date, column: opened_on}
  measures:
    claim_amount: {label: Claim amount, kind: aggregate, expr: claim_amount,
      accumulation: {kind: flow}, value_type: currency%(times)s}
    paid_amount: {label: Paid amount, kind: aggregate, expr: claim_amount,
      accumulation: {kind: flow}, value_type: currency, times: [opened_on]}
"""
METRICS = """
metrics:
  claim_total:
    as: metric.ins.claim_total
    label: Claim total
    kind: derived
    value_type: currency
    expression: {kind: aggregate, measure: measure.ins.claim_amount}
"""
GRAPH = {
    "graph": {"entities": {"claim": {"label": "Claim", "key": ["claim_id"], "model": "claims"}}}
}

ROLE = "temporal_role.ins_claim_opened_on"
AMOUNT: dict[str, Any] = {"kind": "measure", "measure": "measure.ins.claim_amount"}
SELECT = [{"expression": AMOUNT, "as": "amount"}]
# What each fix to the package looks like: nothing (no clock), the model's default time, or
# a clock listed on the measure.
NO_CLOCK = {"default": "", "times": ""}
DEFAULT_TIME = {"default": ", default: true", "times": ""}
MEASURE_TIME = {"default": "", "times": ", times: [opened_on]"}


def _package(root: Path, variant: dict[str, str]) -> Path:
    package = root / "ins"
    files = {
        "data/seed.sql": SEED,
        "package.yml": PACKAGE,
        "models/claims.yml": CLAIMS % variant,
        "metrics/claims.yml": METRICS,
        "graph.yml": yaml.safe_dump(GRAPH),
    }
    for name, text in files.items():
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text(text, encoding="utf-8")
    return package


def _by_bucket(package: Path, unit: str) -> list[tuple[date, Decimal]]:
    """The independent answer: claim amounts summed by the truncated open date."""
    with duckdb.connect(str(package / "data" / "warehouse.duckdb"), read_only=True) as conn:
        rows = conn.execute(
            f"SELECT date_trunc('{unit}', opened_on)::DATE, SUM(claim_amount) "
            "FROM claims GROUP BY 1 ORDER BY 1"
        ).fetchall()
    return [(bucket, Decimal(str(total))) for bucket, total in rows]


def _answer(package: Path, grain: str) -> list[tuple[date, Decimal]]:
    engine = Runtime.from_path(str(package))
    try:
        result = engine.query(
            {"version": 1, "select": SELECT, "time": {"temporal_role": ROLE, "grain": grain}}
        )
    finally:
        engine.close()
    return sorted(
        (row[f"{ROLE}__{grain}"].date(), Decimal(str(row["amount"]))) for row in result["rows"]
    )


@pytest.mark.parametrize("grain", ["month", "quarter"])
def test_a_measure_with_no_clock_is_refused_when_asked_by_a_date_at_a_grain(tmp_path, grain):
    package = _package(tmp_path, NO_CLOCK)
    engine = Runtime.from_path(str(package))
    try:
        with pytest.raises(SemanticLayerError) as raised:
            engine.query(
                {"version": 1, "select": SELECT, "time": {"temporal_role": ROLE, "grain": grain}}
            )
    finally:
        engine.close()
    error = raised.value
    assert error.code == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert error.details["measure"] == "measure.ins.claim_amount"
    assert error.details["requested"] == ROLE
    assert error.details["compatible"] == []
    assert "default: true" in str(error)


@pytest.mark.parametrize("operation", ["validate", "compile"])
def test_the_refusal_carries_a_recovery_hint_on_every_dry_run(tmp_path, operation):
    package = _package(tmp_path, NO_CLOCK)
    engine = Runtime.from_path(str(package))
    query = {"version": 1, "select": SELECT, "time": {"temporal_role": ROLE, "grain": "month"}}
    try:
        if operation == "compile":
            with pytest.raises(SemanticLayerError) as raised:
                engine.compile(query)
            assert raised.value.code == "INCOMPATIBLE_TEMPORAL_ROLE"
            hints = recovery_hints_for_error(raised.value.code, raised.value.details)
        else:
            report = engine.validate(query)
            assert report["ok"] is False
            assert [item["code"] for item in report["errors"]] == ["INCOMPATIBLE_TEMPORAL_ROLE"]
            hints = report["recovery_hints"]
    finally:
        engine.close()
    assert [item["kind"] for item in hints] == ["declare_measure_time_role"]
    assert hints[0]["measure"] == "measure.ins.claim_amount"
    assert hints[0]["requested_temporal_role"] == ROLE


def test_a_role_override_on_a_clockless_measure_gets_the_same_hint(tmp_path):
    """The natural first fix, an override or an explicit role, must not dead-end."""
    engine = Runtime.from_path(str(_package(tmp_path, NO_CLOCK)))
    time = {"temporal_role": ROLE, "grain": "month"}
    explicit = {"kind": "aggregate", "measure": "measure.ins.claim_amount", "temporal_role": ROLE}
    queries = [
        {
            "version": 1,
            "select": SELECT,
            "time": time,
            "temporal_role_overrides": {"measure.ins.claim_amount": ROLE},
        },
        {"version": 1, "select": [{"expression": explicit, "as": "amount"}]},
    ]
    try:
        reports = [engine.validate(query) for query in queries]
    finally:
        engine.close()
    for report in reports:
        assert [item["code"] for item in report["errors"]] == ["INCOMPATIBLE_TEMPORAL_ROLE"]
        assert report["errors"][0]["details"]["compatible"] == []
        assert [hint["kind"] for hint in report["recovery_hints"]] == ["declare_measure_time_role"]
        assert report["recovery_hints"][0]["measure"] == "measure.ins.claim_amount"
        assert report["recovery_hints"][0]["requested_temporal_role"] == ROLE


def test_over_http_the_refusal_is_a_client_error_never_internal_error(tmp_path):
    engine = Runtime.from_path(str(_package(tmp_path, NO_CLOCK)))
    service = SemanticHTTPService(engine)
    try:
        try:
            body, status = service.handle(
                "POST",
                normalize_route("/api/v1/query"),
                {
                    "version": 1,
                    "select": SELECT,
                    "time": {"temporal_role": ROLE, "grain": "month"},
                },
            )
        except Exception as exc:  # noqa: BLE001 - mirrors the transport wrappers.
            body, status = service.exception_payload(exc, stage="http")
    finally:
        engine.close()
    assert status == 400
    assert body["error"]["code"] == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert [hint["kind"] for hint in body["recovery_hints"]] == ["declare_measure_time_role"]


@pytest.mark.parametrize(
    "expression",
    [
        {"kind": "rolling", "input": AMOUNT, "window": {"unit": "month", "value": 3}},
        {"kind": "prior_period", "input": AMOUNT, "offset": {"unit": "month", "value": 1}},
        {"kind": "arithmetic", "op": "/", "left": AMOUNT, "right": AMOUNT},
        {"kind": "scoped_aggregate", "measure": "measure.ins.claim_amount", "aggregation": "sum"},
    ],
    ids=["rolling", "prior_period", "arithmetic", "scoped_aggregate"],
)
def test_every_expression_over_a_clockless_measure_goes_through_the_same_refusal(
    tmp_path, expression
):
    package = _package(tmp_path, NO_CLOCK)
    engine = Runtime.from_path(str(package))
    try:
        report = engine.validate(
            {
                "version": 1,
                "select": [{"expression": expression, "as": "value"}],
                "time": {"temporal_role": ROLE, "grain": "month"},
            }
        )
    finally:
        engine.close()
    assert [item["code"] for item in report["errors"]] == ["INCOMPATIBLE_TEMPORAL_ROLE"]
    assert report["errors"][0]["details"]["compatible"] == []
    assert [hint["kind"] for hint in report["recovery_hints"]] == ["declare_measure_time_role"]


CLOCKED: dict[str, Any] = {"kind": "measure", "measure": "measure.ins.paid_amount"}
MONTHLY = {"temporal_role": ROLE, "grain": "month"}


@pytest.mark.parametrize(
    "query",
    [
        {
            "select": [
                {
                    "expression": {"kind": "metric", "metric": "metric.ins.claim_total"},
                    "as": "value",
                }
            ]
        },
        {"select": [{"expression": CLOCKED, "as": "paid"}, {"expression": AMOUNT, "as": "amount"}]},
        {
            "select": [{"expression": CLOCKED, "as": "paid"}],
            "metric_filters": [{"expression": AMOUNT, "op": ">", "value": 0}],
        },
    ],
    ids=["metric_ref", "mixed_select", "metric_filters"],
)
def test_the_refusal_names_the_clockless_measure_however_it_is_reached(tmp_path, query):
    engine = Runtime.from_path(str(_package(tmp_path, NO_CLOCK)))
    try:
        report = engine.validate({"version": 1, **query, "time": MONTHLY})
    finally:
        engine.close()
    assert [item["code"] for item in report["errors"]] == ["INCOMPATIBLE_TEMPORAL_ROLE"]
    assert report["errors"][0]["details"]["measure"] == "measure.ins.claim_amount"
    assert report["errors"][0]["details"]["compatible"] == []
    assert [hint["kind"] for hint in report["recovery_hints"]] == ["declare_measure_time_role"]
    assert report["recovery_hints"][0]["measure"] == "measure.ins.claim_amount"


def test_the_grain_recovery_never_suggests_a_time_grain_for_a_clockless_measure(tmp_path):
    """Grouping by a calendar date used to offer 'query it by month', which is now refused."""
    engine = Runtime.from_path(str(_package(tmp_path, NO_CLOCK)))
    try:
        config = engine.config
    finally:
        engine.close()
    date_dim = "dimension.ins_claim_opened_on_date"
    # A package-wide default query clock is what the recovery falls back to for a measure with none.
    config = replace(
        config,
        temporal_roles=[
            replace(role, default_query_time_axis=True) for role in config.temporal_roles
        ],
    )

    def recovery(measure: str) -> dict[str, Any]:
        query = normalize_query(
            {
                "version": 1,
                "select": [{"expression": {"kind": "measure", "measure": measure}, "as": "v"}],
                "group_by": [date_dim],
            }
        )
        enrichment = _enrichment_unsafe(
            config=config, query=query, measure_ids=[measure], target_entity="entity.ins_claim"
        )
        return enrichment["time_axis_recovery"]

    assert (
        recovery("measure.ins.paid_amount")["closest_valid_query"]["time"]["temporal_role"] == ROLE
    )
    clockless = recovery("measure.ins.claim_amount")
    assert clockless["calendar_dimension"] == date_dim
    assert "closest_valid_query" not in clockless


def test_a_clockless_measure_still_answers_without_time_or_by_a_plain_date(tmp_path):
    package = _package(tmp_path, NO_CLOCK)
    engine = Runtime.from_path(str(package))
    try:
        total = engine.query({"version": 1, "select": SELECT})["rows"]
        by_date = engine.query(
            {"version": 1, "select": SELECT, "group_by": ["dimension.ins_claim_opened_on_date"]}
        )["rows"]
    finally:
        engine.close()
    assert [Decimal(str(row["amount"])) for row in total] == [Decimal("270.0")]
    assert sorted(
        (row["dimension.ins_claim_opened_on_date"], Decimal(str(row["amount"]))) for row in by_date
    ) == [
        (date(2024, 1, 5), Decimal("100.0")),
        (date(2024, 1, 20), Decimal("50.0")),
        (date(2024, 2, 10), Decimal("70.0")),
        (date(2024, 4, 2), Decimal("30.0")),
        (date(2024, 8, 15), Decimal("20.0")),
    ]


@pytest.mark.parametrize(
    "variant", [DEFAULT_TIME, MEASURE_TIME], ids=["default_time", "measure_times"]
)
@pytest.mark.parametrize("grain", ["month", "quarter"])
def test_once_the_measure_has_a_clock_the_answer_matches_independent_sql(tmp_path, variant, grain):
    package = _package(tmp_path, variant)
    got = _answer(package, grain)
    assert got == _by_bucket(package, grain)
    assert len(got) >= 3
