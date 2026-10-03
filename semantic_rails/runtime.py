"""Runtime — the programmatic entry point for the semantic layer.

:class:`Runtime` loads a package, holds the warehouse adapter and
compiled-SQL cache, and exposes the canonical query methods
(:meth:`validate`, :meth:`compile`, :meth:`query`)
plus the segment-* analogues. Every other surface (HTTP, MCP, CLI,
ASGI) wraps a Runtime. :meth:`reload` re-reads the package config
in-process so a hosted deployment can pick up YAML changes without
restarting the server.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from functools import wraps
from threading import Condition, RLock, get_ident
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .acceleration.routing import (
    AGGREGATE_ROUTING_ENV,
    aggregate_routing,
    aggregate_routing_enabled,
    parse_aggregate_routing,
)
from .ast import every_filter, normalize_query, rewrite_select_shorthand
from .cache import (
    CachedCompilation,
    CompiledSqlCache,
    LruCompiledSqlCache,
    compilation_cache_key,
    package_fingerprint,
)
from .catalog_search import CatalogSearchIndex
from .caveats import caveat_warnings
from .compiler import BoundQuery, NonAdditiveRefusal, bind_query, compile_query, read_routes
from .compiler_parts.empty_groups import (
    GUARDED_BASE,
    base_reads,
    observation_scope,
    observed_outside_filters,
    sql_nodes,
)
from .compiler_parts.paths import _leaf_time_role
from .config import (
    SEED_KIND_EXTERNAL,
    ensure_contained_package_path,
    get_package_config,
    get_package_path,
    load_package_config,
    package_root_for_source,
    project_managed_source,
    repo_root,
    resolve_repo_path,
    semantic_rails_home,
)
from .db import (
    Database,
    WarehouseAdapter,
    build_seed_database,
    create_warehouse_adapter,
    load_csv_dir_to_duckdb,
    seed_db,
    seed_digest,
)
from .db_parts.base import query_with_limits, reject_parameters
from .db_parts.duckdb_confinement import confinement_directory, require_inside
from .diagnostics import (
    enrich_diagnostic_candidates,
    enrich_expression_ast_error,
    enrich_object_not_found,
    enrich_path_not_found,
    exception_issue,
    filter_value_miss,
    history_warning_payload,
    provenance_summary,
    rewrite_warning_payload,
    semantic_issue,
)
from .dialects import dialect_for_warehouse
from .errors import SemanticLayerError, query_execution_error
from .expressions import collect_object_references, expr_to_dict
from .fanout import (
    build_hop_profile,
    entity_label,
    offered_rows,
    query_route_decisions,
    route_note,
    route_reading,
)
from .ir import ValidationReport
from .package_snapshot import LoadedPackageSnapshot, load_package_snapshot
from .policies import (
    diagnostic_hidden_object_ids,
    enforce_query_policies,
    query_policy_effects,
    row_filters_for_context,
    withheld_rank_order,
)
from .registry import Registry
from .relation_pipelines import relation_source_tables
from .renderer import render_select_for_profile
from .request_context import (
    context_from_policy_context,
    request_context_payload,
    without_trusted_attributes,
)
from .result_values import result_rows
from .runtime_parts.disclosures import mixed_time_role_warnings
from .runtime_parts.responses import (
    TIME_SHAPE_WINDOW_TOTAL,
    WINDOW_TOTAL_ASSUMPTION,
    apply_response_verbosity,
    compile_response_metadata,
    output_columns,
    resolve_sql_profile,
    resolve_verbosity,
)
from .scope import classify_question
from .seed_provenance import (
    missing_duckdb_relations,
    publish_seed_database,
    recorded_seed_digest,
)
from .segments import build_segment_query, normalize_segment, strip_segment_preview_metric
from .sql_ast import SqlCall, SqlField, SqlIdentifier, SqlSelect, SqlTableRef
from .sql_identifiers import plain_relation_parts
from .sql_preparation import PreparedQuery, checked_parameter_values

__all__ = [
    "CachedCompilation",
    "CompiledSqlCache",
    "Database",
    "LruCompiledSqlCache",
    "Registry",
    "Runtime",
    "SemanticLayerError",
    "ValidationReport",
    "WarehouseAdapter",
    "_KIND_PRESERVING_EXPRESSION_KINDS",
    "_SQL_OUTLINE_KEYWORDS",
    "_adapter_query",
    "_collect_expr_object_ids",
    "_compiled_expression_kind",
    "_compiled_warnings",
    "_crosses_boundary",
    "_date_key",
    "_debug_sql_authorized",
    "_expression_normalized_away_warnings",
    "_freshness_as_of",
    "_freshness_by_leaf",
    "_history_warnings",
    "_input_expression_kind",
    "_is_repo_managed_source",
    "_measure_validity_warnings",
    "_methodology_hints",
    "_metric_payload",
    "_normalize_query_limits",
    "_operator_allows_debug_sql",
    "_policy_context",
    "_query_execution_error_details",
    "_query_object_ids",
    "_range_intersects",
    "_scope_refusal",
    "_serialise_dropped_expression",
    "_sql_outline",
    "_sql_summary",
    "_walk_expr_payload",
    "apply_response_verbosity",
    "build_segment_query",
    "classify_question",
    "compilation_cache_key",
    "compile_query",
    "compile_response_metadata",
    "context_from_policy_context",
    "create_warehouse_adapter",
    "dialect_for_warehouse",
    "enforce_query_policies",
    "enrich_object_not_found",
    "exception_issue",
    "expr_to_dict",
    "get_package_config",
    "get_package_path",
    "history_warning_payload",
    "load_csv_dir_to_duckdb",
    "load_package_config",
    "normalize_query",
    "normalize_segment",
    "package_fingerprint",
    "package_root_for_source",
    "provenance_summary",
    "query_policy_effects",
    "repo_root",
    "request_context_payload",
    "runtime_request_scope",
    "resolve_repo_path",
    "resolve_sql_profile",
    "resolve_verbosity",
    "rewrite_warning_payload",
    "seed_db",
    "semantic_issue",
    "strip_segment_preview_metric",
]


def _non_additive_refusal_for_visibility(
    refusal: NonAdditiveRefusal, config: Any, hidden_ids: frozenset[str] | None
) -> NonAdditiveRefusal:
    """Name a key only when every key dimension is discoverable by this caller.

    Internal planning without request context keeps the generic refusal. A
    failed visibility check must also keep that refusal byte for byte.
    """
    if hidden_ids is None or not refusal.dimensions:
        return refusal
    try:
        measure = next(row for row in config.measures if row.id == refusal.details["measure_id"])
        entity = next(row for row in config.entities if row.id == measure.entity)
        key = list(measure.row_grain or entity.key or [])
        key_dimensions = [
            dimension
            for dimension in config.dimensions
            if dimension.entity == measure.entity and dimension.column in key
        ]
        if not set(refusal.columns) <= {dimension.column for dimension in key_dimensions}:
            return refusal
        if any(dimension.id in hidden_ids for dimension in key_dimensions):
            return refusal
    except Exception:  # noqa: BLE001 — diagnostics must fail closed on uncertain visibility
        return refusal
    names = ", ".join(refusal.dimensions)
    details = dict(refusal.details)
    details["key_dimensions"] = list(refusal.dimensions)
    details["recovery_hints"] = [
        {**hint, "message": hint["message"].replace("key", f"key ({names})", 1)}
        for hint in details["recovery_hints"]
    ]
    return NonAdditiveRefusal(
        str(refusal).replace("key,", f"key ({names}),", 1),
        details=details,
        columns=refusal.columns,
        dimensions=refusal.dimensions,
    )


def _enrich_runtime_error(
    exc: SemanticLayerError, config: Any, policy_context: Mapping[str, Any] | None = None
) -> SemanticLayerError:
    """Run every applicable diagnostics enricher over a runtime error.

    Each enricher is a no-op when its code doesn't match, so we can
    chain them safely. Keeping this in one place means new enrichers
    only need to be added here, not at every catch site.
    """
    hidden_ids = diagnostic_hidden_object_ids(config, policy_context)
    if isinstance(exc, NonAdditiveRefusal):
        exc = _non_additive_refusal_for_visibility(exc, config, hidden_ids)
    exc = enrich_diagnostic_candidates(exc, config, hidden_ids=hidden_ids)
    exc = enrich_object_not_found(exc, config, hidden_ids=hidden_ids)
    exc = enrich_expression_ast_error(exc, config, hidden_ids=hidden_ids)
    exc = enrich_path_not_found(exc, config, hidden_ids=hidden_ids)
    return exc


def runtime_request_scope(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Keep a complete application operation on one runtime generation.

    This decorator is intentionally public so shared planner and metadata
    operations can use the same boundary as ``Runtime`` methods. Every
    transport calls those shared functions, which keeps HTTP, MCP, and CLI
    consistent without duplicating locking at each transport edge.
    """

    @wraps(operation)
    def wrapped(runtime: Runtime, *args: Any, **kwargs: Any) -> Any:
        from .resource_access import run_authorized_operation

        with runtime.request_scope():
            return run_authorized_operation(operation, runtime, args, kwargs)

    return wrapped


