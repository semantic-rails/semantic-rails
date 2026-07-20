"""Stable public embedding facade for hosts of the Semantic Rails engine.

Hosted products and other embedders should import integration seams from this
module rather than reaching into implementation modules.  Everything exported
here is generic: the engine has no dependency on, or knowledge of, any
particular hosting product.
"""

from __future__ import annotations

from .cache import CachedCompilation, CompiledSqlCache, LruCompiledSqlCache
from .config_validation import (
    PackageReference,
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
    validate_runtime_package,
)
from .db import (
    ConnectionCredentialProvider,
    Database,
    DuckDBAdapter,
    SnowflakeCliAdapter,
    SnowflakeNativeAdapter,
    WarehouseAdapter,
    create_duckdb_adapter,
    create_warehouse_adapter,
)
from .db_parts.common import normalize_connection_options
from .dialects import (
    ATHENA_CONNECTION_OPTIONS,
    BIGQUERY_CONNECTION_OPTIONS,
    CLICKHOUSE_CONNECTION_OPTIONS,
    DATABRICKS_CONNECTION_OPTIONS,
    DUCKLAKE_CONNECTION_OPTIONS,
    MOTHERDUCK_CONNECTION_OPTIONS,
    POSTGRES_CONNECTION_OPTIONS,
    SNOWFLAKE_CLI_CONNECTION_OPTIONS,
    SNOWFLAKE_NATIVE_CONNECTION_OPTIONS,
    WarehouseConnectorSpec,
    dialect_for_warehouse,
    supported_warehouses,
    warehouse_connector,
)
from .errors import SemanticLayerError
from .http_core import API_VERSION, PUBLIC_V1_ROUTES, SemanticHTTPService
from .mcp import MCP_INTERFACE_VERSION, SemanticLayerMCPAdapter
from .mcp_server import MCP_PROTOCOL_VERSION, handle_jsonrpc_message
from .package_tools import run_package_tests_report
from .request_context import (
    AuditSink,
    HeaderPolicyContextResolver,
    PolicyContextResolver,
    RequestContext,
    StderrAuditSink,
    context_from_headers,
    context_from_policy_context,
    emit_audit_event,
    get_audit_sink,
    get_policy_context_resolver,
    request_context_payload,
    set_audit_sink,
    set_policy_context_resolver,
)
from .runtime import Runtime

__all__ = [
    "API_VERSION",
    "ATHENA_CONNECTION_OPTIONS",
    "AuditSink",
    "BIGQUERY_CONNECTION_OPTIONS",
    "CLICKHOUSE_CONNECTION_OPTIONS",
    "CachedCompilation",
    "CompiledSqlCache",
    "ConnectionCredentialProvider",
    "DATABRICKS_CONNECTION_OPTIONS",
    "DUCKLAKE_CONNECTION_OPTIONS",
    "Database",
    "DuckDBAdapter",
    "HeaderPolicyContextResolver",
    "LruCompiledSqlCache",
    "MCP_INTERFACE_VERSION",
    "MCP_PROTOCOL_VERSION",
    "MOTHERDUCK_CONNECTION_OPTIONS",
    "POSTGRES_CONNECTION_OPTIONS",
    "PUBLIC_V1_ROUTES",
    "PackageReference",
    "PolicyContextResolver",
    "RequestContext",
    "Runtime",
    "SNOWFLAKE_CLI_CONNECTION_OPTIONS",
    "SNOWFLAKE_NATIVE_CONNECTION_OPTIONS",
    "SemanticHTTPService",
    "SemanticLayerError",
    "SemanticLayerMCPAdapter",
    "SnowflakeCliAdapter",
    "SnowflakeNativeAdapter",
    "StderrAuditSink",
    "WarehouseAdapter",
    "WarehouseConnectorSpec",
    "context_from_headers",
    "context_from_policy_context",
    "create_duckdb_adapter",
    "create_warehouse_adapter",
    "dialect_for_warehouse",
    "emit_audit_event",
    "get_audit_sink",
    "get_policy_context_resolver",
    "handle_jsonrpc_message",
    "normalize_connection_options",
    "parse_config_report",
    "request_context_payload",
    "resolve_package_reference",
    "run_package_tests_report",
    "set_audit_sink",
    "set_policy_context_resolver",
    "supported_warehouses",
    "validate_config_report",
    "validate_runtime_package",
    "warehouse_connector",
]
