"""``kind: lookup`` measures carry a parent's measure total onto each of its child rows.

A coverage's premium (the sum of its premium rows) sits on each claim made against it, and
the engine never adds that total across two coverages or repeats it over a claim's own child
rows. Every number here is checked against SQL written on the base tables, not the engine.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import NonAdditiveRefusal, bind_query, compile_query, plan_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import opened

NS = "lkp"
CLAIM_KEY = f"dimension.{NS}_claim_id"
COVERAGE_KEY = f"dimension.{NS}_coverage_id"
POLICY_KEY = f"dimension.{NS}_policy_id"
LINE_TYPE = f"dimension.{NS}_claim_line_line_type"
PREMIUM_KIND = f"dimension.{NS}_premium_premium_kind"
OPENED = f"dimension.{NS}_claim_opened_on"
OPENED_ROLE = f"temporal_role.{NS}_claim_opened_on"
BOOKED_ROLE = f"temporal_role.{NS}_premium_booked_on"
CARRIED = f"measure.{NS}.coverage_premium"


def _measure(key: str) -> str:
    return f"measure.{NS}.{key}"


def _models() -> dict[str, dict[str, Any]]:
    """The package's models; tests change a copy to author a bad declaration."""
    day = {"kind": "date", "class": "event_time", "default": True}
    lookup = {"kind": "lookup", "via": "coverage"}
    return {
        "policies": {
            "relation": "policy",
            "entities": {"policy": {}},
            "dimensions": {"region": {}},
        },
        "coverages": {
            "relation": "coverage",
            "entities": {"coverage": {}, "policy": {}},
            "dimensions": {"line_of_business": {}},
        },
        "premiums": {
            "relation": "premium",
            "entities": {"premium": {}, "coverage": {}},
            "dimensions": {"premium_kind": {}},
            "times": {"booked_on": {"column": "booked_on", **day}},
            "measures": {
                "premium_amount": {
                    "kind": "aggregate",
                    "expr": "amount",
                    "value_type": "currency",
                    "currency": "USD",
                },
                "written_premium": {
                    "kind": "aggregate",
                    "value_type": "currency",
                    "expr": {
                        "kind": "case",
                        "whens": [
                            {
                                "when": {
                                    "kind": "comparison",
                                    "op": "=",
                                    "left": {"kind": "column", "column": "premium_kind"},
                                    "right": {"kind": "literal", "value": "written"},
                                },
                                "then": {"kind": "column", "column": "amount"},
                            }
                        ],
                        "else": {"kind": "literal", "value": None},
                    },
                },
                "average_premium": {"kind": "aggregate", "expr": "amount", "default_agg": "avg"},
                "waived_amount": {"kind": "aggregate", "expr": "waived"},
                "premium_rows": {"kind": "entity_count", "entity_key": "premium_id"},
                "premium_rate": {"kind": "aggregate", "expr": "amount", "additive": False},
                "premium_balance": {
                    "kind": "aggregate",
                    "expr": "amount",
                    "accumulation": {"kind": "stock"},
                },
            },
        },
        "claims": {
            "relation": "claim",
            "entities": {"claim": {}, "coverage": {}},
            "times": {"opened_on": {"column": "opened_on", **day}},
            "measures": {
                "claim_count": {"kind": "entity_count", "entity_key": "claim_id"},
                "reserve": {"kind": "aggregate", "expr": "reserve"},
                "coverage_premium": {**lookup, "from": "premium_amount"},
                "written_coverage_premium": {**lookup, "from": "written_premium"},
                "average_coverage_premium": {**lookup, "from": "average_premium"},
                "waived_coverage_amount": {**lookup, "from": "waived_amount"},
            },
        },
        "claim_lines": {
            "relation": "claim_line",
            "entities": {"claim_line": {}, "claim": {}},
            "dimensions": {"line_type": {}},
            "measures": {"line_amount": {"kind": "aggregate", "expr": "line_amount"}},
        },
    }


def _write(
    root: Path,
    models: dict[str, dict[str, Any]],
    coverage_key: tuple[str, ...] = ("coverage_id",),
    *,
    relationships: dict | None = None,
) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put(
        "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": NS,
                "namespace": NS,
                "name": NS,
                "warehouse": "duckdb",
                "default_db": f"data/{NS}.duckdb",
                "seed": {"kind": "external"},
                "environments": ["development"],
            },
        },
    )
    entities = {
        "policy": (["policy_id"], "policies"),
        "coverage": (list(coverage_key), "coverages"),
        "premium": (["premium_id"], "premiums"),
        "claim": (["claim_id"], "claims"),
        "claim_line": (["line_id"], "claim_lines"),
    }
    put(
        "graph.yml",
        {
            "graph": {
                "entities": {
                    name: {"key": key, "model": model, "allowed_as_root": True}
                    for name, (key, model) in entities.items()
                },
                "relationships": relationships or {},
            }
        },
    )
    for model_id, model in models.items():
        put(f"models/{model_id}.yml", {"model": {"id": model_id, **model}})
    metric = {"value_type": "currency", "description": "Test metric."}
    put(
        "metrics/metrics.yml",
        {
            "metrics": {
                "coverage_premium": {"kind": "aggregate", "measure": "coverage_premium", **metric},
                "cumulative_coverage_premium": {
                    "kind": "cumulative",
                    "measure": "coverage_premium",
                    "temporal_role": OPENED_ROLE,
                    **metric,
                },
            }
        },
    )
    return root


