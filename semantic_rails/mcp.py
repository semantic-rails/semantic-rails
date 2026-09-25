"""MCP adapter and tool definitions.

Exposes :class:`SemanticLayerMCPAdapter`, which presents governed
tools backed by a single ``Runtime``. Interface v1 has thirteen tools
(``capabilities``, ``catalog``, ``discover``, ``inspect``,
``build-options``, ``valid-values``, ``plan``, ``validate``,
``compile``, ``execute``, plus ``segment-validate`` /
``segment-explain`` / ``segment-preview``). Interface v2 has six:
``discover``, ``inspect``, ``valid-values``, ``plan``,
``execute(mode)`` and ``segment(action)``, served by the same
handlers. The transport — stdio vs HTTP — lives in
:mod:`semantic_rails.mcp_server`; this module is the protocol-agnostic
adapter.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from .ast import QUERY_INPUT_KEYS
from .catalog_service import resolve_catalog
from .diagnostics import enrich_object_not_found, exception_issue, semantic_issue
from .errors import SemanticLayerError
from .metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
    valid_values_payload,
)
from .metadata_parts.capabilities import capabilities_payload
from .planner import plan_payload
from .request_context import (
    RequestContext,
    context_from_policy_context,
    emit_audit_event,
    request_context_payload,
)
from .request_payload import (
    build_query_payload,
    without_policy_context,
)
from .request_payload import (
    clean_request_id as _clean_request_id,
)
from .request_payload import (
    coerce_bool as _coerce_bool,
)
from .runtime import Runtime

__all__ = [
    "JSON_OBJECT_SCHEMA",
    "MCP_DEFAULT_INTERFACE",
    "MCP_INTERFACE_ENV",
    "MCP_INTERFACE_VERSION",
    "MCP_INTERFACE_VERSIONS",
    "MCP_PROMPT_DEFINITIONS",
    "MCP_RESOURCE_DEFINITIONS",
    "MCP_SERVER_INSTRUCTIONS",
    "MCP_SERVER_INSTRUCTIONS_V2",
    "MCP_TOOL_DEFINITIONS",
    "POLICY_CONTEXT_SCHEMA",
    "PromptDefinition",
    "ResourceDefinition",
    "Runtime",
    "SemanticLayerError",
    "SemanticLayerMCPAdapter",
    "ToolDefinition",
    "build_options_payload",
    "catalog_payload",
    "context_from_policy_context",
    "create_optional_fastmcp_server",
    "discover_payload",
    "emit_audit_event",
    "enrich_object_not_found",
    "exception_issue",
    "inspect_payload",
    "json_text",
    "list_prompt_definitions",
    "list_resource_definitions",
    "list_tool_definitions",
    "plan_payload",
    "request_context_payload",
    "resolve_interface",
    "valid_values_payload",
]


MCP_INTERFACE_VERSION = "v1"
# Interface v1 is frozen in query_mcp.v1.json. v2 folds validate and compile
# into execute(mode) and the segment tools into segment(action), drops
# capabilities, catalog and build-options, and defaults every tool to its
# smallest response. An adapter built without an interface reads this
# environment variable, then falls back to the default.
MCP_INTERFACE_VERSIONS = ("v1", "v2")
MCP_DEFAULT_INTERFACE = "v1"
MCP_INTERFACE_ENV = "SEMANTIC_RAILS_MCP_INTERFACE"

# Default response verbosity for the MCP validate/compile/execute tools.
# Context-constrained agents drown in the ~90-100KB envelopes the runtime
# emits at its own 'compact' default (the HTTP v1 surface keeps that
# default — see runtime_parts.responses.resolve_verbosity). The MCP
# adapter defaults to 'minimal' instead: validate={ok,errors,warnings},
# compile adds rendered_sql, execute adds rows+row_count. An explicit
# 'verbosity' argument (outer envelope or inside `query`) always wins.
MCP_DEFAULT_QUERY_VERBOSITY = "minimal"

# Row cap for MCP execute: v2's default, and an opt-in in v1, which stays uncapped.
# Hosts warn about tool results over 10K tokens; 200 rows keeps a typical
# answer well under that. A larger result comes back with a truncation hint.
MCP_DEFAULT_MAX_ROWS = 200
# Execute asks the warehouse for up to this many rows (never past a
# limits.max_rows the query sets itself), so a truncated result can still
# report its total. Some adapters fetch the full result and clip it.
MCP_ROW_COUNT_CEILING = 10_000
# The largest max_rows an MCP caller may request.
MCP_MAX_ROWS_LIMIT = 100_000

_TOOL_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "semantic_rails_mcp_tool_request_context", default=None
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
    "objects by full id (measure.jaffle.revenue_usd, dimension.jaffle_store_name), never "
    "by label.\n"
    "\n"
    "To answer a question:\n"
    "1. discover(terms) ranks measures, metrics and dimensions for it. inspect(object_id)"
    " shows one object's card when you need its aggregations, values or time roles.\n"
    '2. plan(intent) drafts Query IR. Run best.query_ir only when status is "ok" and '
    "there are no warnings; otherwise why and warnings name what the draft misses, so fix"
    " the Query IR or ask the user. out_of_scope or unrealizable means the package can't "
    "answer.\n"
    "3. execute(query) validates, compiles and runs Query IR. Omitted max_rows keeps the "
    "query's limit; set max_rows to cap rows and receive a truncation hint when needed. "
    "validate and compile are optional dry runs.\n"
    "\n"
    'Query IR: select measures or metrics, group_by dimension ids, where filters (op "in"'
    " for several values), and time {temporal_role, grain, start, end}. A window without "
    "a grain groups by the raw timestamp. The validate tool lists expression shapes; "
    "capabilities has examples.\n"
    "\n"
    "Other tools: catalog lists every id; valid-values lists a dimension's values; "
    "build-options suggests the next choice for a guided builder; segment-validate, "
    "segment-explain and segment-preview work with package-authored segments.\n"
    "For small resource reads, use capabilities/summary and catalog/index.\n"
    "\n"
    'MCP validate, compile and execute default to minimal; pass verbosity "compact" or "full" '
    'for more. plan defaults to detail "best"; detail "query" is shorter. Errors carry '
    "recovery_hints and closest_matches; follow them "
    "before retrying. For local testing, any tool accepts policy_context {environment, "
    "audience, roles}; hosted servers set it for you."
)

MCP_SERVER_INSTRUCTIONS_V2 = (
    "Semantic Rails answers analytics questions from a governed semantic layer. Refer to "
    "objects by full id (measure.jaffle.revenue_usd, dimension.jaffle_store_name), never "
    "by label.\n"
    "\n"
    "To answer a question:\n"
    "1. discover(terms) ranks measures, metrics and dimensions for it; empty terms list "
    "every id. inspect(object_id) shows one object's card when you need its aggregations, "
    "values or time roles. valid-values(dimension_id) lists a dimension's values.\n"
    "2. plan(intent) drafts Query IR; draft with plan rather than writing Query IR from "
    'scratch. Run best.query_ir only when status is "ok" and there are no warnings; '
    "otherwise why and warnings name what the draft misses, so fix the Query IR or ask the "
    "user. out_of_scope or unrealizable means the package can't answer.\n"
    "3. execute(query) validates, compiles and runs the Query IR and returns at most "
    f"max_rows rows (default {MCP_DEFAULT_MAX_ROWS}); a capped result reports truncated and "
    'total_row_count. mode "validate" only checks the query; mode "sql" also returns its '
    "SQL.\n"
    "\n"
    'Query IR: select measures or metrics, group_by dimension ids, where filters (op "in"'
    " for several values), and time {temporal_role, grain, start, end}, where end is "
    "exclusive. A window without a grain groups by the raw timestamp. The execute tool "
    "schema lists expression shapes.\n"
    "\n"
    "segment(segment_id, action) validates, explains or previews a package-authored "
    "segment.\n"
    "\n"
    'Every tool returns its smallest response by default (verbosity "minimal", plan detail '
    '"query"); pass verbosity "compact" or "full", or detail "best", for more. Errors carry '
    "recovery_hints and closest_matches; follow them before retrying. For local testing, "
    "any tool accepts policy_context {environment, audience, roles}; hosted servers set it "
    "for you."
)

POLICY_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Optional visibility/access policy context.",
    "properties": {
        "environment": {"type": "string"},
        "audience": {"type": "string"},
    },
    "additionalProperties": True,
}

QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Semantic Layer Query IR. Full spec at schemas/query_ir.v1.json; "
        "narrative at docs/QUERY_IR_SCHEMA.md. Unknown keys rejected as "
        "INVALID_QUERY (offenders under details.unsupported_keys)."
    ),
    "additionalProperties": True,
    "properties": {
        "time": {
            "type": ["object", "null"],
            "description": "Time anchor: temporal_role + grain + start|end|range + fill + calendar_id. Omit entirely for an all-time scalar aggregate.",
            "additionalProperties": True,
            "properties": {
                "temporal_role": {
                    "type": "string",
                    "description": "Clock id, e.g. 'temporal_role.<x>'. Must match the metric's anchor; mismatch fails as INVALID_TEMPORAL_BINDING.",
                },
                "grain": {
                    "type": "string",
                    "enum": ["", "day", "week", "month", "quarter", "year", "hour", "minute"],
                    "description": "Bucket size. Required when fill=true or for inline prior_period.",
                },
                "start": {"type": ["string", "null"], "description": "ISO-8601."},
                "end": {"type": ["string", "null"], "description": "ISO-8601."},
                "range": {
                    "type": "object",
                    "description": "Relative window {last: {unit, value}}: window ends at the floor of now (start of current period) — covers the last N completed periods, NOT a rolling-to-today window. Mutually exclusive with start/end; string shorthand rejected as USE_OBJECT_SHAPE.",
                    "additionalProperties": True,
                    "properties": {
                        "last": {
                            "type": "object",
                            "description": "{unit, value} — e.g. {unit: 'day', value: 90}.",
                            "additionalProperties": False,
                            "required": ["unit", "value"],
                            "properties": {
                                "unit": {
                                    "type": "string",
                                    "enum": [
                                        "minute",
                                        "hour",
                                        "day",
                                        "week",
                                        "month",
                                        "quarter",
                                        "year",
                                    ],
                                },
                                "value": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                },
                "fill": {
                    "type": "boolean",
                    "default": False,
                    "description": "Dense calendar spine for grain buckets (0 / NULL fill). Requires grain; otherwise fails as INVALID_QUERY.",
                },
                "calendar_id": {
                    "type": "string",
                    "default": "default",
                    "description": "Calendar for dense spine (Gregorian or authored fiscal).",
                },
            },
        },
    },
}

# Slim Query-IR schema for tools/list dedupe. The full QUERY_SCHEMA
# (with the detailed time-block spec) used to be embedded verbatim in
# all eight IR-accepting tools, costing ~1.8KB x 8 on every tools/list.
# It now ships once — on 'validate', the loop's gate — and the other
# tools point there. Runtime acceptance is unchanged: both schemas are
# `additionalProperties: true` documentation hints, not validators.
QUERY_SCHEMA_SLIM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "description": (
        "Semantic Layer Query IR (JSON object). IR + time-block shape: "
        "see the 'validate' tool schema, or schemas/query_ir.v1.json."
    ),
}

VERBOSITY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["minimal", "compact", "full"],
    "default": "minimal",
    "description": (
        "Response detail. 'minimal' (default)={ok,errors,warnings}, "
        "+rendered_sql on compile, +rows/row_count on execute — a few KB. "
        "'compact' adds rendered_sql/sql_plan/explain/normalized query. "
        "'full' = legacy maximal envelope (~100KB)."
    ),
}

SQL_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["audit", "compact", "debug", "off"],
    "default": "audit",
    "description": (
        "SQL rendering: 'audit'(default)/'compact'/'debug' format rendered_sql; "
        "'off' suppresses rendered_sql + sql_plan entirely."
    ),
}

ROW_FORMAT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["records", "columns"],
    "default": "records",
    "description": (
        "Execute row shape. 'records' (default)=rows as objects; "
        "'columns'=columns plus array rows, avoiding repeated field names."
    ),
}

MCP_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "description": (
        "Semantic Rails MCP result envelope. Tool-specific fields are additive; "
        "the status, issue, request, and package fields are stable v1."
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
        "api_version": {"type": "string", "const": MCP_INTERFACE_VERSION},
        "request_id": {"type": "string"},
        "package_id": {"type": "string"},
        "warnings": {"type": "array", "items": {"$ref": "#/$defs/issue"}},
        "errors": {"type": "array", "items": {"$ref": "#/$defs/issue"}},
        "error": {"oneOf": [{"$ref": "#/$defs/issue"}, {"type": "null"}]},
        "recovery_hints": {"type": "array", "items": {"type": "object"}},
        "request_context": {"type": "object"},
        "timing_ms": {"type": "number", "minimum": 0},
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
    "description": "Semantic Rails MCP v1 result envelope.",
    "required": ["ok", "status"],
    "properties": {
        "ok": {"type": "boolean"},
        "status": {"type": "string"},
        "api_version": {"type": "string", "const": MCP_INTERFACE_VERSION},
        "request_id": {"type": "string"},
        "package_id": {"type": "string"},
        "warnings": {"type": "array"},
        "errors": {"type": "array"},
    },
    "additionalProperties": True,
}


def _result_schema(interface: str) -> dict[str, Any]:
    """The result envelope of ``interface``: the v1 envelope with its version."""

    schema = copy.deepcopy(MCP_RESULT_SCHEMA)
    schema["description"] = schema["description"].replace("stable v1", f"stable {interface}")
    schema["properties"]["api_version"]["const"] = interface
    return schema


def _result_schema_slim(interface: str) -> dict[str, Any]:
    schema = copy.deepcopy(MCP_RESULT_SCHEMA_SLIM)
    schema["description"] = f"Semantic Rails MCP {interface} result envelope."
    schema["properties"]["api_version"]["const"] = interface
    return schema


def _tool_annotations(name: str) -> dict[str, Any]:
    """Return MCP-standard behavioral hints for a query tool.

    Query execution and live value/segment previews can contact an external
    warehouse, hence ``openWorldHint``. They remain read-only and
    non-destructive: the engine only compiles and executes SELECT-shaped SQL.
    """

    open_world = name in {"execute", "valid-values", "segment-preview", "segment"}
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
    # These optional fields are part of the published v1 tool schemas. Keep
    # them advertised even though the workflow explains them only once.
    schema_properties = copy.deepcopy(dict(properties))
    schema_properties.setdefault("request_id", {"type": "string"})
    schema_properties.setdefault("policy_context", copy.deepcopy(POLICY_CONTEXT_SCHEMA))
    schema: dict[str, Any] = {
        "type": "object",
        "properties": schema_properties,
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


SELECT_EXPRESSION_SHAPES_HELP = (
    "IR: select[]={expression,as}, group_by[]=[<dim>,...] (bare ids), "
    "where[]={field,op,value}, order_by[]={field,direction}. "
    "select.expression: {aggregation, measure} | {metric} | "
    "{kind:prior_period|rolling|cumulative|ratio|conversion|aggregate_if|between|...}. "
    "Call 'capabilities' first for runnable example IR per kind "
    "(capabilities.expression_shapes[].example)."
)


# Omitted segment verbosity preserves the v1 whole response. "compact" is
# accepted by the adapter as an alias for "full".
SEGMENT_VERBOSITY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["minimal", "full"],
    "default": "full",
}

TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="capabilities",
        description=(
            "Semantic Rails MCP question-answering entrypoint: answer with "
            "discover, plan and execute (validate and compile are optional dry "
            "runs); use catalog to list ids. Returns what this package supports "
            "and doesn't: rolling windows, prior-period offsets, metric "
            "predicates, scoped aggregates, conversion metrics, with runnable "
            "expression_shapes examples. Gotcha: capabilities are package-scoped;"
            " call again after switching packages."
        ),
        input_schema=_schema({}),
    ),
    ToolDefinition(
        name="catalog",
        description=(
            "List the governed objects in the package: measures, metrics, "
            "dimensions, segments and entities. Use it first to see what exists; "
            "prefer 'discover' to look up terms. Gotcha: the default verbosity "
            "'summary' returns flat id lists; 'compact' adds row metadata (capped"
            " at 200 per kind) and 'full' is uncapped with alias_index (large)."
        ),
        input_schema=_schema(
            {
                "view": {"type": "string", "default": "summary"},
                "verbosity": {
                    "type": "string",
                    "default": "summary",
                    "enum": ["summary", "minimal", "compact", "full"],
                },
                "kind": {"type": "string", "description": "Optional object kind filter."},
                "search": {"type": "string", "description": "Optional substring filter."},
                "entity": {"type": "string", "description": "Optional root entity context."},
            }
        ),
    ),
    ToolDefinition(
        name="discover",
        description=(
            "Rank semantic objects against business terms (e.g. 'revenue', "
            "'aov by store'). Returns measures, metrics, dimensions, and "
            "entities, up to 'limit' per kind. "
            "Default: full cards; verbosity='minimal': slim cards without "
            "match_reasons or starter patches. Gotcha: nonsense terms return "
            "'out_of_scope' or 'low_relevance' with empty buckets; branch "
            "before using a candidate."
        ),
        input_schema=_schema(
            {
                "terms": {"type": "string"},
                "kinds": {
                    "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                    "description": "Object kinds to rank, such as measure or metric. Default: all.",
                },
                "query": QUERY_SCHEMA_SLIM,
                "stage": {
                    "type": "string",
                    "description": (
                        "Builder stage that tunes ranking: initial, post_measure, "
                        "post_dimension or comparison. Inferred when omitted."
                    ),
                },
                "verbosity": {
                    "type": "string",
                    "enum": ["minimal", "compact", "full"],
                    "default": "compact",
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1},
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="inspect",
        description=(
            "Return one object's card: label, description, aggregations or "
            "values, temporal roles, related objects, policy. Default: full "
            "card; verbosity='minimal' drops duplicates. Gotcha: 'object_id' "
            "must be a full id like 'measure.jaffle.revenue_usd', not a "
            "label — use 'discover' first if you only have a phrase."
        ),
        input_schema=_schema(
            {
                "object_id": {"type": "string"},
                "query": QUERY_SCHEMA_SLIM,
                "verbosity": {
                    "type": "string",
                    "enum": ["minimal", "compact", "full"],
                    "default": "compact",
                },
            },
            required=["object_id"],
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="build-options",
        description=(
            "Return ranked next choices for a guided query builder: given partial"
            " Query IR, which dimension, filter or time range to add next. Use it"
            " to compose a query step by step instead of from one intent "
            "('plan'). Gotcha: pass the partial 'query' you've assembled; empty "
            "input returns initial-stage options."
        ),
        input_schema=_schema(
            {
                "query": QUERY_SCHEMA_SLIM,
                "focus_terms": {"type": "string", "description": "Terms to rank the options by."},
                "focus_object_id": {
                    "type": "string",
                    "description": "An object id to rank the options around.",
                },
                "step": {
                    "type": "string",
                    "description": (
                        "Step to rank options for: measure, group_by, filter_dimension, time "
                        "or review. Inferred from 'query' when omitted."
                    ),
                },
                "stage": {
                    "type": "string",
                    "description": (
                        "Builder stage that tunes ranking: initial, post_measure, "
                        "post_dimension or comparison. Inferred when omitted."
                    ),
                },
                "verbosity": {"type": "string", "default": "compact"},
                "include_blocked": {
                    "type": "boolean",
                    "default": True,
                    "description": "Also list options this query can't use, with the reason.",
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1},
            },
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
            "'dimension.jaffle_store_name'; allow_live_query costs a warehouse "
            "round-trip, so use it only when no domain is declared."
        ),
        input_schema=_schema(
            {
                "dimension_id": {"type": "string"},
                "query": QUERY_SCHEMA_SLIM,
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
            "Single public intent-planning surface: natural-language intent "
            "→ one best Query IR. Returns 'status' ('ok' | 'low_confidence' | "
            "'unrealizable' | 'out_of_scope'), 'best.query_ir', and 'why' or "
            "'warnings' naming any part of the question the draft doesn't "
            "honor. 'status=ok' has already paid validation cost, so agents "
            "may pass 'best.query_ir' to 'execute'. By default the v1 response "
            "includes intent_ir, trace and next steps; detail='query' opts "
            "into a compact response. 'full' adds alternatives and "
            "blocked drafts; 'debug' adds compose_hints. Gotcha: read "
            "'status' and 'warnings' before running 'best.query_ir'."
        ),
        input_schema=_schema(
            {
                "intent": {"type": "string"},
                "query": QUERY_SCHEMA_SLIM,
                "detail": {
                    "type": "string",
                    "enum": ["query", "best", "full", "debug"],
                    "default": "best",
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
    ),
    ToolDefinition(
        name="validate",
        description=(
            "Check Query IR without running it: errors, warnings and repair "
            "hints. Optional: 'execute' runs the same checks first. Gotcha: "
            "'query' must be a JSON object, not a string; wrap it as {query: "
            "{...}}. verbosity='compact' or 'full' adds the normalized query, "
            "policy effects and plans. IR: select[]={expression,as}, "
            "group_by[]=[<dim>,...] (bare ids), where[]={field,op,value}, "
            "order_by[]={field,direction}. select.expression: {aggregation, "
            "measure} | {metric} | "
            "{kind:prior_period|rolling|cumulative|ratio|conversion|aggregate_if|between|...}."
            " Runnable examples per kind: "
            "capabilities.expression_shapes[].example."
        ),
        input_schema=_schema(
            {
                # Full IR schema ships once across tools/list — here, on
                # the loop's gate. The other IR tools embed QUERY_SCHEMA_SLIM
                # and point back to this one.
                "query": QUERY_SCHEMA,
                "verbosity": VERBOSITY_SCHEMA,
                "sql_profile": SQL_PROFILE_SCHEMA,
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="compile",
        description=(
            "Compile Query IR into SQL without running it. Use it before "
            "'execute' when you want to show or review the SQL; 'execute' "
            "compiles on its own. Gotcha: invalid Query IR fails here too, and "
            "'validate' is the cheaper check. verbosity='compact' or 'full' adds "
            "sql_plan, explain and logical plans. IR shape: see the 'validate' "
            "tool."
        ),
        input_schema=_schema(
            {
                "query": QUERY_SCHEMA_SLIM,
                "verbosity": VERBOSITY_SCHEMA,
                "sql_profile": SQL_PROFILE_SCHEMA,
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="execute",
        description=(
            "Validate, compile and run Query IR against the warehouse after "
            "plan, or once Query IR is ready. Omitted "
            "max_rows keeps the caller's query limit; set max_rows to cap output. "
            "A capped result reports truncated and total_row_count. Gotcha: "
            "this tool incurs warehouse cost and latency. row_format='columns' "
            "is compact; verbosity='compact' or 'full' adds SQL and plans. "
            "IR shape: see the 'validate' tool."
        ),
        input_schema=_schema(
            {
                "query": QUERY_SCHEMA_SLIM,
                "verbosity": VERBOSITY_SCHEMA,
                "sql_profile": SQL_PROFILE_SCHEMA,
                "row_format": ROW_FORMAT_SCHEMA,
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MCP_MAX_ROWS_LIMIT,
                    "description": (
                        "Optional row cap; omitted v1 calls keep the caller's query limit. "
                        "A larger result sets truncated=true and total_row_count."
                    ),
                },
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="segment-validate",
        description=(
            "Validate a package-authored segment and the Query IR derived from "
            "it. Use it first in the segment workflow, then 'segment-explain' and"
            " 'segment-preview'. Gotcha: 'segment_id' must be a full id like "
            "'segment.jaffle.high_value_customers'; 'catalog' lists segments."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "verbosity": SEGMENT_VERBOSITY_SCHEMA,
            },
            required=["segment_id"],
        ),
    ),
    ToolDefinition(
        name="segment-explain",
        description=(
            "Explain a package-authored segment: its definition, derived Query IR"
            " and SQL. Use it after 'segment-validate', before previewing rows. "
            "Gotcha: governed segments only; for an ad-hoc cohort, write Query IR"
            " with a 'where' clause and call 'compile'."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "verbosity": SEGMENT_VERBOSITY_SCHEMA,
            },
            required=["segment_id"],
        ),
    ),
    ToolDefinition(
        name="segment-preview",
        description=(
            "Preview a package-authored segment's members: sample rows and the "
            "total member count. Use it after 'segment-explain', to show rows "
            "to the user. Gotcha: it queries the warehouse (cost and latency); "
            "'limit' caps the sample rows."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1},
                "verbosity": SEGMENT_VERBOSITY_SCHEMA,
            },
            required=["segment_id"],
        ),
    ),
)

RESOURCE_DEFINITIONS: tuple[ResourceDefinition, ...] = (
    ResourceDefinition(
        uri="semantic-rails://capabilities",
        name="capabilities",
        description=(
            "V1 capabilities with complete tool definitions, resources and prompts. Large; "
            "use capabilities/summary for names and titles."
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
            "V1 catalog summary with descriptive rows and counts. Large; use catalog/index "
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
            "Guide an agent from a question to rows: discover, inspect, plan (or build-options "
            "and valid-values), then execute."
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
        description="Review existing Query IR with validate and compile (its response includes the `explain` payload) before execution.",
        arguments=(
            {"name": "query_json", "description": "Query IR JSON to review.", "required": True},
        ),
    ),
    PromptDefinition(
        name="semantic-rails-segment-workflow",
        description="Validate, segment-explain, and segment-preview a package-authored segment.",
        arguments=(
            {
                "name": "segment_id",
                "description": "Segment id such as segment.jaffle.high_value_customers.",
                "required": True,
            },
        ),
    ),
)

# Interface v2 reuses the v1 definitions where a tool is unchanged, so the two
# can't drift apart; only the result envelope names v2.
_V1_TOOLS = {definition.name: definition for definition in TOOL_DEFINITIONS}
_EXECUTE_MODES = ("run", "validate", "sql")
_SEGMENT_ACTIONS = ("validate", "explain", "preview")


def _v2_tool(definition: ToolDefinition, **changes: Any) -> ToolDefinition:
    return replace(definition, output_schema=_result_schema_slim("v2"), **changes)


def _v2_input(name: str, **defaults: Any) -> dict[str, Any]:
    """A v1 tool's input schema whose Query IR points at v2's execute, with v2 defaults."""

    schema = copy.deepcopy(dict(_V1_TOOLS[name].input_schema))
    schema["properties"]["query"]["description"] = schema["properties"]["query"][
        "description"
    ].replace("'validate' tool", "'execute' tool")
    for field, default in defaults.items():
        schema["properties"][field]["default"] = default
    return schema


_V2_QUERY_SCHEMA = copy.deepcopy(QUERY_SCHEMA)
_V2_QUERY_SCHEMA["properties"]["time"]["properties"]["end"]["description"] = (
    "ISO-8601, exclusive: March 2017 is start 2017-03-01, end 2017-04-01."
)

V2_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    _v2_tool(
        _V1_TOOLS["discover"],
        description=(
            "Rank semantic objects against business terms (e.g. 'revenue', 'aov by store'). "
            "Returns measures, metrics, dimensions, and entities, up to 'limit' per kind; "
            "empty terms list every id per kind instead. Default: slim cards (id, label, "
            "description, score); verbosity='compact' adds match_reasons and starter patches. "
            "Gotcha: nonsense terms return 'out_of_scope' or 'low_relevance' with empty "
            "buckets; branch before using a candidate."
        ),
        # v2 defaults every tool to its smallest response.
        input_schema=_v2_input("discover", verbosity="minimal"),
    ),
    _v2_tool(
        _V1_TOOLS["inspect"],
        description=(
            "Return one object's card: label, description, aggregations or values, temporal "
            "roles, related objects, policy. Default: the card without duplicate fields; "
            "verbosity='compact' returns the full card. Gotcha: 'object_id' must be a full "
            "id like 'measure.jaffle.revenue_usd', not a label — use 'discover' first if you "
            "only have a phrase."
        ),
        input_schema=_v2_input("inspect", verbosity="minimal"),
    ),
    _v2_tool(_V1_TOOLS["valid-values"], input_schema=_v2_input("valid-values")),
    _v2_tool(
        _V1_TOOLS["plan"],
        description=(
            "Draft one best Query IR from a natural-language intent. Returns 'status' ('ok' | "
            "'low_confidence' | 'unrealizable' | 'out_of_scope'), 'best.query_ir', and 'why' "
            "or 'warnings' naming any part of the question the draft doesn't honor. "
            "detail='query' (default) is compact; 'best' adds intent_ir, trace and next "
            "steps; 'full' adds alternatives and blocked drafts; 'debug' adds compose_hints. "
            "Gotcha: pass 'best.query_ir' to 'execute' only when status is 'ok' and there are "
            "no warnings."
        ),
        input_schema=_v2_input("plan", detail="query"),
    ),
    ToolDefinition(
        name="execute",
        description=(
            "Validate, compile and run Query IR against the warehouse: the best.query_ir that "
            "'plan' drafted, or Query IR you fixed from it. Returns at most "
            "max_rows rows; a capped result reports truncated and total_row_count. "
            "mode='validate' only checks the query (errors, warnings, repair hints); "
            "mode='sql' also returns rendered_sql; neither runs it. Gotcha: 'query' must be "
            "a JSON object, and mode 'run' costs warehouse time. row_format='columns' is "
            "compact. IR: select[]={expression,as}, group_by[]=[<dim>,...] (bare ids), "
            "where[]={field,op,value}, order_by[]={field,direction}. select.expression: "
            "{aggregation, measure} | {metric} | "
            "{kind:prior_period|rolling|cumulative|ratio|conversion|aggregate_if|between|...}."
        ),
        input_schema=_schema(
            {
                "query": _V2_QUERY_SCHEMA,
                "mode": {
                    "type": "string",
                    "enum": list(_EXECUTE_MODES),
                    "default": "run",
                    "description": (
                        "'run' returns rows; 'validate' only checks the query; 'sql' also "
                        "returns rendered_sql. Only 'run' queries the warehouse."
                    ),
                },
                "verbosity": {
                    **VERBOSITY_SCHEMA,
                    "description": (
                        "Response detail. 'minimal' (default)={ok,errors,warnings}, plus "
                        "rendered_sql in mode 'sql' and rows in mode 'run'. 'compact' adds "
                        "sql_plan, explain and the normalized query; 'full' is the maximal "
                        "envelope (~100KB)."
                    ),
                },
                "sql_profile": SQL_PROFILE_SCHEMA,
                "row_format": ROW_FORMAT_SCHEMA,
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MCP_MAX_ROWS_LIMIT,
                    "default": MCP_DEFAULT_MAX_ROWS,
                    "description": (
                        "Rows to return in mode 'run'. A larger result sets truncated=true "
                        "and total_row_count."
                    ),
                },
            },
            additional_properties=True,
        ),
        output_schema=_result_schema_slim("v2"),
    ),
    ToolDefinition(
        name="segment",
        description=(
            "Work with a package-authored segment: action='validate' checks it and the Query IR"
            " derived from it, 'explain' adds the SQL, and 'preview' returns sample member rows"
            " and the total member count. Default verbosity='minimal' leaves out compiler "
            "plans; 'full' returns them. Gotcha: 'segment_id' must be a full id like "
            "'segment.jaffle.high_value_customers' (discover with empty terms lists them); "
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
                "verbosity": {**SEGMENT_VERBOSITY_SCHEMA, "default": "minimal"},
            },
            required=["segment_id", "action"],
        ),
        output_schema=_result_schema_slim("v2"),
    ),
)

V2_RESOURCE_DEFINITIONS: tuple[ResourceDefinition, ...] = tuple(
    replace(
        definition,
        description=(
            "Interface v2 tool definitions, resources and prompts. Large; use "
            "capabilities/summary for names and titles."
        ),
    )
    if definition.name == "capabilities"
    else definition
    for definition in RESOURCE_DEFINITIONS
)

# v2 keeps the v1 prompt names; each (description, text) names v2 tools.
_V2_PROMPTS = {
    "semantic-rails-query-builder": (
        "Guide an agent from a question to rows: discover, inspect, plan (and valid-values), "
        "then execute.",
        "Use the Semantic Layer MCP tools against package '{package_id}' to answer: {intent}\n"
        "Start with discover and inspect the best governed objects. Draft Query IR with plan, "
        "check filter values with valid-values, then run it with execute, which validates and "
        "compiles first. execute modes 'validate' and 'sql' are optional dry runs; "
        "verbosity 'compact' adds the explain payload.",
    ),
    "semantic-rails-query-review": (
        "Review existing Query IR with execute mode 'validate', then mode 'sql', before running it.",
        "Review this Semantic Layer Query IR with execute mode 'validate', then mode 'sql' "
        "(verbosity 'compact' adds the explain payload). Only run it with mode 'run' after "
        "validation succeeds and the user needs result rows.\n\n{query_json}",
    ),
    "semantic-rails-segment-workflow": (
        "Validate, explain and preview a package-authored segment with the segment tool.",
        "Use the segment tool with action 'validate', 'explain' and 'preview' for segment "
        "'{segment_id}'. Report validation errors first, then summarize the derived query and "
        "member preview.",
    ),
}
V2_PROMPT_DEFINITIONS: tuple[PromptDefinition, ...] = tuple(
    replace(definition, description=_V2_PROMPTS[definition.name][0])
    for definition in PROMPT_DEFINITIONS
)
# What a v2 caller should use instead of a v1-only tool.
_V2_REPLACEMENTS = {
    "validate": "execute with mode 'validate'",
    "compile": "execute with mode 'sql'",
    "segment-validate": "segment with action 'validate'",
    "segment-explain": "segment with action 'explain'",
    "segment-preview": "segment with action 'preview'",
    "catalog": "discover with empty terms",
    "capabilities": f"interface v1 ({MCP_INTERFACE_ENV}=v1)",
    "build-options": f"plan, or interface v1 ({MCP_INTERFACE_ENV}=v1)",
}

MCP_TOOL_DEFINITIONS = tuple(definition.to_dict() for definition in TOOL_DEFINITIONS)
MCP_RESOURCE_DEFINITIONS = tuple(definition.to_dict() for definition in RESOURCE_DEFINITIONS)
MCP_PROMPT_DEFINITIONS = tuple(definition.to_dict() for definition in PROMPT_DEFINITIONS)


def list_tool_definitions(interface: str = MCP_INTERFACE_VERSION) -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in _INTERFACES[interface].rules.tools]


def list_resource_definitions(interface: str = MCP_INTERFACE_VERSION) -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in _INTERFACES[interface].resources]


def list_prompt_definitions(interface: str = MCP_INTERFACE_VERSION) -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in _INTERFACES[interface].prompts]


_CATALOG_VERBOSITIES: frozenset[str] = frozenset({"summary", "minimal", "compact", "full"})


_CATALOG_KIND_FILTERS: frozenset[str] = frozenset(
    {
        "entity",
        "dimension",
        "measure",
        "segment",
        "temporal_role",
        "relationship",
        "value_domain",
        "metric",
    }
)


def _argument_error(message: str, *, field: str, value: Any | None = None) -> SemanticLayerError:
    details: dict[str, Any] = {"field": field}
    if value is not None:
        details["argument_type"] = type(value).__name__
    return SemanticLayerError("INVALID_MCP_ARGUMENTS", message, details=details)


def _tool_required_properties(
    tool_name: str, tools: Sequence[ToolDefinition]
) -> tuple[list[str], list[str]]:
    """Return (required, known) properties from the tool's input_schema."""
    for definition in tools:
        if definition.name != tool_name:
            continue
        schema = dict(definition.input_schema or {})
        required = list(schema.get("required") or [])
        known = list((schema.get("properties") or {}).keys())
        return required, known
    return [], []


