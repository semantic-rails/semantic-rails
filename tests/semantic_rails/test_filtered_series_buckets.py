"""Authored filters preserve observed time buckets, without inventing unloaded periods."""

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import leaves
from semantic_rails.compiler_parts.empty_groups import LeafScope, guard_empty_groups
from semantic_rails.config import load_package_config
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_config_expression, parse_semantic_expression
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.renderer import render_expr, render_select
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.runtime_parts import filtered_series
from semantic_rails.schema import MetricConfig
from semantic_rails.sql_ast import SqlCall, SqlField, SqlIdentifier, SqlSelect, SqlTableRef
from tests.integration.correctness.conftest import _write_variant
from tests.semantic_rails.conftest import opened
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
WEEKS = [(START + timedelta(weeks=week)).date() for week in range(12)]
# Each week's apple quantity and apple sales: 0 in the five weeks that sold only a pear.
SUMS = [week + 1 if week in MATCHING_WEEKS else 0 for week in range(12)]
COUNTS = [int(week in MATCHING_WEEKS) for week in range(12)]
QUIET_WEEKS = [{"period": WEEKS[week].isoformat()} for week in range(12) if not COUNTS[week]]
TWELVE_WEEKS = {"start": "2025-01-06", "end": "2025-03-31"}


def _twelve_weeks(connection) -> None:
    """One sale a week for twelve weeks: an apple in MATCHING_WEEKS, a pear otherwise."""
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


def _metric(name: str, expression: dict) -> MetricConfig:
    expression = parse_semantic_expression(expression, context="query")
    return MetricConfig(id=f"metric.obs.filtered_{name}", kind="derived", expression=expression)


@pytest.fixture(scope="module")
def series(tmp_path_factory):
    package = _package(tmp_path_factory.mktemp("filtered_series") / "obs", {})
    with duckdb.connect(str(package / "obs.duckdb")) as connection:
        _twelve_weeks(connection)
    apple = {"all": [{"field": PRODUCT, "op": "=", "value": "apple"}]}
    metrics = [
        _metric(name, {"kind": "aggregate", **expression, "filter": apple})
        for name, expression in [
            ("sum", {"measure": QTY}),
            ("count", {"measure": SALES}),
            ("average", {"measure": QTY, "aggregation": "avg"}),
        ]
    ]
    ratio = {"kind": "ratio", "numerator": {"metric": "metric.obs.filtered_sum"}}
    ratio["denominator"] = {"metric": "metric.obs.filtered_count"}
    metrics.append(_metric("ratio", ratio))
    config = replace(load_package_config(str(package)), metric_recipes=metrics)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        yield opened(runtime)
    finally:
        runtime.close()


def _conditional_runtime(series):
    """The package with qty read only in the east: a conditional operand no retention supports."""
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
    measures = [
        replace(row, expr=conditional) if row.id == QTY else row for row in series.config.measures
    ]
    return Runtime.from_config(
        replace(series.config, measures=measures), source_path=series.source_path
    )


@pytest.fixture(scope="module")
def conditional(series):
    runtime = _conditional_runtime(series)
    try:
        yield opened(runtime)
    finally:
        runtime.close()


def _query(kind, scope="dataset", **time):
    return {
        "select": [{"expression": f"metric.obs.filtered_{kind}", "as": "value"}],
        "observation_scope": scope,
        "time": {"temporal_role": SOLD, "grain": "week", **time},
    }


def _warnings(response, prefix):
    return [w for w in response.get("warnings", []) if w["code"].startswith(prefix)]


@pytest.mark.parametrize(
    ("kinds", "scope", "time"),
    [
        (["sum"], "query", TWELVE_WEEKS),
        (["count"], "dataset", TWELVE_WEEKS),
        # Folded into one leaf, the sum and the count keep the same buckets.
        (["sum", "count"], "dataset", {}),
    ],
)
def test_twelve_observed_weeks_include_five_zeros(series, kinds, scope, time):
    select = [{"expression": f"metric.obs.filtered_{kind}", "as": kind} for kind in kinds]
    response = series.query({**_query(kinds[0], scope, **time), "select": select})
    expected = {"sum": SUMS, "count": COUNTS}
    assert [(row["period"], *(row[kind] for kind in kinds)) for row in typed_rows(response)] == [
        (WEEKS[week], *(expected[kind][week] for kind in kinds)) for week in range(12)
    ]
    assert not _warnings(response, "EMPTY_RESULT_WINDOW")