_TABLES = {
    "policy": ("policy_id, region", "('P1', 'north'), ('P2', 'south')"),
    "coverage": (
        "coverage_id, policy_id, line_of_business",
        "('C1', 'P1', 'auto'), ('C2', 'P1', 'home'), ('C3', 'P2', 'auto'), "
        "('C4', 'P2', 'home'), ('C5', 'P2', 'auto'), ('C6', 'P2', 'home')",
    ),
    # C1: 3 + 4; C2: an equal 7; C3: 10 written + 2 adjusted; C4: no rows; C5: an actual 0;
    # C6: only an adjustment, so its written premium is 0. No row was ever waived.
    "premium": (
        "premium_id, coverage_id, premium_kind, booked_on, amount, waived",
        "(1, 'C1', 'written', DATE '2026-01-05', 3, NULL::INTEGER), "
        "(2, 'C1', 'written', DATE '2026-02-10', 4, NULL), "
        "(3, 'C2', 'written', DATE '2026-01-20', 7, NULL), "
        "(4, 'C3', 'written', DATE '2026-01-25', 10, NULL), "
        "(5, 'C3', 'adjustment', DATE '2026-03-01', 2, NULL), "
        "(6, 'C5', 'written', DATE '2026-01-30', 0, NULL), "
        "(7, 'C6', 'adjustment', DATE '2026-02-01', 5, NULL)",
    ),
    "claim": (
        "claim_id, coverage_id, opened_on, reserve",
        "('K1', 'C1', DATE '2026-03-01', 14), ('K2', 'C1', DATE '2026-04-15', 21), "
        "('K3', 'C2', DATE '2026-03-10', 7), ('K4', NULL, DATE '2026-03-12', 5), "
        "('K5', 'C4', DATE '2026-05-01', 9), ('K6', 'C5', DATE '2026-05-02', 3), "
        "('K7', 'C6', DATE '2026-05-03', 4), ('K8', 'C3', DATE '2026-06-01', 6)",
    ),
    "claim_line": (
        "line_id, claim_id, line_type, line_amount",
        "(1, 'K1', 'medical', 100), (2, 'K1', 'legal', 50), (3, 'K2', 'medical', 30)",
    ),
}


def _seed(root: Path, renamed: dict[str, str] | None = None) -> None:
    (root / "data").mkdir()
    connection = duckdb.connect(str(root / "data" / f"{NS}.duckdb"))
    for table, (columns, rows) in _TABLES.items():
        table = (renamed or {}).get(table, table)
        connection.execute(f"create table {table} as select * from (values {rows}) t({columns})")
    connection.close()


@pytest.fixture(scope="module")
def package_dir(tmp_path_factory) -> Path:
    root = _write(tmp_path_factory.mktemp("lookup") / NS, _models())
    _seed(root)
    return root


@pytest.fixture(scope="module")
def runtime(package_dir: Path) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(package_dir))
    try:
        yield opened(rt)
    finally:
        rt.close()


def _reference(package_dir: Path, sql: str) -> dict[Any, Any]:
    connection = duckdb.connect(str(package_dir / "data" / f"{NS}.duckdb"), read_only=True)
    try:
        return dict(connection.execute(sql).fetchall())
    finally:
        connection.close()


# The coverage total on each claim, written on the base tables: 0 for a coverage with no rows
# (premiums exist elsewhere), NULL for a claim with no coverage.
_PER_CLAIM = """
with per_coverage as (
    select coverage_id, sum({value}) as total from premium group by coverage_id
)
select claim.claim_id,
       case when claim.coverage_id is not null then coalesce(per_coverage.total, 0) end
from claim left join per_coverage on per_coverage.coverage_id = claim.coverage_id
"""


