"""A hop into a time-valid table needs a query time.

A relationship with ``temporal_validity`` joins a table holding several versions of a row, each
valid over a window. A hop into that table reaches at most one version only when the query
gives each row an instant to pick its version by (a ``time``). Without one the join reaches
every version, and a row counts once per version, so the engine refuses with ``FANOUT_UNSAFE``,
naming the relationship and the entity, rather than count a row twice. It never picks a
"current" version or de-duplicates: a row in two versions would belong to two groups. A hop out
of the table holding the window reads one version per row, and needs no time.

Fixture: usage events of accounts, with a segment history. Account A1 was ``starter`` in
January and ``business`` from February; A2 is ``starter`` from January; A3 has no history row;
usage 5 is A1's, from December, before its first version. Tier is read through the history.
The gold values come from SQL written independently of the engine (scalar subqueries, no
joins), run on the same database, and are also spelled out by hand.
"""

from __future__ import annotations

import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

import semantic_rails.fanout as fanout_module
from semantic_rails.ast import normalize_query
from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.grain_recovery import mixed_grain_pairing_enrichment
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import analyze_fanout
from semantic_rails.metadata import discover_payload, inspect_payload
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.planner.plan import plan_payload
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

SEED_SQL = """
CREATE TABLE accounts (account_id VARCHAR, region VARCHAR);
INSERT INTO accounts VALUES ('A1', 'North'), ('A2', 'South'), ('A3', 'North');
CREATE TABLE tiers (tier_id VARCHAR, tier_name VARCHAR);
INSERT INTO tiers VALUES ('T1', 'Gold'), ('T2', 'Silver');
CREATE TABLE account_segments (account_id VARCHAR, segment VARCHAR, tier_id VARCHAR,
  seats INTEGER, valid_from TIMESTAMP, valid_to TIMESTAMP);
INSERT INTO account_segments VALUES
  ('A1', 'starter', 'T1', 2, TIMESTAMP '2026-01-01 00:00:00', TIMESTAMP '2026-02-01 00:00:00'),
  ('A1', 'business', 'T2', 5, TIMESTAMP '2026-02-01 00:00:00', NULL),
  ('A2', 'starter', 'T1', 1, TIMESTAMP '2026-01-01 00:00:00', NULL);
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
              as: relationship.usage_account_segment
              entities: [usage, account_segment]
              cardinality: many_to_one
              target: [account_id]
              allowed_directions: [forward]
              temporal_validity:
                valid_from: account_segments.valid_from
                valid_to: account_segments.valid_to
            # The window is on the near table: each segment row is one version already.
            account_segment_account:
              as: relationship.account_segment_account
              entities: [account_segment, account]
              cardinality: many_to_one
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
          measures:
            seats: {label: Seats, kind: aggregate, expr: seats, default_agg: sum,
              accumulation: {kind: flow}}
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
    # Two named metrics adding the same two measures, in either order.
    **{
        f"metrics/{left}_plus_{right}.yml": f"""
        metric:
          as: metric.hist.{left}_plus_{right}
          label: {left.title()} plus {right}
          kind: derived
          value_type: number
          expression:
            kind: arithmetic
            op: add
            left: {{measure: measure.hist.{left}}}
            right: {{measure: measure.hist.{right}}}
        """
        for left, right in (("amount", "seats"), ("seats", "amount"))
    },
}

HOP = "relationship.usage_account_segment"
OUT_HOP = "relationship.account_segment_account"
ACCOUNT = "entity.hist_account"
ACCOUNT_KEY = "dimension.hist_account_id"
HISTORY = "entity.hist_account_segment"
SEGMENT = "dimension.hist_account_segment_segment"
HISTORY_KEY = "dimension.hist_account_segment_account_id"
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


def _write_package(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "hist"
    (pkg / "data").mkdir(parents=True)
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    for name, body in files.items():
        (pkg / name).parent.mkdir(exist_ok=True)
        (pkg / name).write_text(textwrap.dedent(body))
    return pkg


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_package(tmp_path_factory.mktemp("time_valid"), FILES)


@pytest.fixture(scope="module")
def runtime(package: Path):
    runtime = Runtime.from_path(str(package))
    yield opened(runtime)
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


def _seats(**extra: Any) -> dict[str, Any]:
    select = [{"as": "value", "expression": {"measure": "measure.hist.seats"}}]
    return {"version": 1, "select": select, **extra}


def _amount_and_seats(*, seats_first: bool = False, **extra: Any) -> dict[str, Any]:
    amount = {"as": "value", "expression": {"measure": "measure.hist.amount"}}
    seats = {"as": "seats", "expression": {"measure": "measure.hist.seats"}}
    select = [seats, amount] if seats_first else [amount, seats]
    return {"version": 1, "select": select, **extra}


def _compound(*, seats_first: bool = False, **extra: Any) -> dict[str, Any]:
    left, right = [
        row["expression"] for row in _amount_and_seats(seats_first=seats_first)["select"]
    ]
    expression = {"kind": "arithmetic", "op": "add", "left": left, "right": right}
    return {"version": 1, "select": [{"as": "value", "expression": expression}], **extra}


def _named_metric(*, seats_first: bool = False, **extra: Any) -> dict[str, Any]:
    metric = "metric.hist.seats_plus_amount" if seats_first else "metric.hist.amount_plus_seats"
    return {"version": 1, "select": [{"as": "value", "expression": {"metric": metric}}], **extra}


STARTER = [{"field": SEGMENT, "op": "=", "value": "starter"}]


def _rows(runtime: Runtime, query: dict[str, Any], keys: list[str]) -> dict[Any, float]:
    return {
        tuple(
            datetime.fromisoformat(row[key]).month if key == MONTH else row[key] for key in keys
        ): float(row["value"])
        for row in runtime.query(query)["rows"]
    }


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(_amount(group_by=[SEGMENT]), id="group-by"),
        pytest.param(_amount(group_by=[HISTORY_KEY]), id="group-by-history-key"),
        pytest.param(_amount(where=STARTER), id="where"),
        pytest.param(
            _amount(where=[{"field": HISTORY_KEY, "op": "=", "value": "A1"}]),
            id="where-history-key",
        ),
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
                            "filter": {"all": STARTER},
                        },
                    }
                ],
            },
            id="measure-filter",
        ),
        pytest.param({"version": 1, "group_by": [USAGE_ID, SEGMENT]}, id="dimension-only"),
        # Amount and seats, as two measures, one compound, or a named metric, in either order.
        *[
            pytest.param(
                shape(seats_first=seats_first, **clause),
                id=f"{name}{'-seats-first' if seats_first else ''}-{next(iter(clause))}",
            )
            for name, shape in (
                ("two-measures", _amount_and_seats),
                ("compound", _compound),
                ("named-metric", _named_metric),
            )
            for seats_first in (False, True)
            for clause in ({"group_by": [SEGMENT]}, {"where": STARTER})
        ],
    ],
)
def test_a_time_valid_hop_without_a_query_time_is_refused(runtime, query):
    """Without a time, usage 1 would count under both of A1's segments: amount grouped by
    segment would read 38 + 33 + 7 = 78 against a total of 45."""
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["reason"] == "time_valid_hop_without_query_time"
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


@pytest.mark.parametrize(
    ("clause", "expected", "sql"),
    [
        pytest.param(
            {"group_by": [HISTORY_KEY]},
            {("A1", 1): 10.0, ("A1", 2): 20.0, ("A2", 1): 5.0, (None, 1): 7.0, (None, 12): 3.0},
            f"SELECT {SQL_AS_OF.format(column='account_id')}, month(u.used_at),"
            " SUM(u.amount) FROM usage AS u GROUP BY 1, 2",
            id="group-by",
        ),
        pytest.param(
            {"where": [{"field": HISTORY_KEY, "op": "=", "value": "A1"}]},
            {(1,): 10.0, (2,): 20.0},
            "SELECT month(u.used_at), SUM(u.amount) FROM usage AS u"
            f" WHERE {SQL_AS_OF.format(column='account_id')} = 'A1' GROUP BY 1",
            id="where",
        ),
    ],
)
def test_the_history_key_reads_the_version_valid_at_the_usage_time(
    runtime, package, gold, clause, expected, sql
):
    """A matching source key does not prove a history version exists at the usage time."""
    config = load_package_config(str(package))
    query = _amount(**clause, time=MONTHLY)

    result = _rows(runtime, query, [*clause.get("group_by", []), MONTH])

    assert "LEFT JOIN account_segments" in compile_query(config, Registry(config), query)["sql"]
    assert result == expected
    assert result == gold(sql)


def test_a_hop_out_of_the_table_holding_the_window_needs_no_time(runtime, gold):
    """Each segment row is one version already, so reading its account is a plain lookup."""
    query = {
        "version": 1,
        "select": [{"as": "value", "expression": {"measure": "measure.hist.seats"}}],
        "group_by": [REGION],
    }

    by_region = _rows(runtime, query, [REGION])

    assert by_region == {("North",): 7.0, ("South",): 1.0}
    assert by_region == gold(
        "SELECT (SELECT a.region FROM accounts AS a WHERE a.account_id = s.account_id),"
        " SUM(s.seats) FROM account_segments AS s GROUP BY 1"
    )


def test_a_hop_out_of_the_validity_window_keeps_the_source_key(runtime, package, gold):
    query = _seats(group_by=[ACCOUNT_KEY])
    config = load_package_config(str(package))
    sql = compile_query(config, Registry(config), query)["sql"]

    by_account = _rows(runtime, query, [ACCOUNT_KEY])

    assert "account_segments.account_id AS g1" in sql
    assert "JOIN accounts" not in sql
    assert by_account == {("A1",): 7.0, ("A2",): 1.0}
    assert by_account == gold(
        "SELECT account_id, SUM(seats) FROM account_segments GROUP BY account_id"
    )


@pytest.mark.parametrize("clock", ["valid_from", "valid_to"])
@pytest.mark.parametrize("schema", ["", "analytics"])
def test_an_anchored_outgoing_hop_keeps_closed_and_open_versions_regions(
    tmp_path, gold, clock, schema
):
    """An existing segment version looks up its account even at its exclusive end time."""
    files = dict(FILES)
    files["models/account_segments.yml"] = files["models/account_segments.yml"].replace(
        f"column: {clock}, kind: timestamp,", f"column: {clock}, kind: timestamp, default: true,"
    )
    seed = SEED_SQL
    if schema:
        files["models/account_segments.yml"] = files["models/account_segments.yml"].replace(
            "relation: account_segments", f"relation: {schema}.account_segments"
        )
        files["graph.yml"] = files["graph.yml"].replace(
            "account_segments.valid_", f"{schema}.account_segments.valid_"
        )
        seed = f"CREATE SCHEMA {schema};\n" + seed.replace(
            "TABLE account_segments", f"TABLE {schema}.account_segments"
        ).replace("INTO account_segments", f"INTO {schema}.account_segments")
    package = _write_package(tmp_path, files)
    (package / "data" / "seed.sql").write_text(seed)
    runtime = opened(Runtime.from_path(str(package)))
    role = f"temporal_role.hist_account_segment_{clock}"
    time_key = f"{role}__month"
    query = _seats(group_by=[REGION], time={"temporal_role": role, "grain": "month"})
    try:
        rows = runtime.query(query)["rows"]
    finally:
        runtime.close()
    actual = {
        (row[REGION], datetime.fromisoformat(row[time_key]) if row[time_key] else None): float(
            row["value"]
        )
        for row in rows
    }
    expected = gold(
        "SELECT (SELECT a.region FROM accounts a WHERE a.account_id = s.account_id),"
        f" date_trunc('month', s.{clock}), SUM(s.seats) FROM account_segments s GROUP BY 1, 2"
    )
    assert expected == (
        {
            ("North", datetime(2026, 1, 1)): 2.0,
            ("North", datetime(2026, 2, 1)): 5.0,
            ("South", datetime(2026, 1, 1)): 1.0,
        }
        if clock == "valid_from"
        else {("North", datetime(2026, 2, 1)): 2.0, ("North", None): 5.0, ("South", None): 1.0}
    )
    assert actual == expected


def test_schema_qualified_windows_keep_outgoing_lookups_safe_and_incoming_hops_anchored(tmp_path):
    files = dict(FILES)
    files["models/account_segments.yml"] = files["models/account_segments.yml"].replace(
        "relation: account_segments", "relation: analytics.account_segments"
    )
    files["graph.yml"] = files["graph.yml"].replace(
        "account_segments.valid_", "analytics.account_segments.valid_"
    )
    pkg = _write_package(tmp_path, files)
    seed = "CREATE SCHEMA analytics;\n" + SEED_SQL.replace(
        "TABLE account_segments", "TABLE analytics.account_segments"
    ).replace("INTO account_segments", "INTO analytics.account_segments")
    (pkg / "data" / "seed.sql").write_text(seed)
    runtime = Runtime.from_path(str(pkg))
    try:
        query = _seats(group_by=[REGION])
        assert runtime.validate(query)["ok"] is True
        by_region = _rows(runtime, query, [REGION])
        assert by_region == {("North",): 7.0, ("South",): 1.0}
        gold = runtime.adapter.query(
            "SELECT (SELECT a.region FROM accounts a WHERE a.account_id = s.account_id) AS region,"
            " SUM(s.seats) AS seats FROM analytics.account_segments s GROUP BY 1"
        )
        assert by_region == {(row["region"],): float(row["seats"]) for row in gold}
        with pytest.raises(SemanticLayerError) as exc:
            runtime.compile(_amount(group_by=[SEGMENT]))
        assert exc.value.details["reason"] == "time_valid_hop_without_query_time"
        assert _rows(runtime, _amount(group_by=[SEGMENT], time=MONTHLY), [SEGMENT, MONTH]) == {
            ("starter", 1): 15.0,
            ("business", 2): 20.0,
            (None, 1): 7.0,
            (None, 12): 3.0,
        }
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        pytest.param([], {(): (45.0, 8.0)}, id="total"),
        pytest.param([REGION], {("North",): (40.0, 7.0), ("South",): (5.0, 1.0)}, id="by-region"),
    ],
)
@pytest.mark.parametrize("seats_first", [False, True], ids=["amount-first", "seats-first"])
def test_measures_aggregated_on_their_own_need_no_time(
    runtime, gold, seats_first, group_by, expected
):
    """Amount and seats are aggregated separately and joined on the grain keys: no usage row
    joins a segment version, whichever measure the query selects first."""
    rows = runtime.query(_amount_and_seats(seats_first=seats_first, group_by=group_by))["rows"]
    result = {
        tuple(row[key] for key in group_by): (float(row["value"]), float(row["seats"]))
        for row in rows
    }

    def total(column: str, table: str) -> dict[Any, float]:
        region = "(SELECT a.region FROM accounts AS a WHERE a.account_id = t.account_id), "
        return gold(
            f"SELECT {region if group_by else ''}SUM(t.{column}) FROM {table} AS t"
            + (" GROUP BY 1" if group_by else "")
        )

    amounts, seats = total("amount", "usage"), total("seats", "account_segments")
    assert result == expected
    assert result == {key: (amounts[key], seats[key]) for key in amounts}


def _predicate(entity: str, scope_mode: str) -> dict[str, Any]:
    expression = {
        "kind": "metric_predicate",
        "entity": entity,
        "scope_mode": scope_mode,
        "input": {"measure": "measure.hist.seats"},
        "op": ">",
        "value": 0,
    }
    return {"expression": expression, "op": "=", "value": True}


@pytest.mark.parametrize(
    ("query", "code"),
    [
        pytest.param(
            _seats(metric_filters=[_predicate(ACCOUNT, "entity_only")]),
            "PREDICATE_SCOPE_UNSAFE",
            id="entity",
        ),
        pytest.param(
            _seats(group_by=[REGION], metric_filters=[_predicate(HISTORY, "contextual")]),
            "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE",
            id="context",
        ),
        pytest.param(
            _seats(
                where=[{"field": REGION, "op": "=", "value": "North"}],
                metric_filters=[_predicate(HISTORY, "contextual")],
            ),
            "PREDICATE_FILTER_INCOMPATIBLE",
            id="filter",
        ),
    ],
)
def test_a_metric_predicate_needs_a_time_for_any_time_valid_hop_on_its_path(runtime, query, code):
    """Stricter than the query itself: the predicate's scope refuses even the hop out of the
    segment table, which seats grouped or filtered by region crosses without a time."""
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)

    assert exc.value.code == code
    assert "requires a time anchor" in str(exc.value)
    assert exc.value.details["path"] == [OUT_HOP]


CUSTOMER_SEGMENT = "dimension.jaffle_customer_history_segment"


def _order_conversion(
    *, group_by: list[str] | None = None, base: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    conversion = {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.jaffle.order_count", **(base or {})},
        "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
        **extra,
    }
    select = [{"as": "rate", "expression": conversion}]
    return {"version": 1, "select": select, "group_by": list(group_by or [])}


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(_order_conversion(group_by=[CUSTOMER_SEGMENT]), id="group-by"),
        pytest.param(_order_conversion(constant_properties=[CUSTOMER_SEGMENT]), id="property"),
        pytest.param(
            _order_conversion(
                base={"filter": {"all": [{"field": CUSTOMER_SEGMENT, "op": "=", "value": "new"}]}}
            ),
            id="operand-filter",
        ),
    ],
)
def test_a_conversion_reading_a_history_dimension_without_a_time_is_refused(runtime_factory, query):
    """Each order would join every version of its customer's history."""
    runtime = runtime_factory("jaffle_shop")
    try:
        with pytest.raises(SemanticLayerError) as exc:
            runtime.query(query)
    finally:
        runtime.close()

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["reason"] == "time_valid_hop_without_query_time"
    assert exc.value.details["relationships"] == ["relationship.jaffle_order_customer_history"]
    assert exc.value.details["entities"] == ["entity.jaffle_customer_history"]


