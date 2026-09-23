"""Who built a DuckDB warehouse file, and may the runtime replace it?

The runtime builds a DuckDB package's ``default_db`` from ``package.seed`` when
it is missing. It must never replace a file its package's seed did not build:
a dbt project, a loader or a person may own it. This module keeps that promise:

- :func:`record_seed_provenance` writes, inside a freshly built seed file, the
  package id and a canonical inventory of everything the file holds.
- :func:`seeded_database_unchanged` proves a file is that package's seed output
  and that nothing has changed it since. Anything it cannot prove is False.
- :func:`missing_duckdb_relations` resolves relation names the way compiled SQL
  does, so schema-qualified relations and views count.
- :func:`hold_database` opens a file as it is now on disk (not a copy this
  process opened before it was replaced) and keeps a shared lock on it, so a
  writer that starts meanwhile fails instead of losing its work;
  :func:`rebuild_lock` serializes rebuilds of one file across threads and
  processes, and
  :func:`publish_seed_database` replaces only the file that was judged and never
  overwrites a file that appeared while a seed was being built.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

import duckdb

from .errors import SemanticLayerError

DB_RESEED_ENV = "SEMANTIC_RAILS_ALLOW_DB_RESEED"
_PROVENANCE_SCHEMA = "_semantic_rails"
_PROVENANCE_TABLE_NAME = "seed_provenance"
_PROVENANCE_TABLE = f"{_PROVENANCE_SCHEMA}.{_PROVENANCE_TABLE_NAME}"
# Bump when the inventory changes: a file recorded under another format never
# counts as unchanged, so the runtime refuses rather than guesses.
_INVENTORY_FORMAT = 2
# Catalog sections the inventory cannot fingerprint completely (DuckDB does not
# report macro default arguments or the definition of a user-defined type). A
# file holding any of them never counts as unchanged.
_UNFINGERPRINTED_SECTIONS = ("macros", "types")
REBUILD_LOCK_TIMEOUT_SECONDS = 120.0
# os.link errors meaning "this filesystem has no hard links" (FAT, some network
# and FUSE filesystems), as opposed to a real failure.
_NO_HARD_LINK_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EXDEV", "EMLINK")
    )
    if code is not None
)


def db_reseed_allowed() -> bool:
    """Whether the operator opted in to replacing a database the package's seed did not build."""
    return os.environ.get(DB_RESEED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_relation(relation: str) -> str:
    # Split on dots like the SQL renderer; DuckDB matches quoted names
    # case-insensitively, so this resolves exactly what compiled SQL reads.
    return ".".join(_quote_identifier(part) for part in relation.split("."))


def _rows(conn: Any, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, list(params))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def missing_duckdb_relations(db: Any, relations: Iterable[str]) -> list[str]:
    """Return the relations a DuckDB file cannot resolve, probed as compiled SQL reads them.

    ``db`` is a connection from :func:`hold_database`, or a path, which is opened
    that way. Raises if the file cannot be opened read-only.
    """
    if isinstance(db, (str, os.PathLike)):
        with hold_database(os.fspath(db)) as (conn, _identity):
            return missing_duckdb_relations(conn, relations)
    missing: list[str] = []
    for relation in sorted(set(relations)):
        try:
            db.execute(f"SELECT 1 FROM {_quote_relation(relation)} LIMIT 0")
        except Exception:  # noqa: BLE001 — any binder/catalog error means "not resolvable"
            missing.append(relation)
    return missing


# Every persisted catalog object of the open file except the provenance table
# itself. Only stable columns: no OIDs, no database name (a seed is built under
# a temporary file name) and no size estimates.
_IN_FILE = "database_name = current_database()"
_NOT_PROVENANCE = (
    f"NOT (schema_name = '{_PROVENANCE_SCHEMA}' AND table_name = '{_PROVENANCE_TABLE_NAME}')"
)
_CATALOG_QUERIES = {
    "schemas": "SELECT schema_name, comment, sql FROM duckdb_schemas() "
    f"WHERE {_IN_FILE} AND NOT internal AND schema_name <> '{_PROVENANCE_SCHEMA}'",
    "tables": "SELECT schema_name, table_name, comment, sql FROM duckdb_tables() "
    f"WHERE {_IN_FILE} AND NOT internal AND NOT temporary AND {_NOT_PROVENANCE}",
    "views": "SELECT schema_name, view_name, comment, sql FROM duckdb_views() "
    f"WHERE {_IN_FILE} AND NOT internal AND NOT temporary",
    "columns": "SELECT schema_name, table_name, column_index, column_name, data_type, "
    "is_nullable, column_default, comment FROM duckdb_columns() "
    f"WHERE {_IN_FILE} AND NOT internal AND {_NOT_PROVENANCE}",
    "constraints": "SELECT schema_name, table_name, constraint_index, constraint_type, "
    f"constraint_text FROM duckdb_constraints() WHERE {_IN_FILE} AND {_NOT_PROVENANCE}",
    "indexes": "SELECT schema_name, table_name, index_name, is_unique, is_primary, "
    f"expressions, comment, sql FROM duckdb_indexes() WHERE {_IN_FILE}",
    "sequences": "SELECT schema_name, sequence_name, start_value, min_value, max_value, "
    "increment_by, cycle, last_value, comment, sql FROM duckdb_sequences() "
    f"WHERE {_IN_FILE} AND NOT temporary",
    "macros": "SELECT schema_name, function_name, function_type, parameters, "
    "macro_definition, comment FROM duckdb_functions() "
    f"WHERE {_IN_FILE} AND NOT internal AND function_type IN ('macro', 'table_macro')",
    "types": "SELECT schema_name, type_name, logical_type, labels, comment FROM duckdb_types() "
    f"WHERE {_IN_FILE} AND NOT internal",
}


def _catalog(conn: Any) -> dict[str, list[dict[str, Any]]]:
    """Every catalog object (schemas, tables, views, columns with defaults and
    nullability, constraints, indexes, sequences with their positions, macros,
    types, comments), canonically ordered and JSON-normalized."""
    catalog = {
        section: _rows(conn, f"{sql} ORDER BY ALL") for section, sql in _CATALOG_QUERIES.items()
    }
    normalized: dict[str, list[dict[str, Any]]] = json.loads(
        json.dumps(catalog, sort_keys=True, default=str)
    )
    return normalized


def _contents(
    conn: Any, catalog: dict[str, list[dict[str, Any]]], *, hashed: bool
) -> list[list[Any]]:
    """Each table's row count and, when ``hashed``, an order-independent sum of row hashes.

    Rows are hashed from their explicit column list, so no column name can
    shadow the row being hashed.
    """
    columns: dict[tuple[str, str], list[str]] = {}
    for column in catalog["columns"]:
        key = (str(column["schema_name"]), str(column["table_name"]))
        columns.setdefault(key, []).append(_quote_identifier(str(column["column_name"])))
    contents: list[list[Any]] = []
    for table in catalog["tables"]:
        schema, name = str(table["schema_name"]), str(table["table_name"])
        relation = f"{_quote_identifier(schema)}.{_quote_identifier(name)}"
        if hashed:
            row_hash = f"sum(hash(row({', '.join(columns[(schema, name)])})))"
            count, content_hash = conn.execute(
                f"SELECT count(*), {row_hash} FROM {relation}"
            ).fetchone()
            contents.append([schema, name, int(count), str(content_hash)])
        else:
            (count,) = conn.execute(f"SELECT count(*) FROM {relation}").fetchone()
            contents.append([schema, name, int(count)])
    return contents


def record_seed_provenance(db_path: str, package_id: str) -> None:
    """Record, in the closed seed file at ``db_path``, that ``package_id``'s seed built it.

    The inventory is read over a fresh read-only connection, the way
    :func:`seeded_database_unchanged` reads it later: DuckDB reports some state
    (sequence positions) differently inside the session that wrote it. When it
    cannot be read, the record stores none and the file never counts as unchanged.
    """
    inventory: str | None
    try:
        with hold_database(db_path) as (reader, _identity):
            catalog = _catalog(reader)
            inventory = json.dumps(
                {"catalog": catalog, "contents": _contents(reader, catalog, hashed=True)},
                sort_keys=True,
            )
    except Exception:  # noqa: BLE001 — fail closed: no inventory, never "unchanged"
        inventory = None
    conn = duckdb.connect(db_path)
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_PROVENANCE_SCHEMA}")
        conn.execute(
            f"CREATE OR REPLACE TABLE {_PROVENANCE_TABLE} "
            "(format INTEGER, package_id VARCHAR, duckdb_version VARCHAR, inventory VARCHAR)"
        )
        conn.execute(
            f"INSERT INTO {_PROVENANCE_TABLE} VALUES (?, ?, ?, ?)",
            [_INVENTORY_FORMAT, package_id, duckdb.__version__, inventory],
        )
    finally:
        conn.close()


