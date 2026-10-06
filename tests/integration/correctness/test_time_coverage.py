"""Time coverage and observation, checked against independent SQL on each backend."""

from dataclasses import replace

import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.semantic_rails.result_helpers import typed_rows

from .conftest import _rows, _write_variant
from .test_correctness import (
    AVERAGE,
    ORDERS,
    REFUNDS,
    REVENUE,
    ROLE,
    SIGNUP_ROLE,
    STORE,
    _ask,
    _assert_rows,
    _backend,
    _item,
)


@pytest.fixture
def raw_runtime(request, backend_name):
    backend = _backend(request, backend_name)
    opened = []

    def make(variant):
        original = backend.runtimes[variant]
        rt = Runtime.from_config(
            replace(original.config, aggregate_relations=[]), source_path=original.source_path
        )
        opened.append(rt)
        return rt

    yield make
    for rt in opened:
        rt.close()


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("bucket", ["2023-10-01", "2024-02-01", "2024-08-01", "2024-09-01"])
@pytest.mark.parametrize("variant", ["utc_authored", "utc_implicit"])
def test_loaded_buckets_use_the_whole_base_not_the_filtered_measure(
    request, backend_name, bucket, variant, raw_runtime
):
    backend = _backend(request, backend_name)
    # The measured value occurs only in November; February is loaded but has no order.
    apple = {
        "kind": "aggregate",
        **REVENUE,
        "filter": {"all": [{"field": STORE, "op": "=", "value": "b"}]},
    }
    query = _ask(
        "month",
        _item(apple, "v"),
        _item(AVERAGE, "avg"),
        start=bucket,
        end=f"{int(bucket[:4]) + 1}-01-01",
        fill=True,
    )
    result = raw_runtime(variant).query(query)
    reference = f"""
      WITH scope AS (SELECT SUM(amount) v FROM orders WHERE store_id = 'b'),
      coverage AS (SELECT date_trunc('month', MIN(ordered_at)) lo,
        date_trunc('month', MAX(CASE WHEN ordered_at <= CURRENT_TIMESTAMP THEN ordered_at END)) hi
        FROM orders),
      m AS (SELECT date_trunc('month', ordered_at) b,
        SUM(CASE WHEN store_id = 'b' THEN amount END) v, AVG(amount) a
        FROM orders GROUP BY 1)
      SELECT g.b, COALESCE(m.v, CASE WHEN scope.v IS NOT NULL
        AND g.b BETWEEN coverage.lo AND coverage.hi THEN 0 END), m.a
      FROM generate_series(TIMESTAMP '{bucket}', TIMESTAMP '{int(bucket[:4]) + 1}-01-01' - INTERVAL '1 month',
        INTERVAL '1 month') g(b) CROSS JOIN scope CROSS JOIN coverage LEFT JOIN m ON m.b=g.b
    """
    _assert_rows(
        backend.reference(reference), [tuple(r.values()) for r in typed_rows(result)], variant
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    ("measure", "condition"),
    [("large_order_count", "amount >= 10"), ("huge_order_count", "amount >= 1000")],
)
def test_conditional_observation_requires_source_rows_even_when_nothing_matches(
    request, backend_name, measure, condition
):
    backend = _backend(request, backend_name)
    query = _ask(
        "month",
        _item({"measure": f"measure.shop.{measure}"}, "v"),
        start="2024-02-01",
        end="2024-03-01",
        fill=True,
    )
    result = backend.runtimes["utc_implicit"].query(query)
    gold = backend.reference(
        f"SELECT CASE WHEN EXISTS (SELECT 1 FROM orders) THEN "
        f"COUNT(CASE WHEN {condition} THEN order_id END) END FROM orders "
        "WHERE ordered_at >= TIMESTAMP '2024-02-01' AND ordered_at < TIMESTAMP '2024-03-01'"
    )
    assert [r["v"] for r in typed_rows(result)] == [r[0] for r in gold]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_placeholder_rows_do_not_extend_coverage(request, backend_name, tmp_path):
    backend = _backend(request, backend_name)
    insert = "INSERT INTO orders (order_id, ordered_at, amount) VALUES (999, TIMESTAMP '9999-12-31', 100);"
    rt = backend.runtimes["utc_implicit"]
    if backend_name == "duckdb":
        package = _write_variant(tmp_path, "utc_implicit")
        seed = package / "data/seed.sql"
        seed.write_text(seed.read_text() + "\n" + insert)
        rt = Runtime.from_path(str(package))
    else:
        backend.reference(insert)
    try:
        result = rt.query(
            _ask(
                "month",
                _item(REVENUE, "v"),
                _item(AVERAGE, "avg"),
                start="2024-09-01",
                end="2024-10-01",
                fill=True,
            )
        )
        gold = _rows(
            rt,
            "SELECT CASE WHEN TIMESTAMP '2024-09-01' <= "
            "date_trunc('month', MAX(CASE WHEN ordered_at <= CURRENT_TIMESTAMP THEN ordered_at END)) "
            "THEN 0 END FROM orders",
        )
        assert [r["v"] for r in typed_rows(result)] == [r[0] for r in gold] == [None]
    finally:
        if backend_name == "duckdb":
            rt.close()
        else:
            backend.reference("DELETE FROM orders WHERE order_id = 999")


