"""``kind: lookup`` measures carry a parent's measure total onto each of its child rows.

A coverage's premium (the sum of its premium rows) sits on each claim made against it, and
the engine never adds that total across two coverages or repeats it over a claim's own child
rows. Every number here is checked against SQL written on the base tables, not the engine.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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
COVERAGE = f"entity.{NS}_coverage"
MONTHLY = {"temporal_role": OPENED_ROLE, "grain": "month"}


def _measure(key: str) -> str:
    return f"measure.{NS}.{key}"


def _is(field: str, value: Any) -> dict[str, Any]:
    return {"field": field, "op": "is", "value": value}


_PACKAGE = """
schema_version: 1
package: {id: lkp, namespace: lkp, name: lkp, warehouse: duckdb, default_db: data/lkp.duckdb,
  seed: {kind: external}, environments: [development]}
"""
_METRICS = """
metrics:
  coverage_premium: {kind: aggregate, measure: coverage_premium, value_type: currency,
    description: Test metric.}
  cumulative_coverage_premium: {kind: cumulative, measure: coverage_premium,
    temporal_role: temporal_role.lkp_claim_opened_on, value_type: currency,
    description: Test metric.}
"""
_MODELS = """
policies: {relation: policy, entities: {policy: {}}, dimensions: {region: {}}}
coverages:
  relation: coverage
  entities: {coverage: {}, policy: {}}
  dimensions: {line_of_business: {}}
premiums:
  relation: premium
  entities: {premium: {}, coverage: {}}
  dimensions: {premium_kind: {}}
  times: {booked_on: {column: booked_on, kind: date, class: event_time, default: true}}
  measures:
    premium_amount: {kind: aggregate, expr: amount, value_type: currency, currency: USD}
    written_premium:
      kind: aggregate
      value_type: currency
      expr:
        kind: case
        whens:
          - when: {kind: comparison, op: "=", left: {kind: column, column: premium_kind},
              right: {kind: literal, value: written}}
            then: {kind: column, column: amount}
        else: {kind: literal, value: null}
    average_premium: {kind: aggregate, expr: amount, default_agg: avg}
    waived_amount: {kind: aggregate, expr: waived}
    premium_rows: {kind: entity_count, entity_key: premium_id}
    premium_rate: {kind: aggregate, expr: amount, additive: false}
    premium_balance: {kind: aggregate, expr: amount, accumulation: {kind: stock}}
claims:
  relation: claim
  entities: {claim: {}, coverage: {}}
  times: {opened_on: {column: opened_on, kind: date, class: event_time, default: true}}
  measures:
    claim_count: {kind: entity_count, entity_key: claim_id}
    reserve: {kind: aggregate, expr: reserve}
    coverage_premium: {kind: lookup, via: coverage, from: premium_amount}
    written_coverage_premium: {kind: lookup, via: coverage, from: written_premium}
    average_coverage_premium: {kind: lookup, via: coverage, from: average_premium}
    waived_coverage_amount: {kind: lookup, via: coverage, from: waived_amount}
claim_lines:
  relation: claim_line
  entities: {claim_line: {}, claim: {}}
  dimensions: {line_type: {}}
  measures: {line_amount: {kind: aggregate, expr: line_amount}}
"""
_ENTITIES = {
    "policy": (["policy_id"], "policies"),
    "coverage": (["coverage_id"], "coverages"),
    "premium": (["premium_id"], "premiums"),
    "claim": (["claim_id"], "claims"),
    "claim_line": (["line_id"], "claim_lines"),
}


def _models() -> dict[str, dict[str, Any]]:
    """The package's models; tests change a copy to author a bad declaration."""
    return yaml.safe_load(_MODELS)


