"""A conversion rate counts every base event in its denominator, and matches on the events' keys.

A session with no customer (anonymous web traffic) or no store is still a session: it counts in
the denominator and converts only if it matches an order. Events match on their own customer_id,
so a customer the customers table doesn't hold yet (late-arriving) still converts, as in plain
SQL. Before, the base side joined its customer and store lookups INNER, so those sessions
vanished and the rate went up (one anonymous session among 11: 1.0 instead of 0.909).

Each answer is checked against independent SQL on the same data: the comparison pack's answer
key (`comparisons/semantic_layers/shared/oracle/`) for its four conversion questions, and the
reference queries below for the rest.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest

from semantic_rails import config as config_module
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

PACK = Path(__file__).resolve().parents[2] / "comparisons" / "semantic_layers"
SCRIPTS = PACK / "shared" / "scripts"
PHILADELPHIA = "7f790ed7-0fc4-4de2-a1b0-cce72e657fc4"
# SES-005's customer: her first order after 06:20 on 2016-09-01 is at 07:49, at Philadelphia.
KNOWN_CUSTOMER = "7c76af9c-d5fd-460e-82d9-5a7b2a4a118b"
# A customer whose first order is at 07:30 on 2016-09-01 and whose next is at 07:36 the day after.
HISTORY_CUSTOMER = "dd3b89f6-3929-46fc-91cb-d62cee594688"


def _order(order_id: str, customer: str, at: str) -> str:
    customer_sql = "NULL" if customer == "NULL" else f"'{customer}'"
    return (
        f"INSERT INTO jaffle_order VALUES ('{order_id}', {customer_sql}, TIMESTAMP '{at}', "
        f"CAST(TIMESTAMP '{at}' AS DATE), '{PHILADELPHIA}', 1000, 0, 1000, 1, true, false, 0, 0, "
        "0, false, false, 0, 0, 0, 0, 0)"
    )


ADDED = [
    # An anonymous session, and an anonymous order 20 minutes later: no customer matches no
    # customer, so the session doesn't convert.
    f"INSERT INTO jaffle_storefront_session VALUES ('ANON-1', NULL, '{PHILADELPHIA}', "
    "TIMESTAMP '2016-09-01 10:00:00', NULL, NULL)",
    _order("anon-order-1", "NULL", "2016-09-01 10:20:00"),
    # A known customer's session with no store: it converts, but never at the same store.
    f"INSERT INTO jaffle_storefront_session VALUES ('NOSTORE-1', '{KNOWN_CUSTOMER}', NULL, "
    "TIMESTAMP '2016-09-01 06:20:00', NULL, NULL)",
    # A session whose customer the customers table doesn't hold, with no orders.
    f"INSERT INTO jaffle_storefront_session VALUES ('ORPHAN-1', 'no-such-customer', "
    f"'{PHILADELPHIA}', TIMESTAMP '2016-09-01 11:00:00', NULL, NULL)",
    # A late-arriving customer, not in the customers table yet, who orders 30 minutes after the
    # session: matched on the events' own customer_id, the session converts.
    f"INSERT INTO jaffle_storefront_session VALUES ('LATE-1', 'late-customer', "
    f"'{PHILADELPHIA}', TIMESTAMP '2016-09-01 12:00:00', NULL, NULL)",
    _order("late-order-1", "late-customer", "2016-09-01 12:30:00"),
    # HISTORY_CUSTOMER's history: a NULL segment until 08:30 on 2016-09-01, then no row at all.
    f"DELETE FROM jaffle_customer_history WHERE customer_id = '{HISTORY_CUSTOMER}'",
    f"INSERT INTO jaffle_customer_history VALUES ('{HISTORY_CUSTOMER}', NULL, NULL, "
    "TIMESTAMP '2016-01-01 00:00:00', TIMESTAMP '2016-09-01 08:30:00')",
]
# Converted sessions / all 14 sessions in 2016-09: the pack's 10, plus the four above. q15 and
# q18 also need the same store, which NOSTORE-1 lacks.
EXPECTED = {
    "q09_session_to_order_conversion_7d": Fraction(12, 14),
    "q15_same_store_session_to_order_conversion_7d": Fraction(11, 14),
    "q17_session_to_order_conversion_14d": Fraction(12, 14),
    "q18_same_store_session_to_order_conversion_50m": Fraction(9, 14),
}
RATE = "metric.jaffle.session_to_order_conversion_rate_7d"
STORE = "dimension.jaffle_store_name"
# Each session's 7-day conversion, matched on its own customer_id, by the session's own store.
STORE_RATES_SQL = """
WITH sessions AS (
  SELECT s.store_id, EXISTS (
    SELECT 1 FROM comparison_orders AS o
    WHERE o.customer_id = s.customer_id
      AND o.ordered_at >= s.started_at AND o.ordered_at < s.started_at + INTERVAL 7 DAY
  ) AS converted
  FROM comparison_storefront_sessions AS s
)
SELECT st.store_name, AVG(CASE WHEN converted THEN 1.0 ELSE 0.0 END)
FROM sessions LEFT JOIN comparison_stores AS st ON st.store_id = sessions.store_id
GROUP BY 1
"""


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"conversion_base_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[ModuleType, Path, Runtime]]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        bootstrap = _load("bootstrap", SCRIPTS / "bootstrap_shared_duckdb.py")
        validator = _load("validator", SCRIPTS / "validate_output_consistency.py")
    finally:
        sys.path.remove(str(SCRIPTS))
    runner = _load("runner", PACK / "semantic_rails" / "scripts" / "run_questions.py")
    database = tmp_path_factory.mktemp("conversion_base") / "shared.duckdb"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bootstrap, "DB_PATH", database)
        bootstrap.main()
        with duckdb.connect(str(database)) as con:
            for statement in ADDED:
                con.execute(statement)
        patch.setattr(
            config_module,
            "list_package_paths",
            lambda: {runner.PACKAGE_ID: str(runner.PACKAGE_DIR)},
        )
        resolve = Runtime._resolve_asset_path
        patch.setattr(
            Runtime,
            "_resolve_asset_path",
            lambda self, value, *, kind: (
                str(database) if kind == "default_db" else resolve(self, value, kind=kind)
            ),
        )
        runtime = opened(Runtime(runner.PACKAGE_ID))
        try:
            yield validator, database, runtime
        finally:
            runtime.close()


def _reference(database: Path, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(database), read_only=True) as con:
        return con.execute(sql).fetchall()


@pytest.mark.parametrize("question_id", sorted(EXPECTED))
def test_every_base_event_counts_in_the_conversion_rate(pack, question_id) -> None:
    validator, database, runtime = pack
    query = json.loads((PACK / "semantic_rails" / "queries" / f"{question_id}.json").read_text())
    rows = runtime.query(query)["rows"]
    with duckdb.connect(str(database), read_only=True) as con:
        cursor = con.execute((PACK / "shared" / "oracle" / f"{question_id}.sql").read_text())
        columns = [column[0] for column in cursor.description]
        key = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    maps = validator._load_column_maps()["semantic_rails"]
    expected = validator._normalize_rows(question_id, json.loads(json.dumps(key, default=str)))
    got = validator._normalize_rows(
        question_id, json.loads(json.dumps(rows, default=str)), maps[question_id]
    )
    equal, detail = validator._rows_equal(expected, got)
    assert equal, (question_id, detail)
    (row,) = rows
    rate = next(value for name, value in row.items() if name not in {"period", "period_partial"})
    assert rate == pytest.approx(float(EXPECTED[question_id]))


def test_a_session_with_no_store_groups_under_no_store(pack) -> None:
    _, database, runtime = pack
    rows = runtime.query({"select": [RATE], "group_by": [STORE]})["rows"]
    by_store = {row[STORE]: row["session_to_order_conversion_rate_7d"] for row in rows}
    # Philadelphia: the pack's 10 sessions and LATE-1 convert, ANON-1 and ORPHAN-1 don't
    # (11 of 13). No store: NOSTORE-1's customer orders 89 minutes later (1 of 1).
    assert by_store == {"Philadelphia": pytest.approx(11 / 13), None: pytest.approx(1.0)}
    assert by_store == {
        name: pytest.approx(rate) for name, rate in _reference(database, STORE_RATES_SQL)
    }


@pytest.mark.parametrize(("threshold", "qualifies"), [(0.9, False), (0.8, True)])
def test_a_rate_filtered_on_counts_the_events_the_rate_shows(pack, threshold, qualifies) -> None:
    """The stores whose conversion rate passes a threshold, read inside a metric filter, are the
    stores whose shown rate passes it: Philadelphia's 11/13 = 0.846 fails 0.9 and passes 0.8."""
    _, database, runtime = pack
    shown = {
        name: rate
        for name, rate in _reference(database, STORE_RATES_SQL)
        if name is not None and rate >= threshold
    }
    assert ("Philadelphia" in shown) is qualifies
    rows = runtime.query(
        {
            "select": ["measure.jaffle.order_count"],
            "group_by": [STORE],
            "where": [
                {
                    "entity": "entity.jaffle_store",
                    "having": [{"field": RATE, "op": "gte", "value": threshold}],
                    "over": "all_time",
                }
            ],
        }
    )["rows"]
    orders = dict(
        _reference(
            database,
            "SELECT st.store_name, COUNT(*) FROM comparison_orders AS o "
            "JOIN comparison_stores AS st ON st.store_id = o.store_id GROUP BY 1",
        )
    )
    assert {row[STORE]: row["order_count"] for row in rows} == {
        name: orders[name] for name in shown
    }


