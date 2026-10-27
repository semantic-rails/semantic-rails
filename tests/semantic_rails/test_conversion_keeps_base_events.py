"""A conversion rate counts every base event in its denominator.

A session with no customer (anonymous web traffic), a customer the customers table doesn't
hold, or no store is still a session: it counts in the denominator and converts only if it
matches an order. Before, the base side joined its customer and store lookups INNER, so those
sessions vanished and the rate went up (one anonymous session among 11: 1.0 instead of 0.909).
Each answer is checked against the comparison pack's independent answer key
(`comparisons/semantic_layers/shared/oracle/`), run on the same data.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

from semantic_rails import config as config_module
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

PACK = Path(__file__).resolve().parents[2] / "comparisons" / "semantic_layers"
SCRIPTS = PACK / "shared" / "scripts"
PHILADELPHIA = "7f790ed7-0fc4-4de2-a1b0-cce72e657fc4"
# SES-005's customer: her first order after 06:20 on 2016-09-01 is at 07:49, at Philadelphia.
KNOWN_CUSTOMER = "7c76af9c-d5fd-460e-82d9-5a7b2a4a118b"
ADDED = [
    # An anonymous session, and an anonymous order 20 minutes later: no customer matches no
    # customer, so the session doesn't convert.
    f"INSERT INTO jaffle_storefront_session VALUES ('ANON-1', NULL, '{PHILADELPHIA}', "
    "TIMESTAMP '2016-09-01 10:00:00', NULL, NULL)",
    f"INSERT INTO jaffle_order VALUES ('anon-order-1', NULL, TIMESTAMP '2016-09-01 10:20:00', "
    f"DATE '2016-09-01', '{PHILADELPHIA}', 1000, 0, 1000, 1, false, false, 0, 0, 0, false, "
    "false, 0, 0, 0, 0, 0)",
    # A known customer's session with no store: it converts, but never at the same store.
    f"INSERT INTO jaffle_storefront_session VALUES ('NOSTORE-1', '{KNOWN_CUSTOMER}', NULL, "
    "TIMESTAMP '2016-09-01 06:20:00', NULL, NULL)",
    # A session whose customer the customers table doesn't hold, with no orders.
    f"INSERT INTO jaffle_storefront_session VALUES ('ORPHAN-1', 'no-such-customer', "
    f"'{PHILADELPHIA}', TIMESTAMP '2016-09-01 11:00:00', NULL, NULL)",
]
# Converted sessions / all 13 sessions in 2016-09: the pack's 10, plus the three above.
EXPECTED = {
    "q09_session_to_order_conversion_7d": Fraction(11, 13),
    "q15_same_store_session_to_order_conversion_7d": Fraction(10, 13),
    "q17_session_to_order_conversion_14d": Fraction(11, 13),
    "q18_same_store_session_to_order_conversion_50m": Fraction(8, 13),
}


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
    _, _, runtime = pack
    rows = runtime.query(
        {
            "select": ["metric.jaffle.session_to_order_conversion_rate_7d"],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "month"},
        }
    )["rows"]
    by_store = {
        row["dimension.jaffle_store_name"]: row["session_to_order_conversion_rate_7d"]
        for row in rows
    }
    # Philadelphia: the pack's 10 sessions convert, ANON-1 and ORPHAN-1 don't (10 of 12).
    # No store: NOSTORE-1's customer orders 89 minutes later (1 of 1).
    assert by_store == {
        "Philadelphia": pytest.approx(10 / 12),
        None: pytest.approx(1.0),
    }


def test_no_key_matches_no_key(pack) -> None:
    _, _, runtime = pack
    sql = runtime.query(
        json.loads(
            (
                PACK / "semantic_rails" / "queries" / "q17_session_to_order_conversion_14d.json"
            ).read_text()
        )
    )["rendered_sql"]
    assert "base_events.__match_key_1 = converted_events.__match_key_1" in sql
    assert "IS NOT DISTINCT FROM converted_events.__match_key_1" not in sql