def _query(
    measure: str = "coverage_premium",
    group_by: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    expression: dict[str, Any] = {"measure": _measure(measure)}
    if "aggregation" in extra:
        expression["aggregation"] = extra.pop("aggregation")
    return {
        "version": 1,
        "select": [{"expression": expression, "as": "v"}],
        "group_by": [CLAIM_KEY] if group_by is None else group_by,
        **extra,
    }


def _rows(runtime: Runtime, payload: dict[str, Any], key: str = CLAIM_KEY) -> dict[Any, Any]:
    result = runtime.query(payload)
    assert result["ok"], result.get("errors")
    return {row[key]: row["v"] for row in result["rows"]}


def _refusal(runtime: Runtime, payload: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(payload)
    return raised.value


def _warnings(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in result.get("warnings") or [] if isinstance(row, dict)]


# 1. Two premium rows (3 and 4) on one coverage with two claims: 7 on each claim, never 14.
def test_each_claim_carries_its_coverage_total(runtime: Runtime, package_dir: Path) -> None:
    expected = _reference(package_dir, _PER_CLAIM.format(value="amount"))
    assert _rows(runtime, _query()) == expected
    assert expected["K1"] == expected["K2"] == 7
    by_coverage = {
        "version": 1,
        "select": [
            {"expression": {"measure": CARRIED}, "as": "v"},
            {"expression": {"measure": _measure("claim_count")}, "as": "claims"},
        ],
        "group_by": [COVERAGE_KEY],
    }
    result = runtime.query(by_coverage)
    rows = {row[COVERAGE_KEY]: (row["v"], row["claims"]) for row in result["rows"]}
    assert rows["C1"] == (7, 2)
    assert rows[None] == (None, 1)


def test_a_claim_level_ratio_reads_the_carried_total(runtime: Runtime) -> None:
    ratio = {
        "kind": "ratio",
        "numerator": {"measure": _measure("reserve")},
        "denominator": {"measure": CARRIED},
    }
    payload = {"version": 1, "select": [{"expression": ratio, "as": "v"}], "group_by": [CLAIM_KEY]}
    rows = _rows(runtime, payload)
    assert (rows["K1"], rows["K2"], rows["K8"]) == (2.0, 3.0, 0.5)


def test_the_rewrite_step_names_the_source_and_route(runtime: Runtime) -> None:
    plan = plan_query(runtime.config, None, _query())
    [step] = plan.rewrite_steps
    assert (step.kind, step.measure_id) == ("parent_lookup", CARRIED)
    assert step.details == {
        "from": _measure("premium_amount"),
        "via": f"entity.{NS}_coverage",
        "path": ["relationship.claims_coverage"],
        "source_path": ["relationship.premiums_coverage"],
    }
    assert "over all time" in step.reason
    assert [leaf.rewrite_strategy for leaf in plan.measure_plans] == ["parent_lookup"]
    [warning] = [w for w in _warnings(runtime.query(_query())) if w["code"] == "REWRITE_APPLIED"]
    assert warning["details"]["rewrite_kind"] == "parent_lookup"


# 8. Differential: the lookup equals a hand-written view join of claims to coverage totals.
def test_the_lookup_equals_a_hand_written_view_join(runtime: Runtime, package_dir: Path) -> None:
    view = """
    with coverage_premium as (
        select coverage_id, sum(amount) as premium from premium group by coverage_id
    )
    select claim.coverage_id,
           max(case when claim.coverage_id is not null
                    then coalesce(coverage_premium.premium, 0) end)
    from claim left join coverage_premium using (coverage_id)
    group by claim.coverage_id
    """
    engine = _rows(runtime, _query(group_by=[COVERAGE_KEY]), key=COVERAGE_KEY)
    assert engine == _reference(package_dir, view)
    assert engine[None] is None and engine["C4"] == engine["C5"] == 0


@pytest.mark.parametrize("model,table", [("claims", "claim"), ("premiums", "premium")])
@pytest.mark.parametrize(
    "relation",
    [
        "lookup_source",
        "leaf_1_lookup_source",
        "leaf_1_lookup_source_gate",
        "leaf_1__leaf_1_lookup_source",
        "leaf_1_lookup_source__leaf_1",
        "leaf_1__leaf_1_lookup_source__leaf_1",
    ],
)
def test_a_physical_relation_can_be_named_lookup_source(
    tmp_path: Path, model: str, table: str, relation: str
) -> None:
    models = _models()
    models[model]["relation"] = relation
    root = _write(tmp_path / NS, models)
    _seed(root, {table: relation})
    reference = _PER_CLAIM.format(value="amount").replace(f"from {table}", f"from {relation}")
    expected = _reference(root, reference.replace(f"{table}.", f"{relation}."))
    engine = Runtime.from_path(str(root))
    try:
        assert len(expected) == 8
        assert _rows(engine, _query()) == expected
        assert (
            f"FROM {relation}" in compile_query(engine.config, None, _query())["sql"].splitlines()
        )
    finally:
        engine.close()


# 2. Coarser than the coverage: refused, whatever the totals are.
def test_a_grain_coarser_than_the_via_key_is_refused(runtime: Runtime) -> None:
    for payload in (_query(group_by=[POLICY_KEY]), _query(group_by=[])):
        error = _refusal(runtime, payload)
        assert error.code == "ROLLUP_UNSAFE" and isinstance(error, NonAdditiveRefusal)
        assert error.details["construct"] == "parent_lookup"
        assert f"entity.{NS}_coverage" in error.details["recovery_hints"][0]["message"]
        assert error.columns == ["coverage_id"] and error.dimensions == [COVERAGE_KEY]
    # Pinning one coverage, or one claim, holds one total per output row.
    pinned = _query(group_by=[POLICY_KEY], where=[{"field": COVERAGE_KEY, "value": "C1"}])
    assert _rows(runtime, pinned, key=POLICY_KEY) == {"P1": 7}


@pytest.mark.parametrize("aggregation", ["max", "avg", "min", "count"])
def test_statistics_cannot_dodge_the_guard(runtime: Runtime, aggregation: str) -> None:
    error = _refusal(runtime, _query(group_by=[POLICY_KEY], aggregation=aggregation))
    assert error.code == "UNSUPPORTED_AGGREGATION"


# 3. A claim's own child rows would repeat the total.
@pytest.mark.parametrize(
    "payload",
    [
        _query(group_by=[CLAIM_KEY, LINE_TYPE]),
        _query(where=[{"field": LINE_TYPE, "value": "medical"}]),
    ],
    ids=["group", "filter"],
)
def test_a_child_dimension_of_the_claim_is_refused(
    runtime: Runtime, payload: dict[str, Any]
) -> None:
    error = _refusal(runtime, payload)
    assert error.code == "MIXED_GRAIN_INVALID"


# 4. NULL versus 0, and the claim count is never changed.
def test_null_and_zero(runtime: Runtime, package_dir: Path) -> None:
    rows = _rows(runtime, _query())
    assert rows["K4"] is None  # no coverage
    assert rows["K5"] == 0  # a coverage with no premium rows, premiums observed elsewhere
    assert rows["K6"] == 0  # an actual zero premium
    written = _rows(runtime, _query("written_coverage_premium"))
    assert written == _reference(
        package_dir, _PER_CLAIM.format(value="case when premium_kind = 'written' then amount end")
    )
    assert (written["K7"], written["K8"]) == (0, 10)  # the condition holds on no row, on one
    average = _rows(runtime, _query("average_coverage_premium"))
    assert average["K1"] == 3.5 and average["K5"] is None  # an average of nothing is NULL
    counts = _rows(runtime, _query("claim_count"))
    assert set(counts.values()) == {1} and len(counts) == len(rows)


def test_a_lookup_preserves_an_all_unknown_source_group(tmp_path: Path) -> None:
    root = _write(tmp_path / NS, _models())
    _seed(root)
    with duckdb.connect(str(root / "data" / f"{NS}.duckdb")) as connection:
        connection.execute("update premium set amount = NULL where coverage_id = 'C1'")
    reference = """
    with per_coverage as (
        select coverage_id, sum(amount) as total, count(*) as source_rows
        from premium group by coverage_id
    )
    select claim.coverage_id,
           max(case
               when claim.coverage_id is null then null
               when per_coverage.source_rows is null
                    and exists (select 1 from premium where amount is not null) then 0
               else per_coverage.total
           end)
    from claim left join per_coverage using (coverage_id)
    group by claim.coverage_id
    """
    engine = Runtime.from_path(str(root))
    try:
        rows = _rows(engine, _query(group_by=[COVERAGE_KEY]), key=COVERAGE_KEY)
        assert rows == _reference(root, reference)
        assert rows["C1"] is None  # Two source rows with unknown amounts, two child rows.
        assert rows["C2"] == 7
        assert rows["C4"] == rows["C5"] == 0  # No source rows, and an actual zero.
        assert rows[None] is None
    finally:
        engine.close()


def test_empty_groups_are_settled_in_one_place(runtime: Runtime) -> None:
    from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place

    beside = {"expression": {"measure": _measure("claim_count")}, "as": "claims"}
    for payload in (_query(), {**_query(), "select": [*_query()["select"], beside]}):
        compiled = compile_query(runtime.config, None, payload)
        assert_settled_in_one_place(compiled, runtime.config)


def test_an_unobserved_source_reads_null_with_one_warning(runtime: Runtime) -> None:
    result = runtime.query(_query("waived_coverage_amount"))
    assert {row["v"] for row in result["rows"]} == {None}
    [warning] = [row for row in _warnings(result) if row.get("code") == "NO_DATA_IN_SCOPE"]
    assert warning["details"]["outputs"] == ["v"]
    assert not [w for w in _warnings(runtime.query(_query())) if w["code"] == "NO_DATA_IN_SCOPE"]


def test_a_row_filter_on_the_source_is_denied_never_unfiltered(package_dir: Path) -> None:
    config = load_package_config(str(package_dir))
    policy = SemanticPolicyConfig(
        id="policy.test.own_premiums",
        kind="row_filter",
        config={"dimension": PREMIUM_KIND, "attribute": "premium_kind"},
        audiences=["customer"],
    )
    engine = Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=str(package_dir)
    )
    context = {"audience": "customer", "attributes": {"premium_kind": "written"}}
    error = _refusal(engine, {**_query(), "policy_context": context})
    assert error.code == "POLICY_DENIED"


def test_access_to_the_source_measure_is_required(package_dir: Path) -> None:
    config = load_package_config(str(package_dir))
    policy = SemanticPolicyConfig(
        id="policy.test.no_premiums",
        kind="object_access",
        object_ids=[_measure("premium_amount")],
        audiences=["external"],
        action="deny",
    )
    engine = Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=str(package_dir)
    )
    error = _refusal(engine, {**_query(), "policy_context": {"audience": "external"}})
    assert error.code == "POLICY_DENIED"
    assert engine.query({**_query("reserve"), "policy_context": {"audience": "external"}})["ok"]