def test_a_missing_history_row_on_either_side_pairs_with_nothing(pack) -> None:
    """First order to repeat order within 28 days, holding the customer's segment constant. The
    first order's history row has a NULL segment (a matched NULL); the repeat order falls in the
    history gap, so it has no segment at all and must not pair as "the same" NULL."""
    _, database, runtime = pack

    def operand(first: bool) -> dict[str, Any]:
        new = {"field": "dimension.jaffle_order_is_new_customer_order", "op": "is", "value": first}
        return {
            "kind": "aggregate",
            "measure": "measure.jaffle.order_count",
            "filter": {"all": [new]},
        }

    segment = """(SELECT h.customer_segment FROM comparison_customer_history AS h
      WHERE h.customer_id = {o}.customer_id AND {o}.ordered_at >= h.valid_from
        AND (h.valid_to IS NULL OR {o}.ordered_at < h.valid_to))"""
    found = """EXISTS (SELECT 1 FROM comparison_customer_history AS h
      WHERE h.customer_id = {o}.customer_id AND {o}.ordered_at >= h.valid_from
        AND (h.valid_to IS NULL OR {o}.ordered_at < h.valid_to))"""
    reference = f"""
    SELECT AVG(CASE WHEN {found.format(o="b")} AND EXISTS (
      SELECT 1 FROM comparison_orders AS c
      WHERE c.customer_id = b.customer_id AND NOT c.is_new_customer_order
        AND c.ordered_at >= b.ordered_at AND c.ordered_at < b.ordered_at + INTERVAL 28 DAY
        AND {found.format(o="c")}
        AND {segment.format(o="c")} IS NOT DISTINCT FROM {segment.format(o="b")}
    ) THEN 1.0 ELSE 0.0 END)
    FROM comparison_orders AS b
    WHERE b.customer_id = '{HISTORY_CUSTOMER}' AND b.is_new_customer_order
      AND CAST(b.ordered_at AS DATE) = DATE '2016-09-01'
    """
    [(expected,)] = _reference(database, reference)
    assert expected == 0.0
    for mode in ("first_converted_after_base", "closest_converted_after_base"):
        rows = runtime.query(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "conversion",
                            "entity": "entity.jaffle_customer",
                            "window": {"unit": "day", "value": 28},
                            "matching_mode": mode,
                            "base": operand(True),
                            "converted": operand(False),
                            "constant_properties": ["dimension.jaffle_customer_history_segment"],
                        },
                        "as": "rate",
                    }
                ],
                "where": [
                    {
                        "field": "dimension.jaffle_order_customer_id",
                        "op": "is",
                        "value": HISTORY_CUSTOMER,
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            }
        )["rows"]
        [rate] = [row["rate"] for row in rows if str(row["period"]).startswith("2016-09-01")]
        assert rate == pytest.approx(expected), mode


