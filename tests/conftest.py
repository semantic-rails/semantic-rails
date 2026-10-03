from __future__ import annotations

import faulthandler
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest_plugins = ["scripts.test_quarantine"]


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


WORKER_EXIT_WATCHDOG_SECONDS = 120


def pytest_sessionfinish(session: pytest.Session) -> None:
    # pytest-timeout only watches tests: dump the stacks of an xdist worker that cannot exit
    # after its last test (a non-daemon thread, a child it waits on), then end it.
    if hasattr(session.config, "workerinput"):
        faulthandler.dump_traceback_later(
            WORKER_EXIT_WATCHDOG_SECONDS, exit=True, file=sys.__stderr__
        )
