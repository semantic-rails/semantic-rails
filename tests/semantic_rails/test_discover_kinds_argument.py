"""``discover`` never answers "no match" because of how ``kinds`` was encoded.

Invariant: the "no semantic objects matched" text appears only when the search
ran over the requested kinds and found nothing (the payload's own ``no_matches``). A JSON-encoded ``kinds`` string
means the same as the array, and a value that names no real kind is refused.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from semantic_rails.cli.commands import query as cli_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.http_request import HTTPInputError, coerce_string_list
from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions
from semantic_rails.metadata import discover_payload
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.request_payload import parse_string_list
from semantic_rails.resource_access import GRANT_DISCOVER_KINDS
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

# The docs quote "No semantic objects ... matched" as the sign a search really ran.
NO_MATCH = "No semantic objects"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_snapshot(load_package_snapshot(path))
    yield opened(runtime)
    runtime.close()


@pytest.fixture(scope="module")
def adapter(runtime: Any) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(runtime)
    yield adapter
    adapter.close()


@pytest.fixture(scope="module")
def grant(runtime: Any) -> dict[str, Any]:
    """A policy context in resource-grant mode: one allowed metric."""
    return {"metric_allowlist": [discover_payload(runtime, terms="revenue")["metrics"][0]["id"]]}


@pytest.fixture(scope="module")
def granted(grant: dict[str, Any]) -> Any:
    return context_from_policy_context(grant)


def _ids(response: dict[str, Any]) -> list[str]:
    return [
        card["id"]
        for bucket in (
            "measures",
            "metrics",
            "segments",
            "dimensions",
            "dimension_values",
            "entities",
            "relationships",
        )
        for card in response.get(bucket) or []
    ]


def _hints_text(response: dict[str, Any]) -> str:
    return " ".join(str(hint.get("message", "")) for hint in response.get("recovery_hints") or [])


MALFORMED = ['["metric"', '{"a": 1}', "[1, 2]", '[["metric"]]', ["metric", 3], {"oops": True}, 7, True, "[" * 100_000]  # fmt: skip


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("", []),
        ("metric", ["metric"]),
        (" measure , metric ", ["measure", "metric"]),
        (["metric", " measure "], ["metric", "measure"]),
        ('["metric"]', ["metric"]),
        ('  ["measure", "metric"] ', ["measure", "metric"]),
        ("[]", []),
        *((value, ValueError) for value in MALFORMED),
    ],
)
def test_parse_string_list_reads_every_encoding_and_refuses_the_rest(
    value: Any, expected: Any
) -> None:
    if expected is ValueError:
        with pytest.raises(ValueError):
            parse_string_list(value)
    else:
        assert parse_string_list(value) == expected


def test_http_coercion_refuses_instead_of_guessing() -> None:
    assert coerce_string_list('["metric"]', field="kinds") == ["metric"]
    with pytest.raises(HTTPInputError, match="Field 'kinds'"):
        coerce_string_list('["metric"', field="kinds")


@pytest.mark.parametrize("kinds", ['["measure"]', '["measure", "metric"]', "measure,metric"])
def test_string_encoded_kinds_match_the_list_form(
    adapter: SemanticLayerMCPAdapter, kinds: str
) -> None:
    as_list = [part.strip(' []"') for part in kinds.split(",")]
    encoded = adapter.call_tool("discover", {"terms": "revenue", "kinds": kinds})
    listed = adapter.call_tool("discover", {"terms": "revenue", "kinds": as_list})
    assert encoded["ok"] is True
    assert _ids(encoded), "the fixture must rank something for 'revenue'"
    assert _ids(encoded) == _ids(listed)
    assert not encoded.get("warnings")
    assert NO_MATCH not in _hints_text(encoded)


def test_string_encoded_kinds_filter_empty_terms_listing(adapter: SemanticLayerMCPAdapter) -> None:
    encoded = adapter.call_tool("discover", {"terms": "", "kinds": '["segment"]'})["catalog"]
    listed = adapter.call_tool("discover", {"terms": "", "kinds": ["segment"]})["catalog"]
    assert encoded == listed
    assert "segment_ids" in encoded and "measure_ids" not in encoded


@pytest.mark.parametrize(
    ("terms", "kinds"),
    [
        ("revenue", ["metirc"]),
        ("revenue", '["metirc"]'),
        ("revenue", "metric,bogus"),
        ("revenue", "temporal_role"),  # a catalog kind, but never ranked
        ("", ["bogus"]),
        ("revenue", '["metric"'),
        ("revenue", '{"kind": "metric"}'),
        ("revenue", [1]),
    ],
)
def test_invalid_kinds_are_refused_not_searched(
    adapter: SemanticLayerMCPAdapter, terms: str, kinds: Any
) -> None:
    out = adapter.call_tool("discover", {"terms": terms, "kinds": kinds})
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert out["errors"][0]["details"]["field"] == "kinds"
    assert NO_MATCH not in str(out)
    assert not _ids(out)


def test_a_misspelled_kind_argument_never_claims_a_closed_world(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    """The search is screened out and the misspelled filter ignored; the hint says both."""
    out = adapter.call_tool("discover", {"terms": "zxqv plugh", "kind": "metric"})
    assert out["ok"] is True
    (warning,) = out["warnings"]
    assert warning["code"] == "DISCOVER_UNKNOWN_ARG"
    assert warning["details"] == {"received": "kind", "expected": "kinds"}
    assert "low_relevance" in out and "no_matches" not in out
    text = _hints_text(out)
    assert "Nothing ranked" not in text and NO_MATCH not in text
    assert "not searched" in text and "'kind' argument was also ignored" in text


def test_a_real_search_over_the_requested_kinds_still_says_no_match(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    # "revenue" passes the relevance floor, so the search really runs over the
    # requested kind (a measure-only term never ranks a segment).
    out = adapter.call_tool("discover", {"terms": "revenue", "kinds": ["segment"]})
    assert out["ok"] is True and not _ids(out)
    assert "low_relevance" not in out and "out_of_scope" not in out
    assert "No semantic objects of kind ['segment'] matched" in _hints_text(out)
    assert out["no_matches"]["terms"] == "revenue"
    assert "['segment']" in out["no_matches"]["reason"]


def test_a_search_screened_out_before_it_ran_never_claims_a_kind_scoped_no_match(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "zxqv plugh", "kinds": ["metric"]})
    assert out["ok"] is True and not _ids(out)
    assert "low_relevance" in out and "no_matches" not in out
    text = _hints_text(out)
    assert NO_MATCH not in text and "['metric']" not in text
    assert "not searched" in text


@pytest.mark.parametrize("kinds", [["dimension_value"], "dimension_value", None])
def test_dimension_value_matches_never_come_with_a_no_match_hint(
    adapter: SemanticLayerMCPAdapter, kinds: Any
) -> None:
    arguments: dict[str, Any] = {"terms": "Food"}
    if kinds is not None:
        arguments["kinds"] = kinds
    out = adapter.call_tool("discover", arguments)
    assert out["ok"] is True
    assert out["dimension_values"], "the fixture must match a dimension value for 'Food'"
    assert "dimension.jaffle_item_product_type=jaffle" in _ids(out)
    assert "no_matches" not in out
    assert NO_MATCH not in _hints_text(out)


def test_refusal_hint_for_an_unknown_kind_names_the_valid_kinds(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "revenue", "kinds": ["metirc"]})
    assert out["errors"][0]["details"]["unknown_kinds"] == ["metirc"]
    assert out["errors"][0]["details"]["valid_kinds"] == sorted(DISCOVER_RANKED_KINDS)
    hints = out["errors"][0]["recovery_hints"]
    (hint,) = [h for h in hints if h["kind"] == "use_valid_kind"]
    assert "['metirc']" in hint["message"]
    assert all(kind in hint["message"] for kind in DISCOVER_RANKED_KINDS)
    assert hint["details"]["valid_kinds"] == sorted(DISCOVER_RANKED_KINDS)
    assert not [h for h in hints if h["kind"] == "use_string_or_array"]
    assert "argument_type" not in out["errors"][0]["details"]


def test_refusal_hint_for_a_malformed_kinds_value_still_teaches_the_encodings(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "revenue", "kinds": {"oops": True}})
    hints = out["errors"][0]["recovery_hints"]
    (hint,) = [h for h in hints if h["kind"] == "use_string_or_array"]
    assert "JSON array in a string" in hint["message"]
    assert not [h for h in hints if h["kind"] == "use_valid_kind"]


def test_discover_payload_refuses_a_bypass_of_the_transport_checks(runtime: Any) -> None:
    """Every caller goes through ``discover_payload``, so it guards on its own."""
    with pytest.raises(SemanticLayerError) as raised:
        discover_payload(runtime, terms="revenue", kinds=["metirc"])
    assert raised.value.code == "INVALID_MCP_ARGUMENTS"
    assert discover_payload(runtime, terms="revenue", kinds=["metric"])["metrics"]


def test_http_discover_reads_string_encoded_kinds_and_refuses_bad_ones(runtime: Any) -> None:
    service = SemanticHTTPService(runtime)
    encoded, status = service.handle(
        "POST", "/discover", {"terms": "revenue", "kinds": '["metric"]', "verbosity": "compact"}
    )
    listed, _ = service.handle(
        "POST", "/discover", {"terms": "revenue", "kinds": ["metric"], "verbosity": "compact"}
    )
    assert status == 200 and _ids(encoded)
    assert _ids(encoded) == _ids(listed)
    assert all(card["kind"] == "metric" for card in encoded["metrics"])
    assert not encoded["measures"]

    for bad, code in (('["metric"', "INVALID_REQUEST"), (["metirc"], "INVALID_MCP_ARGUMENTS")):
        with pytest.raises((HTTPInputError, SemanticLayerError)) as raised:
            service.handle("POST", "/discover", {"terms": "revenue", "kinds": bad})
        out, status = service.exception_payload(raised.value, stage="http")
        assert status == 400 and out["ok"] is False
        assert out["error"]["code"] == code
        assert not _ids(out)


@pytest.mark.parametrize(
    "kinds",
    [
        ["metirc"],
        "metirc",
        "metric,bogus",
        ["dimension_value"],
        ["measure"],
        ["segment"],
        ["entity"],
    ],
)
def test_resource_grant_mode_refuses_a_kind_it_cannot_produce_and_says_which(
    runtime: Any, granted: Any, kinds: Any
) -> None:
    """Grant errors are sanitised, so the kinds refusal must still keep its recovery detail."""
    service = SemanticHTTPService(runtime)
    # HTTP identity is the request's resolved context, never the body.
    with pytest.raises(SemanticLayerError) as raised:
        service.handle("POST", "/discover", {"terms": "revenue", "kinds": kinds}, context=granted)
    out, status = service.exception_payload(raised.value, stage="http")
    assert status == 400 and out["ok"] is False
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    details = out["error"]["details"]
    assert details["field"] == "kinds" and details["unknown_kinds"]
    assert details["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    assert not _ids(out)


@pytest.mark.parametrize("kinds", [["metirc"], ["measure"], ["dimension_value"]])
def test_resource_grant_mode_mcp_refusal_keeps_its_recovery_hint(
    adapter: SemanticLayerMCPAdapter, granted: Any, kinds: list[str]
) -> None:
    out = adapter.call_tool(
        "discover", {"terms": "revenue", "kinds": kinds}, request_context=granted
    )
    assert out["ok"] is False and out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert out["errors"][0]["details"]["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    (hint,) = [h for h in out["errors"][0]["recovery_hints"] if h["kind"] == "use_valid_kind"]
    assert hint["details"]["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    assert NO_MATCH not in str(out)
    # Following the hint must work on the same transport: no hint names a refused kind.
    for kind in hint["details"]["valid_kinds"]:
        followed = adapter.call_tool(
            "discover", {"terms": "revenue", "kinds": [kind]}, request_context=granted
        )
        assert followed["ok"] is True, kind


@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_resource_grant_mode_searches_a_kind_the_grant_produces_on_every_transport(
    runtime: Any, adapter: SemanticLayerMCPAdapter, granted: Any, transport: str
) -> None:
    body = {"terms": "revenue", "kinds": ["temporal_role"]}
    if transport == "mcp":
        out = adapter.call_tool("discover", body, request_context=granted)
    else:
        out, _status = SemanticHTTPService(runtime).handle(
            "POST", "/discover", body, context=granted
        )
    assert out["ok"] is True
    assert "temporal_roles" in out and "no_matches" not in out
    assert NO_MATCH not in _hints_text(out)


def test_grant_catalog_keeps_its_shape(runtime: Any, grant: dict[str, Any]) -> None:
    """The grant refusal lives in discover's listing; the shared catalog is unchanged."""
    from semantic_rails.catalog_service import resolve_catalog

    granted = resolve_catalog(runtime, view="summary", verbosity="summary", policy_context=grant)
    empty = ("measure", "entity", "segment", "relationship", "value_domain")
    plural = {"entity": "entities", "relationship": "relationships"}
    for kind in empty:
        assert granted[f"{kind}_ids"] == [], kind
        assert granted["counts"][plural.get(kind, f"{kind}s")] == 0, kind
    assert granted["metric_ids"]
    service = SemanticHTTPService(runtime)
    out, _ = service.handle(
        "POST", "/catalog", {"verbosity": "summary"}, context=context_from_policy_context(grant)
    )
    assert out["catalog"] == granted


