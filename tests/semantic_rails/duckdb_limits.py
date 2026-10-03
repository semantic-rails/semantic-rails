"""Test-only connection limits, shared with Python subprocesses."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import duckdb


def limited_connect(root: Path):
    connect = duckdb.connect
    root = root / str(os.getpid())
    root.mkdir(parents=True, exist_ok=True)

    def capped(database=":memory:", read_only=False, config=None):
        # Connections to one file must share config; independent instances must
        # not collide on DuckDB's spill filenames (including across processes).
        identity = str(Path(database).resolve()).encode()
        key = uuid.uuid4().hex if database == ":memory:" else hashlib.sha256(identity).hexdigest()
        options = dict(config or {})
        options.update(
            temp_directory=str(root / key),
            max_temp_directory_size=os.environ.get("SR_TEST_DUCKDB_MAX_TEMP", "4GB"),
            memory_limit=os.environ.get("SR_TEST_DUCKDB_MEMORY", "2GB"),
        )
        connection = connect(database, read_only=read_only, config=options)
        # The startup config reports the cap but DuckDB 1.5.6 needs SET to enforce it.
        connection.execute("SET max_temp_directory_size = ?", [options["max_temp_directory_size"]])
        return connection

    return capped
