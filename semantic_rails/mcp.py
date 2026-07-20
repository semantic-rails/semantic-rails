"""MCP adapter and tool definitions.

Exposes :class:`SemanticLayerMCPAdapter`, which presents governed
tools (``capabilities``, ``catalog``, ``discover``, ``inspect``,
``build-options``, ``valid-values``, ``plan``, ``validate``,
``compile``, ``execute``, plus ``segment-validate`` /
``segment-explain`` / ``segment-preview``) backed by a single
``Runtime``. The transport — stdio vs HTTP — lives in
:mod:`semantic_rails.mcp_server`; this module is the protocol-agnostic
adapter.
"""

from __future__ import annotations

import contextlib
import copy
import json
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .catalog_service import resolve_catalog
from .diagnostics import enrich_object_not_found, exception_issue
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
from .runtime import Runtime

__all__ = [
    "JSON_OBJECT_SCHEMA",
    "MCP_INTERFACE_VERSION",
    "MCP_PROMPT_DEFINITIONS",
    "MCP_RESOURCE_DEFINITIONS",
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
    "list_prompt_definitions",
    "list_resource_definitions",
    "list_tool_definitions",
    "plan_payload",
    "request_context_payload",
    "valid_values_payload",
]


MCP_INTERFACE_VERSION = "v1"

# Default response verbosity for the MCP validate/compile/execute tools.
# Context-constrained agents drown in the ~90-100KB envelopes the runtime
# emits at its own 'compact' default (the HTTP v1 surface keeps that
# default — see runtime_parts.responses.resolve_verbosity). The MCP
# adapter defaults to 'minimal' instead: validate={ok,errors,warnings},
# compile adds rendered_sql, execute adds rows+row_count. An explicit
# 'verbosity' argument (outer envelope or inside `query`) always wins.
MCP_DEFAULT_QUERY_VERBOSITY = "minimal"

_TOOL_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "semantic_rails_mcp_tool_request_context", default=None
)


JSON_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}

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


def _tool_annotations(name: str) -> dict[str, Any]:
    """Return MCP-standard behavioral hints for a query tool.

    Query execution and live value/segment previews can contact an external
    warehouse, hence ``openWorldHint``. They remain read-only and
    non-destructive: the engine only compiles and executes SELECT-shaped SQL.
    """

    open_world = name in {"execute", "valid-values", "segment-preview"}
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