@pytest.mark.parametrize("action", ["deny"])
@pytest.mark.parametrize("group_by", [[CLAIM_KEY], [COVERAGE_KEY]], ids=["child", "via"])
def test_access_to_the_lookup_relationship_is_required(
    package_dir: Path, monkeypatch, action: str, group_by: list[str]
) -> None:
    from semantic_rails import compiler

    relationship = "relationship.claims_coverage"
    config = load_package_config(str(package_dir))
    query = _query(group_by=group_by, policy_context={"audience": "external"})
    assert relationship in bind_query(config, None, query).object_ids
    policy = SemanticPolicyConfig(
        id="policy.test.no_coverage_link",
        kind="object_access",
        object_ids=[relationship],
        audiences=["external"],
        action=action,
    )
    engine = Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=str(package_dir)
    )

    def no_output(*args, **kwargs):
        pytest.fail("rendering or adapter access before relationship authorization")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as raised:
                operation(query)
            assert raised.value.code == "POLICY_DENIED"
    finally:
        engine.close()


# 5. The query's filters and time select claims; the carried total never changes.
def test_child_filters_select_claims_not_the_total(runtime: Runtime) -> None:
    every = _rows(runtime, _query())
    late = {"field": OPENED, "op": ">=", "value": "2026-04-01"}
    filtered = _rows(runtime, _query(where=[late]))
    assert set(filtered) == {"K2", "K5", "K6", "K7", "K8"}
    assert filtered == {claim: every[claim] for claim in filtered}
    timed = _rows(runtime, _query(time={"temporal_role": OPENED_ROLE, "start": "2026-04-01"}))
    assert timed == filtered
    monthly = runtime.query(
        {
            **_query(time={"temporal_role": OPENED_ROLE, "grain": "month"}),
            "select": [
                {"expression": {"measure": CARRIED}, "as": "v"},
                {"expression": {"measure": _measure("claim_count")}, "as": "claims"},
            ],
        }
    )
    assert {row[CLAIM_KEY]: (row["v"], row["claims"]) for row in monthly["rows"]} == {
        claim: (value, 1) for claim, value in every.items()
    }
    auto = {"field": f"dimension.{NS}_coverage_line_of_business", "value": "auto"}
    assert _rows(runtime, _query(where=[auto])) == {"K1": 7, "K2": 7, "K6": 0, "K8": 12}
    # A metric predicate selects claims too: those of coverages with two or more claims.
    busy = {
        "kind": "metric_predicate",
        "entity": f"entity.{NS}_coverage",
        "input": {"measure": _measure("claim_count")},
        "op": ">=",
        "value": 2,
        "scope_mode": "entity_only",
    }
    predicate = {"expression": busy, "op": "=", "value": True}
    assert _rows(runtime, _query(metric_filters=[predicate])) == {"K1": 7, "K2": 7}


