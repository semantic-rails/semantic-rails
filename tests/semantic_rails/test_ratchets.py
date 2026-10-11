"""Ratchets: counts per module that may only shrink.

Each pins today's count of one pattern per module. A change that adds an occurrence fails; one
that removes some passes, so work in flight is not blocked (lower the pin while you are there).
A new occurrence belongs in the module that owns the pattern instead, named on each ratchet.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from semantic_rails import embedding

PACKAGE = Path(__file__).resolve().parents[2] / "semantic_rails"

# Filter meaning lives in operators.py (the word table) and sql_ast.py (its SQL): string
# literals spelling a filter operator, outside them.
FILTER_OP_SPELLINGS = frozenset(
    {"!=", "<>", "IN", "NOT IN", "IS DISTINCT FROM", "IS NOT DISTINCT FROM", "LIKE", "NOT LIKE"}
    | {"ILIKE", "eq", "neq", "gt", "gte", "lt", "lte", "not_in", "nin"}
)
FILTER_OP_OWNERS = frozenset({"operators.py", "sql_ast.py"})
# Identity comes from credentials, parsed at ingress: request_context.py and the transports.
INGRESS = frozenset(
    {"request_context.py", "request_payload.py", "api.py", "asgi.py", "http_core.py"}
    | {"http_request.py", "mcp.py", "mcp_query.py", "mcp_server.py", "mcp_session.py"}
    | {"mcp_streamable_http.py", "architect_mcp.py"}
)
# A query call drafted by the planner is built by planner/call.py's QueryBuilder.
CALL_KEYS = frozenset({"select", "group_by", "where", "having", "order_by"})

PINS: dict[str, dict[str, int]] = {
    "filter op spellings": {
        "ast.py": 4,
        "cli/interpretation.py": 4,
        "compiler.py": 1,
        "compiler_parts/bind.py": 6,
        "compiler_parts/temporal.py": 4,
        "diagnostics.py": 1,
        "dialects.py": 4,
        "expressions.py": 4,
        "metadata.py": 1,
        "planner/patterns/inline_period_shift.py": 1,
        "planner/patterns/scoped_predicate_ratio.py": 1,
        "planner/qualifiers.py": 13,
        "query_call.py": 5,
        "renderer.py": 9,
        "repl/authoring.py": 1,
        "runtime_parts/answer_warnings.py": 1,
    },
    "identity rebuilt outside ingress": {
        "cache.py": 1,
        "catalog_service.py": 1,
        "metadata_parts/valid_values.py": 1,
        "policies.py": 1,
        "resource_access.py": 1,
        "answer.py": 1,
        "runtime.py": 5,
        "visible_view.py": 2,
    },
    "wall clock read outside clock.py": {
        "audit.py": 1,
        "planner/time_phrases.py": 1,
        "seed_provenance.py": 1,
    },
    "planner call dicts outside its builder": {
        "planner/answer_shape.py": 1,
        "planner/exclusions.py": 1,
        "planner/faithfulness.py": 1,
        "planner/filter_checks.py": 2,
        "planner/patterns/distribution_entity_value.py": 1,
        "planner/patterns/metric_by_dimension_rollup.py": 1,
        "planner/patterns/scoped_predicate_ratio.py": 1,
        "planner/plan.py": 2,
        "planner/plan_trace.py": 1,
        "planner/ranking_checks.py": 2,
    },
    "response envelopes outside the answer builder": {
        "architect_mcp.py": 4,
        "architect_service.py": 3,
        "architect_transactions.py": 2,
        "asgi.py": 1,
        "cli/output.py": 1,
        "cli/scaffold.py": 1,
        "contracts/generation.py": 1,
        "http_core.py": 4,
        "mcp.py": 6,
        "metadata_parts/valid_values.py": 1,
        "repl/home.py": 1,
        "resource_access.py": 2,
        "runtime.py": 3,
        "upgrade/service.py": 3,
    },
}

# What the hosted embedder may import; a new name is a deliberate contract change.
FACADE = frozenset(
    {
        "ABSENT_PROJECT_REVISION",
        "API_VERSION",
        "ATHENA_CONNECTION_OPTIONS",
        "ArchitectMutation",
        "ArchitectProject",
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
        "LoadedPackageSnapshot",
        "LruCompiledSqlCache",
        "MCPAdapter",
        "MCPHTTPResponse",
        "MCP_PROTOCOL_VERSION",
        "MOTHERDUCK_CONNECTION_OPTIONS",
        "POSTGRES_CONNECTION_OPTIONS",
        "PUBLIC_V1_ROUTES",
        "PackageReference",
        "PolicyContextResolver",
        "PreparedQuery",
        "RequestContext",
        "Runtime",
        "SNOWFLAKE_ADBC_CONNECTION_OPTIONS",
        "SNOWFLAKE_CLI_CONNECTION_OPTIONS",
        "SNOWFLAKE_NATIVE_CONNECTION_OPTIONS",
        "SemanticHTTPService",
        "SemanticLayerError",
        "SemanticLayerMCPAdapter",
        "SnowflakeCliAdapter",
        "SnowflakeNativeAdapter",
        "StderrAuditSink",
        "TrustedAttributes",
        "WarehouseAdapter",
        "WarehouseConnectorSpec",
        "audit_logging_enabled",
        "context_from_headers",
        "context_from_policy_context",
        "create_duckdb_adapter",
        "create_warehouse_adapter",
        "dialect_for_warehouse",
        "diff_semantic_contract",
        "emit_audit_event",
        "get_audit_sink",
        "get_policy_context_resolver",
        "handle_jsonrpc_message",
        "handle_streamable_http_request",
        "impact_report",
        "load_package_snapshot",
        "normalize_connection_options",
        "parse_config_report",
        "project_revision",
        "request_context_payload",
        "resolve_package_reference",
        "run_examples_report",
        "run_package_tests_report",
        "set_audit_sink",
        "set_policy_context_resolver",
        "supported_warehouses",
        "validate_config_report",
        "validate_runtime_package",
        "warehouse_connector",
    }
)


def _called(node: ast.AST) -> str:
    func = getattr(node, "func", None)
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _filter_op(node: ast.AST, module: str) -> bool:
    return (
        module not in FILTER_OP_OWNERS
        and isinstance(node, ast.Constant)
        and node.value in FILTER_OP_SPELLINGS
    )


def _identity(node: ast.AST, module: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and _called(node) in {"RequestContext", "context_from_policy_context"}
        and module not in INGRESS
        and not module.startswith("cli/")
    )


def _clock_read(node: ast.AST, module: str) -> bool:
    """``datetime.now()``, ``utcnow()``, ``today()``, ``date.today()`` and ``time.time()``;
    perf counters and monotonic clocks time work and are not reads of the date."""
    func = getattr(node, "func", None)
    if (
        module == "clock.py"
        or not isinstance(node, ast.Call)
        or not isinstance(func, ast.Attribute)
    ):
        return False
    base = ast.unparse(func.value)
    return (
        func.attr in {"now", "utcnow", "today"} and base.split(".")[-1] in {"datetime", "date"}
    ) or (func.attr == "time" and base == "time")


def _keys(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _planner_call(node: ast.AST, module: str) -> bool:
    return (
        module.startswith("planner/")
        and module != "planner/call.py"
        and bool(_keys(node) & CALL_KEYS)
    )


def _envelope(node: ast.AST, module: str) -> bool:
    return module != "answer.py" and {"ok", "status"} <= _keys(node)


RATCHETS: dict[str, Callable[[ast.AST, str], bool]] = {
    "filter op spellings": _filter_op,
    "identity rebuilt outside ingress": _identity,
    "wall clock read outside clock.py": _clock_read,
    "planner call dicts outside its builder": _planner_call,
    "response envelopes outside the answer builder": _envelope,
}


def _counts(matches: Callable[[ast.AST, str], bool]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        module = path.relative_to(PACKAGE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        counts[module] += sum(matches(node, module) for node in ast.walk(tree))
    return +counts


@pytest.mark.parametrize("name", sorted(RATCHETS))
def test_ratchet_never_grows(name):
    pins = PINS[name]
    grown = {
        module: f"{count} > {pins.get(module, 0)}"
        for module, count in _counts(RATCHETS[name]).items()
        if count > pins.get(module, 0)
    }
    assert grown == {}, f"{name} grew; use the owning module instead: {grown}"


def test_the_embedding_facade_does_not_widen():
    assert set(embedding.__all__) - FACADE == set()