def _write(root: Path, models: dict[str, Any] | None = None, **graph: Any) -> Path:
    """Write the package; ``graph`` adds graph.yml keys, and its ``entities`` merge by name."""
    entities = {
        name: {"key": key, "model": model, "allowed_as_root": True}
        for name, (key, model) in _ENTITIES.items()
    }
    for name, changes in graph.pop("entities", {}).items():
        entities[name] = {**entities.get(name, {}), **changes}
    files = {
        "package.yml": _PACKAGE,
        "metrics/metrics.yml": _METRICS,
        "graph.yml": yaml.safe_dump(
            {"graph": {"entities": entities, "relationships": {}, **graph}}
        ),
        **{
            f"models/{name}.yml": yaml.safe_dump({"model": {"id": name, **model}}, sort_keys=False)
            for name, model in (models or _models()).items()
        },
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
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


def _seed(root: Path, *changes: str, renamed: dict[str, str] | None = None) -> Path:
    """Create the tables (``renamed`` maps a table to another name), then run ``changes``."""
    (root / "data").mkdir()
    with duckdb.connect(str(root / "data" / f"{NS}.duckdb")) as connection:
        for table, (columns, rows) in _TABLES.items():
            table = (renamed or {}).get(table, table)
            connection.execute(
                f"create table {table} as select * from (values {rows}) t({columns})"
            )
        for change in changes:
            connection.execute(change)
    return root


@pytest.fixture(scope="module")
def package_dir(tmp_path_factory) -> Path:
    return _seed(_write(tmp_path_factory.mktemp("lookup") / NS))


@pytest.fixture(scope="module")
def runtime(package_dir: Path) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(package_dir))
    yield opened(rt)
    rt.close()


@pytest.fixture
def engine() -> Iterator[Callable[..., Runtime]]:
    """Runtimes on a test's own package or config, closed at teardown."""
    engines: list[Runtime] = []

    def build(root: Path, config: Any = None) -> Runtime:
        path = str(root)
        engines.append(
            Runtime.from_config(config, source_path=path) if config else Runtime.from_path(path)
        )
        return engines[-1]

    yield build
    for built in engines:
        built.close()


def _policy(package_dir: Path, **policy: Any) -> Any:
    """The package's config under one policy."""
    config = load_package_config(str(package_dir))
    return replace(config, semantic_policies=[SemanticPolicyConfig(**policy)])


def _reference(package_dir: Path, sql: str) -> dict[Any, Any]:
    with duckdb.connect(str(package_dir / "data" / f"{NS}.duckdb"), read_only=True) as connection:
        return dict(connection.execute(sql).fetchall())


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
_WITH_CLAIMS = [
    {"expression": CARRIED, "as": "v"},
    {"expression": _measure("claim_count"), "as": "claims"},
]


