"""query.v2.json and the parser give every condition the same verdict; execute's schema lists
the words and states the rest once.

Each operator's value shape (one value for ranges, text for contains, true or false for missing,
one or several values for is and not) and its spellings (any case, any spacing, the SQL aliases)
come from the operator registry, so the published schema refuses what the parser refuses and
accepts what it accepts. The tool's schema takes the words and says in one sentence which
values each takes and which SQL symbols read as them.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from semantic_rails import operators
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import list_tool_definitions
from semantic_rails.query_call import read_call

FIELD = "dimension.jaffle_store_name"
SPELLINGS = [
    *operators.WORDS,
    *operators.ALIASES,
    *(word.upper() for word in operators.WORDS),
    *(alias.upper() for alias in operators.ALIASES),
    "Gt",
    " gte ",
    "NOT   IN",
    "not\tin",
    "Missing",
    "is null",
    "like",
    "between",
    "",
    "g t",
    5,
    None,
    True,
    ["is"],
]
VALUES: list[Any] = [
    "Brooklyn",
    7,
    2.5,
    True,
    False,
    None,
    [],
    ["Brooklyn"],
    ["Brooklyn", "Philadelphia"],
    [1, 2],
    ["Brooklyn", None],
    [["nested"]],
    {"value": 1},
]
CASES = [(op, value) for op in SPELLINGS for value in VALUES]


def _call(op: Any, value: Any) -> dict[str, Any]:
    return {
        "select": ["measure.jaffle.order_count"],
        "where": [{"field": FIELD, "op": op, "value": value}],
    }


def _parser_accepts(call: dict[str, Any]) -> bool:
    try:
        read_call(call)
    except SemanticLayerError:
        return False
    return True


@pytest.fixture(scope="module")
def validators() -> dict[str, Draft202012Validator]:
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    return {
        "published": Draft202012Validator(load_contract("query.v2.json")),
        "embedded": Draft202012Validator(execute["inputSchema"]),
    }


def test_the_published_schema_gives_every_condition_the_parsers_verdict(validators) -> None:
    differing = [
        (op, value)
        for op, value in CASES
        if validators["published"].is_valid(_call(op, value)) != _parser_accepts(_call(op, value))
    ]
    assert not differing, differing[:5]


def test_execute_takes_the_words_and_states_their_values_once(validators) -> None:
    differing = [
        (op, value)
        for op, value in CASES
        if validators["embedded"].is_valid({"query": _call(op, value)}) != (op in operators.WORDS)
    ]
    assert not differing, differing[:5]
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    op = execute["inputSchema"]["$defs"]["Op"]
    assert op["enum"] == list(operators.WORDS)
    assert [alias for alias in operators.ALIASES if alias not in op["description"]] == []
    shapes = {"a value or a list": ["is", "not"], "one value": ["gt", "gte", "lt", "lte"]}
    for phrase, words in {**shapes, "text": ["contains"], "true or false": ["missing"]}.items():
        assert f"{'/'.join(words)}: {phrase}" in op["description"]


@pytest.mark.parametrize("op", ["gt", "GTE", "<", "lte"])
def test_a_range_takes_one_value_not_a_list(validators, op) -> None:
    call = _call(op, ["Brooklyn", "Philadelphia"])
    assert not _parser_accepts(call)
    assert not validators["published"].is_valid(call)
    assert _parser_accepts(_call(op, "Brooklyn")) and validators["published"].is_valid(
        _call(op, "Brooklyn")
    )


def test_the_corpus_has_both_verdicts() -> None:
    accepted = sum(_parser_accepts(_call(op, value)) for op, value in CASES)
    assert 100 <= accepted <= len(CASES) - 100, accepted