def test_each_side_matches_on_its_own_key_column(pack) -> None:
    _, _, runtime = pack
    query = json.loads(
        (
            PACK / "semantic_rails" / "queries" / "q17_session_to_order_conversion_14d.json"
        ).read_text()
    )
    sql = runtime.query(query)["rendered_sql"]
    assert "base_events.__match_key_1 = converted_events.__match_key_1" in sql
    assert "IS NOT DISTINCT FROM converted_events.__match_key_1" not in sql
    # Neither side reads the key through the customers table.
    assert "comparison_storefront_sessions.customer_id AS __match_key_1" in sql
    assert "comparison_orders.customer_id AS __match_key_1" in sql
    assert "JOIN comparison_customers" not in sql


def test_a_warehouse_whose_outer_join_reads_a_default_refuses_base_lookups() -> None:
    """ClickHouse's outer join reads '' or 0 for a missing row, so a base event's lookup can't
    keep the event: such a conversion is refused, never answered with a different number. One
    that reads only the events' own columns still compiles."""
    config = load_package_config(str(PACK / "semantic_rails" / "package"))
    config = dataclasses.replace(
        config, package=dataclasses.replace(config.package, warehouse="clickhouse")
    )
    month = {"time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "month"}}

    def compile_call(call: dict[str, Any]) -> dict[str, Any]:
        return compile_query(config, Registry(config), read_call(call, config=config)[0])

    compile_call({"select": [RATE], **month})
    for query in (
        {"select": [RATE], "group_by": [STORE], **month},
        {"select": ["metric.jaffle.session_to_order_conversion_rate_7d_same_store"], **month},
    ):
        with pytest.raises(SemanticLayerError) as refused:
            compile_call(query)
        assert refused.value.code == "CONVERSION_NOT_SUPPORTED"
        assert refused.value.details["lookup_entities"] == ["entity.jaffle_store"]