@pytest.mark.parametrize(
    "kinds", [["measure"], ["segment"], ["entity"], ["relationship"], ["value_domain"], ["bogus"]]
)
def test_resource_grant_mode_listing_refuses_a_kind_it_cannot_produce(
    adapter: SemanticLayerMCPAdapter, granted: Any, kinds: list[str]
) -> None:
    """Empty terms list ids; the kind check is the ranked path's, not the wider catalog's."""
    out = adapter.call_tool("discover", {"terms": "", "kinds": kinds}, request_context=granted)
    assert out["ok"] is False and out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    issue = out["errors"][0]
    assert issue["details"]["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    assert "catalog" not in out
    (hint,) = [h for h in out["errors"][0]["recovery_hints"] if h["kind"] == "use_valid_kind"]
    assert hint["details"]["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    # Following the hint on the same call shape lists that kind's ids.
    for kind in hint["details"]["valid_kinds"]:
        followed = adapter.call_tool(
            "discover", {"terms": "", "kinds": [kind]}, request_context=granted
        )
        assert followed["ok"] is True, kind
        assert f"{kind}_ids" in followed["catalog"], kind


def test_listing_hint_outside_grant_mode_names_only_kinds_it_can_list(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "", "kinds": ["bogus"]})
    assert out["ok"] is False and out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    (hint,) = [h for h in out["errors"][0]["recovery_hints"] if h["kind"] == "use_valid_kind"]
    for kind in hint["details"]["valid_kinds"]:
        followed = adapter.call_tool("discover", {"terms": "", "kinds": [kind]})
        assert followed["ok"] is True and f"{kind}_ids" in followed["catalog"], kind


def test_resource_grant_mode_searches_its_kinds_and_never_claims_no_match(
    runtime: Any, adapter: SemanticLayerMCPAdapter, grant: dict[str, Any], granted: Any
) -> None:
    """A grant searches a filtered view, so it cannot say the catalog has no match."""
    context = {"policy_context": grant}
    found = discover_payload(runtime, terms="revenue", kinds=["metric"], partial_query=context)
    assert found["metrics"]
    empty = discover_payload(runtime, terms="zxqv", kinds=["dimension"], partial_query=context)
    assert not _ids(empty) and "no_matches" not in empty
    miss = adapter.call_tool(
        "discover", {"terms": "zxqv", "kinds": ["dimension"]}, request_context=granted
    )
    assert miss["ok"] is True and not _ids(miss)
    assert NO_MATCH not in _hints_text(miss)


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_that_would_empty_the_buckets_is_refused(runtime: Any, limit: int) -> None:
    """A zero limit cuts every bucket to nothing, which would read as "no match"."""
    with pytest.raises(SemanticLayerError) as raised:
        discover_payload(runtime, terms="revenue", limit=limit)
    assert raised.value.details["field"] == "limit"


def test_http_empty_terms_ranks_and_refuses_what_it_cannot_rank(runtime: Any) -> None:
    service = SemanticHTTPService(runtime)
    with pytest.raises(SemanticLayerError) as raised:
        service.handle("POST", "/discover", {"terms": "", "kinds": ["temporal_role"]})
    assert raised.value.details["unknown_kinds"] == ["temporal_role"]


class _FakeRuntime:
    def close(self) -> None:
        pass


def _cli_discover(monkeypatch: pytest.MonkeyPatch, kinds: str) -> dict[str, Any]:
    """Run ``discover`` from the CLI and return the arguments it passed on."""
    seen: dict[str, Any] = {}

    def fake_discover(runtime: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli_query, "discover_payload", fake_discover)
    monkeypatch.setattr(cli_query, "_runtime_from_package_or_path", lambda args: _FakeRuntime())
    monkeypatch.setattr(cli_query, "_print", lambda payload: None)
    args = argparse.Namespace(
        terms="revenue", kinds=kinds, query_json="", stage="", verbosity="compact", limit=10
    )
    cli_query.cmd_discover(args)
    return seen


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [("metric, measure", ["metric", "measure"]), ('["metric"]', ["metric"])],
)
def test_cli_kinds_use_the_shared_list_normaliser(
    monkeypatch: pytest.MonkeyPatch, kinds: str, expected: list[str]
) -> None:
    assert _cli_discover(monkeypatch, kinds)["kinds"] == expected