def test_valid_values_counts_usage_at_its_version_or_refuses(runtime):
    """A live lookup filtered by usage has to count usage, which reads the segment through the
    hop: at each usage's version with a time, and never through every version without one."""
    by_usage = {"where": [{"field": USAGE_ID, "op": ">", "value": 0}]}

    counted = valid_values_payload(
        runtime,
        dimension_id=SEGMENT,
        query={**by_usage, "time": MONTHLY},
        allow_live_query=True,
        include_counts=True,
    )
    with pytest.raises(SemanticLayerError) as exc:
        valid_values_payload(runtime, dimension_id=SEGMENT, query=by_usage, allow_live_query=True)

    assert counted["anchor_measure"] == "measure.hist.amount"
    assert {row["value"]: float(row["count"]) for row in counted["values"]} == {
        "starter": 15.0,
        "business": 20.0,
    }
    attempts = {row["measure"]: row["code"] for row in exc.value.details["attempts"]}
    assert attempts["measure.hist.amount"] == "FANOUT_UNSAFE"


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
    # Reachability metadata has no query, and rates the hop by its cardinality alone.
    assert analyze_fanout(config, usage, [HOP], validity_windows=False)["status"] == "ok"


UNKNOWN = {"as": "nope", "expression": {"measure": "measure.hist.nope"}}


