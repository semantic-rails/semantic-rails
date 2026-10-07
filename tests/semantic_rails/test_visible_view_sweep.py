from pathlib import Path

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.mcp import _MAX_RESULT_CHARS_ENV
from semantic_rails.schema import PackageConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import object_rows
from tests.semantic_rails.visible_view_sweep import (
    TARGETS,
    _with_default_metric,
    assert_hidden_responses,
)

SWEEP_TARGETS = TARGETS[0::3]


@pytest.fixture(autouse=True)
def _whole_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compare whole payloads, never a budget refusal's size: that size counts timings."""
    monkeypatch.setenv(_MAX_RESULT_CHARS_ENV, "100000000")


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("sweep"), "jaffle_shop", preseed_db=True)
    return root, _with_default_metric(load_package_config(str(root)))


def test_the_sweep_covers_every_object_of_every_kind():
    config = _with_default_metric(load_package_config("configs/semantic_rails/jaffle_shop"))
    assert len(TARGETS) == len(set(TARGETS)) == len(object_rows(config))
    assert {type(row).__name__ for row in object_rows(config)} == {
        "EntityConfig",
        "DimensionConfig",
        "TemporalRoleConfig",
        "RelationshipConfig",
        "ValueDomainConfig",
        "MeasureConfig",
        "MetricConfig",
        "SegmentConfig",
    }

    from tests.semantic_rails.test_visible_view_sweep_a import SWEEP_TARGETS as third
    from tests.semantic_rails.test_visible_view_sweep_e import SWEEP_TARGETS as second

    slices = [set(SWEEP_TARGETS), set(second), set(third)]
    assert all(
        left.isdisjoint(right) for index, left in enumerate(slices) for right in slices[index + 1 :]
    )
    assert set().union(*slices) == set(TARGETS)


@pytest.mark.parametrize("target", SWEEP_TARGETS)
def test_no_response_names_or_answers_from_a_hidden_object(package, target):
    assert_hidden_responses(package, target)
