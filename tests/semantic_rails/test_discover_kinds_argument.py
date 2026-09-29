"""``discover`` never answers "no match" because of how ``kinds`` was encoded.

Invariant: the "no semantic objects matched" text appears only when the search
ran over the requested kinds and found nothing. A JSON-encoded ``kinds`` string
means the same as the array, and a value that names no real kind is refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.http_request import HTTPInputError, coerce_string_list
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import DISCOVER_RANKED_KINDS, discover_payload
from semantic_rails.request_payload import parse_string_list

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
        for bucket in ("measures", "metrics", "segments", "dimensions", "entities")
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
    ['["metric"', '{"a": 1}', "[1, 2]", '[["metric"]]', ["metric", 3], {"oops": True}, 7, True],
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


def test_a_real_search_over_the_requested_kinds_still_says_no_match(
    adapter: SemanticLayerMCPAdapter,
) -> None:
    out = adapter.call_tool("discover", {"terms": "zxqv plugh", "kinds": ["metric"]})
    assert out["ok"] is True and not _ids(out)
    assert "No semantic objects of kind ['metric'] matched" in _hints_text(out)


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

    for bad in ('["metric"', ["metirc"]):
        with pytest.raises((HTTPInputError, SemanticLayerError)) as raised:
            service.handle("POST", "/discover", {"terms": "revenue", "kinds": bad})
        out, status = service.exception_payload(raised.value, stage="http")
        assert status == 400 and out["ok"] is False
        assert not _ids(out)
