"""A test's leftover DuckDB connections are closed and reported at its own teardown."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.timeout(90)
def test_connections_a_test_leaves_open_are_closed_and_reported(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest("from tests.conftest import *  # noqa: F403\n")
    pytester.makepyfile(
        test_leaky=textwrap.dedent(
            """\
            import duckdb
            import pytest


            @pytest.fixture(scope="module")
            def kept():
                return duckdb.connect()


            def test_leaks():
                test_leaks.connection = duckdb.connect()


            def test_closes_its_own():
                with duckdb.connect() as connection:
                    connection.execute("SELECT 1")


            def test_uses_module_fixture(kept):
                kept.execute("SELECT 1")


            def test_leak_was_closed_and_fixture_connection_was_kept(kept):
                with pytest.raises(duckdb.ConnectionException):
                    test_leaks.connection.execute("SELECT 1")
                kept.execute("SELECT 1")
            """
        )
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(ROOT), os.getenv("PYTHONPATH")]))
    )
    # One worker keeps the order and still relays its counts to the controller.
    result = pytester.runpytest_subprocess(
        "-p", "xdist.plugin", "-p", "no:cacheprovider", "-n", "1", timeout=60
    )
    output = result.stdout.str() + result.stderr.str()

    assert result.ret == 0, output
    summary = "DuckDB connections left open by 1 tests (closed at teardown)"
    assert result.stdout.lines.count(summary) == 1, output
    assert "  1 test_leaky.py::test_leaks" in result.stdout.lines, output
    assert f"[gw0] {summary}" in result.stderr.str(), output
