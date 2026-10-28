"""MCP adapter and tool definitions.

Exposes :class:`SemanticLayerMCPAdapter`, which presents six governed
tools backed by a single ``Runtime`` (interface v2): ``discover``,
``inspect``, ``valid-values``, ``plan``, ``execute(mode)`` and
``segment(action)``. The transport — stdio vs HTTP — lives in
:mod:`semantic_rails.mcp_server`; this module is the protocol-agnostic
adapter.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

from . import operations
from .audit import emit_audit_event
from .contracts.query_schema import embedded_query_call_schema
from .diagnostics import (
    enrich_object_not_found,
    exception_issue,
)
from .errors import UNEXPECTED_ERROR_MESSAGE, SemanticLayerError
from .mcp_query import coerce_arguments
from .mcp_session import MCPQuerySession
from .metadata import _slim_discover_minimal
from .request_context import (
    RequestContext,
    context_from_policy_context,
    request_context_payload,
)
from .request_payload import (
    DISCOVER_KIND_NAMES,
    DISCOVER_KINDS,
    checked_discover_kinds,
    parse_string_list,
    singular_kinds,
)
from .request_payload import (
    clean_request_id as _clean_request_id,
)
from .request_payload import (
    coerce_bool as _coerce_bool,
)
from .resource_access import GRANT_DISCOVER_KINDS
from .runtime import Runtime
from .runtime_parts.limits import max_valid_values_limit, max_valid_values_offset
from .runtime_parts.responses import resolve_verbosity
from .schema import PackageConfig

__all__ = [
    "JSON_OBJECT_SCHEMA",
    "MCP_PROMPT_DEFINITIONS",
    "MCP_RESOURCE_DEFINITIONS",
    "MCP_SERVER_INSTRUCTIONS",
    "MCP_TOOL_DEFINITIONS",
    "PromptDefinition",
    "ResourceDefinition",
    "Runtime",
    "SemanticLayerError",
    "SemanticLayerMCPAdapter",
    "ToolDefinition",
    "context_from_policy_context",
    "emit_audit_event",
    "enrich_object_not_found",
    "exception_issue",
    "json_text",
    "list_prompt_definitions",
    "list_resource_definitions",
    "list_tool_definitions",
    "request_context_payload",
]


# The query-MCP contract version, frozen in query_mcp.v2.json.
_INTERFACE = "v2"

# Default response verbosity for MCP execute, in every mode.
# Context-constrained agents drown in the ~90-100KB envelopes the runtime
# emits at its own 'compact' default (the HTTP v1 surface keeps that
# default — see runtime_parts.responses.resolve_verbosity). The MCP
# adapter defaults to 'minimal' instead: mode validate={ok,errors,warnings},
# mode sql adds rendered_sql, mode run adds rows+row_count. An explicit
# 'verbosity' argument (outer envelope or inside `query`) always wins.
MCP_DEFAULT_QUERY_VERBOSITY = "minimal"

# Default row cap for MCP execute. Hosts warn about tool results over 10K tokens; 200 rows keeps a typical
# answer well under that. A larger result comes back with a truncation hint.
MCP_DEFAULT_MAX_ROWS = 200
# The largest max_rows an MCP caller may request.
MCP_MAX_ROWS_LIMIT = 100_000
# Every final tool payload, serialized identically for both MCP content channels,
# fits this character budget. Optional plans are trimmed before refusing an answer.
MCP_DEFAULT_MAX_RESULT_CHARS = 32_000
# An error envelope must itself fit, even with an unusably small operator setting.
MCP_MIN_RESULT_CHARS = 512
_MAX_RESULT_CHARS_ENV = "SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS"

_TOOL_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "semantic_rails_mcp_tool_request_context", default=None
)
_BUILTIN_TOOL_DISPATCH: ContextVar[bool] = ContextVar(
    "semantic_rails_mcp_builtin_tool_dispatch", default=False
)


JSON_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}

# Server-level guidance, sent once in ``initialize``: the workflow and the
# conventions every tool shares. Tool descriptions say what each tool does,
# when to use it and its one gotcha.
MCP_SERVER_INSTRUCTIONS = (
    "Semantic Rails answers analytics questions from a governed semantic layer. Refer to "
    "objects by full id (measure.sales.revenue, dimension.store_name), never "
    "by label.\n"
    "\n"
    "To answer a question:\n"
    "1. discover(terms) ranks measures, metrics and dimensions for it; empty terms list "
    "every id. inspect(object_id) shows one object's card when you need its aggregations, "
    "values or time roles. valid-values(dimension_id) lists a dimension's values.\n"
    "2. plan(intent) drafts the query call; draft with plan rather than writing one from "
    'scratch. Run best.query only when status is "ok" and there are no warnings; '
    "otherwise why and warnings name what the draft misses, so fix the call or ask the "
    "user. out_of_scope or unrealizable means the package can't answer. When status is ok, a "
    "question asking several things returns parts; execute each part's best.query.\n"
    "3. execute(query) validates, compiles and runs the call and returns at most "
    f"max_rows rows (default {MCP_DEFAULT_MAX_ROWS}); a capped result reports truncated. "
    'mode "validate" only checks the call; mode "sql" also returns its SQL.\n'
    "\n"
    "The call: select measure or metric ids, group_by dimension ids, where "
    "[{field, op, value}] with word ops (is, not, gt, gte, lt, lte, contains, starts_with, "
    "ends_with, missing), and "
    "time {grain, last, this, start, end}; end is exclusive. A window without a grain "
    "returns one total. Relative windows resolve against the now argument.\n"
    "No time/where: the query adds no time window or filter; "
    "metric definitions and package policies still apply.\n"
    "\n"
    "segment(segment_id, action) validates, explains or previews a package-authored "
    "segment.\n"
    "\n"
    'Every tool returns its smallest response by default (verbosity "minimal", plan detail '
    '"query"). Responses share one character budget; omitted_fields names trimmed detail. Errors carry '
    "recovery_hints and closest_matches; follow them before retrying."
)


# The query call schema as execute's ``query`` property, and its ``$defs`` (moved to the tool
# schema's root so its ``#/$defs/...`` references resolve).
QUERY_SCHEMA, QUERY_SCHEMA_DEFS = embedded_query_call_schema()
# A metadata tool or plan takes a partial call to start from.
QUERY_SEED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A partial query call (execute's query schema) to start from.",
}
NOW_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "ISO date or date-time for time.last and time.this.",
}

# execute's response options; each description says only what its enum does not.
VERBOSITY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["minimal", "compact", "full"],
    "default": "minimal",
    "description": "'compact' adds metadata; 'full' adds plans too.",
}

SQL_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["audit", "compact", "debug", "off"],
    "default": "audit",
    "description": "'off' omits rendered_sql and sql_plan.",
}

ROW_FORMAT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["records", "columns"],
    "default": "records",
    "description": "'columns': column names once, rows as arrays.",
}

MCP_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "description": (
        "Semantic Rails MCP result envelope. Tool-specific fields are additive; "
        "the status, issue, request, and package fields are stable v2."
    ),
    "required": [
        "ok",
        "status",
        "api_version",
        "request_id",
        "package_id",
        "warnings",
        "errors",
    ],
    "properties": {
        "ok": {"type": "boolean"},
        "status": {
            "type": "string",
            "enum": ["ok", "warning", "error", "low_confidence", "unrealizable", "out_of_scope"],
        },
        "api_version": {"type": "string", "const": _INTERFACE},
        "request_id": {"type": "string"},
        "package_id": {"type": "string"},
        "warnings": {"type": "array", "items": {"$ref": "#/$defs/issue"}},
        "errors": {"type": "array", "items": {"$ref": "#/$defs/issue"}},
        "error": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "request_context": {"type": "object"},
        "timing_ms": {"type": "number", "minimum": 0},
        "next": {"type": ["object", "string"]},
    },
    "additionalProperties": True,
    "$defs": {
        "issue": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "severity": {"type": "string"},
                "stage": {"type": "string"},
                "details": {"type": "object"},
                "recovery_hints": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        }
    },
}

MCP_RESULT_SCHEMA_SLIM: dict[str, Any] = {
    "type": "object",
    "required": ["ok", "status"],
    "properties": {
        "ok": {"type": "boolean"},
        "status": {"type": "string"},
        "api_version": {"type": "string", "const": _INTERFACE},
        "request_id": {"type": "string"},
        "package_id": {"type": "string"},
        "warnings": {"type": "array"},
        "errors": {"type": "array"},
    },
    "additionalProperties": True,
}

# plan's result: a question asking several things comes back as parts, each its own plan.
MCP_PLAN_RESULT_SCHEMA: dict[str, Any] = {
    **MCP_RESULT_SCHEMA_SLIM,
    "properties": {
        **MCP_RESULT_SCHEMA_SLIM["properties"],
        "parts": {
            "type": "array",
            "description": "One plan per part of a question asking several things.",
            "items": {
                "type": "object",
                "required": ["text", "spans"],
                "properties": {
                    "text": {"type": "string"},
                    "spans": {"type": "array", "items": {"type": "array"}},
                    "status": {"type": "string"},
                    "best": {"type": ["object", "null"]},
                    "why": {"type": "object"},
                },
                "additionalProperties": True,
            },
        },
    },
}


def _tool_annotations(name: str) -> dict[str, Any]:
    """Return MCP-standard behavioral hints for a query tool.

    Query execution and live value/segment previews can contact an external
    warehouse, hence ``openWorldHint``. They remain read-only and
    non-destructive: the engine only compiles and executes SELECT-shaped SQL.
    """

    open_world = name in {"execute", "valid-values", "segment"}
    return {
        "title": name.replace("-", " ").title(),
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": open_world,
    }


def _schema(
    properties: Mapping[str, Any],
    *,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": copy.deepcopy(dict(properties)),
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None = None
    annotations: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": copy.deepcopy(dict(self.input_schema)),
            "outputSchema": copy.deepcopy(dict(self.output_schema or MCP_RESULT_SCHEMA_SLIM)),
            "annotations": copy.deepcopy(dict(self.annotations or _tool_annotations(self.name))),
        }


@dataclass(frozen=True)
class ResourceDefinition:
    uri: str
    name: str
    description: str
    mime_type: str = "application/json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True)
class PromptDefinition:
    name: str
    description: str
    arguments: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": copy.deepcopy(list(self.arguments)),
        }


# The adapter also accepts "compact" as an alias for "full".
SEGMENT_VERBOSITY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["minimal", "full"],
    "default": "minimal",
}

_EXECUTE_MODES = ("run", "validate", "sql")
_SEGMENT_ACTIONS = ("validate", "explain", "preview")
# Empty-terms discover lists this many ids per kind at a time: under 10K tokens for all kinds.
_DISCOVER_ID_PAGE = 100
_VERBOSITY_MINIMAL: dict[str, Any] = {
    "type": "string",
    "enum": ["minimal", "compact", "full"],
    "default": "minimal",
}

# Every tool defaults to its smallest response.
TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="discover",
        description=(
            f"Rank objects by business terms; empty terms list ids ({_DISCOVER_ID_PAGE} per kind; limit, offset). "
            "Default: id, label, description; compact omits blocked cards. "
            "Gotcha: 'out_of_scope' or 'low_relevance' with empty buckets; "
            "branch before using a candidate."
        ),
        input_schema=_schema(
            {
                "terms": {"type": "string"},
                "kinds": {
                    "type": ["string", "array"],
                    "items": {"type": "string", "enum": sorted(DISCOVER_KIND_NAMES)},
                    "description": "Unset: all but relationship, temporal_role, value_domain.",
                },
                "query": QUERY_SEED_SCHEMA,
                "stage": {
                    "type": "string",
                    "description": (
                        "Ranking stage: initial, post_measure, post_dimension or "
                        "comparison; inferred if omitted."
                    ),
                },
                "verbosity": _VERBOSITY_MINIMAL,
                # No schema default: clients that fill defaults in would cap
                # an empty-terms page at 10 ids.
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"Per kind: default 10, or {_DISCOVER_ID_PAGE} ids with empty terms."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "With empty terms, ids to skip per kind.",
                },
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="inspect",
        description=(
            "Return one object's card: label, description, aggregations or values, temporal "
            "roles, related objects, policy. Default: the card without duplicate fields; "
            "verbosity='compact' returns the full card. Gotcha: 'object_id' must be a full "
            "id like 'measure.sales.revenue', not a label — use 'discover' first if you "
            "only have a phrase."
        ),
        input_schema=_schema(
            {
                "object_id": {"type": "string"},
                "query": QUERY_SEED_SCHEMA,
                "verbosity": _VERBOSITY_MINIMAL,
            },
            required=["object_id"],
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="valid-values",
        description=(
            "Return a dimension's governed values: from its declared value "
            "domain, or from a constrained warehouse probe with allow_live_query:"
            " true. Use it before writing a 'where' filter on a categorical "
            "dimension. Gotcha: 'dimension_id' must be a full id like "
            "'dimension.store_name'; allow_live_query costs a warehouse "
            "round-trip, so use it only when no domain is declared."
        ),
        input_schema=_schema(
            {
                "dimension_id": {"type": "string"},
                "query": QUERY_SEED_SCHEMA,
                "search": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "include_counts": {
                    "type": "boolean",
                    "default": False,
                    "description": "With allow_live_query, add each value's row count.",
                },
                "allow_live_query": {"type": "boolean", "default": False},
            },
            required=["dimension_id"],
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="plan",
        description=(
            "Draft one best query call from a natural-language intent; call it before "
            "'execute' instead of writing a call from scratch. Returns 'status' ('ok' | "
            "'low_confidence' | 'unrealizable' | 'out_of_scope'), 'best.query', and 'why' "
            "or 'warnings' naming any part of the question the draft doesn't honor. "
            "detail='query' (default) is compact; 'best' adds intent_ir, trace and next "
            "steps; 'full' adds alternatives and blocked drafts; 'debug' adds compose_hints. "
            "Gotcha: pass 'best.query' to 'execute' only when status is 'ok' and there are "
            "no warnings."
        ),
        input_schema=_schema(
            {
                "intent": {"type": "string"},
                "query": QUERY_SEED_SCHEMA,
                "now": NOW_SCHEMA,
                "detail": {
                    "type": "string",
                    "enum": ["query", "best", "full", "debug"],
                    "default": "query",
                },
                "limit": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "description": "Drafts to consider; detail='full' returns the runners-up.",
                },
            },
            required=["intent"],
            additional_properties=True,
        ),
        output_schema=MCP_PLAN_RESULT_SCHEMA,
    ),
    ToolDefinition(
        name="execute",
        description=(
            "Answer a call; call plan first to draft it. mode 'run' returns rows, 'validate' "
            "only checks (skip it: run a call directly), 'sql' adds rendered_sql; only 'run' "
            "reads the warehouse. Call: select ids or {expression, as}, group_by dimension ids "
            "(alone, it lists rows), where/having [{field, op, value}] and entity filters, time "
            "(time.end is exclusive), order_by (period: the time column), limit inside it; "
            "max_rows and row_format go beside query."
        ),
        input_schema={
            **_schema(
                {
                    "query": QUERY_SCHEMA,
                    "now": NOW_SCHEMA,
                    "mode": {
                        "type": "string",
                        "enum": list(_EXECUTE_MODES),
                        "default": "run",
                    },
                    "verbosity": VERBOSITY_SCHEMA,
                    "sql_profile": SQL_PROFILE_SCHEMA,
                    "row_format": ROW_FORMAT_SCHEMA,
                    "max_rows": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MCP_MAX_ROWS_LIMIT,
                        "default": MCP_DEFAULT_MAX_ROWS,
                    },
                },
                required=["query"],
                additional_properties=True,
            ),
            "$defs": QUERY_SCHEMA_DEFS,
        },
    ),
    ToolDefinition(
        name="segment",
        description=(
            "Work with a package-authored segment, after discover finds its id: "
            "action='validate' checks it and the query"
            " derived from it, 'explain' adds the SQL, and 'preview' returns sample member rows"
            " and the total member count. Default verbosity='minimal' leaves out compiler "
            "plans; 'full' returns them. Gotcha: 'segment_id' must be a full id like "
            "'segment.high_value_customers' (discover with empty terms lists them); "
            "'preview' queries the warehouse."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "action": {"type": "string", "enum": list(_SEGMENT_ACTIONS)},
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "description": "Sample rows for action 'preview'.",
                },
                "verbosity": SEGMENT_VERBOSITY_SCHEMA,
            },
            required=["segment_id", "action"],
        ),
    ),
)

RESOURCE_DEFINITIONS: tuple[ResourceDefinition, ...] = (
    ResourceDefinition(
        uri="semantic-rails://capabilities",
        name="capabilities",
        description=(
            "Interface v2 tool definitions, resources and prompts. Large; use "
            "capabilities/summary for names and titles."
        ),
    ),
    ResourceDefinition(
        uri="semantic-rails://capabilities/summary",
        name="capabilities-summary",
        description=(
            "Small capabilities index: tool names and titles, plus resources and prompts."
        ),
    ),
    ResourceDefinition(
        uri="semantic-rails://catalog/summary",
        name="catalog-summary",
        description=(
            "Catalog summary with descriptive rows and counts. Large; use catalog/index "
            "for counts and ids."
        ),
    ),
    ResourceDefinition(
        uri="semantic-rails://catalog/index",
        name="catalog-index",
        description=("Small catalog index: counts and ids per object kind for the active package."),
    ),
    ResourceDefinition(
        uri="semantic-rails://catalog/full",
        name="catalog-full",
        description=(
            "Every object's full card and the alias index for the active package. Large: "
            "start with catalog-index."
        ),
    ),
)

PROMPT_DEFINITIONS: tuple[PromptDefinition, ...] = (
    PromptDefinition(
        name="semantic-rails-query-builder",
        description=(
            "Guide an agent from a question to rows: discover, inspect, plan (and valid-values), "
            "then execute."
        ),
        arguments=(
            {
                "name": "intent",
                "description": "Business question or analysis goal.",
                "required": True,
            },
            {"name": "package_id", "description": "Semantic layer package id.", "required": False},
        ),
    ),
    PromptDefinition(
        name="semantic-rails-query-review",
        description=(
            "Review an existing query call with execute mode 'validate', then mode 'sql', "
            "before running it."
        ),
        arguments=(
            {"name": "query_json", "description": "Query call JSON to review.", "required": True},
        ),
    ),
    PromptDefinition(
        name="semantic-rails-segment-workflow",
        description="Validate, explain and preview a package-authored segment with the segment tool.",
        arguments=(
            {
                "name": "segment_id",
                "description": "Segment id such as segment.high_value_customers.",
                "required": True,
            },
        ),
    ),
)

_PROMPT_TEXT = {
    "semantic-rails-query-builder": (
        "Use the Semantic Layer MCP tools against package '{package_id}' to answer: {intent}\n"
        "Start with discover and inspect the best governed objects. Draft the call with plan, "
        "check filter values with valid-values, then run it with execute, which validates and "
        "compiles first. execute modes 'validate' and 'sql' are optional dry runs. "
        "The response includes diagnostics and any rendered SQL."
    ),
    "semantic-rails-query-review": (
        "Review this Semantic Layer query call with execute mode 'validate', then mode 'sql'. "
        "Only run it with mode 'run' after "
        "validation succeeds and the user needs result rows.\n\n{query_json}"
    ),
    "semantic-rails-segment-workflow": (
        "Use the segment tool with action 'validate', 'explain' and 'preview' for segment "
        "'{segment_id}'. Report validation errors first, then summarize the derived query and "
        "member preview."
    ),
}
MCP_TOOL_DEFINITIONS = tuple(definition.to_dict() for definition in TOOL_DEFINITIONS)
MCP_RESOURCE_DEFINITIONS = tuple(definition.to_dict() for definition in RESOURCE_DEFINITIONS)
MCP_PROMPT_DEFINITIONS = tuple(definition.to_dict() for definition in PROMPT_DEFINITIONS)


def _offered_tool_names(has_segments: bool) -> set[str]:
    """The tools a package offers: no segment tool when it defines no segment."""
    return {
        definition.name
        for definition in TOOL_DEFINITIONS
        if definition.name != "segment" or has_segments
    }


def list_tool_definitions(*, config: PackageConfig | None = None) -> list[dict[str, Any]]:
    offered = _offered_tool_names(config is None or bool(config.segments))
    return [definition.to_dict() for definition in TOOL_DEFINITIONS if definition.name in offered]


def list_resource_definitions() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in RESOURCE_DEFINITIONS]


def list_prompt_definitions() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in PROMPT_DEFINITIONS]


def _argument_error(message: str, *, field: str, value: Any | None = None) -> SemanticLayerError:
    details: dict[str, Any] = {"field": field}
    if value is not None:
        details["argument_type"] = type(value).__name__
    return SemanticLayerError("INVALID_MCP_ARGUMENTS", message, details=details)


# Argument tables, derived from the tool schemas. A closed schema rejects
# unknown keys, an open one warns and ignores them.
_TOOL_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    definition.name: dict(definition.input_schema or {}) for definition in TOOL_DEFINITIONS
}
_TRANSPORT_ARGS = ("request_id",)
_KNOWN_ARGS: Mapping[str, frozenset[str]] = {
    name: frozenset(schema.get("properties") or {}) | frozenset(_TRANSPORT_ARGS)
    for name, schema in _TOOL_SCHEMAS.items()
}
_STRICT_REJECT_TOOLS: frozenset[str] = frozenset(
    name for name, schema in _TOOL_SCHEMAS.items() if not schema.get("additionalProperties", True)
)
_UNKNOWN_ARG_WARNING_CODE: Mapping[str, str] = {
    name: f"{name.replace('-', '_').upper()}_UNKNOWN_ARG"
    for name in _TOOL_SCHEMAS
    if name not in _STRICT_REJECT_TOOLS
}
_WARN_AND_IGNORE_TOOLS: frozenset[str] = frozenset(_UNKNOWN_ARG_WARNING_CODE)


def _tool_required_properties(tool_name: str) -> tuple[list[str], list[str]]:
    """Return required schema properties and known runtime arguments."""
    schema = _TOOL_SCHEMAS.get(tool_name, {})
    return list(schema.get("required") or []), [
        *list(schema.get("properties") or {}),
        *_TRANSPORT_ARGS,
    ]


_ROW_FORMATS: frozenset[str] = frozenset({"records", "columns"})


def _unknown_arg_keys(*, tool_name: str, arguments: Mapping[str, Any]) -> list[str]:
    """Return the sorted list of unknown argument keys for ``tool_name``.

    Consults the interface's known arguments regardless of the schema's
    ``additionalProperties`` flag — the flag is a hint about reaction
    policy, not a gate on the check.
    """
    known = _KNOWN_ARGS.get(tool_name)
    if known is None:
        return []
    return sorted(key for key in (arguments or {}) if key not in known)


def _unknown_argument_error(
    *, tool_name: str, arguments: Mapping[str, Any]
) -> SemanticLayerError | None:
    """Reject unknown tool arguments on strict-reject tools.

    Strict tools (``segment``) raise
    ``INVALID_MCP_ARGUMENTS`` on any unknown key. Warn-and-ignore tools
    are handled separately via :func:`_unknown_argument_warnings` so
    ``policy_context`` and top-level IR passthrough still work.
    """
    if tool_name not in _STRICT_REJECT_TOOLS:
        return None
    unknown = _unknown_arg_keys(tool_name=tool_name, arguments=arguments)
    if not unknown:
        return None
    known = _KNOWN_ARGS.get(tool_name, frozenset())
    suggestions: list[str] = []
    for unknown_key in unknown:
        suggestions.extend(get_close_matches(unknown_key, sorted(known), n=2, cutoff=0.4))
    ranked = list(dict.fromkeys(suggestions))
    message = (
        f"MCP tool '{tool_name}' received unknown argument(s) {unknown}. "
        f"Known arguments: {sorted(known)}."
    )
    if ranked:
        message += f" Did you mean {ranked}?"
    return SemanticLayerError(
        "INVALID_MCP_ARGUMENTS",
        message,
        details={
            "tool": tool_name,
            "unknown_keys": unknown,
            "known_arguments": sorted(known),
            "closest_matches": ranked,
        },
    )


def _unknown_argument_warnings(
    *, tool_name: str, arguments: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Emit one warning per unknown argument on warn-and-ignore tools.

    Returns ``[]`` for strict-reject tools (handled by
    :func:`_unknown_argument_error`), unknown tool names, or when every
    received key is legitimate. The warning shape matches the existing
    ``DISCOVER_UNKNOWN_ARG`` pattern so
    downstream consumers can branch on ``code`` alone.
    """
    if tool_name not in _UNKNOWN_ARG_WARNING_CODE:
        return []
    unknown = _unknown_arg_keys(tool_name=tool_name, arguments=arguments)
    if not unknown:
        return []
    known = _KNOWN_ARGS.get(tool_name, frozenset())
    code = _UNKNOWN_ARG_WARNING_CODE[tool_name]
    warnings: list[dict[str, Any]] = []
    for key in unknown:
        closest = get_close_matches(key, sorted(known), n=2, cutoff=0.4)
        message = f"Received unknown argument '{key}'."
        if closest:
            message += f" Did you mean {closest}?"
        message += " The value was ignored."
        warnings.append(
            {
                "code": code,
                "severity": "warning",
                "message": message,
                "details": {
                    "received": key,
                    "closest_matches": closest,
                },
            }
        )
    return warnings