class _ReentrantReadWriteGate:
    """Thread-aware shared-reader/exclusive-writer generation gate.

    Ordinary application operations are readers: they may overlap, including
    a long warehouse query and cheap catalog/planner work. Runtime mutations
    (reload, adapter/cache replacement, and close) are writers and wait for
    every in-flight reader to finish. Reader acquisition is re-entrant per
    thread so shared operations can safely call decorated Runtime methods.
    Waiting writers block *new* readers to prevent reload starvation, while a
    thread that already owns a read may re-enter to finish its operation.
    """

    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    @contextlib.contextmanager
    def read(self):
        thread_id = get_ident()
        with self._condition:
            reentrant = thread_id in self._readers or self._writer == thread_id
            while not reentrant and (self._writer is not None or self._waiting_writers):
                self._condition.wait()
            self._readers[thread_id] = self._readers.get(thread_id, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                depth = self._readers.get(thread_id, 0) - 1
                if depth:
                    self._readers[thread_id] = depth
                else:
                    self._readers.pop(thread_id, None)
                self._condition.notify_all()

    @contextlib.contextmanager
    def write(self):
        thread_id = get_ident()
        with self._condition:
            if self._writer == thread_id:
                self._writer_depth += 1
            else:
                if thread_id in self._readers:
                    raise RuntimeError(
                        "Runtime generation gate does not support read-to-write upgrades"
                    )
                self._waiting_writers += 1
                try:
                    while self._writer is not None or self._readers:
                        self._condition.wait()
                    self._writer = thread_id
                    self._writer_depth = 1
                finally:
                    self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer_depth -= 1
                if self._writer_depth == 0:
                    self._writer = None
                    self._condition.notify_all()


def _metric_payload(config, object_id: str, kind: str) -> dict[str, Any]:
    if kind == "metric":
        recipe = next((row for row in config.metric_recipes if row.id == object_id), None)
        if recipe is None:
            return {}
        compatible = list(recipe.compatible_temporal_roles) or (
            [recipe.temporal_role] if recipe.temporal_role else []
        )
        return {
            "default_temporal_role": recipe.temporal_role or (compatible[0] if compatible else ""),
            "compatible_temporal_roles": compatible,
        }
    if kind == "measure":
        measure = next((row for row in config.measures if row.id == object_id), None)
        if measure is None:
            return {}
        return {
            "default_temporal_role": measure.default_temporal_role
            or (measure.compatible_temporal_roles[0] if measure.compatible_temporal_roles else ""),
            "compatible_temporal_roles": list(measure.compatible_temporal_roles),
        }
    return {}


_LOG = logging.getLogger(__name__)


_ROUTE_ROW_KEYS = ("source_entity", "target_entity", "relationship_path")


def _hop_profile(config, compiled) -> dict[str, Any]:
    """``build_hop_profile`` under the query's own route rows, so a pair a row decided reports
    ``route_basis: query``."""
    plan = compiled["logical_plan"]
    rows = {
        (row["source_entity"], row["target_entity"]): row["relationship_path"]
        for row in compiled.get("route_decisions") or []
    }
    with query_route_decisions(rows):
        return build_hop_profile(
            config,
            root_entity=plan.root_entity,
            selected_paths=plan.selected_paths,
            candidate_paths=plan.candidate_paths,
        )


def _route_notes(config, compiled, payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One short note per entity pair the compiled query reads where the engine chose one of
    two or more routes (``fanout.route_note``): by the start's own key (ROUTE_COLOCATED_KEY,
    with the row that would make each other route the default in ``details.alternatives`` when
    it would load, else the rows it disagrees with in ``details.conflicts_with``),
    or by ``graph.path_preferences`` rows (ROUTE_RECORDED: the pair's own row, or, with
    ``details.rows``, the rows of pairs its routes walk through). The note is the code plus
    the chosen route, its relationship ids and its readable meaning; a single-route pair gets
    none.

    The pairs come, each with the route the SQL read, from the plan's root and leaf paths and
    from the paths lowering read (``compiler.read_routes``); a note names only a route its
    pair's resolution chose, so a pair the SQL read another way gets none. The minimal
    response leaves the notes out: the route is the package's own meaning for the pair, not a
    caveat on the numbers, and a pair with no such meaning is refused instead.

    A pair the query decided itself (``route_decisions``) gets ROUTE_CHOSEN_BY_QUERY instead,
    at every verbosity: the row and the basis it replaced, since the answer may differ from
    the package's.
    """
    notes: list[dict[str, Any]] = []
    decided: set[tuple[str, str]] = set()
    for row in compiled.get("route_decisions") or []:
        start, target, path = row["source_entity"], row["target_entity"], row["relationship_path"]
        decided.add((start, target))
        notes.append(
            semantic_issue(
                code="ROUTE_CHOSEN_BY_QUERY",
                message=f"{route_reading(config, start, path)} (chosen by this query)",
                severity="info",
                stage="planning",
                details={
                    "row": {key: row[key] for key in _ROUTE_ROW_KEYS},
                    "replaced": row["replaced"],
                },
                object_ids=[start, target],
            )
        )
    if resolve_verbosity(payload) == "minimal":
        return notes
    for start, target, path in read_routes(
        compiled["logical_plan"], compiled.get("route_choices") or []
    ):
        resolution = None if (start, target) in decided else route_note(config, start, target, path)
        if resolution is None:
            continue
        route = list(resolution.routes[0])
        details: dict[str, Any] = {"route": route}
        if resolution.basis == "colocated_key":
            code, how = "ROUTE_COLOCATED_KEY", "own key"
            details["alternatives"], conflicts = offered_rows(
                config, start, target, resolution.routes[1:]
            )
            if conflicts:
                details["conflicts_with"] = conflicts
        elif resolution.basis == "inherited":
            code = "ROUTE_RECORDED"
            how = "recorded for " + ", ".join(
                f"{entity_label(config, source)} → {entity_label(config, end)}"
                for source, end in resolution.rows
            )
            details["rows"] = [
                {"source_entity": source, "target_entity": end} for source, end in resolution.rows
            ]
        else:
            code, how = "ROUTE_RECORDED", "recorded route"
        notes.append(
            semantic_issue(
                code=code,
                message=f"{route_reading(config, start, route)} ({how})",
                severity="info",
                stage="planning",
                details=details,
                object_ids=[start, target],
            )
        )
    return notes


def _history_warnings(config, logical_plan) -> list[dict[str, Any]]:
    relationships = {row.id: row for row in config.relationships}
    history_paths = []
    for entity_id, path in dict(getattr(logical_plan, "selected_paths", {}) or {}).items():
        temporal_rels = [
            rel_id
            for rel_id in list(path or [])
            if relationships.get(rel_id) and relationships[rel_id].temporal_validity
        ]
        if temporal_rels:
            history_paths.append({"entity": entity_id, "relationship_ids": temporal_rels})
    if not history_paths:
        return []
    return [history_warning_payload(paths=history_paths)]


def _collect_expr_object_ids(expr_payload: dict[str, Any], config: Any = None) -> list[str]:
    return collect_object_references(expr_payload, config)


def _collect_spec_object_ids(spec: Any, config: Any = None) -> list[str]:
    return collect_object_references(spec, config)


def _query_object_ids(payload: dict[str, Any], config: Any = None) -> list[str]:
    """Authorization identities come from actual compiler binding, never strings."""
    if config is not None:
        return sorted(bind_query(config, None, payload).object_ids)
    query = normalize_query(payload)
    return collect_object_references(query.to_dict())


def _policy_context(payload: dict[str, Any]) -> dict[str, Any]:
    context = dict(payload.get("policy_context", {}) or {})
    normalized = context_from_policy_context(context).to_policy_context()
    if context.get("now") not in (None, ""):
        normalized["now"] = context.get("now")
    return normalized


_SQL_OUTLINE_KEYWORDS = (
    "with",
    "select",
    "from",
    "join",
    "left join",
    "right join",
    "inner join",
    "full join",
    "outer join",
    "where",
    "group by",
    "order by",
    "having",
    "limit",
    "union",
    "union all",
    "qualify",
)


def _sql_outline(sql: str) -> list[str]:
    """Return a coarse structural outline of a rendered SQL string.

    Lists which top-level SQL keywords appear, in order of first occurrence,
    so error consumers can tell whether the failed plan was a join, a CTE
    chain, an aggregate, etc. — without leaking column names, literals,
    table identifiers, or join predicates.
    """
    if not sql:
        return []
    lowered = sql.lower()
    seen: list[tuple[int, str]] = []
    for keyword in _SQL_OUTLINE_KEYWORDS:
        # word-boundary match so 'in' inside 'inner' / 'join' inside 'rejoin' do not pollute
        pattern = re.compile(rf"\b{re.escape(keyword)}\b")
        for match in pattern.finditer(lowered):
            seen.append((match.start(), keyword))
    seen.sort(key=lambda row: row[0])
    out: list[str] = []
    for _, keyword in seen:
        if not out or out[-1] != keyword:
            out.append(keyword)
    return out


def _sql_summary(sql: str) -> dict[str, Any]:
    """Redacted structural summary of a rendered SQL string.

    Replaces the raw SQL in error payloads by default so MNPI-tagged column
    names, table identifiers, and literal filter values do not leak through
    error envelopes into downstream logging sinks. Returns a deterministic
    sha256 plus length + outline so debugging is still tractable.
    """
    body = str(sql or "")
    return {
        "sql_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
        "sql_chars": len(body),
        "sql_outline": _sql_outline(body),
    }


def _debug_sql_authorized(payload: dict[str, Any], policy_context: dict[str, Any]) -> bool:
    """Return True when raw SQL may be included in error envelopes.

    Three conditions must all hold:

    1. The request explicitly opts in (``debug: true`` in the payload).
    2. The process operator opts in via the
       ``SEMANTIC_RAILS_ALLOW_DEBUG_SQL`` env var. This protects the
       OSS default: the bundled ``HeaderPolicyContextResolver`` lets a
       caller self-assert ``roles=['debug']`` in headers or the request
       body, so role membership alone is *not* sufficient to leak SQL.
       Operators flip this flag only after they have replaced the
       resolver with one that derives roles from authenticated identity.
    3. The resolved request context carries the ``debug`` role.

    The audit trail for raw-SQL exposure is therefore: opt-in flag in
    payload + operator-controlled env var + identity-bound role.
    """
    if not bool(payload.get("debug", False)):
        return False
    if not _operator_allows_debug_sql():
        return False
    roles_raw = policy_context.get("roles", []) if isinstance(policy_context, dict) else []
    if isinstance(roles_raw, (list, tuple, set)):
        roles = {str(item).strip().lower() for item in roles_raw}
    else:
        roles = {str(roles_raw).strip().lower()}
    return "debug" in roles


def _operator_allows_debug_sql() -> bool:
    """Honour ``SEMANTIC_RAILS_ALLOW_DEBUG_SQL`` as a boolean env flag.

    Centralised so tests can monkeypatch ``os.environ`` and the value
    is re-read on every request rather than frozen at import time.
    """
    raw = os.environ.get("SEMANTIC_RAILS_ALLOW_DEBUG_SQL", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _query_execution_error_details(
    *,
    engine: str,
    sql: str,
    payload: dict[str, Any],
    policy_context: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the `details` payload for a QUERY_EXECUTION_ERROR.

    Default (safe) mode returns a structural sha256 + outline rather than
    the rendered SQL. The full SQL is only included when the caller opts
    in via `debug: true` AND the request context has the `debug` role.
    """
    details: dict[str, Any] = {"engine": engine, **_sql_summary(sql)}
    if _debug_sql_authorized(payload, policy_context):
        details["sql"] = sql
        details["sql_debug_authorized"] = True
    else:
        details["sql_redacted"] = True
    if extra:
        for key, value in extra.items():
            if key not in {"sql", "sql_redacted", "sql_debug_authorized"}:
                # The runtime owns SQL disclosure, including its authorization flags.
                details[key] = value
    return details


def _normalize_query_limits(raw: Any, time_zone: str = "") -> dict[str, Any]:
    """Normalize the request envelope's optional `limits` block.

    Recognized keys:
      - `statement_timeout_ms` — positive int, query is aborted after N ms
      - `max_rows` — positive int, result is clipped to N rows

    Unrecognized keys are dropped silently so a future addition does not
    break existing clients. Missing or invalid values yield an empty dict
    (no enforcement). `time_zone` is not a request key: the runtime adds the
    zone the query runs in (see `_time_zone`) for the adapter.
    """
    normalized: dict[str, Any] = {"time_zone": time_zone} if time_zone else {}
    if not isinstance(raw, dict):
        return normalized
    for key in ("statement_timeout_ms", "max_rows"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            continue
        if coerced > 0:
            normalized[key] = coerced
    return normalized


# Warehouses whose adapters run each query in the zone `_time_zone` picks.
_SESSION_ZONE_WAREHOUSES = frozenset({"duckdb", "motherduck", "ducklake", "postgres"})


def _time_zone(config: Any, compiled: dict[str, Any]) -> str:
    """The zone a query runs in: its time role's, else UTC ("" if the name isn't a zone).

    DuckDB and Postgres evaluate zone-dependent SQL in the session's zone: the clock of a
    ``TIMESTAMP WITH TIME ZONE`` value, its comparison with a plain timestamp, ``now()``.
    Their adapters run each query in this zone, so a zone-aware column buckets and filters
    in the role's zone at every grain. Naive ``TIMESTAMP`` and ``DATE`` values don't
    depend on the session zone.
    """
    role_id = (compiled["logical_plan"].time or {}).get("temporal_role")
    zone = next((role.timezone for role in config.temporal_roles if role.id == role_id), "")
    zone = str(zone or "UTC").strip()
    try:
        ZoneInfo(zone)
    except (ValueError, KeyError, OSError):  # an unknown or malformed name
        return ""
    return zone


def _time_zone_warnings(config: Any, compiled: dict[str, Any]) -> list[dict[str, Any]]:
    """Name the measures bucketed on a time role whose zone the query doesn't run in."""
    plan = compiled["logical_plan"]
    zone = _time_zone(config, compiled)
    warehouse = str(config.package.warehouse or "duckdb").lower()
    if not zone or not plan.time or warehouse not in _SESSION_ZONE_WAREHOUSES:
        return []
    query = normalize_query(plan.query)
    zones = {role.id: str(role.timezone or "UTC").strip() for role in config.temporal_roles}
    roles = sorted(
        {_leaf_time_role(item.bound_measure, query, config) for item in plan.measure_plans}
    )
    others = {role: zones[role] for role in roles if zones.get(role, zone) != zone}
    if not others:
        return []
    message = (
        f"The query runs in {zone}, its time role's zone, so a TIMESTAMP WITH TIME ZONE"
        f" column on {', '.join(others)} buckets and filters in {zone}, not in its own zone."
    )
    details = {"time_zone": zone, "role_zones": others}
    return [
        {
            "code": "TIME_ZONE_NOT_APPLIED",
            "severity": "warning",
            "message": message,
            "details": details,
        }
    ]


def _adapter_query(
    adapter: Any,
    query: str | PreparedQuery,
    *,
    limits: dict[str, Any],
    policy_context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(query, PreparedQuery):
        if query.parameters:
            # No fallback: an adapter without separate value binding never
            # receives the statement, and values never enter the SQL text.
            if getattr(adapter, "supports_parameters", False) is not True:
                reject_parameters(query, adapter)
            attributes = context_from_policy_context(policy_context).attributes
            slot_values = [attributes.get(slot.attribute) for slot in query.parameters]
            values = checked_parameter_values(query, slot_values)
            try:
                return adapter.query_prepared(query, limits=limits, parameters=values)
            except Exception as exc:  # noqa: BLE001 — re-raised below without the driver's text
                failure = _unchained_failure(exc, adapter, query)
            # Raised outside the handler: driver text (a conversion error, say) can quote a
            # bound value, so it reaches neither __cause__ nor __context__.
            raise failure
        execute = getattr(adapter, "query_prepared", None)
        if execute is not None:
            return execute(query, limits=limits)
        return WarehouseAdapter.query_prepared(adapter, query, limits=limits)
    return query_with_limits(adapter, query, limits=limits)


def _unchained_failure(exc: Exception, adapter: Any, query: PreparedQuery) -> SemanticLayerError:
    engine = str(getattr(adapter, "engine", "") or "")
    root: BaseException = exc
    while root.__cause__ is not None:
        root = root.__cause__
    shape = _sql_summary(query.sql)
    _LOG.debug(
        "parameterized statement failed on %s: %s (sql_sha256=%s, outline=%s)",
        engine,
        type(root).__name__,
        shape["sql_sha256"],
        shape["sql_outline"],
    )
    if isinstance(exc, SemanticLayerError) and exc.code != "QUERY_EXECUTION_ERROR":
        return SemanticLayerError(exc.code, str(exc), details=exc.details)
    return query_execution_error({"engine": engine, "sql_redacted": True})


def _coverage_probe_query(
    warehouse: str, table: str, column: str, *, entity: str, dimension: str
) -> PreparedQuery:
    """``SELECT MIN(column), MAX(column) FROM table``, rendered as compiled SQL is.

    Both names come from package config, so each must be a plain SQL identifier;
    anything else refuses before any SQL is built.
    """
    parts = plain_relation_parts(table)
    if parts is None or ".".join(parts) != table or plain_relation_parts(column) != [column]:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "The data coverage probe reads only tables and columns with plain SQL names.",
            details={
                "reason": "probe_identifier_not_plain",
                "entity": entity,
                "dimension": dimension,
            },
        )
    value = SqlIdentifier([column])
    probe = SqlSelect(
        select=[
            SqlField(SqlCall("MIN", [value]), "min_t"),
            SqlField(SqlCall("MAX", [value]), "max_t"),
        ],
        from_table=SqlTableRef(table),
    )
    dialect = dialect_for_warehouse(warehouse)
    return dialect.prepare_query(render_select_for_profile(probe, dialect=dialect))


def _data_coverage_probe(
    adapter: Any,
    config: Any,
    *,
    warehouse: str,
    root_entity: str,
    temporal_role: str,
    limits: dict[str, Any],
) -> dict[str, str]:
    """Run a single ``SELECT MIN(<col>), MAX(<col>) FROM <table>`` probe
    to discover what time window actually has data for the root entity's
    temporal role. Used only on the zero-row + bounded-time path, where
    the agent needs to distinguish "wrong question" from "data sparse
    here" — the round-three "signal only, no probes" rule still holds
    because the original query already ran and returned nothing.

    Returns ``{"min": iso, "max": iso}`` on success; empty dict if any
    lookup fails, a name is not plain (see ``_coverage_probe_query``) or the
    probe raises. Failures are silent — coverage is a hint, not a guarantee.
    """
    try:
        entity_idx = {row.id: row for row in config.entities}
        role_idx = {row.id: row for row in config.temporal_roles}
        dim_idx = {row.id: row for row in config.dimensions}
        entity_row = entity_idx.get(root_entity)
        role_row = role_idx.get(temporal_role)
        if entity_row is None or role_row is None:
            return {}
        dim_row = dim_idx.get(role_row.dimension)
        if dim_row is None or not dim_row.column or not entity_row.table:
            return {}
        query = _coverage_probe_query(
            warehouse,
            entity_row.table,
            dim_row.column,
            entity=entity_row.id,
            dimension=dim_row.id,
        )
        rows = _adapter_query(adapter, query, limits=limits)
        if not rows:
            return {}
        first = rows[0] or {}
        min_val = first.get("min_t") or first.get("MIN_T") or first.get("MIN(min_t)")
        max_val = first.get("max_t") or first.get("MAX_T") or first.get("MAX(max_t)")
        if min_val is None and max_val is None:
            return {}
        return {
            "min": str(min_val) if min_val is not None else "",
            "max": str(max_val) if max_val is not None else "",
        }
    except Exception:  # noqa: BLE001 — coverage is a best-effort signal
        return {}


def _scope_refusal(payload: dict[str, Any]) -> SemanticLayerError | None:
    if not bool(payload.get("enforce_scope", payload.get("classify_scope", False))):
        return None
    text = str(
        payload.get("question", payload.get("intent", payload.get("text", ""))) or ""
    ).strip()
    if not text:
        return None
    classification = classify_question(text)
    if classification.category == "data_query":
        return None
    return SemanticLayerError(
        "OUT_OF_SCOPE",
        f"Request is outside the semantic layer query scope: {classification.category}",
        details={
            **classification.to_dict(),
            "unsupported_construct": classification.category,
            "why_invalid": classification.rationale,
            "missing_metadata_or_capability": "semantic layers compile governed data queries; this request needs a downstream reasoning or workflow tool",
            "suggested_query_ir_change": classification.what_the_sl_can_do_instead,
            "recommended_handoff": classification.recommended_handoff,
        },
    )


def _compiled_warnings(
    config, compiled, payload: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = [
        *(rewrite_warning_payload(step) for step in compiled["logical_plan"].rewrite_steps),
        *_history_warnings(config, compiled["logical_plan"]),
        *_measure_validity_warnings(config, compiled["logical_plan"]),
        *_stock_key_gap_warnings(compiled),
        *_route_notes(config, compiled, payload),
        *_time_zone_warnings(config, compiled),
        *mixed_time_role_warnings(config, compiled["logical_plan"]),
    ]
    if payload is not None:
        # Every check reads the canonical query the compiler saw, not the caller's shorthand.
        canonical, notes = rewrite_select_shorthand(payload)
        warnings.extend(caveat_warnings(config, compiled, canonical))
        warnings.extend(_expression_normalized_away_warnings(canonical, compiled))
        if not compiled["logical_plan"].time.get("window_total"):
            warnings.extend(_ungrained_time_projection_warnings(canonical))
        warnings.extend(_shorthand_normalized_warnings(notes))
    return warnings


def _window_total_fields(compiled) -> dict[str, Any]:
    """``assumptions`` for every response, plus ``time_shape`` when the window was collapsed."""
    if not compiled["logical_plan"].time.get("window_total"):
        return {"assumptions": []}
    return {"assumptions": [WINDOW_TOTAL_ASSUMPTION], "time_shape": TIME_SHAPE_WINDOW_TOTAL}


def _withheld_columns(policy_effects: list[dict[str, Any]]) -> set[str]:
    """The outputs excluded from both value diagnostics and public result metadata."""
    return {row["withheld_column"] for row in policy_effects if row.get("withheld_column")}


def _withhold_values(out: dict[str, Any], policy_effects: list[dict[str, Any]]) -> None:
    """Drop the column of a rank by withheld values, and name the withheld objects instead."""
    effects = [row for row in policy_effects if row.get("withheld_column")]
    if not effects:
        return
    columns = _withheld_columns(policy_effects)
    column = effects[0]["withheld_column"]
    withheld = sorted({object_id for row in effects for object_id in row["withheld_objects"]})
    if "rows" in out:
        out["rows"] = [
            {key: value for key, value in row.items() if key not in columns} for row in out["rows"]
        ]
        for key in columns:
            out["column_types"].pop(key, None)
    if "output_columns" in out:
        out["output_columns"] = [
            row for row in out["output_columns"] if row.get("field") not in columns
        ]
    out["withheld"] = withheld
    out["warnings"] = [
        *out["warnings"],
        semantic_issue(
            code="VALUES_WITHHELD",
            message=(
                f"Rows are ordered by {', '.join(withheld)}, whose values are withheld by "
                "policy and not shown; ties are ordered by the group keys."
            ),
            severity="info",
            stage="policy",
            details={"withheld_objects": withheld, "order_by": column},
            object_ids=withheld,
        ),
    ]


def _shorthand_normalized_warnings(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tell the caller which select shorthands were rewritten, with the canonical form."""
    return [
        semantic_issue(
            code="QUERY_SHORTHAND_NORMALIZED",
            message=(
                f"{note['path']} was accepted as shorthand and rewritten; next time send "
                f"{json.dumps(note['canonical'], separators=(',', ':'))}."
            ),
            severity="warning",
            stage="compile",
            path=note["path"],
            details=note,
        )
        for note in notes
    ]


_UNGRAINED_INLINE_WINDOW_KINDS = frozenset(
    {
        "prior_period",
        "rolling",
        "cumulative",
        "period_to_date",
        # conversion + offset_window carry their own time semantics
        # (event-pair anchor / per-row window); they aggregate over their
        # own clock, so the outer query.time.grain isn't needed for a
        # well-defined result.
        "conversion",
        "offset_window",
    }
)


def _contains_inline_window_kind(expr: Any) -> bool:
    """Walk an expression dict looking for any nested kind in
    ``_UNGRAINED_INLINE_WINDOW_KINDS``. Returns True if the suppression
    list matches anywhere in the tree — the outer kind (`ratio`, `case`,
    `scoped_aggregate`, arithmetic / boolean wrappers) doesn't reveal
    what's underneath.
    """
    if not isinstance(expr, dict):
        return False
    kind = str(expr.get("kind", "") or "").strip()
    if kind in _UNGRAINED_INLINE_WINDOW_KINDS:
        return True
    # Recurse into every dict-valued child + list-valued children of
    # known expression-container keys. This is deliberately permissive:
    # any nested expression we recognise as a window kind suppresses,
    # even if it's only one branch of a case/ratio. False negatives
    # (missing the suppression and firing a spurious warning) are worse
    # than false positives (suppressing on a legit nested window).
    for value in expr.values():
        if isinstance(value, dict):
            if _contains_inline_window_kind(value):
                return True
        elif isinstance(value, list):
            for item in value:
                if _contains_inline_window_kind(item):
                    return True
    return False


def _ungrained_time_projection_warnings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Warn when ``time.temporal_role`` is set without ``time.grain`` AND the
    query has no group_by AND no inline window expression that carries its
    own grain. The agent expected a scalar; without grain the planner groups
    by the raw timestamp column and returns one row per distinct value
    (5,480 rows for a scalar revenue query in the round-six benchmark).

    Narrow on purpose: legitimate timestamp-detail queries set a group_by
    or use an inline window expression, both of which suppress the warning.
    """
    time_block = dict(payload.get("time", {}) or {})
    if not time_block:
        return []
    temporal_role = str(time_block.get("temporal_role", "") or "").strip()
    grain = str(time_block.get("grain", "") or "").strip()
    if not temporal_role or grain:
        return []
    group_by = list(payload.get("group_by", []) or [])
    if group_by:
        return []
    # Inline window kinds carry their own grain (prior_period.grain,
    # rolling.window.unit, etc.) — exempt those queries. Walk nested
    # expressions because window kinds can be wrapped in ratio / case /
    # scoped_aggregate / boolean / arithmetic at any depth, and the
    # outer expression's `kind` doesn't reveal what's underneath.
    select_rows = list(payload.get("select", []) or [])
    has_inline_window = any(
        _contains_inline_window_kind(row.get("expression", {}) or {})
        for row in select_rows
        if isinstance(row, dict)
    )
    if has_inline_window:
        return []
    return [
        semantic_issue(
            code="UNGRAINED_TIME_PROJECTION",
            message=(
                "query.time.temporal_role is set without time.grain; the "
                "planner will group by the raw timestamp and return one row "
                "per distinct value. Set time.grain (e.g. 'month', 'day') for "
                "a bucketed result, or to a grain whose one calendar bucket "
                "covers the window (e.g. 'quarter') for one total."
            ),
            severity="warning",
            stage="validate",
            details={
                "temporal_role": temporal_role,
                "recovery_hints": [
                    {
                        "code": "SET_TIME_GRAIN",
                        "message": (
                            "Add time.grain to bucket the result; a grain whose "
                            "one calendar bucket covers the window returns one total."
                        ),
                        "suggested_patches": [{"add": {"time.grain": "month"}}],
                    }
                ],
            },
        )
    ]


# Expression kinds that must survive normalization intact — if the caller
# included one of these at parse time but the compiled output dropped the
# kind, that's a silent rewrite and we emit an
# ``EXPRESSION_NORMALIZED_AWAY`` warning. This is the durable defensive
# net against the "silent drop" footgun (e.g. the original Phase 4
# report where ``{kind: prior_period, offset: N}`` was stripped to a
# literal duplicate of the current-period column). Plain measure/metric
# references are intentionally omitted — they are expected to normalize
# to the same shape.
_KIND_PRESERVING_EXPRESSION_KINDS: set[str] = {
    "prior_period",
    "rolling",
    "cumulative",
    "period_to_date",
    "metric_predicate",
    "conversion",
    "distribution",
    "ratio",
    "scoped_aggregate",
    "case",
    "arithmetic",
    "binary",
    "comparison",
    "boolean",
    "call",
    "date_add",
    "in",
    "not_in",
    "nullif",
    "entity_value",
}


def _input_expression_kind(raw: Any) -> str:
    """Return the recognized top-level ``kind`` of a raw select/filter
    expression payload, or ``""`` when none applies. This is what the
    silent-drop guard compares against the compiled output.
    """
    if not isinstance(raw, dict):
        return ""
    kind = str(raw.get("kind", "")).strip()
    return kind


def _compiled_expression_kind(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("kind", "")).strip()


def _serialise_dropped_expression(raw: Any) -> dict[str, Any]:
    """Trim a raw expression payload to a compact diagnostic shape so the
    warning details payload stays small even for deeply nested inputs."""
    if not isinstance(raw, dict):
        return {"value": raw}
    keep = {
        "kind",
        "measure",
        "metric",
        "offset",
        "grain",
        "period",
        "window",
        "input",
        "entity",
        "op",
    }
    out: dict[str, Any] = {}
    for key in keep:
        if key in raw:
            out[key] = raw[key]
    return out


def _expression_normalized_away_warnings(
    payload: dict[str, Any], compiled: dict[str, Any]
) -> list[dict[str, Any]]:
    """Emit an ``EXPRESSION_NORMALIZED_AWAY`` warning for any input
    expression whose ``kind`` was recognised by the parser but did not
    survive normalization into the compiled output.

    This is intentionally narrow: it compares the top-level ``kind``
    field at each known position (``select[i].expression``,
    ``metric_filters[i].expression``, ``order_by[i].expression``).
    Plain ``{measure: ...}`` / ``{metric: ...}`` shorthand passes
    through with no ``kind`` and is not flagged. The list of guarded
    kinds is :data:`_KIND_PRESERVING_EXPRESSION_KINDS`.
    """
    warnings: list[dict[str, Any]] = []
    logical_plan = compiled.get("logical_plan")
    if logical_plan is None:
        return warnings

    # Position: select
    post_exprs: dict[str, Any] = dict(getattr(logical_plan, "post_aggregation_exprs", {}) or {})
    raw_select = list(payload.get("select", []) or [])
    for index, row in enumerate(raw_select):
        if not isinstance(row, dict):
            continue
        raw_expr = row.get("expression", {}) or {}
        original_kind = _input_expression_kind(raw_expr)
        if original_kind not in _KIND_PRESERVING_EXPRESSION_KINDS:
            continue
        alias = str(row.get("as", "")).strip()
        compiled_expr = post_exprs.get(alias, {}) if alias else None
        if compiled_expr is None and alias:
            # Alias missing entirely — clearer "dropped" signal.
            warnings.append(
                semantic_issue(
                    code="EXPRESSION_NORMALIZED_AWAY",
                    message=(
                        f"select[{index}] expression with kind={original_kind!r} did not survive normalization"
                    ),
                    severity="warning",
                    stage="compile",
                    details={
                        "dropped_expression": _serialise_dropped_expression(raw_expr),
                        "position": "select",
                        "index": index,
                        "alias": alias,
                        "reason": "alias_missing_from_compiled_output",
                    },
                )
            )
            continue
        compiled_kind = _compiled_expression_kind(compiled_expr)
        if compiled_kind != original_kind:
            warnings.append(
                semantic_issue(
                    code="EXPRESSION_NORMALIZED_AWAY",
                    message=(
                        f"select[{index}] expression with kind={original_kind!r} was normalized to kind={compiled_kind!r}"
                    ),
                    severity="warning",
                    stage="compile",
                    details={
                        "dropped_expression": _serialise_dropped_expression(raw_expr),
                        "compiled_kind": compiled_kind,
                        "position": "select",
                        "index": index,
                        "alias": alias,
                    },
                )
            )

    # Position: metric_filters — there is no direct surviving "compiled"
    # field for these in post_aggregation_exprs, so we check the
    # normalized query echoed back in the explain payload.
    normalized = getattr(getattr(logical_plan, "query", None), "get", lambda *_: None)
    if callable(normalized):
        norm_query = logical_plan.query if hasattr(logical_plan, "query") else {}
    else:
        norm_query = {}
    raw_filters = list(payload.get("metric_filters", []) or [])
    norm_filters = list(dict(norm_query or {}).get("metric_filters", []) or [])
    for index, item in enumerate(raw_filters):
        if not isinstance(item, dict):
            continue
        raw_expr = item.get("expression", {}) or {}
        original_kind = _input_expression_kind(raw_expr)
        if original_kind not in _KIND_PRESERVING_EXPRESSION_KINDS:
            continue
        compiled_item = norm_filters[index] if index < len(norm_filters) else None
        compiled_expr = (
            (compiled_item or {}).get("expression", {}) if isinstance(compiled_item, dict) else {}
        )
        compiled_kind = _compiled_expression_kind(compiled_expr)
        if compiled_kind != original_kind:
            warnings.append(
                semantic_issue(
                    code="EXPRESSION_NORMALIZED_AWAY",
                    message=(
                        f"metric_filters[{index}] expression with kind={original_kind!r} was normalized to kind={compiled_kind!r}"
                    ),
                    severity="warning",
                    stage="compile",
                    details={
                        "dropped_expression": _serialise_dropped_expression(raw_expr),
                        "compiled_kind": compiled_kind,
                        "position": "metric_filters",
                        "index": index,
                    },
                )
            )

    return warnings


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0].split(" ", 1)[0]


def _range_intersects(start: str, end: str, window_start: str, window_end: str) -> bool:
    lower_ok = not window_end or not start or start < window_end
    upper_ok = not window_start or not end or end > window_start
    return lower_ok and upper_ok


def _crosses_boundary(start: str, end: str, window_start: str, window_end: str) -> bool:
    if window_start and start and start < window_start and (not end or end > window_start):
        return True
    return bool(window_end and (not start or start < window_end) and end and end > window_end)


def _stock_key_gap_warnings(compiled) -> list[dict[str, Any]]:
    """Say when a stock answered as if each row were its own series.

    A stock whose key lacks its event- or state-time clock takes every row as a
    series, so each cell adds up every row in it. That is right for a table with one
    row per series and wrong for one that keeps snapshots, which the engine can't
    tell apart, so the answer carries the warning (an as-of clock refuses instead).
    The lowering records each such stock it reads, metric predicates included.
    """
    gaps = {
        (gap["measure_id"], gap["temporal_role"]): gap
        for gap in list(compiled.get("stock_key_gaps") or [])
    }
    return [
        semantic_issue(
            code="STOCK_SNAPSHOT_KEY_MISSING_CLOCK",
            message=(
                f"Stock measure '{gap['measure_id']}' is keyed by {gap['row_key']}, which doesn't "
                f"contain its {gap['clock_class']} clock {gap['clock_column']!r}, so each row counts "
                "as its own series and each result adds up every row in it. That's right only if "
                "the table holds one row per series (current state); if it keeps snapshots, "
                f"they were summed. {gap['fix']} With that clock declared class: as_of_time, a "
                "key without it is refused instead."
            ),
            severity="warning",
            stage="planning",
            details=gap,
            object_ids=[gap["measure_id"]],
        )
        for gap in gaps.values()
    ]


def _no_data_in_scope_warnings(
    compiled, rows, *, excluded_outputs=(), dataset: bool = False, runtime=None, payload=None
) -> list[dict[str, Any]]:
    """Say when a measure that reads 0 for empty groups had no data at all, so it read NULL.

    A sum, count or distinct count is 0 in a group with no rows only while its measure has
    data somewhere in scope; with none, every group reads NULL. A misspelled filter value
    produces exactly that, so the answer names the outputs that came back NULL on every row
    (or, when nothing came back and no time bounds explain it, every such output). One
    warning covers them all. When observation looks outside the query's filters
    (``dataset``), an empty answer says nothing about the measure's data elsewhere, and
    populated NULLs are unknown amounts unless the settlement's own probes found no data.
    """
    outputs = {
        item["output"]: item
        for item in list(compiled.get("zero_outputs") or [])
        if item["output"] not in excluded_outputs
    }
    window = compiled["logical_plan"].time
    if getattr(rows, "truncated", False) or not outputs:
        return []
    if rows:
        outputs = {
            name: item
            for name, item in outputs.items()
            if all(row.get(name) is None for row in rows)
        }
    elif dataset:
        return []
    elif window.get("start") is not None or window.get("end") is not None:
        return []  # a window with no rows is EMPTY_RESULT_WINDOW's to explain
    elif compiled["logical_plan"].query.get("metric_filters"):
        return []  # a metric filter may have removed every group that holds data
    if not outputs:
        return []
    if dataset and runtime is not None:
        from .relation_pipelines import attach_relation_ctes
        from .renderer import render_select_for_profile
        from .sql_ast import SqlField, SqlIdentifier, SqlJoin, SqlParameter, SqlSelect, SqlTableRef
        from .sql_preparation import finalize_parameters

        ctes = {cte.name: cte for cte in compiled["sql_ast"].ctes}
        guard = ctes.get(GUARDED_BASE)
        projection = ctes["projected"].query if "projected" in ctes else compiled["sql_ast"]
        if guard is not None:
            by_alias = {
                field.alias: {
                    node.parts[0]
                    for node in sql_nodes(field.expression)
                    if isinstance(node, SqlIdentifier) and node.parts[0].startswith("observed_")
                }
                for field in guard.query.select
            }
            by_output = {
                field.alias: set().union(
                    *(by_alias.get(a, set()) for a in base_reads(field.expression))
                )
                for field in projection.select
                if field.alias in outputs
            }
            probes = sorted(set().union(*by_output.values()))
            if probes:
                # Reuse exactly the observation the settlement emitted, including authored
                # conditions and row filters, rather than interpreting returned NULLs.
                probe = SqlSelect(
                    select=[SqlField(SqlIdentifier([name, "seen"]), name) for name in probes],
                    from_table=SqlTableRef(probes[0]),
                    joins=[SqlJoin("CROSS", SqlTableRef(name)) for name in probes[1:]],
                    ctes=[
                        cte
                        for name, cte in ctes.items()
                        if name in probes or name in {f"{p}_rows" for p in probes}
                    ],
                )
                probe = attach_relation_ctes(runtime._config, probe)
                dialect = dialect_for_warehouse(runtime.warehouse)
                prepared = replace(
                    dialect.prepare_query(render_select_for_profile(probe, dialect=dialect)),
                    parameters=tuple(
                        node.slot
                        for cte in probe.ctes
                        for node in sql_nodes(cte.query)
                        if isinstance(node, SqlParameter)
                    ),
                )
                prepared = finalize_parameters(prepared, runtime._config.package.connection.kind)
                with runtime._query_lock:
                    seen = _adapter_query(
                        runtime._get_adapter(),
                        prepared,
                        limits=_normalize_query_limits(
                            (payload or {}).get("limits"), _time_zone(runtime._config, compiled)
                        ),
                        policy_context=_policy_context(payload or {}),
                    )
                outputs = {
                    name: item
                    for name, item in outputs.items()
                    if not by_output.get(name)
                    or not seen
                    or any(not seen[0].get(p) for p in by_output[name])
                }
    if not outputs:
        return []
    return [
        semantic_issue(
            code="NO_DATA_IN_SCOPE",
            message=(
                f"No data in scope for {', '.join(outputs)}: nothing in this query's filters and "
                f"time window holds a value, so {'it reads' if len(outputs) == 1 else 'they read'}"
                " NULL rather than 0. A sum or count reads 0 only where its measure has data "
                "elsewhere in scope; check the filter values."
            ),
            severity="warning",
            stage="execution",
            details={"outputs": list(outputs)},
            object_ids=[measure for item in outputs.values() for measure in item["measures"]],
        )
    ]


def _filter_value_warnings(runtime: Runtime, compiled, payload) -> list[dict[str, Any]]:
    """Under the dataset scope, say when a where value matches no row of its dimension.

    There a misspelled ``product = 'appels'`` reads a confident 0, so each string ``=`` or
    ``IN`` literal gets one existence probe under warehouse equality, under
    the caller's policy context, so it never sees a row the caller's row filter hides. A miss
    reads the values the caller can see for the closest one, as package validation does. The
    ``query`` scope already reads such a filter as NULL with ``NO_DATA_IN_SCOPE``.
    """
    from .runtime_parts.limits import max_valid_values_limit

    config = runtime._config
    query = compiled["logical_plan"].query
    if observation_scope(query, config) != "dataset":
        return []
    dimensions = {row.id: row for row in config.dimensions}
    probe = {"version": 1, "select": [], "observation_scope": "query"}
    for key in ("policy_context", "limits", "request_id"):
        if key in payload:
            probe[key] = payload[key]

    def values(field: str, where: list[dict[str, Any]], limit: int) -> list[str] | None:
        try:
            result = runtime.query({**probe, "group_by": [field], "where": where, "limit": limit})
        except SemanticLayerError:
            return None
        return [str(row[field]) for row in result["rows"] if row.get(field) is not None]

    misses: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for item in every_filter(query.get("where")):
        field, raw = str(item["field"]), item.get("value")
        literals = [v for v in (raw if isinstance(raw, list) else [raw]) if isinstance(v, str)]
        op = str(item.get("op", "=")).upper()
        if not literals or field not in dimensions or op not in {"=", "IN"}:
            continue
        for literal in literals:
            found = (
                values(field, [{"field": field, "op": "=", "value": literal}], 1)
                if dimensions[field].groupable
                else None
            )
            if found is None:
                unverified.append({"dimension": field, "value": literal})
            elif not found:
                misses.append({"dimension": field, "value": literal})
    limit = max_valid_values_limit()
    known: dict[str, list[str]] = {}
    for miss in misses:
        if miss["dimension"] not in known:
            # A full page may hide the value meant, so it suggests nothing.
            page = values(miss["dimension"], [], limit) or []
            known[miss["dimension"]] = page if len(page) < limit else []
        message, suggestion = filter_value_miss(
            "This query", miss["dimension"], miss["value"], known[miss["dimension"]]
        )
        miss.update(message=message, suggestion=suggestion)
    warnings = []
    if misses:
        warnings.append(
            semantic_issue(
                code="FILTER_VALUE_NOT_FOUND",
                message="; ".join(miss.pop("message") for miss in misses),
                severity="warning",
                stage="execution",
                details={"filters": misses},
                object_ids=sorted({miss["dimension"] for miss in misses}),
            )
        )
    if unverified:
        warnings.append(
            semantic_issue(
                code="FILTER_VALUE_UNVERIFIED",
                message="; ".join(
                    f"Filter values for {field} could not be verified: "
                    + ", ".join(repr(v["value"]) for v in unverified if v["dimension"] == field)
                    for field in sorted({v["dimension"] for v in unverified})
                ),
                severity="warning",
                stage="execution",
                details={"filters": unverified},
                object_ids=sorted({v["dimension"] for v in unverified}),
            )
        )
    return warnings


def _measure_validity_warnings(config, logical_plan) -> list[dict[str, Any]]:
    time_spec = dict(getattr(logical_plan, "time", {}) or {})
    start = _date_key(time_spec.get("start"))
    end = _date_key(time_spec.get("end"))
    if not start and not end:
        return []
    measures = {row.id: row for row in config.measures}
    warnings: list[dict[str, Any]] = []
    for bound in list(getattr(logical_plan, "bound_measures", []) or []):
        measure = measures.get(bound.measure_id)
        if measure is None:
            continue
        for window in list(measure.validity_windows or []):
            window_start = _date_key(window.from_)
            window_end = _date_key(window.to)
            if (
                _crosses_boundary(start, end, window_start, window_end)
                and str(measure.cross_window_policy or "caveat").lower() != "refuse"
            ):
                warnings.append(
                    semantic_issue(
                        code="MEASURE_BOUNDARY_CROSSED",
                        message=f"Measure '{measure.id}' crosses declared validity window '{window.semantics or window.from_ or window.to}'.",
                        severity="warning",
                        stage="planning",
                        details={
                            "measure_id": measure.id,
                            "window": {
                                "from": window.from_,
                                "to": window.to,
                                "semantics": window.semantics,
                            },
                            "cross_window_policy": measure.cross_window_policy,
                        },
                        object_ids=[measure.id],
                    )
                )
        for discontinuity in list(measure.external_discontinuities or []):
            if _range_intersects(
                start, end, _date_key(discontinuity.from_), _date_key(discontinuity.to)
            ):
                warnings.append(
                    semantic_issue(
                        code="EXTERNAL_DISCONTINUITY_PRESENT",
                        message=f"Measure '{measure.id}' intersects external discontinuity '{discontinuity.what}'.",
                        severity="warning",
                        stage="planning",
                        details={
                            "measure_id": measure.id,
                            "from": discontinuity.from_,
                            "to": discontinuity.to,
                            "what": discontinuity.what,
                            "magnitude_estimate_pct": discontinuity.magnitude_estimate_pct,
                        },
                        object_ids=[measure.id],
                    )
                )
    return warnings


def _freshness_by_leaf(config, compiled) -> list[dict[str, Any]]:
    entities = {row.id: row for row in config.entities}
    aggregate_by_relation = {str(row.relation): row for row in config.aggregate_relations}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for plan in list(getattr(compiled["logical_plan"], "measure_plans", []) or []):
        entity = entities.get(plan.source_entity)
        if entity is None or not (
            entity.freshness_source or entity.freshness_sla_seconds or entity.freshness_as_of
        ):
            continue
        key = (plan.cte_name, entity.id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "leaf_id": plan.cte_name,
                "source": entity.freshness_source,
                "source_entity": entity.id,
                "relation": entity.table,
                "sla_seconds": entity.freshness_sla_seconds,
                "as_of": entity.freshness_as_of,
            }
        )
    for node in list(getattr(compiled["physical_plan"], "nodes", []) or []):
        if node.kind != "Scan":
            continue
        relation = str(node.details.get("selected_relation") or node.details.get("relation") or "")
        aggregate = aggregate_by_relation.get(relation)
        if aggregate is None or not (
            aggregate.freshness_source
            or aggregate.freshness_sla_seconds
            or aggregate.freshness_as_of
        ):
            continue
        key = (node.id, aggregate.id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "leaf_id": node.id,
                "source": aggregate.freshness_source,
                "source_entity": aggregate.source_entity,
                "relation": relation,
                "aggregate_relation_id": aggregate.id,
                "sla_seconds": aggregate.freshness_sla_seconds,
                "as_of": aggregate.freshness_as_of,
            }
        )
    return rows


def _freshness_as_of(freshness_rows: list[dict[str, Any]]) -> str:
    values = sorted(str(row.get("as_of", "") or "") for row in freshness_rows if row.get("as_of"))
    return values[0] if values else ""


def _walk_expr_payload(expr: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [expr]
    for key in (
        "left",
        "right",
        "input",
        "numerator",
        "denominator",
        "over",
        "base",
        "converted",
        "value",
        "null_value",
    ):
        child = expr.get(key)
        if isinstance(child, dict):
            rows.extend(_walk_expr_payload(child))
    for key in ("args",):
        for child in list(expr.get(key, []) or []):
            if isinstance(child, dict):
                rows.extend(_walk_expr_payload(child))
    for item in list(expr.get("whens", []) or []):
        if isinstance(item, dict):
            for key in ("when", "then"):
                child = item.get(key)
                if isinstance(child, dict):
                    rows.extend(_walk_expr_payload(child))
    return rows


def _methodology_hints(config, payload: dict[str, Any], compiled) -> list[dict[str, Any]]:
    measures = {row.id: row for row in config.measures}
    hints: list[dict[str, Any]] = []
    normalized = dict(compiled["explain"].normalized_query or {})
    has_time_grain = bool(dict(normalized.get("time") or {}).get("grain"))
    for select in list(normalized.get("select", []) or []):
        expr = dict(select.get("expression", {}) or {})
        for node in _walk_expr_payload(expr):
            measure_id = str(node.get("measure", "") or "")
            aggregation = str(node.get("aggregation", "") or "").lower()
            measure = measures.get(measure_id)
            if (
                measure is not None
                and has_time_grain
                and aggregation == "sum"
                and measure.measure_class == "semi_additive"
            ):
                hints.append(
                    {
                        "kind": "methodology_error",
                        "code": "EXPLICIT_SUM_OVER_SNAPSHOT",
                        "message": "This query explicitly sums a stock snapshot over a time grain; use the measure's natural snapshot aggregation unless a dollar-days style exposure is intended.",
                        "severity": "warning",
                        "query_patch": {
                            "select": [
                                {
                                    "as": select.get("as", ""),
                                    "expression": {
                                        "measure": measure_id,
                                        "aggregation": measure.default_aggregation,
                                    },
                                }
                            ]
                        },
                    }
                )
            if str(node.get("kind", "")) == "prior_period":
                replacement = next(
                    (
                        row
                        for row in config.metric_recipes
                        if row.meta.get("published_ttm")
                        or row.meta.get("replaces_hand_rolled_ttm")
                        or "ttm" in row.id.lower()
                    ),
                    None,
                )
                if replacement is not None:
                    hints.append(
                        {
                            "kind": "methodology_error",
                            "code": "RECOMPUTED_TTM",
                            "message": "A published TTM metric exists; prefer it over hand-rolled prior-period stitching.",
                            "severity": "warning",
                            "query_patch": {
                                "select": [
                                    {
                                        "as": select.get("as", replacement.id.split(".")[-1]),
                                        "expression": {"metric": replacement.id},
                                    }
                                ]
                            },
                        }
                    )
    if payload.get("export"):
        object_ids = _query_object_ids(payload, config)
        protected = []
        for measure in config.measures:
            if measure.id in object_ids and (
                measure.meta.get("mnpi") or measure.operational.get("mnpi")
            ):
                protected.append(measure.id)
        for recipe in config.metric_recipes:
            if recipe.id in object_ids and (
                recipe.meta.get("mnpi") or recipe.operational.get("mnpi")
            ):
                protected.append(recipe.id)
        if protected:
            hints.append(
                {
                    "kind": "methodology_error",
                    "code": "MNPI_BULK_EXPORT_DISCOURAGED",
                    "message": "The query references MNPI-tagged semantic objects and was marked for export.",
                    "severity": "warning",
                    "query_patch": {"export": False},
                    "object_ids": protected,
                }
            )
    return hints


def _is_repo_managed_source(path: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), repo_root()]) == repo_root()
    except ValueError:
        return False


