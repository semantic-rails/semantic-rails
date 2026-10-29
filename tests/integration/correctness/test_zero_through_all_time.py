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


# Sales over all time: sale 4 (East, 999) lands on 2024-10-01, after the load log's Sunday
# 2024-09-29.
SALES = [(1, "2024-09-03 10:00:00", "East", 100), (2, "2024-09-05 12:00:00", "West", 60)]
SALES += [(3, "2024-09-17 09:00:00", "East", 80), (4, "2024-10-01 08:00:00", "East", 999)]
AMOUNT = {"kind": "column", "column": "amount", "entity": "entity.ever_sale"}
OVER_50 = {
    "kind": "comparison",
    "op": ">",
    "left": AMOUNT,
    "right": {"kind": "literal", "value": 50},
}


def _sales(request, backend_name, tmp_path, edge):
    """Sales read under ``edge``, with "sales over 50" declared (``large_sales``, a CASE with no
    ELSE) beside revenue."""
    zero_through = {"loaded": {"loaded": "loads.through_day"}, "empty_log": {"loaded": "loads.through_day"}, "now": "now"}  # fmt: skip
    over_50 = {"kind": "comparison", "op": ">", "left": {"kind": "column", "column": "amount"}}
    over_50["right"] = {"kind": "literal", "value": 50}
    models = {
        "sales": {
            "id": "sales",
            "relation": "events",
            "entities": {"sale": {}},
            **({"zero_through": zero_through[edge]} if edge in zero_through else {}),
            "times": {"event_at": EVENT_TIME},
            "dimensions": {"region": {"kind": "categorical"}},
            "measures": {
                "revenue": {"kind": "aggregate", "expr": "amount"},
                "large_sales": {
                    "kind": "aggregate",
                    "expr": {
                        "kind": "case",
                        "whens": [
                            {"when": over_50, "then": {"kind": "column", "column": "amount"}}
                        ],
                    },
                },
            },
        },
        "loads": {
            "id": "loads",
            "relation": "loads",
            "entities": {"load": {}},
            "times": {"through_day": {"column": "through_day", "kind": "date"}},
        },
    }
    entities = {
        "sale": {"key": ["event_id"], "model": "sales"},
        "load": {"key": ["run_id"], "model": "loads"},
    }
    values = ", ".join(f"({n}, TIMESTAMP '{at}', '{region}', {a})" for n, at, region, a in SALES)
    seed = (
        "CREATE TABLE {events} (event_id INTEGER, event_at TIMESTAMP, region VARCHAR(20), "
        f"amount INTEGER); INSERT INTO {{events}} VALUES {values};"
        "CREATE TABLE {loads} (run_id INTEGER, through_day DATE);"
        + ("" if edge == "empty_log" else "INSERT INTO {loads} VALUES (1, DATE '2024-09-29');")
    )
    return _package(request, backend_name, tmp_path, "ever", models, entities, seed)


def _number(value):
    return None if value is None else float(value)


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "edge,east,total",
    [
        # Sale 4, after the load log, is not read, as revenue beside it doesn't read it.
        ("loaded", (180.0, 2.0, 180.0), 240.0),
        ("now", (1179.0, 3.0, 1179.0), 1239.0),
        ("data", (1179.0, 3.0, 1179.0), 1239.0),
        # An empty load log certifies nothing: no row is read.
        ("empty_log", None, None),
    ],
)
def test_sales_over_50_ever_written_inline_stop_at_the_load_log_as_revenue_does(
    request, backend_name, tmp_path, edge, east, total
):
    """ "The value and number of sales over 50 we have ever had, by region", written inline
    (``aggregate_if``) with no time window: they read the rows the table's edge lets through,
    as revenue and the same sum declared do."""
    runtime, tables = _sales(request, backend_name, tmp_path, edge)
    value = {"kind": "aggregate_if", "aggregation": "sum", "value": AMOUNT, "condition": OVER_50}
    count = {"kind": "aggregate_if", "aggregation": "count", "condition": OVER_50}
    select = [
        {"expression": value, "as": "large_value"},
        {"expression": count, "as": "large_count"},
        "measure.ever.revenue",
        "measure.ever.large_sales",
    ]
    region = "dimension.ever_sale_region"
    call = {"select": select, "group_by": [region], "order_by": [{"field": region}]}
    cap = CAPS[edge].format(**tables)
    try:
        by_region = runtime.query(call, now=NOW)
        ungrouped = runtime.query({"select": select[:1]}, now=NOW)
        reference = _rows(
            runtime,
            "SELECT region, SUM(CASE WHEN amount > 50 THEN amount END), "
            "COUNT(CASE WHEN amount > 50 THEN 1 END), SUM(amount) "
            f"FROM {tables['events']} WHERE {cap} GROUP BY region ORDER BY region",
        )
    finally:
        if backend_name == "postgres":
            _rows(runtime, "DROP TABLE {events}; DROP TABLE {loads}".format(**tables))
        runtime.close()
    rows = [
        (
            row[region],
            *(_number(row[key]) for key in ("large_value", "large_count", "revenue")),
            _number(row["large_sales"]),
        )
        for row in by_region["rows"]
    ]
    assert rows == [(name, *map(_number, cells), _number(cells[0])) for name, *cells in reference]
    expected = [] if east is None else [("East", *east, east[0]), ("West", 60.0, 1.0, 60.0, 60.0)]
    assert rows == expected
    assert [_number(row["large_value"]) for row in ungrouped["rows"]] == [total]


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