def _tool_known_args(tool_name: str, tools: Sequence[ToolDefinition]) -> frozenset[str]:
    """Source of truth for the legitimate argument keys per tool.

    Combines the tool's ``input_schema.properties`` keys with canonical
    :data:`semantic_rails.ast.QUERY_INPUT_KEYS` for tools that accept top-level
    Query-IR passthrough (``validate``, ``compile``, ``execute``). Callers can
    skip the ``query`` wrapper without tripping the unknown-arg check. Query
    IR's own additional-keys gate (in ``ast.py``) handles unknown IR keys
    separately — no double-validation here.
    """
    for definition in tools:
        if definition.name != tool_name:
            continue
        schema = dict(definition.input_schema or {})
        known = set((schema.get("properties") or {}).keys())
        if tool_name in {"validate", "compile", "execute"}:
            known.update(QUERY_INPUT_KEYS)
        return frozenset(known)
    return frozenset()


@dataclass(frozen=True)
class _ArgumentRules:
    """One interface's argument tables, derived from its tool schemas.

    Tool schemas own unknown-argument behavior: a closed schema rejects
    unknown keys, an open one warns and ignores them. Query-IR passthrough
    keys come from the parser, so adding an IR field needs no second
    transport list.
    """

    tools: tuple[ToolDefinition, ...]
    known: Mapping[str, frozenset[str]]
    strict: frozenset[str]
    warning_codes: Mapping[str, str]