def test_cli_refuses_a_kinds_value_that_does_not_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SemanticLayerError) as raised:
        _cli_discover(monkeypatch, '["metric"')
    assert raised.value.code == "INVALID_MCP_ARGUMENTS"
    assert raised.value.details["field"] == "kinds"


PLURALS = [
    (["measures"], ["measure"]),
    ("metrics,dimensions", ["metric", "dimension"]),
    ('["entities"]', ["entity"]),
    (["dimension_values", "segments"], ["dimension_value", "segment"]),
    (["relationships"], ["relationship"]),
]


@pytest.mark.parametrize(("plural", "singular"), PLURALS)
def test_a_plural_kind_means_its_singular(
    adapter: SemanticLayerMCPAdapter, plural: Any, singular: list[str]
) -> None:
    terms = "food store revenue"
    named = adapter.call_tool("discover", {"terms": terms, "kinds": plural})
    expected = adapter.call_tool("discover", {"terms": terms, "kinds": singular})
    assert named["ok"] is True, named
    assert _ids(named) == _ids(expected) and _ids(named)


@pytest.mark.parametrize(
    ("plural", "singular"),
    [(["segments"], ["segment"]), ('["relationships", "metrics"]', ["relationship", "metric"])],
)
def test_a_plural_kind_filters_the_empty_terms_listing(
    adapter: SemanticLayerMCPAdapter, plural: Any, singular: list[str]
) -> None:
    listed = adapter.call_tool("discover", {"terms": "", "kinds": plural})["catalog"]
    assert listed == adapter.call_tool("discover", {"terms": "", "kinds": singular})["catalog"]
    assert {f"{kind}_ids" for kind in singular} <= set(listed)


