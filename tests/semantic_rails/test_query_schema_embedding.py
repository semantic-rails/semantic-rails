"""execute embeds a compact form of query.v2.json; both must accept and refuse the same calls.

The corpus is every call in the answer ledgers and the example package's tests, one call per
expression kind, and each expression in them broken a few ways.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from semantic_rails.contracts import load_contract
from semantic_rails.mcp import list_tool_definitions

ROOT = Path(__file__).resolve().parents[2]
LEDGERS = [
    *sorted(ROOT.glob("tests/integration/correctness/*/tests/*.yml")),
    *sorted(ROOT.glob("configs/semantic_rails/jaffle_shop/tests/*.yml")),
]
PUBLISHED = load_contract("query.v2.json")
KINDS = {
    name.removeprefix("Kind_"): schema
    for name, schema in PUBLISHED["$defs"].items()
    if name.startswith("Kind_")
}
REF = "measure.shop.revenue"
EXPRESSION_KEYS = (
    "input",
    "left",
    "right",
    "numerator",
    "denominator",
    "base",
    "converted",
    "expr",
)


def _ledger_calls() -> Iterator[dict[str, Any]]:
    def walk(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            query = node.get("query")
            if isinstance(query, dict) and "select" in query:
                yield query
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    for path in LEDGERS:
        yield from walk(yaml.safe_load(path.read_text(encoding="utf-8")))


def _placeholder(schema: dict[str, Any]) -> Any:
    if "$ref" in schema:
        return REF
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if schema.get("type") == "array":
        return [_placeholder(schema["items"])]
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        return {key: _placeholder(properties[key]) for key in schema.get("required", [])}
    return "x"


def _kind_calls() -> Iterator[dict[str, Any]]:
    """One call per kind, with only its required keys."""
    for kind, schema in sorted(KINDS.items()):
        expression = {key: _placeholder(schema["properties"][key]) for key in schema["required"]}
        expression["kind"] = kind
        yield {"select": [{"expression": expression, "as": kind}]}


def _expressions(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if isinstance(node.get("kind"), str):
            yield node
        for value in node.values():
            yield from _expressions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _expressions(value)


def _break(expression: dict[str, Any], how: str) -> bool:
    """Break ``expression`` in place; False when ``how`` does not apply to it."""
    if how == "unknown key":
        expression["unknown_key"] = 1
    elif how == "required key dropped":
        required = [
            key for key in KINDS.get(expression["kind"], {}).get("required", ()) if key != "kind"
        ]
        if not required:
            return False
        expression.pop(required[0], None)
    elif how == "unknown kind":
        expression["kind"] = "no_such_kind"
    elif how == "kind dropped":
        expression.pop("kind")
    elif how == "operand not a ref":
        key = next((key for key in EXPRESSION_KEYS if key in expression), None)
        if key is None:
            return False
        expression[key] = "not a ref"
    return True


def _corpus() -> list[dict[str, Any]]:
    calls = [*_ledger_calls(), *_kind_calls()]
    cases = list(calls)
    for call in calls:
        for index in range(len(list(_expressions(call)))):
            for how in (
                "unknown key",
                "required key dropped",
                "unknown kind",
                "kind dropped",
                "operand not a ref",
            ):
                broken = copy.deepcopy(call)
                if _break(list(_expressions(broken))[index], how):
                    cases.append(broken)
    return cases


CORPUS = _corpus()


@pytest.fixture(scope="module")
def embedded() -> Draft202012Validator:
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    return Draft202012Validator(execute["inputSchema"])


def test_the_corpus_covers_every_kind_and_both_verdicts() -> None:
    published = Draft202012Validator(PUBLISHED)
    seen = {expression["kind"] for call in CORPUS for expression in _expressions(call)}
    assert set(KINDS) <= seen
    accepted = sum(published.is_valid(call) for call in CORPUS)
    assert accepted >= 200 and len(CORPUS) - accepted >= 500, (accepted, len(CORPUS))


def test_execute_accepts_and_refuses_exactly_what_the_published_schema_does(
    embedded: Draft202012Validator,
) -> None:
    published = Draft202012Validator(PUBLISHED)
    differing = [
        call for call in CORPUS if embedded.is_valid({"query": call}) != published.is_valid(call)
    ]
    assert not differing, differing[:3]
