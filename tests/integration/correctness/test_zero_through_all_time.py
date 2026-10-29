"""How many of something a package has ever had, with no time window, when its table declares
how far it is loaded (``zero_through``): a filter that matched no row reads 0, as a loaded
period does; without a declared edge it reads NULL, as before."""

import pytest

from semantic_rails.errors import SemanticLayerError

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


# Refunds dated by their order's clock (another model's), over all time. Orders 1 and 2 are
# before the load log's Sunday 2024-09-29; order 3 lands on 2024-10-01, after it. No refund is
# a goods refund unless ``stray`` adds one on order 3.
ORDERS = [(1, "2024-09-03 10:00:00"), (2, "2024-09-20 12:00:00"), (3, "2024-10-01 08:00:00")]
REFUNDS = [(11, 1, "shipping"), (12, 2, "shipping")]
ORDER_CLOCK = "temporal_role.dated_order_ordered_at"


def _refunds(request, backend_name, tmp_path, edge, stray=False, replacement=False):
    """Refunds whose clock is their order's ``ordered_at``, read under ``edge``; with
    ``replacement`` each also names a replacement order, a second route to the clock that the
    package doesn't decide between."""
    zero_through = {"loaded": {"loaded": "loads.through_day"}, "empty_log": {"loaded": "loads.through_day"}, "now": "now"}  # fmt: skip
    models = {
        "refunds": {
            "id": "refunds",
            "relation": "events",
            "entities": {"refund": {}, "order": {}},
            **({"zero_through": zero_through[edge]} if edge in zero_through else {}),
            "dimensions": {"refund_type": {"kind": "categorical"}},
            "measures": {
                "refunds": {
                    "kind": "entity_count",
                    "entity_key": "event_id",
                    "accumulation": {"kind": "flow"},
                    "times": [ORDER_CLOCK],
                }
            },
        },
        "orders": {
            "id": "orders",
            "relation": "purchases",
            "entities": {"order": {}},
            "times": {"ordered_at": {**EVENT_TIME, "column": "ordered_at"}},
        },
        "loads": {
            "id": "loads",
            "relation": "loads",
            "entities": {"load": {}},
            "times": {"through_day": {"column": "through_day", "kind": "date"}},
        },
    }
    metrics = {
        "goods_refunds": {
            "as": "metric.dated.goods_refunds",
            "label": "Goods refunds",
            "kind": "derived",
            "value_type": "count",
            "temporal_role": ORDER_CLOCK,
            "expression": {
                "kind": "aggregate",
                "measure": "measure.dated.refunds",
                "aggregation": "count_distinct",
                "filter": {
                    "all": [
                        {
                            "field": "dimension.dated_refund_refund_type",
                            "op": "is",
                            "value": "goods",
                        }
                    ]
                },
            },
        }
    }
    entities = {
        "refund": {"key": ["event_id"], "model": "refunds"},
        "order": {"key": ["order_id"], "model": "orders"},
        "load": {"key": ["run_id"], "model": "loads"},
    }
    refunds = [*REFUNDS, *([(13, 3, "goods")] if stray else [])]
    relationships = {
        "refund_replacement": {
            "entities": ["refund", "order"],
            "cardinality": "many_to_one",
            "via": ["replacement_id"],
            "target": ["order_id"],
        }
    }
    seed = (
        "CREATE TABLE {purchases} (order_id INTEGER, ordered_at TIMESTAMP); INSERT INTO "
        "{purchases} VALUES "
        + ", ".join(f"({n}, TIMESTAMP '{at}')" for n, at in ORDERS)
        + "; CREATE TABLE {events} (event_id INTEGER, order_id INTEGER, refund_type VARCHAR(20),"
        " replacement_id INTEGER); INSERT INTO {events} VALUES "
        + ", ".join(f"({n}, {order}, '{kind}', 3)" for n, order, kind in refunds)
        + "; CREATE TABLE {loads} (run_id INTEGER, through_day DATE);"
        + ("" if edge == "empty_log" else "INSERT INTO {loads} VALUES (1, DATE '2024-09-29');")
    )
    return _package(
        request,
        backend_name,
        tmp_path,
        "dated",
        models,
        entities,
        seed,
        metrics,
        relationships if replacement else None,
    )