@pytest.mark.parametrize(
    "partial",
    [
        pytest.param(_amount(), id="amount"),
        pytest.param(
            {**_amount(), "select": [*_amount()["select"], UNKNOWN]}, id="unknown-measure"
        ),
    ],
)
def test_discovery_offers_the_history_grouping_that_compilation_refuses_without_a_time(
    runtime, partial
):
    """Discovery and inspection answer from reachability alone, also with an unknown measure in
    the partial query. The grouping they offer without a time is refused when it is compiled,
    naming the hop and asking for a time; it is never answered from every version."""
    found = discover_payload(runtime, terms="segment", partial_query=partial, kinds=["dimension"])
    card = inspect_payload(runtime, object_id=SEGMENT, partial_query=partial)["card"]

    (row,) = [row for row in found["dimensions"] if row["id"] == SEGMENT]
    assert row["available"] is True
    patches = [
        row["query_patch"] for row in card["starter_query_patches"] if row["kind"] == "group_by"
    ]
    assert [patch["group_by"] for patch in patches] == [[SEGMENT]]
    with pytest.raises(SemanticLayerError) as exc:
        runtime.compile({**patches[0], "select": _amount()["select"]})
    assert exc.value.details["reason"] == "time_valid_hop_without_query_time"
    assert "Add `time`" in exc.value.details["hint"]


