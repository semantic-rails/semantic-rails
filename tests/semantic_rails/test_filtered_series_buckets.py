"""Authored filters preserve observed time buckets, without inventing unloaded periods."""

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_config_expression, parse_semantic_expression
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig
from tests.integration.correctness.conftest import _write_variant
from tests.semantic_rails.result_helpers import typed_rows
from tests.semantic_rails.test_observation_scope import (
    PRODUCT,
    QTY,
    REGIONAL,
    SALES,
    SOLD,
    STORE,
    _package,
)

MATCHING_WEEKS = {0, 2, 3, 5, 7, 9, 11}
START = datetime(2025, 1, 6)


@pytest.fixture(scope="module")
def series(tmp_path_factory):
    package = _package(tmp_path_factory.mktemp("filtered_series") / "obs", {})
    with duckdb.connect(str(package / "obs.duckdb")) as connection:
        connection.execute("DELETE FROM sales")
        connection.executemany(
            "INSERT INTO sales VALUES (?, 's1', 'east', ?, ?, ?)",
            [
                (
                    week + 1,
                    "apple" if week in MATCHING_WEEKS else "pear",
                    week + 1,
                    START + timedelta(weeks=week),
                )
                for week in range(12)
            ],
        )
    config = load_package_config(str(package))
    metrics = [
        MetricConfig(
            id=f"metric.obs.filtered_{name}",
            kind="derived",
            expression=parse_semantic_expression(
                {
                    "kind": "aggregate",
                    **expression,
                    "filter": {"all": [{"field": PRODUCT, "op": "=", "value": "apple"}]},
                },
                context="query",
            ),
        )
        for name, expression in [
            ("sum", QTY),
            ("count", SALES),
            ("average", {**QTY, "aggregation": "avg"}),
        ]
    ]
    runtime = Runtime.from_config(replace(config, metric_recipes=metrics), source_path=str(package))
    try:
        runtime._get_adapter()
        yield runtime
    finally:
        runtime.close()


def _query(kind, scope="dataset", **time):
    return {
        "select": [{"expression": {"metric": f"metric.obs.filtered_{kind}"}, "as": "value"}],
        "observation_scope": scope,
        "time": {"temporal_role": SOLD, "grain": "week", **time},
    }


@pytest.mark.parametrize("scope", ["dataset", "query"])
@pytest.mark.parametrize("kind", ["sum", "count"])
def test_twelve_observed_weeks_include_five_zeros(series, scope, kind):
    response = series.query(_query(kind, scope, start="2025-01-06", end="2025-03-31"))
    aggregate = (
        "SUM(CASE WHEN product = 'apple' THEN qty ELSE 0 END)"
        if kind == "sum"
        else ("COUNT(CASE WHEN product = 'apple' THEN sale_id END)")
    )
    gold = series._get_adapter().query(
        f"SELECT date_trunc('week', sold_at) AS bucket, {aggregate} AS value "
        "FROM sales GROUP BY 1 ORDER BY 1"
    )
    assert len(gold) == 12
    assert sum(row["value"] == 0 for row in gold) == 5
    assert [(row[f"{SOLD}__week"], row["value"]) for row in typed_rows(response)] == [
        (row["bucket"], row["value"]) for row in gold
    ]
    assert not any(w["code"] == "EMPTY_RESULT_WINDOW" for w in response["warnings"])


@pytest.mark.parametrize("scope", ["dataset", "query"])
@pytest.mark.parametrize("kind", ["sum", "count"])
def test_a_quiet_week_agrees_with_its_ungrouped_total(series, scope, kind):
    query = _query(kind, scope, start="2025-01-13", end="2025-01-20")
    grouped = series.query(query)
    query["time"].pop("grain")
    total = series.query(query)
    assert [row["value"] for row in grouped["rows"]] == [0]
    assert total["rows"] == [{"value": 0}]


def test_unobserved_weeks_stay_absent(series):
    response = series.query(_query("count", start="2024-12-30", end="2025-04-07"))
    assert len(response["rows"]) == 12


def test_a_filtered_average_keeps_its_seven_populated_buckets(series):
    response = series.query(_query("average"))
    assert len(response["rows"]) == 7
    assert [row["value"] for row in response["rows"]] == [
        week + 1 for week in sorted(MATCHING_WEEKS)
    ]