def _query(
    measure: str = "coverage_premium", group_by: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    expression: dict[str, Any] = {"kind": "measure", "measure": _measure(measure)}
    if "aggregation" in extra:
        expression["aggregation"] = extra.pop("aggregation")
    return {
        "version": 1,
        "select": [{"expression": expression, "as": "v"}],
        "group_by": [CLAIM_KEY] if group_by is None else group_by,
        **extra,
    }


def _rows(runtime: Runtime, payload: dict[str, Any], key: str = CLAIM_KEY) -> dict[Any, Any]:
    """{key: value}, or {key: (value, claims)} with the claim count beside it."""
    result = runtime.query(payload)
    assert result["ok"], result.get("errors")
    if payload["select"] == _WITH_CLAIMS:
        return {row[key]: (row["v"], row["claims"]) for row in result["rows"]}
    return {row[key]: row["v"] for row in result["rows"]}


def _refusal(runtime: Runtime, payload: dict[str, Any], **options: Any) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(payload, **options)
    return raised.value


def _warnings(result: dict[str, Any], code: str) -> list[dict[str, Any]]:
    warnings = result.get("warnings") or []
    return [row for row in warnings if isinstance(row, dict) and row.get("code") == code]


def test_each_claim_carries_its_coverage_total(runtime: Runtime, package_dir: Path) -> None:
    """Two premium rows (3 and 4) on one coverage with two claims: 7 on each claim, never 14.
    NULL without a coverage; 0 for a coverage with no premium rows (premiums are observed
    elsewhere) and for an actual zero premium. The claim count is never changed."""
    rows = _rows(runtime, _query())
    assert rows == _reference(package_dir, _PER_CLAIM.format(value="amount"))
    assert (rows["K1"], rows["K2"], rows["K4"], rows["K5"], rows["K6"]) == (7, 7, None, 0, 0)
    counts = _rows(runtime, _query("claim_count"))
    assert set(counts.values()) == {1} and len(counts) == len(rows)
    written = _rows(runtime, _query("written_coverage_premium"))
    assert written == _reference(
        package_dir, _PER_CLAIM.format(value="case when premium_kind = 'written' then amount end")
    )
    assert (written["K7"], written["K8"]) == (0, 10)  # the condition holds on no row, on one
    average = _rows(runtime, _query("average_coverage_premium"))
    assert average["K1"] == 3.5 and average["K5"] is None  # an average of nothing is NULL


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
    engine = _rows(runtime, _query(group_by=[COVERAGE_KEY], select=_WITH_CLAIMS), COVERAGE_KEY)
    values = {coverage: value for coverage, (value, _) in engine.items()}
    assert values == _reference(package_dir, view)
    assert engine["C1"] == (7, 2) and engine[None] == (None, 1)
    assert engine["C4"][0] == engine["C5"][0] == 0


def test_a_claim_level_ratio_reads_the_carried_total(runtime: Runtime) -> None:
    ratio = {"kind": "ratio", "numerator": _measure("reserve"), "denominator": CARRIED}
    rows = _rows(runtime, {"select": [{"expression": ratio, "as": "v"}], "group_by": [CLAIM_KEY]})
    assert (rows["K1"], rows["K2"], rows["K8"]) == (2.0, 3.0, 0.5)


def test_the_rewrite_step_names_the_source_and_route(runtime: Runtime) -> None:
    plan = plan_query(runtime.config, None, _query())
    [step] = plan.rewrite_steps
    assert (step.kind, step.measure_id) == ("parent_lookup", CARRIED)
    assert step.details == {
        "from": _measure("premium_amount"),
        "via": COVERAGE,
        "path": ["relationship.claims_coverage"],
        "source_path": ["relationship.premiums_coverage"],
    }
    assert "over all time" in step.reason
    assert [leaf.rewrite_strategy for leaf in plan.measure_plans] == ["parent_lookup"]
    [warning] = _warnings(runtime.query(_query()), "REWRITE_APPLIED")
    assert warning["details"]["rewrite_kind"] == "parent_lookup"


# Each name the lookup's own relations take, on the child's or on the source's table.
@pytest.mark.parametrize(
    ("model", "table", "relation"),
    [
        ("claims", "claim", "lookup_source"),
        ("premiums", "premium", "leaf_1_lookup_source"),
        ("claims", "claim", "leaf_1_lookup_source_gate"),
        ("premiums", "premium", "leaf_1__leaf_1_lookup_source"),
        ("claims", "claim", "leaf_1_lookup_source__leaf_1"),
        ("premiums", "premium", "leaf_1__leaf_1_lookup_source__leaf_1"),
    ],
)
def test_a_physical_relation_can_be_named_lookup_source(
    tmp_path: Path, engine, model: str, table: str, relation: str
) -> None:
    models = _models()
    models[model]["relation"] = relation
    root = _seed(_write(tmp_path / NS, models), renamed={table: relation})
    reference = _PER_CLAIM.format(value="amount").replace(f"from {table}", f"from {relation}")
    expected = _reference(root, reference.replace(f"{table}.", f"{relation}."))
    assert len(expected) == 8
    named = engine(root)
    assert _rows(named, _query()) == expected
    assert f"FROM {relation}" in compile_query(named.config, None, _query())["sql"].splitlines()


# Coarser than the coverage: refused, whatever the totals are.
def test_a_grain_coarser_than_the_via_key_is_refused(runtime: Runtime) -> None:
    for payload in (_query(group_by=[POLICY_KEY]), _query(group_by=[])):
        error = _refusal(runtime, payload)
        assert error.code == "ROLLUP_UNSAFE" and isinstance(error, NonAdditiveRefusal)
        assert error.details["construct"] == "parent_lookup"
        assert COVERAGE in error.details["recovery_hints"][0]["message"]
        assert error.columns == ["coverage_id"] and error.dimensions == [COVERAGE_KEY]
    # Pinning one coverage, or one claim, holds one total per output row.
    pinned = _query(group_by=[POLICY_KEY], where=[_is(COVERAGE_KEY, "C1")])
    assert _rows(runtime, pinned, key=POLICY_KEY) == {"P1": 7}


_MISPLACING = {
    # Statistics cannot dodge the coarse-grain guard.
    **{
        agg: (_query(group_by=[POLICY_KEY], aggregation=agg), "UNSUPPORTED_AGGREGATION")
        for agg in ("max", "avg", "min", "count")
    },
    # A claim's own child rows would repeat the total.
    "child-group": (_query(group_by=[CLAIM_KEY, LINE_TYPE]), "MIXED_GRAIN_INVALID"),
    "child-filter": (_query(where=[_is(LINE_TYPE, "medical")]), "MIXED_GRAIN_INVALID"),
    "source-filter": (_query(where=[_is(PREMIUM_KIND, "written")]), "MIXED_GRAIN_INVALID"),
    # The source's clock alone binds no measure.
    "source-clock": (
        _query(time={**MONTHLY, "temporal_role": BOOKED_ROLE}),
        "INCOMPATIBLE_TEMPORAL_ROLE",
    ),
}


@pytest.mark.parametrize(("payload", "code"), _MISPLACING.values(), ids=list(_MISPLACING))
def test_shapes_that_would_misplace_the_total_are_refused(
    runtime: Runtime, payload: dict[str, Any], code: str
) -> None:
    assert _refusal(runtime, payload).code == code


def test_a_lookup_preserves_an_all_unknown_source_group(tmp_path: Path, engine) -> None:
    root = _seed(_write(tmp_path / NS), "update premium set amount = NULL where coverage_id = 'C1'")
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
    rows = _rows(engine(root), _query(group_by=[COVERAGE_KEY]), key=COVERAGE_KEY)
    assert rows == _reference(root, reference)
    assert rows["C1"] is None  # Two source rows with unknown amounts, two child rows.
    assert rows["C2"] == 7
    assert rows["C4"] == rows["C5"] == 0  # No source rows, and an actual zero.
    assert rows[None] is None


def test_empty_groups_are_settled_in_one_place(runtime: Runtime) -> None:
    from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place

    beside = {"expression": {"measure": _measure("claim_count")}, "as": "claims"}
    for payload in (_query(), {**_query(), "select": [*_query()["select"], beside]}):
        compiled = compile_query(runtime.config, None, payload)
        assert_settled_in_one_place(compiled, runtime.config)


def test_an_unobserved_source_reads_null_with_one_warning(runtime: Runtime) -> None:
    result = runtime.query(_query("waived_coverage_amount"))
    assert {row["v"] for row in result["rows"]} == {None}
    [warning] = _warnings(result, "NO_DATA_IN_SCOPE")
    assert warning["details"]["outputs"] == ["v"]
    assert not _warnings(runtime.query(_query()), "NO_DATA_IN_SCOPE")


def test_a_row_filter_on_the_source_is_denied_never_unfiltered(package_dir: Path, engine) -> None:
    config = _policy(
        package_dir,
        id="policy.test.own_premiums",
        kind="row_filter",
        config={"dimension": PREMIUM_KIND, "attribute": "premium_kind"},
        audiences=["customer"],
    )
    context = {"audience": "customer", "attributes": {"premium_kind": "written"}}
    assert _refusal(engine(package_dir, config), _query(), context=context).code == "POLICY_DENIED"


@pytest.mark.parametrize(
    ("denied", "group_by"),
    [
        (_measure("premium_amount"), [CLAIM_KEY]),
        ("relationship.claims_coverage", [CLAIM_KEY]),
        ("relationship.claims_coverage", [COVERAGE_KEY]),
    ],
    ids=["source", "relationship-child", "relationship-via"],
)
def test_access_to_the_source_and_its_relationship_is_required(
    runtime: Runtime, package_dir: Path, engine, monkeypatch, denied: str, group_by: list[str]
) -> None:
    from semantic_rails import compiler

    query, context = _query(group_by=group_by), {"audience": "external"}
    bound = bind_query(runtime.config, None, {**query, "policy_context": context})
    assert denied in bound.object_ids
    config = _policy(
        package_dir,
        id="policy.test.no_premiums",
        kind="object_access",
        object_ids=[denied],
        audiences=["external"],
        action="deny",
    )
    denying = engine(package_dir, config)
    assert denying.query(_query("reserve"), context=context)["ok"]

    def no_output(*args, **kwargs):
        pytest.fail("rendering or adapter access before authorization")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(denying, "_get_adapter", no_output)
    assert denying.validate(query, context=context)["errors"][0]["code"] == "POLICY_DENIED"
    for operation in (denying.compile, denying.query):
        with pytest.raises(SemanticLayerError) as raised:
            operation(query, context=context)
        assert raised.value.code == "POLICY_DENIED"


# The query's filters and time select claims; the carried total never changes.
def test_child_filters_select_claims_not_the_total(runtime: Runtime) -> None:
    every = _rows(runtime, _query())
    late = {"field": OPENED, "op": ">=", "value": "2026-04-01"}
    filtered = _rows(runtime, _query(where=[late]))
    assert set(filtered) == {"K2", "K5", "K6", "K7", "K8"}
    assert filtered == {claim: every[claim] for claim in filtered}
    timed = _rows(runtime, _query(time={"temporal_role": OPENED_ROLE, "start": "2026-04-01"}))
    assert timed == filtered
    monthly = _rows(runtime, _query(time=MONTHLY, select=_WITH_CLAIMS))
    assert monthly == {claim: (value, 1) for claim, value in every.items()}
    auto = _is(f"dimension.{NS}_coverage_line_of_business", "auto")
    assert _rows(runtime, _query(where=[auto])) == {"K1": 7, "K2": 7, "K6": 0, "K8": 12}
    # An entity filter selects claims too: those of coverages with two or more claims.
    busy = {
        "entity": COVERAGE,
        "having": [{"field": _measure("claim_count"), "op": "gte", "value": 2}],
        "over": "all_time",
    }
    assert _rows(runtime, _query(where=[busy])) == {"K1": 7, "K2": 7}


_PRIOR_MONTH = {"kind": "prior_period", "input": CARRIED, "offset": {"unit": "month", "value": 1}}


# Time on the lookup itself: refused, never silently retimed.
@pytest.mark.parametrize(
    "payload",
    [
        # Beside a premium measure the source's clock binds, and the lookup would be retimed.
        {
            "select": [
                {"expression": _measure("premium_amount"), "as": "premium"},
                {"expression": CARRIED, "as": "v"},
            ],
            "group_by": [COVERAGE_KEY],
            "time": {**MONTHLY, "temporal_role": BOOKED_ROLE},
        },
        _query(time=MONTHLY, select=[{"expression": _PRIOR_MONTH, "as": "v"}]),
        _query(
            time=MONTHLY,
            select=[{"expression": f"metric.{NS}.cumulative_coverage_premium", "as": "v"}],
        ),
        _query(time=MONTHLY, temporal_role_overrides={CARRIED: OPENED_ROLE}),
    ],
    ids=["source-clock", "prior-period", "cumulative", "override"],
)
def test_time_on_the_lookup_itself_is_refused(runtime: Runtime, payload: dict[str, Any]) -> None:
    # A clock override has no call spelling; the engine's own form still reads one.
    ask = runtime.query_internal if "temporal_role_overrides" in payload else runtime.query
    with pytest.raises(SemanticLayerError) as raised:
        ask(payload)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details["unsupported_construct"] == "lookup_time"


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
                    "entity": COVERAGE,
                    "input": CARRIED,
                    "op": "gt",
                    "value": 5,
                    "over": "all_time",
                }
            ],
        },
        {
            "kind": "distribution",
            "function": "avg",
            "over": {"kind": "entity_value", "entity": COVERAGE, "input": CARRIED},
        },
    ],
    ids=["metric-predicate", "distribution"],
)
def test_per_entity_rollups_over_a_lookup_are_refused(
    runtime: Runtime, expression: dict[str, Any]
) -> None:
    error = _refusal(runtime, {**_query(), "select": [{"expression": expression, "as": "v"}]})
    assert error.code == "ROLLUP_UNSAFE" and error.details["construct"] != "parent_lookup"