@pytest.mark.parametrize(("kind", "scope"), [("sum", "dataset"), ("count", "query")])
def test_a_quiet_week_agrees_with_its_ungrouped_total(series, scope, kind):
    query = _query(kind, scope, start="2025-01-13", end="2025-01-20")
    grouped = series.query(query)
    query["time"].pop("grain")
    total = series.query(query)
    assert [row["value"] for row in grouped["rows"]] == [0]
    assert total["rows"] == [{"value": 0}]


def test_weeks_outside_the_data_read_null(series):
    """The named window answers its 14 weeks: the twelve with sales (0 where no apple sold),
    and NULL before the first sale and after the last one."""
    response = series.query(_query("count", start="2024-12-30", end="2025-04-07"))
    assert [row["value"] for row in response["rows"]] == [None, *COUNTS, None]


@pytest.mark.parametrize("kind", ["average", "ratio"])
def test_a_filtered_average_or_ratio_keeps_its_seven_populated_buckets(series, kind):
    response = series.query(_query(kind))
    assert [row["value"] for row in response["rows"]] == [w + 1 for w in sorted(MATCHING_WEEKS)]
    assert not _warnings(response, "FILTERED_SERIES_BUCKETS")


def test_query_filters_still_define_the_observed_population(series):
    query = _query("sum", "dataset")
    query["where"] = [{"field": PRODUCT, "op": "=", "value": "pear"}]
    response = series.query(query)
    assert [row["value"] for row in response["rows"]] == [0] * 5


def test_retained_buckets_respect_row_filters(series):
    east = series.query(_query("count"), context=REGIONAL)
    assert len(east["rows"]) == 12
    hidden = RequestContext(actor="end-user", audience="regional", attributes={"region": "west"})
    west = series.query(_query("count"), context=hidden)
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
        # A named window answers each week it touches; weeks with no apple row, inside the
        # loaded data, read 0, whether or not a pear sold in them.
        quiet = runtime.query(_query("sum", start="2025-03-02", end="2025-03-09"))
        assert [row["value"] for row in quiet["rows"]] == [0, 0]
        empty = runtime.query(_query("sum", start="2025-02-01", end="2025-02-08"))
        assert [row["value"] for row in empty["rows"]] == [0, 0]
    finally:
        runtime.close()


def test_a_diagnostic_cannot_read_through_a_metric_only_grant(conditional):
    context = RequestContext(
        metric_allowlist=("metric.obs.filtered_sum",), dimension_allowlist=(PRODUCT, SOLD)
    )
    query = _query("sum")
    query.pop("observation_scope")
    response = conditional.query(query, context=context)
    (warning,) = _warnings(response, "FILTERED_SERIES_BUCKETS_UNVERIFIED")
    assert warning["details"]["dropped_buckets"] == []
    assert warning["details"]["reason"] == "RESOURCE_ACCESS_DENIED"


@pytest.mark.parametrize(
    ("failure", "scope", "reason"),
    [
        ("denied", "dataset", "QUERY_EXECUTION_ERROR"),
        ("capped", "dataset", "source_limited"),
        # A bucket key that can't be normalized only warns, after the answer is computed.
        ("unnormalizable", "query", "source_probe_failed"),
    ],
)
def test_an_incomplete_source_probe_never_claims_a_dropped_bucket(
    conditional, monkeypatch, failure, scope, reason
):
    original = conditional.query_internal

    def query(payload):
        if failure != "unnormalizable" and payload["select"][0]["as"] == "source":
            if failure == "denied":
                raise SemanticLayerError("QUERY_EXECUTION_ERROR", "Source read failed")
            return {"rows": [], "truncated": True}
        return original(payload)

    def failed_key(*args, **kwargs):
        raise TypeError("Key cannot be normalized")

    monkeypatch.setattr(conditional, "query_internal", query)
    if failure == "unnormalizable":
        monkeypatch.setattr(
            filtered_series, "json", SimpleNamespace(dumps=failed_key), raising=False
        )
    response = conditional.query(_query("sum", scope))
    assert len(response["rows"]) == 7
    (warning,) = _warnings(response, "FILTERED_SERIES_")
    assert warning["code"] == "FILTERED_SERIES_BUCKETS_UNVERIFIED"
    assert warning["details"] == {"dropped_buckets": [], "reason": reason}