def _argument_rules(tools: tuple[ToolDefinition, ...]) -> _ArgumentRules:
    strict = frozenset(
        definition.name
        for definition in tools
        if not definition.input_schema.get("additionalProperties", True)
    )
    return _ArgumentRules(
        tools=tools,
        known={definition.name: _tool_known_args(definition.name, tools) for definition in tools},
        strict=strict,
        warning_codes={
            definition.name: f"{definition.name.replace('-', '_').upper()}_UNKNOWN_ARG"
            for definition in tools
            if definition.name not in strict
        },
    )


_V1_RULES = _argument_rules(TOOL_DEFINITIONS)
_TOOL_KNOWN_ARGS: Mapping[str, frozenset[str]] = _V1_RULES.known
_STRICT_REJECT_TOOLS: frozenset[str] = _V1_RULES.strict
_WARN_AND_IGNORE_TOOLS: frozenset[str] = frozenset(_V1_RULES.warning_codes)
_UNKNOWN_ARG_WARNING_CODE: Mapping[str, str] = _V1_RULES.warning_codes

_ROW_FORMATS: frozenset[str] = frozenset({"records", "columns"})


@dataclass(frozen=True)
class _Interface:
    version: str
    rules: _ArgumentRules
    resources: tuple[ResourceDefinition, ...]
    prompts: tuple[PromptDefinition, ...]
    instructions: str


