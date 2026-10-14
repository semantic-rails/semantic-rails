"""Packages a planner test module opens once and shares across its tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing

import pytest

from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened


def jaffle_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """jaffle_shop, for tests that only read it or change it through monkeypatch."""
    path = copy_package_config(tmp_path_factory.mktemp("jaffle"), "jaffle_shop", preseed_db=True)
    with closing(opened(Runtime.from_path(str(path)))) as runtime:
        yield runtime
