"""execute embeds a compact form of query.v2.json: the same fields, words and rules, with each
expression kind's keys written as one line of a table instead of a schema per kind.

The tool's schema refuses no call the published one accepts, and the two differ only inside an
expression, where the parser refuses an unknown or missing key at its path. The corpus is every
call in the answer ledgers and the example package's tests, one call per expression kind, and
each expression in them broken a few ways.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from semantic_rails import operators
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


def test_execute_refuses_no_call_the_published_schema_accepts(
    embedded: Draft202012Validator,
) -> None:
    published = Draft202012Validator(PUBLISHED)
    refused = [
        call
        for call in CORPUS
        if published.is_valid(call) and not embedded.is_valid({"query": call})
    ]
    assert not refused, refused[:3]


# A value of each operator's value shape: the published schema checks the shape, the tool's
# Op description states it.
SAMPLES = {"scalar_or_list": "x", "scalar": 1, "text": "x", "boolean": True}


def _plain(call: dict[str, Any]) -> dict[str, Any]:
    """``call`` with each select expression an id and each condition's value one its op word
    takes: what is left is the call outside the kind table and the Op description."""
    plain = copy.deepcopy(call)
    for item in plain.get("select") or []:
        if isinstance(item, dict) and isinstance(item.get("expression"), dict):
            item["expression"] = REF
    conditions = [*(plain.get("where") or []), *(plain.get("having") or [])]
    for item in list(conditions):
        if isinstance(item, dict):
            conditions.extend(c for key in ("any", "none", "having") for c in item.get(key) or [])
    for item in conditions:
        if isinstance(item, dict) and item.get("op") in operators.OPERATORS:
            item["value"] = SAMPLES[operators.OPERATORS[item["op"]].value_shape]
    return plain


def test_execute_and_the_published_schema_differ_only_inside_expressions(
    embedded: Draft202012Validator,
) -> None:
    published = Draft202012Validator(PUBLISHED)
    differing = [
        call
        for call in map(_plain, CORPUS)
        if embedded.is_valid({"query": call}) != published.is_valid(call)
    ]
    assert not differing, differing[:3]


def _split(text: str, separator: str) -> list[str]:
    """``text`` split at each ``separator`` outside brackets."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        depth += (char in "[{") - (char in "]}")
        if char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    return [*parts, text[start:]]


def test_the_kind_table_names_every_kind_with_its_keys_and_their_shapes() -> None:
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    table = execute["inputSchema"]["$defs"]["Expression"]["description"].split("): ", 1)[1]
    written: dict[str, dict[str, tuple[bool, str]]] = {}
    for row in _split(table, ";"):
        names, keys = row.split(":", 1)
        for kind in names.split("/"):
            written[kind] = {}
            for key in _split(keys, ","):
                name, _, shape = key.partition(":")
                written[kind][name.rstrip("*")] = (name.endswith("*"), shape)
    assert written.keys() == KINDS.keys()
    for kind, schema in KINDS.items():
        keys = {key: value for key, value in schema["properties"].items() if key != "kind"}
        assert keys.keys() == written[kind].keys(), kind
        for key, value in keys.items():
            # A required key is starred; a key whose value the schema shapes has a shape, and it
            # names every word the key takes.
            required, shape = written[kind][key]
            assert required == (key in schema["required"]), (kind, key)
            assert bool(shape) == bool(value), (kind, key)
            assert all(word in shape for word in _values(value)), (kind, key)


def _values(node: Any) -> list[str]:
    """Every enum and const value inside a schema."""
    if isinstance(node, list):
        return [value for item in node for value in _values(item)]
    if not isinstance(node, dict):
        return []
    own = [*map(str, node.get("enum", [])), *([str(node["const"])] if "const" in node else [])]
    return own + [value for child in node.values() for value in _values(child)]


def _fields(node: Any, path: str, out: dict[str, set[str]], defs: dict[str, Any]) -> None:
    """Each field path of a call a schema names, with the enum and const values there; an
    expression's keys are the kind table's."""
    reference = str(node.get("$ref", "")).rsplit("/", 1)[-1]
    if reference:
        if reference != "Expression":
            _fields(defs[reference], path, out, defs)
        return
    values = out.setdefault(path, set())
    values.update(json.dumps(value) for value in node.get("enum", []))
    if "const" in node:
        values.add(json.dumps(node["const"]))
    for key, child in node.get("properties", {}).items():
        _fields(child, f"{path}.{key}", out, defs)
    if isinstance(node.get("items"), dict):
        _fields(node["items"], f"{path}[]", out, defs)
    for key in ("anyOf", "oneOf", "allOf"):
        for child in node.get(key, []):
            _fields(child, path, out, defs)


def test_execute_names_every_field_and_value_of_the_published_schema() -> None:
    execute = next(tool for tool in list_tool_definitions() if tool["name"] == "execute")
    published: dict[str, set[str]] = {}
    _fields(PUBLISHED, "query", published, PUBLISHED["$defs"])
    tool: dict[str, set[str]] = {}
    schema = execute["inputSchema"]
    _fields(schema["properties"]["query"], "query", tool, schema["$defs"])
    # A condition value's list form is the Op description's (see test_query_schema_operators).
    published = {
        path: values for path, values in published.items() if not path.endswith(".value[]")
    }
    assert len(published) > 40
    assert sorted(published) == sorted(tool)
    assert {
        path: values - tool[path] for path, values in published.items() if values - tool[path]
    } == {}
