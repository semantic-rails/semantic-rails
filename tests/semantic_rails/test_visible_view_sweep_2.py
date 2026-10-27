import pytest

from tests.semantic_rails.visible_view_sweep import SLICES, TARGETS, assert_hidden_responses


@pytest.mark.parametrize("target", TARGETS[2::SLICES])
def test_no_response_names_or_answers_from_a_hidden_object(sweep_package, target):
    assert_hidden_responses(sweep_package, target)
