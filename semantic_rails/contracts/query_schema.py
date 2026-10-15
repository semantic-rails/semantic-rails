"""The query call's JSON Schema, generated from the front door's own tables.

``query.v2.json`` is this document; the ``execute`` MCP tool embeds a compact form of it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _kind_property(kind: str, key: str) -> dict[str, Any]:
    from semantic_rails.query_call import EXPRESSION_KEYS, EXPRESSION_LISTS, OVER

    expression = {"$ref": "#/$defs/Expression"}
    if kind == "share":
        # A share reads one id, over group_by ids or "period" (a parser rule checks which).
        if key == "input":
            return {"$ref": "#/$defs/Ref"}
        if key == "over":
            return {"type": "array", "items": {"type": "string"}, "minItems": 1}
    if key in EXPRESSION_KEYS or key == "else" or (key == "over" and kind == "distribution"):
        return expression
    if key == "value" and kind in {"aggregate_if", "nullif", "date_add"}:
        return expression
    if key == "where" and kind == "entity_value":
        # Conditions on each entity's value itself; dimension filters go in the call's where.
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"kind": {"const": "value_filter"}, "op": {}, "value": {}},
                "additionalProperties": False,
                "allOf": [{"$ref": "#/$defs/OpValue"}],
            },
        }
    if key in {"where", "predicates"} or (
        key == "filter" and kind in {"aggregate", "semi_additive"}
    ):
        # Inline conditions: the call's condition grammar (and a predicate object for
        # predicates); the parser checks each one at its path.
        condition_list = {"type": "array", "items": {"type": "object"}}
        return (
            {"type": "object", "properties": {"all": condition_list}, "additionalProperties": False}
            if key == "filter"
            else condition_list
        )
    if key in EXPRESSION_LISTS:
        return {"type": "array", "items": expression}
    if key == "whens":
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"when": expression, "then": expression},
                "required": ["when", "then"],
                "additionalProperties": False,
            },
        }
    if key == "over":
        return {"enum": list(OVER)}
    if key == "kind":
        return {"const": kind}
    return {}


def _spelling_pattern(names: Iterable[str]) -> str:
    """A pattern matching exactly the spellings ``operators.operator_word`` reads as ``names``:
    any letter case, surrounding whitespace, and any run of whitespace between words."""

    def one(name: str) -> str:
        return "".join(
            r"\s+" if char == " " else f"[{char.lower()}{char.upper()}]" if char.isalpha() else char
            for char in name
        )

    return r"^\s*(?:" + "|".join(one(name) for name in names) + r")\s*$"


def query_call_schema_document() -> dict[str, Any]:
    """The query call's JSON Schema, generated from the parser's own tables."""

    from semantic_rails import operators
    from semantic_rails.query_call import (
        CONDITION_KEYS,
        DIRECTIONS,
        FIELDS,
        GRAINS,
        KIND_REQUIRED,
        OVER,
        SCHEMA_ID,
        call_kinds,
    )
    from semantic_rails.schema import OBSERVATION_SCOPES

    scalar = {"type": ["string", "number", "boolean"]}
    shapes = {
        "scalar_or_list": {"anyOf": [scalar, {"type": "array", "items": scalar, "minItems": 1}]},
        "scalar": scalar,
        "text": {"type": "string"},
        "boolean": {"type": "boolean"},
    }
    spellings: dict[str, list[str]] = {}
    for word, operator in operators.OPERATORS.items():
        aliases = [alias for alias, target in operators.ALIASES.items() if target == word]
        spellings.setdefault(operator.value_shape, []).extend([word, *aliases])
    # The op and value rules every condition follows, a per-entity value filter's too: one
    # branch per value shape, with the spellings the parser reads for it (any case, any spacing).
    op_value = {
        "description": (
            f"op is one of {', '.join(operators.WORDS)} or an alias "
            f"({', '.join(operators.ALIASES)}), in any case. is and not take one value or a "
            "list; the ranges take one value; contains, starts_with, ends_with and their not_ "
            "forms take text; missing takes true or false."
        ),
        "properties": {"op": {"type": "string"}},
        "anyOf": [
            {"properties": {"op": {"pattern": _spelling_pattern(names)}, "value": shapes[shape]}}
            for shape, names in spellings.items()
        ],
    }
    condition = {
        "type": "object",
        "properties": {"field": {"type": "string", "minLength": 1}, "op": {}, "value": {}},
        "required": list(CONDITION_KEYS),
        "additionalProperties": False,
        "allOf": [{"$ref": "#/$defs/OpValue"}],
    }
    window = {
        "type": "object",
        "properties": {
            "unit": {"enum": list(GRAINS)},
            "value": {"type": "integer", "minimum": 1},
        },
        "required": ["unit", "value"],
        "additionalProperties": False,
    }
    exclusive = [
        ["last", "this"],
        ["last", "start"],
        ["last", "end"],
        ["this", "start"],
        ["this", "end"],
    ]
    kinds = call_kinds()
    kind_defs = {
        f"Kind_{kind}": {
            "type": "object",
            "properties": {key: _kind_property(kind, key) for key in ("kind", *keys)},
            "required": ["kind", *KIND_REQUIRED.get(kind, ())],
            "additionalProperties": False,
        }
        for kind, keys in kinds.items()
    }
    ref = {"type": "string", "pattern": "^(measure|metric)\\.[^\\s]+$"}
    defs: dict[str, Any] = {
        "Ref": ref,
        "Expression": {
            "anyOf": [
                {"$ref": "#/$defs/Ref"},
                {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {"kind": {"enum": sorted(kinds)}},
                    "allOf": [
                        {
                            "if": {"properties": {"kind": {"const": kind}}},
                            "then": {"$ref": f"#/$defs/Kind_{kind}"},
                        }
                        for kind in sorted(kinds)
                    ],
                },
            ]
        },
        "OpValue": op_value,
        "Condition": condition,
        "Conditions": {"type": "array", "items": {"$ref": "#/$defs/Condition"}, "minItems": 1},
        # Exactly one of any, none or having; over goes with having, and only with it.
        "EntityFilter": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "minLength": 1},
                "any": {"$ref": "#/$defs/Conditions"},
                "none": {"$ref": "#/$defs/Conditions"},
                "having": {"$ref": "#/$defs/Conditions"},
                "over": {"enum": list(OVER)},
            },
            "required": ["entity"],
            "additionalProperties": False,
            "oneOf": [
                {"required": ["any"]},
                {"required": ["none"]},
                {"required": ["having", "over"]},
            ],
            "dependentRequired": {"over": ["having"]},
        },
        **kind_defs,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Semantic Rails query call",
        "description": (
            "A question for Semantic Rails. Identity comes from credentials and now is an "
            "argument beside the call; neither travels in it."
        ),
        "type": "object",
        "properties": {
            "select": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"$ref": "#/$defs/Ref"},
                        {"type": "string", "pattern": "^dimension\\.[^\\s]+$"},
                        {
                            "type": "object",
                            "properties": {
                                "expression": {"$ref": "#/$defs/Expression"},
                                "as": {"type": "string", "minLength": 1},
                            },
                            "required": ["expression"],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "group_by": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "where": {
                "type": "array",
                "items": {
                    "anyOf": [{"$ref": "#/$defs/Condition"}, {"$ref": "#/$defs/EntityFilter"}]
                },
            },
            "having": {"type": "array", "items": {"$ref": "#/$defs/Condition"}},
            "time": {
                "type": "object",
                "properties": {
                    "temporal_role": {"type": "string", "minLength": 1},
                    "grain": {"enum": list(GRAINS)},
                    "start": {"type": "string", "minLength": 1},
                    "end": {
                        "type": "string",
                        "minLength": 1,
                        "description": "ISO date or date-time, exclusive: [start, end).",
                    },
                    "last": window,
                    "this": {"enum": list(GRAINS)},
                    "fill": {"type": "boolean"},
                },
                "additionalProperties": False,
                "anyOf": [
                    {"required": ["grain"]},
                    {"required": ["last"]},
                    {"required": ["this"]},
                    {"required": ["start"]},
                    {"required": ["end"]},
                ],
                "not": {"anyOf": [{"required": pair} for pair in exclusive]},
            },
            "order_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "minLength": 1},
                        "direction": {"enum": list(DIRECTIONS)},
                    },
                    "required": ["field"],
                    "additionalProperties": False,
                },
            },
            "limit": {"type": "integer", "minimum": 1},
            "routes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string", "minLength": 1},
                        "to": {"type": "string", "minLength": 1},
                        "via": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                        },
                    },
                    "required": ["from", "to", "via"],
                    "additionalProperties": False,
                },
            },
            "observation_scope": {"enum": list(OBSERVATION_SCOPES)},
            "version": {"description": "Ignored."},
        },
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
        "x-fields": list(FIELDS),
        "$defs": defs,
    }


def _shape(schema: dict[str, Any]) -> str:
    """A key's schema in a few characters, for the tool's kind table: ``E`` an expression,
    ``id`` a measure or metric id, ``a|b`` one of the words, ``[x]`` a list, ``{k:x}`` an
    object, ``&X`` also follows the definition ``X``; "" when the parser checks the value."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return {"Expression": "E", "Ref": "id"}.get(name, name)
    if "enum" in schema:
        return "|".join(map(str, schema["enum"]))
    if "const" in schema:
        return str(schema["const"])
    if schema.get("type") == "array":
        return f"[{_shape(schema['items'])}]"
    if "properties" in schema:
        keys = ",".join(_typed(key, value) for key, value in schema["properties"].items())
        rules = "".join(f"&{_shape(rule)}" for rule in schema.get("allOf", ()))
        return f"{{{keys}}}{rules}"
    return str(schema.get("type", ""))


