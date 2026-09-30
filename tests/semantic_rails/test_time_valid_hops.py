"""A hop over a time-valid relationship needs a query time.

A relationship with ``temporal_validity`` keeps several versions of the far row, each valid
over a window. A hop over it reaches at most one version only when the query gives each row an
instant to pick its version by (a ``time``). Without one the join reaches every version, and a
row counts once per version, so the engine refuses with ``FANOUT_UNSAFE``, naming the
relationship and the entity, rather than count a row twice. It never picks a "current" version
or de-duplicates: a row in two versions would belong to two groups.

Fixture: usage events of accounts, with a segment history. Account A1 was ``starter`` in
January and ``business`` from February; A2 is ``starter`` from January; A3 has no history row;
usage 5 is A1's, from December, before its first version. Tier is read through the history.
The gold values come from SQL written independently of the engine (scalar subqueries, no
joins), run on the same database, and are also spelled out by hand.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest

import semantic_rails.fanout as fanout_module
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import analyze_fanout
from semantic_rails.metadata_parts.path_coverage import _path_availability
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime

SEED_SQL = """
CREATE TABLE accounts (account_id VARCHAR, region VARCHAR);
INSERT INTO accounts VALUES ('A1', 'North'), ('A2', 'South'), ('A3', 'North');
CREATE TABLE tiers (tier_id VARCHAR, tier_name VARCHAR);
INSERT INTO tiers VALUES ('T1', 'Gold'), ('T2', 'Silver');
CREATE TABLE account_segments (account_id VARCHAR, segment VARCHAR, tier_id VARCHAR,
  valid_from TIMESTAMP, valid_to TIMESTAMP);
INSERT INTO account_segments VALUES
  ('A1', 'starter', 'T1', TIMESTAMP '2026-01-01 00:00:00', TIMESTAMP '2026-02-01 00:00:00'),
  ('A1', 'business', 'T2', TIMESTAMP '2026-02-01 00:00:00', NULL),
  ('A2', 'starter', 'T1', TIMESTAMP '2026-01-01 00:00:00', NULL);
CREATE TABLE usage (usage_id INTEGER, account_id VARCHAR, used_at TIMESTAMP,
  amount DECIMAL(10, 2));
INSERT INTO usage VALUES
  (1, 'A1', TIMESTAMP '2026-01-10 00:00:00', 10),
  (2, 'A1', TIMESTAMP '2026-02-10 00:00:00', 20),
  (3, 'A2', TIMESTAMP '2026-01-20 00:00:00', 5),
  (4, 'A3', TIMESTAMP '2026-01-05 00:00:00', 7),
  (5, 'A1', TIMESTAMP '2025-12-15 00:00:00', 3)
