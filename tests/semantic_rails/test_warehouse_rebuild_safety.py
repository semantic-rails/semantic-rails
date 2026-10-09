"""Runtime bootstrap creates missing seeds and never replaces an existing DuckDB file."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails import db as db_module
from semantic_rails import runtime as runtime_module
from semantic_rails import seed_provenance
from semantic_rails.cli import scaffold
from semantic_rails.cli.reports import project_validation_report
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, validate_runtime_package
from semantic_rails.db import load_csv_dir_to_duckdb, seed_db
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.seed_provenance import missing_duckdb_relations
from tests.semantic_rails.dbt_warehouse import (
    ORDER_COUNT_QUERY,
    PLACEHOLDER_SEED_SQL,
    build_dbt_warehouse,
    file_digest,
    write_orders_package,
)
from tests.semantic_rails.test_relation_pipelines import _write_relation_demo

ORDERS_ONLY_SEED_SQL = PLACEHOLDER_SEED_SQL.split("CREATE TABLE dim_customers")[0]


def _ensure_db(package_dir: Path) -> None:
    runtime = Runtime.from_path(str(package_dir))
    try:
        runtime._ensure_db()  # noqa: SLF001 — bootstrap is the surface under test
    finally:
        runtime.close()


def _write_db(db_path: Path, sql: str) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(sql)
    finally:
        conn.close()
    return db_path


def _explicit_seed(tmp_path: Path, db_path: Path, kind: str) -> None:
    if kind == "sql":
        source = tmp_path / "new_seed.sql"
        source.write_text("CREATE TABLE replacement AS SELECT 2 AS marker", encoding="utf-8")
        seed_db(str(db_path), str(source))
    else:
        source = tmp_path / "new_csv"
        source.mkdir()
        (source / "replacement.csv").write_text("marker\n2\n", encoding="utf-8")
        load_csv_dir_to_duckdb(str(db_path), str(source))


@pytest.mark.parametrize("kind", ["sql_script", "csv_dir_duckdb"])
@pytest.mark.parametrize("existing", [False, True])
def test_failed_seed_statement_reports_context_without_publishing(
    tmp_path: Path, kind: str, existing: bool
) -> None:
    source = tmp_path / "seed.sql"
    source.write_text(
        "-- a comment with a semicolon;\nSELECT 'a;b';\n\n"
        "SELECT missing_seed_column\nFROM missing_seed_table;\n"
        "CREATE TABLE must_not_run AS SELECT 1;",
        encoding="utf-8",
    )
    target = tmp_path / "warehouse.duckdb"
    before = None
    if existing:
        _write_db(target, "CREATE TABLE original AS SELECT 1 AS marker")
        before = file_digest(target)

    with pytest.raises(SemanticLayerError) as caught:
        if kind == "sql_script":
            seed_db(str(target), str(source))
        else:
            load_csv_dir_to_duckdb(str(target), str(tmp_path), str(source))

    error = caught.value
    assert error.code == "INVALID_CONFIG"
    assert error.details == {
        "reason": "seed_failed",
        "file": str(source),
        "statement_number": 2,
        "statement_first_line": "SELECT missing_seed_column",
        "database_message": str(error.__cause__),
    }
    assert "missing_seed_table" in error.details["database_message"]
    for text in (str(source), "statement 2", "SELECT missing_seed_column", "missing_seed_table"):
        assert text in str(error)
    if existing:
        assert file_digest(target) == before
    else:
        assert not target.exists()
    assert not list(tmp_path.glob("*.seed.*.tmp*"))


@pytest.mark.parametrize("surface", ["query", "runtime_validation"])
def test_runtime_preserves_failed_seed_statement_details(tmp_path: Path, surface: str) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    source = package_dir / "data" / "seed.sql"
    source.write_text("SELECT 1;\nSELECT * FROM missing_seed_table;", encoding="utf-8")
    if surface == "query":
        runtime = Runtime.from_path(str(package_dir))
        try:
            with pytest.raises(SemanticLayerError) as caught:
                runtime.query(ORDER_COUNT_QUERY)
            errors = [{"code": caught.value.code, "details": caught.value.details}]
        finally:
            runtime.close()
    else:
        report = project_validation_report(
            PackageReference(source_path=str(package_dir)), mode="runtime"
        )
        assert not report["ok"]
        errors = [probe["error"] for probe in report["runtime"]["probes"] if not probe["ok"]]
        assert errors
    for error in errors:
        assert error["code"] == "INVALID_CONFIG"
        assert error["details"]["reason"] == "seed_failed"
        assert error["details"]["file"] == str(source)
        assert error["details"]["statement_number"] == 2
        assert error["details"]["statement_first_line"] == "SELECT * FROM missing_seed_table"
        assert "missing_seed_table" in error["details"]["database_message"]
    assert not (package_dir / "data" / "warehouse.duckdb").exists()
    assert not list((package_dir / "data").glob("*.seed.*.tmp*"))


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("surface", ["query", "runtime_validation", "segment_preview"])
def test_failed_query_reports_stale_seed_hint_without_replacing_database(
    tmp_path: Path, stale: bool, surface: str
) -> None:
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    model_path = package_dir / "models" / "orders.yml"
    model = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    model["model"]["measures"]["order_total"]["expr"] = "new_order_total"
    if surface == "segment_preview":
        model["model"]["dimensions"]["order_id"] = {"label": "Order ID", "kind": "categorical"}
        segments = package_dir / "segments"
        segments.mkdir()
        (segments / "orders.yml").write_text(
            yaml.safe_dump(
                {
                    "segment": {
                        "as": "segment.shop.orders",
                        "label": "Orders",
                        "entity": "entity.shop_order",
                        "basis_metric": "metric.shop.revenue",
                    }
                }
            ),
            encoding="utf-8",
        )
    model_path.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    if stale:
        source = package_dir / "data" / "seed.sql"
        source.write_text(
            ORDERS_ONLY_SEED_SQL.replace("order_total", "new_order_total"), encoding="utf-8"
        )
    before = file_digest(db_path)
    query = {"version": 1, "select": [{"expression": {"measure": "measure.shop.order_total"}}]}
    if surface in {"query", "segment_preview"}:
        runtime = Runtime.from_path(str(package_dir))
        try:
            with pytest.raises(SemanticLayerError) as caught:
                if surface == "query":
                    runtime.query(query)
                else:
                    runtime.segment_preview("segment.shop.orders")
            error = {"code": caught.value.code, "details": caught.value.details}
        finally:
            runtime.close()
    else:
        report = project_validation_report(
            PackageReference(source_path=str(package_dir)), mode="runtime"
        )
        assert not report["ok"]
        error = next(
            probe["error"]
            for probe in report["runtime"]["probes"]
            if probe["object_id"] == "measure.shop.order_total"
        )
    assert error["code"] == "QUERY_EXECUTION_ERROR"
    details = error["details"]
    assert details["sql_redacted"] is True and "sql" not in details
    assert "database_message" not in details
    if stale:
        (warning,) = details["warnings"]
        assert warning["code"] == "STALE_SEED_DATABASE"
        assert warning["details"] == {"default_db": str(db_path), "reason": "seed_files_changed"}
        assert warning["message"].endswith(f"rm {db_path}")
    else:
        assert "warnings" not in details
    assert file_digest(db_path) == before


@pytest.mark.parametrize("kind", ["sql", "csv"])
def test_explicit_seed_refuses_existing_wal_without_touching_database(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db_path = _write_db(
        tmp_path / "warehouse.duckdb", "CREATE TABLE original AS SELECT 1 AS marker"
    )
    before, inode = file_digest(db_path), db_path.stat().st_ino
    wal_path = Path(f"{db_path}.wal")
    wal_path.write_bytes(b"pending committed changes")
    monkeypatch.setattr(db_module.os, "replace", lambda *_: pytest.fail("must not replace"))

    with pytest.raises(SemanticLayerError, match="close and checkpoint"):
        _explicit_seed(tmp_path, db_path, kind)

    assert file_digest(db_path) == before and db_path.stat().st_ino == inode
    assert wal_path.read_bytes() == b"pending committed changes"
    assert not list(tmp_path.glob("*.seed.*.tmp"))


@pytest.mark.parametrize("kind", ["sql", "csv"])
def test_explicit_seed_refuses_wal_created_during_build(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db_path = _write_db(
        tmp_path / "warehouse.duckdb", "CREATE TABLE original AS SELECT 1 AS marker"
    )
    before = file_digest(db_path)
    wal_path = Path(f"{db_path}.wal")
    build = db_module.build_seed_database

    def build_then_create_wal(*args: Any, **kwargs: Any) -> str:
        temporary = build(*args, **kwargs)
        wal_path.write_bytes(b"pending committed changes")
        return temporary

    monkeypatch.setattr(db_module, "build_seed_database", build_then_create_wal)
    monkeypatch.setattr(db_module.os, "replace", lambda *_: pytest.fail("must not replace"))

    with pytest.raises(SemanticLayerError, match="close and checkpoint"):
        _explicit_seed(tmp_path, db_path, kind)

    assert file_digest(db_path) == before
    assert wal_path.read_bytes() == b"pending committed changes"
    assert not list(tmp_path.glob("*.seed.*.tmp"))


@pytest.mark.parametrize("kind", ["sql", "csv"])
def test_explicit_seed_failed_publication_preserves_database_and_new_wal(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db_path = _write_db(
        tmp_path / "warehouse.duckdb", "CREATE TABLE original AS SELECT 1 AS marker"
    )
    before, inode = file_digest(db_path), db_path.stat().st_ino
    wal_path = Path(f"{db_path}.wal")
    unrelated_seed = tmp_path / "warehouse.duckdb.seed.other.tmp"
    unrelated_seed.write_bytes(b"another seed operation")

    def fail_publication(_source: str, _target: str) -> None:
        wal_path.write_bytes(b"concurrent recovery log")
        raise OSError("injected publication failure")

    monkeypatch.setattr(db_module.os, "replace", fail_publication)
    with pytest.raises(OSError, match="injected publication failure"):
        _explicit_seed(tmp_path, db_path, kind)

    assert file_digest(db_path) == before and db_path.stat().st_ino == inode
    assert wal_path.read_bytes() == b"concurrent recovery log"
    assert list(tmp_path.glob("*.seed.*.tmp")) == [unrelated_seed]
    assert unrelated_seed.read_bytes() == b"another seed operation"


def _seeded_orders_only_package(tmp_path: Path) -> tuple[Path, Path]:
    package_dir = write_orders_package(tmp_path, schema="", with_customers=False)
    (package_dir / "data" / "seed.sql").write_text(ORDERS_ONLY_SEED_SQL, encoding="utf-8")
    _ensure_db(package_dir)
    return package_dir, package_dir / "data" / "warehouse.duckdb"


def test_dbt_built_custom_schema_and_views_are_read_without_replacement(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path)
    db_path = build_dbt_warehouse(package_dir / "data" / "warehouse.duckdb")
    before = file_digest(db_path)
    runtime = Runtime.from_path(str(package_dir))
    try:
        assert runtime.query(ORDER_COUNT_QUERY)["rows"] == [{"orders": 8}]
    finally:
        runtime.close()
    assert file_digest(db_path) == before
    assert missing_duckdb_relations(
        str(db_path), ["main_staging.stg_orders", "MAIN_MARTS.FCT_ORDERS", "main.fct_orders"]
    ) == ["main.fct_orders"]


@pytest.mark.parametrize("seed_kind", ["sql_script", "external"])
def test_existing_database_missing_relation_fails_and_preserves_file(
    tmp_path: Path, monkeypatch, seed_kind: str
) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DB_RESEED", "1")
    seed = {"kind": "external"} if seed_kind == "external" else None
    package_dir = write_orders_package(tmp_path, schema="", seed=seed)
    db_path = _write_db(
        package_dir / "data" / "warehouse.duckdb",
        "CREATE TABLE fct_orders AS SELECT 1 AS order_id",
    )
    before, inode = file_digest(db_path), db_path.stat().st_ino

    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)

    assert excinfo.value.code == "INVALID_CONFIG"
    assert excinfo.value.details["reason"] == "default_db_missing_relations"
    assert excinfo.value.details["missing_relations"] == ["dim_customers"]
    assert "SEMANTIC_RAILS_ALLOW_DB_RESEED" in str(excinfo.value)
    assert file_digest(db_path) == before and db_path.stat().st_ino == inode


def test_package_seeded_database_missing_new_relation_is_preserved(tmp_path: Path) -> None:
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    write_orders_package(tmp_path, schema="")  # package now reads dim_customers
    before = file_digest(db_path)
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["missing_relations"] == ["dim_customers"]
    assert file_digest(db_path) == before


def test_missing_database_is_seeded_with_provenance(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    _ensure_db(package_dir)
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute(
            "SELECT package_id FROM _semantic_rails.seed_provenance"
        ).fetchall() == [("shop",)]
        assert conn.execute("SELECT count(*) FROM fct_orders").fetchone() == (1,)
    finally:
        conn.close()
    inode = db_path.stat().st_ino
    _ensure_db(package_dir)
    assert db_path.stat().st_ino == inode


def test_creation_race_cannot_overwrite_a_new_foreign_database(tmp_path: Path, monkeypatch) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    original_build = runtime_module.build_seed_database

    def build_while_dbt_creates(*args: Any, **kwargs: Any) -> str:
        temporary = original_build(*args, **kwargs)
        _write_db(db_path, "CREATE TABLE dbt_output AS SELECT 42 AS answer")
        return temporary

    monkeypatch.setattr(runtime_module, "build_seed_database", build_while_dbt_creates)
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["missing_relations"] == ["dim_customers", "fct_orders"]
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT answer FROM dbt_output").fetchall() == [(42,)]
    finally:
        conn.close()
    assert not list(db_path.parent.glob("*.tmp"))


def test_creation_race_accepts_complete_peer_seed(tmp_path: Path, monkeypatch) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    original_build = runtime_module.build_seed_database

    def build_while_peer_publishes(*args: Any, **kwargs: Any) -> str:
        temporary = original_build(*args, **kwargs)
        seed_db(str(db_path), str(package_dir / "data" / "seed.sql"), package_id="shop")
        return temporary

    monkeypatch.setattr(runtime_module, "build_seed_database", build_while_peer_publishes)
    _ensure_db(package_dir)
    assert missing_duckdb_relations(str(db_path), ["fct_orders", "dim_customers"]) == []


def test_no_hard_links_fails_closed_on_posix(tmp_path: Path, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("Windows rename is no-clobber")
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"

    def no_hard_links(_src: str, _dst: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr(seed_provenance.os, "link", no_hard_links)
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["reason"] == "atomic_publish_unavailable"
    assert not db_path.exists()


def test_unreadable_database_is_preserved(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    db_path.write_bytes(b"not a duckdb file")
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["reason"] == "default_db_unreadable"
    assert db_path.read_bytes() == b"not a duckdb file"


def test_missing_external_database_is_reported_not_created(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, seed={"kind": "external"})
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
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
    assert any(
        "takes no source or post_sql" in issue for issue in validate_runtime_package(package_dir)
    )


def test_external_seed_validates_without_source(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, seed={"kind": "external"})
    assert load_package_config(str(package_dir)).package.seed.kind == "external"
    assert validate_runtime_package(package_dir) == []


def test_pipeline_output_is_not_required_as_stored_relation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package_dir = _write_relation_demo(tmp_path)
    _ensure_db(package_dir)
    _ensure_db(package_dir)


def test_missing_pipeline_source_is_reported_without_rebuild(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package_dir = _write_relation_demo(tmp_path)
    full_seed = (package_dir / "seed.sql").read_text(encoding="utf-8")
    partial_seed = tmp_path / "partial_seed.sql"
    partial_seed.write_text(
        "\n".join(line for line in full_seed.splitlines() if "email_sends" not in line),
        encoding="utf-8",
    )
    db_path = tmp_path / "relation_demo.duckdb"
    seed_db(str(db_path), str(partial_seed), package_id="relation_demo")
    before = file_digest(db_path)
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["missing_relations"] == ["email_sends"]
    assert file_digest(db_path) == before


def test_replaced_path_is_probed_from_fresh_catalog_even_with_open_fd(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    _ensure_db(package_dir)
    old_conn = duckdb.connect(str(db_path), read_only=True)
    replacement = tmp_path / "replacement.duckdb"
    _write_db(replacement, "CREATE TABLE fct_orders AS SELECT 1 AS order_id")
    os.replace(replacement, db_path)
    fd = os.open(db_path, os.O_RDONLY)
    try:
        with pytest.raises(SemanticLayerError) as excinfo:
            _ensure_db(package_dir)
        assert excinfo.value.details["missing_relations"] == ["dim_customers"]
    finally:
        os.close(fd)
        old_conn.close()


def test_close_during_validation_does_not_lose_a_writer_commit(tmp_path: Path, monkeypatch) -> None:
    package_dir, db_path = _seeded_orders_only_package(tmp_path)
    write_orders_package(tmp_path, schema="")
    serving = duckdb.connect(str(db_path), read_only=True)
    original_probe = runtime_module.missing_duckdb_relations

    def close_and_write(
        path: str, relations: set[str], *, confine_to: str | None = None
    ) -> list[str]:
        serving.close()  # the same-process POSIX-lock transition from the Sol review
        writer = subprocess.run(
            [
                sys.executable,
                "-c",
                "import duckdb,sys; c=duckdb.connect(sys.argv[1]); "
                "c.execute('CREATE TABLE dbt_after AS SELECT 99 AS marker'); c.close()",
                str(db_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert writer.returncode == 0, writer.stderr
        return original_probe(path, relations, confine_to=confine_to)

    monkeypatch.setattr(runtime_module, "missing_duckdb_relations", close_and_write)
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["missing_relations"] == ["dim_customers"]
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT marker FROM dbt_after").fetchall() == [(99,)]
    finally:
        conn.close()


def test_validation_keeps_serving_connections_lock(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    _ensure_db(package_dir)
    db_path = package_dir / "data" / "warehouse.duckdb"
    served = duckdb.connect(str(db_path), read_only=True)
    try:
        _ensure_db(package_dir)
        attempt = subprocess.run(
            [sys.executable, "-c", "import duckdb,sys; duckdb.connect(sys.argv[1])", str(db_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert attempt.returncode != 0 and "lock" in attempt.stderr.lower()
    finally:
        served.close()


@pytest.mark.parametrize(
    "name", ["main.duckdb", "Main.duckdb", "memory.duckdb", "information_schema.duckdb"]
)
def test_catalog_names_do_not_trigger_false_missing_relations(tmp_path: Path, name: str) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    package_yml = package_dir / "package.yml"
    doc = yaml.safe_load(package_yml.read_text(encoding="utf-8"))
    doc["package"]["default_db"] = f"data/{name}"
    package_yml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    _ensure_db(package_dir)
    db_path = package_dir / "data" / name
    inode = db_path.stat().st_ino
    _ensure_db(package_dir)
    assert db_path.stat().st_ino == inode


def test_broken_default_db_link_is_not_followed(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    db_path = package_dir / "data" / "warehouse.duckdb"
    os.symlink(tmp_path / "gone.duckdb", db_path)
    with pytest.raises(SemanticLayerError) as excinfo:
        _ensure_db(package_dir)
    assert excinfo.value.details["reason"] == "default_db_broken_link"


def test_seed_source_needed_only_when_creating_missing_file(tmp_path: Path) -> None:
    package_dir = write_orders_package(tmp_path, schema="")
    seed = package_dir / "data" / "seed.sql"
    seed.unlink()
    with pytest.raises(SemanticLayerError, match="package.seed.source"):
        _ensure_db(package_dir)
    seed.write_text(PLACEHOLDER_SEED_SQL, encoding="utf-8")
    _ensure_db(package_dir)
    seed.unlink()
    _ensure_db(package_dir)  # a complete existing file needs no seed access


def test_changed_seed_csv_is_reported_stale_until_the_file_is_deleted(tmp_path: Path) -> None:
    project = Path(
        scaffold.create_project_report(
            package_id="shop",
            workspace_root=str(tmp_path),
            entity="order",
            relation="orders",
            primary_key="order_id",
            run_checks=False,
        )["project_path"]
    )
    order_count = {
        "version": 1,
        "select": [{"expression": {"metric": "metric.shop.order_count"}, "as": "orders"}],
    }

    def run() -> tuple[int, list[str]]:
        runtime = Runtime.from_path(str(project))
        try:
            result = runtime.query(order_count)
        finally:
            runtime.close()
        return result["rows"][0]["orders"], [warning["code"] for warning in result["warnings"]]

    assert run() == (2, [])
    seed = project / "data" / "shop_csv" / "orders.csv"
    header = seed.read_text(encoding="utf-8").splitlines()[0]
    rows = "".join(f"{i},2026-01-01T09:00:00,starter,1.0\n" for i in range(1, 3001))
    seed.write_text(f"{header}\n{rows}", encoding="utf-8")
    db_path = project / "data" / "shop.duckdb"
    before = file_digest(db_path)

    assert run() == (2, ["STALE_SEED_DATABASE"])
    ref = PackageReference(source_path=str(project))
    for mode in ("runtime", "full"):
        report = project_validation_report(ref, mode=mode)
        assert report["ok"] and [warning["code"] for warning in report["warnings"]] == [
            "STALE_SEED_DATABASE"
        ], mode
        assert report["warnings"][0]["message"].endswith(f"rm {db_path}")
    assert file_digest(db_path) == before  # reported, never replaced

    db_path.unlink()  # the fix the warning names
    assert run() == (3000, [])