def _typed(key: str, schema: dict[str, Any]) -> str:
    """``key:shape``, or ``key`` when the parser checks the value."""
    shape = _shape(schema)
    return f"{key}:{shape}" if shape else key


def _kind_table(kinds: dict[str, dict[str, Any]]) -> str:
    """Each kind's keys, required ones first and starred, kinds with the same keys together."""
    rows: dict[str, list[str]] = {}
    for kind, schema in kinds.items():
        required = [key for key in schema["required"] if key != "kind"]
        optional = sorted(key for key in schema["properties"] if key not in {"kind", *required})
        keys = ",".join(
            _typed(key + ("*" if key in required else ""), schema["properties"][key])
            for key in (*required, *optional)
        )
        rows.setdefault(keys, []).append(kind)
    return ";".join(f"{'/'.join(names)}:{keys}" for keys, names in rows.items())


# Each value shape of the operator registry, in words.
_VALUE_SHAPES = {
    "scalar_or_list": "a value or a list",
    "scalar": "one value",
    "text": "text",
    "boolean": "true or false",
}


def _compact(node: Any, shared: dict[str, Any]) -> Any:
    """``node`` without prose, each ``shared`` schema as its reference, and an op and value that
    follow ``OpValue`` as an ``op`` that is one of the ``Op`` words."""
    if isinstance(node, list):
        return [_compact(item, shared) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _compact(value, shared) for key, value in node.items() if key != "description"}
    name = next((name for name, schema in shared.items() if out == schema), None)
    if name:
        return {"$ref": f"#/$defs/{name}"}
    if out.get("allOf") == [{"$ref": "#/$defs/OpValue"}]:
        del out["allOf"]
        out["properties"] = {**out["properties"], "op": {"$ref": "#/$defs/Op"}}
    return out