def test_a_filtered_ratio_keeps_its_seven_populated_buckets(series):
    config = series.config
    metric = MetricConfig(
        id="metric.obs.filtered_ratio",
        kind="derived",
        expression=parse_semantic_expression(
            {
                "kind": "ratio",
                "numerator": {"metric": "metric.obs.filtered_sum"},
                "denominator": {"metric": "metric.obs.filtered_count"},
            },
            context="query",
        ),
    )
    runtime = Runtime.from_config(
        replace(config, metric_recipes=[*config.metric_recipes, metric]),
        source_path=series.source_path,
    )
    try:
        response = runtime.query(_query("ratio"))
        assert len(response["rows"]) == 7
        assert [row["value"] for row in response["rows"]] == [
            week + 1 for week in sorted(MATCHING_WEEKS)
        ]
        assert not any(
            w["code"].startswith("FILTERED_SERIES_BUCKETS") for w in response["warnings"]
        )
    finally:
        runtime.close()


def test_folded_sum_and_count_keep_the_same_buckets(series):
    query = _query("sum")
    query["select"].append({"expression": {"metric": "metric.obs.filtered_count"}, "as": "count"})
    response = series.query(query)
    assert len(response["rows"]) == 12
    assert [(row["value"], row["count"]) for row in response["rows"]] == [
        (week + 1, 1) if week in MATCHING_WEEKS else (0, 0) for week in range(12)
    ]


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_query_filters_still_define_the_observed_population(series, scope):
    query = _query("sum", scope)
    query["where"] = [{"field": PRODUCT, "op": "=", "value": "pear"}]
    response = series.query(query)
    assert len(response["rows"]) == 5
    assert all(row["value"] == 0 for row in response["rows"])


def test_retained_buckets_respect_row_filters(series):
    east = series.query({**_query("count"), "policy_context": REGIONAL.to_policy_context()})
    assert len(east["rows"]) == 12
    hidden = RequestContext(actor="end-user", audience="regional", attributes={"region": "west"})
    west = series.query({**_query("count"), "policy_context": hidden.to_policy_context()})
    assert west["rows"] == []


def test_matching_null_amounts_remain_unknown(series, tmp_path):
    package = _package(tmp_path / "unknown", {})
    with duckdb.connect(str(package / "obs.duckdb")) as connection:
        connection.execute("UPDATE sales SET qty = NULL WHERE product = 'apple'")
    config = replace(load_package_config(str(package)), metric_recipes=series.config.metric_recipes)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        response = runtime.query(_query("sum", grain="month"))
        assert [row["value"] for row in response["rows"]] == [None, None]
        quiet = runtime.query(_query("sum", start="2025-03-02", end="2025-03-09"))
        assert [row["value"] for row in quiet["rows"]] == [0]
        empty = runtime.query(_query("sum", start="2025-02-01", end="2025-02-08"))
        assert empty["rows"] == []
    finally:
        runtime.close()


def _conditional_runtime(series):
    config = series.config
    conditional = parse_config_expression(
        {
            "kind": "case",
            "whens": [
                {
                    "when": {
                        "kind": "comparison",
                        "op": "=",
                        "left": {"kind": "column", "column": "region"},
                        "right": {"kind": "literal", "value": "east"},
                    },
                    "then": {"kind": "column", "column": "qty"},
                }
            ],
        }
    )
    config = replace(
        config,
        measures=[
            replace(row, expr=conditional) if row.id == QTY["measure"] else row
            for row in config.measures
        ],
    )
    return Runtime.from_config(config, source_path=series.source_path)


def test_unsupported_conditional_operands_name_the_five_dropped_buckets(series):
    runtime = _conditional_runtime(series)
    try:
        response = runtime.query(_query("sum"))
        assert len(response["rows"]) == 7
        warning = next(
            w for w in response["warnings"] if w["code"] == "FILTERED_SERIES_BUCKETS_DROPPED"
        )
        assert warning["details"]["dropped_buckets"] == [
            {f"{SOLD}__week": (START + timedelta(weeks=week)).isoformat()}
            for week in range(12)
            if week not in MATCHING_WEEKS
        ]
    finally:
        runtime.close()


