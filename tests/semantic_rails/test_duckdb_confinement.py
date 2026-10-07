"""DuckDB and DuckLake connections confined to one directory with ``confine_to``."""

from __future__ import annotations

import json
import os
import shutil
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import _duckdb
import duckdb
import pytest

from semantic_rails import config as config_module
from semantic_rails import seed_provenance
from semantic_rails.config import load_package_snapshot
from semantic_rails.db import Database, DuckDBAdapter, create_warehouse_adapter
from semantic_rails.db_parts.duckdb_confinement import confine_duckdb
from semantic_rails.db_parts.ducklake import DuckLakeAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, PackageMeta
from tests.semantic_rails.dbt_warehouse import (
    ORDER_COUNT_QUERY,
    build_dbt_warehouse,
    write_orders_package,
)

FILE_ACCESS = "disabled by configuration"
EXTENSION_LOAD = "disabled through configuration"
LOCKED = "configuration has been locked"

# Statements a confined connection refuses, with the reason DuckDB gives.
REFUSED = [
    ("read_csv", "SELECT * FROM read_csv('{outside}/rows.csv')", FILE_ACCESS),
    ("read_parquet", "SELECT * FROM read_parquet('{outside}/rows.parquet')", FILE_ACCESS),
    ("file_scan", "SELECT * FROM '{outside}/rows.csv'", FILE_ACCESS),
    ("read_text", "SELECT * FROM read_text('{outside}/rows.csv')", FILE_ACCESS),
    ("glob", "SELECT * FROM glob('{outside}/*')", FILE_ACCESS),
    ("glob_root", "SELECT * FROM glob('/*')", FILE_ACCESS),
    ("dot_dot", "SELECT * FROM read_csv('{inside}/../outside/rows.csv')", FILE_ACCESS),
    ("attach", "ATTACH '{outside}/other.duckdb' AS other (READ_ONLY)", FILE_ACCESS),
    ("copy_to", "COPY (SELECT 1 AS x) TO '{outside}/written.csv'", FILE_ACCESS),
    ("export", "EXPORT DATABASE '{outside}/export'", FILE_ACCESS),
    ("install", "INSTALL httpfs", FILE_ACCESS),
    ("load", "LOAD httpfs", EXTENSION_LOAD),
    (
        "persistent_secret",
        "CREATE PERSISTENT SECRET leaked (TYPE ducklake, METADATA_PATH '{outside}/x.ducklake')",
        FILE_ACCESS,
    ),
    ("reopen_access", "SET enable_external_access = true", LOCKED),
    ("widen_directories", "SET allowed_directories = ['/']", LOCKED),
    ("unlock_settings", "SET allowed_configs = ['enable_external_access']", LOCKED),
    ("autoload", "SET autoload_known_extensions = true", LOCKED),
    ("move_spill", "SET temp_directory = '{outside}'", LOCKED),
    ("unlock", "SET lock_configuration = false", LOCKED),
]

# The same reads and writes succeed on an unconfined connection (OSS default).
UNCONFINED = [
    ("read_csv", "SELECT * FROM read_csv('{outside}/rows.csv')"),
    ("read_parquet", "SELECT * FROM read_parquet('{outside}/rows.parquet')"),
    ("glob", "SELECT * FROM glob('{outside}/*')"),
    ("attach", "ATTACH '{outside}/other.duckdb' AS other (READ_ONLY)"),
    ("copy_to", "COPY (SELECT 1 AS x) TO '{outside}/written.csv'"),
    ("autoload", "SET autoload_known_extensions = true"),
]


def _sql(template: str, paths: SimpleNamespace) -> str:
    return template.format(inside=paths.inside, outside=paths.outside)


