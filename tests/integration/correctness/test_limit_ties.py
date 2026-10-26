"""Top-N subsets and warnings checked against independent reference SQL."""

from copy import deepcopy

import pytest

from .answer_ledger import comparable, encode, load_entries

SPEC = dict(load_entries())["shop/top-stores-with-cutoff-ties"]


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_top_n_cutoff_ties_match_reference_on_repeated_calls(request, backend, monkeypatch):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    adapter = runtime._get_adapter()
    execute = adapter.query_prepared
    statements = []

    def capture(prepared, *, limits=None):
        statements.append(prepared.sql)
        return execute(prepared, limits=limits)

    monkeypatch.setattr(adapter, "query_prepared", capture)
    expected = comparable(encode(runtime, SPEC, source.reference), ordered=True)
    for _ in range(3):
        result = runtime.query(SPEC["query"])
        assert comparable(result, ordered=True) == expected
        assert result["row_count"] == 1
        assert not result["truncated"]
        warnings = [
            warning for warning in result.get("warnings", []) if warning["code"] == "TIES_AT_LIMIT"
        ]
        assert [warning["details"] for warning in warnings] == [{"at": 1, "tied": 2}]
    # One answer query per call, including cache hits; beside it, the window total's loaded
    # coverage is read once per call for its data-through date.
    answers = [sql for sql in statements if "guarded_base" in sql]
    assert len(answers) == 3
    assert all("LIMIT 2" in sql for sql in answers)
    assert len(statements) == 6


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 2},
        {"limit": None},
        {"order_by": []},
        {
            "order_by": [
                {"field": "orders", "direction": "desc"},
                {"field": "dimension.shop_order_store_id", "direction": "desc"},
            ]
        },
        {
            "time": {
                "temporal_role": "temporal_role.shop_order_ordered_at",
                "start": "2024-01-01",
                "end": "2024-02-01",
            }
        },
    ],
)
def test_no_cutoff_warning_without_a_boundary_tie(duckdb_backend, changes):
    runtime = duckdb_backend.runtimes["utc_authored"]
    # None removes the key: a call without a limit.
    query = {
        key: value
        for key, value in {**deepcopy(SPEC["query"]), **changes}.items()
        if value is not None
    }
    result = runtime.query(query)
    assert not any(warning["code"] == "TIES_AT_LIMIT" for warning in result.get("warnings", []))
    if query.get("limit") is None:
        # Only the requested terms: an unlimited query gets no tie-break columns.
        assert result["rendered_sql"].count("NULLS LAST") == len(query["order_by"])


def test_ties_break_by_group_by_then_period_never_by_a_measure(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    spec = dict(load_entries())["shop/top-store-months-break-ties-by-store-then-period"]
    result = runtime.query(spec["query"])
    assert [
        (row["dimension.shop_order_store_id"], row["period"], row["orders"])
        for row in result["rows"]
    ] == [("a", "2023-11-01", 1), ("a", "2023-12-01", 1), ("a", "2024-01-01", 1)]
    order_by = result["rendered_sql"].rsplit("ORDER BY", 1)[1]
    assert order_by.index("dimension.shop_order_store_id") < order_by.index("period")
    assert [w["details"] for w in result.get("warnings", []) if w["code"] == "TIES_AT_LIMIT"] == [
        {"at": 3, "tied": 4}
    ]


@pytest.mark.parametrize(
    "cap,expected_count,truncated", [(1, 1, True), (2, 2, False), (3, 2, False)]
)
def test_row_cap_is_preserved(duckdb_backend, cap, expected_count, truncated):
    runtime = duckdb_backend.runtimes["utc_authored"]
    result = runtime.query({**SPEC["query"], "limit": 2}, limits={"max_rows": cap})
    assert result["row_count"] == expected_count
    assert result["truncated"] is truncated
    assert not any(warning["code"] == "TIES_AT_LIMIT" for warning in result.get("warnings", []))


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_larger_tie_group_uses_nulls_last_and_an_observed_count(duckdb_backend, limit):
    runtime = duckdb_backend.runtimes["utc_authored"]
    measure = "measure.shop.order_count"
    query = {
        "select": [
            {
                "as": "score",
                "expression": {
                    "kind": "arithmetic",
                    "op": "subtract",
                    "left": measure,
                    "right": measure,
                },
            }
        ],
        "group_by": ["dimension.shop_order_store_id"],
        "order_by": [{"field": "score", "direction": "desc"}],
        "limit": limit,
    }
    reference = duckdb_backend.reference(
        "SELECT store_id, COUNT(order_id) - COUNT(order_id) AS score FROM orders "
        f"GROUP BY store_id ORDER BY score DESC, store_id ASC NULLS LAST LIMIT {limit}"
    )
    result = runtime.query(query)
    assert [
        (row["dimension.shop_order_store_id"], row["score"]) for row in result["rows"]
    ] == reference
    assert [row["dimension.shop_order_store_id"] for row in result["rows"]] == ["a", "b", None][
        :limit
    ]
    warnings = [
        warning for warning in result.get("warnings", []) if warning["code"] == "TIES_AT_LIMIT"
    ]
    if limit < 3:
        assert [warning["details"] for warning in warnings] == [{"at": limit, "tied": limit + 1}]
    else:
        assert not warnings


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_limit_without_order_by_keeps_the_first_groups_by_the_tie_break(request, backend):
    """With no order_by, a limit keeps the first rows by the group_by columns, then the period,
    ascending with NULLs last: the same call keeps the same rows."""
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    query = {
        "select": [{"expression": "measure.shop.order_count", "as": "orders"}],
        "group_by": ["dimension.shop_order_store_id"],
        "limit": 2,
    }
    expected = source.reference(
        "SELECT store_id, COUNT(order_id) FROM orders GROUP BY store_id "
        "ORDER BY store_id ASC NULLS LAST LIMIT 2"
    )
    for _ in range(2):
        result = runtime.query(query)
        rows = [(row["dimension.shop_order_store_id"], row["orders"]) for row in result["rows"]]
        assert rows == [tuple(row) for row in expected] == [("a", 6), ("b", 4)]
        assert not [w for w in result.get("warnings", []) if w["code"] == "TIES_AT_LIMIT"]
    order_by = " ".join(result["rendered_sql"].rsplit("ORDER BY", 1)[1].split())
    assert order_by == '"dimension.shop_order_store_id" ASC NULLS LAST LIMIT 2'


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_time_bucketed_limit_without_order_by_keeps_the_series_order(request, backend):
    """With a time grain and no order_by, a limit keeps the first rows of the series in its own
    order, period first and then the group_by columns: November's two stores."""
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    query = {
        "select": [{"expression": "measure.shop.order_count", "as": "orders"}],
        "group_by": ["dimension.shop_order_store_id"],
        "time": {"temporal_role": "temporal_role.shop_order_ordered_at", "grain": "month"},
        "limit": 2,
    }
    expected = source.reference(
        "SELECT CAST(date_trunc('month', ordered_at) AS DATE), store_id, COUNT(order_id) "
        "FROM orders GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 2"
    )
    # The monthly aggregate table answers it, and Postgres sums its bigint counts as NUMERIC:
    # a count is still an integer, never decimal text.
    for _ in range(2):
        result = runtime.query(query)
        rows = [
            (row["period"], row["dimension.shop_order_store_id"], row["orders"])
            for row in result["rows"]
        ]
        assert rows == [(str(day), store, count) for day, store, count in expected]
        assert result["column_types"]["orders"] == {"type": "integer"}
    assert rows == [("2023-11-01", "a", 1), ("2023-11-01", "b", 1)]
