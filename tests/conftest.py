from __future__ import annotations

import os
import sys
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
        patch.setattr(duckdb, "connect", limited_connect(root))
        patch.setenv(
            "PYTHONPATH", os.pathsep.join(filter(None, [str(startup), os.getenv("PYTHONPATH")]))
        )
        yield
