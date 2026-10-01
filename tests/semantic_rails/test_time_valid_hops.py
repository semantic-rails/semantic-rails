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
from dataclasses import replace
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
from semantic_rails.metadata import build_options_payload, discover_payload, inspect_payload
from semantic_rails.metadata_parts.path_coverage import _path_availability
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.planner._base import _dimension, _score
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime

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
              id: relationship.usage_account_segment
              entities: [usage, account_segment]
              cardinality: many_to_one
              target: [account_id]
              allowed_directions: [forward]
              temporal_validity:
                valid_from: account_segments.valid_from
                valid_to: account_segments.valid_to
            # The window is on the near table: each segment row is one version already.
            account_segment_account:
              id: relationship.account_segment_account
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
}

# The bidirectional pair of the authoring guide: from an account, the reverse hop into the segment
# table joins every version of the account, unless a time picks one.
BIDIRECTIONAL = {
    **{name: body for name, body in FILES.items() if name not in {"models/usage.yml", "models/tiers.yml"}},
    "graph.yml": """
        graph:
          entities:
            account: {label: Account, key: [account_id], model: accounts}
            account_segment: {label: Account segment, key: [account_id, valid_from],
              model: account_segments, allowed_as_root: false}
          relationships:
            account_segment_account:
              id: relationship.account_segment_account
              entities: [account_segment, account]
              cardinality: many_to_one
              safety: requires_rewrite
              temporal_validity:
                valid_from: account_segments.valid_from
                valid_to: account_segments.valid_to
        """,
    "models/accounts.yml": """
        model:
          id: accounts
          relation: accounts
          entities: {account: {}}
          measures:
            account_count: {label: Accounts, kind: entity_count, entity_key: account_id,
              accumulation: {kind: event}, value_type: count}
        """,
    "models/account_segments.yml": FILES["models/account_segments.yml"].replace(
        "{account_segment: {}, account: {}, tier: {}}", "{account_segment: {}, account: {}}"
    ),
}

HOP = "relationship.usage_account_segment"
OUT_HOP = "relationship.account_segment_account"
ACCOUNT = "entity.hist_account"
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


def _write_package(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "hist"
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    for name, body in files.items():
        (pkg / name).write_text(textwrap.dedent(body))
    return pkg


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_package(tmp_path_factory.mktemp("time_valid"), FILES)


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


def _seats(**extra: Any) -> dict[str, Any]:
    select = [{"as": "value", "expression": {"measure": "measure.hist.seats"}}]
    return {"version": 1, "select": select, **extra}


def _amount_and_seats(*, seats_first: bool = False, **extra: Any) -> dict[str, Any]:
    amount = {"as": "value", "expression": {"measure": "measure.hist.amount"}}
    seats = {"as": "seats", "expression": {"measure": "measure.hist.seats"}}
    select = [seats, amount] if seats_first else [amount, seats]
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
        pytest.param(
            _amount(where=[{"field": SEGMENT, "op": "=", "value": "starter"}]), id="where"
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
                            "filter": {"all": [{"field": SEGMENT, "op": "=", "value": "starter"}]},
                        },
                    }
                ],
            },
            id="measure-filter",
        ),
        pytest.param({"version": 1, "group_by": [USAGE_ID, SEGMENT]}, id="dimension-only"),
        pytest.param(_amount_and_seats(group_by=[SEGMENT]), id="two-measures"),
        pytest.param(
            _amount_and_seats(seats_first=True, group_by=[SEGMENT]), id="two-measures-seats-first"
        ),
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
    return {"version": 2, "select": select, "group_by": list(group_by or [])}


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
def test_a_conversion_reading_a_history_dimension_without_a_time_is_refused(
    runtime_factory, query
):
    """Each order would join every version of its customer's history."""
    runtime = runtime_factory("jaffle_shop")
    try:
        with pytest.raises(SemanticLayerError) as exc:
            runtime.query(query)
    finally:
        runtime.close()

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["reason"] == "time_valid_hop_without_query_time"
    assert exc.value.details["relationships"] == ["relationship.order_to_customer_history"]
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
    # Metadata offers the hop only when told the query gives a time; by default it has none.
    assert _path_availability(config, usage, HISTORY, query_time=True)["available"] is True
    assert _path_availability(config, usage, HISTORY)["error_code"] == "FANOUT_UNSAFE"


