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
    # As in this repository, the conftest sits below the rootdir, where a
    # session fixture's setup runs.
    pytester.makeini("[pytest]\n")
    suite = pytester.mkdir("suite")
    (suite / "conftest.py").write_text("from tests.conftest import *  # noqa: F403\n")
    (suite / "test_leaky.py").write_text(
        textwrap.dedent(
            """\
            import duckdb
            import pytest


            @pytest.fixture(scope="session")
            def kept():
                return duckdb.connect()


            def test_leaks():
                test_leaks.connection = duckdb.connect()


            def test_closes_its_own():
                with duckdb.connect() as connection:
                    connection.execute("SELECT 1")


            def test_requests_session_fixture_in_its_body(request):
                request.getfixturevalue("kept").execute("SELECT 1")


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
        "-p", "xdist.plugin", "-p", "no:cacheprovider", "-n", "1", "suite", timeout=60
    )
    output = result.stdout.str() + result.stderr.str()

    assert result.ret == 0, output
    summary = "DuckDB connections left open by 1 tests (closed at teardown)"
    assert result.stdout.lines.count(summary) == 1, output
    assert "  1 suite/test_leaky.py::test_leaks" in result.stdout.lines, output
    assert f"[gw0] {summary}" in result.stderr.str(), output