def test_a_plural_kind_means_its_singular_on_http_and_under_a_grant(
    runtime: Any, granted: Any
) -> None:
    service = SemanticHTTPService(runtime)
    out, status = service.handle("POST", "/discover", {"terms": "revenue", "kinds": ["metrics"]})
    assert status == 200 and out["metrics"] and not out["measures"]
    out, status = service.handle(
        "POST", "/discover", {"terms": "revenue", "kinds": ["metrics"]}, context=granted
    )
    assert status == 200 and out["metrics"]


RELATIONSHIP_FIELDS = (
    "source_entity",
    "target_entity",
    "cardinality",
    "allowed_directions",
    "source_columns",
    "target_columns",
)


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_discover_ranks_relationships_with_their_join_fields(
    adapter: SemanticLayerMCPAdapter, verbosity: str
) -> None:
    out = adapter.call_tool(
        "discover", {"terms": "store", "kinds": ["relationship"], "verbosity": verbosity}
    )
    assert out["ok"] is True, out
    rows = {row["id"]: row for row in out["relationships"]}
    assert "relationship.orders_store" in rows
    for relationship_id, row in rows.items():
        card = adapter.call_tool("inspect", {"object_id": relationship_id, "verbosity": "compact"})
        for field in RELATIONSHIP_FIELDS:
            assert row[field] == card["card"][field], (relationship_id, field)
    for bucket in ("measures", "metrics", "dimensions", "entities", "segments"):
        assert not out.get(bucket), bucket
    assert NO_MATCH not in _hints_text(out)