def _required_string_type_error(
    *, tool_name: str, arguments: Mapping[str, Any]
) -> SemanticLayerError | None:
    """Reject non-string values on string-typed required args (object_id,
    segment_id, dimension_id, intent). Returning a clean
    INVALID_MCP_ARGUMENTS here beats letting the handler silently coerce
    ``inspect({object_id: 12345})`` into the misleading
    ``OBJECT_NOT_FOUND: Unknown object '12345'``.
    """
    required, _known = _tool_required_properties(tool_name)
    properties = dict(_TOOL_SCHEMAS.get(tool_name, {}).get("properties") or {})
    payload = dict(arguments or {})
    for field_name in required:
        if field_name not in payload or properties[field_name].get("type") != "string":
            continue
        value = payload[field_name]
        if value is None:
            continue
        if not isinstance(value, str):
            return SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                (
                    f"MCP tool '{tool_name}' argument '{field_name}' must "
                    f"be a string; got {type(value).__name__}."
                ),
                details={
                    "tool": tool_name,
                    "field": field_name,
                    "argument_type": type(value).__name__,
                    "expected_type": "string",
                },
            )
    return None


def _missing_required_argument_error(
    *, tool_name: str, arguments: Mapping[str, Any]
) -> SemanticLayerError | None:
    """Return a structured INVALID_MCP_ARGUMENTS error when a required
    tool argument is missing or blank — preferred to letting the
    handler treat ``""`` as a real value and emit a misleading
    ``OBJECT_NOT_FOUND`` for the empty string.
    """
    required, known = _tool_required_properties(tool_name)
    if not required:
        return None
    payload = dict(arguments or {})
    payload.pop("request_id", None)
    received_keys = sorted(payload.keys())
    for field_name in required:
        present = field_name in payload
        value = payload.get(field_name)
        is_empty = value in (None, "")
        if not is_empty:
            continue
        unknown_received = [key for key in received_keys if key not in set(known)]
        closest: list[str] = []
        for unknown_key in unknown_received:
            closest.extend(get_close_matches(unknown_key, known, n=2, cutoff=0.4))
        # de-dupe while preserving order
        ranked_closest = list(dict.fromkeys(closest))
        if present:
            verb = "is empty"
            qualifier = "non-empty string"
        else:
            verb = "is missing"
            qualifier = "value"
        message = (
            f"MCP tool '{tool_name}' required argument '{field_name}' "
            f"{verb} (expected {qualifier})."
        )
        if unknown_received and ranked_closest:
            message += f" Received unknown keys {unknown_received}; did you mean {ranked_closest}?"
        elif unknown_received:
            message += f" Received unknown keys {unknown_received}."
        details: dict[str, Any] = {
            "tool": tool_name,
            "field": field_name,
            "required": required,
            "received_keys": received_keys,
            "unknown_keys": unknown_received,
            "closest_matches": ranked_closest,
            "argument_state": "empty" if present else "missing",
        }
        return SemanticLayerError("INVALID_MCP_ARGUMENTS", message, details=details)
    return None


