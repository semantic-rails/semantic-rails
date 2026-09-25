"""DuckDB seed provenance and safe runtime bootstrap publication.

Runtime bootstrap only publishes a complete seed when the target does not yet
exist. Existing files are probed in a separate process so DuckDB's in-process
catalog cache cannot certify an older inode, and closing the probe cannot drop
a healthy serving connection's process-wide POSIX lock.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import duckdb

from .errors import SemanticLayerError
from .sql_identifiers import quote_relation

_PROVENANCE_TABLE = "_semantic_rails.seed_provenance"
_NO_HARD_LINK_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EXDEV", "EMLINK")
    )
    if code is not None
)


def _missing_on_connection(conn: Any, relations: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for relation in sorted(set(relations)):
        try:
            conn.execute(f"SELECT 1 FROM {quote_relation(relation)} LIMIT 0")
        except Exception:  # noqa: BLE001 — a binder/catalog failure is not a usable relation
            missing.append(relation)
    return missing


def _identity(db_path: str) -> tuple[int, int, int, int]:
    info = os.stat(db_path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _probe_cli() -> None:
    """Isolated child entrypoint. The parent communicates only through JSON."""
    request = json.load(sys.stdin)
    path, relations = request["path"], request["relations"]
    try:
        before = _identity(path)
        conn = duckdb.connect(path, read_only=True)
        try:
            missing = _missing_on_connection(conn, relations)
        finally:
            conn.close()
        after = _identity(path)
        if before != after:
            raise OSError(errno.EAGAIN, "database changed during catalog probe", path)
        result: dict[str, Any] = {"identity": before, "missing": missing}
    except Exception as exc:  # noqa: BLE001 — only a bounded diagnostic crosses process
        result = {"error": type(exc).__name__}
    print(json.dumps(result))


def missing_duckdb_relations(db: Any, relations: Iterable[str]) -> list[str]:
    """Return configured relations that the current DuckDB file cannot resolve.

    An existing path is opened in a new process, which cannot reuse a stale
    in-process DuckDB catalog or release this process's serving locks. A caller
    that already owns a connection may pass it directly. A changing or unreadable
    file raises so runtime bootstrap can fail closed.
    """
    if not isinstance(db, (str, os.PathLike)):
        return _missing_on_connection(db, relations)
    path = os.fspath(db)
    before = _identity(path)
    import_root = os.path.dirname(os.path.dirname(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (import_root, env.get("PYTHONPATH", ""))))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from semantic_rails.seed_provenance import _probe_cli; _probe_cli()",
        ],
        input=json.dumps({"path": path, "relations": sorted(set(relations))}),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
        env=env,
    )
    if result.returncode:
        raise OSError(errno.EIO, "database catalog probe failed", path)
    try:
        payload = json.loads(result.stdout)
        if "error" in payload or tuple(payload["identity"]) != before or _identity(path) != before:
            raise OSError(errno.EAGAIN, "database could not be judged consistently", path)
        return list(payload["missing"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError(errno.EIO, "database catalog probe returned invalid data", path) from exc


def record_seed_provenance(db_path: str, package_id: str, seed_digest: str = "") -> None:
    """Record which package and seed files built a new seed, without authorizing replacement."""
    conn = duckdb.connect(db_path)
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS _semantic_rails")
        conn.execute(
            f"CREATE OR REPLACE TABLE {_PROVENANCE_TABLE} (package_id VARCHAR, "
            "built_at_utc VARCHAR, duckdb_version VARCHAR, seed_digest VARCHAR)"
        )
        conn.execute(
            f"INSERT INTO {_PROVENANCE_TABLE} VALUES (?, ?, ?, ?)",
            [package_id, datetime.now(UTC).isoformat(), duckdb.__version__, seed_digest],
        )
    finally:
        conn.close()


def recorded_seed_digest(adapter: Any, package_id: str) -> str:
    """The seed digest recorded when ``package_id``'s seed built the adapter's database, or ''."""
    try:
        rows = adapter.query(f"SELECT package_id, seed_digest FROM {_PROVENANCE_TABLE}")
    except SemanticLayerError:  # built by another tool, or before digests were recorded
        return ""
    return next((str(row["seed_digest"]) for row in rows if row["package_id"] == package_id), "")


def _appeared(db_path: str) -> SemanticLayerError:
    return SemanticLayerError(
        "CONFIG_CONFLICT",
        f"package.default_db '{db_path}' appeared while its seed was being built",
        details={"default_db": db_path, "reason": "default_db_created_concurrently"},
    )


def publish_seed_database(tmp_path: str, db_path: str) -> None:
    """Publish a complete seed without replacing a file another process made."""
    try:
        os.link(tmp_path, db_path)
    except FileExistsError:
        raise _appeared(db_path) from None
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINK_ERRNOS:
            raise
        if os.name != "nt":
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"cannot safely create package.default_db '{db_path}': this filesystem does "
                "not support atomic no-clobber publication. Use a local filesystem or build "
                "the database explicitly before starting Semantic Rails.",
                details={"default_db": db_path, "reason": "atomic_publish_unavailable"},
            ) from exc
        try:
            os.rename(tmp_path, db_path)  # Windows rename refuses an existing target.
        except FileExistsError:
            raise _appeared(db_path) from None
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp_path)
