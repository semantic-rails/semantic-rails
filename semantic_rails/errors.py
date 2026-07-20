"""Error type and the registered error-code catalog.

Defines :class:`SemanticLayerError` — the single exception type the
runtime raises — plus the canonical ``SEMANTIC_ERROR_CODES`` tuple that
the contract test in ``tests/semantic_rails/test_error_codes_contract.py``
pins down. Adding a new error code without registering it here will
fail that test, which is intentional.
"""

from __future__ import annotations

from typing import Any


class SemanticLayerError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


ERROR_CODES = {
    "AMBIGUOUS_ALIAS",
    "AMBIGUOUS_PATH",
    "DUPLICATE_OUTPUT_ALIAS",
    "UNSUPPORTED_AGGREGATION",
    "UNSUPPORTED_CONDITIONAL_AGGREGATE",
    "UNSUPPORTED_PLATFORM",
    "INVALID_TEMPORAL_ROLE",
    "INCOMPATIBLE_TEMPORAL_ROLE",
    "INVALID_TEMPORAL_BINDING",
    "INVALID_ANCHOR_ROLE",
    "INCOMPATIBLE_CALENDAR",
    "FANOUT_UNSAFE",
    "ROLLUP_UNSAFE",
    "MEASURE_VALIDITY_BOUNDARY",
    "OUT_OF_SCOPE",
    "CUMULATIVE_TIME_FILTER_UNSUPPORTED",
    "WINDOWED_TIME_FILTER_UNSUPPORTED",
    "MIXED_GRAIN_INVALID",
    "NO_VALID_VALUES_SOURCE",
    "REWRITE_NOT_SUPPORTED",
    "INVALID_EXPRESSION_AST",
    "INVALID_EXPRESSION",
    "INVALID_EXPRESSION_KEY",
    "OBJECT_NOT_FOUND",
    "INVALID_QUERY",
    "INVALID_CONFIG",
    "INVALID_METRIC_FILTER",
    "INVALID_SEGMENT",
    "MISSING_DEPENDENCY",
    "QUERY_EXECUTION_ERROR",
    "PATH_NOT_FOUND",
    "PATH_JOIN_CONFLICT",
    "POLICY_DENIED",
    "INVALID_METRIC_PREDICATE",
    "PREDICATE_SCOPE_UNSAFE",
    "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE",
    "PREDICATE_FILTER_INCOMPATIBLE",
    "PREDICATE_GRAIN_UNSAFE",
    "PREDICATE_ENTITY_REQUIRED",
    "PREDICATE_INPUT_REQUIRED",
    "PREDICATE_NOT_SUPPORTED",
    "INVALID_ORDER_BY",
    "CONVERSION_NOT_SUPPORTED",
    "CONVERSION_ENTITY_REQUIRED",
    "CONVERSION_WINDOW_REQUIRED",
    "CONVERSION_MATCHING_MODE_REQUIRED",
    "CONFIG_CONFLICT",
    "UNKNOWN_MCP_PROMPT",
    "UNKNOWN_MCP_RESOURCE",
    "UNKNOWN_MCP_TOOL",
    "INVALID_MCP_ARGUMENTS",
}