def test_a_diagnostic_cannot_read_through_a_metric_only_grant(series):
    runtime = _conditional_runtime(series)
    try:
        context = RequestContext(
            metric_allowlist=("metric.obs.filtered_sum",),
            dimension_allowlist=(PRODUCT, SOLD),
        )
        query = _query("sum")
        query.pop("observation_scope")
        response = runtime.query({**query, "policy_context": context.to_policy_context()})
        warning = next(
            w for w in response["warnings"] if w["code"] == "FILTERED_SERIES_BUCKETS_UNVERIFIED"
        )
        assert warning["details"]["dropped_buckets"] == []
        assert warning["details"]["reason"] == "RESOURCE_ACCESS_DENIED"
    finally:
        runtime.close()


def test_forcing_a_conditional_operand_bypass_refuses(series, monkeypatch):
    monkeypatch.setattr(sql_lowering, "_filtered_series_operand", lambda value, conditions: value)
    with pytest.raises(SemanticLayerError) as exc:
        series.compile(_query("sum", start="2025-01-06", end="2025-01-07"))
    assert exc.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert exc.value.details["missing"] == "filtered_series_operand"


def test_a_routed_series_warns_about_its_missing_months(tmp_path):
    runtime = Runtime.from_path(str(_write_variant(tmp_path, "utc_authored")))
    try:
        response = runtime.query(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate",
                            "measure": "measure.shop.order_count",
                            "filter": {
                                "all": [
                                    {
                                        "field": "dimension.shop_order_store_id",
                                        "op": "=",
                                        "value": "b",
                                    }
                                ]
                            },
                        },
                        "as": "orders",
                    }
                ],
                "time": {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"},
            }
        )
        assert any(
            row["aggregate_relation_id"] for row in response["logical_plan"]["measure_plans"]
        )
        warning = next(
            w for w in response["warnings"] if w["code"] == "FILTERED_SERIES_BUCKETS_DROPPED"
        )
        assert warning["details"]["dropped_buckets"] == [
            {"temporal_role.shop_order_ordered_at__month": f"{month}-01T00:00:00"}
            for month in ["2023-12", "2024-01", "2024-05", "2024-08"]
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", ["denied", "capped"])
def test_an_incomplete_source_probe_never_claims_a_dropped_bucket(series, monkeypatch, failure):
    runtime = _conditional_runtime(series)
    original = runtime.query

    def query(payload):
        if payload["select"][0]["as"] == "source":
            if failure == "denied":
                raise SemanticLayerError("QUERY_EXECUTION_ERROR", "Source read failed")
            return {"rows": [], "truncated": True}
        return original(payload)

    monkeypatch.setattr(runtime, "query", query)
    try:
        response = runtime.query(_query("sum"))
        warning = next(
            w for w in response["warnings"] if w["code"] == "FILTERED_SERIES_BUCKETS_UNVERIFIED"
        )
        assert warning["details"]["dropped_buckets"] == []
        assert warning["details"]["reason"] == (
            "QUERY_EXECUTION_ERROR" if failure == "denied" else "source_limited"
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", ["sum", "count"])
@pytest.mark.parametrize("scope", ["dataset", "query"])
@pytest.mark.parametrize("limit", [None, 20])
@pytest.mark.parametrize("metric_only", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_retained_series_skip_bucket_probes_on_cold_and_cached_compiles(
    series, monkeypatch, kind, scope, limit, metric_only, cached
):
    config = replace(series.config, package=replace(series.config.package, observation_scope=scope))
    runtime = Runtime.from_config(config, source_path=series.source_path)
    original = runtime.query
    calls = []

    def query(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(runtime, "query", query)
    payload = {**_query(kind, scope), "verbosity": "full"}
    if limit is not None:
        payload["limit"] = limit
    if metric_only:
        payload.pop("observation_scope")
        context = RequestContext(
            metric_allowlist=(f"metric.obs.filtered_{kind}",),
            dimension_allowlist=(PRODUCT, SOLD),
        )
        payload["policy_context"] = context.to_policy_context()
    try:
        if cached:
            assert runtime.compile(payload)["compile_stats"]["cache_hit"] is False
        response = runtime.query(payload)
        assert response["compile_stats"]["cache_hit"] is cached
        assert len(response["rows"]) == 12
        # The only extra call is the shared filter-literal existence probe.
        assert len(calls) == 2
        assert calls[1]["select"] == []
        assert not any(
            w["code"].startswith("FILTERED_SERIES_") or w["code"] == "RESOURCE_ACCESS_DENIED"
            for w in response["warnings"]
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("sql_type", "sql_value", "expected"),
    [("VARCHAR[]", "[store_id]", ["s1"]), ("STRUCT(id VARCHAR)", "{'id': store_id}", {"id": "s1"})],
)
def test_unsupported_series_accept_container_valued_group_keys(
    series, tmp_path, sql_type, sql_value, expected
):
    package = _package(tmp_path / "container", {})
    with duckdb.connect(str(package / "obs.duckdb")) as connection:
        connection.execute("DELETE FROM sales")
        connection.executemany(
            "INSERT INTO sales VALUES (?, 's1', 'east', ?, ?, ?)",
            [
                (
                    week + 1,
                    "apple" if week in MATCHING_WEEKS else "pear",
                    week + 1,
                    START + timedelta(weeks=week),
                )
                for week in range(12)
            ],
        )
        connection.execute(f"ALTER TABLE sales ALTER store_id TYPE {sql_type} USING {sql_value}")
    config = replace(load_package_config(str(package)), metric_recipes=series.config.metric_recipes)
    base = Runtime.from_config(config, source_path=str(package))
    runtime = _conditional_runtime(base)
    try:
        response = runtime.query({**_query("sum", "query"), "group_by": [STORE]})
        assert len(response["rows"]) == 7
        assert all(row[STORE] == expected for row in response["rows"])
        (warning,) = [w for w in response["warnings"] if w["code"].startswith("FILTERED_SERIES_")]
        assert warning["code"] == "FILTERED_SERIES_BUCKETS_DROPPED"
        assert warning["details"]["dropped_buckets"] == [
            {STORE: expected, f"{SOLD}__week": (START + timedelta(weeks=week)).isoformat()}
            for week in range(12)
            if week not in MATCHING_WEEKS
        ]
    finally:
        runtime.close()
        base.close()


def test_a_key_normalization_failure_only_warns_after_the_answer_is_computed(series, monkeypatch):
    from semantic_rails.runtime_parts import filtered_series

    runtime = _conditional_runtime(series)

    def failed_key(*args, **kwargs):
        raise TypeError("Key cannot be normalized")

    monkeypatch.setattr(filtered_series, "json", SimpleNamespace(dumps=failed_key), raising=False)
    try:
        response = runtime.query(_query("sum", "query"))
        assert len(response["rows"]) == 7
        (warning,) = [w for w in response["warnings"] if w["code"].startswith("FILTERED_SERIES_")]
        assert warning["code"] == "FILTERED_SERIES_BUCKETS_UNVERIFIED"
        assert warning["details"] == {"dropped_buckets": [], "reason": "source_probe_failed"}
    finally:
        runtime.close()


@pytest.mark.parametrize("scope", ["dataset", "query"])
@pytest.mark.parametrize(("op", "value"), [("=", "appels"), ("IN", ["apple", "appels"])])
def test_retained_aggregate_filter_literals_keep_the_shared_misspelling_guard(
    series, scope, op, value
):
    query = _query("sum", scope)
    query["select"] = [
        {
            "as": "value",
            "expression": {
                "kind": "aggregate",
                **QTY,
                "filter": {"all": [{"field": PRODUCT, "op": op, "value": value}]},
            },
        }
    ]
    response = series.query(query)
    assert len(response["rows"]) == 12
    (warning,) = [w for w in response["warnings"] if w["code"] == "FILTER_VALUE_NOT_FOUND"]
    assert warning["details"]["filters"] == [
        {"dimension": PRODUCT, "value": "appels", "suggestion": "apple"}
    ]


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_a_failed_retained_filter_literal_probe_warns_without_losing_rows(
    series, monkeypatch, scope
):
    original = series.query

    def query(payload):
        if not payload["select"]:
            raise SemanticLayerError("QUERY_EXECUTION_ERROR", "Existence probe failed")
        return original(payload)

    monkeypatch.setattr(series, "query", query)
    response = series.query(_query("sum", scope))
    assert len(response["rows"]) == 12
    (warning,) = [w for w in response["warnings"] if w["code"] == "FILTER_VALUE_UNVERIFIED"]
    assert warning["details"]["filters"] == [{"dimension": PRODUCT, "value": "apple"}]