def test_a_filter_on_the_source_is_refused(runtime: Runtime) -> None:
    written = {"field": PREMIUM_KIND, "value": "written"}
    assert _refusal(runtime, _query(where=[written])).code == "MIXED_GRAIN_INVALID"


# 6. Time on the lookup itself: refused, never silently retimed.
@pytest.mark.parametrize(
    "payload",
    [
        # Beside a premium measure the source's clock binds, and the lookup would be retimed.
        {
            "version": 1,
            "select": [
                {"expression": {"measure": _measure("premium_amount")}, "as": "premium"},
                {"expression": {"measure": CARRIED}, "as": "v"},
            ],
            "group_by": [COVERAGE_KEY],
            "time": {"temporal_role": BOOKED_ROLE, "grain": "month"},
        },
        {
            **_query(),
            "select": [
                {
                    "expression": {
                        "kind": "prior_period",
                        "input": {"measure": CARRIED},
                        "offset": {"unit": "month", "value": 1},
                    },
                    "as": "v",
                }
            ],
            "time": {"temporal_role": OPENED_ROLE, "grain": "month"},
        },
        {
            **_query(),
            "select": [
                {"expression": {"metric": f"metric.{NS}.cumulative_coverage_premium"}, "as": "v"}
            ],
            "time": {"temporal_role": OPENED_ROLE, "grain": "month"},
        },
        _query(
            time={"temporal_role": OPENED_ROLE, "grain": "month"},
            temporal_role_overrides={CARRIED: OPENED_ROLE},
        ),
    ],
    ids=["source-clock", "prior-period", "cumulative", "override"],
)
def test_time_on_the_lookup_itself_is_refused(runtime: Runtime, payload: dict[str, Any]) -> None:
    error = _refusal(runtime, payload)
    assert error.code == "REWRITE_NOT_SUPPORTED"
    assert error.details["unsupported_construct"] == "lookup_time"


