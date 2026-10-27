import importlib

import pytest

from semantic_rails.config import load_package_config
from tests.semantic_rails.hidden_absent import object_rows
from tests.semantic_rails.visible_view_sweep import (
    SLICES,
    TARGETS,
    _with_default_metric,
    assert_hidden_responses,
)


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
    # Every slice file exists and sweeps exactly its slice.
    swept = []
    for index in range(SLICES):
        module = importlib.import_module(f"tests.semantic_rails.test_visible_view_sweep_{index}")
        test = module.test_no_response_names_or_answers_from_a_hidden_object
        (mark,) = [mark for mark in test.pytestmark if mark.name == "parametrize"]
        assert list(mark.args[1]) == TARGETS[index::SLICES]
        swept += mark.args[1]
    assert sorted(swept) == sorted(TARGETS)


@pytest.mark.parametrize("target", TARGETS[0::SLICES])
def test_no_response_names_or_answers_from_a_hidden_object(sweep_package, target):
    assert_hidden_responses(sweep_package, target)