def embedded_query_call_schema() -> tuple[dict[str, Any], dict[str, Any]]:
    """The call schema as a tool embeds it, and the ``$defs`` its references resolve against.

    The published document's fields, words and rules in under half its size, for every
    ``tools/list``: no prose (the tool's description has it), each kind's keys as one line of a
    table instead of a schema per kind, and the filter words and their SQL aliases (lower case
    and capitals) as one enum, with each word's value shape in one sentence. The parser refuses
    what the table and the sentence rule out at its path, with a hint; a test holds them to the
    published document.
    """
    from semantic_rails import operators
    from semantic_rails.query_call import GRAINS

    # Written once: a non-empty string (every id, name and date of a call) and a time unit.
    shared = {"Text": {"type": "string", "minLength": 1}, "Unit": {"enum": list(GRAINS)}}
    document = _compact(query_call_schema_document(), shared)
    definitions: dict[str, Any] = document.pop("$defs")
    for key in ("$schema", "$id", "title", "x-fields"):
        document.pop(key, None)
    kinds = {
        name.removeprefix("Kind_"): definitions.pop(name)
        for name in sorted(definitions)
        if name.startswith("Kind_")
    }
    words: dict[str, list[str]] = {}
    for word, operator in operators.OPERATORS.items():
        words.setdefault(operator.value_shape, []).append(word)
    spellings = [*operators.WORDS, *operators.ALIASES]
    aliases_of: dict[str, list[str]] = {}
    for alias, word in operators.ALIASES.items():
        aliases_of.setdefault(word, []).append(alias)
    del definitions["OpValue"]
    definitions.update(
        {
            **shared,
            "Op": {
                # The words, then the SQL aliases the parser reads as them, each also in capitals.
                "enum": [*spellings, *(name.upper() for name in spellings if name.upper() != name)],
                "description": "Aliases read as their word ("
                + "; ".join(f"{', '.join(aliases)}: {word}" for word, aliases in aliases_of.items())
                + "). Values: "
                + "; ".join(
                    f"{'/'.join(names)}: {_VALUE_SHAPES[shape]}" for shape, names in words.items()
                )
                + ".",
            },
            "Expression": {
                "description": (
                    "An id, or an object whose kind names its keys (*: required; E: "
                    f"expression; id: measure or metric id): {_kind_table(kinds)}"
                ),
                "anyOf": [{"$ref": "#/$defs/Ref"}, {"type": "object", "required": ["kind"]}],
            },
        }
    )
    return document, definitions