_INTERFACES: dict[str, _Interface] = {
    "v1": _Interface(
        "v1", _V1_RULES, RESOURCE_DEFINITIONS, PROMPT_DEFINITIONS, MCP_SERVER_INSTRUCTIONS
    ),
    "v2": _Interface(
        "v2",
        _argument_rules(V2_TOOL_DEFINITIONS),
        V2_RESOURCE_DEFINITIONS,
        V2_PROMPT_DEFINITIONS,
        MCP_SERVER_INSTRUCTIONS_V2,
    ),
}


def resolve_interface(interface: str | None = None) -> str:
    """Return the interface to serve: ``interface``, else the environment, else the default."""

    raw = interface if interface is not None else os.environ.get(MCP_INTERFACE_ENV, "")
    chosen = str(raw or "").strip().lower() or MCP_DEFAULT_INTERFACE
    if chosen not in _INTERFACES:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unknown MCP interface {raw!r}; choose one of {', '.join(MCP_INTERFACE_VERSIONS)} "
            f"(argument or {MCP_INTERFACE_ENV}).",
            details={"interface": str(raw), "valid_values": list(MCP_INTERFACE_VERSIONS)},
        )
    return chosen


def _unknown_arg_keys(
    *, tool_name: str, arguments: Mapping[str, Any], rules: _ArgumentRules
) -> list[str]:
    """Return the sorted list of unknown argument keys for ``tool_name``.

    Consults the interface's known arguments regardless of the schema's
    ``additionalProperties`` flag — the flag is a hint about reaction
    policy, not a gate on the check.
    """
    known = rules.known.get(tool_name)
    if known is None:
        return []
    return sorted(key for key in (arguments or {}) if key not in known)