def _bad_package(tmp_path: Path, **package: Any) -> SemanticLayerError:
    root = _write(tmp_path / NS, **package)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(root))
    assert raised.value.code == "INVALID_CONFIG"
    return raised.value


def _claims_measure(name: str = "bad", **spec: Any) -> dict[str, Any]:
    """The package with one claims measure added (a lookup by default) or changed."""
    models = _models()
    measures = models["claims"]["measures"]
    measures[name] = {**measures.get(name, {"kind": "lookup", "via": "coverage"}), **spec}
    return {"models": models}


def _link(name: str, source: str, **spec: Any) -> dict[str, Any]:
    """The package with one more many-to-one relationship from ``source`` to coverage."""
    link = {"entities": [source, "coverage"], "cardinality": "many_to_one", **spec}
    return {"relationships": {name: link}}


def _routed(start: str, direct: bool = False) -> dict[str, Any]:
    """The package with a recorded route from claims or premiums to their coverage: through
    an assignment, or the direct relationship."""
    models = _models()
    models["assignments"] = {
        "relation": "assignment",
        "entities": {"assignment": {}, "coverage": {}},
    }
    models[start]["entities"]["assignment"] = {}
    path = [f"relationship.{start}_assignment", "relationship.assignments_coverage"]
    route = {
        "source_entity": start[:-1],
        "target_entity": "coverage",
        "relationship_path": [f"relationship.{start}_coverage"] if direct else path,
    }
    assignment = {"key": ["assignment_id"], "model": "assignments"}
    return {"models": models, "entities": {"assignment": assignment}, "path_preferences": [route]}


