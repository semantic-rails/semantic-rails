"""A stock measure's key must contain its snapshot clock.

A snapshot table holds one row per series per snapshot. Keyed by a surrogate
that is unique per snapshot row (``a@2026-09-21``), "the last snapshot per key"
keeps every snapshot, and a week holding two daily snapshots of one repository
reported 8 unique visitors instead of 4 (and 2 stars instead of 1), with parse
and runtime validation green. Queries on an ``as_of_time`` clock now refuse, and
every such stock gets a parse warning whatever its clock's class.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from semantic_rails.compiler_parts.sql_lowering import _snapshot_series_columns
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

ROLE = "temporal_role.f4stock_repo_snapshot_snapshot_date"
WARNING = "STOCK_SNAPSHOT_KEY_MISSING_CLOCK"


def _package(
    root: Path, *, key: str, clock_class: str, grain: str = "", measure_times: str = ""
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
        "    collected_at: {column: collected_at, kind: timestamp, class: event_time}\n"
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
        "create table repo_snapshot as select * from (values "
        "('a@2026-09-21', 'a', date '2026-09-21', timestamp '2026-09-21 06:00', 4, 1), "
        "('a@2026-09-22', 'a', date '2026-09-22', timestamp '2026-09-22 06:00', 4, 1)) "
        "t(repo_snapshot_key, repo, snapshot_date, collected_at, visitors_14d, stars)"
    )
    connection.close()
    return package


def _weekly(runtime: Runtime, measure: str) -> list[dict]:
    result = runtime.query(
        {
            "version": 1,
            "select": [{"expression": {"measure": f"measure.f4stock.{measure}"}, "as": "v"}],
            "time": {"temporal_role": ROLE, "grain": "week"},
        }
    )
    assert result["ok"], result.get("errors")
    return [row["v"] for row in result["rows"]]


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


def test_surrogate_key_on_an_as_of_clock_refuses(tmp_path: Path) -> None:
    package = _package(tmp_path, key="[repo_snapshot_key]", clock_class="as_of_time")
    runtime = Runtime.from_path(str(package))
    for measure in ("visitors_14d", "stars"):
        with pytest.raises(SemanticLayerError) as raised:
            _weekly(runtime, measure)
        assert raised.value.code == "INVALID_CONFIG"
        assert "snapshot_date" in str(raised.value)
        assert raised.value.details["row_key"] == ["repo_snapshot_key"]
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
    # is also the right answer for a table with one row per series.
    assert _weekly(runtime, "stars") == [2]
    assert _warning_codes(package).count(WARNING) == 2


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
    assert all("are refused" in row["message"] for row in flagged)
    with pytest.raises(SemanticLayerError):
        _weekly(Runtime.from_path(str(package)), "stars")


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
