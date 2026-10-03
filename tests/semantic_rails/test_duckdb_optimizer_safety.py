"""Every engine connection excludes the optimizer that conflates aggregate leaves."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pytest

from semantic_rails import db, seed_provenance
from semantic_rails.architect_introspection import open_duckdb
from semantic_rails.db_parts import ducklake, motherduck
from semantic_rails.db_parts.duckdb_setup import configure_duckdb_connection
from semantic_rails.embedding import DuckDBAdapter
from semantic_rails.runtime import Runtime

SETTING = "SELECT current_setting('disabled_optimizers')"
SEED = """
CREATE TABLE amt(id INT, amount DECIMAL(15,2));
INSERT INTO amt VALUES (1,10),(2,20),(3,1),(4,2);
CREATE TABLE a(id INT); INSERT INTO a VALUES (1),(3);
CREATE TABLE b(id INT); INSERT INTO b VALUES (2),(4);
CREATE VIEW v AS SELECT amt.id,
  CASE WHEN a.id IS NOT NULL THEN 'A' WHEN b.id IS NOT NULL THEN 'B' END AS typ,
  CASE WHEN a.id IS NOT NULL THEN amount END AS a_amt,
  CASE WHEN b.id IS NOT NULL THEN amount END AS b_amt
FROM amt LEFT JOIN a ON a.id=amt.id LEFT JOIN b ON b.id=amt.id;
"""
MULTI_LEAF = """
SELECT (SELECT SUM(a_amt) FROM v WHERE typ IN ('A','B')) AS a_total,
       (SELECT SUM(b_amt) FROM v WHERE typ IN ('A','B')) AS b_total
"""


def _disabled(connection):
    return set(connection.execute(SETTING).fetchone()[0].split(","))


@pytest.mark.parametrize("existing", ["", "filter_pushdown", "filter_pushdown,common_subplan"])
def test_setup_preserves_existing_exclusions_and_cursors(existing):
    with duckdb.connect(config={"disabled_optimizers": existing}) as connection:
        assert configure_duckdb_connection(connection) is connection
        configure_duckdb_connection(connection)  # idempotent
        expected = {"common_subplan", *filter(None, existing.split(","))}
        assert _disabled(connection) == expected
        with connection.cursor() as cursor:
            assert _disabled(cursor) == expected


@pytest.mark.parametrize("failure", ["read", "set"])
def test_setup_failure_closes_connection_and_never_returns_it(failure):
    connection = Mock()
    if failure == "read":
        connection.execute.side_effect = RuntimeError("setting unavailable")
    else:
        connection.fetchone.return_value = ("",)
        connection.execute.side_effect = [connection, RuntimeError("setting unavailable")]
    with pytest.raises(RuntimeError, match="setting unavailable"):
        configure_duckdb_connection(connection)
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "factory", ["memory", "file", "read_only", "embedding", "registry", "introspection"]
)
def test_local_connection_factories_disable_common_subplan(tmp_path, factory):
    path = str(tmp_path / "warehouse.duckdb")
    with duckdb.connect(path) as connection:
        connection.execute(SEED)
    if factory == "introspection":
        with open_duckdb(path) as warehouse:
            assert "common_subplan" in _disabled(warehouse.connection)
            assert warehouse.connection.execute(MULTI_LEAF).fetchone() == (Decimal(11), Decimal(22))
        return
    if factory == "memory":
        database = db.Database.connect_in_memory()
        database.execute_script(SEED)
        connection, close = database.conn, database.close
    elif factory in {"file", "read_only"}:
        database = db.Database.connect(path, read_only=factory == "read_only")
        connection, close = database.conn, database.close
    else:
        if factory == "embedding":
            adapter = DuckDBAdapter(path)
        else:
            adapter = db.create_warehouse_adapter(SimpleNamespace(warehouse="duckdb"), db_path=path)
        connection, close = adapter._db.conn, adapter.close  # noqa: SLF001
        assert adapter.query(MULTI_LEAF) == [{"a_total": Decimal(11), "b_total": Decimal(22)}]
    try:
        assert "common_subplan" in _disabled(connection)
        assert connection.execute(MULTI_LEAF).fetchone() == (Decimal(11), Decimal(22))
    finally:
        close()


def test_setting_does_not_change_shared_file_connection_config(tmp_path):
    path = str(tmp_path / "shared.duckdb")
    database = db.Database.connect(path)
    try:
        with duckdb.connect(path) as other:
            assert other.execute("SELECT 1").fetchone() == (1,)
    finally:
        database.close()


@pytest.mark.parametrize("factory", ["ducklake", "motherduck", "md_path"])
def test_remote_factories_configure_real_duckdb_before_bootstrap(monkeypatch, tmp_path, factory):
    # Execute settings with real DuckDB; stub only extension/network/namespace work.
    connect = duckdb.connect
    calls = []

    class Connection:
        def __init__(self):
            self.raw = connect(config={"disabled_optimizers": "filter_pushdown"})

        def execute(self, sql, parameters=None):
            if sql.startswith(("INSTALL ", "LOAD ", "ATTACH ", "CREATE ", "USE ")):
                assert _disabled(self.raw) == {"filter_pushdown", "common_subplan"}
                if sql.startswith("ATTACH "):
                    self.raw.execute("ATTACH ':memory:' AS jaffle")
                return self
            return self.raw.execute(sql, parameters or [])

        def cursor(self):
            return self.raw.cursor()

        def close(self):
            self.raw.close()

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return Connection()

    driver = SimpleNamespace(connect=fake_connect)
    if factory == "ducklake":
        monkeypatch.setattr(ducklake, "import_driver", lambda *args, **kwargs: driver)
        adapter = ducklake.DuckLakeAdapter({"catalog_path": str(tmp_path / "catalog.ducklake")})
        connection = adapter._create_connection()  # noqa: SLF001
        raw = connection._conn.raw  # noqa: SLF001
    elif factory == "motherduck":
        monkeypatch.setattr(motherduck, "duckdb", driver)
        monkeypatch.setenv("TEST_MOTHERDUCK_TOKEN", "test-token")
        adapter = motherduck.MotherDuckAdapter(
            {"database": "analytics", "token_env": "TEST_MOTHERDUCK_TOKEN"}
        )
        connection = adapter._create_connection()  # noqa: SLF001
        raw = connection.raw
        assert calls == [(("md:",), {"config": {"motherduck_token": "test-token"}})]
    else:
        monkeypatch.setattr(db, "duckdb", driver)
        database = db.Database.connect("md:analytics")
        connection = database.conn
        raw = connection.raw
        assert calls == [(("md:analytics",), {"read_only": False})]
    try:
        assert _disabled(raw) == {"filter_pushdown", "common_subplan"}
        with connection.cursor() as cursor:
            assert _disabled(cursor) == {"filter_pushdown", "common_subplan"}
    finally:
        connection.close()


@pytest.mark.parametrize("factory", ["record", "probe"])
def test_seed_connection_factories_read_back_exclusion(monkeypatch, tmp_path, capsys, factory):
    path = str(tmp_path / "seed.duckdb")
    with duckdb.connect(path) as connection:
        connection.execute("CREATE TABLE amt(id INT)")
    settings = []

    def checked_configure(connection):
        configured = configure_duckdb_connection(connection)
        settings.append(_disabled(configured))
        return configured

    monkeypatch.setattr(seed_provenance, "configure_duckdb_connection", checked_configure)
    if factory == "record":
        seed_provenance.record_seed_provenance(path, "subtypes")
    else:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"path": path, "relations": ["amt"]}))
        )
        seed_provenance._probe_cli()  # noqa: SLF001
        assert json.loads(capsys.readouterr().out)["missing"] == []
    assert settings == [{"common_subplan"}]


def test_engine_subtype_measures_match_separate_reference_queries(tmp_path: Path):
    package = tmp_path / "subtypes"
    files = {
        "package.yml": """
