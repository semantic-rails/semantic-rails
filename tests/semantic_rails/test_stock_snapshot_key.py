"""A stock measure's key must contain its snapshot clock.

A snapshot table holds one row per series per snapshot. Keyed by a surrogate
that is unique per snapshot row (``a@2026-09-21``), "the last snapshot per key"
keeps every snapshot, and a week holding two daily snapshots of one repository
reported 8 unique visitors instead of 4 (and 2 stars instead of 1), with parse
and runtime validation green. Queries on an ``as_of_time`` clock now refuse, and
every such stock gets a parse warning whatever its clock's class. A stock with a
second clock refuses too wherever a series could still hold several snapshots:
on any clock when its key lacks an as-of clock, and on a clock whose series still
holds one.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from semantic_rails.compiler import bind_query
from semantic_rails.compiler_parts.sql_lowering import _snapshot_series_columns
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime

ROLE = "temporal_role.f4stock_repo_snapshot_snapshot_date"
WARNING = "STOCK_SNAPSHOT_KEY_MISSING_CLOCK"
SERIES_WARNING = "STOCK_SERIES_HOLDS_AS_OF_CLOCK"
COLLECTED = "temporal_role.f4stock_repo_snapshot_collected_at"
TWO_SNAPSHOTS = (
    "('a@2026-09-21', 'a', date '2026-09-21', timestamp '2026-09-21 06:00', 4, 1), "
    "('a@2026-09-22', 'a', date '2026-09-22', timestamp '2026-09-22 06:00', 4, 1)"
)


def _package(
    root: Path,
    *,
    key: str,
    clock_class: str,
    grain: str = "",
    measure_times: str = "",
    collected_class: str = "event_time",
    rows: str = TWO_SNAPSHOTS,
) -> Path:
    package = root / "f4stock"
    (package / "models").mkdir(parents=True)
    (package / "data").mkdir()
    (package / "package.yml").write_text(
        "schema_version: 1\n"
        "package: {id: f4stock, namespace: f4stock, name: f4stock, warehouse: duckdb,\n"
        f"  default_db: data/f4stock.duckdb, seed: {{kind: external}}, schema_strict: {not grain},\n"
        "  environments: [development]}\n"
    )
    (package / "graph.yml").write_text(
        f"graph:\n  entities:\n    repo_snapshot: {{key: {key}, model: repo_snapshots, "
        "allowed_as_root: true}\n"
    )
    stock = "accumulation: {kind: stock, snapshot: end_of_period}"
    (package / "models" / "repo_snapshots.yml").write_text(
        "model:\n"
        "  id: repo_snapshots\n"
        "  relation: repo_snapshot\n"
        "  entities: {repo_snapshot: {}}\n"
        + (f"  grain: {grain}\n" if grain else "")
        + "  dimensions: {repo: {kind: categorical}}\n"
        "  times:\n"
        f"    snapshot_date: {{column: snapshot_date, kind: date, class: {clock_class}, "
        "default: true}\n"
        f"    collected_at: {{column: collected_at, kind: timestamp, class: {collected_class}}}\n"
        "  measures:\n"
        f"    visitors_14d: {{kind: aggregate, expr: visitors_14d, {stock}, value_type: count"
        f"{measure_times}}}\n"
        f"    stars: {{kind: aggregate, expr: stars, {stock}, value_type: count{measure_times}}}\n"
    )
    (package / "metrics").mkdir()
    # A non-strict package (the only kind that takes `grain:`) publishes each measure.
    (package / "metrics" / "metrics.yml").write_text(
        "metrics: {}\n"
        if grain
        else "metrics:\n"
        + "".join(
            f"  {name}: {{kind: semi_additive, measure: {name}, temporal_role: {ROLE}, "
            "value_type: count}\n"
            for name in ("visitors_14d", "stars")
        )
    )
    connection = duckdb.connect(str(package / "data" / "f4stock.duckdb"))
    connection.execute(
        f"create table repo_snapshot as select * from (values {rows}) "
        "t(repo_snapshot_key, repo, snapshot_date, collected_at, visitors_14d, stars)"
    )
    connection.close()
    return package


def _weekly(runtime: Runtime, measure: str, role: str | None = ROLE) -> list[dict]:
    query: dict = {
        "version": 1,
        "select": [{"expression": {"measure": f"measure.f4stock.{measure}"}, "as": "v"}],
    }
    if role:
        query["time"] = {"temporal_role": role, "grain": "week"}
    result = runtime.query(query)
    assert result["ok"], result.get("errors")
    return [row["v"] for row in result["rows"]]


def _query_warning_codes(runtime: Runtime, measure: str) -> list[str]:
    result = runtime.query(
        {
            "version": 1,
            "select": [{"expression": {"measure": measure}, "as": "v"}],
            "time": {"temporal_role": ROLE, "grain": "week"},
        }
    )
    assert result["ok"], result.get("errors")
    return [warning["code"] for warning in result["warnings"]]


def _warning_codes(package: Path) -> list[str]:
    report, _ = parse_config_report(PackageReference(source_path=str(package)))
    assert report["ok"], report["errors"]
    return [warning["code"] for warning in report["warnings"]]


def test_compound_key_takes_one_snapshot_per_series(tmp_path: Path) -> None:
    package = _package(tmp_path, key="[repo, snapshot_date]", clock_class="as_of_time")
    runtime = Runtime.from_path(str(package))
    assert _weekly(runtime, "visitors_14d") == [4]
    assert _weekly(runtime, "stars") == [1]
    assert WARNING not in _warning_codes(package)
    assert WARNING not in _query_warning_codes(runtime, "measure.f4stock.stars")


def test_surrogate_key_on_an_as_of_clock_refuses(tmp_path: Path) -> None:
    package = _package(tmp_path, key="[repo_snapshot_key]", clock_class="as_of_time")
    runtime = Runtime.from_path(str(package))
    for measure in ("visitors_14d", "stars"):
        with pytest.raises(SemanticLayerError) as raised:
            _weekly(runtime, measure)
        assert raised.value.code == "INVALID_CONFIG"
        assert "snapshot_date" in str(raised.value)
        assert "Key the entity by the series columns" in str(raised.value)
        # The key may hold another clock a policy hides.
        assert "repo_snapshot_key" not in f"{raised.value} {raised.value.details}"
    # Both lowering paths (the snapshot leaf and the anchored entity-set window)
    # take the series key from this helper.
    measure = next(row for row in runtime.config.measures if row.id.endswith(".stars"))
    with pytest.raises(SemanticLayerError):
        _snapshot_series_columns(measure, ROLE, runtime.config)
    assert _warning_codes(package).count(WARNING) == 2


@pytest.mark.parametrize("clock_class", ["event_time", "state_time"])
def test_surrogate_key_on_other_clocks_warns(tmp_path: Path, clock_class: str) -> None:
    # A one-row-per-series current-state table has the same shape and is right, so
    # these clocks warn at authoring time instead of refusing.
    package = _package(tmp_path, key="[repo_snapshot_key]", clock_class=clock_class)
    runtime = Runtime.from_path(str(package))
    # Knowingly unrefused: this sums the two snapshots (the true value is 1), which
    # is also the right answer for a table with one row per series. The answer says so.
    assert _weekly(runtime, "stars") == [2]
    assert _warning_codes(package).count(WARNING) == 2
    assert _query_warning_codes(runtime, "measure.f4stock.stars").count(WARNING) == 1
    # MCP execute returns it at its default (minimal) verbosity.
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        response = adapter.call_tool(
            "execute",
            {
                "query": {
                    "version": 2,
                    "select": [{"as": "v", "expression": {"measure": "measure.f4stock.stars"}}],
                    "time": {"temporal_role": ROLE, "grain": "week"},
                }
            },
        )
    finally:
        adapter.close()
    assert [row["code"] for row in response["warnings"]].count(WARNING) == 1
    # So does a dry-run validation, and a ratio reading both stocks warns for each.
    validated = runtime.validate(
        {"version": 1, "select": [{"expression": {"measure": "measure.f4stock.stars"}, "as": "v"}]}
    )
    assert [row["code"] for row in validated["warnings"]].count(WARNING) == 1
    ratio = {
        "kind": "ratio",
        "numerator": {"measure": "measure.f4stock.stars"},
        "denominator": {"measure": "measure.f4stock.visitors_14d"},
    }
    result = runtime.query({"version": 1, "select": [{"expression": ratio, "as": "v"}]})
    assert [row["code"] for row in result["warnings"]].count(WARNING) == 2
    # A key holding the clock warns about nothing.
    keyed = _package(tmp_path / "keyed", key="[repo, snapshot_date]", clock_class=clock_class)
    assert WARNING not in _query_warning_codes(
        Runtime.from_path(str(keyed)), "measure.f4stock.stars"
    )


@pytest.mark.parametrize(
    ("key", "warnings"), [("[repo_snapshot_key]", 1), ("[repo, snapshot_date]", 0)]
)
def test_a_stock_read_only_by_a_metric_predicate_warns_too(
    tmp_path: Path, key: str, warnings: int
) -> None:
    # A predicate compiles as its own nested query; its stock is warned about as well
    # (once), and a compile served from the cache still carries the warning. Keyed by
    # its series and clock, it stays silent.
    package = _package(tmp_path, key=key, clock_class="state_time")
    runtime = Runtime.from_path(str(package))
    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.f4stock_repo_snapshot",
        "input": {"measure": "measure.f4stock.stars"},
        "op": ">",
        "value": 0,
    }
    visitors = {"kind": "aggregate", "measure": "measure.f4stock.visitors_14d"}
    for query in (
        {
            "version": 1,
            "select": [{"expression": visitors, "as": "v"}],
            "metric_filters": [{"expression": predicate, "op": "=", "value": True}],
        },
        {
            "version": 1,
            "select": [
                {
                    "expression": {**visitors, "filter": {"all": [{"expression": predicate}]}},
                    "as": "v",
                }
            ],
        },
    ):
        for cached in (False, True):
            result = runtime.query(query)
            assert result["explain"]["compile_stats"]["cache_hit"] is cached
            warned = [
                row["details"]["measure_id"] for row in result["warnings"] if row["code"] == WARNING
            ]
            assert warned.count("measure.f4stock.stars") == warnings


def test_declared_grain_and_series_key_agree(tmp_path: Path) -> None:
    # A grain of the clock alone is one series: last snapshot, not the entity's
    # surrogate. (Only a non-strict package can author `grain:`.)
    package = _package(
        tmp_path, key="[repo_snapshot_key]", clock_class="as_of_time", grain="[snapshot_date]"
    )
    assert _weekly(Runtime.from_path(str(package)), "stars") == [1]


def test_an_as_of_gap_leads_the_warning_of_a_two_clock_stock(tmp_path: Path) -> None:
    package = _package(
        tmp_path,
        key="[repo_snapshot_key]",
        clock_class="as_of_time",
        measure_times=", times: [collected_at, snapshot_date]",
    )
    report, _ = parse_config_report(PackageReference(source_path=str(package)))
    flagged = [row for row in report["warnings"] if row["code"] == WARNING]
    assert {row["details"]["clock_class"] for row in flagged} == {"as_of_time"}
    assert all("Its queries are refused" in row["message"] for row in flagged)
    # The key can't tell snapshots apart, so the stock refuses on its event clock and
    # untimed too (the untimed query orders by collected_at, listed first), not just
    # on the as-of clock: each used to return 2 (true 1).
    runtime = Runtime.from_path(str(package))
    for role in (ROLE, COLLECTED, None):
        with pytest.raises(SemanticLayerError) as raised:
            _weekly(runtime, "stars", role)
        assert raised.value.code == "INVALID_CONFIG"
        assert raised.value.details["reason"] == "key_missing_as_of_clock"
        # Only an error on the clock at fault names it (a policy may hide the others);
        # each says how to key the table.
        assert ("snapshot_date" in str(raised.value)) == (role == ROLE)
        assert "Key the entity by the series columns" in str(raised.value)


@pytest.mark.parametrize(
    ("key", "collected_class", "answers"),
    [
        # The as-of clock is in the key: right on it, but ordered by collected_at the
        # series is [repo, snapshot_date] and each snapshot is its own series.
        ("[repo, snapshot_date]", "event_time", {ROLE: [1], COLLECTED: "snapshot_date"}),
        # Two as-of clocks with one in the key: right on that one, refused on the other.
        ("[repo, snapshot_date]", "as_of_time", {ROLE: [1], COLLECTED: "snapshot_date"}),
        # Two as-of clocks in the key: either one leaves the other in the series.
        (
            "[repo, snapshot_date, collected_at]",
            "as_of_time",
            {ROLE: "collected_at", COLLECTED: "snapshot_date"},
        ),
    ],
)
def test_a_series_holding_an_as_of_clock_refuses(
    tmp_path: Path, key: str, collected_class: str, answers: dict
) -> None:
    package = _package(
        tmp_path,
        key=key,
        clock_class="as_of_time",
        collected_class=collected_class,
        measure_times=", times: [snapshot_date, collected_at]",
    )
    runtime = Runtime.from_path(str(package))
    for role, answer in answers.items():
        if isinstance(answer, list):
            assert _weekly(runtime, "stars", role) == answer
            # Examining the model's other clocks doesn't make them query inputs.
            query = {
                "version": 1,
                "select": [{"expression": {"measure": "measure.f4stock.stars"}, "as": "v"}],
                "time": {"temporal_role": role, "grain": "week"},
            }
            assert COLLECTED not in bind_query(runtime.config, None, query).object_ids
            continue
        # Each used to return 2 (true 1). The error names neither the as-of clock at
        # fault nor a key holding it (a policy may hide it); the parse warning does.
        with pytest.raises(SemanticLayerError) as raised:
            _weekly(runtime, "stars", role)
        assert raised.value.code == "INVALID_CONFIG"
        assert raised.value.details["reason"] == "series_holds_as_of_clock"
        assert answer not in f"{raised.value} {raised.value.details}"
        # With two as-of clocks in the key, querying on the other one can't help.
        advice = "Keep one as-of clock" if "collected_at" in key else "Query it on its as-of"
        assert advice in str(raised.value)
    report, _ = parse_config_report(PackageReference(source_path=str(package)))
    flagged = [row for row in report["warnings"] if row["code"] == SERIES_WARNING]
    assert len(flagged) == 2
    if isinstance(answers[ROLE], list):
        # The warning names the clock that answers.
        assert all(ROLE in row["message"] for row in flagged)
    runtime.close()


def test_an_event_clock_in_the_key_still_identifies_a_series(tmp_path: Path) -> None:
    # A cohort table: the event clock (here collected_at, as the cohort) is part of what
    # identifies a series, so on the as-of clock each cohort keeps its last snapshot.
    cohorts = ", ".join(
        f"('{cohort}@{day}', 'a', date '{day}', timestamp '{cohort}', 0, {stars})"
        for cohort in ("2026-08-01", "2026-09-01")
        for day, stars in (("2026-09-21", 9), ("2026-09-22", 10))
    )
    package = _package(
        tmp_path,
        key="[collected_at, snapshot_date]",
        clock_class="as_of_time",
        measure_times=", times: [snapshot_date, collected_at]",
        rows=cohorts,
    )
    runtime = Runtime.from_path(str(package))
    assert _weekly(runtime, "stars") == [20]
    # On the cohort clock the series would be the snapshot date, summing each
    # cohort's two snapshots (19, not 10): refused.
    with pytest.raises(SemanticLayerError) as raised:
        _weekly(runtime, "stars", COLLECTED)
    assert raised.value.details["reason"] == "series_holds_as_of_clock"


def test_current_state_stock_in_jaffle_warns_but_answers(tmp_path_factory) -> None:
    from tests.semantic_rails.conftest import copy_package_config

    package = copy_package_config(
        tmp_path_factory.mktemp("stock_key"), "jaffle_shop", preseed_db=True
    )
    report, _ = parse_config_report(PackageReference(source_path=str(package)))
    flagged = [row for row in report["warnings"] if row["code"] == WARNING]
    assert [row["details"]["measure_id"] for row in flagged] == [
        "measure.jaffle.lifetime_spend_usd"
    ]
    result = Runtime.from_path(str(package)).query(
        {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.lifetime_spend_usd"}, "as": "spend"}
            ],
        }
    )
    assert result["ok"] and result["rows"][0]["spend"] > 0
    assert [row["code"] for row in result["warnings"]] == [WARNING]
    # A segment filtering on it through a metric predicate carries the warning too.
    runtime = Runtime.from_path(str(package))
    for answer in (
        runtime.segment_validate("segment.jaffle.high_value_customers"),
        runtime.segment_explain("segment.jaffle.high_value_customers"),
    ):
        assert [(row["code"], row["details"]["measure_id"]) for row in answer["warnings"]] == [
            (WARNING, "measure.jaffle.lifetime_spend_usd")
        ]


def test_an_as_of_clock_on_one_fact_model_leaves_its_siblings_alone(tmp_path_factory) -> None:
    # Fact models share their time entity, so one fact's as-of clock must not count as
    # a clock of another fact's stocks.
    from tests.semantic_rails.conftest import copy_package_config

    package = copy_package_config(tmp_path_factory.mktemp("facts"), "jaffle_shop", preseed_db=True)
    monthly = package / "models" / "core" / "monthly_metrics.yml"
    monthly.write_text(monthly.read_text().replace("class: calendar_time", "class: as_of_time", 1))
    report, _ = parse_config_report(PackageReference(source_path=str(package)))
    flagged = [row for row in report["warnings"] if str(row.get("code", "")).startswith("STOCK_")]
    assert [row["details"]["measure_id"] for row in flagged] == [
        "measure.jaffle.lifetime_spend_usd"
    ]
    runtime = Runtime.from_path(str(package))
    for measure, role in (
        ("rolling_7d_revenue_usd", "temporal_role.jaffle_daily_metric_day"),
        ("prior_period_revenue_usd", "temporal_role.jaffle_monthly_metric_month"),
    ):
        result = runtime.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": f"measure.jaffle.{measure}"}, "as": "v"}],
                "time": {"temporal_role": role, "grain": "month"},
            }
        )
        assert result["ok"] and result["rows"], (measure, result.get("errors"))


def test_a_refusal_names_no_clock_a_policy_hides(tmp_path: Path) -> None:
    from dataclasses import replace

    from semantic_rails.schema import SemanticPolicyConfig

    package = _package(
        tmp_path,
        key="[repo, snapshot_date]",
        clock_class="as_of_time",
        measure_times=", times: [snapshot_date, collected_at]",
    )
    config = Runtime.from_path(str(package)).config
    policy = SemanticPolicyConfig(
        id="policy.test.collected_only",
        kind="metric_constraint",
        object_ids=["measure.f4stock.stars"],
        config={"allowed_temporal_roles": [COLLECTED]},
    )
    runtime = Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=str(package)
    )
    # The policy hides the as-of clock: querying on it is denied.
    with pytest.raises(SemanticLayerError) as denied:
        _weekly(runtime, "stars", ROLE)
    assert denied.value.code == "POLICY_DENIED"
    with pytest.raises(SemanticLayerError) as raised:
        _weekly(runtime, "stars", COLLECTED)
    assert raised.value.code == "INVALID_CONFIG"
    shown = f"{raised.value} {raised.value.details}"
    assert ROLE not in shown and "snapshot_date" not in shown