@pytest.mark.parametrize(
    "expression",
    [
        {
            "kind": "scoped_aggregate",
            "measure": _measure("reserve"),
            "aggregation": "sum",
            "predicates": [
                {
                    "kind": "metric_predicate",
                    "entity": f"entity.{NS}_coverage",
                    "input": {"measure": CARRIED},
                    "op": ">",
                    "value": 5,
                }
            ],
        },
        {
            "kind": "distribution",
            "function": "avg",
            "over": {
                "kind": "entity_value",
                "entity": f"entity.{NS}_coverage",
                "input": {"measure": CARRIED},
            },
        },
    ],
    ids=["metric-predicate", "distribution"],
)
def test_per_entity_rollups_over_a_lookup_are_refused(
    runtime: Runtime, expression: dict[str, Any]
) -> None:
    error = _refusal(runtime, {**_query(), "select": [{"expression": expression, "as": "v"}]})
    assert error.code == "ROLLUP_UNSAFE" and error.details["construct"] != "parent_lookup"


def test_the_source_clock_alone_binds_no_measure(runtime: Runtime) -> None:
    alone = _query(time={"temporal_role": BOOKED_ROLE, "grain": "month"})
    assert _refusal(runtime, alone).code == "INCOMPATIBLE_TEMPORAL_ROLE"


def _bad_package(tmp_path: Path, change, **keys: Any) -> SemanticLayerError:
    models = copy.deepcopy(_models())
    relationships: dict = {}
    change(models, relationships)
    root = _write(tmp_path / NS, models, relationships=relationships, **keys)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(root))
    assert raised.value.code == "INVALID_CONFIG"
    return raised.value


def _lookup(**spec: Any):
    def change(models: dict[str, Any], relationships: dict) -> None:
        models["claims"]["measures"]["bad"] = {"kind": "lookup", "via": "coverage", **spec}

    return change


def _second_coverage_key(models: dict[str, Any], relationships: dict) -> None:
    relationships["prior_claim_coverage"] = {
        "entities": ["claim", "coverage"],
        "cardinality": "many_to_one",
        "via": ["prior_coverage_id"],
    }


def _second_source_coverage_key(models: dict[str, Any], relationships: dict) -> None:
    relationships["prior_premium_coverage"] = {
        "entities": ["premium", "coverage"],
        "cardinality": "many_to_one",
        "via": ["prior_coverage_id"],
    }


def _timed_coverage_key(models: dict[str, Any], relationships: dict) -> None:
    relationships["claims_coverage"] = {
        "entities": ["claim", "coverage"],
        "cardinality": "many_to_one",
        "temporal_validity": {
            "valid_from": "coverage.valid_from",
            "valid_to": "coverage.valid_to",
        },
    }


def _composite_coverage_key(models: dict[str, Any], relationships: dict) -> None:
    for model, entity in (("claims", "claim"), ("premiums", "premium")):
        models[model]["entities"] = {entity: {}, "coverage": {"expr": ["coverage_id", "term"]}}


