"""How many of something a package has ever had, with no time window, when its table declares
how far it is loaded (``zero_through``): a filter that matched no row reads 0, as a loaded
period does; without a declared edge it reads NULL, as before."""

import pytest

from .conftest import _rows
from .test_zero_through import BACKENDS, EVENT_TIME, NOW, _package, _warnings

# Accounts over all time (no window), under each kind of edge: none is an enterprise account.
ACCOUNTS = [(1, "2024-09-03 10:00:00", "smb", "East"), (2, "2024-09-05 12:00:00", "mid", "West")]
ACCOUNTS += [(3, "2024-09-17 09:00:00", "smb", "West")]
ENTERPRISE = {"field": "dimension.ever_account_segment", "op": "is", "value": "enterprise"}
CAPS = {
    "loaded": "event_at < (SELECT CAST(MAX(through_day) AS TIMESTAMP) + INTERVAL '1' DAY "
    "FROM {loads})",
    "empty_log": "event_at < (SELECT CAST(MAX(through_day) AS TIMESTAMP) + INTERVAL '1' DAY "
    "FROM {loads})",
    "now": "event_at <= TIMESTAMP '2024-11-05 12:00:00'",
    "data": "TRUE",
}


def _accounts(request, backend_name, tmp_path, edge, stray=False):
    """Accounts that never include an enterprise one (``stray``: one lands on 2024-10-01,
    after the load log's Sunday 2024-09-29), read under ``edge``."""
    zero_through = {"loaded": {"loaded": "loads.through_day"}, "empty_log": {"loaded": "loads.through_day"}, "now": "now"}  # fmt: skip
    models = {
        "accounts": {
            "id": "accounts",
            "relation": "events",
            "entities": {"account": {}},
            **({"zero_through": zero_through[edge]} if edge in zero_through else {}),
            "times": {"event_at": EVENT_TIME},
            "dimensions": {"segment": {"kind": "categorical"}, "region": {"kind": "categorical"}},
            "measures": {
                "new_accounts": {
                    "kind": "entity_count",
                    "entity_key": "event_id",
                    "accumulation": {"kind": "flow"},
                }
            },
        },
        "loads": {
            "id": "loads",
            "relation": "loads",
            "entities": {"load": {}},
            "times": {"through_day": {"column": "through_day", "kind": "date"}},
        },
    }
    metrics = {
        "enterprise_accounts": {
            "as": "metric.ever.enterprise_accounts",
            "label": "Enterprise accounts",
            "kind": "derived",
            "value_type": "count",
            "temporal_role": "temporal_role.ever_account_event_at",
            "expression": {
                "kind": "aggregate",
                "measure": "measure.ever.new_accounts",
                "aggregation": "count_distinct",
                "filter": {"all": [ENTERPRISE]},
            },
        }
    }
    entities = {
        "account": {"key": ["event_id"], "model": "accounts"},
        "load": {"key": ["run_id"], "model": "loads"},
    }
    rows = [*ACCOUNTS, *([(4, "2024-10-01 08:00:00", "enterprise", "East")] if stray else [])]
    values = ", ".join(
        f"({n}, TIMESTAMP '{at}', '{seg}', '{region}')" for n, at, seg, region in rows
    )
    seed = (
        "CREATE TABLE {events} (event_id INTEGER, event_at TIMESTAMP, segment VARCHAR(20), "
        f"region VARCHAR(20)); INSERT INTO {{events}} VALUES {values};"
        "CREATE TABLE {loads} (run_id INTEGER, through_day DATE);"
        + ("" if edge == "empty_log" else "INSERT INTO {loads} VALUES (1, DATE '2024-09-29');")
    )
    return _package(request, backend_name, tmp_path, "ever", models, entities, seed, metrics)


def _ever_reference(runtime, tables, edge, where="TRUE", scope="dataset"):
    """Plain SQL of the rule over all time: the enterprise accounts among the rows the edge
    lets through and the call's where keeps; with none, 0 when the table declares its edge
    and holds such a row (inside the where under query scope), else NULL."""
    cap = CAPS[edge].format(**tables)
    observed = f"{cap} AND {where}" if scope == "query" else cap
    declared = "FALSE" if edge == "data" else "TRUE"
    sql = (
        "SELECT CASE WHEN (SELECT COUNT(*) FROM {events} WHERE {cap} AND {where} "
        "AND segment = 'enterprise') > 0 OR ({declared} AND (SELECT COUNT(*) FROM {events} "
        "WHERE {observed}) > 0) THEN (SELECT COUNT(DISTINCT event_id) FROM {events} "
        "WHERE {cap} AND {where} AND segment = 'enterprise') END"
    ).format(cap=cap, where=where, observed=observed, declared=declared, **tables)
    [[value]] = _rows(runtime, sql)
    return value


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "edge,stray,count",
    [
        ("loaded", False, 0),
        # The enterprise account after the load log is not read.
        ("loaded", True, 0),
        ("now", False, 0),
        # An empty load log certifies nothing: no row is read.
        ("empty_log", False, None),
        # Without a declared edge, a filter no row has matched reads NULL, as before.
        ("data", False, None),
    ],
)
def test_how_many_enterprise_accounts_ever_reads_zero_where_the_table_declares_its_edge(
    request, backend_name, tmp_path, edge, stray, count
):
    """How many enterprise accounts have we ever had, with no time window, before the first
    one: a table that declares how far it is loaded holds data of nothing, so 0 with no
    NO_DATA_IN_SCOPE; without a declared edge, or under an empty load log, NULL with it."""
    runtime, tables = _accounts(request, backend_name, tmp_path, edge, stray)
    try:
        result = runtime.query({"select": ["metric.ever.enterprise_accounts"]}, now=NOW)
        reference = _ever_reference(runtime, tables, edge)
    finally:
        if backend_name == "postgres":
            _rows(runtime, "DROP TABLE {events}; DROP TABLE {loads}".format(**tables))
        runtime.close()
    assert reference == count
    assert result["rows"] == [{"enterprise_accounts": reference}]
    assert bool(_warnings(result, "NO_DATA_IN_SCOPE")) == (count is None)


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "region,scope,count",
    [
        ("West", "dataset", 0),
        ("West", "query", 0),
        # The call's where keeps no account, so under query scope nothing is observed.
        ("North", "query", None),
        ("North", "dataset", 0),
    ],
)
def test_enterprise_accounts_ever_in_a_region_follow_the_observation_scope(
    request, backend_name, tmp_path, region, scope, count
):
    """ "Enterprise accounts we have ever had in the West" under the load log: 0 in either
    scope; in a region with no account, 0 under dataset and NULL under query."""
    runtime, tables = _accounts(request, backend_name, tmp_path, "loaded")
    call = {
        "select": ["metric.ever.enterprise_accounts"],
        "where": [{"field": "dimension.ever_account_region", "op": "is", "value": region}],
        "observation_scope": scope,
    }
    try:
        result = runtime.query(call, now=NOW)
        reference = _ever_reference(runtime, tables, "loaded", f"region = '{region}'", scope)
    finally:
        if backend_name == "postgres":
            _rows(runtime, "DROP TABLE {events}; DROP TABLE {loads}".format(**tables))
        runtime.close()
    assert reference == count
    assert result["rows"] == [{"enterprise_accounts": reference}]