@pytest.mark.parametrize(("time", "offered"), [(None, False), (MONTHLY, True)])
def test_discover_and_build_options_offer_the_hop_only_with_a_time(runtime, time, offered):
    partial = _amount(**({"time": time} if time else {}))

    found = discover_payload(runtime, terms="segment", partial_query=partial, kinds=["dimension"])
    options = build_options_payload(
        runtime, partial_query=partial, step="group_by", focus_terms="segment"
    )

    (row,) = [row for row in found["dimensions"] if row["id"] == SEGMENT]
    assert row["available"] is offered
    assert (HOP in row["blocked_reason"]) is not offered  # the reason names the hop
    patches = [row["id"] for row in [*options["recommended"], *options["available"]]]
    assert (SEGMENT in patches) is offered


@pytest.mark.parametrize(("time", "offered"), [(None, False), (MONTHLY, True)])
def test_inspect_offers_a_history_grouping_only_with_a_time(runtime, time, offered):
    """Without a time, the card names the hop and asks for one instead of a patch the engine
    would refuse."""
    partial = _amount(**({"time": time} if time else {}))

    card = inspect_payload(runtime, object_id=SEGMENT, partial_query=partial)["card"]

    group_by = [row for row in card["starter_query_patches"] if row["kind"] == "group_by"]
    assert bool(group_by) is offered
    for row in group_by:
        assert runtime.validate(row["query_patch"])["ok"] is True
    assert (HOP in card.get("blocked_reason", "")) is not offered
    hints = [hint["message"] for hint in card.get("recovery_hints", [])]
    assert any("Add `time`" in hint for hint in hints) is not offered


def test_a_reverse_hop_into_the_window_is_not_offered_without_a_time(tmp_path):
    """From an account, the segment table holds every version of it: discover lists it only
    as a rewrite, as before; a time is what would make it one version per account."""
    pkg = _write_package(tmp_path, BIDIRECTIONAL)
    partial = {
        "version": 1,
        "select": [{"as": "accounts", "expression": {"measure": "measure.hist.account_count"}}],
    }
    runtime = Runtime.from_path(str(pkg))
    try:
        found = discover_payload(
            runtime, terms="segment", partial_query=partial, kinds=["entity", "dimension"]
        )
    finally:
        runtime.close()

    rows = {row["id"]: row for row in [*found["entities"], *found["dimensions"]]}
    assert rows[HISTORY]["available"] is False
    assert rows[SEGMENT]["available"] is False
    config = load_package_config(str(pkg))
    assert _path_availability(config, ACCOUNT, HISTORY, query_time=True)["available"] is True


def test_the_planner_prefers_the_dimension_that_needs_no_time_on_an_equal_score(package):
    """The segment table's account id, relabelled to sort first, still loses to the account's
    own: the score ties, and the version only a query time picks comes after any label."""
    config = load_package_config(str(package))
    history_id = "dimension.hist_account_segment_account_id"
    relabelled = replace(
        config,
        dimensions=[
            replace(dim, label="Account") if dim.id == history_id else dim
            for dim in config.dimensions
        ],
    )
    history = next(dim for dim in relabelled.dimensions if dim.id == history_id)

    chosen = _dimension(relabelled, ["account"])

    assert chosen.id == "dimension.hist_account_id"
    assert _score(chosen, ["account"]) == _score(history, ["account"])
    assert chosen.label > history.label


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


def test_the_join_refuses_a_time_valid_hop_the_classification_let_through(package, monkeypatch):
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
        compile_query(config, Registry(config), _amount(group_by=[SEGMENT]))

    assert exc.value.code == "FANOUT_UNSAFE"
    assert exc.value.details["relationships"] == [HOP]
    assert exc.value.details["entities"] == [HISTORY]