def test_the_planner_keeps_a_history_filter_when_the_question_gives_a_time(runtime_factory):
    """Planning answers as before: it keeps the history filter, adds the month the question
    names, and the plan compiles, each order reading its customer's version at its time."""
    runtime = runtime_factory("jaffle_shop")
    try:
        plan = plan_payload(runtime, intent="revenue from high_value customers by month")
        query = plan["best"]["query_ir"]
        report = runtime.validate(query)
    finally:
        runtime.close()

    assert plan["status"] == "ok"
    assert {"field": CUSTOMER_SEGMENT, "op": "=", "value": "high_value"} in query["where"]
    assert query["time"]["temporal_role"] == "temporal_role.jaffle_order_time"
    assert report["ok"] is True


def test_recovery_hints_skip_a_dimension_behind_a_time_valid_hop(package):
    """Mixed-grain enrichment lists the dimensions a measure can group by; one that needs a
    time the query lacks is left out rather than ending the enrichment."""
    config = load_package_config(str(package))

    def compatible(query: dict[str, Any]) -> dict[str, Any]:
        return mixed_grain_pairing_enrichment(
            config=config,
            query=normalize_query(query),
            measure_ids=["measure.hist.amount"],
            target_entity=ACCOUNT,
        )

    without_time, with_time = compatible(_amount()), compatible(_amount(time=MONTHLY))

    assert without_time["measures"] == ["measure.hist.amount"]
    assert REGION in without_time["compatible_dimensions"]
    assert SEGMENT not in without_time["compatible_dimensions"]
    assert SEGMENT in with_time["compatible_dimensions"]


@pytest.mark.parametrize("dimension", [SEGMENT, HISTORY_KEY])
def test_the_join_refuses_a_time_valid_hop_the_classification_let_through(
    package, monkeypatch, dimension
):
    """Force the bypass: rate every hop as if the query had a time. The join itself still
    refuses to cross the hop without the time its validity window needs."""
    config = load_package_config(str(package))
    rate = fanout_module._directional_status
    monkeypatch.setattr(
        fanout_module,
        "_directional_status",
        lambda rel, *, current_entity, time_bound=False, near_table="": rate(
            rel, current_entity=current_entity, time_bound=True, near_table=near_table
        ),
    )

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), _amount(group_by=[dimension]))

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["relationships"] == [HOP]
    assert exc.value.details["entities"] == [HISTORY]