_REFUSED: set[tuple[Any, ...]] = set()
_MAX_REFUSED = 256


def _file_identity(db_path: str) -> tuple[Any, ...] | None:
    try:
        stat = os.stat(db_path)
    except OSError:
        return None
    try:
        wal = os.stat(f"{db_path}.wal")
        wal_identity: tuple[int, int] | None = (wal.st_mtime_ns, wal.st_size)
    except OSError:
        wal_identity = None
    return (
        os.path.realpath(db_path),
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_size,
        wal_identity,
    )


def seeded_database_unchanged(db_path: str, package_id: str, *, conn: Any = None) -> bool:
    """True only when ``package_id``'s seed built this file and nothing has changed it since.

    ``conn`` is the :func:`hold_database` connection to judge through; without
    one the file is opened that way. Anything that prevents proving it (an
    unreadable file, a missing or foreign provenance record, another inventory
    format, a failing catalog query) is False. The cheap catalog and row counts
    are compared before any row is hashed, and a False verdict is cached per file
    identity so a refused file is not rescanned on every request. A True verdict
    is never cached: a stale one could allow a rebuild over a change the
    identity missed.
    """
    identity = _file_identity(db_path)
    key = (identity, package_id)
    if identity is not None and key in _REFUSED:
        return False
    if conn is not None:
        verdict = _judge(conn, package_id)
    else:
        try:
            with hold_database(db_path) as (opened, _identity):
                verdict = _judge(opened, package_id)
        except Exception:  # noqa: BLE001 — an unreadable file is never provably ours
            verdict = False
    if not verdict and identity is not None:
        if len(_REFUSED) >= _MAX_REFUSED:
            _REFUSED.clear()
        _REFUSED.add(key)
    return verdict


