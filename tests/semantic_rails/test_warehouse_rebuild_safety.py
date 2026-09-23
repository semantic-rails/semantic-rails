"""The DuckDB runtime never replaces a warehouse file its package did not create."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails import seed_provenance
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.db import seed_db
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.seed_provenance import missing_duckdb_relations, seeded_database_unchanged
from tests.semantic_rails.dbt_warehouse import (
    ORDER_COUNT_QUERY,
    PLACEHOLDER_SEED_SQL,
    build_dbt_warehouse,
    file_digest,
    write_orders_package,
)
from tests.semantic_rails.test_relation_pipelines import _write_relation_demo

ORDERS_ONLY_SEED_SQL = PLACEHOLDER_SEED_SQL.split("CREATE TABLE dim_customers")[0]


def _order_count(package_dir: Path) -> list[dict[str, Any]]:
    runtime = Runtime.from_path(str(package_dir))
    try:
        return list(runtime.query(ORDER_COUNT_QUERY)["rows"])
    finally:
        runtime.close()


def _ensure_db(package_dir: Path) -> None:
    runtime = Runtime.from_path(str(package_dir))
    try:
        runtime._ensure_db()  # noqa: SLF001 — seeding is the surface under test
    finally:
        runtime.close()


def _write_foreign_db(db_path: Path, sql: str) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(sql)
    finally:
        conn.close()
    return db_path


def _seed(tmp_path: Path, sql: str, *, package_id: str = "shop") -> Path:
    seed_sql = tmp_path / "seed.sql"
    seed_sql.write_text(sql, encoding="utf-8")
    db_path = tmp_path / "db" / "warehouse.duckdb"
    seed_db(str(db_path), str(seed_sql), package_id=package_id)
    return db_path


def _seeded_orders_only_package(tmp_path: Path) -> tuple[Path, Path]:
    """A package whose own seed built its database, before it needed customers."""
    package_dir = write_orders_package(tmp_path, schema="", with_customers=False)
    (package_dir / "data" / "seed.sql").write_text(ORDERS_ONLY_SEED_SQL, encoding="utf-8")
    _ensure_db(package_dir)
    db_path = package_dir / "data" / "warehouse.duckdb"
    assert seeded_database_unchanged(str(db_path), "shop")
    return package_dir, db_path


# -- the hazard: a database another tool built ---------------------------------


def test_dbt_built_database_in_custom_schemas_is_not_replaced(tmp_path: Path) -> None:
    """Regression: the existence check looked only in schema ``main``, so a
    package reading ``main_marts.*`` rebuilt its placeholder seed over the
    dbt-built file and the dbt output was lost."""
    package_dir = write_orders_package(tmp_path)
    db_path = build_dbt_warehouse(package_dir / "data" / "warehouse.duckdb")
    before = file_digest(db_path)

    runtime = Runtime.from_path(str(package_dir))
    try:
        try:
            rows = runtime.query(ORDER_COUNT_QUERY)["rows"]
        finally:
            assert file_digest(db_path) == before, "the runtime replaced the dbt-built database"
    finally:
        runtime.close()
    assert rows == [{"orders": 8}]


def test_views_count_as_relations(tmp_path: Path) -> None:
    db_path = build_dbt_warehouse(tmp_path / "warehouse.duckdb")

    assert missing_duckdb_relations(
        str(db_path), ["main_staging.stg_orders", "MAIN_MARTS.FCT_ORDERS", "main.fct_orders"]
    ) == ["main.fct_orders"]


def test_foreign_database_missing_relations_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = _write_foreign_db(
        package_dir / "data" / "warehouse.duckdb", "CREATE TABLE fct_orders AS SELECT 1 AS order_id"
    )
    before = file_digest(db_path)

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.code == "INVALID_CONFIG"
    assert excinfo.value.details["missing_relations"] == ["dim_customers"]
    assert excinfo.value.details["reason"] == "default_db_not_built_by_seed"
    assert "package.seed.kind: external" in str(excinfo.value)
    assert "SEMANTIC_RAILS_ALLOW_DB_RESEED=1" in str(excinfo.value)
    assert file_digest(db_path) == before


def test_opt_in_rebuilds_a_foreign_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "1")
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = _write_foreign_db(
        package_dir / "data" / "warehouse.duckdb", "CREATE TABLE fct_orders AS SELECT 1 AS order_id"
    )

    _ensure_db(package_dir)

    assert missing_duckdb_relations(str(db_path), ["fct_orders", "dim_customers"]) == []
    assert seeded_database_unchanged(str(db_path), "shop")


def test_opt_in_rebuild_discards_a_stale_write_ahead_log(tmp_path: Path, monkeypatch) -> None:
    """A write-ahead log left beside the replaced file must not replay into the new one."""
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "1")
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = _write_foreign_db(
        package_dir / "data" / "warehouse.duckdb",
        "CREATE TABLE fct_orders AS SELECT 1 AS order_id",
    )
    crashed_writer = (
        "import duckdb, os, sys; c = duckdb.connect(sys.argv[1]); "
        "c.execute(\"PRAGMA wal_autocheckpoint='1GB'\"); "
        "c.execute('INSERT INTO fct_orders VALUES (2)'); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", crashed_writer, str(db_path)], check=True, timeout=60)
    assert Path(f"{db_path}.wal").exists()

    _ensure_db(package_dir)

    assert not Path(f"{db_path}.wal").exists()
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM fct_orders").fetchone() == (1,)
    finally:
        conn.close()


def test_unreadable_database_is_reported_and_left_untouched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "1")
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    db_path.write_bytes(b"not a duckdb file")

    with pytest.raises(SemanticLayerError, match="could not be opened as a DuckDB database") as err:
        _ensure_db(package_dir)

    assert err.value.details["reason"] == "default_db_unreadable"
    assert db_path.read_bytes() == b"not a duckdb file"


def test_database_held_by_a_writer_is_never_replaced(tmp_path: Path, monkeypatch) -> None:
    """A running writer (``dbt build``) holds DuckDB's file lock; even with the
    opt-in the runtime reports it, without the driver's lock text."""
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "1")
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = _write_foreign_db(
        package_dir / "data" / "warehouse.duckdb", "CREATE TABLE unrelated AS SELECT 1 AS x"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys; c = duckdb.connect(sys.argv[1]); print('ready', flush=True); "
            "sys.stdin.read()",
            str(db_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        with pytest.raises(SemanticLayerError, match="never replaces a file it cannot read") as err:
            _ensure_db(package_dir)
        assert "PID" not in str(err.value) and "lock" not in str(err.value).lower()
        assert "package.seed.kind: external" in str(err.value)
    finally:
        try:
            holder.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate()
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT x FROM unrelated").fetchall() == [(1,)]
    finally:
        conn.close()


def test_database_created_while_the_seed_builds_is_not_overwritten(
    tmp_path: Path, monkeypatch
) -> None:
    """The existence check and the publish are not atomic: a file that
    appears in between (dbt created it) is judged, never overwritten."""
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    original_build = runtime_module.build_seed_database

    def build_while_dbt_creates_the_file(*args: Any, **kwargs: Any) -> str:
        tmp = original_build(*args, **kwargs)
        if not db_path.exists():
            _write_foreign_db(db_path, "CREATE TABLE dbt_output AS SELECT 42 AS answer")
        return tmp

    monkeypatch.setattr(runtime_module, "build_seed_database", build_while_dbt_creates_the_file)

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.details["reason"] == "default_db_not_built_by_seed"
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT answer FROM dbt_output").fetchall() == [(42,)]
    finally:
        conn.close()
    assert not list(db_path.parent.glob("*.tmp"))


def test_concurrent_seeders_keep_the_first_published_file(tmp_path: Path, monkeypatch) -> None:
    """Two runtimes seeding the same missing file: the loser judges the
    winner's file (complete and package-built) and keeps it."""
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    original_build = runtime_module.build_seed_database

    def build_while_a_peer_publishes(*args: Any, **kwargs: Any) -> str:
        tmp = original_build(*args, **kwargs)
        if not db_path.exists():
            seed_db(str(db_path), str(package_dir / "data" / "seed.sql"), package_id="shop")
        return tmp

    monkeypatch.setattr(runtime_module, "build_seed_database", build_while_a_peer_publishes)
    _ensure_db(package_dir)
    winner_inode = db_path.stat().st_ino

    monkeypatch.undo()
    _ensure_db(package_dir)

    assert db_path.stat().st_ino == winner_inode
    assert missing_duckdb_relations(str(db_path), ["fct_orders", "dim_customers"]) == []


def test_a_writer_cannot_start_while_a_rebuild_is_in_progress(tmp_path: Path, monkeypatch) -> None:
    """From the judgement to the publish the runtime holds the file, so a
    writer that starts meanwhile fails loudly instead of losing its work."""
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    write_orders_package(tmp_path, schema="")  # now needs dim_customers
    writer = (
        "import duckdb, sys; c = duckdb.connect(sys.argv[1]); "
        "c.execute('CREATE TABLE dbt_mart AS SELECT 1 AS x'); c.close()"
    )
    attempts: list[subprocess.CompletedProcess[str]] = []
    original_build = runtime_module.build_seed_database

    def build_while_dbt_runs(*args: Any, **kwargs: Any) -> str:
        attempts.append(
            subprocess.run(
                [sys.executable, "-c", writer, str(db_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        )
        return original_build(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "build_seed_database", build_while_dbt_runs)
    _ensure_db(package_dir)

    assert len(attempts) == 1 and attempts[0].returncode != 0
    assert "lock" in attempts[0].stderr.lower(), attempts[
        0
    ].stderr  # locked out, not another failure
    assert missing_duckdb_relations(str(db_path), ["dim_customers"]) == []


def test_concurrent_rebuilders_never_replace_a_file_written_after_the_first_publish(
    tmp_path: Path, monkeypatch
) -> None:
    """Two runtimes rebuild the same package-built file. A publishes, then a
    writer adds to A's new file; B must re-judge that file, not replace it."""
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    write_orders_package(tmp_path, schema="")  # now needs dim_customers
    a_building = threading.Event()
    release_a = threading.Event()
    builds: list[str] = []
    original_build = runtime_module.build_seed_database
    original_publish = runtime_module.publish_seed_database

    def paused_build(*args: Any, **kwargs: Any) -> str:
        builds.append(threading.current_thread().name)
        if len(builds) == 1:
            a_building.set()
            assert release_a.wait(timeout=60)
        return original_build(*args, **kwargs)

    def publish_then_a_writer_writes(*args: Any, **kwargs: Any) -> None:
        original_publish(*args, **kwargs)
        writer = (
            "import duckdb, sys; c = duckdb.connect(sys.argv[1]); "
            "c.execute('CREATE TABLE dbt_mart AS SELECT 7 AS x'); c.close()"
        )
        subprocess.run([sys.executable, "-c", writer, str(db_path)], check=True, timeout=60)

    monkeypatch.setattr(runtime_module, "build_seed_database", paused_build)
    monkeypatch.setattr(runtime_module, "publish_seed_database", publish_then_a_writer_writes)
    errors: list[BaseException] = []

    def rebuild() -> None:
        try:
            _ensure_db(package_dir)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    first = threading.Thread(target=rebuild, name="A")
    second = threading.Thread(target=rebuild, name="B")
    first.start()
    assert a_building.wait(timeout=60)
    second.start()
    time.sleep(0.3)  # B is now waiting for A's rebuild lock
    release_a.set()
    first.join(timeout=120)
    second.join(timeout=120)

    assert errors == []
    assert builds == ["A"]
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT x FROM dbt_mart").fetchall() == [(7,)]
    finally:
        conn.close()


def test_publish_replaces_only_the_judged_file(tmp_path: Path) -> None:
    db_path = _write_foreign_db(tmp_path / "warehouse.duckdb", "CREATE TABLE x AS SELECT 1 AS id")
    replacement = _write_foreign_db(tmp_path / "build.tmp", "CREATE TABLE y AS SELECT 2 AS id")
    before = file_digest(db_path)

    with pytest.raises(SemanticLayerError) as excinfo:
        seed_provenance.publish_seed_database(
            str(replacement), str(db_path), replace_existing=True, judged=(0, 0)
        )

    assert excinfo.value.details["reason"] == "default_db_created_concurrently"
    assert file_digest(db_path) == before


def _no_hard_links(src: str, dst: str) -> None:
    raise OSError(errno.EPERM, "hard links not supported")


def test_a_filesystem_without_hard_links_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Even the first build: a file created meanwhile could be overwritten."""
    monkeypatch.setattr(seed_provenance.os, "link", _no_hard_links)
    package_dir = write_orders_package(tmp_path, schema="")

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.details["reason"] == "atomic_publish_unavailable"
    assert "SEMANTIC_RAILS_ALLOW_DB_RESEED" in str(excinfo.value)
    assert not (package_dir / "data" / "warehouse.duckdb").exists()
    assert not list((package_dir / "data").glob("*.tmp"))


def test_without_hard_links_the_opt_in_builds_the_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(seed_provenance.os, "link", _no_hard_links)
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "on")
    package_dir = write_orders_package(tmp_path, schema="")

    _ensure_db(package_dir)

    assert _order_count(package_dir)
    assert not list((package_dir / "data").glob("*.tmp"))


def test_where_a_publish_cannot_keep_writers_out_only_the_opt_in_rebuilds(
    tmp_path: Path, monkeypatch
) -> None:
    """On Windows the judged file must be released before it is replaced, so
    an unattended rebuild could lose a write: it needs the opt-in there."""
    monkeypatch.setattr(runtime_module, "_PUBLISH_KEEPS_WRITERS_OUT", False)
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    write_orders_package(tmp_path, schema="")  # now needs dim_customers

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["reason"] == "rebuild_unsupported_on_windows"

    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "1")
    _ensure_db(package_dir)
    assert missing_duckdb_relations(str(db_path), ["dim_customers"]) == []


# -- external databases ---------------------------------------------------------


def test_external_database_is_read_as_built(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, seed={"kind": "external"})
    db_path = build_dbt_warehouse(package_dir / "data" / "warehouse.duckdb")
    before = file_digest(db_path)

    assert _order_count(package_dir) == [{"orders": 8}]
    assert file_digest(db_path) == before


def test_external_database_missing_a_relation_is_still_never_rebuilt(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, seed={"kind": "external"})
    db_path = build_dbt_warehouse(package_dir / "data" / "warehouse.duckdb")
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("DROP TABLE main_marts.dim_customers")
    finally:
        conn.close()
    before = file_digest(db_path)

    # Models over relations dbt has built keep working while others are pending.
    assert _order_count(package_dir) == [{"orders": 8}]
    assert file_digest(db_path) == before


def test_missing_external_database_is_reported_not_created(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, seed={"kind": "external"})

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.code == "INVALID_CONFIG"
    assert excinfo.value.details["reason"] == "external_default_db_missing"
    assert not (package_dir / "data" / "warehouse.duckdb").exists()


@pytest.mark.parametrize(
    "seed",
    [
        {"kind": "external", "source": "data/seed.sql"},
        {"kind": "external", "post_sql": "data/post.sql"},
    ],
)
def test_external_seed_takes_no_source(tmp_path: Path, seed: dict[str, str]) -> None:
    package_dir = write_orders_package(tmp_path, seed=seed)

    with pytest.raises(SemanticLayerError, match="takes no source or post_sql"):
        load_package_config(str(package_dir))
    # The validator reports it itself (its message has no loader suffix).
    assert (
        f"{package_dir / 'package.yml'}: package.seed.kind 'external' takes no source or "
        "post_sql" in (validate_runtime_package(package_dir))
    )


def test_external_seed_validates_without_a_source(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, seed={"kind": "external"})

    assert load_package_config(str(package_dir)).package.seed.kind == "external"
    assert validate_runtime_package(package_dir) == []


# -- a database the package's own seed built -------------------------------------


def test_package_built_database_is_rebuilt_when_the_package_needs_more(tmp_path: Path) -> None:
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    first_inode = db_path.stat().st_ino

    # The package now also reads dim_customers, and its seed builds it.
    write_orders_package(tmp_path, schema="")
    _ensure_db(package_dir)

    assert db_path.stat().st_ino != first_inode
    assert missing_duckdb_relations(str(db_path), ["fct_orders", "dim_customers"]) == []


@pytest.mark.parametrize(
    "change",
    [
        "CREATE SCHEMA main_marts; CREATE TABLE main_marts.fct_orders AS SELECT 1 AS order_id",
        "INSERT INTO fct_orders SELECT * FROM fct_orders",
        "UPDATE fct_orders SET order_total = 99",
        "CREATE VIEW open_orders AS SELECT * FROM fct_orders WHERE status = 'placed'",
        "CREATE SEQUENCE extra_ids",
        "CREATE MACRO double_it(x) AS x * 2",
        "CREATE UNIQUE INDEX fct_orders_id ON fct_orders (order_id)",
        "ALTER TABLE fct_orders ALTER COLUMN status SET NOT NULL",
        "ALTER TABLE fct_orders ALTER COLUMN status SET DEFAULT 'placed'",
        "CREATE TYPE mood AS ENUM ('ok')",
        "COMMENT ON TABLE fct_orders IS 'edited'",
        "CREATE TABLE _semantic_rails.external_audit AS SELECT 1 AS x",
    ],
    ids=[
        "new-table",
        "new-rows",
        "updated-rows",
        "new-view",
        "new-sequence",
        "new-macro",
        "new-index",
        "not-null",
        "column-default",
        "new-type",
        "comment",
        "table-beside-provenance",
    ],
)
def test_package_built_database_changed_since_seeding_is_not_replaced(
    tmp_path: Path, change: str
) -> None:
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    _write_foreign_db(db_path, change)
    before = file_digest(db_path)

    write_orders_package(tmp_path, schema="")
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.details["missing_relations"] == ["dim_customers"]
    assert file_digest(db_path) == before


def test_a_column_named_like_the_row_alias_cannot_hide_an_edit(tmp_path: Path) -> None:
    """Regression: ``sum(hash(t)) FROM x AS t`` hashed only a column named ``t``."""
    db_path = _seed(tmp_path, "CREATE TABLE fct_orders AS SELECT 1 AS order_id, 'a' AS t")

    _write_foreign_db(db_path, "UPDATE fct_orders SET order_id = 2")

    assert not seeded_database_unchanged(str(db_path), "shop")


RICH_SEED_SQL = """
CREATE SCHEMA mart;
CREATE SEQUENCE order_ids START 100;
CREATE TABLE mart.orders (
  id INTEGER PRIMARY KEY DEFAULT nextval('order_ids'),
  status VARCHAR NOT NULL DEFAULT 'placed',
  address STRUCT(city VARCHAR, zip INTEGER),
  amount DECIMAL(10, 2) CHECK (amount >= 0)
);
INSERT INTO mart.orders (status, address, amount)
  VALUES ('placed', {'city': 'Oslo', 'zip': 150}, 10.5), ('shipped', NULL, 3);
CREATE UNIQUE INDEX orders_status ON mart.orders (status, id);
CREATE VIEW mart.open_orders AS SELECT * FROM mart.orders WHERE status = 'placed';
COMMENT ON TABLE mart.orders IS 'orders';
CREATE TABLE "orders.history" AS SELECT 1 AS id;
CREATE TABLE "odd""name" AS SELECT 2 AS id;
CREATE TABLE empty_later (id INTEGER);
"""


def test_a_seed_with_every_kind_of_object_counts_as_unchanged(tmp_path: Path) -> None:
    """Sequences, structured columns, indexes, constraints, comments, and table
    names containing dots or quotes survive the round trip from the build session
    to a fresh read-only connection."""
    db_path = _seed(tmp_path, RICH_SEED_SQL)

    assert seeded_database_unchanged(str(db_path), "shop")
    _write_foreign_db(db_path, 'INSERT INTO "orders.history" VALUES (2)')
    assert not seeded_database_unchanged(str(db_path), "shop")


@pytest.mark.parametrize(
    ("seed", "change"),
    [
        (
            "CREATE TABLE t AS SELECT 1 AS id; CREATE MACRO bump(x, y := 1) AS x + y;",
            "CREATE OR REPLACE MACRO bump(x, y := 100) AS x + y",
        ),
        (
            "CREATE TYPE address AS STRUCT(city VARCHAR); CREATE TABLE t AS SELECT 1 AS id;",
            "DROP TYPE address; CREATE TYPE address AS STRUCT(zip INTEGER)",
        ),
    ],
    ids=["macro-default", "struct-type"],
)
def test_objects_the_inventory_cannot_fingerprint_never_count_as_unchanged(
    tmp_path: Path, seed: str, change: str
) -> None:
    """DuckDB reports neither macro default arguments nor the definition of a
    user-defined type, so a file holding either is never provably unchanged."""
    db_path = _seed(tmp_path, seed)
    assert not seeded_database_unchanged(str(db_path), "shop")

    _write_foreign_db(db_path, change)

    assert not seeded_database_unchanged(str(db_path), "shop")


def test_a_package_built_database_with_a_macro_is_not_rebuilt(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="", with_customers=False)
    (package_dir / "data" / "seed.sql").write_text(
        ORDERS_ONLY_SEED_SQL + "CREATE MACRO bump(x, y := 1) AS x + y;\n", encoding="utf-8"
    )
    _ensure_db(package_dir)
    db_path = package_dir / "data" / "warehouse.duckdb"
    _write_foreign_db(db_path, "CREATE OR REPLACE MACRO bump(x, y := 100) AS x + y")
    before = file_digest(db_path)

    write_orders_package(tmp_path, schema="")  # now needs dim_customers
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.details["reason"] == "default_db_not_built_by_seed"
    assert file_digest(db_path) == before


def test_a_new_column_on_an_empty_table_counts_as_a_change(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, RICH_SEED_SQL)

    _write_foreign_db(db_path, "ALTER TABLE empty_later ADD COLUMN tier VARCHAR")

    assert not seeded_database_unchanged(str(db_path), "shop")


def test_provenance_is_bound_to_the_package(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, PLACEHOLDER_SEED_SQL)

    assert seeded_database_unchanged(str(db_path), "shop")
    assert not seeded_database_unchanged(str(db_path), "another_package")


def test_provenance_without_an_inventory_never_counts_as_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    def _unreadable_catalog(conn: Any) -> dict[str, Any]:
        raise RuntimeError("catalog query failed")

    monkeypatch.setattr(seed_provenance, "_catalog", _unreadable_catalog)
    db_path = _seed(tmp_path, PLACEHOLDER_SEED_SQL)
    monkeypatch.undo()

    assert missing_duckdb_relations(str(db_path), ["fct_orders", "dim_customers"]) == []
    assert not seeded_database_unchanged(str(db_path), "shop")


def test_provenance_from_another_inventory_format_never_counts_as_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _seed(tmp_path, PLACEHOLDER_SEED_SQL)

    monkeypatch.setattr(seed_provenance, "_INVENTORY_FORMAT", seed_provenance._INVENTORY_FORMAT + 1)

    assert not seeded_database_unchanged(str(db_path), "shop")


def test_a_refused_database_is_not_rescanned_until_it_changes(tmp_path: Path, monkeypatch) -> None:
    db_path = _write_foreign_db(tmp_path / "warehouse.duckdb", "CREATE TABLE x AS SELECT 1 AS id")
    judged: list[str] = []
    original_judge = seed_provenance._judge

    def counting_judge(path: str, package_id: str) -> bool:
        judged.append(path)
        return original_judge(path, package_id)

    monkeypatch.setattr(seed_provenance, "_judge", counting_judge)

    assert not seeded_database_unchanged(str(db_path), "shop")
    assert not seeded_database_unchanged(str(db_path), "shop")
    assert len(judged) == 1
    _write_foreign_db(db_path, "INSERT INTO x VALUES (2)")
    assert not seeded_database_unchanged(str(db_path), "shop")
    assert len(judged) == 2


def test_seed_without_a_package_records_no_provenance(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, PLACEHOLDER_SEED_SQL, package_id="")

    assert not seeded_database_unchanged(str(db_path), "")


# -- relation pipelines -----------------------------------------------------------


def _relation_demo_without(tmp_path: Path, table: str) -> tuple[Path, Path, str]:
    package_dir = _write_relation_demo(tmp_path)
    full_seed = (package_dir / "seed.sql").read_text(encoding="utf-8")
    partial_seed = tmp_path / "partial_seed.sql"
    partial_seed.write_text(
        "\n".join(line for line in full_seed.splitlines() if table not in line), encoding="utf-8"
    )
    return package_dir, tmp_path / "relation_demo.duckdb", str(partial_seed)


def test_relation_pipeline_entities_do_not_force_a_rebuild(tmp_path: Path, monkeypatch) -> None:
    """Entities over a relation pipeline read a compiler-built CTE; counting
    them as stored tables rebuilt the database on every runtime start."""
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package_dir = _write_relation_demo(tmp_path)
    db_path = tmp_path / "relation_demo.duckdb"
    _ensure_db(package_dir)
    first_inode = db_path.stat().st_ino

    _ensure_db(package_dir)

    assert db_path.stat().st_ino == first_inode


def test_a_missing_pipeline_source_rebuilds_a_package_built_database(
    tmp_path: Path, monkeypatch
) -> None:
    """``send_feed`` unions sms_sends and email_sends: a pipeline's stored
    sources are relations the package reads."""
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package_dir, db_path, partial_seed = _relation_demo_without(tmp_path, "email_sends")
    seed_db(str(db_path), partial_seed, package_id="relation_demo")
    assert missing_duckdb_relations(str(db_path), ["email_sends"]) == ["email_sends"]

    _ensure_db(package_dir)

    assert missing_duckdb_relations(str(db_path), ["email_sends", "sms_sends"]) == []


def test_a_foreign_database_missing_a_pipeline_source_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package_dir, db_path, partial_seed = _relation_demo_without(tmp_path, "email_sends")
    seed_db(str(db_path), partial_seed)  # no package id: another tool built it
    before = file_digest(db_path)

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.details["missing_relations"] == ["email_sends"]
    assert file_digest(db_path) == before


# -- a file replaced while this process still has the old one open ---------------


def _probe(db_path: Path, sql: str) -> str:
    """Query the file from another process, which shares no DuckDB instance with this one."""
    code = (
        "import duckdb, sys; c = duckdb.connect(sys.argv[1], read_only=True); "
        "print(c.execute(sys.argv[2]).fetchall())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(db_path), sql], capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip() or result.stderr.strip()


def test_a_replaced_file_is_judged_as_it_is_on_disk(tmp_path: Path) -> None:
    """A served runtime keeps the old database open, and DuckDB would hand a new
    connection in this process that copy. The check reads the file at the path."""
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    served = Runtime.from_path(str(package_dir))
    try:
        assert served.query(ORDER_COUNT_QUERY)["rows"]  # the placeholder build, held open
        dbt_file = build_dbt_warehouse(tmp_path / "dbt_out" / "warehouse.duckdb")
        os.replace(dbt_file, db_path)  # dbt's output moved into place
        write_orders_package(tmp_path, schema="main_marts")
        before = file_digest(db_path)

        _ensure_db(package_dir)  # dbt's file has every relation: nothing to do

        assert file_digest(db_path) == before
    finally:
        served.close()


def test_a_write_to_a_rebuilt_file_survives_while_the_old_file_is_still_open(
    tmp_path: Path,
) -> None:
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    served = Runtime.from_path(str(package_dir))
    try:
        assert served.query(ORDER_COUNT_QUERY)["rows"]  # holds the first build
        write_orders_package(tmp_path, schema="")  # now needs dim_customers
        _ensure_db(package_dir)  # a legitimate rebuild into a new file
        writer = (
            "import duckdb, sys; c = duckdb.connect(sys.argv[1]); "
            "c.execute('CREATE TABLE my_work AS SELECT 42 AS answer'); c.close()"
        )
        subprocess.run([sys.executable, "-c", writer, str(db_path)], check=True, timeout=60)

        _ensure_db(package_dir)  # judged by the rebuilt file, which has dim_customers

        assert _probe(db_path, "SELECT answer FROM my_work") == "[(42,)]"
    finally:
        served.close()


# -- the rebuild lock ----------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="per-user lock directories are POSIX")
def test_the_rebuild_lock_directory_must_be_private(tmp_path: Path, monkeypatch) -> None:
    """Another user (or a sudo run) can't make every query fail by creating the
    lock directory first, and only a rebuild takes the lock at all."""
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(seed_provenance.tempfile, "tempdir", str(temp_root))
    shared = temp_root / f"semantic-rails-db-locks-{os.getuid()}"
    shared.mkdir()
    shared.chmod(0o755)
    package_dir, db_path = _seeded_orders_only_package(tmp_path)

    assert _order_count(package_dir)  # nothing missing: no lock taken

    write_orders_package(tmp_path, schema="")  # now needs dim_customers: a rebuild
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["reason"] == "rebuild_lock_unavailable"
    assert seeded_database_unchanged(str(db_path), "shop")  # left alone


# -- errors before work ----------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_broken_default_db_link_is_reported_not_built(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "gone.duckdb", db_path)

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.details["reason"] == "default_db_broken_link"
    assert not (tmp_path / "gone.duckdb").exists()


def test_a_missing_seed_source_is_reported_before_the_database_is_scanned(
    tmp_path: Path, monkeypatch
) -> None:
    package_dir, _db_path = _seeded_orders_only_package(tmp_path)
    write_orders_package(tmp_path, schema="")  # now needs dim_customers
    (package_dir / "data" / "seed.sql").unlink()
    scans: list[Any] = []
    monkeypatch.setattr(
        runtime_module, "seeded_database_unchanged", lambda *args, **kwargs: scans.append(args)
    )

    with pytest.raises(SemanticLayerError, match="package.seed.source"):
        _ensure_db(package_dir)

    assert scans == []