SELECT_EXPRESSION_SHAPES_HELP = (
    "IR: select[]={expression,as}, group_by[]=[<dim>,...] (bare ids), "
    "where[]={field,op,value}, order_by[]={field,direction}. "
    "select.expression: {aggregation, measure} | {metric} | "
    "{kind:prior_period|rolling|cumulative|ratio|conversion|aggregate_if|between|...}. "
    "Call 'capabilities' first for runnable example IR per kind "
    "(capabilities.expression_shapes[].example)."
)


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="capabilities",
        description=(
            "Semantic Rails MCP question-answering entrypoint. For "
            "answering governed-data questions, use: discover, plan, "
            "validate, compile, execute. Use catalog for orientation. "
            "Return the package's supported and unsupported capability "
            "surface — rolling windows, prior-period offsets, metric "
            "predicates, scoped aggregates, conversion metrics, etc. "
            "Recommended loop position: 0 (cold-start orientation). "
            "This compact orientation response tells you which IR expression kinds the "
            "runtime can compile and execute for this package. "
            "Gotcha: capabilities are package-scoped — re-call after "
            "switching packages."
        ),
        input_schema=_schema(
            {
                "request_id": {"type": "string"},
            }
        ),
    ),
    ToolDefinition(
        name="catalog",
        description=(
            "List every governed semantic object in the active package — "
            "measures, metrics, dimensions, segments, entities. "
            "Recommended loop position: 1 (after capabilities, before "
            "discover). Prefer 'discover' for term-targeted lookups. "
            "Gotcha: default verbosity is 'summary' — flat ID lists, "
            "right for orientation. Bump to 'compact' for row "
            "metadata (capped 200/kind) or 'full' for uncapped + "
            "alias_index (large)."
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
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            }
        ),
    ),
    ToolDefinition(
        name="discover",
        description=(
            "Rank semantic objects against business terms (e.g. 'revenue', "
            "'aov by store'). Returns measures, metrics, dimensions, and "
            "entities scored with 'match_reasons'. Recommended loop "
            "position: 1 (after the user's question). Pick best id, then "
            "'inspect'. Verbosity: 'minimal' trims each row to "
            "{id,kind,score,default_temporal_role,available,match_reasons} "
            "(cheap orientation); 'compact' (default) ships full cards. "
            "Gotcha: nonsense or out-of-scope terms return an "
            "'out_of_scope' or 'low_relevance' block with empty buckets — "
            "branch on those before assuming a candidate."
        ),
        input_schema=_schema(
            {
                "terms": {"type": "string"},
                "kinds": {
                    "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]
                },
                "query": QUERY_SCHEMA_SLIM,
                "stage": {"type": "string"},
                "verbosity": {"type": "string", "default": "compact"},
                "limit": {"type": "integer", "default": 10, "minimum": 1},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="inspect",
        description=(
            "Return the stable object card for one id — label, description, "
            "search terms, related dimensions, valid temporal roles, "
            "policy/validity windows. Recommended loop position: 2 (after "
            "'discover', before composing Query IR). Gotcha: 'object_id' "
            "must be a full id like 'measure.jaffle.revenue_usd', not a "
            "label — use 'discover' first if you only have a phrase."
        ),
        input_schema=_schema(
            {
                "object_id": {"type": "string"},
                "query": QUERY_SCHEMA_SLIM,
                "verbosity": {"type": "string", "default": "compact"},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            required=["object_id"],
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="build-options",
        description=(
            "Return ranked next choices for a guided query builder — given "
            "partial Query IR, what dimension/filter/time-range to add "
            "next. Recommended loop position: 2.5 (between 'inspect' and "
            "'plan' when composing step-by-step instead of from a single "
            "intent). Gotcha: pass the partial 'query' you've assembled — "
            "empty input returns initial-stage options."
        ),
        input_schema=_schema(
            {
                "query": QUERY_SCHEMA_SLIM,
                "focus_terms": {"type": "string"},
                "focus_object_id": {"type": "string"},
                "step": {"type": "string"},
                "stage": {"type": "string"},
                "verbosity": {"type": "string", "default": "compact"},
                "include_blocked": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 10, "minimum": 1},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="valid-values",
        description=(
            "Return governed valid values for one dimension — from its "
            "declared value_domain by default, or via a constrained "
            "warehouse probe when 'allow_live_query: true'. Recommended "
            "loop position: 3 (composing a 'where' filter on a "
            "categorical dimension). Gotcha: 'dimension_id' must be a "
            "full id like 'dimension.jaffle.store_name'. Set "
            "allow_live_query: true only when no declared domain exists "
            "— it costs a warehouse round-trip."
        ),
        input_schema=_schema(
            {
                "dimension_id": {"type": "string"},
                "query": QUERY_SCHEMA_SLIM,
                "search": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "include_counts": {"type": "boolean", "default": False},
                "allow_live_query": {"type": "boolean", "default": False},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            required=["dimension_id"],
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="plan",
        description=(
            "Single public intent-planning surface: natural-language intent "
            "→ one best Query IR plus optional alternatives. Use "
            "detail='query' for the tight QA loop before execute(row_format="
            "'columns'). Returns "
            "'status' ('ok' | 'low_confidence' | 'unrealizable' | "
            "'out_of_scope') and 'best.query_ir'. 'status=ok' has already "
            "paid validation cost, so agents may proceed to 'compile' or "
            "'execute' with 'best.query_ir'. Use detail='full' for alternatives/blocked "
            "drafts, or detail='debug' for compose_hints. Gotcha: branch on "
            "'status' before reading 'best'."
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
                "limit": {"type": "integer", "default": 3, "minimum": 1},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            required=["intent"],
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="validate",
        description=(
            "Check Query IR before compiling — returns errors, warnings, "
            "and repair hints. Loop position: 5 (before 'compile' or "
            "'execute'). Gotcha: 'query' must be a JSON object, not a "
            "stringified blob; wrap as {query: {...}}. Defaults to "
            "verbosity='minimal' — pass verbosity=compact|full for "
            "normalized query/policy effects/plans. "
            f"{SELECT_EXPRESSION_SHAPES_HELP}"
        ),
        input_schema=_schema(
            {
                # Full IR schema ships once across tools/list — here, on
                # the loop's gate. The other IR tools embed QUERY_SCHEMA_SLIM
                # and point back to this one.
                "query": QUERY_SCHEMA,
                "verbosity": VERBOSITY_SCHEMA,
                "sql_profile": SQL_PROFILE_SCHEMA,
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="compile",
        description=(
            "Compile validated Query IR into rendered SQL — no warehouse "
            "execution. Loop position: 6 (after 'validate', before "
            "'execute'). Gotcha: do not call before validate — compile "
            "raises on errors; validate returns structured repair hints. "
            "Defaults to verbosity='minimal' (keeps rendered_sql) — pass "
            "verbosity=compact|full for sql_plan/explain/logical plans. "
            "IR shape: see the 'validate' tool or 'build-options'."
        ),
        input_schema=_schema(
            {
                "query": QUERY_SCHEMA_SLIM,
                "verbosity": VERBOSITY_SCHEMA,
                "sql_profile": SQL_PROFILE_SCHEMA,
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="execute",
        description=(
            "Execute Query IR against the warehouse and return rows "
            "(compiles + runs in one call). Loop position: 7 (final — "
            "only after validate + compile succeed). Gotcha: only tool "
            "that costs warehouse credits + round-trip latency; "
            "side-effecting. Use 'compile' for SQL only. Defaults to "
            "verbosity='minimal' (rows + row_count) — pass verbosity="
            "compact|full for SQL/plans/explain. Use row_format='columns' "
            "for compact answers. IR shape: see the "
            "'validate' tool or 'build-options'."
        ),
        input_schema=_schema(
            {
                "query": QUERY_SCHEMA_SLIM,
                "verbosity": VERBOSITY_SCHEMA,
                "sql_profile": SQL_PROFILE_SCHEMA,
                "row_format": ROW_FORMAT_SCHEMA,
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            additional_properties=True,
        ),
    ),
    ToolDefinition(
        name="segment-validate",
        description=(
            "Validate a package-authored segment and the Query IR derived "
            "from it. Recommended loop position: 5 (segment workflow "
            "analogue of 'validate' for ad-hoc IR). Gotcha: 'segment_id' "
            "must be a full id like 'segment.jaffle.high_value_customers' "
            "— list known segments via 'catalog' if you only have a label."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            required=["segment_id"],
        ),
    ),
    ToolDefinition(
        name="segment-explain",
        description=(
            "Explain the derived query for a package-authored segment — "
            "filters, joins, and time bounds it compiles to. Recommended "
            "loop position: 6 (after 'segment-validate', before previewing "
            "rows). Gotcha: governed segments only — for ad-hoc cohorts, "
            "compose Query IR with a 'where' clause and call 'compile' to "
            "read the explain payload on its response."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            required=["segment_id"],
        ),
    ),
    ToolDefinition(
        name="segment-preview",
        description=(
            "Preview members of a package-authored segment — sample rows "
            "plus total member count. Hits the warehouse. Recommended "
            "loop position: 7 (segment analogue of 'execute' — final "
            "step). Gotcha: only segment tool that costs warehouse "
            "credits. Run segment-validate + segment-explain first; "
            "preview is for showing rows to the user."
        ),
        input_schema=_schema(
            {
                "segment_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1},
                "policy_context": POLICY_CONTEXT_SCHEMA,
                "request_id": {"type": "string"},
            },
            required=["segment_id"],
        ),
    ),
)

RESOURCE_DEFINITIONS: tuple[ResourceDefinition, ...] = (
    ResourceDefinition(
        uri="semantic-rails://capabilities",
        name="capabilities",
        description="Dependency-free MCP interface capabilities, including tool, resource, and prompt definitions.",
    ),
    ResourceDefinition(
        uri="semantic-rails://catalog/summary",
        name="catalog-summary",
        description="Compact summary catalog for the active package.",
    ),
    ResourceDefinition(
        uri="semantic-rails://catalog/full",
        name="catalog-full",
        description="Full catalog cards and payloads for the active package.",
    ),
)

PROMPT_DEFINITIONS: tuple[PromptDefinition, ...] = (
    PromptDefinition(
        name="semantic-rails-query-builder",
        description="Guide an agent through discover, inspect, build-options, valid-values, validate, compile, and execute.",
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

MCP_TOOL_DEFINITIONS = tuple(definition.to_dict() for definition in TOOL_DEFINITIONS)
MCP_RESOURCE_DEFINITIONS = tuple(definition.to_dict() for definition in RESOURCE_DEFINITIONS)
MCP_PROMPT_DEFINITIONS = tuple(definition.to_dict() for definition in PROMPT_DEFINITIONS)


def list_tool_definitions() -> list[dict[str, Any]]:
    return copy.deepcopy(list(MCP_TOOL_DEFINITIONS))


def list_resource_definitions() -> list[dict[str, Any]]:
    return copy.deepcopy(list(MCP_RESOURCE_DEFINITIONS))


def list_prompt_definitions() -> list[dict[str, Any]]:
    return copy.deepcopy(list(MCP_PROMPT_DEFINITIONS))


def _clean_request_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n")
    return cleaned[:128]


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


def _tool_required_properties(tool_name: str) -> tuple[list[str], list[str]]:
    """Return (required, known) properties from the tool's input_schema."""
    for definition in TOOL_DEFINITIONS:
        if definition.name != tool_name:
            continue
        schema = dict(definition.input_schema or {})
        required = list(schema.get("required") or [])
        known = list((schema.get("properties") or {}).keys())
        return required, known
    return [], []


# ---------------------------------------------------------------------------
# Argument strictness contract
# ---------------------------------------------------------------------------
#
# Eight rounds of blind-agent probing found three different strictness
# models across the 13 MCP tools. The contract advertised in
# ``inputSchema.additionalProperties`` didn't match the runtime
# behavior on the six tools that have to accept ``policy_context`` and
# top-level Query-IR passthrough — typos on those tools silently
# resolved to empty responses.
#
# The uniform contract:
#
# * **strict-reject** tools raise ``INVALID_MCP_ARGUMENTS`` on any
#   unknown argument (today's behavior on the five segment / catalog
#   tools).
# * **warn-and-ignore** tools emit a per-tool ``*_UNKNOWN_ARG`` warning
#   with ``closest_matches`` and continue. These are the tools that
#   need passthrough — rejecting all unknown keys would break
#   legitimate ``policy_context`` / top-level IR usage.
#
# The ``additionalProperties`` flag on each ``inputSchema`` is now a
# documentation hint about HOW to react to unknowns, not WHETHER to
# react. The ``_TOOL_KNOWN_ARGS`` table below is the source of truth.

_IR_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "select",
        "group_by",
        "where",
        "metric_filters",
        "order_by",
        "limit",
        "time",
        "temporal_role_overrides",
        "path_policy",
        "debug",
        "explain",
        "export",
        # Envelope keys that ``normalize_query`` accepts on a top-level
        # IR payload alongside the canonical IR keys.
        "policy_context",
        "limits",
        "verbosity",
        "sql_profile",
    }
)


def _tool_known_args(tool_name: str) -> frozenset[str]:
    """Source of truth for the legitimate argument keys per tool.

    Combines the tool's ``input_schema.properties`` keys with:

    * ``request_id`` — universal MCP convention threaded through every
      tool's envelope.
    * For tools that accept top-level Query-IR passthrough
      (``validate``, ``compile``, ``execute``), the canonical IR keys
      declared in :data:`_IR_TOP_LEVEL_KEYS` so callers can skip the
      ``query`` wrapper without tripping the unknown-arg check. Query
      IR's own additional-keys gate (in ``ast.py``) handles unknown IR
      keys separately — no double-validation here.
    """
    for definition in TOOL_DEFINITIONS:
        if definition.name != tool_name:
            continue
        schema = dict(definition.input_schema or {})
        known = set((schema.get("properties") or {}).keys())
        known.add("request_id")
        if tool_name in {"validate", "compile", "execute"}:
            known.update(_IR_TOP_LEVEL_KEYS)
        return frozenset(known)
    return frozenset()


_TOOL_KNOWN_ARGS: dict[str, frozenset[str]] = {
    definition.name: _tool_known_args(definition.name) for definition in TOOL_DEFINITIONS
}

_ROW_FORMATS: frozenset[str] = frozenset({"records", "columns"})

# Strict-reject tools — unknown args raise ``INVALID_MCP_ARGUMENTS``.
# These tools have ``additionalProperties: false`` and no passthrough
# needs (no ``policy_context``, no top-level IR keys).
_STRICT_REJECT_TOOLS: frozenset[str] = frozenset(
    {
        "capabilities",
        "catalog",
        "segment-validate",
        "segment-explain",
        "segment-preview",
    }
)

# Warn-and-ignore tools — unknown args emit a ``<TOOL>_UNKNOWN_ARG``
# warning and continue. These tools accept ``policy_context`` and/or
# top-level Query-IR passthrough; rejecting unknown keys outright
# would break those legitimate uses.
_WARN_AND_IGNORE_TOOLS: frozenset[str] = frozenset(
    {
        "discover",
        "build-options",
        "inspect",
        "valid-values",
        "plan",
        "validate",
        "compile",
        "execute",
    }
)

# Per-tool warning code for the generic unknown-argument signal.
_UNKNOWN_ARG_WARNING_CODE: dict[str, str] = {
    "discover": "DISCOVER_UNKNOWN_ARG",
    "build-options": "BUILD_OPTIONS_UNKNOWN_ARG",
    "inspect": "INSPECT_UNKNOWN_ARG",
    "valid-values": "VALID_VALUES_UNKNOWN_ARG",
    "plan": "PLAN_UNKNOWN_ARG",
    "validate": "VALIDATE_UNKNOWN_ARG",
    "compile": "COMPILE_UNKNOWN_ARG",
    "execute": "EXECUTE_UNKNOWN_ARG",
}


def _unknown_arg_keys(*, tool_name: str, arguments: Mapping[str, Any]) -> list[str]:
    """Return the sorted list of unknown argument keys for ``tool_name``.

    Consults :data:`_TOOL_KNOWN_ARGS` regardless of the schema's
    ``additionalProperties`` flag — the flag is a hint about reaction
    policy, not a gate on the check.
    """
    known = _TOOL_KNOWN_ARGS.get(tool_name)
    if known is None:
        return []
    return sorted(key for key in (arguments or {}) if key not in known)


def _unknown_argument_error(
    *, tool_name: str, arguments: Mapping[str, Any]
) -> SemanticLayerError | None:
    """Reject unknown tool arguments on strict-reject tools.

    Strict tools (capabilities, catalog, segment-*) raise
    ``INVALID_MCP_ARGUMENTS`` on any unknown key. Warn-and-ignore tools
    are handled separately via :func:`_unknown_argument_warnings` so
    ``policy_context`` and top-level IR passthrough still work.
    """
    from difflib import get_close_matches

    if tool_name not in _STRICT_REJECT_TOOLS:
        return None
    unknown = _unknown_arg_keys(tool_name=tool_name, arguments=arguments)
    if not unknown:
        return None
    known = _TOOL_KNOWN_ARGS.get(tool_name, frozenset())
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
    ``DISCOVER_UNKNOWN_ARG`` / ``BUILD_OPTIONS_UNKNOWN_ARG`` pattern so
    downstream consumers can branch on ``code`` alone.
    """
    from difflib import get_close_matches

    if tool_name not in _WARN_AND_IGNORE_TOOLS:
        return []
    unknown = _unknown_arg_keys(tool_name=tool_name, arguments=arguments)
    if not unknown:
        return []
    known = _TOOL_KNOWN_ARGS.get(tool_name, frozenset())
    code = _UNKNOWN_ARG_WARNING_CODE.get(tool_name, "MCP_UNKNOWN_ARG")
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
    *, tool_name: str, arguments: Mapping[str, Any]
) -> SemanticLayerError | None:
    """Return a structured INVALID_MCP_ARGUMENTS error when a required
    tool argument is missing or blank — preferred to letting the
    handler treat ``""`` as a real value and emit a misleading
    ``OBJECT_NOT_FOUND`` for the empty string.
    """
    from difflib import get_close_matches

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

    out.pop("policy_context", None)
    raw_query = out.get("query")
    if isinstance(raw_query, Mapping):
        query = dict(raw_query)
        query.pop("policy_context", None)
        out["query"] = query

    # The transport owns correlation identity too. Do not let a JSON-RPC
    # argument disagree with the request context recorded in audit events.
    out.pop("request_id", None)
    if context.request_id:
        out["request_id"] = context.request_id
    if inject_policy_context:
        out["policy_context"] = context.to_policy_context()
    return out


def _query_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    query = _object_argument(payload.get("query", payload), field="query")
    query.pop("request_id", None)
    # Hoist verbosity / sql_profile out of the outer MCP argument envelope
    # into the query payload — the runtime reads these from the query dict
    # via `resolve_verbosity` / `resolve_sql_profile`. Existing in-query
    # values (callers who put them inside `query` themselves) win.
    raw_payload = dict(payload or {})
    if "verbosity" not in query and raw_payload.get("verbosity") not in (None, ""):
        query["verbosity"] = raw_payload["verbosity"]
    if "sql_profile" not in query and raw_payload.get("sql_profile") not in (None, ""):
        query["sql_profile"] = raw_payload["sql_profile"]
    policy_context = _policy_context_payload(payload)
    if "policy_context" in query or policy_context:
        query_policy_context = _object_argument(
            query.get("policy_context"), field="query.policy_context"
        )
        query_policy_context.update(policy_context)
        query["policy_context"] = query_policy_context
    return query


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
    return cleaned


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


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


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

    def __init__(self, runtime: Runtime):
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

    @classmethod
    def from_package(cls, package_id: str) -> SemanticLayerMCPAdapter:
        return cls(Runtime(package_id))

    @classmethod
    def from_path(cls, path: str) -> SemanticLayerMCPAdapter:
        return cls(Runtime.from_path(path))

    @property
    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return dict(self._tool_handlers)

    def close(self) -> None:
        self.runtime.close()

    def list_tools(self) -> list[dict[str, Any]]:
        return list_tool_definitions()

    def list_resources(self) -> list[dict[str, Any]]:
        return list_resource_definitions()

    def list_prompts(self) -> list[dict[str, Any]]:
        return list_prompt_definitions()

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

        policy_aware = "policy_context" in _TOOL_KNOWN_ARGS.get(name, frozenset())
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
            return finish(
                self._error_response(
                    SemanticLayerError(
                        "UNKNOWN_MCP_TOOL",
                        f"Unknown MCP tool '{name}'",
                        details={"tool": name, "available_tools": sorted(self._tool_handlers)},
                    ),
                    args_dict,
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
        # ``term``→``terms``, BUILD_OPTIONS_UNKNOWN_ARG for
        # ``object_id``→``focus_object_id``) still fire from inside the
        # handler with their domain-specific guidance; this generic
        # pass catches everything else.
        unknown_arg_warnings = _unknown_argument_warnings(tool_name=name, arguments=args_dict)
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
        if uri == "semantic-rails://capabilities":
            payload = {
                "interface_version": MCP_INTERFACE_VERSION,
                "package_id": self.package_id,
                "tools": self.list_tools(),
                "resources": self.list_resources(),
                "prompts": self.list_prompts(),
            }
        elif uri == "semantic-rails://catalog/summary":
            payload = self._envelope(
                {
                    "catalog": resolve_catalog(
                        self.runtime,
                        view="summary",
                        verbosity="compact",
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
        text = json.dumps(payload, indent=2, sort_keys=True, default=str)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": text,
            "payload": payload,
        }

    def get_prompt(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        if name == "semantic-rails-query-builder":
            intent = str(args.get("intent", "") or "")
            package_id = str(args.get("package_id", self.package_id) or self.package_id)
            text = (
                f"Use the Semantic Layer MCP tools against package '{package_id}' to answer: {intent}\n"
                "Start with discover, inspect the best governed objects, use build-options and valid-values to assemble Query IR, "
                "then validate and compile before execute. compile's response includes an `explain` payload — read it when the user needs methodology or SQL lineage."
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
        out.setdefault("api_version", MCP_INTERFACE_VERSION)
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
            exc = enrich_object_not_found(exc, self.runtime.config)
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
            lambda args: capabilities_payload(self.runtime),
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
            if not terms_str.strip():
                effective_limit = min(effective_limit, 3)
            payload = discover_payload(
                self.runtime,
                terms=terms_str,
                kinds=_coerce_kinds(args.get("kinds", [])),
                partial_query=_query_payload(args)
                if args.get("query") or args.get("policy_context")
                else None,
                stage=str(args.get("stage", "")),
                verbosity=str(args.get("verbosity", "compact")),
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
                existing_hints.append(
                    {
                        "kind": "browse_catalog_or_capabilities",
                        "message": (
                            f"No semantic objects matched '{terms_str}'. "
                            "Try the 'catalog' tool to browse the inventory "
                            "or 'capabilities' to see what the package supports."
                        ),
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
                partial_query=_query_payload(args)
                if args.get("query") or args.get("policy_context")
                else None,
                verbosity=str(args.get("verbosity", "compact")),
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
                partial_query=_query_payload(args),
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
                query=_query_payload(args)
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
                partial_query=_query_payload(args)
                if args.get("query") or args.get("policy_context")
                else None,
                detail=str(args.get("detail", "best") or "best"),
                limit=_coerce_int(args.get("limit"), 3, field="limit", minimum=1),
            ),
        )

    def _handle_validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: self.runtime.validate(_query_payload_with_mcp_default_verbosity(args)),
        )

    def _handle_compile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: self.runtime.compile(_query_payload_with_mcp_default_verbosity(args)),
        )

    def _handle_execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            row_format = _row_format_arg(args)
            query_payload = _query_payload_with_mcp_default_verbosity(
                _strip_execute_transport_args(args)
            )
            result = self.runtime.query(query_payload)
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
                                "`compile` with verbosity=full to inspect the "
                                "resolved plan."
                            ),
                        }
                    )
                    result["warnings"] = existing
            if bool(result.get("ok", True)) and row_format == "columns":
                result = _columnar_rows(result)
            return result

        return self._guarded(arguments, _run)

    def _handle_segment_validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: self.runtime.segment_validate(
                str(args.get("segment_id", "")),
                policy_context=_policy_context_payload(args),
            ),
        )

    def _handle_segment_explain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: self.runtime.segment_explain(
                str(args.get("segment_id", "")),
                policy_context=_policy_context_payload(args),
            ),
        )

    def _handle_segment_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._guarded(
            arguments,
            lambda args: self.runtime.segment_preview(
                str(args.get("segment_id", "")),
                limit=_coerce_int(args.get("limit"), 50, field="limit", minimum=1),
                policy_context=_policy_context_payload(args),
            ),
        )


def create_optional_fastmcp_server(
    adapter: SemanticLayerMCPAdapter,
    *,
    server_name: str = "semantic-rails",
) -> Any:
    """Create a stdio-only FastMCP facade if the package is installed.

    The import is intentionally local so importing semantic_rails.mcp never
    requires an external MCP runtime. This helper deliberately cannot start
    or expose FastMCP's SSE/Streamable-HTTP apps: those generic network
    runners do not pass Semantic Rails' transport-authenticated
    :class:`RequestContext` into tool calls. Remote callers must use the
    authenticated ASGI ``/mcp`` boundary or the legacy guarded HTTP server.
    """

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "Install an MCP runtime that provides mcp.server.fastmcp.FastMCP "
            "to create the optional local stdio server."
        ) from exc

    server = FastMCP(server_name)
    for definition in adapter.list_tools():
        name = str(definition["name"])
        description = str(definition["description"])

        def _make_tool(
            tool_name: str, tool_description: str
        ) -> Callable[[dict[str, Any] | None], str]:
            def _tool(arguments: dict[str, Any] | None = None) -> str:
                return json.dumps(
                    adapter.call_tool(tool_name, arguments or {}),
                    indent=2,
                    sort_keys=True,
                    default=str,
                )

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
