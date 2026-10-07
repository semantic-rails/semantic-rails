"""Confine an embedded DuckDB database's file access to one directory.

A host serving several packages from one process passes ``confine_to`` to the
DuckDB and DuckLake adapters (or to ``create_warehouse_adapter``). Once the
database files are open, :func:`confine_duckdb` limits file access to that
directory, turns off extension installs and loads, checks that each setting
took effect, and locks the configuration. Anything that does not hold refuses
with ``INVALID_CONFIG``; the adapter never runs unconfined in its place.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from ..errors import SemanticLayerError

# The settings a locked configuration still lets a statement change: the
# adapters run each query in its time role's zone.
_UNLOCKED_SETTINGS = ("TimeZone",)

_EXPECTED_SETTINGS = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "lock_configuration": True,
}


def _refusal(reason: str, message: str, **details: str) -> SemanticLayerError:
    # Details name settings and options, never paths: the directory is the host's.
    return SemanticLayerError(
        "INVALID_CONFIG", message, details={"reason": f"duckdb_{reason}", **details}
    )


def _inside(path: str, directory: str) -> bool:
    path = os.path.normpath(path)
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:  # another drive, or a relative path
        return False


def confinement_directory(directory: str | os.PathLike[str]) -> str:
    """The real path of ``directory``, which must be an existing absolute directory."""
    path = os.fspath(directory)
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise _refusal(
            "confinement_directory_invalid",
            "DuckDB confinement needs an existing absolute directory.",
        )
    return os.path.realpath(path)


def require_inside(directory: str, path: str, *, option: str, relative_to: str = "") -> str:
    """Return an absolute real file path inside the directory, or refuse it."""
    if path == ":memory:" or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path):
        raise _refusal(
            "path_not_file",
            f"The DuckDB {option} must be a filesystem path.",
            option=option,
        )
    resolved = os.path.realpath(os.path.join(relative_to, path))
    if not _inside(resolved, directory):
        raise _refusal(
            "path_outside_confinement",
            f"The DuckDB {option} is outside the confinement directory.",
            option=option,
        )
    return resolved


def _setting(conn: Any, name: str) -> Any:
    return conn.execute("SELECT system.main.current_setting(?)", [name]).fetchone()[0]


def confine_duckdb(conn: Any, directory: str) -> None:
    """Limit ``conn``'s database to ``directory``, check it, and lock its configuration.

    The settings belong to the database instance, which DuckDB shares between
    the connections a process opens to one file. An instance that is already
    locked is checked, not changed, so a second adapter on a confined file
    holds only if the first confined it to this directory or one inside it.
    """
    if _setting(conn, "lock_configuration") is not True:
        try:
            temp = _setting(conn, "temp_directory")
            if temp and not _inside(os.path.realpath(temp), directory):
                # DuckDB always allows its spill directory, so keep it inside too.
                spill = os.path.join(directory, f".duckdb_tmp_{uuid.uuid4().hex}")
                conn.execute("SET temp_directory = ?", [spill])
            conn.execute("SET allowed_directories = ?", [[directory]])
            conn.execute("SET autoinstall_known_extensions = false")
            conn.execute("SET autoload_known_extensions = false")
            conn.execute("SET enable_external_access = false")
            conn.execute("SET allowed_configs = ?", [list(_UNLOCKED_SETTINGS)])
            conn.execute("SET lock_configuration = true")
        except Exception as exc:  # noqa: BLE001 — refused below without the driver's text
            raise _refusal(
                "confinement_failed", "DuckDB refused the confinement settings."
            ) from exc
    for name, expected in _EXPECTED_SETTINGS.items():
        if _setting(conn, name) is not expected:
            raise _refusal(
                "confinement_failed", f"DuckDB {name} did not take effect.", setting=name
            )
    if not set(_setting(conn, "allowed_configs")) <= set(_UNLOCKED_SETTINGS):
        raise _refusal(
            "confinement_failed",
            "DuckDB allowed_configs leaves other settings unlocked.",
            setting="allowed_configs",
        )
    for name in ("allowed_directories", "allowed_paths"):
        if not all(_inside(str(entry), directory) for entry in _setting(conn, name)):
            raise _refusal(
                "confinement_failed",
                f"DuckDB {name} reaches outside the confinement directory.",
                setting=name,
            )