class Runtime:
    def __init__(self, package_id: str, *, confine_to: str | os.PathLike[str] = ""):
        source_path = get_package_path(package_id)
        # Built-in packages (repo checkout or installed share dir) keep
        # their seeded assets under the project data/ root; anything else
        # registered by id (tests monkeypatching list_package_paths)
        # resolves relative assets against its own package root so a
        # relative default_db never lands in the repo checkout.
        self._init_loaded(
            snapshot=load_package_snapshot(source_path),
            package_id=package_id,
            source_path=source_path,
            prefer_package_root_assets=not project_managed_source(source_path),
            confine_to=confine_to,
        )

    @classmethod
    def from_path(cls, path: str) -> Runtime:
        source_path = os.path.abspath(path)
        return cls.from_snapshot(
            load_package_snapshot(source_path),
            prefer_package_root_assets=not _is_repo_managed_source(source_path),
        )

    @classmethod
    def from_config(
        cls,
        config,
        *,
        source_path: str,
        package_id: str = "",
        prefer_package_root_assets: bool | None = None,
    ) -> Runtime:
        return cls.from_snapshot(
            LoadedPackageSnapshot.from_config(config, source_path=os.path.abspath(source_path)),
            package_id=package_id,
            prefer_package_root_assets=prefer_package_root_assets,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: LoadedPackageSnapshot,
        *,
        package_id: str = "",
        prefer_package_root_assets: bool | None = None,
        confine_to: str | os.PathLike[str] = "",
    ) -> Runtime:
        source_path = snapshot.source_path
        if prefer_package_root_assets is None:
            # Same rule as Runtime(package_id): registered packages in the repo
            # checkout or the installed share root keep the shared data/ root.
            managed = project_managed_source if package_id else _is_repo_managed_source
            prefer_package_root_assets = not managed(source_path)
        prefer_assets = prefer_package_root_assets
        runtime = cls.__new__(cls)
        runtime._init_loaded(
            snapshot=snapshot,
            package_id=package_id,
            source_path=source_path,
            prefer_package_root_assets=prefer_assets,
            confine_to=confine_to,
        )
        return runtime

    def _init_loaded(
        self,
        *,
        snapshot: LoadedPackageSnapshot,
        package_id: str,
        source_path: str,
        prefer_package_root_assets: bool,
        confine_to: str | os.PathLike[str] = "",
    ) -> None:
        self._confine_to = confinement_directory(confine_to) if confine_to else ""
        self._snapshot = snapshot
        config = snapshot.config
        self.package_id = package_id or config.package.package_id
        self.source_path = source_path
        self.package_root = package_root_for_source(source_path)
        self.prefer_package_root_assets = prefer_package_root_assets
        self._config: Any = config
        self.registry = Registry(snapshot.config)
        self.warehouse = str(self._config.package.warehouse or "duckdb").strip().lower() or "duckdb"
        self.db_path = (
            self._resolve_asset_path(self._config.package.default_db, kind="default_db")
            if self.warehouse == "duckdb"
            else ""
        )
        self.adapter: WarehouseAdapter | None = None
        self._seed_warnings: list[dict[str, Any]] = []
        self._catalog_cache: dict[str, Any] | None = None
        self._catalog_search_index: CatalogSearchIndex | None = None
        self._resolve_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._explain_cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = RLock()
        # Coordinates config/registry generations without serializing normal
        # traffic. Complete application operations take a shared read; reload,
        # adapter/cache replacement, and close take the exclusive write side.
        # The gate is re-entrant because planner/metadata/segment helpers call
        # decorated Runtime operations internally.
        self._state_gate = _ReentrantReadWriteGate()
        # Warehouse drivers commonly expose one mutable connection/cursor
        # per adapter. ASGI may offload several requests concurrently, so
        # adapter creation, execution, replacement, and close are serialized
        # until a future session-pool abstraction can provide per-task handles.
        self._query_lock = RLock()
        self._package_fingerprint = snapshot.source_fingerprint
        self._compile_cache: CompiledSqlCache = LruCompiledSqlCache(
            maxsize=int(os.environ.get("SEMANTIC_RAILS_COMPILE_CACHE_SIZE", "512"))
        )
        self._aggregate_routing = parse_aggregate_routing(os.environ.get(AGGREGATE_ROUTING_ENV, ""))
        # Lazily loaded on first access; None = not yet looked up,
        # False = looked up and absent/stale (don't retry this call).
        self._manifest: dict[str, Any] | None | bool = None

    @property
    def snapshot(self) -> LoadedPackageSnapshot:
        return self._snapshot

    @property
    def config(self):
        """An isolated configuration view; use from_config or reload to replace semantics."""
        return self.snapshot.config

    @contextlib.contextmanager
    def request_scope(self):
        """Pin a complete shared operation to the current runtime state.

        The lock is re-entrant because planner and metadata operations can
        call governed ``Runtime`` methods internally. Reload therefore waits
        for the outer application operation, not merely for its eventual
        validate/compile/query call.
        """

        with self._state_gate.read(), aggregate_routing(self._aggregate_routing):
            yield self

    def set_compile_cache(self, cache: CompiledSqlCache) -> None:
        """Swap the compiled-SQL cache backend.

        Operators can inject a process-local implementation for custom
        eviction or instrumentation. ``CachedCompilation`` is a typed Python
        object, not a distributed serialization contract; see
        :mod:`semantic_rails.cache`. The runtime continues to keep the cache
        as the underscore-prefixed `_compile_cache` attribute internally.

        Validates that `cache` implements the `CompiledSqlCache` protocol
        (`get` and `put`) at injection time. Raises `TypeError` for any
        object that doesn't conform, so a misconfigured deployment fails
        fast at startup instead of corrupting the cache at request time.
        """
        if not isinstance(cache, CompiledSqlCache):
            raise TypeError(
                "compile_cache must implement the CompiledSqlCache protocol "
                "(get(key) -> Optional[CachedCompilation], put(key, value) "
                f"-> None). Got: {type(cache).__name__}"
            )
        with self._state_gate.write(), self._cache_lock:
            self._compile_cache = cache

    def set_aggregate_routing(self, enabled: bool) -> None:
        """Turn routing to declared rollups on or off for this runtime's next requests.

        Off, every measure leaf runs on the base tables and each rollup it considered is reported
        as ``aggregate_routing_off``. The compile cache keys on the switch, so cached plans
        follow it. The initial value comes from ``SEMANTIC_RAILS_AGGREGATE_ROUTING`` (``on`` or
        ``off``; default ``on``). Anything but a bool raises ``TypeError``. It doesn't wait for
        requests in flight: each request reads the switch once, when it starts.
        """
        if not isinstance(enabled, bool):
            raise TypeError(f"set_aggregate_routing takes True or False, not {enabled!r}")
        self._aggregate_routing = enabled

    def _resolve_asset_path(self, value: str, *, kind: str) -> str:
        if not value:
            return value
        # Re-checked here (not only at YAML parse time) so configs built
        # programmatically via Runtime.from_config get the same containment.
        ensure_contained_package_path(value, field=f"package.{kind}")
        if os.path.isabs(value):
            return value
        package_candidate = os.path.abspath(os.path.join(self.package_root, value))
        repo_candidate = resolve_repo_path(value)
        if kind == "default_db":
            if self.prefer_package_root_assets:
                return package_candidate
            if not _is_repo_managed_source(self.package_root):
                # An installed bundled package builds its database in the user's cache, not
                # beside the installed code (maybe read-only; uninstall would leave it behind).
                cache = os.path.join(semantic_rails_home(), "cache", self.package_id, __version__)
                return os.path.join(cache, value)  # per version: installs may ship other seeds
            return repo_candidate
        if self.prefer_package_root_assets and os.path.exists(package_candidate):
            return package_candidate
        if not os.path.exists(repo_candidate) and os.path.exists(package_candidate):
            return package_candidate
        return repo_candidate

    def _expected_tables(self) -> set[str]:
        # An entity over a relation pipeline reads a CTE the compiler builds;
        # the stored tables it needs are the pipeline's own sources. Declared
        # aggregate relations are stored tables the compiler may route to.
        pipeline_outputs = {row.output_name for row in self._config.relations}
        stored = {
            str(row.table)
            for row in self._config.entities
            if str(row.table).strip() and not row.relation_id
        }
        stored |= {
            str(row.relation)
            for row in self._config.aggregate_relations
            if str(row.relation).strip() and row.relation not in pipeline_outputs
        }
        pipelines = {row.relation_id for row in self._config.entities if row.relation_id}
        return stored | relation_source_tables(self._config, pipelines)

    def _ensure_db(self) -> None:
        """Create a missing seed database, but never replace an existing file.

        Probe existing files in a fresh process. DuckDB can return an older
        in-process catalog after another process replaces a path, and closing a
        second connection here can release a serving connection's POSIX lock.
        """
        if self.warehouse != "duckdb":
            return
        if self._confine_to:
            self.db_path = require_inside(self._confine_to, self.db_path, option="database path")
        seed = self._config.package.seed
        for _attempt in range(2):
            if os.path.islink(self.db_path) and not os.path.exists(self.db_path):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"package.default_db '{self.db_path}' is a symbolic link to a file that "
                    "does not exist; Semantic Rails does not build a database through a "
                    "broken link. Fix or remove the link.",
                    details={"default_db": self.db_path, "reason": "default_db_broken_link"},
                )
            if not os.path.exists(self.db_path):
                if self._confine_to:
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        "A confined Runtime requires an existing database; build it first.",
                        details={"reason": "duckdb_confined_default_db_missing"},
                    )
                if seed.kind == SEED_KIND_EXTERNAL:
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"package.default_db '{self.db_path}' does not exist. The package declares "
                        "package.seed.kind: external, so Semantic Rails never creates this database; "
                        "build it first (for example with `dbt build`).",
                        details={
                            "default_db": self.db_path,
                            "reason": "external_default_db_missing",
                        },
                    )
                try:
                    self._publish_seed(self._seed_source())
                except SemanticLayerError as exc:
                    if exc.details.get("reason") != "default_db_created_concurrently":
                        raise
                # Whether this process or another one created the file, check
                # its actual catalog before the runtime serves it.
                continue
            try:
                missing = missing_duckdb_relations(
                    self.db_path, self._expected_tables(), confine_to=self._confine_to
                )
            except Exception as exc:  # noqa: BLE001 — any uncertain probe fails closed
                raise self._unreadable_db_error() from exc
            if missing:
                raise self._missing_db_relations_error(missing)
            return
        raise SemanticLayerError(
            "CONFIG_CONFLICT",
            f"package.default_db '{self.db_path}' kept changing while its seed was built; retry",
            details={"default_db": self.db_path, "reason": "default_db_created_concurrently"},
        )

    def _seed_source(self) -> str:
        """The seed source's resolved path; a missing one is a clear INVALID_CONFIG."""
        seed = self._config.package.seed
        src = self._resolve_asset_path(seed.source, kind="seed_source")
        if not os.path.exists(src):
            # _resolve_asset_path falls back to the repo root when neither
            # candidate exists, so a raw FileNotFoundError would show a
            # path the author never wrote. Name the field and both
            # locations checked instead.
            package_candidate = os.path.abspath(os.path.join(self.package_root, seed.source))
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"package.seed.source '{seed.source}' not found — looked for "
                f"'{package_candidate}' (relative to the package) and "
                f"'{src}'; create the file or fix package.seed.source",
            )
        return src

    def _publish_seed(self, src: str) -> None:
        seed = self._config.package.seed
        tmp_path = build_seed_database(
            self.db_path,
            kind=seed.kind,
            source=src,
            post_sql=self._resolve_asset_path(seed.post_sql, kind="post_sql"),
            null_strings=seed.null_strings,
            package_id=self._config.package.package_id,
        )
        try:
            publish_seed_database(tmp_path, self.db_path)
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)

    def _stale_seed_warnings(self) -> list[dict[str, Any]]:
        """Warn when this package's seed built the open database from other seed files."""
        seed = self._config.package.seed
        if self.warehouse != "duckdb" or seed.kind == SEED_KIND_EXTERNAL:
            return []
        recorded = recorded_seed_digest(self.adapter, self._config.package.package_id)
        if not recorded:
            return []
        try:
            post_sql = self._resolve_asset_path(seed.post_sql, kind="post_sql")
            current = seed_digest(seed.kind, self._seed_source(), post_sql)
        except (OSError, SemanticLayerError):  # no seed files to compare against
            return []
        if current == recorded:
            return []
        message = (
            f"package.default_db '{self.db_path}' was built before its seed files changed, "
            "so queries return the old data. To rebuild it from the current seed, delete "
            f"the file and rerun: rm {shlex.quote(self.db_path)}"
        )
        return [
            {
                "code": "STALE_SEED_DATABASE",
                "severity": "warning",
                "message": message,
                "details": {"default_db": self.db_path, "reason": "seed_files_changed"},
            }
        ]

    def _unreadable_db_error(self) -> SemanticLayerError:
        return SemanticLayerError(
            "INVALID_CONFIG",
            f"package.default_db '{self.db_path}' exists but could not be opened as a DuckDB "
            "database; another process (for example a running `dbt build`) may be writing it. "
            "Semantic Rails never replaces a file it cannot read. If another tool builds this "
            "database, declare package.seed.kind: external; otherwise stop the process holding "
            "it, or delete the file to rebuild it from the seed.",
            details={"default_db": self.db_path, "reason": "default_db_unreadable"},
        )

    def _missing_db_relations_error(self, missing: list[str]) -> SemanticLayerError:
        shown = ", ".join(missing[:5])
        if len(missing) > 5:
            shown += f", and {len(missing) - 5} more"
        return SemanticLayerError(
            "INVALID_CONFIG",
            f"package.default_db '{self.db_path}' lacks relations the package reads ({shown}). "
            "Semantic Rails never replaces an existing database during runtime validation. "
            "If another tool (such as dbt) owns it, declare package.seed.kind: external and "
            "build the missing relations there. For a disposable database built from this "
            "package's seed, stop its users, back up any data you need, then explicitly delete "
            "the file so the next bootstrap can create it. The former "
            "SEMANTIC_RAILS_ALLOW_DB_RESEED flag no longer enables automatic replacement.",
            details={
                "default_db": self.db_path,
                "missing_relations": missing,
                "reason": "default_db_missing_relations",
            },
        )

    def close(self) -> None:
        with self._state_gate.write(), self._query_lock:
            if self.adapter is not None:
                self.adapter.close()
                self.adapter = None

    def reload(self) -> dict[str, Any]:
        """Re-read the package config from disk, rebuild the registry, and
        clear in-memory caches. Returns a small diagnostic of what changed.

        Intended for long-running hosted deployments that push package
        config edits without a process restart. Safe to call concurrently
        with serving traffic: ordinary requests share a generation read gate,
        while reload takes its exclusive write side. In-flight requests finish
        entirely on the old generation; reload then swaps config/registry and
        subsequent requests use the refreshed state.

        The OSS local runtime does not call this on its own — invocation
        is the operator's choice, which keeps the standalone developer
        experience identical to before. A hosting layer can wire this
        behind an admin endpoint or a config-push signal.
        """
        if not self.source_path:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Runtime has no source_path; cannot reload package config.",
            )
        with self._state_gate.write(), self._query_lock, self._cache_lock:
            previous_fingerprint = self._package_fingerprint
            new_snapshot = load_package_snapshot(self.source_path)
            new_config = new_snapshot.config
            new_fingerprint = new_snapshot.source_fingerprint
            # Replace cached state. Drop the adapter so it reconnects with
            # the refreshed connection config on the next query.
            if self.adapter is not None:
                # noqa: BLE001 — adapter close must not block reload
                with contextlib.suppress(Exception):
                    self.adapter.close()
                self.adapter = None
            self._snapshot = new_snapshot
            self._config = new_config
            self.registry = Registry(new_snapshot.config)
            self.warehouse = (
                str(self._config.package.warehouse or "duckdb").strip().lower() or "duckdb"
            )
            self.db_path = (
                self._resolve_asset_path(self._config.package.default_db, kind="default_db")
                if self.warehouse == "duckdb"
                else ""
            )
            self._catalog_cache = None
            self._catalog_search_index = None
            self._resolve_cache = {}
            self._explain_cache = {}
            self._manifest = None
            self._package_fingerprint = new_fingerprint
            # Preserve an operator-injected cache backend. Cache keys include
            # the package fingerprint, so prior-generation entries cannot be
            # reused after reload and can expire under the backend's policy.
        return {
            "package_id": self.package_id,
            "source_path": self.source_path,
            "previous_fingerprint": previous_fingerprint,
            "package_fingerprint": new_fingerprint,
            "semantic_fingerprint": new_snapshot.semantic_fingerprint,
            "changed": previous_fingerprint != new_fingerprint,
        }

    @property
    def warehouse_engine(self) -> str:
        return self.warehouse

    def _get_adapter(self) -> WarehouseAdapter:
        with self._query_lock:
            if self.adapter is None:
                if self.warehouse == "duckdb":
                    self._ensure_db()
                self.adapter = create_warehouse_adapter(
                    self._config.package, db_path=self.db_path, confine_to=self._confine_to
                )
                self._seed_warnings = self._stale_seed_warnings()
            return self.adapter

    def set_adapter(self, adapter: WarehouseAdapter) -> None:
        """Inject a pre-built :class:`WarehouseAdapter`.

        Hosted operators use this to plug a per-tenant adapter (built from
        credentials resolved out of a vault) into the runtime instead of
        letting :func:`create_warehouse_adapter` construct one from the
        package's declared connection. The setter is the natural
        counterpart to :meth:`set_compile_cache` — narrow, additive, and
        the same shape of injection seam.

        Closes any previously held adapter so callers can swap mid-life
        without leaking connections. Raises ``TypeError`` if ``adapter``
        is not a :class:`WarehouseAdapter`.
        """
        if not isinstance(adapter, WarehouseAdapter):
            raise TypeError(
                f"adapter must be a WarehouseAdapter instance. Got: {type(adapter).__name__}"
            )
        with self._state_gate.write(), self._query_lock, self._cache_lock:
            if self.adapter is not None and self.adapter is not adapter:
                with contextlib.suppress(Exception):
                    self.adapter.close()
            self.adapter = adapter
            self._seed_warnings = []

    @runtime_request_scope
    def manifest_catalog(self, *, view: str, verbosity: str) -> dict[str, Any] | None:
        """Return a precomputed catalog payload if a matching manifest exists.

        Returns ``None`` when no manifest is present, when the manifest is
        stale relative to current sources, when the requested ``(view,
        verbosity)`` pair was not precomputed, or when the env override
        ``SR_DEV_NO_MANIFEST`` is set. Callers fall back to live compute.

        The manifest is loaded lazily on first call. To guarantee that
        downstream mutation (transport layers, middleware) cannot poison
        the in-memory cache, each variant is stored as a JSON string and
        re-parsed per call. ``json.loads`` of a few-MB payload is ~1ms —
        still a >100x speedup over live compute — and gives true
        isolation between requests.
        """
        from .manifest import get_catalog_json, load_manifest

        with self._cache_lock:
            if self._manifest is None:
                loaded = load_manifest(self.source_path, snapshot=self.snapshot)
                self._manifest = loaded if loaded is not None else False
        manifest = self._manifest
        if not isinstance(manifest, dict):
            return None
        encoded = get_catalog_json(manifest, view, verbosity)
        if encoded is None:
            return None
        return json.loads(encoded)

    @runtime_request_scope
    def catalog(self) -> dict[str, Any]:
        with self._cache_lock:
            if self._catalog_cache is None:
                dialect = dialect_for_warehouse(self.warehouse)
                self._catalog_cache = {
                    "package": asdict(self._config.package),
                    "objects": [asdict(obj) for obj in self.registry.list_objects()],
                    "compiler": {
                        "dialect": dialect.name,
                        "warehouse_capabilities": dialect.capabilities(),
                        "runtime_expression_kinds": [
                            "metric_ref",
                            "measure_ref",
                            "scoped_aggregate",
                            "ratio",
                            "arithmetic",
                            "metric_predicate",
                            "entity_value",
                            "distribution",
                        ],
                        "aggregate_relation_candidates": [
                            asdict(row) for row in self._config.aggregate_relations
                        ],
                        "physical_plan_nodes": [
                            "Scan",
                            "Filter",
                            "Project",
                            "Join",
                            "Aggregate",
                            "SemiJoin",
                            "AlignFacts",
                            "SnapshotSelect",
                            "Window",
                            "FinalProject",
                        ],
                    },
                }
            return deepcopy(self._catalog_cache)

    @runtime_request_scope
    def _get_catalog_search_index(self) -> CatalogSearchIndex:
        """Return the immutable text index for the current config generation.

        The enclosing plan/discover operation pins the runtime generation with
        ``request_scope``.  The cache lock only coordinates the first builder;
        policy-sensitive visibility and availability are evaluated later for
        every request and are never stored in this index.
        """

        with self._cache_lock:
            if self._catalog_search_index is None:
                self._catalog_search_index = CatalogSearchIndex.from_config(self._config)
            return self._catalog_search_index

    def resolve(self, term: str, *, kind: str = "") -> dict[str, Any]:
        key = (term, kind)
        with self._cache_lock:
            if key not in self._resolve_cache:
                resolved = self.registry.resolve(term, kind=kind)
                obj = dict(resolved.get("object", {}) or {})
                obj_kind = str(obj.get("kind", ""))
                if obj_kind in {"metric", "measure"}:
                    payload = dict(obj.get("payload", {}) or {})
                    payload.update(_metric_payload(self._config, str(obj.get("id", "")), obj_kind))
                    obj["payload"] = payload
                    resolved = {**resolved, "object": obj}
                self._resolve_cache[key] = deepcopy(resolved)
            return deepcopy(self._resolve_cache[key])

    def _query_fingerprint(self, payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload, sort_keys=True, default=str)

    def _compile(
        self,
        payload: dict[str, Any],
        *,
        policy_context: dict[str, str],
        binding: BoundQuery | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        normalized = (
            binding.plan.query
            if binding is not None
            else normalize_query(payload, config=self._config).to_dict()
        )
        key = compilation_cache_key(
            package_hash=self._package_fingerprint,
            normalized_query=normalized,
            warehouse=self.warehouse,
            relation_profile=str(
                self._config.package.connection.name or self._config.package.default_db or ""
            ),
            render_profile=str(
                payload.get("sql_profile", payload.get("render_profile", "audit")) or "audit"
            ),
            policy_context=dict(policy_context),
            aggregate_routing=aggregate_routing_enabled(),
        )
        # Lock policy: hold the cache lock only across the in-memory get/put
        # operations. The compile itself (compile_query) runs unlocked so that
        # concurrent requests with different cache keys do not serialize behind
        # a single in-flight cold compile. Two threads racing on the same key
        # will each compile and the last writer wins — duplicate work but no
        # correctness issue (the LRU cache already deep-copies on get and put,
        # see cache.py:33,37).
        # A certification can be revoked between requests, so a package with a rollup that
        # requires one compiles every request.
        cacheable = not any(row.requires_certification for row in self._config.aggregate_relations)
        with self._cache_lock:
            cached = self._compile_cache.get(key) if cacheable else None
        if cached is not None:
            stats = {
                **dict(cached.compiled.get("compile_stats", {}) or {}),
                "cache_hit": True,
                "cache_lookup_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            return {
                **cached.compiled,
                "compile_stats": stats,
                "explain": replace(cached.compiled["explain"], compile_stats=stats),
            }
        compiled = compile_query(self._config, self.registry, payload, binding=binding)
        stats = {
            **dict(compiled.get("compile_stats", {}) or {}),
            "cache_hit": False,
            "cache_lookup_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        compiled = {
            **compiled,
            "compile_stats": stats,
            "explain": replace(compiled["explain"], compile_stats=stats),
        }
        if cacheable:
            with self._cache_lock:
                self._compile_cache.put(key, CachedCompilation(compiled=compiled))
        return compiled

    @runtime_request_scope
    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        verbosity = resolve_verbosity(payload)
        sql_profile = resolve_sql_profile(payload)
        policy_context = _policy_context(payload)
        try:
            refusal = _scope_refusal(payload)
            if refusal is not None:
                raise refusal
            binding = self._bind(payload, policy_context)
            object_ids = binding.object_ids
            policy_effects = enforce_query_policies(
                self._config,
                object_ids,
                environment=str(policy_context.get("environment", "")),
                audience=str(policy_context.get("audience", "")),
                roles=policy_context.get("roles", []),
                query=payload,
                binding=binding,
            )
            compiled = self._compile(payload, policy_context=policy_context, binding=binding)
            report = ValidationReport(
                version=2,
                ok=True,
                warnings=[],
                disabled_options=list(compiled["logical_plan"].disabled_options),
                logical_plan=asdict(compiled["logical_plan"]),
                explain=asdict(compiled["explain"]),
            )
            out = asdict(report)
            freshness_rows = _freshness_by_leaf(self._config, compiled)
            out["status"] = "ok"
            out["warnings"] = _compiled_warnings(self._config, compiled, payload)
            out["errors"] = []
            out["query"] = without_trusted_attributes(payload)
            out["normalized_query"] = compiled["explain"].normalized_query
            out["recovery_hints"] = []
            out.update(_window_total_fields(compiled))
            out["methodology_hints"] = _methodology_hints(self._config, payload, compiled)
            out["freshness_by_leaf"] = freshness_rows
            out["freshness_as_of"] = _freshness_as_of(freshness_rows)
            out["policy_effects"] = policy_effects
            out["request_context"] = request_context_payload(policy_context)
            out["provenance_summary"] = provenance_summary(
                self._config, compiled["logical_plan"], policy_effects=policy_effects
            )
            metadata = compile_response_metadata(self, payload, compiled)
            # validate has historically returned a curated subset of the
            # heavy metadata envelope. Honour the gating decisions encoded
            # in `compile_response_metadata`: only forward keys it produced.
            validate_keep = {
                "sql_profile",
                "warehouse",
                "dialect",
                "warehouse_capabilities",
                "output_columns",
                "semantic_summary",
                "trace",
            }
            out.update({key: value for key, value in metadata.items() if key in validate_keep})
            _withhold_values(out, policy_effects)
            if verbosity == "full":
                out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
                out["performance_plan"] = asdict(compiled["performance_plan"])
            out["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return apply_response_verbosity(
                out, verbosity=verbosity, sql_profile=sql_profile, kind="validate"
            )
        except SemanticLayerError as exc:
            exc = _enrich_runtime_error(exc, self._config, policy_context)
            issue = exception_issue(exc, stage="validate")
            report = ValidationReport(
                version=2,
                ok=False,
                errors=[issue],  # type: ignore[list-item]  # exception_issue returns dict; asdict serializes either shape
                disabled_options=list(exc.details.get("disabled_options", [])),
            )
            out = asdict(report)
            out["status"] = "error"
            out["query"] = without_trusted_attributes(payload)
            out["warnings"] = []
            out["recovery_hints"] = list(issue.get("recovery_hints", []))
            out["authoring_hints"] = list(exc.details.get("authoring_hints", []) or [])
            out["query_ir_hints"] = list(
                exc.details.get("query_ir_hints", list(issue.get("recovery_hints", [])) or [])
            )
            out["assumptions"] = []
            out["methodology_hints"] = []
            out["freshness_by_leaf"] = []
            out["freshness_as_of"] = ""
            out["policy_effects"] = list(exc.details.get("policy_effects", []) or [])
            out["request_context"] = request_context_payload(policy_context)
            out["provenance_summary"] = {
                "root_entity": "",
                "time": dict(payload.get("time", {}) or {}),
                "rewrite_status": "",
                "selected_paths": [],
                "policy_effects": out["policy_effects"],
            }
            out["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return apply_response_verbosity(
                out, verbosity=verbosity, sql_profile=sql_profile, kind="validate"
            )

    @runtime_request_scope
    def compile(self, payload: dict[str, Any]) -> dict[str, Any]:
        refusal = _scope_refusal(payload)
        if refusal is not None:
            raise refusal
        verbosity = resolve_verbosity(payload)
        sql_profile = resolve_sql_profile(payload)
        policy_context = _policy_context(payload)
        try:
            binding = self._bind(payload, policy_context)
            object_ids = binding.object_ids
            policy_effects = enforce_query_policies(
                self._config,
                object_ids,
                environment=str(policy_context.get("environment", "")),
                audience=str(policy_context.get("audience", "")),
                roles=policy_context.get("roles", []),
                query=payload,
                binding=binding,
            )
            compiled = self._compile(payload, policy_context=policy_context, binding=binding)
        except SemanticLayerError as exc:
            raise _enrich_runtime_error(exc, self._config, policy_context) from exc
        freshness_rows = _freshness_by_leaf(self._config, compiled)
        out = {
            "ok": True,
            "status": "ok",
            "errors": [],
            "warnings": _compiled_warnings(self._config, compiled, payload),
            "recovery_hints": [],
            "authoring_hints": [],
            "query_ir_hints": [],
            **_window_total_fields(compiled),
            "methodology_hints": _methodology_hints(self._config, payload, compiled),
            "freshness_by_leaf": freshness_rows,
            "freshness_as_of": _freshness_as_of(freshness_rows),
            "policy_effects": policy_effects,
            "request_context": request_context_payload(policy_context),
            "provenance_summary": provenance_summary(
                self._config, compiled["logical_plan"], policy_effects=policy_effects
            ),
            "hop_profile": _hop_profile(self._config, compiled),
            "query": without_trusted_attributes(payload),
            "normalized_query": compiled["explain"].normalized_query,
            "logical_plan": asdict(compiled["logical_plan"]),
            "sql_plan": asdict(compiled["sql_ast"]),
            "explain": asdict(compiled["explain"]),
            **compile_response_metadata(self, payload, compiled),
        }
        _withhold_values(out, policy_effects)
        return apply_response_verbosity(
            out, verbosity=verbosity, sql_profile=sql_profile, kind="compile"
        )

    @runtime_request_scope
    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        refusal = _scope_refusal(payload)
        if refusal is not None:
            raise refusal
        verbosity = resolve_verbosity(payload)
        sql_profile = resolve_sql_profile(payload)
        policy_context = _policy_context(payload)
        try:
            binding = self._bind(payload, policy_context)
            object_ids = binding.object_ids
            policy_effects = enforce_query_policies(
                self._config,
                object_ids,
                environment=str(policy_context.get("environment", "")),
                audience=str(policy_context.get("audience", "")),
                roles=policy_context.get("roles", []),
                query=payload,
                binding=binding,
            )
            compiled = self._compile(payload, policy_context=policy_context, binding=binding)
        except SemanticLayerError as exc:
            raise _enrich_runtime_error(exc, self._config, policy_context) from exc
        freshness_rows = _freshness_by_leaf(self._config, compiled)
        # Per-request resource limits (statement_timeout_ms, max_rows) flow
        # from the request envelope through to the warehouse adapter. Hosted
        # operators use this to enforce per-tenant policies without forking;
        # local users typically leave `limits` unset.
        limits = _normalize_query_limits(payload.get("limits"), _time_zone(self._config, compiled))
        limit = compiled["sql_ast"].limit
        probe = compiled.get("limit_probe")
        # A resource fence that hides the boundary row takes precedence over
        # tie detection. Keep both the adapter cap and its truncation signal.
        if probe is not None and limits.get("max_rows", 0) and limits["max_rows"] <= limit:
            probe = None
        # If the caller asked for a statement_timeout_ms but the adapter
        # can't honor it at the warehouse boundary, surface a warning so
        # the caller learns the limit was best-effort. Without this, the
        # request silently completes on a runaway query and only the
        # `max_rows` post-fetch fence clips the result — the v2 audit
        # called out the "half-fake contract" smell on the DuckDB path.
        limits_warnings: list[dict[str, Any]] = []
        tie_warnings: list[dict[str, Any]] = []
        try:
            with self._query_lock:
                # Keep adapter selection and execution in one critical
                # section. Otherwise set_adapter()/reload() can close the
                # selected connection in the gap before query execution.
                adapter = self._get_adapter()
                if (
                    limits
                    and limits.get("statement_timeout_ms")
                    and not getattr(adapter, "supports_statement_timeout", False)
                ):
                    limits_warnings.append(
                        {
                            "code": "STATEMENT_TIMEOUT_NOT_HONORED",
                            "severity": "warning",
                            "message": (
                                f"statement_timeout_ms not honored by adapter "
                                f"'{getattr(adapter, 'engine', '')}'. The query "
                                "ran to completion; max_rows still applied as a "
                                "post-fetch fence."
                            ),
                            "details": {
                                "engine": getattr(adapter, "engine", ""),
                                "statement_timeout_ms": int(
                                    limits.get("statement_timeout_ms", 0) or 0
                                ),
                            },
                        }
                    )
                rows = _adapter_query(
                    adapter,
                    probe if probe is not None else compiled["prepared_query"],
                    limits=limits,
                    policy_context=policy_context,
                )
            if probe is not None:
                from .top_n import limit_rows

                rows, tie_warnings = limit_rows(rows, limit, compiled["limit_order_keys"])
        except Exception as exc:
            if isinstance(exc, SemanticLayerError) and exc.code != "QUERY_EXECUTION_ERROR":
                raise
            raise query_execution_error(
                _query_execution_error_details(
                    engine=self.warehouse_engine,
                    sql=compiled["sql"],
                    payload=payload,
                    policy_context=policy_context,
                    extra=exc.details if isinstance(exc, SemanticLayerError) else None,
                ),
            ) from exc
        out: dict[str, Any] = {
            "ok": True,
            **result_rows(
                rows,
                output_columns=output_columns(self._config, compiled),
                zone=_time_zone(self._config, compiled),
            ),
            "row_count": len(rows),
            "truncated": bool(getattr(rows, "truncated", False)),
            "rendered_sql": compiled["sql"],
            "logical_plan": asdict(compiled["logical_plan"]),
            "sql_plan": asdict(compiled["sql_ast"]),
            "explain": asdict(compiled["explain"]),
            "status": "ok",
            "errors": [],
            "warnings": [
                *_compiled_warnings(self._config, compiled, payload),
                *_no_data_in_scope_warnings(
                    compiled,
                    rows,
                    excluded_outputs=_withheld_columns(policy_effects),
                    dataset=observed_outside_filters(compiled["logical_plan"].query, self._config),
                    runtime=self,
                    payload=payload,
                ),
                *_filter_value_warnings(self, compiled, payload),
                *limits_warnings,
                *tie_warnings,
                *self._seed_warnings,
            ],
            "recovery_hints": [],
            **_window_total_fields(compiled),
            "methodology_hints": _methodology_hints(self._config, payload, compiled),
            "freshness_by_leaf": freshness_rows,
            "freshness_as_of": _freshness_as_of(freshness_rows),
            "policy_effects": policy_effects,
            "request_context": request_context_payload(policy_context),
            "provenance_summary": provenance_summary(
                self._config, compiled["logical_plan"], policy_effects=policy_effects
            ),
            "hop_profile": _hop_profile(self._config, compiled),
            "query": without_trusted_attributes(payload),
            "normalized_query": compiled["explain"].normalized_query,
        }
        metadata = compile_response_metadata(self, payload, compiled)
        execute_keep = {
            "semantic_fingerprint",
            "source_fingerprint",
            "sql_profile",
            "warehouse",
            "dialect",
            "warehouse_capabilities",
            "output_columns",
            "semantic_summary",
            "trace",
        }
        out.update({key: value for key, value in metadata.items() if key in execute_keep})
        # Data-sparseness diagnostic: when a query returns zero rows AND
        # the request applied a time filter, the most common cause is
        # "data not present in this window" — not a query bug. The
        # reviewer's cross-clock conversion returned 0 rows because the
        # storefront_session fact only spans Sept 2016, but there was
        # no signal to distinguish that from a silent row-drop. Surface
        # the applied time bounds and point at inspect for freshness.
        if not rows:
            time_block = dict(payload.get("time", {}) or {})
            # Time block accepts ``{start, end}`` or a relative ``range``
            # (see ast.py:_SUPPORTED_TIME_KEYS). Either signal counts as
            # an applied filter for the diagnostic.
            has_time_filter = (
                time_block.get("start")
                or time_block.get("end")
                or time_block.get("range")
                or time_block.get("temporal_role")
            )
            if has_time_filter and policy_context.get("metric_allowlist") is None:
                root_entity = ""
                provenance = out.get("provenance_summary") or {}
                if isinstance(provenance, dict):
                    root_entity = str(provenance.get("root_entity", "") or "")
                if not root_entity:
                    selected_paths = dict(
                        getattr(compiled.get("logical_plan"), "selected_paths", {}) or {}
                    )
                    if selected_paths:
                        root_entity = str(next(iter(selected_paths.keys()), "") or "")
                inspect_target = root_entity or str(time_block.get("temporal_role", "") or "")
                applied = {
                    "temporal_role": str(time_block.get("temporal_role", "") or ""),
                    "grain": str(time_block.get("grain", "") or ""),
                }
                # Echo whichever interval shape was supplied so the
                # agent can see exactly what bounds applied.
                for key in ("start", "end", "range"):
                    if time_block.get(key):
                        applied[key] = time_block[key]
                # Probe the root entity's time column for actual data
                # coverage. Best-effort: failure returns {} and the warning
                # still ships. The probe only fires on the zero-row path
                # (we already paid for the original query) so the
                # round-three "signal only, no probes" rule still holds.
                # The probe reads the whole relation, so a row-filtered query skips it.
                actual_data_coverage: dict[str, str] = {}
                if not compiled["prepared_query"].parameters:
                    with self._query_lock:
                        actual_data_coverage = _data_coverage_probe(
                            self._get_adapter(),
                            self._config,
                            warehouse=self.warehouse,
                            root_entity=root_entity,
                            temporal_role=str(time_block.get("temporal_role", "") or ""),
                            limits=limits,
                        )
                # Resolve requested_window from whichever shape was supplied.
                # When the agent uses `range.last`, the payload only has the
                # relative window — show the resolved absolute bounds so the
                # agent can see WHERE the filter actually landed (a relative
                # window against historical data ends up far outside the
                # data range, the original cause of the blind-agent's
                # silent 0-rows on Q2).
                normalized_time = dict(
                    (compiled["explain"].normalized_query or {}).get("time", {}) or {}
                )
                requested_window: dict[str, Any] = {
                    "start": str(time_block.get("start") or normalized_time.get("start") or ""),
                    "end": str(time_block.get("end") or normalized_time.get("end") or ""),
                }
                if time_block.get("range"):
                    requested_window["relative_range"] = dict(time_block.get("range") or {})
                out["data_diagnostics"] = {
                    "rows_returned": 0,
                    "applied_time_filter": applied,
                    "root_entity": root_entity,
                    "actual_data_coverage": actual_data_coverage,
                    "requested_window": requested_window,
                    "hint": (
                        "Zero rows can mean the data isn't present in this "
                        "time window rather than a query bug. Call "
                        f"inspect({inspect_target!r}) to check freshness and "
                        "row-count bounds; widen the time interval or remove "
                        "filters to confirm."
                    ),
                }
                # Push a structured warning onto ``out['warnings']`` so
                # agents that read warnings (but not data_diagnostics)
                # still see the signal. Sibling shape to the existing
                # STATEMENT_TIMEOUT_NOT_HONORED warning emitted higher
                # up in this method.
                out["warnings"].append(
                    {
                        "code": "EMPTY_RESULT_WINDOW",
                        "severity": "warning",
                        "message": (
                            "Query returned zero rows under the applied "
                            "time filter. The data may not exist in this "
                            f"window; inspect({inspect_target!r}) reveals "
                            "freshness and row-count bounds."
                        ),
                        "details": {
                            "applied_time_filter": applied,
                            "root_entity": root_entity,
                            "actual_data_coverage": actual_data_coverage,
                            "requested_window": requested_window,
                        },
                    }
                )
        _withhold_values(out, policy_effects)
        if verbosity == "full":
            out["physical_plan"] = asdict(compiled["physical_plan"])
            out["performance_plan"] = asdict(compiled["performance_plan"])
            out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
        return apply_response_verbosity(
            out, verbosity=verbosity, sql_profile=sql_profile, kind="execute"
        )

    def _bind(self, payload: dict[str, Any], policy_context: dict[str, Any]) -> BoundQuery:
        filters = row_filters_for_context(self._config, policy_context)

        def check_policies(option: dict[str, Any], binding: BoundQuery) -> None:
            # A child-scope reading is offered only if this request's policy gate passes it.
            enforce_query_policies(
                self._config,
                binding.object_ids,
                environment=str(policy_context.get("environment", "")),
                audience=str(policy_context.get("audience", "")),
                roles=policy_context.get("roles", []),
                query=option,
                binding=binding,
            )

        def bind(query: dict[str, Any]) -> BoundQuery:
            return bind_query(
                self._config,
                self.registry,
                query,
                row_filters=filters,
                check_policies=check_policies,
            )

        binding = bind(payload)
        # A rank by a withheld value breaks its ties by the group keys, in the same direction.
        return withheld_rank_order(
            self._config,
            binding,
            rebind=bind,
            environment=str(policy_context.get("environment", "")),
            audience=str(policy_context.get("audience", "")),
            roles=policy_context.get("roles", []),
        )

    def _segment_policy_effects(
        self, segment_id: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return enforce_query_policies(
            self._config,
            [segment_id],
            environment=str(context.get("environment", "")),
            audience=str(context.get("audience", "")),
            roles=context.get("roles", []),
        )

    @runtime_request_scope
    def segment_validate(
        self, segment_id: str, *, policy_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        context = context_from_policy_context(policy_context).to_policy_context()
        try:
            segment_policy_effects = self._segment_policy_effects(segment_id, context)
            normalized = normalize_segment(self._config, segment_id)
            derived_query = build_segment_query(normalized, include_preview_dimensions=True)
            if context:
                derived_query["policy_context"] = context
            validation = self.validate(derived_query)
            validation.update(
                {
                    "segment": {
                        "id": normalized.id,
                        "entity": normalized.entity,
                        "basis_metric": normalized.basis_metric,
                    },
                    "normalized_segment": normalized.to_dict(),
                    "derived_query": without_trusted_attributes(derived_query),
                    "segment_policy_effects": segment_policy_effects,
                    "request_context": request_context_payload(context),
                }
            )
            # Validation type-checks booleans and numbers only; for text, ids, dates and times only
            # the warehouse can say a value fits its column (text for a BOOLEAN column fails there).
            typed = {"boolean", "integer", "number"}
            untyped_ids = {row.id for row in self._config.dimensions if row.data_type not in typed}
            unchecked = sorted({str(item.get("field")) for item in normalized.where} & untyped_ids)
            if validation.get("ok") and unchecked:
                validation.setdefault("warnings", []).append(
                    {
                        "code": "SEGMENT_VALUES_UNCHECKED",
                        "severity": "warning",
                        "message": (
                            f"Membership values on text, id, date or time dimensions "
                            f"({', '.join(unchecked)}) are checked in the warehouse only: preview "
                            "the segment, or run "
                            "`semantic-rails project validate --mode runtime` (REPL: "
                            "`validate runtime`)."
                        ),
                        "details": {"segment_id": normalized.id, "dimensions": unchecked},
                    }
                )
            validation["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return validation
        except SemanticLayerError as exc:
            exc = _enrich_runtime_error(exc, self._config, context)
            # Route through `exception_issue` so the soft-fail envelope
            # carries the same `recovery_hints` + `closest_matches` +
            # `severity/stage/object_ids/...` fields that the MCP error
            # surface produces. Otherwise segment-validate degrades to
            # a thin `{code, message, details}` envelope while peer tools
            # ship the rich version.
            issue = exception_issue(exc, stage="segment_validate")
            report = ValidationReport(
                version=2,
                ok=False,
                # raw dict instead of ValidationIssue; asdict serializes either shape
                errors=[issue],  # type: ignore[list-item]
            )
            out = asdict(report)
            out["segment"] = {"id": segment_id}
            out["request_context"] = request_context_payload(context)
            out["recovery_hints"] = list(issue.get("recovery_hints", []) or [])
            out["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return out

    @runtime_request_scope
    def segment_explain(
        self, segment_id: str, *, policy_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        context = context_from_policy_context(policy_context).to_policy_context()
        segment_policy_effects = self._segment_policy_effects(segment_id, context)
        normalized = normalize_segment(self._config, segment_id)
        derived_query = build_segment_query(normalized, include_preview_dimensions=True)
        if context:
            derived_query["policy_context"] = context
        explained = self.compile(derived_query)
        explained.update(
            {
                "segment": {
                    "id": normalized.id,
                    "entity": normalized.entity,
                    "basis_metric": normalized.basis_metric,
                },
                "normalized_segment": normalized.to_dict(),
                "derived_query": without_trusted_attributes(derived_query),
                "segment_policy_effects": segment_policy_effects,
                "request_context": request_context_payload(context),
            }
        )
        return explained

    @runtime_request_scope
    def segment_preview(
        self,
        segment_id: str,
        *,
        limit: int = 50,
        policy_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Preview rows are caller-controlled on every transport; cap them so
        # a remote caller can't force an arbitrarily large warehouse scan.
        # Operators can raise the ceiling via the env var.
        max_rows = int(os.environ.get("SEMANTIC_RAILS_MAX_SEGMENT_PREVIEW_ROWS", "0") or 0)
        limit = max(1, min(int(limit), max_rows if max_rows > 0 else 1_000))
        context = context_from_policy_context(policy_context).to_policy_context()
        segment_policy_effects = self._segment_policy_effects(segment_id, context)
        normalized = normalize_segment(self._config, segment_id)
        preview_query = build_segment_query(
            normalized, include_preview_dimensions=True, limit=limit
        )
        membership_query = build_segment_query(normalized, include_preview_dimensions=False)
        if context:
            preview_query["policy_context"] = context
            membership_query["policy_context"] = context
        query_policy_effects: list[dict[str, Any]] = []
        for derived in (preview_query, membership_query):
            for effect in enforce_query_policies(
                self._config,
                _query_object_ids(derived, self._config),
                environment=str(context.get("environment", "")),
                audience=str(context.get("audience", "")),
                roles=context.get("roles", []),
                query=derived,
            ):
                if effect not in query_policy_effects:
                    query_policy_effects.append(effect)
        filters = row_filters_for_context(self._config, context)
        preview_compiled = compile_query(
            self._config, self.registry, preview_query, row_filters=filters
        )
        membership_compiled = compile_query(
            self._config, self.registry, membership_query, row_filters=filters
        )
        dialect = dialect_for_warehouse(self.warehouse)
        count_shell = dialect.prepare_query(
            'SELECT COUNT(*) AS "member_count" FROM (__SR_MEMBERSHIP__) AS "segment_members"'
        )
        count_prepared = PreparedQuery(
            count_shell.sql.replace("__SR_MEMBERSHIP__", membership_compiled["sql"]),
            parameters=membership_compiled["prepared_query"].parameters,
        )
        adapter = self._get_adapter()
        try:
            with self._query_lock:
                rows = _adapter_query(
                    adapter,
                    preview_compiled["prepared_query"],
                    limits={"time_zone": _time_zone(self._config, preview_compiled)},
                    policy_context=context,
                )
                count_rows = _adapter_query(
                    adapter,
                    count_prepared,
                    limits={"time_zone": _time_zone(self._config, membership_compiled)},
                    policy_context=context,
                )
        except Exception as exc:
            if isinstance(exc, SemanticLayerError) and exc.code != "QUERY_EXECUTION_ERROR":
                raise
            raise query_execution_error(
                _query_execution_error_details(
                    engine=self.warehouse_engine,
                    sql=preview_compiled["sql"],
                    payload={},
                    policy_context=context,
                    extra={
                        **(exc.details if isinstance(exc, SemanticLayerError) else {}),
                        "segment_id": segment_id,
                    },
                )
            ) from exc
        visible_rows = strip_segment_preview_metric(list(rows))
        member_count = int(count_rows[0].get("member_count", 0)) if count_rows else 0
        return {
            "segment": {
                "id": normalized.id,
                "entity": normalized.entity,
                "basis_metric": normalized.basis_metric,
            },
            "normalized_segment": normalized.to_dict(),
            "member_key_dimensions": list(normalized.member_key_dimensions),
            "preview_dimensions": list(normalized.preview_dimensions),
            **result_rows(
                visible_rows,
                output_columns=output_columns(self._config, preview_compiled),
                zone=_time_zone(self._config, preview_compiled),
            ),
            "preview_row_count": len(visible_rows),
            "member_count": member_count,
            "policy_effects": [*segment_policy_effects, *query_policy_effects],
            "request_context": request_context_payload(context),
            "derived_query": without_trusted_attributes(preview_query),
            "rendered_sql": preview_compiled["sql"],
            "count_sql": count_prepared.sql,
            "semantic_fingerprint": self.snapshot.semantic_fingerprint,
            "source_fingerprint": self.snapshot.source_fingerprint,
            "logical_plan": asdict(preview_compiled["logical_plan"]),
            "sql_plan": asdict(preview_compiled["sql_ast"]),
            "explain": asdict(preview_compiled["explain"]),
            "query": without_trusted_attributes(preview_query),
            "normalized_query": preview_compiled["explain"].normalized_query,
        }