DUCKDB = dialect_for_warehouse("duckdb")
LOADED = LeafScope(
    SqlTableRef("t"),
    (),
    (),
    SqlCall("SUM", [SqlIdentifier(["t", "v"])]),
    bucket=SqlIdentifier(["t", "b"]),
    raw_time=SqlIdentifier(["t", "at"]),
)


def _guard_sql(aggregation, retained, scopes, time_key):
    ctes = guard_empty_groups(
        "base",
        ["t"],
        ["v"],
        {"v": aggregation},
        scopes,
        rows={"v": "v_rows"} if aggregation == "sum" else {},
        time_key=time_key,
        dialect=DUCKDB,
        observed_buckets={"v"} if retained else (),
    )
    return render_select(
        SqlSelect(
            select=[SqlField(SqlIdentifier(["guarded_base", "v"]), "v")],
            from_table=SqlTableRef("guarded_base"),
            ctes=ctes,
        )
    )


@pytest.mark.parametrize(
    ("scopes", "time_key", "aggregation"),
    [
        (None, "", "sum"),
        ({"v": LOADED}, "", "count"),
        ({"v": replace(LOADED, bucket=None)}, "t", "sum"),
    ],
    ids=["no_scope", "no_coverage", "no_bucket"],
)
def test_an_ungated_retained_scope_emits_no_marker(aggregation, scopes, time_key):
    # Without the loaded-bucket check, a retained leaf settles exactly as an unretained one.
    ungated = _guard_sql(aggregation, True, scopes, time_key)
    assert ungated == _guard_sql(aggregation, False, scopes, time_key)
    assert ">= 0" not in ungated
    gated = _guard_sql(aggregation, True, {"v": LOADED}, "t")
    assert ">= 0" in gated and "coverage_1" in gated


@pytest.mark.parametrize(
    ("warehouse", "kind", "scope", "time"),
    [
        ("duckdb", "sum", "dataset", {}),
        ("duckdb", "count", "dataset", TWELVE_WEEKS),
        ("snowflake", "sum", "query", {}),
        ("bigquery", "sum", "query", {}),
    ],
)
def test_only_a_warehouse_with_loaded_coverage_retains_a_series(
    series, warehouse, kind, scope, time
):
    config = replace(series.config, package=replace(series.config.package, warehouse=warehouse))
    query = _query(kind, scope, **time)
    compiled = compile_query(config, Registry(config), read_call(query, config=config)[0])
    if warehouse == "duckdb":  # retained: reads its loaded coverage, without fill
        assert compiled["retained_filtered_series"] and "coverage_1" in compiled["sql"]
        return
    assert compiled["retained_filtered_series"] == [] and "coverage_" not in compiled["sql"]
    # The authored filter stays in the leaf's WHERE, as before retention existed.
    leaf = next(cte.query for cte in compiled["sql_ast"].ctes if cte.name == "leaf_1")
    assert "sales.product = 'apple'" in [render_expr(clause) for clause in leaf.where]


def test_forcing_a_conditional_operand_bypass_refuses(series, monkeypatch):
    monkeypatch.setattr(leaves, "_filtered_series_operand", lambda value, conditions: value)
    with pytest.raises(SemanticLayerError) as exc:
        series.compile(_query("sum", start="2025-01-06", end="2025-01-07"))
    assert exc.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert exc.value.details["missing"] == "filtered_series_operand"