@pytest.fixture
def changed_runtime(request, backend_name, tmp_path):
    """A disposable seed or a transaction rolled back on the CI Postgres backend.

    ``zones`` overrides role time zones by role id; ``routed`` keeps the package's rollups.
    """
    backend = _backend(request, backend_name)
    opened = []

    def make(variant="utc_implicit", insert="", routed=False, zones=None):
        if backend_name == "duckdb":
            package = _write_variant(tmp_path, variant)
            seed = package / "data/seed.sql"
            seed.write_text(seed.read_text() + "\n" + insert + ";")
            config = load_package_config(str(package))
            source = str(package)
        else:
            config = backend.runtimes[variant].config
            source = backend.runtimes[variant].source_path
        if not routed:
            config = replace(config, aggregate_relations=[])
        roles = [
            replace(r, timezone=(zones or {}).get(r.id, r.timezone)) for r in config.temporal_roles
        ]
        rt = Runtime.from_config(replace(config, temporal_roles=roles), source_path=source)
        if backend_name == "postgres":
            _rows(rt, "BEGIN")
            _rows(rt, insert)
        opened.append(rt)
        return rt

    yield make
    for rt in opened:
        if backend_name == "postgres":
            _rows(rt, "ROLLBACK")
        rt.close()


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    ("stamp", "fill"),
    [("TIMESTAMP '2098-01-15'", True), ("NULL", False), ("TIMESTAMP '2098-01-15'", False)],
    ids=["after_coverage_edge", "null_time_key", "future_without_window"],
)
def test_populated_values_survive_coverage(changed_runtime, stamp, fill):
    rt = changed_runtime(
        insert=f"INSERT INTO orders (order_id, ordered_at, amount) VALUES (999, {stamp}, 100)"
    )
    extra = {"fill": True, "start": "2098-01-01", "end": "2098-02-01"} if fill else {}
    result = rt.query(_ask("month", _item(REVENUE, "v"), _item(ORDERS, "n"), **extra))
    gold = _rows(
        rt,
        "SELECT date_trunc('month', ordered_at), SUM(amount), COUNT(order_id) "
        "FROM orders WHERE order_id = 999 GROUP BY 1",
    )
    got = [(r[f"{ROLE}__month"], r["v"], r["n"]) for r in typed_rows(result)]
    _assert_rows(gold, [row for row in got if row[0] == gold[0][0]], "populated coverage")


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["utc_implicit", "ny_implicit"])
def test_recent_observation_uses_the_roles_current_instant(changed_runtime, variant):
    rt = changed_runtime(
        variant,
        "INSERT INTO orders (order_id, ordered_at, amount) VALUES "
        "(999, (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '1 hour', 100)",
    )
    clock = (
        "((ordered_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')"
        if variant.startswith("ny")
        else "ordered_at"
    )
    start, end = _rows(
        rt,
        f"SELECT date_trunc('day', {clock}), "
        f"date_trunc('day', {clock}) + INTERVAL '1 day' FROM orders WHERE order_id = 999",
    )[0]
    apple = {
        "kind": "aggregate",
        **REVENUE,
        "filter": {"all": [{"field": STORE, "op": "=", "value": "b"}]},
    }
    result = rt.query(
        _ask(
            "day",
            _item(REVENUE, "v"),
            _item(ORDERS, "n"),
            _item(apple, "b"),
            start=str(start),
            end=str(end),
            fill=True,
        )
    )
    gold = _rows(
        rt,
        f"SELECT date_trunc('day', {clock}), SUM(amount), COUNT(order_id), "
        "COALESCE(SUM(CASE WHEN store_id = 'b' THEN amount END), 0) "
        "FROM orders WHERE order_id = 999 GROUP BY 1",
    )
    _assert_rows(
        gold,
        [(r[f"{ROLE}__day"], r["v"], r["n"], r["b"]) for r in typed_rows(result)],
        "recent observation",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("reverse_rows", [False, True])
def test_fiscal_coverage_preserves_the_populated_final_quarter(
    request, backend_name, raw_runtime, reverse_rows
):
    backend = _backend(request, backend_name)
    result = raw_runtime("utc_authored").query(
        _ask(
            "quarter",
            _item(REVENUE, "v"),
            start="2024-05-01",
            end="2024-11-01",
            calendar_id="fiscal",
            fill=True,
        )
    )
    bucket = "date_trunc('quarter', ordered_at - INTERVAL '1 month') + INTERVAL '1 month'"
    gold = backend.reference(
        f"SELECT {bucket}, SUM(amount) FROM orders "
        "WHERE ordered_at >= TIMESTAMP '2024-05-01' GROUP BY 1"
    )
    rows = typed_rows(result)
    quarters = [r[f"{ROLE}__quarter"] for r in rows]
    assert quarters == sorted(quarters)
    # Exercise the value comparison in both result orders on each backend.
    if reverse_rows:
        rows.reverse()
    _assert_rows(gold, [(r[f"{ROLE}__quarter"], r["v"]) for r in rows], "fiscal coverage")
    assert max(rows, key=lambda row: row[f"{ROLE}__quarter"])["v"] == 2


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("expressions", [(REVENUE,), (REVENUE, ORDERS)])
def test_routed_values_survive_shorter_raw_retention(changed_runtime, expressions, monkeypatch):
    from semantic_rails.compiler_parts import sql_lowering

    rt = changed_runtime(
        insert="DELETE FROM orders WHERE ordered_at < TIMESTAMP '2024-01-01'", routed=True
    )

    def no_shadow(*args):
        pytest.fail("A routed plan must not lower or record a shadow raw leaf")

    monkeypatch.setattr(sql_lowering, "record_leaf_scope", no_shadow)
    result = rt.query(
        _ask(
            "month",
            *[_item(expr, f"v{i}") for i, expr in enumerate(expressions)],
            start="2023-11-01",
            end="2023-12-01",
        )
    )
    assert "FROM orders_monthly" in result["rendered_sql"]
    assert (
        "FROM orders\n" not in result["rendered_sql"] and "coverage_" not in result["rendered_sql"]
    )
    columns = "SUM(revenue)" + (", SUM(order_count)" if len(expressions) == 2 else "")
    gold = _rows(rt, f"SELECT {columns} FROM orders_monthly WHERE month_start = DATE '2023-11-01'")
    _assert_rows(
        gold,
        [tuple(row[f"v{i}"] for i in range(len(expressions))) for row in typed_rows(result)],
        "routed retention",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_filled_monthly_answers_match_with_and_without_rollups(request, backend_name, raw_runtime):
    backend = _backend(request, backend_name)
    query = _ask("month", _item(REVENUE, "v"), start="2023-10-01", end="2024-09-01", fill=True)
    routed = backend.runtimes["utc_authored"].query(query)
    raw = raw_runtime("utc_authored").query(query)

    def key(row):
        return str(row[f"{ROLE}__month"])

    assert sorted(routed["rows"], key=key) == sorted(raw["rows"], key=key)
    october = next(r for r in routed["rows"] if str(r[f"{ROLE}__month"]).startswith("2023-10-01"))
    assert october["v"] is None
    assert "FROM orders_monthly" not in routed["rendered_sql"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("hours", [-1, 1], ids=["past", "future"])
def test_aware_coverage_cutoff_ignores_the_session_zone(changed_runtime, backend_name, hours):
    rt = changed_runtime(
        "tz_implicit",
        "INSERT INTO orders (order_id, ordered_at_tz, amount) VALUES "
        f"(999, CURRENT_TIMESTAMP + INTERVAL '{hours} hour', NULL)",
    )
    setting = "SET GLOBAL TimeZone" if backend_name == "duckdb" else "SET TimeZone"
    _rows(rt, f"{setting} = 'America/Los_Angeles'")
    start, end = _rows(
        rt,
        "SELECT date_trunc('day', ordered_at_tz AT TIME ZONE 'UTC'), "
        "date_trunc('day', ordered_at_tz AT TIME ZONE 'UTC') + INTERVAL '1 day' "
        "FROM orders WHERE order_id = 999",
    )[0]
    # Store b's revenue has no row that day (order 999 has no store), so it reads 0 exactly
    # where the day is loaded.
    store_b = {
        "kind": "aggregate",
        **REVENUE,
        "filter": {"all": [{"field": STORE, "op": "=", "value": "b"}]},
    }
    result = rt.query(_ask("day", _item(store_b, "v"), start=str(start), end=str(end), fill=True))
    gold = _rows(
        rt,
        "SELECT CASE WHEN ordered_at_tz <= CURRENT_TIMESTAMP THEN 0 END "
        "FROM orders WHERE order_id = 999",
    )
    assert [r["v"] for r in typed_rows(result)] == [r[0] for r in gold]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_filled_query_executes_one_warehouse_statement(raw_runtime, monkeypatch):
    rt = raw_runtime("utc_implicit")
    original = runtime_module._adapter_query
    calls = []

    def query(*args, **kwargs):
        calls.append(args[1].sql)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "_adapter_query", query)
    result = rt.query(
        _ask("month", _item(REVENUE, "v"), start="2024-01-01", end="2024-03-01", fill=True)
    )
    assert len(calls) == 1 and result["row_count"] == 2


# 2024-01-31 22:00 in Los Angeles is 2024-02-01 06:00 UTC.
EDGE_ORDER = (
    "INSERT INTO orders (order_id, ordered_at_tz, amount) "
    "VALUES (999, TIMESTAMPTZ '2024-02-01 06:00:00+00', 50)"
)


def _in_los_angeles(rt, backend_name, *queries):
    """Each query's compiled SQL, run as is in a Los Angeles session."""
    setting = "SET GLOBAL TimeZone" if backend_name == "duckdb" else "SET TimeZone"
    _rows(rt, f"{setting} = 'America/Los_Angeles'")
    return [
        [(str(row[0])[:10], *row[1:]) for row in _rows(rt, compiled["sql"])]
        for compiled in (compile_query(rt.config, Registry(rt.config), q) for q in queries)
    ]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["tz_authored", "tz_implicit"])
def test_filled_and_unfilled_buckets_share_the_windows_frame(
    changed_runtime, backend_name, variant
):
    rt = changed_runtime(variant, EDGE_ORDER)
    window = {"start": "2024-01-01", "end": "2024-02-01"}
    plain, filled = _in_los_angeles(
        rt,
        backend_name,
        _ask("month", _item(REVENUE, "v"), **window),
        _ask("month", _item(REVENUE, "v"), **window, fill=True),
    )
    assert plain == filled == [("2024-01-01", 70)]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("variant", ["tz_authored", "tz_implicit"])
def test_combined_branches_align_on_the_windows_frame(changed_runtime, backend_name, variant):
    rt = changed_runtime(variant, EDGE_ORDER)
    query = _ask(
        "month", _item(REVENUE, "v"), _item(REFUNDS, "r"), start="2024-01-01", end="2024-03-01"
    )
    (rows,) = _in_los_angeles(rt, backend_name, query)
    # Order 5 (2024-03-01 02:00 UTC) is February in Los Angeles, inside the window.
    assert sorted(rows) == [("2024-01-01", 70, 1), ("2024-02-01", 8, 0)]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_a_secondary_zone_aware_role_keeps_its_populated_value(changed_runtime):
    rt = changed_runtime(
        insert="ALTER TABLE signups ALTER COLUMN signed_up_at TYPE TIMESTAMPTZ "
        "USING signed_up_at AT TIME ZONE 'UTC'; "
        "INSERT INTO signups VALUES (999, TIMESTAMPTZ '2024-03-01 02:00:00+00', 'web')",
        zones={SIGNUP_ROLE: "America/New_York"},
    )
    result = rt.query(
        _ask(
            "day",
            _item(REVENUE, "v"),
            _item({"measure": "measure.shop.signup_count"}, "n"),
            start="2024-03-01",
            end="2024-03-02",
            fill=True,
        )
    )
    rows = [(str(r[f"{ROLE}__day"])[:10], r["v"], r["n"]) for r in typed_rows(result)]
    assert rows == [("2024-03-01", 8, 1)]
