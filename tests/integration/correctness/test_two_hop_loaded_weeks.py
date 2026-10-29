"""Order sums grouped two hops away (orders -> customers -> regions) read their empty weeks as
the same sums grouped one hop away (the customer's tier) do, on DuckDB and Postgres.

Weekly revenue by customer region adds up each customer's orders before the hops. Only the
orders table's own load log (``zero_through``) says how far orders are loaded: a week it covers
with no order reads 0, a week before the first order or past the log reads NULL. A log on the
customers table covers signups, not orders, so the orders read as a table with no declared
edge: 0 from the first order through the last one. Refunds follow the same rule: under the
orders' log, a loaded week (or all time) with no refund reads 0 and a refund rate of 0; with no
log on orders and no refund anywhere, NULL. An order whose customer the customers table
doesn't hold yet reads in the NULL region, never dropped.

Each number below is the rule worked by hand over the rows in ``ORDERS``.
"""

import pytest

from .conftest import _rows
from .test_zero_through import BACKENDS, _package, _warnings

NOW = "2024-10-07T06:00:00Z"
REGION = "dimension.hops_region_region_name"
TIER = "dimension.hops_customer_tier"
WEEKS = {"start": "2024-08-26", "end": "2024-10-07", "grain": "week"}
REVENUE, REFUNDS, RATE = "measure.hops.revenue", "metric.hops.refunds", "metric.hops.refund_rate"
# (order, customer, ordered at, status, amount, refunded). Customers 11 (east, vip), 12 (west,
# regular) and 13 (west, vip). The load log runs through Sunday 2024-09-29; order 6 is after it.
ORDERS = [
    (1, 11, "2024-09-03 10:00:00", "paid", 100, None),
    (2, 12, "2024-09-04 12:00:00", "paid", 60, None),
    (3, 11, "2024-09-10 09:00:00", "paid", 80, None),
    (4, 13, "2024-09-24 15:00:00", "paid", 40, None),
    (5, 12, "2024-09-25 18:00:00", "paid", 20, None),
    (6, 11, "2024-10-01 08:00:00", "paid", 999, None),
]
REFUNDED = [(*row[:3], "refunded", row[4], 30) if row[0] == 3 else row for row in ORDERS]
# Customer 14 isn't in the customers table yet.
LAGGING = [*ORDERS, (7, 14, "2024-09-18 11:00:00", "paid", 50, None)]
LOG = {"loaded": "loads.through_day"}


