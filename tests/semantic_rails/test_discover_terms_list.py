"""``discover`` takes ``terms`` as a list: each term ranks on its own, exactly as a call with
that term alone, and the results are keyed by term. The engine never splits or joins terms."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import HTTPInputError, SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text, list_tool_definitions
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import CALLER, visibility_policy, with_policies
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot
from tests.semantic_rails.visible_view_sweep import http_as, mcp_as

TERMS = ["revenue", "store"]
SEED = {"select": [{"expression": "measure.jaffle.revenue_usd"}]}
# The envelope every response carries, and what the call's own arguments decide.
ENVELOPE = {
    "ok",
    "status",
    "api_version",
    "package_id",
    "request_id",
    "request_context",
    "timing_ms",
}
CALL_FIELDS = {"verbosity", "query_state", "selection_context", "warnings"}


@pytest.fixture()
def runtime(jaffle_snapshot) -> Iterator[Runtime]:
    runtime = Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


def _http(runtime: Runtime, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    service = SemanticHTTPService(runtime)
    try:
        return service.handle("POST", "/discover", body, context=RequestContext())
    except (HTTPInputError, SemanticLayerError) as exc:
        return service.exception_payload(exc, stage="http", context=RequestContext())


def _alone(response: dict[str, Any]) -> dict[str, Any]:
    """A one-term response as a ``by_term`` entry: without the envelope, the call's fields or
    the terms echo."""
    return {
        key: value
        for key, value in response.items()
        if key not in ENVELOPE | CALL_FIELDS | {"terms"}
    }


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_each_term_ranks_as_it_would_alone_over_mcp(runtime: Runtime, verbosity: str) -> None:
    adapter = SemanticLayerMCPAdapter(runtime)
    arguments = {"query": SEED, "verbosity": verbosity, "limit": 3}
    listed = adapter.call_tool("discover", {**arguments, "terms": TERMS})
    assert listed["ok"], listed
    assert list(listed["by_term"]) == TERMS
    for term in TERMS:
        alone = adapter.call_tool("discover", {**arguments, "terms": term})
        assert listed["by_term"][term] == _alone(alone)
        # The seed's state is said once, beside the terms.
        for key in CALL_FIELDS & alone.keys():
            assert listed[key] == alone[key]


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_each_term_ranks_as_it_would_alone_over_rest(runtime: Runtime, verbosity: str) -> None:
    body = {"query": SEED, "verbosity": verbosity, "limit": 3}
    listed, status = _http(runtime, {**body, "terms": TERMS})
    assert status == 200, listed
    assert list(listed["by_term"]) == TERMS
    for term in TERMS:
        alone, _ = _http(runtime, {**body, "terms": term})
        assert listed["by_term"][term] == _alone(alone)
        assert listed["query_state"] == alone["query_state"]


def test_a_plain_string_is_one_search(runtime: Runtime) -> None:
    adapter = SemanticLayerMCPAdapter(runtime)
    response = adapter.call_tool("discover", {"terms": "revenue store"})
    assert response["ok"] and "by_term" not in response
    assert response["measures"]


def test_a_repeated_term_is_ranked_once_and_an_empty_list_lists_ids(runtime: Runtime) -> None:
    adapter = SemanticLayerMCPAdapter(runtime)
    repeated = adapter.call_tool("discover", {"terms": ["store", "revenue", "store"]})
    assert list(repeated["by_term"]) == ["store", "revenue"]
    empty = adapter.call_tool("discover", {"terms": []})
    assert empty["catalog"] == adapter.call_tool("discover", {"terms": ""})["catalog"]


def test_a_misspelled_argument_is_warned_about_once(runtime: Runtime) -> None:
    response = SemanticLayerMCPAdapter(runtime).call_tool(
        "discover", {"terms": TERMS, "kind": "dimension"}
    )
    assert [warning["code"] for warning in response["warnings"]] == ["DISCOVER_UNKNOWN_ARG"]
    assert all("warnings" not in result for result in response["by_term"].values())


@pytest.mark.parametrize("terms", [["revenue", 7], ["revenue", " "], {"revenue": 1}])
def test_a_term_that_is_not_text_is_refused(runtime: Runtime, terms: Any) -> None:
    response = SemanticLayerMCPAdapter(runtime).call_tool("discover", {"terms": terms})
    assert not response["ok"]
    [error] = response["errors"]
    assert error["code"] == "INVALID_MCP_ARGUMENTS"
    assert error["message"] == (
        "MCP argument 'terms' must be a string or a list of non-blank strings."
    )
    assert error["details"]["field"] == "terms"
    if isinstance(terms, list):
        rest, status = _http(runtime, {"terms": terms})
        assert status == 400
        assert rest["error"]["code"] == "INVALID_REQUEST"
        assert rest["error"]["message"] == (
            "Field 'terms' must be a string or a list of non-blank strings."
        )


def test_the_schema_offers_a_list_of_terms() -> None:
    tool = next(row for row in list_tool_definitions() if row["name"] == "discover")
    validator = Draft202012Validator(tool["inputSchema"])
    validator.validate({"terms": TERMS})
    validator.validate({"terms": "revenue"})
    assert not validator.is_valid({"terms": ["revenue", 7]})
    assert "each term of a list alone (by_term)" in tool["description"]


def test_terms_that_do_not_fit_the_budget_are_refused_whole(runtime: Runtime) -> None:
    terms = ["revenue", "store", "customer", "order"]
    full = SemanticLayerMCPAdapter(runtime).call_tool(
        "discover", {"terms": terms, "verbosity": "full"}
    )
    assert not full["ok"]
    [error] = full["errors"]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert error["details"]["largest_field"] == "by_term"
    default = SemanticLayerMCPAdapter(runtime).call_tool("discover", {"terms": terms})
    assert default["ok"] and list(default["by_term"]) == terms
    assert len(json_text(default)) <= error["details"]["max_result_chars"]


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("terms"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


def test_each_term_reads_only_the_callers_view(package) -> None:
    root, config = package
    hidden = "measure.jaffle.revenue_usd"
    governed = with_policies(config, visibility_policy("hidden", hidden))
    runtime = Runtime.from_config(governed, source_path=str(root))
    try:
        listed = mcp_as(runtime, "discover", {"terms": TERMS}, CALLER)
        assert listed["ok"], listed
        assert hidden not in json.dumps(listed)
        for term in TERMS:
            alone = mcp_as(runtime, "discover", {"terms": term}, CALLER)
            assert listed["by_term"][term] == _alone(alone)
        rest = http_as(runtime, "/discover", {"terms": TERMS}, CALLER)
        assert rest["http_status"] == 200 and hidden not in json.dumps(rest)
    finally:
        runtime.close()
