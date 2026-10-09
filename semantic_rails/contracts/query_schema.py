"""The query call's JSON Schema, generated from the front door's own tables.

``query.v2.json`` is this document; the ``execute`` MCP tool embeds a compact form of it.
"""

from __future__ import annotations

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

    ops = sorted(
        {*operators.WORDS, *operators.ALIASES, *(alias.upper() for alias in operators.ALIASES)}
    )
    scalar = {"type": ["string", "number", "boolean"]}
    values = {"anyOf": [scalar, {"type": "array", "items": scalar, "minItems": 1}]}
    # The op and value rules every condition follows, a per-entity value filter's too.
    op_value = {
        "properties": {"op": {"enum": ops}, "value": values},
        "allOf": [
            {
                "if": {"properties": {"op": {"const": "missing"}}},
                "then": {"properties": {"value": {"type": "boolean"}}},
            },
            {
                "if": {"properties": {"op": {"const": "contains"}}},
                "then": {"properties": {"value": {"type": "string"}}},
            },
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
        "EntityFilter": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "minLength": 1},
                "any": {"type": "array", "items": {"$ref": "#/$defs/Condition"}, "minItems": 1},
                "none": {"type": "array", "items": {"$ref": "#/$defs/Condition"}, "minItems": 1},
                "having": {"type": "array", "items": {"$ref": "#/$defs/Condition"}, "minItems": 1},
                "over": {"enum": list(OVER)},
            },
            "required": ["entity"],
            "additionalProperties": False,
            "oneOf": [
                {"required": ["any"], "not": {"required": ["over"]}},
                {"required": ["none"], "not": {"required": ["over"]}},
                {"required": ["having", "over"]},
            ],
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
                "allOf": [{"not": {"required": pair}} for pair in exclusive],
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


def embedded_query_call_schema() -> tuple[dict[str, Any], dict[str, Any]]:
    """The call schema as a tool embeds it, and the ``$defs`` its references resolve against.

    The published document's rules, with each kind's keys written out in one ``anyOf``
    instead of an ``if``/``then`` chain over ``Kind_*`` definitions: about 4 KB less in every
    ``tools/list``. A test holds both forms to the same verdicts.
    """

    document = query_call_schema_document()
    definitions: dict[str, Any] = document.pop("$defs")
    for key in ("$schema", "$id", "title", "x-fields"):
        document.pop(key, None)
    kinds = []
    for name in sorted(name for name in definitions if name.startswith("Kind_")):
        kind = definitions.pop(name)
        kind.pop("type")
        kinds.append(kind)
    definitions["Expression"] = {
        "anyOf": [{"$ref": "#/$defs/Ref"}, {"type": "object", "anyOf": kinds}]
    }
    return document, definitions
