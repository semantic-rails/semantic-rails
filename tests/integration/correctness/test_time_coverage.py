"""Time coverage and observation, checked against independent SQL on each backend."""

from dataclasses import replace

import pytest

from semantic_rails.runtime import Runtime

from .conftest import _rows, _write_variant
from .test_correctness import (
    AVERAGE,
    REVENUE,
    ROLE,
    STORE,
    _ask,
    _assert_rows,
    _backend,
    _item,
)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("bucket", ["2023-10-01", "2024-02-01", "2024-08-01", "2024-09-01"])
@pytest.mark.parametrize("variant", ["utc_authored", "utc_implicit"])
def test_loaded_buckets_use_the_whole_base_not_the_filtered_measure(
    request, backend_name, bucket, variant
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
    result = backend.runtimes[variant].query(query)
    reference = f"""
      WITH scope AS (SELECT SUM(amount) v FROM orders WHERE store_id = 'b'),
      coverage AS (SELECT date_trunc('month', MIN(ordered_at)) lo,
        date_trunc('month', MAX(CASE WHEN ordered_at <= CURRENT_TIMESTAMP THEN ordered_at END)) hi
        FROM orders),
      m AS (SELECT date_trunc('month', ordered_at) b,
        SUM(CASE WHEN store_id = 'b' THEN amount END) v, AVG(amount) a
        FROM orders GROUP BY 1)
      SELECT g.b, CASE WHEN scope.v IS NOT NULL AND g.b BETWEEN coverage.lo AND coverage.hi
        THEN COALESCE(m.v, 0) END, m.a
      FROM generate_series(TIMESTAMP '{bucket}', TIMESTAMP '{int(bucket[:4]) + 1}-01-01' - INTERVAL '1 month',
        INTERVAL '1 month') g(b) CROSS JOIN scope CROSS JOIN coverage LEFT JOIN m ON m.b=g.b
    """
    _assert_rows(backend.reference(reference), [tuple(r.values()) for r in result["rows"]], variant)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    ("measure", "condition"),
    [("large_order_count", "amount >= 10"), ("huge_order_count", "amount >= 1000")],
)
def test_conditional_observation_does_not_count_rows_that_fail_the_condition(
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
        f"SELECT CASE WHEN EXISTS (SELECT 1 FROM orders WHERE {condition}) THEN 0 END"
    )
    assert [r["v"] for r in result["rows"]] == [r[0] for r in gold]


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
        assert [r["v"] for r in result["rows"]] == [r[0] for r in gold] == [None]
    finally:
        if backend_name == "duckdb":
            rt.close()
        else:
            backend.reference("DELETE FROM orders WHERE order_id = 999")


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_freshness_extends_coverage_and_reports_the_partial_edge(request, backend_name):
    backend = _backend(request, backend_name)
    original = backend.runtimes["utc_implicit"]
    config = replace(
        original.config,
        entities=[
            replace(e, freshness_as_of="2024-10-15T12:00:00") if e.id == "entity.shop_order" else e
            for e in original.config.entities
        ],
    )
    rt = Runtime.from_config(config, source_path=original.source_path)
    try:
        result = rt.query(
            _ask(
                "month",
                _item(REVENUE, "v"),
                _item(AVERAGE, "avg"),
                start="2024-09-01",
                end="2024-12-01",
                fill=True,
            )
        )
        gold = backend.reference(
            "SELECT TIMESTAMP '2024-09-01', 0 UNION ALL SELECT TIMESTAMP '2024-10-01', 0 "
            "UNION ALL SELECT TIMESTAMP '2024-11-01', NULL"
        )
        _assert_rows(gold, [(r[f"{ROLE}__month"], r["v"]) for r in result["rows"]], "freshness")
        warning = [w for w in result["warnings"] if w["code"] == "PARTIAL_BUCKET"]
        assert len(warning) == 1 and warning[0]["details"]["buckets"] == ["2024-10-01 00:00:00"]
    finally:
        rt.close()


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_outside_coverage_does_not_report_an_observed_measure_as_unobserved(request, backend_name):
    backend = _backend(request, backend_name)
    result = backend.runtimes["utc_implicit"].query(
        _ask("month", _item(REVENUE, "v"), start="2024-09-01", end="2024-10-01", fill=True)
    )
    assert [r["v"] for r in result["rows"]] == [None]
    assert not [w for w in result["warnings"] if w["code"] == "NO_DATA_IN_SCOPE"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    ("variant", "stamp", "grain", "calendar", "start", "end", "edge"),
    [
        (
            "utc_implicit",
            "2024-11-01T00:30:00+02:00",
            "month",
            None,
            "2024-10-01",
            "2024-12-01",
            "2024-10-01",
        ),
        (
            "ny_implicit",
            "2024-11-01T02:00:00Z",
            "month",
            None,
            "2024-10-01",
            "2024-12-01",
            "2024-10-01",
        ),
        (
            "utc_authored",
            "2024-05-15T00:00:00Z",
            "quarter",
            "fiscal",
            "2024-05-01",
            "2024-11-01",
            "2024-05-01",
        ),
    ],
)
def test_freshness_uses_the_role_zone_and_calendar(
    request, backend_name, variant, stamp, grain, calendar, start, end, edge
):
    backend = _backend(request, backend_name)
    original = backend.runtimes[variant]
    config = replace(
        original.config,
        entities=[
            replace(e, freshness_as_of=stamp) if e.id == "entity.shop_order" else e
            for e in original.config.entities
        ],
    )
    rt = Runtime.from_config(config, source_path=original.source_path)
    try:
        extra = {"calendar_id": calendar} if calendar else {}
        result = rt.query(
            _ask(
                grain,
                _item(REVENUE, "v"),
                _item(AVERAGE, "avg"),
                start=start,
                end=end,
                fill=True,
                **extra,
            )
        )
        zone = "America/New_York" if variant.startswith("ny") else "UTC"
        freshness = f"(TIMESTAMPTZ '{stamp}' AT TIME ZONE '{zone}')"
        clock = (
            "((ordered_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')"
            if zone != "UTC"
            else "ordered_at"
        )
        bucket = f"date_trunc('{grain}', {clock})"
        edge_bucket = f"date_trunc('{grain}', {freshness})"
        if calendar:
            bucket = f"date_trunc('quarter', {clock} - INTERVAL '1 month') + INTERVAL '1 month'"
            edge_bucket = (
                f"date_trunc('quarter', {freshness} - INTERVAL '1 month') + INTERVAL '1 month'"
            )
        step = "3 months" if grain == "quarter" else "1 month"
        gold = backend.reference(
            f"SELECT g.b, CASE WHEN g.b <= {edge_bucket} THEN COALESCE("
            f"(SELECT SUM(amount) FROM orders WHERE {bucket} = g.b), 0) END FROM "
            f"generate_series(TIMESTAMP '{start}', TIMESTAMP '{end}' - INTERVAL '{step}', INTERVAL '{step}') g(b)"
        )
        _assert_rows(
            gold, [(r[f"{ROLE}__{grain}"], r["v"]) for r in result["rows"]], "freshness frame"
        )
        warning = [w for w in result["warnings"] if w["code"] == "PARTIAL_BUCKET"]
        assert [b[:10] for b in warning[0]["details"]["buckets"]] == [edge]
    finally:
        rt.close()


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_future_authored_freshness_is_capped_at_now(request, backend_name):
    backend = _backend(request, backend_name)
    original = backend.runtimes["utc_implicit"]
    config = replace(
        original.config,
        entities=[
            replace(e, freshness_as_of="9999-12-31T00:00:00Z") if e.id == "entity.shop_order" else e
            for e in original.config.entities
        ],
    )
    rt = Runtime.from_config(config, source_path=original.source_path)
    try:
        result = rt.query(
            _ask(
                "month",
                _item(REVENUE, "v"),
                _item(AVERAGE, "avg"),
                start="2098-01-01",
                end="2098-02-01",
                fill=True,
            )
        )
        gold = backend.reference(
            "SELECT CASE WHEN TIMESTAMP '2098-01-01' <= "
            "date_trunc('month', CURRENT_TIMESTAMP) THEN 0 END"
        )
        assert [r["v"] for r in result["rows"]] == [r[0] for r in gold]
    finally:
        rt.close()