"""

FILES = {
    "package.yml": """
        schema_version: 1
        package:
          id: hist
          namespace: hist
          warehouse: duckdb
          default_db: data/hist.duckdb
          seed: {kind: sql_script, source: data/seed.sql}
        defaults:
          dimension: {groupable: true, filterable: true}
        """,
    "graph.yml": """
        graph:
          entities:
            account: {label: Account, key: [account_id], model: accounts}
            tier: {label: Tier, key: [tier_id], model: tiers}
            account_segment: {label: Account segment, key: [account_id, valid_from],
              model: account_segments, allowed_as_root: false}
            usage: {label: Usage, key: [usage_id], model: usage}
          relationships:
            usage_account_segment:
              id: relationship.usage_account_segment
              entities: [usage, account_segment]
              cardinality: many_to_one
              target: [account_id]
              allowed_directions: [forward]
              temporal_validity:
                valid_from: account_segments.valid_from
                valid_to: account_segments.valid_to
        """,
    "models/accounts.yml": """
        model:
          id: accounts
          relation: accounts
          entities: {account: {}}
          dimensions:
            region: {label: Region, kind: categorical}
        """,
    "models/tiers.yml": """
        model:
          id: tiers
          relation: tiers
          entities: {tier: {}}
          dimensions:
            tier_name: {label: Tier name, kind: categorical}
        """,
    "models/account_segments.yml": """
        model:
          id: account_segments
          relation: account_segments
          entities: {account_segment: {}, account: {}, tier: {}}
          times:
            valid_from: {label: Valid from, column: valid_from, kind: timestamp,
              class: state_time}
            valid_to: {label: Valid to, column: valid_to, kind: timestamp, class: state_time}
          dimensions:
            segment: {label: Segment, kind: categorical}
        """,
    "models/usage.yml": """
        model:
          id: usage
          relation: usage
          entities:
            usage: {}
            account: {}
            account_segment: {expr: account_id}
          times:
            used_at: {label: Used at, column: used_at, kind: timestamp, class: event_time,
              supported_grains: [day, month], default: true}
          measures:
            amount: {label: Amount, kind: aggregate, expr: amount, default_agg: sum,
              accumulation: {kind: flow}}
        """,
}

HOP = "relationship.usage_account_segment"
HISTORY = "entity.hist_account_segment"
SEGMENT = "dimension.hist_account_segment_segment"
TIER = "dimension.hist_tier_tier_name"
REGION = "dimension.hist_account_region"
USAGE_ID = "dimension.hist_usage_id"
MONTHLY = {"temporal_role": "temporal_role.hist_usage_used_at", "grain": "month"}
MONTH = "temporal_role.hist_usage_used_at__month"

# The version valid at each usage's time, read with a scalar subquery: NULL where none is.
SQL_AS_OF = (
    "(SELECT s.{column} FROM account_segments AS s WHERE s.account_id = u.account_id"
    " AND s.valid_from <= u.used_at AND (s.valid_to > u.used_at OR s.valid_to IS NULL))"
)
SQL_SEGMENT = SQL_AS_OF.format(column="segment")
SQL_TIER = (
    f"(SELECT t.tier_name FROM tiers AS t WHERE t.tier_id = {SQL_AS_OF.format(column='tier_id')})"
)


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pkg = tmp_path_factory.mktemp("time_valid") / "hist"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    for name, body in FILES.items():
        (pkg / name).write_text(textwrap.dedent(body))
    return pkg


@pytest.fixture(scope="module")
def runtime(package: Path):
    runtime = Runtime.from_path(str(package))
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def gold():
    connection = duckdb.connect()
    connection.execute(SEED_SQL)

    def run(sql: str) -> dict[Any, float]:
        return {tuple(row[:-1]): float(row[-1]) for row in connection.execute(sql).fetchall()}

    yield run
    connection.close()


def _amount(**extra: Any) -> dict[str, Any]:
    select = [{"as": "value", "expression": {"measure": "measure.hist.amount"}}]
    return {"version": 1, "select": select, **extra}


def _rows(runtime: Runtime, query: dict[str, Any], keys: list[str]) -> dict[Any, float]:
    return {
        tuple(row[key].month if key == MONTH else row[key] for key in keys): float(row["value"])
        for row in runtime.query(query)["rows"]
    }


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(_amount(group_by=[SEGMENT]), id="group-by"),
        pytest.param(_amount(where=[{"field": SEGMENT, "op": "=", "value": "starter"}]), id="where"),
        pytest.param(_amount(group_by=[TIER]), id="two-hops"),
        pytest.param(
            {
                "version": 1,
                "select": [
                    {
                        "as": "value",
                        "expression": {
                            "kind": "aggregate",
                            "measure": "measure.hist.amount",
                            "filter": {"all": [{"field": SEGMENT, "op": "=", "value": "starter"}]},
                        },
                    }
                ],
            },
            id="measure-filter",
        ),
        pytest.param({"version": 1, "group_by": [USAGE_ID, SEGMENT]}, id="dimension-only"),
    ],
)
def test_a_time_valid_hop_without_a_query_time_is_refused(runtime, query):
    """Without a time, usage 1 would count under both of A1's segments: amount grouped by
    segment would read 38 + 33 + 7 = 78 against a total of 45."""
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["relationships"] == [HOP]
    assert exc.value.details["entities"] == [HISTORY]
    assert HOP in str(exc.value) and HISTORY in str(exc.value)
    report = runtime.validate(query)
    (error,) = report["errors"]
    assert error["code"] == "FANOUT_UNSAFE"
    assert "Add `time`" in error["recovery_hints"][0]["message"]


def test_with_a_time_each_row_reads_the_version_valid_at_its_time(runtime, gold):
    by_segment = _rows(runtime, _amount(group_by=[SEGMENT], time=MONTHLY), [SEGMENT, MONTH])

    assert by_segment == {
        ("starter", 1): 15.0,  # usage 1 (A1 was starter) and 3
        ("business", 2): 20.0,  # usage 2
        (None, 1): 7.0,  # usage 4: A3 has no history row
        (None, 12): 3.0,  # usage 5: before A1's first version
    }
    assert sum(by_segment.values()) == 45.0
    assert by_segment == gold(
        f"SELECT {SQL_SEGMENT}, month(u.used_at), SUM(u.amount) FROM usage AS u GROUP BY 1, 2"
    )


def test_with_a_time_a_filter_and_a_second_hop_read_the_same_version(runtime, gold):
    starter = _rows(
        runtime,
        _amount(where=[{"field": SEGMENT, "op": "=", "value": "starter"}], time=MONTHLY),
        [MONTH],
    )
    by_tier = _rows(runtime, _amount(group_by=[TIER], time=MONTHLY), [TIER, MONTH])

    assert starter == {(1,): 15.0}
    assert starter == gold(
        f"SELECT month(u.used_at), SUM(u.amount) FROM usage AS u"
        f" WHERE {SQL_SEGMENT} = 'starter' GROUP BY 1"
    )
    assert by_tier == {("Gold", 1): 15.0, ("Silver", 2): 20.0, (None, 1): 7.0, (None, 12): 3.0}
    assert by_tier == gold(
        f"SELECT {SQL_TIER}, month(u.used_at), SUM(u.amount) FROM usage AS u GROUP BY 1, 2"
    )


def test_a_relationship_without_temporal_validity_needs_no_time(runtime, gold):
    by_region = _rows(runtime, _amount(group_by=[REGION]), [REGION])

    assert by_region == {("North",): 40.0, ("South",): 5.0}
    assert by_region == gold(
        "SELECT (SELECT a.region FROM accounts AS a WHERE a.account_id = u.account_id),"
        " SUM(u.amount) FROM usage AS u GROUP BY 1"
    )


def test_valid_values_counts_rows_at_their_version_or_refuses(runtime):
    """A live valid-values lookup counts rows through the same classification."""
    counted = valid_values_payload(
        runtime,
        dimension_id=SEGMENT,
        query={"time": MONTHLY},
        allow_live_query=True,
        include_counts=True,
    )
    with pytest.raises(SemanticLayerError) as exc:
        valid_values_payload(runtime, dimension_id=SEGMENT, allow_live_query=True)

    assert {row["value"]: float(row["count"]) for row in counted["values"]} == {
        "starter": 15.0,
        "business": 20.0,
    }
    assert {attempt["code"] for attempt in exc.value.details["attempts"]} == {"FANOUT_UNSAFE"}


def test_the_classification_names_the_hop_and_the_fix(package):
    config = load_package_config(str(package))
    usage = "entity.hist_usage"

    with pytest.raises(SemanticLayerError) as exc:
        analyze_fanout(config, usage, [HOP])
    anchored = analyze_fanout(config, usage, [HOP], time_bound_relationships={HOP})

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["reason"] == "time_valid_hop_without_query_time"
    assert "time" in exc.value.details["hint"]
    assert anchored["status"] == "ok"
    # Reachability metadata answers for a query that gives a time.
    assert _path_availability(config, usage, HISTORY)["available"] is True


def test_the_join_refuses_a_time_valid_hop_the_classification_let_through(package, monkeypatch):
    """Force the bypass: rate every hop as if the query had a time. The join itself still
    refuses to cross the hop without the time its validity window needs."""
    config = load_package_config(str(package))
    rate = fanout_module._directional_status
    monkeypatch.setattr(
        fanout_module,
        "_directional_status",
        lambda rel, *, current_entity, time_bound=False: rate(
            rel, current_entity=current_entity, time_bound=True
        ),
    )

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), _amount(group_by=[SEGMENT]))

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["relationships"] == [HOP]
    assert exc.value.details["entities"] == [HISTORY]