# Order lines read on four clocks, with no window: booked by the order's `ordered_at`, fulfilled
# by its `fulfilled_at` (orders 3 and 4 are not fulfilled), shipped and invoiced by the line's own
# times. Order 3 and the late times land after the load log's Sunday 2024-09-29; line 26's order
# is missing and line 25 hasn't shipped.
FOLD_ORDERS = [
    (1, "2024-09-03 10:00:00", "'2024-09-10 09:00:00'"),
    (2, "2024-09-20 12:00:00", "'2024-10-02 08:00:00'"),
    (3, "2024-10-01 08:00:00", "NULL"),
    (4, "2024-09-25 09:00:00", "NULL"),
]
FOLD_LINES = [
    (21, 1, 5, "'2024-09-05 10:00:00'", "'2024-09-06 10:00:00'"),
    (22, 1, 3, "'2024-09-08 10:00:00'", "'2024-10-03 10:00:00'"),
    (23, 2, 7, "'2024-09-22 10:00:00'", "'2024-09-23 10:00:00'"),
    (24, 3, 11, "'2024-10-02 10:00:00'", "'2024-10-02 11:00:00'"),
    (25, 4, 2, "NULL", "'2024-09-26 10:00:00'"),
    (26, 99, 13, "'2024-09-15 10:00:00'", "'2024-09-16 10:00:00'"),
]
FOLD_CLOCKS = {
    "booked": ("temporal_role.folded_order_ordered_at", "o.ordered_at"),
    "fulfilled": ("temporal_role.folded_order_fulfilled_at", "o.fulfilled_at"),
    "shipped": ("shipped_at", "l.shipped_at"),
    "invoiced": ("invoiced_at", "l.invoiced_at"),
}
# Each alone, by hand from the rows above: (load log, now).
FOLD_ALONE = {"booked": (17, 28), "fulfilled": (8, 15), "shipped": (28, 39), "invoiced": (27, 41)}


