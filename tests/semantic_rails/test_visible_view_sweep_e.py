from pathlib import Path

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.mcp import _MAX_RESULT_CHARS_ENV
from semantic_rails.schema import PackageConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.visible_view_sweep import (
    TARGETS,
    _with_default_metric,
    assert_hidden_responses,
)

SWEEP_TARGETS = TARGETS[1::3]


@pytest.fixture(autouse=True)
def _whole_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compare whole payloads, never a budget refusal's size: that size counts timings."""
    monkeypatch.setenv(_MAX_RESULT_CHARS_ENV, "100000000")


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("sweep"), "jaffle_shop", preseed_db=True)
    return root, _with_default_metric(load_package_config(str(root)))


@pytest.mark.parametrize("target", SWEEP_TARGETS)
def test_no_response_names_or_answers_from_a_hidden_object(package, target):
    assert_hidden_responses(package, target)