def _judge(conn: Any, package_id: str) -> bool:
    try:
        records = _rows(conn, f"SELECT format, package_id, inventory FROM {_PROVENANCE_TABLE}")
        if len(records) != 1:
            return False
        record = records[0]
        if record["format"] != _INVENTORY_FORMAT or str(record["package_id"]) != package_id:
            return False
        if record["inventory"] is None:
            return False
        recorded = json.loads(str(record["inventory"]))
        catalog = _catalog(conn)
        if any(catalog[section] for section in _UNFINGERPRINTED_SECTIONS):
            return False
        if catalog != recorded["catalog"]:
            return False
        counts = [entry[:3] for entry in recorded["contents"]]
        if _contents(conn, catalog, hashed=False) != counts:
            return False
        return bool(_contents(conn, catalog, hashed=True) == recorded["contents"])
    except Exception:  # noqa: BLE001 — no provenance table: another tool built the file
        return False


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _rebuild_busy(db_path: str) -> SemanticLayerError:
    return SemanticLayerError(
        "CONFIG_CONFLICT",
        f"another process is rebuilding package.default_db '{db_path}'; retry",
        details={"default_db": db_path, "reason": "default_db_rebuild_in_progress"},
    )


def _lock_unavailable(db_path: str, exc: OSError) -> SemanticLayerError:
    return SemanticLayerError(
        "INVALID_CONFIG",
        f"cannot take the rebuild lock for package.default_db '{db_path}': "
        f"{exc.strerror or type(exc).__name__} ({exc.filename or 'lock directory'})",
        details={"default_db": db_path, "reason": "rebuild_lock_unavailable"},
    )


def _lock_directory() -> str:
    """This user's private directory for rebuild locks.

    Another user must not be able to create it first (and lock everyone out) or
    hold a lock in it, so it is per user, mode 0700, and must be a real
    directory the user owns that no one else can write.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    name = "semantic-rails-db-locks" if uid is None else f"semantic-rails-db-locks-{uid}"
    path = os.path.join(tempfile.gettempdir(), name)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or (
        uid is not None and (info.st_uid != uid or info.st_mode & 0o077)
    ):
        raise PermissionError(errno.EPERM, "not a private directory of this user", path)
    return path


@contextlib.contextmanager
def rebuild_lock(db_path: str, *, timeout: float = REBUILD_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize judging and rebuilding one database file across threads and processes.

    The lock file lives in this user's private lock directory, keyed by the
    file's real path, so it never lands in (or changes the revision of) a
    package. Rebuilders under different users are not serialized; the judged
    file check at publish still keeps either from replacing a file it did not
    judge.
    """
    # Imported here: architect_transactions imports config validation, which
    # imports this module's importers.
    from .architect_transactions import _release_file_lock, _try_file_lock

    key = hashlib.sha256(os.path.realpath(db_path).encode("utf-8")).hexdigest()
    with _LOCAL_LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    if not local.acquire(timeout=timeout):
        raise _rebuild_busy(db_path)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                os.path.join(_lock_directory(), f"{key}.lock"), os.O_CREAT | os.O_RDWR, 0o600
            )
        except OSError as exc:
            raise _lock_unavailable(db_path, exc) from exc
        deadline = time.monotonic() + timeout
        while not _try_file_lock(descriptor):
            if time.monotonic() >= deadline:
                raise _rebuild_busy(db_path)
            time.sleep(0.05)
        yield
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                _release_file_lock(descriptor)
            os.close(descriptor)
        local.release()