# 6. Each load refusal names the key at fault.
@pytest.mark.parametrize(
    ("change", "key"),
    [
        (_lookup(), "from"),
        (_lookup(**{"from": "premium_amount", "via": ""}), "via"),
        (_lookup(**{"from": "no_such_measure"}), "from"),
        (_lookup(**{"from": "premium_rows"}), "from"),  # an entity count
        (_lookup(**{"from": "premium_rate"}), "from"),  # additive: false
        (_lookup(**{"from": "premium_balance"}), "from"),  # a stock
        (_lookup(**{"from": "coverage_premium"}), "from"),  # another lookup
        (_lookup(**{"from": "premium_amount", "via": "claim_line"}), "via"),  # one-to-many
        (_lookup(**{"from": "premium_amount", "via": "policy"}), "via"),  # not direct
        (_lookup(**{"from": "line_amount"}), "via"),  # the source can't reach coverage
        (_lookup(**{"from": "premium_amount", "default_agg": "max"}), "default_agg"),
        (_lookup(**{"from": "premium_amount", "additive": True}), "additive"),
        (_lookup(**{"from": "premium_amount", "accumulation": "stock"}), "accumulation"),
        (_lookup(**{"from": "premium_amount", "value_type": "number"}), "value_type"),
        (_lookup(**{"from": "premium_amount", "expr": "coverage_id"}), "expr"),
        (_second_coverage_key, "via"),
        (_second_source_coverage_key, "via"),
        (_timed_coverage_key, "via"),
    ],
)
def test_bad_declarations_are_refused_at_load(tmp_path: Path, change, key: str) -> None:
    assert _bad_package(tmp_path, change).details["key"] == key


def test_from_and_via_belong_to_lookups_only(tmp_path: Path) -> None:
    def change(models: dict[str, Any], relationships: dict) -> None:
        models["claims"]["measures"]["reserve"]["via"] = "coverage"

    assert _bad_package(tmp_path, change).details["key"] == "via"


def test_a_fully_covered_composite_via_key_is_refused(tmp_path: Path) -> None:
    error = _bad_package(tmp_path, _composite_coverage_key, coverage_key=("coverage_id", "term"))
    assert error.details["key"] == "via"
    assert "composite" in str(error) and "term" in str(error)


def test_a_time_via_is_refused_at_load(tmp_path: Path) -> None:
    root = _write(tmp_path / NS, _models())
    graph_path = root / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["entities"]["coverage"]["kind"] = "time"
    graph_path.write_text(yaml.safe_dump(graph))
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(root))
    assert raised.value.code == "INVALID_CONFIG" and raised.value.details["key"] == "via"
    assert "non-time" in str(raised.value)


def _routed_package(root: Path, start: str, direct: bool = False) -> Path:
    models = _models()
    models["assignments"] = {
        "relation": "assignment",
        "entities": {"assignment": {}, "coverage": {}},
    }
    models[start]["entities"]["assignment"] = {}
    _write(root, models)
    graph_path = root / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text())
    graph["graph"]["entities"]["assignment"] = {"key": ["assignment_id"], "model": "assignments"}
    graph["graph"]["path_preferences"] = [
        {
            "source_entity": "claim" if start == "claims" else "premium",
            "target_entity": "coverage",
            "relationship_path": [f"relationship.{start}_coverage"]
            if direct
            else [f"relationship.{start}_assignment", "relationship.assignments_coverage"],
        }
    ]
    graph_path.write_text(yaml.safe_dump(graph))
    return root


@pytest.mark.parametrize("start", ["claims", "premiums"], ids=["child", "source"])
def test_a_recorded_route_conflicting_with_the_lookup_is_refused(
    tmp_path: Path, start: str
) -> None:
    root = _routed_package(tmp_path / NS, start)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(root))
    assert raised.value.code == "INVALID_CONFIG" and raised.value.details["key"] == "via"
    assert "graph.path_preferences" in str(raised.value)
    assert f"relationship.{start}_assignment" in str(raised.value)
    assert "relationship.assignments_coverage" in str(raised.value)


@pytest.mark.parametrize("start", ["claims", "premiums"], ids=["child", "source"])
def test_a_recorded_direct_route_is_allowed(tmp_path: Path, start: str) -> None:
    root = _routed_package(tmp_path / NS, start, direct=True)
    config = load_package_config(str(root))
    assert compile_query(config, None, _query())["sql"]


