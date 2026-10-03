from __future__ import annotations

import os
import sys
import uuid
import weakref
from collections.abc import Generator, Iterator
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest_plugins = ["scripts.test_quarantine"]

_call_connections = pytest.StashKey["weakref.WeakSet[duckdb.DuckDBPyConnection]"]()
_fixture_connections: weakref.WeakSet[duckdb.DuckDBPyConnection] = weakref.WeakSet()
# Per process: {node ID: connections its call phase opened and left open}.
_duckdb_leaks: dict[str, int] = {}


@pytest.fixture(scope="session", autouse=True)
def duckdb_test_limits(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    from tests.semantic_rails.duckdb_limits import limited_connect

    root = tmp_path_factory.mktemp("duckdb-spill")
    # Python children inherit the same hook without changing runtime code or
    # adding the source checkout to their import path.
    startup = tmp_path_factory.mktemp("duckdb-startup")
    helper = REPO_ROOT / "tests" / "semantic_rails" / "duckdb_limits.py"
    (startup / "sitecustomize.py").write_text(
        "import importlib.util, runpy\n"
        "if importlib.util.find_spec('duckdb') is not None:\n"
        f"    hook = runpy.run_path({str(helper)!r})\n"
        f"    hook['duckdb'].connect = hook['limited_connect'](hook['Path']({str(root)!r}))\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as patch:
        mktemp = tmp_path_factory.mktemp  # Deleted paths may still have live readers/caches.

        def unique_mktemp(basename, numbered=True):
            return mktemp(f"{basename}-{uuid.uuid4().hex}", numbered=numbered)

        patch.setattr(tmp_path_factory, "mktemp", unique_mktemp)
        patch.setattr(duckdb, "connect", limited_connect(root))
        patch.setenv(
            "PYTHONPATH", os.pathsep.join(filter(None, [str(startup), os.getenv("PYTHONPATH")]))
        )
        yield


class _FixtureConnections:
    """A fixture may keep a connection across tests, even one a test body requested."""

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self) -> Generator[None, object, object]:
        from tests.semantic_rails.duckdb_limits import open_connections

        before = weakref.WeakSet(open_connections)
        try:
            return (yield)
        finally:
            _fixture_connections.update(c for c in open_connections if c not in before)


def pytest_configure(config: pytest.Config) -> None:
    # A plugin, not a conftest hook: a session fixture sets up on the session
    # node, where this directory's conftest hooks don't apply.
    config.pluginmanager.register(_FixtureConnections())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    from tests.semantic_rails.duckdb_limits import open_connections

    before = weakref.WeakSet(open_connections)
    try:
        return (yield)
    finally:
        item.stash[_call_connections] = weakref.WeakSet(
            c for c in open_connections if c not in before and c not in _fixture_connections
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    # Close after the test's own fixtures so GC never finalizes a leaked
    # connection in the middle of a later test.
    try:
        return (yield)
    finally:
        left_open = 0
        for connection in list(item.stash.get(_call_connections, ())):
            try:
                connection.execute("SELECT 1")
            except duckdb.ConnectionException:
                continue  # Already closed.
            except duckdb.Error:
                pass
            left_open += 1
            connection.close()
        if left_open:
            _duckdb_leaks[item.nodeid] = left_open


def _duckdb_leak_report() -> list[str]:
    worst = sorted(_duckdb_leaks.items(), key=lambda leak: (-leak[1], leak[0]))[:20]
    return [
        f"DuckDB connections left open by {len(_duckdb_leaks)} tests (closed at teardown)",
        *(f"  {count} {nodeid}" for nodeid, count in worst),
    ]


def pytest_sessionfinish(session: pytest.Session) -> None:
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:  # xdist worker: hand the counts to the controller.
        workeroutput["duckdb_leaks"] = dict(_duckdb_leaks)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "worker")
        sys.stderr.write("".join(f"[{worker}] {line}\n" for line in _duckdb_leak_report()))


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    _duckdb_leaks.update(getattr(node, "workeroutput", {}).get("duckdb_leaks", {}))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for line in _duckdb_leak_report():
        terminalreporter.write_line(line)
