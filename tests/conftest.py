from __future__ import annotations

import gc
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

pytest_plugins = ["scripts.test_quarantine", "scripts.test_sharding"]

_call_connections = pytest.StashKey["weakref.WeakSet[duckdb.DuckDBPyConnection]"]()
_fixture_connections: weakref.WeakSet[duckdb.DuckDBPyConnection] = weakref.WeakSet()
# The connections each fixture's setup opened, checked when that fixture is finalized.
_opened_by: dict[pytest.FixtureDef, weakref.WeakSet[duckdb.DuckDBPyConnection]] = {}
# Tests (any parametrization) known to leave a connection open, reported but not failed on, until
# their file is fixed. Remove an entry when its test closes what it opens.
_KNOWN_LEAKS = frozenset(
    {
        "tests/semantic_rails/test_route_census.py::"
        "test_a_second_route_to_a_dimensionless_child_requires_a_decision",
    }
)
# Per process: {node ID: connections its call phase opened and left open}.
_duckdb_leaks: dict[str, int] = {}
# Per process: connections still open after every fixture has been torn down.
_duckdb_open_at_end: list[str] = []


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
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef
    ) -> Generator[None, object, object]:
        from tests.semantic_rails.duckdb_limits import open_connections

        before = weakref.WeakSet(open_connections)
        try:
            return (yield)
        finally:
            opened = _opened_by.setdefault(fixturedef, weakref.WeakSet())
            for connection in [c for c in open_connections if c not in before]:
                _fixture_connections.add(connection)
                opened.add(connection)

    def pytest_fixture_post_finalizer(self, fixturedef: pytest.FixtureDef) -> None:
        # The fixture's own teardown has run; its cached value is still held here, so a
        # connection it never closed is still open instead of finalized by GC.
        for connection in _opened_by.pop(fixturedef, ()):
            if _is_open(connection):
                _duckdb_open_at_end.append(_fixture_name(fixturedef))
                connection.close()


def _fixture_name(fixturedef: pytest.FixtureDef) -> str:
    return f"{fixturedef.func.__module__}.{fixturedef.argname}"


def pytest_configure(config: pytest.Config) -> None:
    # A plugin, not a conftest hook: a session fixture sets up on the session
    # node, where this directory's conftest hooks don't apply.
    config.pluginmanager.register(_FixtureConnections())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    from tests.semantic_rails.duckdb_limits import open_connections

    before = weakref.WeakSet(open_connections)
    gc.disable()
    try:
        return (yield)
    finally:
        item.stash[_call_connections] = weakref.WeakSet(
            c for c in open_connections if c not in before and c not in _fixture_connections
        )


def _is_open(connection: duckdb.DuckDBPyConnection) -> bool:
    try:
        connection.execute("SELECT 1")
    except duckdb.ConnectionException:
        return False  # Already closed.
    except duckdb.Error:
        pass
    return True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    # Close after the test's own fixtures so GC never finalizes a leaked
    # connection in the middle of a later test.
    try:
        return (yield)
    finally:
        left_open = 0
        for connection in list(item.stash.get(_call_connections, ())):
            if _is_open(connection):
                left_open += 1
                connection.close()
        gc.enable()
        if left_open:
            _duckdb_leaks[item.nodeid] = left_open


def _is_known_leak(nodeid: str) -> bool:
    return nodeid.split("[", 1)[0] in _KNOWN_LEAKS


def _new_leaks() -> dict[str, int]:
    return {nodeid: n for nodeid, n in _duckdb_leaks.items() if not _is_known_leak(nodeid)}


def _duckdb_leak_report() -> list[str]:
    lines = []
    if _duckdb_leaks:
        lines.append(
            f"DuckDB connections left open by {len(_duckdb_leaks)} tests (closed at teardown)"
        )
        lines += [
            f"  {count} {nodeid}" + (" (known, not failing)" if _is_known_leak(nodeid) else "")
            for nodeid, count in sorted(_duckdb_leaks.items(), key=lambda i: (-i[1], i[0]))
        ]
    if _duckdb_open_at_end:
        lines.append(
            f"DuckDB connections still open after fixture teardown: {len(_duckdb_open_at_end)}"
        )
        lines += [f"  {where}" for where in sorted(_duckdb_open_at_end)]
    if lines:
        lines.append("Close every connection a test or fixture opens; see DEVELOPMENT.md.")
    return lines


# Innermost wrapper, so this runs right after the session's fixtures are torn down and
# before the terminal summary reports it.
@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> Generator[None, None, None]:
    result = yield  # The session's own fixtures are torn down by now.
    from tests.semantic_rails.duckdb_limits import open_connections

    gc.collect()
    for connection in list(open_connections):
        if _is_open(connection):
            _duckdb_open_at_end.append("opened outside a fixture and a test body")
            connection.close()
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:  # xdist worker: hand the counts to the controller.
        workeroutput["duckdb_leaks"] = dict(_duckdb_leaks)
        workeroutput["duckdb_open_at_end"] = list(_duckdb_open_at_end)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "worker")
        sys.stderr.write("".join(f"[{worker}] {line}\n" for line in _duckdb_leak_report()))
    elif (_new_leaks() or _duckdb_open_at_end) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED  # The guard: no leaked handles.
    return result


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    output = getattr(node, "workeroutput", {})
    _duckdb_leaks.update(output.get("duckdb_leaks", {}))
    _duckdb_open_at_end.extend(output.get("duckdb_open_at_end", []))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for line in _duckdb_leak_report():
        terminalreporter.write_line(line, red=True)