def _composite_coverage_key() -> dict[str, Any]:
    models = _models()
    for model, entity in (("claims", "claim"), ("premiums", "premium")):
        models[model]["entities"] = {entity: {}, "coverage": {"expr": ["coverage_id", "term"]}}
    return {"models": models, "entities": {"coverage": {"key": ["coverage_id", "term"]}}}


_VALIDITY = {"valid_from": "coverage.valid_from", "valid_to": "coverage.valid_to"}


# Each load refusal names the key at fault.
@pytest.mark.parametrize(
    ("package", "key"),
    [
        (_claims_measure(), "from"),
        (_claims_measure(**{"from": "premium_amount", "via": ""}), "via"),
        (_claims_measure(**{"from": "no_such_measure"}), "from"),
        (_claims_measure(**{"from": "premium_rows"}), "from"),  # an entity count
        (_claims_measure(**{"from": "premium_rate"}), "from"),  # additive: false
        (_claims_measure(**{"from": "premium_balance"}), "from"),  # a stock
        (_claims_measure(**{"from": "coverage_premium"}), "from"),  # another lookup
        (_claims_measure(**{"from": "premium_amount", "via": "claim_line"}), "via"),  # 1:N
        (_claims_measure(**{"from": "premium_amount", "via": "policy"}), "via"),  # not direct
        (_claims_measure(**{"from": "line_amount"}), "via"),  # the source can't reach coverage
        (_claims_measure(**{"from": "premium_amount", "default_agg": "max"}), "default_agg"),
        (_claims_measure(**{"from": "premium_amount", "additive": True}), "additive"),
        (_claims_measure(**{"from": "premium_amount", "accumulation": "stock"}), "accumulation"),
        (_claims_measure(**{"from": "premium_amount", "value_type": "number"}), "value_type"),
        (_claims_measure(**{"from": "premium_amount", "expr": "coverage_id"}), "expr"),
        (_claims_measure("reserve", via="coverage"), "via"),  # from and via are for lookups only
        (_link("prior_claim_coverage", "claim", via=["prior_coverage_id"]), "via"),
        (_link("prior_premium_coverage", "premium", via=["prior_coverage_id"]), "via"),
        (_link("claims_coverage", "claim", temporal_validity=_VALIDITY), "via"),
    ],
)
def test_bad_declarations_are_refused_at_load(
    tmp_path: Path, package: dict[str, Any], key: str
) -> None:
    assert _bad_package(tmp_path, **package).details["key"] == key


