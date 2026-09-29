"""``discover`` never answers "no match" because of how ``kinds`` was encoded.

Invariant: the "no semantic objects matched" text appears only when the search
ran over the requested kinds and found nothing (the payload's own ``no_matches``). A JSON-encoded ``kinds`` string
means the same as the array, and a value that names no real kind is refused.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.cli.commands import query as cli_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.http_request import HTTPInputError, coerce_string_list
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import discover_payload
from semantic_rails.request_payload import DISCOVER_RANKED_KINDS, parse_string_list
from semantic_rails.resource_access import GRANT_DISCOVER_KINDS

NO_MATCH = "No semantic objects"


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Any]:
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


@pytest.fixture()
def adapter(runtime: Any) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(runtime)
    yield adapter
    adapter.close()


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
        )
        for card in response.get(bucket) or []
    ]


def _hints_text(response: dict[str, Any]) -> str:
    return " ".join(str(hint.get("message", "")) for hint in response.get("recovery_hints") or [])


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
    ],
)
def test_parse_string_list_reads_every_encoding(value: Any, expected: list[str]) -> None:
    assert parse_string_list(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        '["metric"',
        '{"a": 1}',
        "[1, 2]",
        '[["metric"]]',
        ["metric", 3],
        {"oops": True},
        7,
        True,
        "[" * 100_000,
    ],
)
def test_parse_string_list_refuses_malformed_values(value: Any) -> None:
    with pytest.raises(ValueError):
        parse_string_list(value)


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
    assert out["error"]["details"]["field"] == "kinds"
    assert NO_MATCH not in str(out)
    assert not _ids(out)


def test_refusal_names_the_valid_kinds(adapter: SemanticLayerMCPAdapter) -> None:
    out = adapter.call_tool("discover", {"terms": "revenue", "kinds": ["metirc"]})
    assert out["error"]["details"]["unknown_kinds"] == ["metirc"]
    assert out["error"]["details"]["valid_kinds"] == sorted(DISCOVER_RANKED_KINDS)


def test_misspelled_kind_argument_never_claims_a_closed_world(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "zxqv plugh", "kind": "metric"})
    assert out["ok"] is True
    assert {w["code"] for w in out["warnings"]} == {"DISCOVER_UNKNOWN_ARG"}
    text = _hints_text(out)
    assert NO_MATCH not in text
    assert "'kinds'" in text


def test_a_screened_out_search_with_a_misspelled_kind_never_says_nothing_ranked(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "zxqv plugh", "kind": "metric"})
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
    assert out["no_matches"]["reason"] == (
        "no candidate of kind ['segment'] matched the supplied search terms"
    )


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
    hints = out["recovery_hints"]
    (hint,) = [h for h in hints if h["kind"] == "use_valid_kind"]
    assert "['metirc']" in hint["message"]
    assert all(kind in hint["message"] for kind in DISCOVER_RANKED_KINDS)
    assert hint["details"]["valid_kinds"] == sorted(DISCOVER_RANKED_KINDS)
    assert not [h for h in hints if h["kind"] == "use_string_or_array"]
    assert "argument_type" not in out["error"]["details"]


def test_refusal_hint_for_a_malformed_kinds_value_still_teaches_the_encodings(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "revenue", "kinds": {"oops": True}})
    hints = out["recovery_hints"]
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


def _granted(runtime: Any, **extra: Any) -> dict[str, Any]:
    """A policy context in resource-grant mode: one allowed metric."""
    metric = discover_payload(runtime, terms="revenue")["metrics"][0]["id"]
    return {"metric_allowlist": [metric], **extra}


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
    runtime: Any, kinds: Any
) -> None:
    """Grant errors are sanitised, so the kinds refusal must still keep its recovery detail."""
    service = SemanticHTTPService(runtime)
    with pytest.raises(SemanticLayerError) as raised:
        service.handle(
            "POST",
            "/discover",
            {"terms": "revenue", "kinds": kinds, "policy_context": _granted(runtime)},
        )
    out, status = service.exception_payload(raised.value, stage="http")
    assert status == 400 and out["ok"] is False
    assert out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    details = out["error"]["details"]
    assert details["field"] == "kinds" and details["unknown_kinds"]
    assert details["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    assert not _ids(out)


@pytest.mark.parametrize("kinds", [["metirc"], ["measure"], ["dimension_value"]])
def test_resource_grant_mode_mcp_refusal_keeps_its_recovery_hint(
    runtime: Any, adapter: SemanticLayerMCPAdapter, kinds: list[str]
) -> None:
    out = adapter.call_tool(
        "discover", {"terms": "revenue", "kinds": kinds, "policy_context": _granted(runtime)}
    )
    assert out["ok"] is False and out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    (hint,) = [h for h in out["recovery_hints"] if h["kind"] == "use_valid_kind"]
    assert hint["details"]["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)
    assert NO_MATCH not in str(out)
    # Following the hint must work on the same transport: no hint names a refused kind.
    for kind in hint["details"]["valid_kinds"]:
        followed = adapter.call_tool(
            "discover", {"terms": "revenue", "kinds": [kind], "policy_context": _granted(runtime)}
        )
        assert followed["ok"] is True, kind


def _grant_discover(
    runtime: Any, adapter: SemanticLayerMCPAdapter, transport: str, kinds: list[str]
) -> dict[str, Any]:
    body = {"terms": "revenue", "kinds": kinds, "policy_context": _granted(runtime)}
    if transport == "mcp":
        return adapter.call_tool("discover", body)
    service = SemanticHTTPService(runtime)
    try:
        out, status = service.handle("POST", "/discover", body)
    except SemanticLayerError as exc:
        out, status = service.exception_payload(exc, stage="http")
        assert status == 400
    return out


@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_resource_grant_mode_searches_a_kind_the_grant_produces_on_every_transport(
    runtime: Any, adapter: SemanticLayerMCPAdapter, transport: str
) -> None:
    out = _grant_discover(runtime, adapter, transport, ["temporal_role"])
    assert out["ok"] is True
    assert "temporal_roles" in out and "no_matches" not in out
    assert NO_MATCH not in _hints_text(out)


@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_resource_grant_mode_refuses_a_ranked_kind_the_grant_cannot_produce(
    runtime: Any, adapter: SemanticLayerMCPAdapter, transport: str
) -> None:
    out = _grant_discover(runtime, adapter, transport, ["measure"])
    assert out["ok"] is False and out["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert out["error"]["details"]["valid_kinds"] == sorted(GRANT_DISCOVER_KINDS)


def test_resource_grant_mode_listing_omits_the_kinds_it_cannot_produce(
    runtime: Any, adapter: SemanticLayerMCPAdapter
) -> None:
    out = adapter.call_tool("discover", {"terms": "", "policy_context": _granted(runtime)})
    listed = {key.removesuffix("_ids") for key in out["catalog"] if key.endswith("_ids")}
    assert listed and listed <= GRANT_DISCOVER_KINDS


def test_resource_grant_mode_still_searches_the_kinds_it_produces(runtime: Any) -> None:
    context = {"policy_context": _granted(runtime)}
    found = discover_payload(runtime, terms="revenue", kinds=["metric"], partial_query=context)
    assert found["metrics"]


def test_resource_grant_mode_never_claims_no_match(
    runtime: Any, adapter: SemanticLayerMCPAdapter
) -> None:
    """A grant searches a filtered view, so it cannot say the catalog has no match."""
    context = _granted(runtime)
    empty = discover_payload(
        runtime, terms="zxqv", kinds=["dimension"], partial_query={"policy_context": context}
    )
    assert not _ids(empty) and "no_matches" not in empty
    miss = adapter.call_tool(
        "discover", {"terms": "zxqv", "kinds": ["dimension"], "policy_context": context}
    )
    assert not _ids(miss)
    assert NO_MATCH not in _hints_text(miss)


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_that_would_empty_the_buckets_is_refused(runtime: Any, limit: int) -> None:
    """A zero limit cuts every bucket to nothing, which would read as "no match"."""
    with pytest.raises(SemanticLayerError) as raised:
        discover_payload(runtime, terms="revenue", limit=limit)
    assert raised.value.details["field"] == "limit"


def test_http_empty_terms_listing_refuses_what_it_cannot_rank(runtime: Any) -> None:
    service = SemanticHTTPService(runtime)
    with pytest.raises(SemanticLayerError) as raised:
        service.handle("POST", "/discover", {"terms": "", "kinds": ["temporal_role"]})
    assert raised.value.details["unknown_kinds"] == ["temporal_role"]


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [("metric, measure", ["metric", "measure"]), ('["metric"]', ["metric"])],
)
def test_cli_kinds_use_the_shared_list_normaliser(
    monkeypatch: pytest.MonkeyPatch, kinds: str, expected: list[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_discover(runtime: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    class FakeRuntime:
        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_query, "discover_payload", fake_discover)
    monkeypatch.setattr(cli_query, "_runtime_from_package_or_path", lambda args: FakeRuntime())
    monkeypatch.setattr(cli_query, "_print", lambda payload: None)
    args = argparse.Namespace(
        terms="revenue", kinds=kinds, query_json="", stage="", verbosity="compact", limit=10
    )
    monkeypatch.setattr(cli_query, "_query_with_policy_context", lambda query, args: None)
    cli_query.cmd_discover(args)
    assert seen["kinds"] == expected


def test_cli_refuses_a_kinds_value_that_does_not_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRuntime:
        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_query, "_runtime_from_package_or_path", lambda args: FakeRuntime())
    monkeypatch.setattr(cli_query, "_query_with_policy_context", lambda query, args: None)
    args = argparse.Namespace(
        terms="revenue", kinds='["metric"', query_json="", stage="", verbosity="compact", limit=10
    )
    with pytest.raises(SemanticLayerError) as raised:
        cli_query.cmd_discover(args)
    assert raised.value.code == "INVALID_MCP_ARGUMENTS"
    assert raised.value.details["field"] == "kinds"