@pytest.fixture
def paths(tmp_path: Path) -> SimpleNamespace:
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    (inside / "rows.csv").write_text("x\n2\n", encoding="utf-8")
    (outside / "rows.csv").write_text("x\n1\n", encoding="utf-8")
    db = inside / "warehouse.duckdb"
    conn = duckdb.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE orders AS SELECT range AS id, "
            "TIMESTAMPTZ '2024-01-01 03:00:00+00' AS ordered_at FROM range(3)"
        )
        conn.execute(f"COPY (SELECT 1 AS x) TO '{outside / 'rows.parquet'}' (FORMAT parquet)")
    finally:
        conn.close()
    other = duckdb.connect(str(outside / "other.duckdb"))
    try:
        other.execute("CREATE TABLE secret AS SELECT 42 AS x")
    finally:
        other.close()
    return SimpleNamespace(inside=inside, outside=outside, db=db)


@pytest.fixture
def confined(paths: SimpleNamespace):
    adapter = DuckDBAdapter(str(paths.db), confine_to=paths.inside)
    yield adapter
    adapter.close()


@pytest.mark.parametrize("path", ["md:warehouse", "ducklake:catalog", "file:warehouse", ":memory:"])
def test_confined_duckdb_requires_file_paths_before_connecting(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.chdir(paths.inside)
    monkeypatch.setattr(Database, "connect", lambda *a, **k: pytest.fail("connected"))
    with pytest.raises(SemanticLayerError) as exc:
        DuckDBAdapter(path, confine_to=paths.inside)
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {
        "reason": "duckdb_path_not_file",
        "option": "database path",
    }


@pytest.mark.parametrize("linked", [False, True], ids=["relative", "link_inside"])
def test_confined_duckdb_connects_with_the_validated_real_path(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    monkeypatch.chdir(paths.inside)
    path = paths.db.name
    if linked:
        path = "linked.duckdb"
        (paths.inside / path).symlink_to(paths.db)
    connect = Database.connect
    opened = []

    def record(db_path: str, **kwargs: Any) -> Database:
        opened.append(db_path)
        return connect(db_path, **kwargs)

    monkeypatch.setattr(Database, "connect", record)
    adapter = DuckDBAdapter(path, confine_to=paths.inside)
    try:
        assert opened == [os.path.realpath(paths.db)]
        assert adapter.query("SELECT count(*) AS n FROM orders") == [{"n": 3}]
    finally:
        adapter.close()


@pytest.mark.parametrize("constructor", ["package_id", "snapshot"])
def test_runtime_preserves_confinement_after_reload_and_close(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, constructor: str
) -> None:
    package = write_orders_package(paths.inside)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    if constructor == "package_id":
        monkeypatch.setattr(config_module, "list_package_paths", lambda: {"shop": str(package)})
        runtime = Runtime("shop", confine_to=paths.inside)
    else:
        runtime = Runtime.from_snapshot(
            load_package_snapshot(str(package)), confine_to=paths.inside
        )
    try:
        for reset in (None, runtime.reload, runtime.close):
            if reset:
                reset()
            result = runtime.query(ORDER_COUNT_QUERY)
            assert result["status"] == "ok"
            assert runtime._get_adapter().query(  # noqa: SLF001
                "SELECT current_setting('enable_external_access') AS enabled"
            ) == [{"enabled": False}]
    finally:
        runtime.close()


@pytest.mark.parametrize("constructor", ["package_id", "snapshot"])
@pytest.mark.parametrize("outside_exists", [True, False], ids=["existing", "missing"])
def test_confined_runtime_refuses_outside_views_when_reconnecting(
    paths: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    constructor: str,
    outside_exists: bool,
) -> None:
    package = write_orders_package(paths.inside, schema="", with_customers=False)
    csv = paths.outside / "orders.csv"
    contents = "order_id,ordered_at,status,order_total\n1,2024-01-01,placed,10\n"
    csv.write_text(contents, encoding="utf-8")
    db = package / "data" / "warehouse.duckdb"
    conn = duckdb.connect(str(db))
    try:
        csv_literal = str(csv).replace("'", "''")
        conn.execute(f"CREATE VIEW fct_orders AS SELECT * FROM read_csv('{csv_literal}')")
    finally:
        conn.close()
    if constructor == "package_id":
        monkeypatch.setattr(config_module, "list_package_paths", lambda: {"shop": str(package)})
        runtime = Runtime("shop", confine_to=paths.inside)
    else:
        runtime = Runtime.from_snapshot(
            load_package_snapshot(str(package)), confine_to=paths.inside
        )
    monkeypatch.setattr(
        "semantic_rails.runtime.create_warehouse_adapter",
        lambda *a, **k: pytest.fail("opened serving adapter"),
    )
    refusal = None
    try:
        for reset in (None, runtime.reload, runtime.close):
            if reset:
                reset()
            for exists in (outside_exists, not outside_exists):
                if exists:
                    csv.write_text(contents, encoding="utf-8")
                else:
                    csv.unlink(missing_ok=True)
                with pytest.raises(SemanticLayerError) as exc:
                    runtime._get_adapter()  # noqa: SLF001
                assert exc.value.code == "INVALID_CONFIG"
                assert exc.value.details == {
                    "default_db": str(db),
                    "missing_relations": ["fct_orders"],
                    "reason": "default_db_missing_relations",
                }
                assert str(paths.outside) not in str(exc.value)
                current = (exc.value.code, exc.value.details, str(exc.value))
                if refusal is None:
                    refusal = current
                assert current == refusal
                assert runtime.adapter is None
    finally:
        runtime.close()


@pytest.mark.parametrize("constructor", ["package_id", "snapshot"])
def test_confined_runtime_never_builds_a_missing_seed(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, constructor: str
) -> None:
    package = write_orders_package(paths.inside)
    monkeypatch.setattr(Runtime, "_publish_seed", lambda *a: pytest.fail("built seed"))
    if constructor == "package_id":
        monkeypatch.setattr(config_module, "list_package_paths", lambda: {"shop": str(package)})
        runtime = Runtime("shop", confine_to=paths.inside)
    else:
        runtime = Runtime.from_snapshot(
            load_package_snapshot(str(package)), confine_to=paths.inside
        )
    before = sorted(package.rglob("*"))
    try:
        with pytest.raises(SemanticLayerError) as exc:
            runtime._get_adapter()  # noqa: SLF001
        assert exc.value.code == "INVALID_CONFIG"
        assert exc.value.details == {"reason": "duckdb_confined_default_db_missing"}
        assert runtime.adapter is None
        assert not Path(runtime.db_path).exists()
        assert sorted(package.rglob("*")) == before
    finally:
        runtime.close()


def test_catalog_probe_refuses_before_binding_when_confinement_fails(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    closed = []
    conn = SimpleNamespace(
        close=lambda: closed.append(True),
        execute=lambda sql: SimpleNamespace(fetchone=lambda: ("common_subplan",)),
    )

    def connect(path: str, *, read_only: bool) -> Any:
        assert path == str(paths.db)
        assert read_only is True
        return conn

    def refuse(connection: Any, directory: str) -> None:
        assert connection is conn
        assert directory == str(paths.inside)
        raise duckdb.InvalidInputException("driver text")

    monkeypatch.setattr(
        seed_provenance.sys,
        "stdin",
        StringIO(
            json.dumps(
                {"path": str(paths.db), "relations": ["orders"], "confine_to": str(paths.inside)}
            )
        ),
    )
    monkeypatch.setattr(seed_provenance.duckdb, "connect", connect)
    monkeypatch.setattr(seed_provenance, "confine_duckdb", refuse)
    monkeypatch.setattr(
        seed_provenance, "_missing_on_connection", lambda *a: pytest.fail("bound relations")
    )
    seed_provenance._probe_cli()  # noqa: SLF001
    assert json.loads(capsys.readouterr().out) == {"error": "InvalidInputException"}
    assert closed == [True]


def _assert_refused(adapter: Any, sql: str, reason: str) -> None:
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query(sql)
    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    assert isinstance(exc.value.__cause__, duckdb.Error)
    assert reason in str(exc.value.__cause__)


@pytest.mark.parametrize(("name", "template", "reason"), REFUSED, ids=[r[0] for r in REFUSED])
def test_confined_duckdb_refuses_access_outside_its_directory(
    confined: DuckDBAdapter, paths: SimpleNamespace, name: str, template: str, reason: str
) -> None:
    before = sorted(os.listdir(paths.outside))
    _assert_refused(confined, _sql(template, paths), reason)
    assert sorted(os.listdir(paths.outside)) == before
    assert confined.query("SELECT count(*) AS n FROM orders") == [{"n": 3}]


def test_confined_duckdb_still_answers_queries_on_its_own_files(
    confined: DuckDBAdapter, paths: SimpleNamespace
) -> None:
    assert confined.query("SELECT count(*) AS n FROM orders WHERE id >= ?", parameters=[1]) == [
        {"n": 2}
    ]
    assert confined.query(f"SELECT x FROM read_csv('{paths.inside}/rows.csv')") == [{"x": 2}]
    # Each query still runs in its time role's zone.
    zoned = confined.query(
        "SELECT ordered_at::VARCHAR AS t FROM orders LIMIT 1", limits={"time_zone": "Asia/Tokyo"}
    )
    assert zoned == [{"t": "2024-01-01 12:00:00+09"}]


def test_confined_duckdb_reports_every_setting_in_effect(
    confined: DuckDBAdapter, paths: SimpleNamespace
) -> None:
    conn = confined._db.conn  # noqa: SLF001

    def setting(name: str) -> Any:
        return conn.execute("SELECT current_setting(?)", [name]).fetchone()[0]

    directory = os.path.realpath(paths.inside)
    assert setting("enable_external_access") is False
    assert setting("autoinstall_known_extensions") is False
    assert setting("autoload_known_extensions") is False
    assert setting("lock_configuration") is True
    assert setting("allowed_configs") == ["TimeZone"]
    assert setting("allowed_directories")[0] == directory + os.sep
    # The test hook spills elsewhere; a confined instance moves its spill inside.
    assert os.path.commonpath([setting("temp_directory"), directory]) == directory


@pytest.mark.parametrize(("name", "template"), UNCONFINED, ids=[r[0] for r in UNCONFINED])
def test_unconfined_duckdb_keeps_full_file_access(
    paths: SimpleNamespace, name: str, template: str
) -> None:
    adapter = DuckDBAdapter(str(paths.db))
    try:
        adapter.query(_sql(template, paths))
    finally:
        adapter.close()
    if name == "copy_to":
        assert (paths.outside / "written.csv").is_file()


@pytest.mark.parametrize(
    "directory",
    [lambda paths: "inside", lambda paths: paths.inside / "missing", lambda paths: paths.db],
    ids=["relative", "missing", "file"],
)
def test_confinement_needs_an_existing_absolute_directory(
    paths: SimpleNamespace, directory: Any
) -> None:
    with pytest.raises(SemanticLayerError) as exc:
        DuckDBAdapter(str(paths.db), confine_to=directory(paths))
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"reason": "duckdb_confinement_directory_invalid"}


@pytest.mark.parametrize("via_symlink", [False, True], ids=["outside", "symlink_inside"])
def test_confinement_refuses_a_database_outside_the_directory_before_opening_it(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, via_symlink: bool
) -> None:
    db = paths.outside / "other.duckdb"
    if via_symlink:
        link = paths.inside / "link.duckdb"
        link.symlink_to(db)
        db = link
    opened: list[str] = []
    monkeypatch.setattr(
        Database, "connect", classmethod(lambda cls, path, **kw: opened.append(path))
    )

    with pytest.raises(SemanticLayerError) as exc:
        DuckDBAdapter(str(db), confine_to=paths.inside)

    assert exc.value.details == {
        "reason": "duckdb_path_outside_confinement",
        "option": "database path",
    }
    assert str(paths.outside) not in str(exc.value)
    assert opened == []


class _ReportingConnection:
    """Accepts every statement and reports fixed settings, like a DuckDB that ignored one."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.statements: list[str] = []

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        if sql == "SELECT system.main.current_setting(?)":
            value = self.settings[parameters[0]]
            return SimpleNamespace(fetchone=lambda: (value,))
        self.statements.append(sql)
        return self


def _held_settings(directory: str) -> dict[str, Any]:
    return {
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "lock_configuration": True,
        "allowed_configs": ["TimeZone"],
        "allowed_directories": [directory + os.sep],
        "allowed_paths": [],
        "temp_directory": "",
    }


@pytest.mark.parametrize(
    ("setting", "reported"),
    [
        ("enable_external_access", True),
        ("autoinstall_known_extensions", True),
        ("autoload_known_extensions", True),
        ("lock_configuration", False),
        ("allowed_configs", ["TimeZone", "enable_external_access"]),
        ("allowed_directories", ["/"]),
        ("allowed_paths", ["/etc/passwd"]),
    ],
)
def test_a_setting_that_did_not_take_effect_refuses(
    tmp_path: Path, setting: str, reported: Any
) -> None:
    directory = os.path.realpath(tmp_path)
    conn = _ReportingConnection({**_held_settings(directory), setting: reported})

    with pytest.raises(SemanticLayerError) as exc:
        confine_duckdb(conn, directory)

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"reason": "duckdb_confinement_failed", "setting": setting}


def test_a_locked_instance_is_checked_not_changed(tmp_path: Path) -> None:
    directory = os.path.realpath(tmp_path)
    conn = _ReportingConnection(_held_settings(directory))

    confine_duckdb(conn, directory)

    assert conn.statements == []


def test_a_setting_duckdb_rejects_refuses_without_its_message(tmp_path: Path) -> None:
    class Rejecting(_ReportingConnection):
        def execute(self, sql: str, parameters: Any = ()) -> Any:
            if sql.startswith("SET enable_external_access"):
                raise duckdb.InvalidInputException("driver text")
            return super().execute(sql, parameters)

    directory = os.path.realpath(tmp_path)
    conn = Rejecting({**_held_settings(directory), "lock_configuration": False})

    with pytest.raises(SemanticLayerError) as exc:
        confine_duckdb(conn, directory)

    assert exc.value.details == {"reason": "duckdb_confinement_failed"}
    assert "driver text" not in str(exc.value)


def test_adapters_sharing_one_file_hold_the_narrowest_directory(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DuckDB shares one instance between a process's connections to a file. The
    # test hook passes a config on every connect, which DuckDB refuses for a
    # shared instance, so this test opens files as a host does.
    monkeypatch.setattr(duckdb, "connect", _duckdb.connect)
    root = paths.inside.parent
    first = DuckDBAdapter(str(paths.db), confine_to=paths.inside)
    try:
        # A wider directory on top: the shared instance stays at the narrower one.
        second = DuckDBAdapter(str(paths.db), confine_to=root)
        try:
            allowed = second.query("SELECT current_setting('allowed_directories') AS a")[0]["a"]
            assert allowed[0] == os.path.realpath(paths.inside) + os.sep
            assert second.query("SELECT count(*) AS n FROM orders") == [{"n": 3}]
        finally:
            second.close()
    finally:
        first.close()

    # A narrower directory on top of a wider one refuses (another file, another instance).
    copy = paths.inside / "copy.duckdb"
    shutil.copyfile(paths.db, copy)
    wide = DuckDBAdapter(str(copy), confine_to=root)
    try:
        with pytest.raises(SemanticLayerError) as exc:
            DuckDBAdapter(str(copy), confine_to=paths.inside)
        assert exc.value.details == {
            "reason": "duckdb_confinement_failed",
            "setting": "allowed_directories",
        }
        assert wide.query("SELECT count(*) AS n FROM orders") == [{"n": 3}]
    finally:
        wide.close()


def _package(warehouse: str, kind: str, options: dict[str, str] | None = None) -> PackageMeta:
    return PackageMeta(
        package_id="confined",
        name="confined",
        description="confined",
        warehouse=warehouse,
        connection=ConnectionSpec(kind=kind, options=options or {}),
    )


@pytest.mark.parametrize(
    "package",
    [
        _package("motherduck", "motherduck_native", {"database": "d", "token_env": "T"}),
        _package("snowflake", "snowflake_cli"),
    ],
    ids=["motherduck", "snowflake"],
)
def test_create_warehouse_adapter_refuses_confinement_it_cannot_apply(
    paths: SimpleNamespace, package: PackageMeta
) -> None:
    with pytest.raises(SemanticLayerError) as exc:
        create_warehouse_adapter(package, confine_to=paths.inside)
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {
        "reason": "duckdb_confinement_unsupported",
        "warehouse": package.warehouse,
    }


def test_create_warehouse_adapter_passes_confinement_to_duckdb_and_ducklake(
    paths: SimpleNamespace,
) -> None:
    adapter = create_warehouse_adapter(
        _package("duckdb", "duckdb_native"), db_path=str(paths.db), confine_to=paths.inside
    )
    try:
        _assert_refused(adapter, _sql(REFUSED[0][1], paths), FILE_ACCESS)
    finally:
        adapter.close()
    lake = create_warehouse_adapter(
        _package("ducklake", "ducklake_native", {"catalog_path": str(paths.inside / "c.ducklake")}),
        confine_to=paths.inside,
    )
    assert isinstance(lake, DuckLakeAdapter)
    assert lake._confine_to == os.path.realpath(paths.inside)  # noqa: SLF001


@pytest.mark.parametrize("option", ["catalog_path", "data_path"])
def test_ducklake_refuses_paths_outside_the_directory_before_connecting(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: pytest.fail("connected"))
    options = {
        "catalog_path": str(paths.inside / "lake" / "c.ducklake"),
        option: str(paths.outside / "lake" / option),
    }
    adapter = DuckLakeAdapter(options, confine_to=paths.inside)

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT 1")

    assert exc.value.details == {"reason": "duckdb_path_outside_confinement", "option": option}
    assert not (paths.outside / "lake").exists()


@pytest.mark.parametrize("option", ["catalog_path", "data_path"])
@pytest.mark.parametrize("path", ["md:warehouse", "ducklake:catalog", "file:warehouse", ":memory:"])
def test_confined_ducklake_requires_file_paths_before_connecting(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, option: str, path: str
) -> None:
    monkeypatch.setattr("semantic_rails.db_parts.ducklake.repo_root", lambda: str(paths.inside))
    connected = []

    def execute(sql: str) -> None:
        if sql.startswith("ATTACH"):
            pytest.fail("attached")
        assert sql in {"INSTALL ducklake", "LOAD ducklake"}

    def connect() -> SimpleNamespace:
        connected.append(True)
        return SimpleNamespace(execute=execute, close=lambda: None)

    monkeypatch.setattr(duckdb, "connect", connect)
    adapter = DuckLakeAdapter(
        {"catalog_path": str(paths.inside / "catalog.ducklake"), option: path},
        confine_to=paths.inside,
    )
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT 1")
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {"reason": "duckdb_path_not_file", "option": option}
    assert not connected


def test_confined_ducklake_resolves_relative_file_paths_and_links(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("semantic_rails.db_parts.ducklake.repo_root", lambda: str(paths.inside))
    (paths.inside / "linked").symlink_to(paths.inside, target_is_directory=True)
    adapter = DuckLakeAdapter(
        {"catalog_path": "linked/catalog.ducklake", "data_path": "linked/data"},
        confine_to=paths.inside,
    )
    assert adapter._resolve_paths() == (  # noqa: SLF001
        str(paths.inside / "catalog.ducklake"),
        str(paths.inside / "data"),
    )


@pytest.mark.parametrize("statement", ["INSTALL ducklake", "LOAD ducklake"])
def test_ducklake_extension_is_required_in_ci(
    paths: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    statement: str,
) -> None:
    class Unavailable:
        def execute(self, sql: str) -> None:
            if sql == statement:
                raise duckdb.Error("unavailable")
            assert sql == "INSTALL ducklake"

        def close(self) -> None:
            pass

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(duckdb, "connect", lambda: Unavailable())
    with pytest.raises(pytest.fail.Exception, match="required in CI"):
        request.getfixturevalue("lake")


@pytest.fixture
def lake(paths: SimpleNamespace) -> SimpleNamespace:
    """A DuckLake catalog inside the directory, with one table stored as parquet."""
    conn = duckdb.connect()
    try:
        try:
            conn.execute("INSTALL ducklake")
            conn.execute("LOAD ducklake")
        except duckdb.Error as exc:
            if os.environ.get("CI"):
                pytest.fail(f"the ducklake extension is required in CI: {type(exc).__name__}")
            pytest.skip(f"the ducklake extension is unavailable here: {type(exc).__name__}")
        catalog = paths.inside / "lake.ducklake"
        data = paths.inside / "lake_files"
        conn.execute(f"ATTACH 'ducklake:{catalog}' AS lake (DATA_PATH '{data}')")
        conn.execute("CREATE TABLE lake.orders AS SELECT range AS id FROM range(100000)")
    finally:
        conn.close()
    return SimpleNamespace(catalog=catalog, data=data)


@pytest.fixture
def confined_lake(paths: SimpleNamespace, lake: SimpleNamespace):
    adapter = DuckLakeAdapter(
        {"catalog_path": str(lake.catalog), "data_path": str(lake.data)}, confine_to=paths.inside
    )
    yield adapter
    adapter.close()


LAKE_REFUSED = [
    *REFUSED,
    ("attach_lake", "ATTACH 'ducklake:{outside}/stolen.ducklake' AS stolen", FILE_ACCESS),
]


@pytest.mark.parametrize(
    ("name", "template", "reason"), LAKE_REFUSED, ids=[r[0] for r in LAKE_REFUSED]
)
def test_confined_ducklake_refuses_access_outside_its_directory(
    confined_lake: DuckLakeAdapter,
    paths: SimpleNamespace,
    name: str,
    template: str,
    reason: str,
) -> None:
    before = sorted(os.listdir(paths.outside))
    _assert_refused(confined_lake, _sql(template, paths), reason)
    assert sorted(os.listdir(paths.outside)) == before
    # A sum reads the parquet files under the data path, not catalog statistics.
    assert confined_lake.query("SELECT sum(id) AS s FROM orders") == [{"s": 4999950000}]


def test_confined_ducklake_secret_cannot_reach_outside(
    confined_lake: DuckLakeAdapter, paths: SimpleNamespace
) -> None:
    # The extension sets up secret storage while attaching, so an in-memory
    # secret can still be declared; nothing it names outside can be opened.
    confined_lake.query(
        f"CREATE SECRET outside (TYPE ducklake, METADATA_PATH '{paths.outside}/x.ducklake')"
    )
    _assert_refused(confined_lake, "ATTACH 'ducklake:outside' AS stolen", FILE_ACCESS)


def test_confined_ducklake_refuses_data_files_its_catalog_keeps_outside(
    paths: SimpleNamespace, lake: SimpleNamespace
) -> None:
    conn = duckdb.connect()
    try:
        conn.execute("LOAD ducklake")
        conn.execute(f"ATTACH 'ducklake:{lake.catalog}' AS lake")
        conn.execute(
            "UPDATE __ducklake_metadata_lake.ducklake_data_file "
            "SET path = ?, path_is_relative = false",
            [str(paths.outside / "rows.parquet")],
        )
    finally:
        conn.close()
    adapter = DuckLakeAdapter({"catalog_path": str(lake.catalog)}, confine_to=paths.inside)
    try:
        _assert_refused(adapter, "SELECT sum(id) AS s FROM orders", FILE_ACCESS)
    finally:
        adapter.close()
