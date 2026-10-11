"""The operations table: the transports serve exactly its rows, with their arguments."""

import inspect

import pytest

from semantic_rails.http_core import PUBLIC_V1_ROUTES
from semantic_rails.operations import OPERATIONS, run
from semantic_rails.runtime import Runtime

TRANSPORT_ONLY = {"health", "ready", "capabilities"}


def test_every_route_is_an_operation_and_every_operation_a_route():
    routes = {row["path"].removeprefix("/api/v1/") for row in PUBLIC_V1_ROUTES}
    assert routes - TRANSPORT_ONLY == set(OPERATIONS)


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_operation_arguments_are_its_service_parameters(name):
    operation = OPERATIONS[name]
    method = operation.service.__name__.removeprefix("Runtime.")
    target = getattr(Runtime, method) if hasattr(Runtime, method) else operation.service
    parameters = list(inspect.signature(target).parameters.values())[1:]
    named = {parameter.name for parameter in parameters}
    takes_more = any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters)
    assert set(operation.arguments) <= named or takes_more
    assert {"now", "context"} & set(operation.arguments) <= named
    required = {
        parameter.name
        for parameter in parameters
        if parameter.default is parameter.empty
        and parameter.kind not in {parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL}
    }
    assert required <= set(operation.arguments)


def test_run_refuses_an_argument_its_operation_does_not_take():
    with pytest.raises(TypeError, match="takes no \\['request_id'\\]"):
        run("validate", None, query={}, request_id="r1")  # type: ignore[arg-type]