@pytest.mark.parametrize("start", ["claims", "premiums"], ids=["child", "source"])
@pytest.mark.parametrize("group_by", [[CLAIM_KEY], [COVERAGE_KEY]], ids=["child-key", "via-key"])
def test_a_route_conflict_bypassing_load_is_refused_by_the_guard(
    tmp_path: Path, monkeypatch, start: str, group_by: list[str]
) -> None:
    from semantic_rails import config as config_module

    # Bypass only the loader's recorded-route check, preserving all other lookup checks.
    resolve = config_module.resolve_lookup_measures

    def skip_recorded_routes(*args, **kwargs):
        if "path_preferences" in kwargs:
            kwargs["path_preferences"] = []
        return resolve(*args, **kwargs)

    monkeypatch.setattr(config_module, "resolve_lookup_measures", skip_recorded_routes)
    root = _routed_package(tmp_path / NS, start)
    _seed(root)
    connection = duckdb.connect(str(root / "data" / f"{NS}.duckdb"))
    try:
        connection.execute("create table assignment as select 'A1' assignment_id, 'C2' coverage_id")
        for table in ("claim", "premium"):
            connection.execute(f"alter table {table} add column assignment_id varchar")
            connection.execute(f"update {table} set assignment_id = 'A1' where coverage_id = 'C1'")
        connection.execute("update premium set amount = 20 where coverage_id = 'C2'")
    finally:
        connection.close()
    if start == "claims":
        assert _reference(root, _PER_CLAIM.format(value="amount"))["K1"] == 7
        routed_reference = """
            with totals as (select coverage_id, sum(amount) total from premium group by coverage_id)
            select claim_id, total from claim
            join assignment using (assignment_id)
            join totals on totals.coverage_id = assignment.coverage_id
            where claim_id = 'K1'
        """
        # The route's parent differs from the direct foreign key's parent.
        assert _reference(root, routed_reference) == {"K1": 20}
    engine = Runtime.from_path(str(root))
    try:
        error = _refusal(engine, _query(group_by=group_by))
        assert error.code == "ROLLUP_UNSAFE" and error.details["construct"] == "parent_lookup"
    finally:
        engine.close()


def test_the_loader_derives_everything_but_from_and_via(runtime: Runtime) -> None:
    measure = next(row for row in runtime.config.measures if row.id == CARRIED)
    assert (measure.lookup_from, measure.lookup_via) == (
        _measure("premium_amount"),
        f"entity.{NS}_coverage",
    )
    assert (measure.value_type, measure.currency) == ("currency", "USD")
    assert measure.additive is False and measure.allowed_aggregations == ["sum"]
    assert measure.accumulation.kind == "flow" and measure.default_aggregation == "sum"


# 7. Bypass: a plan that reaches the leaf unchecked, or a folded scan, still refuses.
def test_a_plan_that_skipped_the_guard_is_refused_at_its_leaf(runtime: Runtime) -> None:
    from semantic_rails.compiler import lower_to_sql

    plan = plan_query(runtime.config, None, _query(group_by=[COVERAGE_KEY]))
    coarse = replace(plan, query={**plan.query, "group_by": [POLICY_KEY]}, group_by=[POLICY_KEY])
    with pytest.raises(NonAdditiveRefusal):
        lower_to_sql(coarse, runtime.config)


def test_a_lookup_read_outside_its_leaf_is_refused(runtime: Runtime, monkeypatch) -> None:
    from semantic_rails.compiler_parts import sql_lowering

    monkeypatch.setattr(sql_lowering, "_foldable_leaf_signature", lambda *args: ("folded",))
    payload = {
        **_query(),
        "select": [
            {"expression": {"measure": _measure("reserve")}, "as": "reserve"},
            {"expression": {"measure": CARRIED}, "as": "v"},
        ],
    }
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(runtime.config, None, payload)
    assert raised.value.details["unsupported_construct"] == "parent_lookup"


def test_a_rollup_never_answers_a_lookup(runtime: Runtime) -> None:
    from semantic_rails.schema import AggregateRelationConfig

    rollup = AggregateRelationConfig(
        id="aggregate_relation.test.claims_by_coverage",
        relation="claim_rollup",
        source_entity=f"entity.{NS}_claim",
        measures=[CARRIED],
        dimensions=[COVERAGE_KEY],
        measure_columns={CARRIED: "premium"},
    )
    config = replace(runtime.config, aggregate_relations=[rollup])
    [leaf] = plan_query(config, None, _query(group_by=[COVERAGE_KEY])).measure_plans
    assert (leaf.aggregate_relation_id, leaf.rewrite_strategy) == ("", "parent_lookup")
    assert leaf.aggregate_relation_rejections == {rollup.id: "parent_lookup"}


def test_validation_probes_the_lookup_at_its_via_key(package_dir: Path) -> None:
    from semantic_rails.config_validation import PackageReference, validate_config_report

    report = validate_config_report(PackageReference(source_path=str(package_dir)))
    failed = sorted(row["details"]["object_id"] for row in report["errors"])
    assert failed == [f"metric.{NS}.cumulative_coverage_premium"]


def test_interchange_export_keeps_lookups_out(runtime: Runtime) -> None:
    from semantic_rails.interop.ossie.export import _Exporter

    exporter = _Exporter(runtime.config)
    exporter.model()
    assert CARRIED in exporter.lost["lookup measures"]
