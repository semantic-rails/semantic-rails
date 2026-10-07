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
    (suite / "conftest.py").write_text(
        textwrap.dedent(
            """\
            import gc
            from types import SimpleNamespace

            import pytest

            from tests.conftest import *  # noqa: F403


            @pytest.fixture(scope="session")
            def retained():
                was_enabled = gc.isenabled()
                gc.disable()
                try:
                    yield SimpleNamespace()
                finally:
                    if was_enabled:
                        gc.enable()
            """
        )
    )
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
    (suite / "test_runtime.py").write_text(
        textwrap.dedent(
            """\
            import pytest

            from semantic_rails.runtime import Runtime
            from tests.semantic_rails.conftest import copy_package_config, opened


            @pytest.fixture(scope="module")
            def runtime(tmp_path_factory, retained):
                package = copy_package_config(tmp_path_factory.mktemp("runtime"), "jaffle_shop")
                rt = Runtime.from_path(str(package))
                try:
                    yield opened(rt)
                finally:
                    rt.close()


            @pytest.mark.xfail(strict=True, raises=RuntimeError)
            def test_traceback_retains_runtime(runtime, retained):
                retained.connection = runtime._get_adapter()._db.conn
                try:
                    raise RuntimeError("retain the runtime in this frame")
                except RuntimeError as error:
                    retained.error = error
                    raise
            """
        )
    )
    (suite / "test_z_after_runtime.py").write_text(
        textwrap.dedent(
            """\
            import gc

            import duckdb
            import pytest


            def test_module_fixture_closed_despite_retained_traceback(retained):
                assert not gc.isenabled()
                traceback = retained.error.__traceback__
                while traceback.tb_next is not None:
                    traceback = traceback.tb_next
                frame = traceback.tb_frame
                assert "runtime" in frame.f_locals
                with pytest.raises(duckdb.ConnectionException):
                    retained.connection.execute("SELECT 1")
                assert frame.f_locals["runtime"].adapter is None
            """
        )
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    # This child checks the entire synthetic teardown sequence, independent of
    # the shard that owns this parent test in CI.
    monkeypatch.setenv("SR_SHARD_COUNT", "1")
    monkeypatch.setenv("SR_SHARD_INDEX", "0")
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(ROOT), os.getenv("PYTHONPATH")]))
    )
    # One worker keeps the order and still relays its counts to the controller.
    result = pytester.runpytest_subprocess(
        "-p", "xdist.plugin", "-p", "no:cacheprovider", "-n", "1", "suite", timeout=60
    )
    output = result.stdout.str() + result.stderr.str()

    assert result.ret == 0, output
    result.assert_outcomes(passed=5, xfailed=1)
    summary = "DuckDB connections left open by 1 tests (closed at teardown)"
    assert result.stdout.lines.count(summary) == 1, output
    assert "  1 suite/test_leaky.py::test_leaks" in result.stdout.lines, output
    assert f"[gw0] {summary}" in result.stderr.str(), output
    assert not any(
        "suite/test_runtime.py::" in line for line in result.stdout.lines if line.startswith("  ")
    ), output