def file_identity(db_path: str) -> tuple[int, int]:
    """The (device, inode) naming the file at ``db_path`` now."""
    stat = os.stat(db_path)
    return (stat.st_dev, stat.st_ino)


def _catalog_name(db_path: str) -> str:
    """The catalog name DuckDB gives the file, so three-part names probe as in compiled SQL.

    DuckDB names it after the file name up to the first dot, renaming ``temp``
    and ``system``. The attaching connection's own catalog is ``memory``, so a
    file named ``memory.*`` is attached under another name, and three-part names
    through it read as missing (the runtime then refuses rather than guesses).
    """
    name = os.path.basename(db_path).split(".")[0] or "database"
    if name.lower() in {"temp", "system"}:
        name = f"{name}_db"
    return "memory_db" if name.lower() == "memory" else name


@contextlib.contextmanager
def hold_database(db_path: str) -> Iterator[tuple[Any, tuple[int, int]]]:
    """Open the file at ``db_path`` read-only, as it is now, and hold it.

    Yields the connection and the file's (device, inode). The file is attached
    to a private in-memory connection rather than opened with
    ``duckdb.connect``: DuckDB reuses one open database per path within a
    process, so a runtime that opened this path before the file was replaced
    would hand back the old file. The file must be the same one before and after
    it is attached. While the connection is held, a writer in another process
    (such as ``dbt build``) cannot open the file, so a rebuild judged safe cannot
    drop a write that started after the judgement. Raises if the file cannot be
    opened, including while a writer in this process holds it.
    """
    before = file_identity(db_path)
    conn = duckdb.connect(":memory:")
    try:
        alias = _quote_identifier(_catalog_name(db_path))
        conn.execute(f"ATTACH '{db_path.replace(chr(39), chr(39) * 2)}' AS {alias} (READ_ONLY)")
        conn.execute(f"USE {alias}")
        if file_identity(db_path) != before:
            raise OSError(errno.EAGAIN, "the file was replaced while it was opened", db_path)
        yield conn, before
    finally:
        conn.close()


def _appeared(db_path: str) -> SemanticLayerError:
    return SemanticLayerError(
        "CONFIG_CONFLICT",
        f"package.default_db '{db_path}' appeared or changed while the seed was being built",
        details={"default_db": db_path, "reason": "default_db_created_concurrently"},
    )


def publish_seed_database(
    tmp_path: str,
    db_path: str,
    *,
    replace_existing: bool,
    judged: tuple[int, int] | None = None,
    allow_overwrite: bool = False,
) -> None:
    """Move a finished seed build at ``tmp_path`` into place at ``db_path``.

    With ``replace_existing`` it replaces the file at ``db_path``, but only if
    that is still the ``judged`` file (when given), and first removes a stale
    write-ahead log so it cannot replay into the new file. Without it, the build
    is published only if no file exists, atomically: a hard link (or, on
    Windows, a rename, which never replaces) fails when another process created
    the file meanwhile, and a ``CONFIG_CONFLICT`` tells the caller to judge
    that file. Where neither is possible (a filesystem without hard links) the
    publish fails closed, unless ``allow_overwrite`` (the operator's opt-in)
    accepts that a file created meanwhile is replaced.
    """
    if replace_existing:
        if judged is not None and file_identity(db_path) != judged:
            raise _appeared(db_path)
        with contextlib.suppress(FileNotFoundError):
            os.remove(f"{db_path}.wal")
        os.replace(tmp_path, db_path)
        return
    try:
        os.link(tmp_path, db_path)
    except FileExistsError:
        raise _appeared(db_path) from None
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINK_ERRNOS:
            raise
        if os.name != "nt":
            if allow_overwrite:
                os.replace(tmp_path, db_path)
                return
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"cannot build package.default_db '{db_path}' safely: its filesystem does not "
                "support hard links, so a file created meanwhile could be overwritten. Keep "
                f"the database on a local filesystem, or set {DB_RESEED_ENV}=1 to build it "
                "anyway.",
                details={"default_db": db_path, "reason": "atomic_publish_unavailable"},
            ) from exc
        try:
            os.rename(tmp_path, db_path)  # never replaces an existing file on Windows
        except FileExistsError:
            raise _appeared(db_path) from None
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp_path)