def _unknown_argument_error(
    *, tool_name: str, arguments: Mapping[str, Any], rules: _ArgumentRules
) -> SemanticLayerError | None:
    """Reject unknown tool arguments on strict-reject tools.

    Strict tools (capabilities, catalog, segment-*) raise
    ``INVALID_MCP_ARGUMENTS`` on any unknown key. Warn-and-ignore tools
    are handled separately via :func:`_unknown_argument_warnings` so
    ``policy_context`` and top-level IR passthrough still work.
    """
    from difflib import get_close_matches

    if tool_name not in rules.strict:
        return None
    unknown = _unknown_arg_keys(tool_name=tool_name, arguments=arguments, rules=rules)
    if not unknown:
        return None
    known = rules.known.get(tool_name, frozenset())
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
    *, tool_name: str, arguments: Mapping[str, Any], rules: _ArgumentRules
) -> list[dict[str, Any]]:
    """Emit one warning per unknown argument on warn-and-ignore tools.

    Returns ``[]`` for strict-reject tools (handled by
    :func:`_unknown_argument_error`), unknown tool names, or when every
    received key is legitimate. The warning shape matches the existing
    ``DISCOVER_UNKNOWN_ARG`` / ``BUILD_OPTIONS_UNKNOWN_ARG`` pattern so
    downstream consumers can branch on ``code`` alone.
    """
    from difflib import get_close_matches

    if tool_name not in rules.warning_codes:
        return []
    unknown = _unknown_arg_keys(tool_name=tool_name, arguments=arguments, rules=rules)
    if not unknown:
        return []
    known = rules.known.get(tool_name, frozenset())
    code = rules.warning_codes.get(tool_name, "MCP_UNKNOWN_ARG")
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
    *, tool_name: str, arguments: Mapping[str, Any], rules: _ArgumentRules
) -> SemanticLayerError | None:
    """Reject non-string values on string-typed required args (object_id,
    segment_id, dimension_id, intent). Returning a clean
    INVALID_MCP_ARGUMENTS here beats letting the handler silently coerce
    ``inspect({object_id: 12345})`` into the misleading
    ``OBJECT_NOT_FOUND: Unknown object '12345'``.
    """
    required, _known = _tool_required_properties(tool_name, rules.tools)
    payload = dict(arguments or {})
    # Per tool input_schema, every required arg on the existing tools is
    # typed `string`. If new non-string required args appear later, this
    # check should consult the per-field type.
    for field_name in required:
        if field_name not in payload:
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
    *, tool_name: str, arguments: Mapping[str, Any], rules: _ArgumentRules
) -> SemanticLayerError | None:
    """Return a structured INVALID_MCP_ARGUMENTS error when a required
    tool argument is missing or blank — preferred to letting the
    handler treat ``""`` as a real value and emit a misleading
    ``OBJECT_NOT_FOUND`` for the empty string.
    """
    from difflib import get_close_matches

    required, known = _tool_required_properties(tool_name, rules.tools)
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