_ROUTE_WORDS = ["graph.path_preferences", "relationship.assignments_coverage"]


@pytest.mark.parametrize(
    ("package", "words"),
    [
        (_composite_coverage_key(), ["composite", "term"]),
        ({"entities": {"coverage": {"kind": "time"}}}, ["non-time"]),
        # A recorded route through an assignment, from claims or from premiums.
        *[
            (_routed(start), [*_ROUTE_WORDS, f"relationship.{start}_assignment"])
            for start in ("claims", "premiums")
        ],
    ],
    ids=["composite-key", "time-entity", "child-route", "source-route"],
)
def test_each_via_refusal_names_its_reason(
    tmp_path: Path, package: dict[str, Any], words: list[str]
) -> None:
    error = _bad_package(tmp_path, **package)
    assert error.details["key"] == "via"
    assert all(word in str(error) for word in words), str(error)


@pytest.mark.parametrize("start", ["claims", "premiums"], ids=["child", "source"])
def test_a_recorded_direct_route_is_allowed(tmp_path: Path, start: str) -> None:
    config = load_package_config(str(_write(tmp_path / NS, **_routed(start, direct=True))))
    assert compile_query(config, None, _query())["sql"]


@pytest.mark.parametrize("start", ["claims", "premiums"], ids=["child", "source"])
@pytest.mark.parametrize("group_by", [[CLAIM_KEY], [COVERAGE_KEY]], ids=["child-key", "via-key"])
def test_a_route_conflict_bypassing_load_is_refused_by_the_guard(
    tmp_path: Path, monkeypatch, engine, start: str, group_by: list[str]
) -> None:
    from semantic_rails.config_parts import package_rows as config_module

    # Bypass only the loader's recorded-route check, preserving all other lookup checks.
    resolve = config_module.resolve_lookup_measures
    monkeypatch.setattr(
        config_module,
        "resolve_lookup_measures",
        lambda *args, **kwargs: resolve(*args, **{**kwargs, "path_preferences": []}),
    )
    root = _seed(
        _write(tmp_path / NS, **_routed(start)),
        "create table assignment as select 'A1' assignment_id, 'C2' coverage_id",
        *[
            f"alter table {table} add column assignment_id varchar"
            for table in ("claim", "premium")
        ],
        *[
            f"update {t} set assignment_id = 'A1' where coverage_id = 'C1'"
            for t in ("claim", "premium")
        ],
        "update premium set amount = 20 where coverage_id = 'C2'",
    )
    # The route's parent differs from the direct foreign key's parent: C2 (20), not C1 (7).
    routed = """
        with totals as (select coverage_id, sum(amount) total from premium group by coverage_id)
        select claim_id, total from claim
        join assignment using (assignment_id)
        join totals on totals.coverage_id = assignment.coverage_id
        where claim_id = 'K1'
    """
    assert _reference(root, routed) == {"K1": 20}
    assert _reference(root, _PER_CLAIM.format(value="amount"))["K1"] == 7
    error = _refusal(engine(root), _query(group_by=group_by))
    assert error.code == "ROLLUP_UNSAFE" and error.details["construct"] == "parent_lookup"


