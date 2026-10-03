from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from semantic_rails.db import Database
from tests.semantic_rails.test_repl_backend import _journey

SETTINGS = "SELECT current_setting('temp_directory'), current_setting('max_temp_directory_size'), current_setting('memory_limit')"


@pytest.mark.parametrize("overrides", [False, True])
def test_limits_cover_direct_runtime_and_child_connections(
    monkeypatch, tmp_path_factory, overrides
):
    if overrides:
        monkeypatch.setenv("SR_TEST_DUCKDB_MAX_TEMP", "8MB")
        monkeypatch.setenv("SR_TEST_DUCKDB_MEMORY", "64MB")
    else:
        monkeypatch.delenv("SR_TEST_DUCKDB_MAX_TEMP", raising=False)
        monkeypatch.delenv("SR_TEST_DUCKDB_MEMORY", raising=False)
    with duckdb.connect(config={"threads": 1, "max_temp_directory_size": "100GB"}) as direct:
        expected = direct.execute(SETTINGS).fetchone()
        assert direct.execute("SELECT current_setting('threads')").fetchone() == (1,)
        with Database.connect_in_memory().conn as runtime, runtime.cursor() as cursor:
            settings = cursor.execute(SETTINGS).fetchone()
        command = f"import duckdb,json; print(json.dumps(duckdb.connect().execute({SETTINGS!r}).fetchone()))"
        child = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        child_settings = json.loads(child.stdout)
    assert expected[1:] == (("7.6 MiB", "61.0 MiB") if overrides else ("3.7 GiB", "1.8 GiB"))
    assert tuple(settings[1:]) == tuple(child_settings[1:]) == expected[1:]
    paths = [Path(row[0]) for row in (expected, settings, child_settings)]
    assert all(path.is_relative_to(tmp_path_factory.getbasetemp()) for path in paths)
    assert len(set(paths)) == 3


def test_connections_to_same_database_share_limits(tmp_path):
    path = tmp_path / "shared.duckdb"
    with duckdb.connect(str(path)) as first, duckdb.connect(path) as second:
        assert first.execute(SETTINGS).fetchone() == second.execute(SETTINGS).fetchone()


def test_exploding_cross_join_stops_at_temp_cap(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("SR_TEST_DUCKDB_MAX_TEMP", "8MB")
    monkeypatch.setenv("SR_TEST_DUCKDB_MEMORY", "32MB")
    with duckdb.connect(config={"threads": 1}) as connection:
        spill = Path(connection.execute(SETTINGS).fetchone()[0])
        with pytest.raises(duckdb.OutOfMemoryException, match="max_temp_directory_size"):
            connection.execute(
                "CREATE TABLE exploding AS SELECT a.i AS a, b.i AS b, hash(a.i, b.i) AS h "
                "FROM range(4000) a(i) CROSS JOIN range(4000) b(i)"
            )
        assert spill.is_relative_to(tmp_path_factory.getbasetemp())
        assert sum(path.stat().st_size for path in spill.rglob("*") if path.is_file()) <= 8_000_000


def test_cli_subprocess_helper_preserves_limits(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("SR_TEST_DUCKDB_MAX_TEMP", "8MB")
    monkeypatch.setenv("SR_TEST_DUCKDB_MEMORY", "64MB")
    output = _journey(
        "import duckdb,json; "
        f"print(json.dumps(duckdb.connect().execute({SETTINGS!r}).fetchone())); "
        "print('journey ok')"
    )
    spill, temp_cap, memory_cap = json.loads(output.splitlines()[0])
    assert (temp_cap, memory_cap) == ("7.6 MiB", "61.0 MiB")
    assert Path(spill).is_relative_to(tmp_path_factory.getbasetemp())