def _raw_policy_context_payload(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw_payload = dict(payload or {})
    policy_context: dict[str, Any] = {}
    query = raw_payload.get("query")
    if isinstance(query, Mapping):
        policy_context.update(
            _object_argument(query.get("policy_context"), field="query.policy_context")
        )
    policy_context.update(
        _object_argument(raw_payload.get("policy_context"), field="policy_context")
    )
    return policy_context


def _policy_context_payload(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    resolved = _TOOL_REQUEST_CONTEXT.get()
    if resolved is not None:
        return resolved.to_policy_context()
    raw_payload = dict(payload or {})
    return context_from_policy_context(
        _raw_policy_context_payload(raw_payload),
        request_id=_clean_request_id(raw_payload.get("request_id")),
    ).to_policy_context()


def _resolved_tool_request_context(
    arguments: Mapping[str, Any], *, request_id: str = ""
) -> RequestContext:
    resolved = _TOOL_REQUEST_CONTEXT.get()
    if resolved is not None:
        return resolved
    return context_from_policy_context(
        _policy_context_payload(arguments),
        request_id=request_id or _clean_request_id(arguments.get("request_id")),
    )


def _arguments_with_trusted_context(
    arguments: Mapping[str, Any] | None,
    context: RequestContext | None,
    *,
    inject_policy_context: bool,
) -> dict[str, Any]:
    """Return MCP arguments governed by a transport-resolved context.

    Direct adapter and stdio calls pass ``context=None`` and retain the
    existing trusted-local behavior: callers may provide ``policy_context``
    themselves. Remote HTTP transports pass a context resolved from the
    authenticated request. In that mode both supported caller-controlled
    locations are removed before the trusted values are injected, preventing
    an outer or nested value from overriding tenant, role, audience, or
    environment policy.
    """

    out = dict(arguments or {})
    if context is None:
        return out

    out = without_policy_context(out)

    # The transport owns correlation identity too. Do not let a JSON-RPC
    # argument disagree with the request context recorded in audit events.
    out.pop("request_id", None)
    if context.request_id:
        out["request_id"] = context.request_id
    if inject_policy_context:
        out["policy_context"] = context.to_policy_context()
    return out


def _query_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return build_query_payload(
        payload,
        object_payload=_object_argument,
        policy_context=_policy_context_payload(payload),
    )


def _partial_query_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the partial Query IR a metadata or plan tool received.

    These tools take an argument envelope, so only ``query`` holds Query IR;
    the tool's other arguments are never read as query fields. An absent seed
    still carries the resolved policy context.
    """
    return _query_payload({**payload, "query": payload.get("query")})


def _query_payload_with_mcp_default_verbosity(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the query payload for validate/compile/execute with the
    MCP-adapter default verbosity applied.

    ``_query_payload`` already hoists an explicit outer ``verbosity``
    into the query dict (and in-query values win over the outer
    envelope), so by the time we get here an empty/missing ``verbosity``
    means the caller expressed no preference — fill in
    :data:`MCP_DEFAULT_QUERY_VERBOSITY`. Because the runtime resolves
    verbosity from the query payload for both success AND soft-fail
    (``ok: false``) envelopes, error responses inherit the minimal
    default too.
    """
    query = _query_payload(payload)
    if str(query.get("verbosity", "") or "").strip() == "":
        query["verbosity"] = MCP_DEFAULT_QUERY_VERBOSITY
    return query


# Explicit minimal verbosity returns the answer without compiler plans
# (logical, SQL, physical, performance) and their copies. An omitted level,
# "compact", and "full" return the v1 whole response.
_SEGMENT_MINIMAL_KEYS: dict[str, frozenset[str]] = {
    "segment-validate": frozenset({"segment", "normalized_segment", "derived_query"}),
    "segment-explain": frozenset(
        {
            "segment",
            "normalized_segment",
            "derived_query",
            "rendered_sql",
        }
    ),
    "segment-preview": frozenset(
        {
            "segment",
            "member_key_dimensions",
            "preview_dimensions",
            "rows",
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


def _segment_response(tool: str, payload: Mapping[str, Any], verbosity: Any) -> dict[str, Any]:
    out = dict(payload or {})
    if str(verbosity or "full").strip().lower() != "minimal":
        return out
    keep = _SEGMENT_MINIMAL_KEYS[tool] | _SEGMENT_OUTCOME_KEYS
    return {
        key: value
        for key, value in out.items()
        if key in keep
        and (key in {"ok", "status", "errors", "warnings"} or value not in ("", [], {}))
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


def _strip_execute_transport_args(arguments: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(arguments or {})
    cleaned.pop("row_format", None)
    cleaned.pop("max_rows", None)
    return cleaned


def _plan_detail(value: Any, default: str = "best") -> str:
    """MCP plan's detail level; anything unknown gets the interface default."""

    detail = str(value or "").strip().lower()
    return detail if detail in {"query", "best", "full", "debug"} else default


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


def _execute_row_limits(query: Mapping[str, Any], requested: int) -> tuple[int, int, bool]:
    """Return (rows to return, rows to fetch, whether the query's limit binds).

    An explicit ``max_rows`` argument sets the response cap. A ``limits.max_rows``
    inside the query is an operator's fetch ceiling: it can only lower the
    cap and bounds what is fetched, but never becomes the response size.
    Without it, execute fetches up to ``MCP_ROW_COUNT_CEILING`` rows so a
    truncated result can report its total.
    """

    limits = query.get("limits")
    fence = _positive_int(limits.get("max_rows")) if isinstance(limits, Mapping) else None
    cap = requested
    if fence is not None and fence <= cap:
        return fence, fence, True
    if fence is not None:
        return cap, fence, False
    return cap, max(cap, MCP_ROW_COUNT_CEILING), False


def _ungrained_time(query: Mapping[str, Any]) -> bool:
    """A time block with a role and no grain: rows group by the raw timestamp."""

    time = query.get("time")
    return isinstance(time, Mapping) and bool(time.get("temporal_role")) and not time.get("grain")


# Expression kinds that aggregate over their own clock, so a grouped query
# using them doesn't project the raw timestamp. Running totals (cumulative,
# period_to_date) are not among them: their buckets come from time.grain.
_OWN_CLOCK_KINDS = frozenset({"prior_period", "rolling", "conversion", "offset_window"})


def _uses_own_clock(expression: Any) -> bool:
    if not isinstance(expression, Mapping):
        return False
    if str(expression.get("kind", "") or "") in _OWN_CLOCK_KINDS:
        return True
    for value in expression.values():
        items = value if isinstance(value, list) else [value]
        if any(_uses_own_clock(item) for item in items):
            return True
    return False


def _truncate_rows(
    result: dict[str, Any], *, cap: int, query: Mapping[str, Any], fence_binds: bool = False
) -> dict[str, Any]:
    """Return at most ``cap`` rows, and say so loudly.

    A truncated result carries ``truncated: true``, ``total_row_count``
    (``None`` when more rows exist than were fetched) and an
    ``EXECUTE_ROWS_TRUNCATED`` warning that says how to narrow the query.
    """

    rows = list(result.get("rows") or [])
    beyond_fetch = bool(result.get("truncated"))
    if len(rows) <= cap and not beyond_fetch:
        return result
    kept = rows[:cap]
    total = None if beyond_fetch else len(rows)
    counted = f"{total:,}" if total is not None else f"more than {len(rows):,}"
    if _ungrained_time(query):
        advice = (
            "set time.grain (for example 'month' or 'year'); without a grain, rows group by the "
            "raw timestamp"
        )
    elif isinstance(query.get("time"), Mapping) and query["time"].get("grain"):
        advice = "use a coarser time.grain, filter, or group by fewer dimensions"
    else:
        advice = "filter, or group by fewer dimensions"
    raise_hint = (
        " The query's limits.max_rows caps the rows fetched."
        if fence_binds
        else " Or raise max_rows."
    )
    warning = {
        "code": "EXECUTE_ROWS_TRUNCATED",
        "severity": "warning",
        "message": f"Returned {len(kept):,} of {counted} rows. To narrow the result, {advice}."
        + raise_hint,
        "details": {"returned_rows": len(kept), "total_row_count": total, "max_rows": cap},
    }
    return {
        **result,
        "rows": kept,
        "row_count": len(kept),
        "truncated": True,
        "total_row_count": total,
        "warnings": [*list(result.get("warnings") or []), warning],
    }


def _grouped_ungrained_time_warning(query: Mapping[str, Any]) -> dict[str, Any] | None:
    """Warn about a temporal role with no grain in a grouped query.

    The runtime's ``UNGRAINED_TIME_PROJECTION`` covers ungrouped queries
    only. A grouped one hits the same trap, each group returning one row per
    distinct timestamp, so MCP adds its own code with the same shape.
    """

    if not query.get("group_by") or not _ungrained_time(query):
        return None
    if any(
        _uses_own_clock(item.get("expression"))
        for item in query.get("select") or []
        if isinstance(item, Mapping)
    ):
        return None
    temporal_role = str((query.get("time") or {}).get("temporal_role", ""))
    return semantic_issue(
        code="UNGRAINED_GROUPED_TIME_PROJECTION",
        message=(
            "query.time.temporal_role is set without time.grain in a grouped query, so each "
            "group returns one row per distinct timestamp. Set time.grain (for example "
            "'month' or 'year') to bucket the result."
        ),
        severity="warning",
        stage="mcp",
        details={
            "temporal_role": temporal_role,
            "recovery_hints": [
                {
                    "code": "SET_TIME_GRAIN",
                    "message": "Add time.grain to bucket each group's rows.",
                    "suggested_patches": [{"add": {"time.grain": "month"}}],
                }
            ],
        },
    )


def _echo_caller_limits(result: dict[str, Any], limits: Any) -> dict[str, Any]:
    """Echo the caller's own ``limits``, not the fetch ceiling execute added.

    Re-running an echoed query must not raise the default cap.
    """

    echoed = result.get("query")
    if not isinstance(echoed, Mapping) or "limits" not in echoed:
        return result
    query = {key: value for key, value in echoed.items() if key != "limits"}
    if isinstance(limits, Mapping):
        query["limits"] = dict(limits)
    return {**result, "query": query}


def _with_warning(result: dict[str, Any], warning: dict[str, Any] | None) -> dict[str, Any]:
    if warning is None or not bool(result.get("ok", True)):
        return result
    existing = list(result.get("warnings") or [])
    if any(isinstance(item, Mapping) and item.get("code") == warning["code"] for item in existing):
        return result
    return {**result, "warnings": [*existing, warning]}


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


def _coerce_kinds(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    raise _argument_error(
        "MCP argument 'kinds' must be a string or array of strings.", field="kinds", value=value
    )


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
        """``interface`` is ``"v1"`` or ``"v2"``; omitted, it comes from
        :data:`MCP_INTERFACE_ENV`, then :data:`MCP_DEFAULT_INTERFACE`."""

        self._interface = _INTERFACES[resolve_interface(interface)]
        self.interface = self._interface.version
        self.instructions = self._interface.instructions
        # discover and inspect default to full cards in v1, slim ones in v2.
        self._card_verbosity = "minimal" if self.interface == "v2" else "compact"
        self.runtime = runtime
        self.package_id = runtime.package_id
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "capabilities": self._handle_capabilities,
            "catalog": self._handle_catalog,
            "discover": self._handle_discover,
            "inspect": self._handle_inspect,
            "build-options": self._handle_build_options,
            "valid-values": self._handle_valid_values,
            "plan": self._handle_plan,
            "validate": self._handle_validate,
            "compile": self._handle_compile,
            "execute": self._handle_execute,
            "segment-validate": self._handle_segment_validate,
            "segment-explain": self._handle_segment_explain,
            "segment-preview": self._handle_segment_preview,
        }
        if self.interface == "v2":
            # v2 routes each call to the v1 handler that serves it.
            v1 = self._tool_handlers
            self._tool_handlers = {
                "discover": v1["discover"],
                "inspect": v1["inspect"],
                "valid-values": v1["valid-values"],
                "plan": v1["plan"],
                "execute": self._handle_execute_mode,
                "segment": self._handle_segment_action,
            }

    @classmethod
    def from_package(
        cls, package_id: str, *, interface: str | None = None
    ) -> SemanticLayerMCPAdapter:
        return cls(Runtime(package_id), interface=interface)

    @classmethod
    def from_path(cls, path: str, *, interface: str | None = None) -> SemanticLayerMCPAdapter:
        return cls(Runtime.from_path(path), interface=interface)

    @property
    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return dict(self._tool_handlers)

    def close(self) -> None:
        self.runtime.close()

    def list_tools(self) -> list[dict[str, Any]]:
        return list_tool_definitions(self.interface)

    def list_resources(self) -> list[dict[str, Any]]:
        return list_resource_definitions(self.interface)

    def list_prompts(self) -> list[dict[str, Any]]:
        return list_prompt_definitions(self.interface)

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Call one MCP tool.

        ``request_context`` is supplied only by remote transports after their
        authentication boundary. Omitting it preserves the stdio/in-process
        contract where the caller is trusted to provide policy context.
        """

        def finish(response: dict[str, Any]) -> dict[str, Any]:
            if request_context is not None:
                response["request_context"] = request_context_payload(request_context)
                if request_context.request_id:
                    response["request_id"] = request_context.request_id
            emit_audit_event(
                "mcp_tool",
                tool=name,
                package_id=self.package_id,
                request_id=str(response.get("request_id", "")),
                status=response.get("status"),
                request_context=dict(response.get("request_context", {}) or {}),
                error_codes=[
                    str(issue.get("code", ""))
                    for issue in list(response.get("errors", []) or [])
                    if isinstance(issue, Mapping) and issue.get("code")
                ],
                timing_ms=response.get("timing_ms"),
            )
            return response

        rules = self._interface.rules
        policy_aware = "policy_context" in rules.known.get(name, frozenset())
        if arguments is not None and not isinstance(arguments, Mapping):
            sanitized = _arguments_with_trusted_context(
                {}, request_context, inject_policy_context=policy_aware
            )
            return finish(
                self._error_response(
                    SemanticLayerError(
                        "INVALID_MCP_ARGUMENTS",
                        "MCP tool arguments must be a JSON object.",
                        details={"tool": name, "argument_type": type(arguments).__name__},
                    ),
                    sanitized,
                )
            )
        args_dict = _arguments_with_trusted_context(
            arguments, request_context, inject_policy_context=policy_aware
        )
        handler = self._tool_handlers.get(name)
        if handler is None:
            details: dict[str, Any] = {"tool": name, "available_tools": sorted(self._tool_handlers)}
            message = f"Unknown MCP tool '{name}'"
            replacement = _V2_REPLACEMENTS.get(name) if self.interface == "v2" else None
            if replacement:
                details["replacement"] = replacement
                message = f"MCP interface v2 has no '{name}' tool; use {replacement}."
            return finish(
                self._error_response(
                    SemanticLayerError("UNKNOWN_MCP_TOOL", message, details=details), args_dict
                )
            )
        unknown_arg_error = _unknown_argument_error(
            tool_name=name, arguments=args_dict, rules=rules
        )
        if unknown_arg_error is not None:
            return finish(self._error_response(unknown_arg_error, args_dict))
        type_error = _required_string_type_error(tool_name=name, arguments=args_dict, rules=rules)
        if type_error is not None:
            return finish(self._error_response(type_error, args_dict))
        missing_arg_error = _missing_required_argument_error(
            tool_name=name, arguments=args_dict, rules=rules
        )
        if missing_arg_error is not None:
            return finish(self._error_response(missing_arg_error, args_dict))
        # Compute unknown-arg warnings once at the boundary (warn-tools
        # only). Per-handler typo checks (e.g. DISCOVER_UNKNOWN_ARG for
        # ``term``→``terms``, BUILD_OPTIONS_UNKNOWN_ARG for
        # ``object_id``→``focus_object_id``) still fire from inside the
        # handler with their domain-specific guidance; this generic
        # pass catches everything else.
        unknown_arg_warnings = _unknown_argument_warnings(
            tool_name=name, arguments=args_dict, rules=rules
        )
        response = handler(args_dict)
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
                response["warnings"] = existing + deduped
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
                    "catalog": resolve_catalog(
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
                    "catalog": resolve_catalog(
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
                {
                    "ok": False,
                    "status": "error",
                    "error": issue,
                    "errors": [issue],
                    "recovery_hints": list(issue.get("recovery_hints", [])),
                },
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
        if self.interface == "v2" and name in _V2_PROMPTS:
            text = _V2_PROMPTS[name][1].format(
                intent=str(args.get("intent", "") or ""),
                package_id=str(args.get("package_id", self.package_id) or self.package_id),
                query_json=str(args.get("query_json", "") or ""),
                segment_id=str(args.get("segment_id", "") or ""),
            )
        elif name == "semantic-rails-query-builder":
            intent = str(args.get("intent", "") or "")
            package_id = str(args.get("package_id", self.package_id) or self.package_id)
            text = (
                f"Use the Semantic Layer MCP tools against package '{package_id}' to answer: {intent}\n"
                "Start with discover and inspect the best governed objects. Draft Query IR with plan, or assemble it "
                "with build-options and valid-values, then run it with execute, which validates and compiles first. "
                "validate and compile are optional dry runs; compile's response includes an `explain` payload — read it "
                "when the user needs methodology or SQL lineage."
            )
        elif name == "semantic-rails-query-review":
            text = (
                "Review this Semantic Layer Query IR with validate and compile (its response includes the `explain` payload). "
                "Only call execute after validation succeeds and the user needs result rows.\n\n"
                f"{str(args.get('query_json', '') or '')}"
            )
        elif name == "semantic-rails-segment-workflow":
            segment_id = str(args.get("segment_id", "") or "")
            text = (
                f"Use segment-validate, segment-explain, and segment-preview for segment '{segment_id}'. "
                "Report validation errors first, then summarize the derived query and member preview."
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
            return {
                "ok": False,
                "status": "error",
                "error": issue,
                "errors": [issue],
                "messages": [],
            }
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
        self, payload: Mapping[str, Any], *, request_id: str, started_at: float
    ) -> dict[str, Any]:
        out = dict(payload or {})
        out.setdefault("ok", not bool(out.get("errors")))
        out["status"] = _status_label(out)
        out.setdefault("api_version", self.interface)
        out.setdefault("request_id", request_id or uuid.uuid4().hex)
        out.setdefault("package_id", self.package_id)
        out.setdefault("warnings", [])
        if "errors" not in out:
            error = out.get("error")
            if isinstance(error, dict):
                out["errors"] = [error]
            elif error:
                out["errors"] = [_internal_issue(str(error))]
            else:
                out["errors"] = []
        # Surface errors[0] at the top-level `error` so agents that read
        # the conventional MCP-envelope `if result.get("error"): ...`
        # branch don't silently treat a soft-fail (validate /
        # segment-validate) as success.
        if not out.get("error"):
            first = next(
                (issue for issue in (out.get("errors") or []) if isinstance(issue, dict)),
                None,
            )
            if first is not None:
                out["error"] = first
        if "recovery_hints" not in out:
            hints: list[Any] = []
            for error in list(out.get("errors", []) or []):
                if isinstance(error, dict):
                    hints.extend(list(error.get("recovery_hints", []) or []))
            out["recovery_hints"] = hints
        out.setdefault("timing_ms", round((time.perf_counter() - started_at) * 1000, 3))
        return out

    def _success(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any], started_at: float
    ) -> dict[str, Any]:
        out = self._envelope(
            payload,
            request_id=_clean_request_id(arguments.get("request_id")),
            started_at=started_at,
        )
        out.setdefault(
            "request_context",
            request_context_payload(
                _resolved_tool_request_context(arguments, request_id=str(out.get("request_id", "")))
            ),
        )
        return out

    def _error_response(
        self,
        exc: SemanticLayerError,
        arguments: Mapping[str, Any],
        *,
        started_at: float | None = None,
    ) -> dict[str, Any]:
        # Surface closest_matches on OBJECT_NOT_FOUND just like the HTTP
        # path — agents shouldn't have to retry blind on a typo'd id.
        # enrich_object_not_found is a pure read on the in-memory config
        # but we defensively never want diagnostics enrichment to mask
        # the original error.
        with contextlib.suppress(Exception):
            exc = enrich_object_not_found(exc, self.runtime._config)
        issue = exception_issue(exc, stage="mcp")
        out = self._envelope(
            {
                "ok": False,
                "status": "error",
                "error": issue,
                "errors": [issue],
                "recovery_hints": list(issue.get("recovery_hints", [])),
            },
            request_id=_clean_request_id(arguments.get("request_id")),
            started_at=started_at or time.perf_counter(),
        )
        out.setdefault(
            "request_context",
            request_context_payload(
                _resolved_tool_request_context(arguments, request_id=str(out.get("request_id", "")))
            ),
        )
        return out

    def _guarded(
        self, arguments: dict[str, Any], handler: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        started = time.perf_counter()
        token = None
        try:
            # Normalize policy/request identity once for the entire tool call.
            # Handlers and success/error envelopes read the same ContextVar
            # value instead of reinterpreting aliases and nested context.
            resolved_context = context_from_policy_context(
                _raw_policy_context_payload(arguments),
                request_id=_clean_request_id(arguments.get("request_id")),
            )
            token = _TOOL_REQUEST_CONTEXT.set(resolved_context)
            return self._success(handler(arguments), arguments, started)
        except SemanticLayerError as exc:
            return self._error_response(exc, arguments, started_at=started)
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
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                exception_type=type(exc).__name__,
            )
            out = self._envelope(
                {
                    "ok": False,
                    "status": "error",
                    "error": issue,
                    "errors": [issue],
                    "recovery_hints": list(issue.get("recovery_hints", [])),
                },
                request_id=_clean_request_id(arguments.get("request_id")),
                started_at=started,
            )
            out.setdefault(
                "request_context",
                request_context_payload(
                    _resolved_tool_request_context(
                        arguments, request_id=str(out.get("request_id", ""))
                    )
                ),
            )
            return out
        finally:
            if token is not None:
                _TOOL_REQUEST_CONTEXT.reset(token)

    def _handle_capabilities(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: capabilities_payload(
                self.runtime, policy_context=_policy_context_payload(args)
            ),
        )

    def _handle_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(arguments, self._catalog_for)

    def _catalog_for(self, args: dict[str, Any]) -> dict[str, Any]:
        view = str(args.get("view", "summary"))
        verbosity = str(args.get("verbosity", "summary"))
        kind = str(args.get("kind", ""))
        search = str(args.get("search", ""))
        entity = str(args.get("entity", ""))
        policy_context = _policy_context_payload(args)
        if verbosity and verbosity not in _CATALOG_VERBOSITIES:
            valid = sorted(_CATALOG_VERBOSITIES)
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                (
                    f"MCP argument 'verbosity' for 'catalog' must be one of "
                    f"{valid}; received {verbosity!r}."
                ),
                details={
                    "field": "verbosity",
                    "received": verbosity,
                    "valid_values": valid,
                },
            )
        if kind and kind not in _CATALOG_KIND_FILTERS:
            from difflib import get_close_matches

            valid = sorted(_CATALOG_KIND_FILTERS)
            closest = get_close_matches(kind, valid, n=3, cutoff=0.4)
            details: dict[str, Any] = {
                "field": "kind",
                "received": kind,
                "valid_kinds": valid,
                "closest_matches": closest,
            }
            message = f"MCP argument 'kind' must be one of {valid}; received {kind!r}."
            if closest:
                message += f" Did you mean {closest}?"
            raise SemanticLayerError("INVALID_MCP_ARGUMENTS", message, details=details)
        return {
            "catalog": resolve_catalog(
                self.runtime,
                view=view,
                verbosity=verbosity,
                kind=kind,
                search=search,
                entity=entity,
                policy_context=policy_context,
            )
        }

    def _handle_discover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _build(args: dict[str, Any]) -> dict[str, Any]:
            raw_terms = args.get("terms", "")
            terms_warnings: list[dict[str, Any]] = []
            # Catch the most common typo (`term` singular) so the call
            # doesn't silently behave like an empty-terms browse.
            for typo, canonical in (("term", "terms"), ("kind", "kinds")):
                if typo in args and canonical not in args:
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
            # Warn on unknown `kinds` values rather than silently
            # ignoring them. The catalog filter accepts these eight
            # object kinds — anything else should surface so the agent
            # knows the filter was effectively a no-op.
            requested_kinds = _coerce_kinds(args.get("kinds", []))
            unknown_kinds = [k for k in requested_kinds if k not in _CATALOG_KIND_FILTERS]
            if unknown_kinds:
                terms_warnings.append(
                    {
                        "code": "DISCOVER_UNKNOWN_KIND",
                        "severity": "warning",
                        "message": (
                            f"Unknown kinds filter value(s) {unknown_kinds}; "
                            f"valid kinds: {sorted(_CATALOG_KIND_FILTERS)}. "
                            "Unknown values were ignored."
                        ),
                        "details": {
                            "unknown_kinds": unknown_kinds,
                            "valid_kinds": sorted(_CATALOG_KIND_FILTERS),
                        },
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
            # Empty / whitespace terms previously fell through to a full
            # ranked-by-default-priority dump (~35 KB) with only a
            # `DISCOVER_NO_TERMS` warning. The blind-agent benchmark
            # caught that a careless caller treats the dump as a real
            # ranked result. For the MCP boundary, cap the limit at 3
            # for empty terms so the response stays small and the warning
            # carries the real signal.
            effective_limit = _coerce_int(args.get("limit"), 10, field="limit", minimum=1)
            if not terms_str.strip() and self.interface == "v2":
                # v2 has no catalog tool: empty terms list every id instead.
                catalog = resolve_catalog(
                    self.runtime,
                    view="summary",
                    verbosity="summary",
                    policy_context=_policy_context_payload(args),
                )
                if kinds := set(requested_kinds) & _CATALOG_KIND_FILTERS:
                    catalog = {
                        key: value
                        for key, value in catalog.items()
                        if not key.endswith("_ids") or key.removesuffix("_ids") in kinds
                    }
                return {"catalog": catalog, "warnings": terms_warnings}
            if not terms_str.strip():
                effective_limit = min(effective_limit, 3)
            payload = discover_payload(
                self.runtime,
                terms=terms_str,
                kinds=_coerce_kinds(args.get("kinds", [])),
                partial_query=_partial_query_payload(args)
                if args.get("query") or args.get("policy_context")
                else None,
                stage=str(args.get("stage", "")),
                verbosity=str(args.get("verbosity") or self._card_verbosity),
                limit=effective_limit,
                enforce_scope=True,
            )
            if terms_warnings:
                existing = list(payload.get("warnings") or [])
                payload["warnings"] = existing + terms_warnings
            # When discover returns no matches across every bucket, emit
            # a recovery hint so the agent doesn't dead-end. Mirrors the
            # empty-terms `DISCOVER_NO_TERMS` warning.
            bucket_keys = ("measures", "metrics", "dimensions", "entities", "segments")
            if terms_str.strip() and all(not payload.get(key) for key in bucket_keys):
                existing_hints = list(payload.get("recovery_hints") or [])
                # Hoist any nested low_relevance/out_of_scope recovery_hint
                # up to the top-level recovery_hints list so callers don't
                # have to dig for it.
                for nested_key in ("low_relevance", "out_of_scope"):
                    nested = payload.get(nested_key)
                    if isinstance(nested, dict):
                        nested_hint = nested.get("recovery_hint")
                        if isinstance(nested_hint, dict):
                            existing_hints.append(nested_hint)
                        elif isinstance(nested_hint, str) and nested_hint.strip():
                            existing_hints.append(
                                {"kind": f"discover_{nested_key}", "message": nested_hint}
                            )
                # And add a catalog/capabilities browse hint so the agent
                # has a concrete next-step regardless of why nothing matched.
                browse = (
                    "Call discover with empty terms to list every id."
                    if self.interface == "v2"
                    else "Try the 'catalog' tool to browse the inventory "
                    "or 'capabilities' to see what the package supports."
                )
                existing_hints.append(
                    {
                        "kind": "browse_catalog_or_capabilities",
                        "message": f"No semantic objects matched '{terms_str}'. {browse}",
                    }
                )
                payload["recovery_hints"] = existing_hints
            return payload

        return self._guarded(arguments, _build)

    def _handle_inspect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: inspect_payload(
                self.runtime,
                object_id=str(args.get("object_id", "")),
                partial_query=_partial_query_payload(args)
                if args.get("query") or args.get("policy_context")
                else None,
                verbosity=str(args.get("verbosity") or self._card_verbosity),
            ),
        )

    def _handle_build_options(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _build(args: dict[str, Any]) -> dict[str, Any]:
            typo_warnings: list[dict[str, Any]] = []
            # Domain-specific typo catches with explicit ``expected``
            # mappings — e.g. ``object_id`` → ``focus_object_id``.
            # The generic unknown-arg pass in ``call_tool`` covers the
            # rest; the boundary dedupes against any key flagged here.
            for typo, canonical in (
                ("object_id", "focus_object_id"),
                ("terms", "focus_terms"),
            ):
                if typo in args and canonical not in args:
                    typo_warnings.append(
                        {
                            "code": "BUILD_OPTIONS_UNKNOWN_ARG",
                            "severity": "warning",
                            "message": (
                                f"Received argument '{typo}'; "
                                f"did you mean '{canonical}'? The value was ignored."
                            ),
                            "details": {
                                "received": typo,
                                "expected": canonical,
                                "closest_matches": [canonical],
                            },
                        }
                    )
            payload = build_options_payload(
                self.runtime,
                partial_query=_partial_query_payload(args),
                focus_terms=str(args.get("focus_terms", "")),
                focus_object_id=str(args.get("focus_object_id", "")),
                step=str(args.get("step", "")),
                stage=str(args.get("stage", "")),
                verbosity=str(args.get("verbosity", "compact")),
                include_blocked=_coerce_bool(args.get("include_blocked"), True),
                limit=_coerce_int(args.get("limit"), 10, field="limit", minimum=1),
            )
            if typo_warnings:
                payload["warnings"] = list(payload.get("warnings") or []) + typo_warnings
            return payload

        return self._guarded(arguments, _build)

    def _handle_valid_values(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: valid_values_payload(
                self.runtime,
                dimension_id=str(args.get("dimension_id", "")),
                query=_partial_query_payload(args)
                if args.get("query") or args.get("policy_context")
                else None,
                search=str(args.get("search", "")),
                limit=_coerce_int(args.get("limit"), 100, field="limit", minimum=1),
                offset=_coerce_int(args.get("offset"), 0, field="offset", minimum=0),
                include_counts=_coerce_bool(args.get("include_counts"), False),
                allow_live_query=_coerce_bool(args.get("allow_live_query"), False),
            ),
        )

    def _handle_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: plan_payload(
                self.runtime,
                intent=str(args.get("intent", "")),
                partial_query=_partial_query_payload(args),
                detail=_plan_detail(
                    args.get("detail"), "query" if self.interface == "v2" else "best"
                ),
                limit=_coerce_int(args.get("limit"), 3, field="limit", minimum=1),
            ),
        )

    def _handle_validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            query = _query_payload_with_mcp_default_verbosity(args)
            return _with_warning(
                self.runtime.validate(query), _grouped_ungrained_time_warning(query)
            )

        return self._guarded(arguments, _run)

    def _handle_compile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            query = _query_payload_with_mcp_default_verbosity(args)
            return _with_warning(
                self.runtime.compile(query), _grouped_ungrained_time_warning(query)
            )

        return self._guarded(arguments, _run)

    def _handle_execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            row_format = _row_format_arg(args)
            requested_cap = _max_rows_arg(args.get("max_rows"))
            if requested_cap is None and self.interface == "v2":
                requested_cap = MCP_DEFAULT_MAX_ROWS
            query_payload = _query_payload_with_mcp_default_verbosity(
                _strip_execute_transport_args(args)
            )
            if requested_cap is None:
                result = self.runtime.query(query_payload)
            else:
                cap, fetch, fence_binds = _execute_row_limits(query_payload, requested_cap)
                limits = query_payload.get("limits")
                query_payload["limits"] = {
                    **(dict(limits) if isinstance(limits, Mapping) else {}),
                    "max_rows": fetch,
                }
                result = _echo_caller_limits(self.runtime.query(query_payload), limits)
            if bool(result.get("ok", True)):
                if requested_cap is not None:
                    result = _truncate_rows(
                        result, cap=cap, query=query_payload, fence_binds=fence_binds
                    )
                result = _with_warning(result, _grouped_ungrained_time_warning(query_payload))
            # Surface an EXECUTE_EMPTY_RESULT warning when a successful
            # execute returns 0 rows and the user authored no filters —
            # the most common "successful but wrong" outcome from a
            # planner-derived IR. Blind-agent benchmark saw a
            # confidently-empty execute on a wrong-measure choice.
            if (
                bool(result.get("ok", True))
                and int(result.get("row_count", 0) or 0) == 0
                and not query_payload.get("where")
                and not query_payload.get("metric_filters")
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
                                "time window, or widen the time range. Use "
                                + (
                                    "execute with mode='sql' and verbosity='full'"
                                    if self.interface == "v2"
                                    else "`compile` with verbosity=full"
                                )
                                + " to inspect the resolved plan."
                            ),
                        }
                    )
                    result["warnings"] = existing
            if bool(result.get("ok", True)) and row_format == "columns":
                result = _columnar_rows(result)
            return result

        return self._guarded(arguments, _run)

    def _handle_execute_mode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """v2 ``execute``: mode ``run``, ``validate`` or ``sql`` calls v1's
        execute, validate or compile handler."""

        try:
            mode = _choice_arg(
                arguments.get("mode"), "run", _EXECUTE_MODES, tool="execute", field="mode"
            )
        except SemanticLayerError as exc:
            return self._error_response(exc, arguments)
        args = {key: value for key, value in arguments.items() if key != "mode"}
        if mode == "run":
            return self._handle_execute(args)
        args = _strip_execute_transport_args(args)
        return self._handle_validate(args) if mode == "validate" else self._handle_compile(args)

    def _handle_segment_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """v2 ``segment``: each action calls the v1 segment tool of that name,
        at minimal verbosity unless the caller asks for more."""

        try:
            action = _choice_arg(
                arguments.get("action"), "", _SEGMENT_ACTIONS, tool="segment", field="action"
            )
        except SemanticLayerError as exc:
            return self._error_response(exc, arguments)
        args = {key: value for key, value in arguments.items() if key != "action"}
        if not str(args.get("verbosity") or "").strip():
            args["verbosity"] = "minimal"
        handlers = {
            "validate": self._handle_segment_validate,
            "explain": self._handle_segment_explain,
            "preview": self._handle_segment_preview,
        }
        return handlers[action](args)

    def _handle_segment_validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: _segment_response(
                "segment-validate",
                self.runtime.segment_validate(
                    str(args.get("segment_id", "")),
                    policy_context=_policy_context_payload(args),
                ),
                args.get("verbosity"),
            ),
        )

    def _handle_segment_explain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: _segment_response(
                "segment-explain",
                self.runtime.segment_explain(
                    str(args.get("segment_id", "")),
                    policy_context=_policy_context_payload(args),
                ),
                args.get("verbosity"),
            ),
        )

    def _handle_segment_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: _segment_response(
                "segment-preview",
                self.runtime.segment_preview(
                    str(args.get("segment_id", "")),
                    limit=_coerce_int(args.get("limit"), 50, field="limit", minimum=1),
                    policy_context=_policy_context_payload(args),
                ),
                args.get("verbosity"),
            ),
        )


def json_text(payload: Any) -> str:
    """Render a payload for an MCP text channel: compact, sorted, deterministic.

    Hosts that forward ``content[].text`` to the model pay for every
    character, and indentation added about half again to each response.
    """

    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def _mcp_server_class() -> Any:
    """The MCP SDK's high-level server: ``MCPServer`` on SDK 2.x, ``FastMCP`` on 1.x."""

    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        pass
    else:
        return MCPServer
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "Install the MCP Python SDK (mcp>=1.27) to create the optional local stdio server."
        ) from exc
    return FastMCP


def create_optional_fastmcp_server(
    adapter: SemanticLayerMCPAdapter,
    *,
    server_name: str = "semantic-rails",
) -> Any:
    """Create a stdio-only MCP SDK facade if the SDK is installed.

    Selects ``FastMCP`` on SDK 1.x or ``MCPServer`` when the SDK 2.x module
    is present. The import is intentionally local so importing semantic_rails.mcp never requires an
    external MCP runtime. This helper deliberately cannot start
    or expose FastMCP's SSE/Streamable-HTTP apps: those generic network
    runners do not pass Semantic Rails' transport-authenticated
    :class:`RequestContext` into tool calls. Remote callers must use the
    authenticated ASGI ``/mcp`` boundary or the legacy guarded HTTP server.
    """

    server = _mcp_server_class()(server_name, instructions=adapter.instructions)
    for definition in adapter.list_tools():
        name = str(definition["name"])
        description = str(definition["description"])

        def _make_tool(
            tool_name: str, tool_description: str
        ) -> Callable[[dict[str, Any] | None], str]:
            def _tool(arguments: dict[str, Any] | None = None) -> str:
                return json_text(adapter.call_tool(tool_name, arguments or {}))

            _tool.__name__ = f"semantic_rails_{tool_name.replace('-', '_')}"
            _tool.__doc__ = tool_description
            return _tool

        tool_fn = _make_tool(name, description)
        if hasattr(server, "add_tool"):
            server.add_tool(tool_fn, name=name, description=description)
        else:
            server.tool(name=name, description=description)(tool_fn)

    class _StdioOnlyFastMCP:
        _blocked_network_members = frozenset(
            {
                "run_sse_async",
                "run_streamable_http_async",
                "sse_app",
                "streamable_http_app",
            }
        )

        def __init__(self, wrapped: Any) -> None:
            self._wrapped = wrapped
            self.transport_scope = "stdio-only"

        @staticmethod
        def _reject_network(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "create_optional_fastmcp_server is stdio-only. Use SemanticLayerASGIApp "
                "or semantic-rails mcp http for an authenticated network transport."
            )

        def run(self, *args: Any, **kwargs: Any) -> Any:
            transport = kwargs.get("transport", args[0] if args else "stdio")
            if str(transport or "stdio").strip().lower() != "stdio":
                return self._reject_network()
            return self._wrapped.run(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            lowered = name.lower()
            if name in self._blocked_network_members or any(
                marker in lowered for marker in ("http", "sse", "streamable")
            ):
                return self._reject_network
            return getattr(self._wrapped, name)

    return _StdioOnlyFastMCP(server)