def test_a_routed_series_warns_about_its_missing_months(tmp_path):
    store_b = {"all": [{"field": "dimension.shop_order_store_id", "op": "is", "value": "b"}]}
    orders = {"kind": "aggregate", "measure": "measure.shop.order_count", "filter": store_b}
    runtime = Runtime.from_path(str(_write_variant(tmp_path, "utc_authored")))
    try:
        response = runtime.query(
            {
                "select": [{"expression": orders, "as": "orders"}],
                "time": {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"},
            }
        )
    finally:
        runtime.close()
    assert any(row["aggregate_relation_id"] for row in response["logical_plan"]["measure_plans"])
    (warning,) = _warnings(response, "FILTERED_SERIES_BUCKETS_DROPPED")
    assert warning["details"]["dropped_buckets"] == [
        {"period": f"{month}-01"} for month in ["2023-12", "2024-01", "2024-05", "2024-08"]
    ]


@pytest.mark.parametrize(
    ("kind", "scope", "limit", "metric_only", "cached"),
    # Every option appears; a metric-only grant meets both a cold and a cached compile.
    [
        ("sum", "dataset", None, False, False),
        ("count", "query", 20, False, True),
        ("sum", "query", 20, True, False),
        ("count", "dataset", None, True, True),
    ],
)
def test_retained_series_skip_bucket_probes_on_cold_and_cached_compiles(
    series, monkeypatch, kind, scope, limit, metric_only, cached
):
    config = replace(series.config, package=replace(series.config.package, observation_scope=scope))
    runtime = Runtime.from_config(config, source_path=series.source_path)
    original = runtime.query_internal
    calls = []

    def query(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(runtime, "query_internal", query)
    call = _query(kind, scope)
    # One request instant: a cached plan serves the same now only.
    options: dict = {"verbosity": "full", "now": "2025-06-01"}
    if limit is not None:
        call["limit"] = limit
    if metric_only:
        call.pop("observation_scope")
        options["context"] = RequestContext(
            metric_allowlist=(f"metric.obs.filtered_{kind}",),
            dimension_allowlist=(PRODUCT, SOLD),
        )
    try:
        if cached:
            assert runtime.compile(call, **options)["compile_stats"]["cache_hit"] is False
        response = runtime.query(call, **options)
        assert response["compile_stats"]["cache_hit"] is cached
        assert len(response["rows"]) == 12
        # The only extra call is the shared filter-literal existence probe.
        assert len(calls) == 2
        assert calls[1]["select"] == []
        assert not _warnings(response, "FILTERED_SERIES_")
        assert not _warnings(response, "RESOURCE_ACCESS_DENIED")
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
        _twelve_weeks(connection)
        connection.execute(f"ALTER TABLE sales ALTER store_id TYPE {sql_type} USING {sql_value}")
    config = replace(load_package_config(str(package)), metric_recipes=series.config.metric_recipes)
    base = Runtime.from_config(config, source_path=str(package))
    runtime = _conditional_runtime(base)
    try:
        response = runtime.query({**_query("sum", "query"), "group_by": [STORE]})
        assert len(response["rows"]) == 7
        assert all(row[STORE] == expected for row in response["rows"])
        (warning,) = _warnings(response, "FILTERED_SERIES_")
        assert warning["code"] == "FILTERED_SERIES_BUCKETS_DROPPED"
        assert warning["details"]["dropped_buckets"] == [
            {STORE: expected, **q} for q in QUIET_WEEKS
        ]
    finally:
        runtime.close()
        base.close()


@pytest.mark.parametrize(
    ("op", "value", "scope"), [("=", "appels", "dataset"), ("IN", ["apple", "appels"], "query")]
)
def test_retained_aggregate_filter_literals_keep_the_shared_misspelling_guard(
    series, scope, op, value
):
    query = _query("sum", scope)
    filter_ = {"all": [{"field": PRODUCT, "op": op, "value": value}]}
    query["select"] = [
        {"as": "value", "expression": {"kind": "aggregate", "measure": QTY, "filter": filter_}}
    ]
    response = series.query(query)
    assert len(response["rows"]) == 12
    (warning,) = _warnings(response, "FILTER_VALUE_NOT_FOUND")
    assert warning["details"]["filters"] == [
        {"dimension": PRODUCT, "value": "appels", "suggestion": "apple"}
    ]


def test_a_failed_retained_filter_literal_probe_warns_without_losing_rows(series, monkeypatch):
    original = series.query_internal

    def query(payload):
        if not payload["select"]:
            raise SemanticLayerError("QUERY_EXECUTION_ERROR", "Existence probe failed")
        return original(payload)

    monkeypatch.setattr(series, "query_internal", query)
    response = series.query(_query("sum", "dataset"))
    assert len(response["rows"]) == 12
    (warning,) = _warnings(response, "FILTER_VALUE_UNVERIFIED")
    assert warning["details"]["filters"] == [{"dimension": PRODUCT, "value": "apple"}]
