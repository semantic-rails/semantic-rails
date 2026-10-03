"""``additive: false`` measures are never summed.

A vendor's pre-counted distinct values (daily unique visitors, a page's unique
visitors over 14 days) can't be added up: three pages with 3, 2 and 2 unique
visitors had 4 distinct visitors between them, and the engine returned 7. Such a
measure is answered only where each output row holds one of its rows (for a stock,
one series); anything else is refused, and avg/min/max stay available.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.compiler import NonAdditiveRefusal
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

NS = "f4add"
DAY = f"temporal_role.{NS}_traffic_day_day"
SNAP = f"temporal_role.{NS}_repo_snapshot_snapshot_date"
REPO_DAY = f"dimension.{NS}_traffic_day_repo"
REPO_SNAP = f"dimension.{NS}_repo_snapshot_repo"
PATH = f"dimension.{NS}_path_snapshot_path"
PATH_REPO = f"dimension.{NS}_path_snapshot_repo"
REPO_KEY = f"dimension.{NS}_repository_repo"
OWNER = f"dimension.{NS}_repository_owner"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(scope="module")
def package_dir(tmp_path_factory) -> Path:
    package = tmp_path_factory.mktemp("non_additive") / NS
    stock = "accumulation: {kind: stock, snapshot: end_of_period}"
    _write(
        package / "package.yml",
        f"schema_version: 1\npackage: {{id: {NS}, namespace: {NS}, name: {NS}, "
        f"warehouse: duckdb, default_db: data/{NS}.duckdb, seed: {{kind: external}}, "
        "schema_strict: true, environments: [development]}\n",
    )
    _write(
        package / "graph.yml",
        "graph:\n  entities:\n"
        "    repository: {key: [repo], model: repositories, allowed_as_root: true}\n"
        "    traffic_day: {key: [repo, day], model: traffic_days, allowed_as_root: true}\n"
        "    repo_snapshot: {key: [repo, snapshot_date], model: repo_snapshots, "
        "allowed_as_root: true}\n"
        "    path_snapshot: {key: [repo, path, snapshot_date], model: path_snapshots, "
        "allowed_as_root: true}\n",
    )
    clock = "kind: date, default: true"
    _write(
        package / "models" / "repositories.yml",
        "model:\n  id: repositories\n  relation: repository\n  entities: {repository: {}}\n"
        "  dimensions: {owner: {kind: categorical}}\n",
    )
    _write(
        package / "models" / "traffic_days.yml",
        "model:\n  id: traffic_days\n  relation: traffic_daily\n"
        "  entities: {traffic_day: {}, repository: {}}\n"
        f"  times: {{day: {{column: day, class: event_time, {clock}}}}}\n"
        "  measures:\n"
        "    daily_visitors: {kind: aggregate, expr: daily_visitors, additive: false, "
        "value_type: count}\n"
        "    views: {kind: aggregate, expr: views, value_type: count}\n",
    )
    _write(
        package / "models" / "repo_snapshots.yml",
        "model:\n  id: repo_snapshots\n  relation: repo_snapshot\n"
        "  entities: {repo_snapshot: {}, repository: {}}\n"
        f"  times: {{snapshot_date: {{column: snapshot_date, class: as_of_time, {clock}}}}}\n"
        "  measures:\n"
        f"    visitors_14d: {{kind: aggregate, expr: visitors_14d, additive: false, {stock}, "
        "value_type: count}\n"
        f"    stars: {{kind: aggregate, expr: stars, {stock}, value_type: count}}\n",
    )
    _write(
        package / "models" / "path_snapshots.yml",
        "model:\n  id: path_snapshots\n  relation: path_snapshot\n"
        "  entities: {path_snapshot: {}, repository: {}}\n"
        "  dimensions: {path: {kind: categorical}}\n"
        f"  times: {{snapshot_date: {{column: snapshot_date, class: as_of_time, {clock}}}}}\n"
        "  measures:\n"
        f"    path_visitors_14d: {{kind: aggregate, expr: path_visitors_14d, additive: false, "
        f"{stock}, value_type: count}}\n",
    )
    metric = "value_type: count, description: Test metric."
    _write(
        package / "metrics" / "metrics.yml",
        "metrics:\n"
        f"  daily_visitors: {{kind: aggregate, measure: daily_visitors, temporal_role: {DAY}, "
        f"{metric}}}\n"
        f"  views: {{kind: aggregate, measure: views, temporal_role: {DAY}, {metric}}}\n"
        f"  cumulative_visitors: {{kind: cumulative, measure: daily_visitors, "
        f"temporal_role: {DAY}, {metric}}}\n"
        f"  rolling_visitors: {{kind: rolling, measure: daily_visitors, aggregation: sum, "
        f"window: {{unit: day, value: 7}}, temporal_role: {DAY}, {metric}}}\n",
    )
    (package / "data").mkdir()
    connection = duckdb.connect(str(package / "data" / f"{NS}.duckdb"))
    connection.execute(
        "create table repository as select * from (values ('a', 'me'), ('b', 'me')) t(repo, owner)"
    )
    connection.execute(
        "create table traffic_daily as select * from (values "
        "('a', date '2026-09-21', 3, 10), ('a', date '2026-09-22', 2, 20), "
        "('b', date '2026-09-21', 5, 30)) t(repo, day, daily_visitors, views)"
    )
    connection.execute(
        "create table repo_snapshot as select * from (values "
        "('a', date '2026-09-21', 4, 1), ('a', date '2026-09-22', 4, 1), "
        "('b', date '2026-09-22', 6, 2)) t(repo, snapshot_date, visitors_14d, stars)"
    )
    connection.execute(
        "create table path_snapshot as select * from (values "
        "('a', '/', date '2026-09-22', 3), ('a', '/docs', date '2026-09-22', 2), "
        "('a', '/src', date '2026-09-22', 2)) t(repo, path, snapshot_date, path_visitors_14d)"
    )
    connection.close()
    return package


@pytest.fixture(scope="module")
def runtime(package_dir: Path) -> Runtime:
    return Runtime.from_path(str(package_dir))


def _query(
    measure: str,
    *,
    aggregation: str = "",
    group_by: list[str] | None = None,
    where: list[dict[str, Any]] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expression: dict[str, Any] = {"measure": f"measure.{NS}.{measure}"}
    if aggregation:
        expression["aggregation"] = aggregation
    payload: dict[str, Any] = {"version": 1, "select": [{"expression": expression, "as": "v"}]}
    if group_by:
        payload["group_by"] = group_by
    if where:
        payload["where"] = where
    if time:
        payload["time"] = time
    return payload


def _values(runtime: Runtime, payload: dict[str, Any]) -> list[Any]:
    result = runtime.query(payload)
    assert result["ok"], result.get("errors")
    return sorted((row["v"] for row in result["rows"]), key=lambda v: (v is not None, v or 0))


def _refused(runtime: Runtime, payload: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(payload)
    assert raised.value.code == "ROLLUP_UNSAFE"
    assert raised.value.details["unsupported_construct"] == "non_additive_sum"
    # Key columns remain available to in-process validation callers.
    assert isinstance(raised.value, NonAdditiveRefusal)
    return {**raised.value.details, "missing_columns": raised.value.columns}


def _eq(field: str, value: Any, op: str = "=") -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


BY_DAY = {"temporal_role": DAY, "grain": "day"}
BY_WEEK_DAY = {"temporal_role": DAY, "grain": "week"}
BY_WEEK_SNAP = {"temporal_role": SNAP, "grain": "week"}
BY_DAY_SNAP = {"temporal_role": SNAP, "grain": "day"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_query("daily_visitors", group_by=[REPO_DAY], time=BY_DAY), [2, 3, 5]),
        (_query("daily_visitors", where=[_eq(REPO_DAY, "a")], time=BY_DAY), [2, 3]),
        (_query("daily_visitors", where=[_eq(REPO_DAY, ["a"], "in")], time=BY_DAY), [2, 3]),
        (_query("daily_visitors", group_by=[REPO_KEY], time=BY_DAY), [2, 3, 5]),
        (_query("daily_visitors", where=[_eq(REPO_KEY, "a")], time=BY_DAY), [2, 3]),
        # Statistics of the stored values, not sums.
        (_query("daily_visitors", aggregation="max"), [5]),
        (_query("daily_visitors", aggregation="avg", group_by=[REPO_DAY]), [2.5, 5]),
        (_query("visitors_14d", group_by=[REPO_SNAP], time=BY_WEEK_SNAP), [4, 6]),
        (_query("visitors_14d", where=[_eq(REPO_SNAP, "a")], time=BY_WEEK_SNAP), [4]),
        (_query("visitors_14d", where=[_eq(REPO_SNAP, "a")]), [4]),
        # A stock's sum takes one snapshot per series per bucket too, then adds series.
        (
            _query("visitors_14d", aggregation="sum", group_by=[REPO_SNAP], time=BY_WEEK_SNAP),
            [4, 6],
        ),
        (_query("path_visitors_14d", group_by=[PATH], where=[_eq(PATH_REPO, "a")]), [2, 2, 3]),
        # An additive stock beside them is untouched.
        (_query("stars", time=BY_WEEK_SNAP), [3]),
    ],
)
def test_one_row_per_output_row_answers(runtime: Runtime, payload, expected) -> None:
    assert _values(runtime, payload) == expected


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        (_query("daily_visitors"), ["repo", "day"]),
        (_query("daily_visitors", group_by=[REPO_DAY], time=BY_WEEK_DAY), ["day"]),
        (_query("daily_visitors", where=[_eq(REPO_DAY, "a", "!=")], time=BY_DAY), ["repo"]),
        (_query("daily_visitors", where=[_eq(REPO_DAY, ["a", "b"], "in")], time=BY_DAY), ["repo"]),
        (_query("daily_visitors", group_by=[OWNER], time=BY_DAY), ["repo"]),
        # Two repositories' 14-day uniques, and three pages' uniques: 7 for 4 visitors.
        (_query("visitors_14d", time=BY_WEEK_SNAP), ["repo"]),
        (_query("path_visitors_14d"), ["repo", "path"]),
        # A measure's own `=` filter narrows its rows but doesn't split the output rows.
        (
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate",
                            "measure": f"measure.{NS}.daily_visitors",
                            "filter": {"all": [_eq(REPO_DAY, "a")]},
                        },
                        "as": "v",
                    }
                ],
                "time": BY_DAY,
            },
            ["repo"],
        ),
        (_query("visitors_14d", aggregation="sum", time=BY_WEEK_SNAP), ["repo"]),
    ],
)
def test_summing_rows_is_refused(runtime: Runtime, payload, missing) -> None:
    details = _refused(runtime, payload)
    assert details["missing_columns"] == missing
    assert details["recovery_hints"][0]["kind"] == "stay_at_stored_grain"


def _metric(name: str, time: dict[str, Any] = BY_DAY, **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": {"metric": f"metric.{NS}.{name}"}, "as": "v"}],
        "group_by": [REPO_DAY],
        "time": time,
        **extra,
    }


def _inline(expression: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": expression, "as": "v"}],
        "group_by": [REPO_DAY],
        "time": BY_DAY,
    }


DAILY = {"measure": f"measure.{NS}.daily_visitors"}


@pytest.mark.parametrize(
    "payload",
    [
        _metric("cumulative_visitors"),
        _metric("rolling_visitors"),
        _inline(
            {
                "kind": "scoped_aggregate",
                "measure": DAILY["measure"],
                "where": [_eq(REPO_DAY, "a")],
            }
        ),
        _inline(
            {
                "kind": "scoped_aggregate",
                "measure": f"measure.{NS}.views",
                "aggregation": "sum",
                "predicates": [
                    {
                        "kind": "metric_predicate",
                        "entity": f"entity.{NS}_repository",
                        "input": {**DAILY, "aggregation": "sum"},
                        "op": ">",
                        "value": 1,
                    }
                ],
            }
        ),
    ],
    ids=["cumulative", "rolling", "scoped-filter", "metric-predicate"],
)
def test_windows_and_per_entity_rollups_are_refused(runtime: Runtime, payload) -> None:
    assert _refused(runtime, payload)["missing_columns"] == []


def test_per_period_comparisons_and_ratios_follow_the_row_rule(runtime: Runtime) -> None:
    prior = {"kind": "prior_period", "input": DAILY, "offset": {"unit": "day", "value": 1}}
    assert _values(runtime, _inline(prior)) == [None, None, 3, 5]
    ratio = {"kind": "ratio", "numerator": {"metric": f"metric.{NS}.views"}, "denominator": DAILY}
    assert len(_values(runtime, _inline(ratio))) == 3
    _refused(runtime, {**_inline(ratio), "time": BY_WEEK_DAY})


@pytest.mark.parametrize(
    "spec",
    [
        "{kind: entity_count, entity_key: repo, additive: false, value_type: count}",
        "{kind: aggregate, expr: views, additive: 'no', value_type: count}",
    ],
)
def test_additive_must_be_a_boolean_on_an_aggregate(tmp_path: Path, spec: str) -> None:
    from semantic_rails.config import load_package_config

    package = tmp_path / "bad"
    _write(
        package / "package.yml",
        "schema_version: 1\npackage: {id: bad, namespace: bad, name: bad, warehouse: duckdb, "
        "seed: {kind: external}, schema_strict: true, environments: [development]}\n",
    )
    _write(package / "graph.yml", "graph:\n  entities:\n    thing: {key: [repo], model: things}\n")
    _write(
        package / "models" / "things.yml",
        f"model:\n  id: things\n  relation: things\n  entities: {{thing: {{}}}}\n"
        f"  measures:\n    m: {spec}\n",
    )
    with pytest.raises(SemanticLayerError, match="additive must be true or false"):
        load_package_config(str(package))


def test_validation_probes_each_measure_at_its_stored_grain(package_dir: Path) -> None:
    from semantic_rails.config_validation import PackageReference, validate_config_report

    report = validate_config_report(PackageReference(source_path=str(package_dir)))
    failed = sorted(row["details"]["object_id"] for row in report["errors"])
    # Only the metrics that add the measure up across days can never be answered.
    assert failed == [f"metric.{NS}.cumulative_visitors", f"metric.{NS}.rolling_visitors"]


def test_a_role_playing_key_pins_nothing(runtime: Runtime) -> None:
    # With two foreign keys to one entity, which one a target-key grouping reads is a
    # choice the check can't see, so neither counts as single-valued.
    from dataclasses import replace

    from semantic_rails.compiler import compile_query

    config = runtime.config
    link = next(
        rel
        for rel in config.relationships
        if rel.source_entity.endswith("traffic_day") and rel.target_entity.endswith("repository")
    )
    second = replace(
        link, id="relationship.test.forked_from", source_column="forked", source_columns=["forked"]
    )
    config = replace(config, relationships=[*config.relationships, second])
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, _query("daily_visitors", group_by=[REPO_KEY], time=BY_DAY))
    assert isinstance(raised.value, NonAdditiveRefusal) and raised.value.columns == ["repo"]


def test_refusal_hints_reach_the_error_envelope(runtime: Runtime) -> None:
    from semantic_rails.diagnostics import recovery_hints_for_error

    details = _refused(runtime, _query("daily_visitors"))
    assert recovery_hints_for_error("ROLLUP_UNSAFE", details)[0]["kind"] == "stay_at_stored_grain"
    assert "avg / min / max / median" in details["recovery_hints"][0]["message"]


@pytest.mark.parametrize("with_aggregate_if", [False, True], ids=["plain", "with-aggregate-if"])
@pytest.mark.parametrize("mode", ["run", "validate", "sql"])
@pytest.mark.parametrize("hidden_key", [None, REPO_DAY, f"dimension.{NS}_traffic_day_day"])
def test_refusal_names_keys_only_when_all_are_visible(
    runtime: Runtime, package_dir: Path, with_aggregate_if: bool, mode: str, hidden_key: str | None
) -> None:
    from semantic_rails.mcp import SemanticLayerMCPAdapter
    from semantic_rails.schema import SemanticPolicyConfig

    policies = []
    if hidden_key:
        policies = [
            SemanticPolicyConfig(
                id="policy.test.hide_the_key",
                kind="object_visibility",
                object_ids=[hidden_key],
                audiences=["external"],
                action="hidden",
            )
        ]
    engine = Runtime.from_config(
        replace(runtime.config, semantic_policies=policies), source_path=str(package_dir)
    )
    query = {**_query("daily_visitors"), "policy_context": {"audience": "external"}}
    if with_aggregate_if:
        # The conditional rewrite must not bypass or lose context on the ordinary
        # non-additive measure beside it.
        query["select"].append(
            {
                "as": "matching_rows",
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": {
                        "kind": "comparison",
                        "op": "=",
                        "left": {
                            "kind": "column",
                            "entity": f"entity.{NS}_traffic_day",
                            "column": "repo",
                        },
                        "right": {"kind": "literal", "value": "a"},
                    },
                },
            }
        )
    try:
        response = SemanticLayerMCPAdapter(engine).call_tool(
            "execute", {"query": query, "mode": mode, "verbosity": "full"}
        )
        assert response["ok"] is False
        issue = response["errors"][0]
        assert issue["code"] == "ROLLUP_UNSAFE"
        names = [REPO_DAY, f"dimension.{NS}_traffic_day_day"]
        if hidden_key:
            assert issue["message"] == (
                f"Measure 'measure.{NS}.daily_visitors' is additive: false, and this query would "
                "sum more than one of its rows into an output row: group by or filter (=) "
                "each column of its key, or query a finer grain, or use aggregation avg / min / max / median."
            )
            assert issue["details"] == {
                "measure_id": f"measure.{NS}.daily_visitors",
                "unsupported_construct": "non_additive_sum",
                "construct": "sum",
                "recovery_hints": [
                    {
                        "kind": "stay_at_stored_grain",
                        "message": "Group by, or filter with = to one value, each column of the measure's "
                        "key, or use aggregation avg / min / max / median.",
                    }
                ],
            }
            assert all(name not in json.dumps(response) for name in names)
        else:
            assert issue["details"]["key_dimensions"] == names
            for name in names:
                assert name in issue["message"]
                assert name in issue["recovery_hints"][0]["message"]
    finally:
        engine.close()


def test_uncertain_key_visibility_preserves_the_generic_refusal(
    runtime: Runtime, monkeypatch
) -> None:
    from semantic_rails import policies

    def unavailable(*args, **kwargs):
        raise RuntimeError("Visibility unavailable")

    monkeypatch.setattr(policies, "hidden_object_ids", unavailable)
    with pytest.raises(NonAdditiveRefusal) as raised:
        runtime.compile(_query("daily_visitors"))
    assert "key_dimensions" not in raised.value.details
    assert REPO_DAY not in str(raised.value)
    assert REPO_DAY not in json.dumps(raised.value.details)


@pytest.mark.parametrize(
    ("context", "visible"),
    [
        ({"audience": "internal", "environment": "production", "roles": ["reader"]}, False),
        ({"audience": "external", "environment": "production", "roles": ["reader"]}, True),
        ({"audience": "internal", "environment": "development", "roles": ["reader"]}, True),
        ({"audience": "internal", "environment": "production", "role": "operator"}, True),
    ],
)
def test_key_names_follow_discovery_policy_context(runtime: Runtime, context, visible) -> None:
    from semantic_rails.schema import SemanticPolicyConfig

    policy = SemanticPolicyConfig(
        id="policy.test.key_visibility",
        kind="object_visibility",
        object_ids=[REPO_DAY],
        audiences=["internal"],
        environments=["production"],
        roles=["reader"],
        action="hidden",
    )
    config = replace(runtime.config, semantic_policies=[policy])
    engine = Runtime.from_config(config, source_path=runtime.source_path)
    try:
        with pytest.raises(NonAdditiveRefusal) as raised:
            engine.compile({**_query("daily_visitors"), "policy_context": context})
        assert (REPO_DAY in str(raised.value)) is visible
    finally:
        engine.close()


@pytest.mark.parametrize("entry_point", ["internal", "plan", "compile"])
def test_internal_planning_without_visibility_context_keeps_keys_private(
    runtime: Runtime, entry_point: str
) -> None:
    from semantic_rails.ast import normalize_query
    from semantic_rails.compiler import _plan_query, compile_query, plan_query

    # Forcing the internal planning path cannot opt into naming a key: it has
    # no request context with which to authorize that disclosure.
    with pytest.raises(NonAdditiveRefusal) as raised:
        if entry_point == "internal":
            _plan_query(
                runtime.config,
                None,
                normalize_query(_query("daily_visitors")),
                collapse_window=True,
            )
        else:
            operation = plan_query if entry_point == "plan" else compile_query
            operation(runtime.config, None, _query("daily_visitors"))
    assert "key_dimensions" not in raised.value.details
    assert REPO_DAY not in str(raised.value)