def _folded(request, backend_name, tmp_path, edge):
    """Order lines whose four amounts are each read on its own clock, under ``edge``."""
    stamp = {"kind": "timestamp", "class": "event_time"}
    models = {
        "lines": {
            "id": "lines",
            "relation": "events",
            "entities": {"line": {}, "order": {}},
            "zero_through": {"loaded": "loads.through_day"} if edge == "loaded" else "now",
            "times": {
                "shipped_at": {**stamp, "column": "shipped_at", "default": True},
                "invoiced_at": {**stamp, "column": "invoiced_at"},
            },
            "measures": {
                name: {
                    "kind": "aggregate",
                    "expr": "amount",
                    "default_agg": "sum",
                    "value_type": "currency",
                    "accumulation": {"kind": "flow"},
                    "times": [clock],
                }
                for name, (clock, _) in FOLD_CLOCKS.items()
            },
        },
        "orders": {
            "id": "orders",
            "relation": "purchases",
            "entities": {"order": {}},
            "times": {
                "ordered_at": {**stamp, "column": "ordered_at", "default": True},
                "fulfilled_at": {**stamp, "column": "fulfilled_at"},
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
        "fulfilment_rate": {
            "as": "metric.folded.fulfilment_rate",
            "label": "Fulfilment rate",
            "kind": "ratio",
            "numerator": "fulfilled",
            "denominator": "booked",
            "value_type": "number",
        },
        "unfulfilled": {
            "as": "metric.folded.unfulfilled",
            "label": "Booked, not fulfilled",
            "kind": "derived",
            "value_type": "currency",
            "expression": {
                "kind": "arithmetic",
                "op": "subtract",
                "left": {"kind": "measure", "measure": "measure.folded.booked"},
                "right": {"kind": "measure", "measure": "measure.folded.fulfilled"},
            },
        },
    }
    entities = {
        "line": {"key": ["event_id"], "model": "lines"},
        "order": {"key": ["order_id"], "model": "orders"},
        "load": {"key": ["run_id"], "model": "loads"},
    }
    seed = (
        "CREATE TABLE {purchases} (order_id INTEGER, ordered_at TIMESTAMP, fulfilled_at "
        "TIMESTAMP); INSERT INTO {purchases} VALUES "
        + ", ".join(
            f"({n}, TIMESTAMP '{at}', CAST({done} AS TIMESTAMP))" for n, at, done in FOLD_ORDERS
        )
        + "; CREATE TABLE {events} (event_id INTEGER, order_id INTEGER, amount INTEGER, "
        "shipped_at TIMESTAMP, invoiced_at TIMESTAMP); INSERT INTO {events} VALUES "
        + ", ".join(
            f"({n}, {order}, {amount}, CAST({shipped} AS TIMESTAMP), CAST({invoiced} AS TIMESTAMP))"
            for n, order, amount, shipped, invoiced in FOLD_LINES
        )
        + "; CREATE TABLE {loads} (run_id INTEGER, through_day DATE);"
        "INSERT INTO {loads} VALUES (1, DATE '2024-09-29');"
    )
    return _package(request, backend_name, tmp_path, "folded", models, entities, seed, metrics)


def _folded_reference(runtime, tables, edge, name):
    """Plain SQL of one amount on its own clock: the lines whose clock (the order's, through
    the line's order, or the line's own) the edge lets through."""
    column = FOLD_CLOCKS[name][1]
    cap = CAPS[edge].replace("event_at", column).format(**tables)
    sql = (
        "SELECT SUM(l.amount) FROM {events} l LEFT JOIN {purchases} o "
        "ON o.order_id = l.order_id WHERE "
    ).format(**tables) + cap
    [[value]] = _rows(runtime, sql)
    return int(value)


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("edge", ["loaded", "now"])
@pytest.mark.parametrize(
    "pair",
    [
        ("booked", "fulfilled"),  # two clocks of another model
        ("shipped", "booked"),  # the line's own clock beside the order's
        ("shipped", "invoiced"),  # two of the line's own clocks
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_two_amounts_on_two_clocks_each_read_their_own_clock_ever(
    request, backend_name, tmp_path, edge, pair, reverse
):
    """ "What are our booked and fulfilled revenue, all time?": asked together, in either
    order, each amount reads the lines its own clock lets through, as it does alone."""
    pair = tuple(reversed(pair)) if reverse else pair
    runtime, tables = _folded(request, backend_name, tmp_path, edge)
    try:
        together = runtime.query({"select": [f"measure.folded.{name}" for name in pair]}, now=NOW)
        reference = {name: _folded_reference(runtime, tables, edge, name) for name in pair}
    finally:
        if backend_name == "postgres":
            _rows(
                runtime,
                "DROP TABLE {events}; DROP TABLE {purchases}; DROP TABLE {loads}".format(**tables),
            )
        runtime.close()
    alone = {name: FOLD_ALONE[name][edge == "now"] for name in pair}
    assert reference == alone
    assert [{name: int(value) for name, value in row.items()} for row in together["rows"]] == [
        alone
    ]


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("edge,rate,unfulfilled", [("loaded", 8 / 17, 9), ("now", 15 / 28, 13)])
def test_the_fulfilment_rate_and_the_unfulfilled_amount_read_each_side_on_its_clock(
    request, backend_name, tmp_path, edge, rate, unfulfilled
):
    """ "Fulfilment rate" and "booked, not fulfilled", all time: fulfilled over booked and
    booked less fulfilled, each side on its own order clock."""
    runtime, tables = _folded(request, backend_name, tmp_path, edge)
    try:
        result = runtime.query(
            {"select": ["metric.folded.fulfilment_rate", "metric.folded.unfulfilled"]}, now=NOW
        )
    finally:
        if backend_name == "postgres":
            _rows(
                runtime,
                "DROP TABLE {events}; DROP TABLE {purchases}; DROP TABLE {loads}".format(**tables),
            )
        runtime.close()
    [row] = result["rows"]
    assert float(row["fulfilment_rate"]) == pytest.approx(rate)
    assert int(row["unfulfilled"]) == unfulfilled