def _object_argument(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _argument_error(
            f"MCP argument '{field}' must be a JSON object.", field=field, value=value
        )
    return dict(value)


def _caller() -> RequestContext:
    """Who is asking: the identity the transport resolved, or the local operator's (empty)."""
    return _TOOL_REQUEST_CONTEXT.get() or RequestContext()


def _policy_context_payload() -> dict[str, Any]:
    return _caller().to_policy_context()


def _verbosity(arguments: Mapping[str, Any]) -> str:
    """The response detail an execute-shaped call asked for (MCP's default is minimal)."""
    raw = str(arguments.get("verbosity") or "").strip() or MCP_DEFAULT_QUERY_VERBOSITY
    return resolve_verbosity({"verbosity": raw})


def _call_key(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """What an execute call asks, for the session's repeat hints: the call and its clock."""
    return {"query": arguments.get("query"), "now": arguments.get("now")}


# Explicit minimal verbosity returns the answer without compiler plans
# (logical, SQL, physical, performance) and their copies. An omitted level,
# "compact", and "full" return the whole response.
_SEGMENT_MINIMAL_KEYS: dict[str, frozenset[str]] = {
    "validate": frozenset({"segment", "normalized_segment", "derived_query"}),
    "explain": frozenset(
        {
            "segment",
            "normalized_segment",
            "derived_query",
            "rendered_sql",
        }
    ),
    "preview": frozenset(
        {
            "segment",
            "member_key_dimensions",
            "preview_dimensions",
            "rows",
            "column_types",
            "preview_row_count",
            "member_count",
            "derived_query",
        }
    ),
}
# The outcome, all policy effects on the segment and its derived query, and
# actionable recovery guidance stay on every response. These fields come from
# different Runtime paths (validate, compile, preview, and soft failure), so
# keep them together rather than relying on a tool-specific success allowlist.
_SEGMENT_OUTCOME_KEYS = frozenset(
    {
        "ok",
        "status",
        "errors",
        "warnings",
        "recovery_hints",
        "authoring_hints",
        "query_ir_hints",
        "assumptions",
        "methodology_hints",
        "disabled_options",
        "policy_effects",
        "segment_policy_effects",
    }
)


def _segment_response(action: str, payload: Mapping[str, Any], verbosity: Any) -> dict[str, Any]:
    out = dict(payload or {})
    if str(verbosity or "full").strip().lower() != "minimal":
        return out
    keep = _SEGMENT_MINIMAL_KEYS[action] | _SEGMENT_OUTCOME_KEYS
    return {
        key: value
        for key, value in out.items()
        if key in keep
        and (
            key in {"ok", "status", "errors", "warnings", "column_types"}
            or value not in ("", [], {})
        )
    }


def _row_format_arg(arguments: Mapping[str, Any]) -> str:
    raw_value = arguments.get("row_format", "records")
    row_format = str(raw_value or "records").strip().lower()
    if row_format not in _ROW_FORMATS:
        valid = ", ".join(sorted(_ROW_FORMATS))
        raise _argument_error(
            f"MCP argument 'row_format' for 'execute' must be one of {valid}.",
            field="row_format",
            value=raw_value,
        )
    return row_format


def _plan_detail(value: Any) -> str:
    """MCP plan's detail level; anything unknown gets the compact default, "query"."""

    detail = str(value or "").strip().lower()
    return detail if detail in {"query", "best", "full", "debug"} else "query"


def _choice_arg(value: Any, default: str, choices: Sequence[str], *, tool: str, field: str) -> str:
    choice = str(value or default).strip().lower()
    if choice not in choices:
        raise SemanticLayerError(
            "INVALID_MCP_ARGUMENTS",
            f"MCP argument '{field}' for '{tool}' must be one of {', '.join(choices)}.",
            details={
                "tool": tool,
                "field": field,
                "received": value,
                "valid_values": list(choices),
            },
        )
    return choice


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _max_rows_arg(value: Any) -> int | None:
    """The ``max_rows`` argument as a whole number, or None when absent."""

    if value is None or value == "":
        return None
    whole = isinstance(value, int) and not isinstance(value, bool)
    if isinstance(value, float) and value.is_integer():
        whole = True
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        whole = True
    if not whole or not 1 <= int(value) <= MCP_MAX_ROWS_LIMIT:
        raise _argument_error(
            f"MCP argument 'max_rows' for 'execute' must be a whole number from 1 to "
            f"{MCP_MAX_ROWS_LIMIT:,}.",
            field="max_rows",
            value=value,
        )
    return int(value)


def _narrowing_advice(query: Mapping[str, Any]) -> str:
    if isinstance(query.get("time"), Mapping) and query["time"].get("grain"):
        return "use a coarser time.grain, filter, or group by fewer dimensions"
    return "filter, or group by fewer dimensions"


def _truncate_rows(result: dict[str, Any], *, cap: int, query: Mapping[str, Any]) -> dict[str, Any]:
    """Say loudly that the answer has more than ``cap`` rows.

    Execute fetches ``cap`` rows (the warehouse adapter reads one more to tell), so the
    total is not counted: a truncated result carries ``truncated: true``,
    ``total_row_count: null`` and an ``EXECUTE_ROWS_TRUNCATED`` warning that says how to
    narrow the query.
    """

    rows = list(result.get("rows") or [])
    if len(rows) <= cap and not result.get("truncated"):
        return result
    kept = rows[:cap]
    advice = _narrowing_advice(query)
    warning = {
        "code": "EXECUTE_ROWS_TRUNCATED",
        "severity": "warning",
        "message": f"Returned the first {len(kept):,} rows; more exist. To narrow the result, "
        f"{advice}. Or raise max_rows.",
        "details": {"returned_rows": len(kept), "total_row_count": None, "max_rows": cap},
    }
    return {
        **result,
        "rows": kept,
        "row_count": len(kept),
        "truncated": True,
        "total_row_count": None,
        "warnings": [*list(result.get("warnings") or []), warning],
    }


def _max_result_chars() -> int:
    """One final-payload budget, with room for a bounded refusal envelope."""

    return max(
        MCP_MIN_RESULT_CHARS,
        _positive_int(os.environ.get(_MAX_RESULT_CHARS_ENV)) or MCP_DEFAULT_MAX_RESULT_CHARS,
    )


def _columnar_rows(result: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(result)
    records = list(out.get("rows") or [])
    columns: list[str] = []
    seen: set[str] = set()
    for row in records:
        if not isinstance(row, Mapping):
            out["row_format"] = "columns"
            return out
        for key in row:
            key_str = str(key)
            if key_str not in seen:
                seen.add(key_str)
                columns.append(key_str)
    if not columns:
        for column in list(out.get("output_columns") or []):
            if not isinstance(column, Mapping):
                continue
            field = str(column.get("field") or column.get("sql_alias") or "")
            if field and field not in seen:
                seen.add(field)
                columns.append(field)
    out["columns"] = columns
    out["rows"] = [[row.get(column) for column in columns] for row in records]
    out["row_format"] = "columns"
    return out


_PLAN_DETAIL_FIELDS = (
    "explain",
    "sql_plan",
    "physical_plan",
    "performance_plan",
    "fanout_strategy",
    "fanout_analysis",
    "semantic_dag",
)
_OPTIONAL_RESPONSE_FIELDS = (
    *_PLAN_DETAIL_FIELDS,
    "compile_stats",
    "semantic_summary",
    "trace",
    "intent_ir",
    "alternatives",
    "compose_hints",
    "output_columns",
    "warehouse_capabilities",
    "freshness_by_leaf",
    "provenance_summary",
    "query_state",
    "selection_context",
    "normalized_query",
    "query",
    "same_as",
    "request_context",
)


def _record_rows(result: Mapping[str, Any]) -> bool:
    rows = result.get("rows")
    return isinstance(rows, list) and any(isinstance(row, Mapping) for row in rows)


def _largest_field(result: Mapping[str, Any]) -> str:
    """The answer column holding the most characters across the rows, else the response's
    largest top-level field."""
    sizes: Counter[str] = Counter()
    columns = list(result.get("columns") or [])
    for row in result.get("rows") or []:
        cells = row.items() if isinstance(row, Mapping) else zip(columns, row, strict=False)
        for key, value in cells:
            sizes[str(key)] += len(json_text(value))
    if not sizes:
        sizes.update({str(key): len(json_text(value)) for key, value in result.items()})
    return sizes.most_common(1)[0][0] if sizes else ""


def _without_optional_fields(
    payload: Mapping[str, Any], *, compact: bool, sql_required: bool, limit: int
) -> dict[str, Any]:
    """``payload`` with optional fields omitted, in order, until it fits ``limit`` or none is
    left; ``omitted_fields`` names them."""
    result = dict(payload)
    omitted = list(result.get("omitted_fields") or [])

    def omit(field: str) -> None:
        if field in result:
            result.pop(field)
            omitted.append(field)
            result["omitted_fields"] = omitted

    # The logical plan belongs only inside full explain, never in a second copy.
    omit("logical_plan")
    if compact:
        for field in _PLAN_DETAIL_FIELDS:
            omit(field)
    optional_fields: tuple[str, ...] = _OPTIONAL_RESPONSE_FIELDS
    if not sql_required:
        optional_fields = (*_PLAN_DETAIL_FIELDS, "rendered_sql", *_OPTIONAL_RESPONSE_FIELDS)
    for field in optional_fields:
        if len(json_text(result)) <= limit:
            return result
        omit(field)
    return result


def _as_columns(payload: Mapping[str, Any], *, limit: int, records_chars: int) -> dict[str, Any]:
    """``payload``'s record rows as columns, with the ``ROW_FORMAT_COLUMNS`` note saying why."""
    out = _columnar_rows(payload)
    note = {
        "code": "ROW_FORMAT_COLUMNS",
        "severity": "info",
        "message": (
            f"Rows are returned as columns: as records they exceed the {limit:,}-character "
            "response budget. Each row is an array in the order of 'columns'."
        ),
        "details": {"max_result_chars": limit, "records_chars": records_chars},
    }
    out["warnings"] = [*list(out.get("warnings") or []), note]
    return out


def _bound_response(
    payload: dict[str, Any],
    *,
    compact: bool,
    query: Mapping[str, Any],
    sql_required: bool = True,
    columns_fallback: bool = False,
) -> dict[str, Any]:
    """Budget the final tool payload without cutting rows or changing its outcome.

    Both MCP representations serialize this same payload. Compiler detail and
    echoes are optional; rows, SQL-mode text, query drafts and diagnostics are not.
    With ``columns_fallback``, record rows that can't fit are returned as columns when
    those fit, from the rows already fetched. If the required fields still cannot fit,
    return a bounded, explicit refusal.
    """
    limit = _max_result_chars()

    def trimmed(response: Mapping[str, Any]) -> dict[str, Any]:
        return _without_optional_fields(
            response, compact=compact, sql_required=sql_required, limit=limit
        )

    result = trimmed(payload)
    if len(json_text(result)) <= limit:
        return result
    if columns_fallback and _record_rows(result):
        columns = trimmed(_as_columns(payload, limit=limit, records_chars=len(json_text(result))))
        if len(json_text(columns)) <= limit:
            return columns

    largest = _largest_field(result)
    retry = {
        **({"row_format": "columns"} if _record_rows(result) else {}),
        **({"sql_profile": "off"} if "rendered_sql" in result else {}),
    }
    details: dict[str, Any] = {
        "max_result_chars": limit,
        "result_chars": len(json_text(result)),
        "largest_field": largest,
        **({"retry": retry} if retry else {}),
    }
    retry_text = " and ".join(f'{key} "{value}"' for key, value in retry.items())
    message = (
        f"Response exceeds the {limit:,}-character budget after omitting optional details; "
        f"'{largest}' is its largest field. "
        + (f"Retry with {retry_text}, or request" if retry_text else "Request")
        + " fewer objects, rows or columns."
    )
    if "rows" in result:
        returned = int(result.get("row_count", result.get("preview_row_count", 0)) or 0)
        total = None if result.get("truncated") else returned
        counted = f"{total:,}" if total is not None else f"more than {returned:,}"
        message = (
            f"The result has {counted} rows and exceeds the {limit:,}-character response budget "
            "without optional details. No rows returned. Retry "
            + (f"with {retry_text}, " if retry_text else "")
            + f"selecting fewer columns ('{largest}' is the largest); or "
            f"{_narrowing_advice(query)}, or lower max_rows."
        )
        details.update(row_count=returned, total_row_count=total)
    error = SemanticLayerError("RESULT_TOO_LARGE", message, details=details)
    issue = {"code": error.code, "message": str(error), "details": error.details}
    refusal: dict[str, Any] = {
        "ok": False,
        "status": "error",
        "error": {"code": issue["code"]},
        "errors": [issue],
    }
    if len(json_text(refusal)) > limit:
        issue["message"] = (
            "Required response fields exceed the budget. Request fewer rows or objects."
        )
        issue["details"] = {"max_result_chars": limit}
    # Identity/context echoes can themselves be unbounded. Include only what fits.
    for field in ("request_id", "api_version", "package_id", "request_context", "omitted_fields"):
        if field in result and len(json_text({**refusal, field: result[field]})) <= limit:
            refusal[field] = result[field]
    return refusal


_DISCOVER_SCREENED_KEYS = ("low_relevance", "out_of_scope")


def _coerce_kinds(value: Any) -> list[str]:
    try:
        return singular_kinds(parse_string_list(value))
    except ValueError as exc:
        raise _argument_error(f"MCP argument 'kinds' {exc}.", field="kinds", value=value) from exc


def _coerce_int(value: Any, default: int, *, field: str, minimum: int | None = None) -> int:
    if value is None or value == "":
        parsed = default
    elif isinstance(value, bool):
        raise _argument_error(
            f"MCP argument '{field}' must be an integer.", field=field, value=value
        )
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise _argument_error(
                f"MCP argument '{field}' must be an integer.", field=field, value=value
            ) from exc
    if minimum is not None and parsed < minimum:
        raise _argument_error(
            f"MCP argument '{field}' must be greater than or equal to {minimum}.",
            field=field,
            value=value,
        )
    return parsed


def _status_label(payload: Mapping[str, Any]) -> str:
    if "status" in payload:
        return str(payload["status"])
    return "ok" if bool(payload.get("ok", True)) else "error"


_EMPTY: tuple[Any, ...] = (None, "", [], {})
_DISCOVER_BUCKETS = (
    "measures",
    "metrics",
    "segments",
    "dimensions",
    "entities",
    "relationships",
    "temporal_roles",
    "value_domains",
    "dimension_values",
    "blocked",
)


_WORDS = re.compile(r"[^\W_]+")


def _label_restates_id(label: Any, object_id: Any) -> bool:
    """Whether every word of ``label`` is already a word of ``object_id`` past its kind.

    "Store name" restates ``dimension.store_name``; "Orders" doesn't restate
    ``measure.order_count``, and "Food" doesn't restate ``dimension.product_type``.
    """

    if not isinstance(label, str) or not isinstance(object_id, str):
        return False
    words = set(_WORDS.findall(label.casefold()))
    return bool(words) and words <= set(_WORDS.findall(object_id.partition(".")[2].casefold()))


def _lean_discover(payload: dict[str, Any]) -> dict[str, Any]:
    """State each fact of a minimal discover response once.

    A card in a kind's bucket leaves out its kind, ``available: true``, empty fields and a label
    whose every word is already in its id; blocked and dimension-value cards keep their explicit
    fields. The terms and verbosity echoes and an empty query state or selection context go too.
    Compact uses this projection without blocked cards.
    """

    if payload.get("verbosity") != "minimal":
        return payload

    def lean(row: Any, bucket: str) -> Any:
        if not isinstance(row, dict) or bucket in ("blocked", "dimension_values"):
            return row
        repeats = {"kind": row.get("kind"), "available": True}
        return {
            key: value
            for key, value in row.items()
            if value not in _EMPTY
            and not (key in repeats and value == repeats[key])
            and not (key == "label" and _label_restates_id(value, row.get("id")))
        }

    for key in ("terms", "verbosity"):
        payload.pop(key, None)
    for key in ("query_state", "selection_context"):
        if isinstance(payload.get(key), dict) and not any(payload[key].values()):
            payload.pop(key)
    for key in _DISCOVER_BUCKETS:
        if isinstance(payload.get(key), list):
            payload[key] = [lean(row, key) for row in payload[key]]
    return payload


def _lean_issue(issue: Any, *, verbosity: str = "compact") -> Any:
    """State an issue's facts once: drop empty optional fields and echoes of code or message."""

    if not isinstance(issue, dict):
        return issue
    echoes = {"why_invalid": issue.get("message"), "unsupported_construct": issue.get("code")}
    lean = {
        key: value
        for key, value in issue.items()
        if key in ("code", "message")
        or not (value in _EMPTY or (key in echoes and value == echoes[key]))
    }
    if isinstance(lean.get("recovery_hints"), list):
        # Hints must be actionable on their own (for example, valid_kinds on a retry).
        lean["recovery_hints"] = [_lean_issue(hint) for hint in lean["recovery_hints"]]
    if verbosity == "minimal" and lean.get("code") in {"MIXED_GRAIN_INVALID", "REWRITE_APPLIED"}:
        details = dict(lean.get("details") or {})
        details.pop("analysis", None)
        if lean["code"] == "REWRITE_APPLIED":
            details.pop("path", None)
        lean["details"] = details
    return lean


def _internal_issue(message: str, *, exception_type: str = "") -> dict[str, Any]:
    return {
        "code": "INTERNAL_ERROR",
        "message": message or exception_type or "internal error",
        "severity": "error",
        "stage": "mcp",
        "details": {"exception_type": exception_type} if exception_type else {},
        "object_ids": [],
        "path": "",
        "recovery_hints": [
            {
                "kind": "file_bug_report",
                "message": (
                    "An unexpected error reached the MCP boundary. "
                    "Retry once; if it recurs, please file a bug at "
                    "https://github.com/semantic-rails/semantic-rails/issues "
                    "with the tool name, arguments, and the request_id "
                    "from this response."
                ),
            }
        ],
    }


class SemanticLayerMCPAdapter:
    """In-process MCP-style adapter backed by a Semantic Layer Runtime.

    The adapter intentionally returns plain dictionaries so tests and host
    applications can call handlers directly without installing an MCP runtime.
    """

    def __init__(self, runtime: Runtime, *, interface: str | None = None):
        raw = interface or ""
        if str(raw).strip().lower() not in {"", _INTERFACE}:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "The v1 MCP interface was removed; v2 is the only interface. "
                "Set interface='v2' or omit interface.",
                details={"interface": str(raw), "valid_values": [_INTERFACE]},
            )
        self.interface = _INTERFACE
        self.runtime = runtime
        self.package_id = runtime.package_id
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "discover": self._handle_discover,
            "inspect": self._handle_inspect,
            "valid-values": self._handle_valid_values,
            "plan": self._handle_plan,
            "execute": self._handle_execute_mode,
            "segment": self._handle_segment_action,
        }
        self._builtin_tool_handlers = self._tool_handlers.copy()

    @property
    def instructions(self) -> str:
        instructions = MCP_SERVER_INSTRUCTIONS
        if "segment" not in self._package_tool_names():
            instructions = instructions.replace(
                "segment(segment_id, action) validates, explains or previews a package-authored "
                "segment.\n\n",
                "",
            )
        return instructions

    @classmethod
    def from_package(cls, package_id: str) -> SemanticLayerMCPAdapter:
        return cls(Runtime(package_id))

    @classmethod
    def from_path(cls, path: str) -> SemanticLayerMCPAdapter:
        return cls(Runtime.from_path(path))

    @property
    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return dict(self._tool_handlers)

    def replace_tool_handler(
        self, name: str, handler: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        """Serve the tool ``name`` with ``handler`` when the current package lists it.

        ``call_tool`` still validates the arguments, merges the trusted request context and
        audits the call. Like the built-in tools, ``handler`` runs inside the adapter's
        response envelope, which turns an exception into a tool error response.
        """
        if name not in self._tool_handlers:
            raise ValueError(f"Unknown MCP tool {name!r}; tools: {sorted(self._tool_handlers)}")
        self._tool_handlers[name] = handler

    def close(self) -> None:
        self.runtime.close()

    def list_tools(self) -> list[dict[str, Any]]:
        return list_tool_definitions(config=self.runtime.config)

    def _package_tool_names(self) -> set[str]:
        """The tools the whole package offers. Every call reads this, so it asks the snapshot:
        ``Runtime.config`` copies the whole package."""
        return _offered_tool_names(self.runtime.snapshot.has_segments)

    def _caller_tool_names(self) -> set[str]:
        """The tools a caller's view offers: no segment tool when they see no segment."""
        try:
            view = self.runtime.view_for(_caller())
        except Exception:  # noqa: BLE001 — the call itself refuses an unresolved view
            return self._package_tool_names()
        return _offered_tool_names(bool(view.segments))

    def list_resources(self) -> list[dict[str, Any]]:
        return list_resource_definitions()

    def list_prompts(self) -> list[dict[str, Any]]:
        has_segment = "segment" in self._package_tool_names()
        return [
            prompt
            for prompt in list_prompt_definitions()
            if prompt["name"] != "semantic-rails-segment-workflow" or has_segment
        ]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_context: RequestContext | None = None,
        session: MCPQuerySession | None = None,
    ) -> dict[str, Any]:
        """Call one MCP tool.

        ``request_context`` is who is asking, resolved from credentials: remote transports
        pass it after their authentication boundary, a host passes its caller's. Without it
        the caller is the local operator. Arguments never say who is asking.
        ``session`` enables advisory repeat hints for calls in that session.
        """

        # A new invocation owns its identity even when a host handler calls it
        # from another tool. Isolate argument-error envelopes and finish too.
        context_token = _TOOL_REQUEST_CONTEXT.set(request_context)
        dispatch_token = _BUILTIN_TOOL_DISPATCH.set(False)
        try:
            return self._call_tool(
                name, arguments, request_context=request_context, session=session
            )
        finally:
            _BUILTIN_TOOL_DISPATCH.reset(dispatch_token)
            _TOOL_REQUEST_CONTEXT.reset(context_token)

    def _call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        request_context: RequestContext | None,
        session: MCPQuerySession | None,
    ) -> dict[str, Any]:
        args_dict: dict[str, Any] = {}

        def finish(response: dict[str, Any]) -> dict[str, Any]:
            if request_context is not None:
                response["request_context"] = request_context_payload(request_context)
                if request_context.request_id:
                    response["request_id"] = request_context.request_id
            audit_context = dict(response.get("request_context", {}) or {})
            compact = _verbosity(args_dict) == "compact"
            raw = args_dict.get("query")
            query: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
            run = name == "execute" and str(args_dict.get("mode") or "run").strip().lower() == "run"
            # Rows a caller asked for as records by name are never sent as columns.
            columns_fallback = run and not str(args_dict.get("row_format") or "").strip()

            def bound(payload: dict[str, Any]) -> dict[str, Any]:
                return _bound_response(
                    payload,
                    compact=compact,
                    query=query,
                    sql_required=not run,
                    columns_fallback=columns_fallback,
                )

            response = bound(response)
            if session is not None and response.get("ok") is True:
                key = _call_key(args_dict) if name == "execute" else None
                session.annotate(self, name, args_dict, response, query=key)
                response = bound(response)
                if key is not None:
                    session.record_run(self, args_dict, response, query=key)
            elif session is not None:
                session.reset_validate_guidance()
            emit_audit_event(
                "mcp_tool",
                tool=name,
                package_id=self.package_id,
                request_id=str(response.get("request_id", "")),
                status=response.get("status"),
                request_context=audit_context,
                error_codes=[
                    str(issue.get("code", ""))
                    for issue in list(response.get("errors", []) or [])
                    if isinstance(issue, Mapping) and issue.get("code")
                ],
                timing_ms=response.get("timing_ms"),
            )
            return response

        if arguments is not None and not isinstance(arguments, Mapping):
            return finish(
                self._error_response(
                    SemanticLayerError(
                        "INVALID_MCP_ARGUMENTS",
                        "MCP tool arguments must be a JSON object.",
                        details={"tool": name, "argument_type": type(arguments).__name__},
                    ),
                    {},
                )
            )
        # The transport owns correlation identity too: a JSON-RPC argument must not
        # disagree with the request context recorded in audit events.
        args_dict = dict(arguments or {})
        if request_context is not None:
            args_dict.pop("request_id", None)
            if request_context.request_id:
                args_dict["request_id"] = request_context.request_id
        try:
            args_dict = coerce_arguments(args_dict, tool=name, remote=request_context is not None)
        except SemanticLayerError as exc:
            return finish(self._error_response(exc, args_dict))
        handler = self._tool_handlers.get(name)
        available_tools = self._package_tool_names()
        if handler is None or name not in available_tools or name == "segment":
            available_tools = self._caller_tool_names()
        if handler is None or name not in available_tools:
            details: dict[str, Any] = {"tool": name, "available_tools": sorted(available_tools)}
            message = f"Unknown MCP tool '{name}'"
            return finish(
                self._error_response(
                    SemanticLayerError("UNKNOWN_MCP_TOOL", message, details=details), args_dict
                )
            )
        unknown_arg_error = _unknown_argument_error(tool_name=name, arguments=args_dict)
        if unknown_arg_error is not None:
            return finish(self._error_response(unknown_arg_error, args_dict))
        type_error = _required_string_type_error(tool_name=name, arguments=args_dict)
        if type_error is not None:
            return finish(self._error_response(type_error, args_dict))
        missing_arg_error = _missing_required_argument_error(tool_name=name, arguments=args_dict)
        if missing_arg_error is not None:
            return finish(self._error_response(missing_arg_error, args_dict))
        # Compute unknown-arg warnings once at the boundary (warn-tools
        # only). Per-handler typo checks (e.g. DISCOVER_UNKNOWN_ARG for
        # ``term``→``terms``) still fire from inside the handler with their
        # domain-specific guidance; this generic pass catches everything else.
        unknown_arg_warnings = _unknown_argument_warnings(tool_name=name, arguments=args_dict)

        def dispatch(args: dict[str, Any]) -> dict[str, Any]:
            # Only this invocation's built-in handler may reuse the boundary.
            token = _BUILTIN_TOOL_DISPATCH.set(handler is self._builtin_tool_handlers.get(name))
            try:
                return handler(args)
            finally:
                _BUILTIN_TOOL_DISPATCH.reset(token)

        response = self._guarded(args_dict, dispatch)
        if unknown_arg_warnings:
            existing = list(response.get("warnings") or [])
            # Avoid duplicating per-handler typo warnings that already
            # cover the same key with a more specific message.
            handler_warned_keys: set[str] = set()
            for warning in existing:
                if not isinstance(warning, dict):
                    continue
                code = str(warning.get("code", ""))
                if not code.endswith("_UNKNOWN_ARG"):
                    continue
                received = (warning.get("details") or {}).get("received")
                if isinstance(received, str):
                    handler_warned_keys.add(received)
            deduped = [
                warning
                for warning in unknown_arg_warnings
                if warning["details"]["received"] not in handler_warned_keys
            ]
            if deduped:
                response["warnings"] = existing + [_lean_issue(warning) for warning in deduped]
        return finish(response)

    def read_resource(
        self, uri: str, *, request_context: RequestContext | None = None
    ) -> dict[str, Any]:
        policy_context = (
            request_context.to_policy_context() if request_context is not None else None
        )
        request_id = request_context.request_id if request_context is not None else ""
        if uri in {"semantic-rails://capabilities", "semantic-rails://capabilities/summary"}:
            tools = self.list_tools()
            if uri.endswith("/summary"):
                tools = [
                    {
                        "name": tool["name"],
                        "title": (tool.get("annotations") or {}).get("title", tool["name"]),
                    }
                    for tool in tools
                ]
            payload: dict[str, Any] = {
                "interface_version": self.interface,
                "package_id": self.package_id,
                "tools": tools,
                "resources": self.list_resources(),
                "prompts": self.list_prompts(),
            }
        elif uri in {"semantic-rails://catalog/summary", "semantic-rails://catalog/index"}:
            payload = self._envelope(
                {
                    "catalog": operations.run(
                        "catalog",
                        self.runtime,
                        view="summary",
                        verbosity="compact" if uri.endswith("/summary") else "summary",
                        policy_context=policy_context,
                    )
                },
                request_id=request_id,
                started_at=time.perf_counter(),
            )
        elif uri == "semantic-rails://catalog/full":
            payload = self._envelope(
                {
                    "catalog": operations.run(
                        "catalog",
                        self.runtime,
                        view="summary",
                        verbosity="full",
                        policy_context=policy_context,
                    )
                },
                request_id=request_id,
                started_at=time.perf_counter(),
            )
        else:
            issue = exception_issue(
                SemanticLayerError(
                    "UNKNOWN_MCP_RESOURCE",
                    f"Unknown MCP resource '{uri}'",
                    details={
                        "uri": uri,
                        "available_resources": [row["uri"] for row in self.list_resources()],
                    },
                ),
                stage="mcp",
            )
            payload = self._envelope(
                {"ok": False, "status": "error", "error": issue, "errors": [issue]},
                request_id=request_id,
                started_at=time.perf_counter(),
            )
        if request_context is not None:
            payload["request_context"] = request_context_payload(request_context)
        text = json_text(payload)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": text,
            "payload": payload,
        }

    def get_prompt(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        if name in {prompt["name"] for prompt in self.list_prompts()}:
            text = _PROMPT_TEXT[name].format(
                intent=str(args.get("intent", "") or ""),
                package_id=str(args.get("package_id", self.package_id) or self.package_id),
                query_json=str(args.get("query_json", "") or ""),
                segment_id=str(args.get("segment_id", "") or ""),
            )
        else:
            issue = exception_issue(
                SemanticLayerError(
                    "UNKNOWN_MCP_PROMPT",
                    f"Unknown MCP prompt '{name}'",
                    details={
                        "prompt": name,
                        "available_prompts": [row["name"] for row in self.list_prompts()],
                    },
                ),
                stage="mcp",
            )
            return self._envelope(
                {"ok": False, "errors": [issue], "messages": []},
                request_id=_clean_request_id(args.get("request_id")),
                started_at=time.perf_counter(),
                arguments=args,
            )
        return {
            "name": name,
            "description": next(
                (row["description"] for row in self.list_prompts() if row["name"] == name), ""
            ),
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": text,
                    },
                }
            ],
        }

    def _envelope(
        self,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        started_at: float,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        out = dict(payload or {})
        out.setdefault("ok", not bool(out.get("errors")))
        out["status"] = _status_label(out)
        out.setdefault("api_version", self.interface)
        out.setdefault("request_id", request_id or uuid.uuid4().hex)
        out.setdefault("package_id", self.package_id)
        if "errors" not in out:
            error = out.get("error")
            if isinstance(error, dict):
                out["errors"] = [error]
            elif error:
                out["errors"] = [_internal_issue(str(error))]
        verbosity = _verbosity(arguments or {})
        # Errors and warnings only when there are some; no empty fields.
        for key in ("errors", "warnings"):
            issues = [_lean_issue(issue, verbosity=verbosity) for issue in out.pop(key, None) or []]
            if issues:
                out[key] = issues
        # Keep the conventional truthy error branch for validate soft-fails,
        # while the complete issues and their hints live only in errors.
        if out.get("errors"):
            first = out["errors"][0]
            hints = [hint for issue in out["errors"] for hint in issue.get("recovery_hints", [])]
            extra = [_lean_issue(hint) for hint in out.pop("recovery_hints", []) or []]
            extra = [hint for hint in extra if hint not in hints]
            if extra:
                first["recovery_hints"] = list(first.get("recovery_hints", [])) + extra
                hints += extra
            if "query_ir_hints" in out:
                out["query_ir_hints"] = [
                    hint for hint in out["query_ir_hints"] if _lean_issue(hint) not in hints
                ]
            out["error"] = {key: first[key] for key in ("code", "message")}
        elif out.get("recovery_hints"):
            # Discovery next steps are independent of error recovery hints.
            out["recovery_hints"] = [_lean_issue(hint) for hint in out["recovery_hints"]]
        else:
            out.pop("recovery_hints", None)
        out.setdefault("timing_ms", round((time.perf_counter() - started_at) * 1000, 3))
        return out

    def _with_request_context(self, out: dict[str, Any]) -> dict[str, Any]:
        # Echo who is asking only when there is an identity (a transport's or a host's).
        context = request_context_payload(_caller())
        if context:
            out.setdefault("request_context", context)
        return out

    def _success(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any], started_at: float
    ) -> dict[str, Any]:
        out = self._envelope(
            payload,
            request_id=_clean_request_id(arguments.get("request_id")),
            started_at=started_at,
            arguments=arguments,
        )
        return self._with_request_context(out)

    def _error_response(
        self,
        exc: SemanticLayerError,
        arguments: Mapping[str, Any],
        *,
        started_at: float | None = None,
    ) -> dict[str, Any]:
        # Surface closest_matches on OBJECT_NOT_FOUND just like the HTTP
        # path — agents shouldn't have to retry blind on a typo'd id.
        request_id = _clean_request_id(arguments.get("request_id")) or uuid.uuid4().hex
        try:
            # Suggestions come from the caller's view; without one, none are offered.
            config = self.runtime.view_for(_caller())
        except Exception:  # noqa: BLE001 — uncertain visibility suggests nothing
            config = None
        if config is not None:
            exc = enrich_object_not_found(exc, config)
        issue = exception_issue(exc, stage="mcp")
        out = self._envelope(
            {"ok": False, "status": "error", "error": issue, "errors": [issue]},
            request_id=request_id,
            started_at=started_at or time.perf_counter(),
            arguments=arguments,
        )
        return self._with_request_context(out)

    def _guarded(
        self, arguments: dict[str, Any], handler: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        if _BUILTIN_TOOL_DISPATCH.get():
            # Consume dispatch's one-use marker before entering the builder.
            _BUILTIN_TOOL_DISPATCH.set(False)
            return handler(arguments)
        started = time.perf_counter()
        try:
            return self._success(handler(arguments), arguments, started)
        except SemanticLayerError as exc:
            return self._error_response(exc, arguments, started_at=started)
        except RecursionError:
            return self._error_response(
                SemanticLayerError(
                    "INVALID_QUERY", "Query nesting is too deep.", details={"path": "query"}
                ),
                arguments,
                started_at=started,
            )
        except Exception as exc:  # noqa: BLE001 — defensive MCP boundary
            # Bare exceptions (KeyError, AttributeError, TypeError, ...)
            # must never escape as raw tracebacks. Log the trace for ops
            # and wrap as a structured INTERNAL_ERROR envelope so the
            # agent can recover (or at least file a bug with context).
            import logging

            logging.getLogger(__name__).exception(
                "unhandled exception in MCP handler: %s",
                exc,
            )
            issue = _internal_issue(
                UNEXPECTED_ERROR_MESSAGE,
                exception_type=type(exc).__name__,
            )
            out = self._envelope(
                {"ok": False, "status": "error", "error": issue, "errors": [issue]},
                request_id=_clean_request_id(arguments.get("request_id")),
                started_at=started,
            )
            return self._with_request_context(out)

    def _handle_discover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _build(args: dict[str, Any]) -> dict[str, Any]:
            raw_terms = args.get("terms", "")
            terms_warnings: list[dict[str, Any]] = []
            filter_dropped = False
            # Catch the most common typo (`term` singular) so the call
            # doesn't silently behave like an empty-terms browse.
            for typo, canonical in (("term", "terms"), ("kind", "kinds")):
                if typo in args and canonical not in args:
                    filter_dropped = filter_dropped or canonical == "kinds"
                    terms_warnings.append(
                        {
                            "code": "DISCOVER_UNKNOWN_ARG",
                            "severity": "warning",
                            "message": (
                                f"Received unknown argument '{typo}'; "
                                f"did you mean '{canonical}'? The value was ignored."
                            ),
                            "details": {"received": typo, "expected": canonical},
                        }
                    )
            if raw_terms is None or isinstance(raw_terms, str):
                terms_str = str(raw_terms or "")
            elif isinstance(raw_terms, (bool, int, float)):
                terms_str = str(raw_terms)
                terms_warnings.append(
                    {
                        "code": "DISCOVER_TERMS_COERCED",
                        "severity": "warning",
                        "message": (
                            f"'terms' arrived as {type(raw_terms).__name__}; "
                            f"coerced to string {terms_str!r}. Pass terms as "
                            "a string to silence this warning."
                        ),
                    }
                )
            else:
                raise _argument_error(
                    "MCP argument 'terms' must be a string.",
                    field="terms",
                    value=raw_terms,
                )
            # A kinds filter that is malformed or names no real kind is refused,
            # never dropped: an empty result must mean the search ran over the
            # requested kinds and found nothing. A ranked search leaves the
            # kind check to `discover_payload` (or the resource grant), which
            # know which kinds this call can produce.
            requested_kinds = _coerce_kinds(args.get("kinds", []))
            if not terms_str.strip():
                # Empty terms list the ids instead of ranking, a page per kind.
                policy_context = _policy_context_payload()
                # A grant lists only the kinds it can produce, so a filter for
                # any other kind is refused, never answered with an empty list.
                granted = policy_context.get("metric_allowlist") is not None
                requested_kinds = checked_discover_kinds(
                    requested_kinds, GRANT_DISCOVER_KINDS if granted else DISCOVER_KINDS
                )
                catalog = operations.run(
                    "catalog",
                    self.runtime,
                    view="summary",
                    verbosity="summary",
                    policy_context=policy_context,
                )
                if "dimension_value" in requested_kinds:
                    # The catalog lists no values; discover's own value cards (the caller's
                    # view, unranked) are their ids.
                    values = operations.run(
                        "discover",
                        self.runtime,
                        terms="",
                        kinds=["dimension_value"],
                        partial_query={"policy_context": policy_context},
                        verbosity="compact",
                        limit=sys.maxsize,
                    )
                    catalog["dimension_value_ids"] = sorted(
                        str(row["id"]) for row in values.get("dimension_values") or []
                    )
                kinds = set(requested_kinds)
                size = _coerce_int(args.get("limit"), _DISCOVER_ID_PAGE, field="limit", minimum=1)
                start = _coerce_int(args.get("offset"), 0, field="offset", minimum=0)
                page: dict[str, Any] = {}
                more: dict[str, int] = {}
                for key, value in catalog.items():
                    kind = key.removesuffix("_ids")
                    if key == kind:
                        page[key] = value
                    elif not kinds or kind in kinds:
                        page[key] = list(value)[start : start + size]
                        if len(value) > start + size:
                            more[kind] = len(value) - start - size
                if more:
                    terms_warnings.append(
                        {
                            "code": "DISCOVER_IDS_TRUNCATED",
                            "severity": "warning",
                            "message": (
                                f"Listed {size} ids per kind from offset {start}; more remain "
                                f"for {sorted(more)}. Pass offset={start + size} for the next "
                                "page, kinds to list fewer kinds, or terms to rank candidates."
                            ),
                            "details": {"remaining": more, "next_offset": start + size},
                        }
                    )
                return {"catalog": page, "warnings": terms_warnings}
            verbosity = str(args.get("verbosity") or "minimal")
            payload = operations.run(
                "discover",
                self.runtime,
                terms=terms_str,
                kinds=requested_kinds,
                partial_query=self._seed(args),
                stage=str(args.get("stage", "")),
                verbosity="compact" if verbosity == "minimal" else verbosity,
                limit=_coerce_int(args.get("limit"), 10, field="limit", minimum=1),
                enforce_scope=True,
            )
            if terms_warnings:
                existing = list(payload.get("warnings") or [])
                payload["warnings"] = existing + terms_warnings
            # Read the payload's own signal instead of re-deriving emptiness: it
            # counts every bucket (dimension values included) and says whether the
            # search ran (`no_matches`) or was screened out before it did
            # (`low_relevance`, `out_of_scope`). Mirrors the empty-terms
            # `DISCOVER_NO_TERMS` warning: give the agent a concrete next step.
            searched_nothing = "no_matches" in payload
            screened = any(isinstance(payload.get(key), dict) for key in _DISCOVER_SCREENED_KEYS)
            if terms_str.strip() and (searched_nothing or screened):
                existing_hints = list(payload.get("recovery_hints") or [])
                # Hoist any nested low_relevance/out_of_scope recovery_hint
                # up to the top-level recovery_hints list so callers don't
                # have to dig for it.
                for nested_key in _DISCOVER_SCREENED_KEYS:
                    nested = payload.get(nested_key)
                    if isinstance(nested, dict):
                        nested_hint = nested.get("recovery_hint")
                        if isinstance(nested_hint, dict):
                            existing_hints.append(nested_hint)
                        elif isinstance(nested_hint, str) and nested_hint.strip():
                            existing_hints.append(
                                {"kind": f"discover_{nested_key}", "message": nested_hint}
                            )
                # "No semantic objects matched" is only true for a search that
                # ran over the requested kinds; a misspelled `kind` was never
                # applied, and a screened-out search never ran at all.
                if screened:
                    browse_message = (
                        f"'{terms_str}' was not searched as a catalog query (see the hint "
                        "above). Call discover with empty terms to list every id."
                    )
                    if filter_dropped:
                        browse_message += (
                            " The 'kind' argument was also ignored (the argument is 'kinds')."
                        )
                elif filter_dropped:
                    browse_message = (
                        f"Nothing ranked for '{terms_str}', and the 'kind' argument was "
                        "ignored (the argument is 'kinds'), so the requested kind filter "
                        "was not applied. Retry with 'kinds', or call discover with empty "
                        "terms to list every id."
                    )
                else:
                    of_kinds = f" of kind {sorted(set(requested_kinds))}" if requested_kinds else ""
                    browse_message = (
                        f"No semantic objects{of_kinds} matched '{terms_str}'. "
                        "Call discover with empty terms to list every id."
                    )
                existing_hints.append(
                    {"kind": "browse_catalog_or_capabilities", "message": browse_message}
                )
                payload["recovery_hints"] = existing_hints
            if verbosity in {"minimal", "compact"} and "verbosity" in payload:
                payload["verbosity"] = "minimal"
                try:
                    view = self.runtime.view_for(_caller())
                except Exception:  # noqa: BLE001 — uncertain visibility: the plain projection
                    view = None
                payload = _slim_discover_minimal(payload, view)
                if verbosity == "compact":
                    payload.pop("blocked", None)
                    for bucket in _DISCOVER_BUCKETS:
                        if bucket in payload:
                            payload[bucket] = [
                                row for row in payload[bucket] if row.get("available") is not False
                            ]
            return _lean_discover(payload)

        return self._guarded(arguments, _build)

    def _handle_inspect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: operations.run(
                "inspect",
                self.runtime,
                object_id=str(args.get("object_id", "")),
                partial_query=self._seed(args),
                verbosity=str(args.get("verbosity") or "minimal"),
            ),
        )

    def _handle_valid_values(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def build(args: dict[str, Any]) -> dict[str, Any]:
            call_arguments: dict[str, Any] = {
                "dimension_id": str(args.get("dimension_id", "")),
                "query": self._seed(args),
                "search": str(args.get("search", "")),
                "limit": min(
                    _coerce_int(args.get("limit"), 100, field="limit", minimum=1),
                    max_valid_values_limit(),
                ),
                "offset": min(
                    _coerce_int(args.get("offset"), 0, field="offset", minimum=0),
                    max_valid_values_offset(),
                ),
                "include_counts": _coerce_bool(args.get("include_counts"), False),
                "allow_live_query": _coerce_bool(args.get("allow_live_query"), False),
            }
            payload = operations.run("valid-values", self.runtime, **call_arguments)
            if payload.get("status") == "needs_live_query":
                retry = {**call_arguments, "allow_live_query": True, "query": args.get("query")}
                if retry["query"] is None:
                    retry.pop("query")
                payload["next_call"] = {"tool": "valid-values", "arguments": retry}
            return payload

        return self._guarded(arguments, build)

    def _handle_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: operations.run(
                "plan",
                self.runtime,
                intent=str(args.get("intent", "")),
                partial_query={
                    **_object_argument(args.get("query"), field="query"),
                    "policy_context": _policy_context_payload(),
                },
                now=args.get("now"),
                detail=_plan_detail(args.get("detail")),
                limit=_coerce_int(args.get("limit"), 3, field="limit", minimum=1),
            ),
        )

    def _seed(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        """A metadata tool's ``query`` seed in the engine's form, with the caller's identity."""
        seed, context = args.get("query"), _policy_context_payload()
        if not seed and not context:
            return None
        return self.runtime.engine_seed(
            {**_object_argument(seed, field="query"), "policy_context": context}
        )

    def _call_options(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The service entry's arguments beside the tool's ``query``."""
        return {
            "now": args.get("now"),
            "context": _caller(),
            "verbosity": str(args.get("verbosity") or "").strip() or MCP_DEFAULT_QUERY_VERBOSITY,
            "sql_profile": str(args.get("sql_profile") or "").strip() or None,
        }

    def _handle_execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            row_format = _row_format_arg(args)
            cap = _max_rows_arg(args.get("max_rows")) or MCP_DEFAULT_MAX_ROWS
            call = args["query"]
            options = self._call_options(args)
            result = operations.run(
                "query", self.runtime, query=call, limits={"max_rows": cap}, **options
            )
            if bool(result.get("ok", True)):
                result = _truncate_rows(result, cap=cap, query=call)
            # Surface an EXECUTE_EMPTY_RESULT warning when a successful
            # execute returns 0 rows and the user authored no filters —
            # the most common "successful but wrong" outcome from a
            # planner-derived call.
            if (
                bool(result.get("ok", True))
                and int(result.get("row_count", 0) or 0) == 0
                and not call.get("where")
                and not call.get("having")
            ):
                existing = list(result.get("warnings") or [])
                # Don't double-warn if the runtime already flagged
                # EMPTY_RESULT_WINDOW or similar.
                if not any(
                    str(w.get("code", "")).startswith("EMPTY_")
                    for w in existing
                    if isinstance(w, dict)
                ):
                    existing.append(
                        {
                            "code": "EXECUTE_EMPTY_RESULT",
                            "severity": "warning",
                            "message": (
                                "Execute returned 0 rows with no user filters. "
                                "Verify the selected measure/metric exists in the "
                                "time window, or widen the time range. Use execute "
                                "with mode='sql' and verbosity='full' to inspect the "
                                "resolved plan."
                            ),
                        }
                    )
                    result["warnings"] = existing
            if bool(result.get("ok", True)) and row_format == "columns":
                result = _columnar_rows(result)
            return result

        return self._guarded(arguments, _run)

    def _handle_execute_mode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """``execute``: mode ``run``, ``validate`` or ``sql`` runs, validates or
        compiles the query."""

        try:
            mode = _choice_arg(
                arguments.get("mode"), "run", _EXECUTE_MODES, tool="execute", field="mode"
            )
        except SemanticLayerError as exc:
            return self._error_response(exc, arguments)
        args = {key: value for key, value in arguments.items() if key != "mode"}
        if mode == "run":
            return self._handle_execute(args)
        operation = "validate" if mode == "validate" else "compile"

        def run(args: dict[str, Any]) -> dict[str, Any]:
            return operations.run(
                operation, self.runtime, query=args["query"], **self._call_options(args)
            )

        # row_format and max_rows shape a run's rows only.
        return self._guarded(
            {key: value for key, value in args.items() if key not in ("row_format", "max_rows")},
            run,
        )

    def _handle_segment_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """``segment``: validate, explain or preview a segment, at minimal
        verbosity unless the caller asks for more."""

        try:
            action = _choice_arg(
                arguments.get("action"), "", _SEGMENT_ACTIONS, tool="segment", field="action"
            )
        except SemanticLayerError as exc:
            return self._error_response(exc, arguments)
        args = {key: value for key, value in arguments.items() if key != "action"}
        if not str(args.get("verbosity") or "").strip():
            args["verbosity"] = "minimal"

        def run(args: dict[str, Any]) -> dict[str, Any]:
            segment_id = str(args.get("segment_id", ""))
            policy_context = _policy_context_payload()
            segment: dict[str, Any] = {"segment_id": segment_id, "policy_context": policy_context}
            if action == "preview":
                segment["limit"] = _coerce_int(args.get("limit"), 50, field="limit", minimum=1)
            payload = operations.run(f"segment-{action}", self.runtime, **segment)
            return _segment_response(action, payload, args.get("verbosity"))

        return self._guarded(args, run)


def json_text(payload: Any) -> str:
    """Render a payload for an MCP text channel: compact, sorted, deterministic.

    Hosts that forward ``content[].text`` to the model pay for every
    character, and indentation added about half again to each response.
    """

    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