def _refunds_reference(runtime, tables, edge):
    """Plain SQL of the rule over all time, on the order's clock: the refunds whose order the
    edge lets through (an empty load log lets none through); goods ones, or 0 when the table
    declares its edge and such a refund exists, else NULL; and every such refund, else NULL."""
    cap = CAPS[edge].replace("event_at", "o.ordered_at").format(**tables)
    read = "{events} r JOIN {purchases} o ON o.order_id = r.order_id WHERE ".format(**tables) + cap
    declared = "FALSE" if edge == "data" else "TRUE"
    goods = f"{read} AND r.refund_type = 'goods'"
    sql = (
        f"SELECT CASE WHEN (SELECT COUNT(*) FROM {goods}) > 0 "
        f"THEN (SELECT COUNT(DISTINCT r.event_id) FROM {goods}) "
        f"WHEN {declared} AND (SELECT COUNT(*) FROM {read}) > 0 THEN 0 END, "
        f"(SELECT CASE WHEN COUNT(*) > 0 THEN COUNT(DISTINCT r.event_id) END FROM {read})"
    )
    [row] = _rows(runtime, sql)
    return list(row)


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "edge,stray,goods,refunds",
    [
        # The load log covers orders 1 and 2: their refunds are read, none is a goods refund.
        ("loaded", False, 0, 2),
        # The goods refund on order 3, after the load log, is not read.
        ("loaded", True, 0, 2),
        # An empty load log certifies nothing: no refund is read.
        ("empty_log", False, None, None),
        ("empty_log", True, None, None),
        # Every order is at or before now.
        ("now", False, 0, 2),
        ("now", True, 1, 3),
        # Without a declared edge every refund is read, and a never-matched filter is NULL.
        ("data", False, None, 2),
        ("data", True, 1, 3),
    ],
)
def test_refunds_dated_by_their_order_are_capped_on_the_orders_clock(
    request, backend_name, tmp_path, edge, stray, goods, refunds
):
    """How many goods refunds, and refunds, have we ever issued, when a refund is dated by its
    order: the declared edge caps them on the order's clock, so an empty load log reads NULL,
    not 0 beside every refund, and a refund on an order after the load log is not read."""
    runtime, tables = _refunds(request, backend_name, tmp_path, edge, stray)
    try:
        result = runtime.query(
            {"select": ["metric.dated.goods_refunds", "measure.dated.refunds"]}, now=NOW
        )
        reference = _refunds_reference(runtime, tables, edge)
    finally:
        if backend_name == "postgres":
            _rows(
                runtime,
                "DROP TABLE {events}; DROP TABLE {purchases}; DROP TABLE {loads}".format(**tables),
            )
        runtime.close()
    assert reference == [goods, refunds]
    assert result["rows"] == [{"goods_refunds": goods, "refunds": refunds}]
    assert bool(_warnings(result, "NO_DATA_IN_SCOPE")) == (goods is None)


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_refunds_whose_clock_has_two_routes_read_as_without_a_declared_edge(
    request, backend_name, tmp_path
):
    """Each refund names its order and a replacement order, and the package doesn't say which
    one dates it: no single order caps it, so with no window the load log isn't read there. The
    goods refunds ever read NULL with NO_DATA_IN_SCOPE, as without a declared edge, and every
    refund is read; a series on that clock is refused for its ambiguous route."""
    runtime, tables = _refunds(request, backend_name, tmp_path, "loaded", replacement=True)
    call = {"select": ["metric.dated.goods_refunds", "measure.dated.refunds"]}
    try:
        result = runtime.query(call, now=NOW)
        with pytest.raises(SemanticLayerError) as series:
            runtime.query(
                {**call, "time": {"start": "2024-09-02", "end": "2024-10-07", "grain": "week"}},
                now=NOW,
            )
    finally:
        if backend_name == "postgres":
            _rows(
                runtime,
                "DROP TABLE {events}; DROP TABLE {purchases}; DROP TABLE {loads}".format(**tables),
            )
        runtime.close()
    assert result["rows"] == [{"goods_refunds": None, "refunds": 2}]
    [warning] = _warnings(result, "NO_DATA_IN_SCOPE")
    assert "goods_refunds" in warning["message"]
    assert series.value.code == "AMBIGUOUS_PATH"