def test_the_loader_derives_everything_but_from_and_via(runtime: Runtime) -> None:
    measure = next(row for row in runtime.config.measures if row.id == CARRIED)
    assert (measure.lookup_from, measure.lookup_via) == (_measure("premium_amount"), COVERAGE)
    assert (measure.value_type, measure.currency) == ("currency", "USD")
    assert measure.additive is False and measure.allowed_aggregations == ["sum"]
    assert measure.accumulation.kind == "flow" and measure.default_aggregation == "sum"


# Bypass: a plan that reaches the leaf unchecked, or a folded scan, still refuses.
def test_a_plan_that_skipped_the_guard_is_refused_at_its_leaf(runtime: Runtime) -> None:
    from semantic_rails.compiler import lower_to_sql

    plan = plan_query(runtime.config, None, _query(group_by=[COVERAGE_KEY]))
    coarse = replace(plan, query={**plan.query, "group_by": [POLICY_KEY]}, group_by=[POLICY_KEY])
    with pytest.raises(NonAdditiveRefusal):
        lower_to_sql(coarse, runtime.config)


def test_a_lookup_read_outside_its_leaf_is_refused(runtime: Runtime, monkeypatch) -> None:
    from semantic_rails.compiler_parts import lowering_common

    monkeypatch.setattr(lowering_common, "_foldable_leaf_signature", lambda *args: ("folded",))
    reserve = {"expression": {"measure": _measure("reserve")}, "as": "reserve"}
    payload = {**_query(), "select": [reserve, {"expression": {"measure": CARRIED}, "as": "v"}]}
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