def _hops(request, backend_name, tmp_path, edge, orders=ORDERS):
    """The orders, customers and regions, with the load log declared on ``edge``'s table:
    "orders", "customers", "both" or "none"."""
    times = {"column": "ordered_at", "kind": "timestamp", "class": "event_time", "default": True}
    signed_up = {"column": "signed_up_at", "kind": "timestamp", "class": "event_time"}
    models = {
        "orders": {
            "id": "orders",
            "relation": "orders",
            "entities": {"order": {}, "customer": {}},
            **({"zero_through": LOG} if edge in ("orders", "both") else {}),
            "times": {"ordered_at": times},
            "dimensions": {"status": {"kind": "categorical"}},
            "measures": {
                "revenue": {"kind": "aggregate", "expr": "amount"},
                "refunded": {"kind": "aggregate", "expr": "refund_amount"},
            },
        },
        "customers": {
            "id": "customers",
            "relation": "customers",
            "entities": {"customer": {}, "region": {}},
            **({"zero_through": LOG} if edge in ("customers", "both") else {}),
            "times": {"signed_up_at": {**signed_up, "default": True}},
            "dimensions": {"tier": {"kind": "categorical"}},
        },
        "regions": {
            "id": "regions",
            "relation": "regions",
            "entities": {"region": {}},
            "dimensions": {"region_name": {"kind": "categorical"}},
        },
        "loads": {
            "id": "loads",
            "relation": "loads",
            "entities": {"load": {}},
            "times": {"through_day": {"column": "through_day", "kind": "date"}},
        },
    }
    entities = {
        "order": {"key": ["order_id"], "model": "orders"},
        "customer": {"key": ["customer_id"], "model": "customers"},
        "region": {"key": ["region_id"], "model": "regions"},
        "load": {"key": ["run_id"], "model": "loads"},
    }
    role = "temporal_role.hops_order_ordered_at"
    metrics = {
        "refunds": {
            "as": REFUNDS,
            "label": "Refunds",
            "kind": "derived",
            "value_type": "currency",
            "temporal_role": role,
            "expression": {
                "kind": "aggregate",
                "measure": "measure.hops.refunded",
                "aggregation": "sum",
                "filter": {
                    "all": [{"field": "dimension.hops_order_status", "op": "is", "value": "refunded"}]
                },
            },
        },
        "refund_rate": {
            "as": RATE,
            "label": "Refund rate",
            "kind": "ratio",
            "value_type": "number",
            "numerator": "refunds",
            "denominator": "revenue",
            "temporal_role": role,
        },
    }
    values = ", ".join(
        f"({o}, {c}, TIMESTAMP '{at}', '{status}', {amount}, {'NULL' if r is None else r})"
        for o, c, at, status, amount, r in orders
    )
    seed = (
        "CREATE TABLE {orders} (order_id INTEGER, customer_id INTEGER, ordered_at TIMESTAMP, "
        "status VARCHAR(20), amount INTEGER, refund_amount INTEGER);"
        f"INSERT INTO {{orders}} VALUES {values};"
        "CREATE TABLE {customers} (customer_id INTEGER, tier VARCHAR(20), region_id INTEGER, "
        "signed_up_at TIMESTAMP);"
        "INSERT INTO {customers} VALUES (11, 'vip', 1, TIMESTAMP '2024-08-01 09:00:00'), "
        "(12, 'regular', 2, TIMESTAMP '2024-08-15 09:00:00'), "
        "(13, 'vip', 2, TIMESTAMP '2024-09-20 09:00:00');"
        "CREATE TABLE {regions} (region_id INTEGER, region_name VARCHAR(20));"
        "INSERT INTO {regions} VALUES (1, 'east'), (2, 'west');"
        "CREATE TABLE {loads} (run_id INTEGER, through_day DATE);"
        "INSERT INTO {loads} VALUES (1, DATE '2024-09-15'), (2, DATE '2024-09-29');"
    )
    names = ("orders", "customers", "regions", "loads")
    return _package(
        request, backend_name, tmp_path, "hops", models, entities, seed, metrics, names=names
    )


def _ask(request, backend_name, tmp_path, edge, calls, orders=ORDERS):
    """Each of ``calls`` answered on one package."""
    runtime, tables = _hops(request, backend_name, tmp_path, edge, orders)
    try:
        return [runtime.query(call, now=NOW) for call in calls]
    finally:
        if backend_name == "postgres":
            _rows(runtime, "; ".join(f"DROP TABLE {table}" for table in tables.values()))
        runtime.close()


def _weekly(result, group, *columns):
    """{week: {group: value, ...}} (one column) or {week: {group: (values), ...}}."""
    weeks = {}
    for row in result["rows"]:
        values = tuple(None if row[c] is None else float(row[c]) for c in columns)
        weeks.setdefault(str(row["period"])[5:10], {})[row[group]] = (
            values[0] if len(columns) == 1 else values
        )
    return weeks


def _empty_weeks(weeks):
    """The weeks every group reads NULL in."""
    return sorted(w for w, groups in weeks.items() if all(v is None for v in groups.values()))