schema_version: 1
package: {id: subtypes, namespace: subtypes, warehouse: duckdb,
  default_db: data/warehouse.duckdb, seed: {kind: sql_script, source: data/seed.sql}}
""",
        "graph.yml": """
graph:
  entities:
    amount: {key: [id], model: amounts, allowed_as_root: true}
""",
        "models/amounts.yml": """
model:
  id: amounts
  relation: v
  entities: {amount: {}}
  dimensions:
    typ: {kind: categorical}
  measures:
    a_amount: {kind: aggregate, expr: a_amt, value_type: currency}
    b_amount: {kind: aggregate, expr: b_amt, value_type: currency}
""",
        "data/seed.sql": SEED,
    }
    for relative, text in files.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    runtime = Runtime.from_path(str(package))
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.subtypes.a_amount"}, "as": "a_total"},
                    {"expression": {"measure": "measure.subtypes.b_amount"}, "as": "b_total"},
                ],
                "where": [
                    {"field": "dimension.subtypes_amount_typ", "op": "in", "value": ["A", "B"]}
                ],
            }
        )
        assert result["ok"], result
        # Independent queries cannot share aggregate leaves through common_subplan.
        with duckdb.connect(str(package / "data/warehouse.duckdb"), read_only=True) as reference:
            a_total = reference.execute(
                "SELECT SUM(a_amt) FROM v WHERE typ IN ('A','B')"
            ).fetchone()[0]
            b_total = reference.execute(
                "SELECT SUM(b_amt) FROM v WHERE typ IN ('A','B')"
            ).fetchone()[0]
        assert (a_total, b_total) == (Decimal(11), Decimal(22))
        assert [{key: Decimal(value) for key, value in row.items()} for row in result["rows"]] == [
            {"a_total": a_total, "b_total": b_total}
        ]
    finally:
        runtime.close()
