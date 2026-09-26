"""Rollups that require certification, the certification provider, and the certify API."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

from semantic_rails.acceleration.certification import certify_aggregate_relation
from semantic_rails.acceleration.routing import set_certification_provider
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_model_physical_variants import (
    _BUYERS,
    _DISTINCT_BUYERS,
    _MONTHLY,
    _NO_PATH,
    _PRODUCT,
    _REGION,
    _REVENUE,
    _ROLLUP_SEED,
    _S1_ONLY,
    _WEEKLY,
    _decisions,
    _monthly,
    _rollup_package,
    _rollup_query,
    _routed_answers,
)

_GATED = ({"monthly": {**_MONTHLY, "requires_certification": True}}, [])
_MONTHLY_ID = "aggregate_relation.orders_monthly"


class _Provider:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def certified(self, config, relation) -> object:
        self.asked.append(relation.id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def install():
    yield set_certification_provider
    set_certification_provider(None)


@pytest.mark.parametrize(
    ("answer", "query", "reason", "asked"),
    [
        pytest.param(None, "sum", "not_certified", False, id="no-provider"),
        pytest.param(True, "sum", None, True, id="certified"),
        pytest.param(False, "sum", "not_certified", True, id="not-certified"),
        pytest.param(1, "sum", "not_certified", True, id="only-true-certifies"),
        pytest.param(RuntimeError("down"), "sum", "not_certified", True, id="provider-fails"),
        # Certification is asked only once every other rule holds.
        pytest.param(True, "max", "aggregation_not_reaggregable", False, id="other-rule-first"),
    ],
)
def test_certification_gates_routing(tmp_path: Path, install, answer, query, reason, asked):
    provider = None if answer is None else _Provider(answer)
    install(provider)
    routing = _routed_answers(tmp_path, _GATED, _rollup_query(_REVENUE, query, "quarter"))

    assert _decisions(routing) == {f"leaf_1:{_MONTHLY_ID}": reason or "selected"}
    assert bool(provider and provider.asked) == asked


def test_revoked_certification_applies_to_the_next_request(tmp_path: Path, install):
    _rollup_package(tmp_path / "p", *_GATED)
    runtime = Runtime.from_path(str(tmp_path / "p"))
    payload = {**_rollup_query(_REVENUE, "sum", "quarter"), "verbosity": "full"}
    provider = _Provider(True)
    install(provider)

    def compile_once() -> tuple[list[str], bool]:
        compiled = runtime.compile(payload)
        return (
            compiled["performance_plan"]["aggregate_routing"]["selected"],
            compiled["compile_stats"]["cache_hit"],
        )

    assert compile_once() == ([_MONTHLY_ID], False)
    assert compile_once() == ([_MONTHLY_ID], False)  # a second request isn't a cache hit either
    provider.answer = False
    assert compile_once() == ([], False)  # the revocation applies to the very next request

    _rollup_package(tmp_path / "ungated", {"monthly": _MONTHLY}, [])
    ungated = Runtime.from_path(str(tmp_path / "ungated"))
    ungated.compile(payload)
    assert ungated.compile(payload)["compile_stats"]["cache_hit"]  # other packages still cache


def test_certification_settings_are_checked(tmp_path: Path):
    _rollup_package(tmp_path / "p", {"monthly": {**_MONTHLY, "requires_certification": "yes"}}, [])
    with pytest.raises(SemanticLayerError, match="requires_certification must be true or false"):
        load_package_config(str(tmp_path / "p"))
    with pytest.raises(TypeError):
        set_certification_provider(object())  # type: ignore[arg-type]


_WEEKLY_ONLY_REVENUE = {
    **_WEEKLY,
    "excludes": {**_WEEKLY["excludes"], "measures": ["order_count", "buyers", "balance"]},
}
_NOT_REAGGREGABLE = "aggregation_not_reaggregable"


@pytest.mark.parametrize(
    ("rollups", "relation_id", "reasons"),
    [
        pytest.param(
            ({"monthly": _MONTHLY}, []),
            _MONTHLY_ID,
            {
                _REVENUE: "",
                "measure.order_count": "",
                _BUYERS: _NOT_REAGGREGABLE,  # a distinct count without `holds:`
                "measure.balance": _NOT_REAGGREGABLE,  # a stock measure
            },
            id="monthly",
        ),
        pytest.param(_DISTINCT_BUYERS, _MONTHLY_ID, {_BUYERS: ""}, id="declared-distinct"),
        pytest.param(
            _monthly(revenue={"column": "max_amount", "holds": "max"}),
            _MONTHLY_ID,
            {_REVENUE: ""},
            id="max",
        ),
        pytest.param(
            ({"weekly": _WEEKLY_ONLY_REVENUE}, []),
            "aggregate_relation.orders_weekly",
            {_REVENUE: ""},
            id="weekly",
        ),
        pytest.param(({}, [_REGION], {"ship_to": False}), _REGION["id"], {_REVENUE: ""}, id="path"),
        pytest.param(
            ({}, [_REGION], {"ship_to": True}),
            _REGION["id"],
            {_REVENUE: "join_path_mismatch"},
            id="other-path",
        ),
        pytest.param(
            ({}, [_NO_PATH], {"ship_to": False}),
            _REGION["id"],
            {_REVENUE: "join_path_mismatch"},
            id="undeclared-path",
        ),
        pytest.param(
            ({}, [_PRODUCT], {"lines": True}),
            _PRODUCT["id"],
            {_REVENUE: "query_not_compiled"},  # the base path can't group by a line's product
            id="one-to-many-pre-join",
        ),
        pytest.param(
            ({}, [_S1_ONLY]), _S1_ONLY["id"], {_REVENUE: "rollup_filter_not_implied"}, id="filter"
        ),
        pytest.param(
            ({"monthly": {**_MONTHLY, "equivalence": {"kind": "approximate"}}}, []),
            _MONTHLY_ID,
            {_REVENUE: "non_exact_equivalence"},
            id="approximate",
        ),
        pytest.param(
            (
                {"monthly": _MONTHLY},
                [],
                {"time": {"timezone": "America/New_York", "column_timezone": "UTC"}},
            ),
            _MONTHLY_ID,
            {_REVENUE: "timezone_mismatch"},
            id="timezone",
        ),
        pytest.param(
            _monthly(revenue={"column": "max_amount", "holds": "max", "aggregation": "sum"}),
            _MONTHLY_ID,
            {_REVENUE: "unsupported_rollup_aggregation"},
            id="max-re-added",
        ),
    ],
)
def test_certify_pairs_answer_alike(tmp_path: Path, rollups: tuple, relation_id: str, reasons):
    _rollup_package(tmp_path / "p", *rollups)
    config = load_package_config(str(tmp_path / "p"))
    verdict = certify_aggregate_relation(config, relation_id)
    table = next(row.relation for row in config.aggregate_relations if row.id == relation_id)
    connection = duckdb.connect()
    connection.execute(_ROLLUP_SEED)

    got = {item["measure_id"]: item["reason"] for item in verdict["measures"]}
    assert {measure: got[measure] for measure in reasons} == reasons
    assert verdict["certifiable"] == (not any(got.values()))
    for item in verdict["measures"]:
        assert not re.search(rf"\b{table}\b", item["base_sql"])
        if not item["reason"]:  # the pair a host compares before certifying
            assert re.search(rf"\b{table}\b", item["rollup_sql"])
            rollup = sorted(connection.execute(item["rollup_sql"]).fetchall())
            assert rollup == sorted(connection.execute(item["base_sql"]).fetchall())


def test_certify_names_an_unknown_relation(tmp_path: Path):
    _rollup_package(tmp_path / "p", *_GATED)
    with pytest.raises(SemanticLayerError, match="Unknown aggregate relation"):
        certify_aggregate_relation(load_package_config(str(tmp_path / "p")), "nope")