# Revenue by region, east and west, per week.
LOGGED = {
    "08-26": {"east": None, "west": None},  # before the first order
    "09-02": {"east": 100, "west": 60},
    "09-09": {"east": 80, "west": 0},
    "09-16": {"east": 0, "west": 0},  # a loaded week with no order
    "09-23": {"east": 0, "west": 60},
    "09-30": {"east": None, "west": None},  # past the log: order 6 is not read
}
UNLOGGED = {**LOGGED, "09-30": {"east": 999, "west": 0}}  # through the last order


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "edge,weeks",
    [("orders", LOGGED), ("both", LOGGED), ("customers", UNLOGGED), ("none", UNLOGGED)],
)
def test_weekly_revenue_by_region_reads_null_where_the_orders_are_not_loaded(
    request, backend_name, tmp_path, edge, weeks
):
    """Weekly revenue by customer region: 0 in a week the orders cover with no order, NULL
    before the first order and past the orders' log, as revenue by customer tier reads."""
    by_region, by_tier = _ask(
        request,
        backend_name,
        tmp_path,
        edge,
        [
            {"select": [REVENUE], "group_by": [REGION], "time": WEEKS},
            {"select": [REVENUE], "group_by": [TIER], "time": WEEKS},
        ],
    )
    assert _weekly(by_region, REGION, "revenue") == weeks
    assert _empty_weeks(_weekly(by_tier, TIER, "revenue")) == _empty_weeks(weeks)
    assert bool(_warnings(by_region, "NO_DATA_YET")) == bool(_warnings(by_tier, "NO_DATA_YET"))


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_refund_rate_by_region_reads_0_in_loaded_weeks_before_the_first_refund(
    request, backend_name, tmp_path
):
    """No order is refunded yet. Under the orders' log a loaded week holds data of nothing:
    refunds 0, a refund rate of 0 where there is revenue. Without it (the log on customers
    only), no refund row anywhere is no data: NULL, with NO_DATA_IN_SCOPE."""
    call = {"select": [REFUNDS, REVENUE, RATE], "group_by": [REGION], "time": WEEKS}
    (logged,) = _ask(request, backend_name, tmp_path, "orders", [call])
    assert _weekly(logged, REGION, "refunds", "refund_rate") == {
        "08-26": {"east": (None, None), "west": (None, None)},
        "09-02": {"east": (0, 0), "west": (0, 0)},
        "09-09": {"east": (0, 0), "west": (0, None)},
        "09-16": {"east": (0, None), "west": (0, None)},
        "09-23": {"east": (0, None), "west": (0, 0)},
        "09-30": {"east": (None, None), "west": (None, None)},
    }
    assert not _warnings(logged, "NO_DATA_IN_SCOPE")

    (unlogged,) = _ask(request, backend_name, tmp_path / "customers", "customers", [call])
    rows = unlogged["rows"]
    assert {(row["refunds"], row["refund_rate"]) for row in rows} == {(None, None)}
    assert _warnings(unlogged, "NO_DATA_IN_SCOPE")


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_refund_reads_in_its_week_and_the_weeks_past_the_log_read_null(
    request, backend_name, tmp_path
):
    """Order 3 (east, week of 2024-09-09) refunded 30 of 80."""
    call = {"select": [REFUNDS, REVENUE, RATE], "group_by": [REGION], "time": WEEKS}
    (result,) = _ask(request, backend_name, tmp_path, "orders", [call], REFUNDED)
    weeks = _weekly(result, REGION, "refunds", "refund_rate")
    assert weeks["09-09"] == {"east": (30, 0.375), "west": (0, None)}
    assert weeks["08-26"] == weeks["09-30"] == {"east": (None, None), "west": (None, None)}


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "edge,regions",
    [
        # Order 6 is past the orders' log: east reads 100 + 80.
        ("orders", {"east": (0, 180, 0), "west": (0, 120, 0)}),
        # The customers' log doesn't cover orders: every order is read, and no refund row is
        # no data.
        ("customers", {"east": (None, 1179, None), "west": (None, 120, None)}),
    ],
)
def test_refunds_by_region_over_all_time(request, backend_name, tmp_path, edge, regions):
    """Refunds by customer region with no time window, before any refund: 0 under the orders'
    log, as refunds by customer tier read; NULL with NO_DATA_IN_SCOPE without it."""
    (result,) = _ask(
        request,
        backend_name,
        tmp_path,
        edge,
        [{"select": [REFUNDS, REVENUE, RATE], "group_by": [REGION]}],
    )
    assert {
        row[REGION]: tuple(None if row[c] is None else float(row[c]) for c in ("refunds", "revenue", "refund_rate"))
        for row in result["rows"]
    } == regions  # fmt: skip
    assert bool(_warnings(result, "NO_DATA_IN_SCOPE")) == (edge == "customers")


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("edge", ["orders", "customers"])
def test_an_order_whose_customer_is_not_loaded_yet_reads_in_the_null_region(
    request, backend_name, tmp_path, edge
):
    """Order 7 (2024-09-18, 50) is from customer 14, whom the customers table doesn't hold:
    it reads under no region, and the region's empty weeks read as the others' do. Which
    table declares the log decides the weeks, not the order's customer."""
    (result,) = _ask(
        request,
        backend_name,
        tmp_path,
        edge,
        [{"select": [REVENUE], "group_by": [REGION], "time": WEEKS}],
        LAGGING,
    )
    weeks = _weekly(result, REGION, "revenue")
    unknown = {week: groups[None] for week, groups in weeks.items()}
    last = None if edge == "orders" else 0
    assert unknown == {
        "08-26": None,
        "09-02": 0,
        "09-09": 0,
        "09-16": 50,
        "09-23": 0,
        "09-30": last,
    }
    assert weeks["09-30"]["east"] == (None if edge == "orders" else 999)