def test_relationships_are_ranked_only_when_kinds_names_them(
    adapter: SemanticLayerMCPAdapter, runtime: Any
) -> None:
    assert "relationships" not in adapter.call_tool("discover", {"terms": "store"})
    assert "relationships" not in discover_payload(runtime, terms="store")
    found = discover_payload(runtime, terms="store", kinds=["relationship"])
    assert "relationship.orders_store" in {row["id"] for row in found["relationships"]}
    miss = adapter.call_tool("discover", {"terms": "revenue", "kinds": ["relationship"]})
    assert miss["ok"] is True and not miss["relationships"]
    assert "No semantic objects of kind ['relationship'] matched" in _hints_text(miss)


@pytest.mark.parametrize(
    ("terms", "kind", "plural"),
    [
        ("revenue", "measure", "measures"),
        ("revenue", "metric", "metrics"),
        ("store", "dimension", "dimensions"),
        ("store", "entity", "entities"),
        ("customer", "segment", "segments"),
        ("store", "dimension_value", "dimension_values"),
        ("store", "relationship", "relationships"),
        ("", "temporal_role", "temporal_roles"),
        ("", "value_domain", "value_domains"),
        ("", "relationship", "relationships"),
        ("", "metric", "metrics"),
    ],
)
@pytest.mark.parametrize("plural_form", [False, True], ids=["singular", "plural"])
@pytest.mark.parametrize("encoding", ["array", "string", "json-string"])
def test_advertised_discover_schema_accepts_supported_kinds(
    adapter: SemanticLayerMCPAdapter,
    terms: str,
    kind: str,
    plural: str,
    plural_form: bool,
    encoding: str,
) -> None:
    name = plural if plural_form else kind
    kinds = (
        [name] if encoding == "array" else json.dumps([name]) if encoding == "json-string" else name
    )
    arguments = {"terms": terms, "kinds": kinds}
    tool = next(row for row in list_tool_definitions() if row["name"] == "discover")
    Draft202012Validator(tool["inputSchema"]).validate(arguments)
    response = adapter.call_tool("discover", arguments)
    assert response["ok"] is True, response
    if not terms:
        assert f"{kind}_ids" in response["catalog"]


def test_advertised_discover_schema_accepts_comma_separated_kinds(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    arguments = {"terms": "revenue by store", "kinds": "metrics, dimensions"}
    tool = next(row for row in list_tool_definitions() if row["name"] == "discover")
    Draft202012Validator(tool["inputSchema"]).validate(arguments)
    response = adapter.call_tool("discover", arguments)
    assert response["ok"] is True and response["metrics"] and response["dimensions"]
