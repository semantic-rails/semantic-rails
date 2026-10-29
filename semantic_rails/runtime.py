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
import json
import logging
import os
import re
import shlex
import time
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from threading import Condition, RLock, get_ident
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__, operators, visible_view
from .acceleration.routing import (
    AGGREGATE_ROUTING_ENV,
    aggregate_routing,
    aggregate_routing_enabled,
    parse_aggregate_routing,
)
from .answer import (
    _answer_now,
    _freshness_as_of,
    _freshness_by_leaf,
    _hop_profile,
    _methodology_hints,
    _metric_payload,
    _policy_context,
    _query_execution_error_details,
    _sql_summary,
    _time_zone,
    _withheld_columns,
    _withhold_values,
    answer_fields,
    finish,
    load_log_name,
    mark_partial_periods,
    read_from,
)
from .ast import (
    every_filter,
    is_child_group,
    normalize_query,
)
from .cache import (
    CachedCompilation,
    CompiledSqlCache,
    LruCompiledSqlCache,
    compilation_cache_key,
)
from .catalog_search import CatalogSearchIndex
from .clock import format_now, parse_now
from .compiler import (
    BoundQuery,
    NonAdditiveRefusal,
    _object_default_query_temporal_role,
    bind_query,
    compile_query,
)
from .compiler import value_page as page_values
from .compiler_parts.empty_groups import (
    CoverageClock,
    observation_scope,
    observed_outside_filters,
)
from .compiler_parts.paths import dimensions_over_column, text_dimension
from .compiler_parts.total_rows import call_rows
from .config import (
    SEED_KIND_EXTERNAL,
    ensure_contained_package_path,
    get_package_path,
    package_root_for_source,
    project_managed_source,
    repo_root,
    resolve_repo_path,
    semantic_rails_home,
)
from .db import (
    WarehouseAdapter,
    build_seed_database,
    create_warehouse_adapter,
    seed_digest,
    temporal_cast_error_value,
)
from .db_parts.base import QueryRows, query_with_limits, reject_parameters
from .db_parts.duckdb_confinement import confinement_directory, require_inside
from .db_parts.duckdb_setup import require_duckdb
from .diagnostics import (
    enrich_column_expression,
    enrich_expression_ast_error,
    enrich_object_not_found,
    enrich_path_not_found,
    exception_issue,
    filter_value_miss,
    lean_issue,
    provenance_summary,
    semantic_issue,
)
from .dialects import dialect_for_warehouse
from .errors import SemanticLayerError, query_execution_error
from .ir import ValidationReport
from .package_snapshot import LoadedPackageSnapshot, load_package_snapshot
from .path_gap import (
    call_measures,
    lookalike_swaps,
    lookalikes_off,
    renamed_columns,
    swapped_call,
)
from .policies import (
    enforce_query_policies,
    kept_column_names,
    row_filters_for_context,
    withheld_rank_order,
)
from .policy_rules import check_request_environment
from .query_call import TOTAL_OVER, read_call, read_joined_call, route_options
from .registry import Registry
from .relation_pipelines import relation_source_tables
from .renderer import render_with_slots
from .request_context import (
    RequestContext,
    context_from_policy_context,
    policy_identity,
    request_context_payload,
    without_trusted_attributes,
)
from .result_values import result_rows
from .route_groups import grouped_refusal
from .runtime_parts.answer_warnings import (
    _covered_through,
    _guard_probe_names,
    _history_result_warnings,
    _requested_window,
    refuse_closing_day_zone,
)
from .runtime_parts.assumptions import call_assumptions, refuse_hidden_governors, split_hidden
from .runtime_parts.filtered_series import filtered_series_warnings
from .runtime_parts.responses import (
    apply_response_verbosity,
    compile_response_metadata,
    counted_outputs,
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
from .sql_preparation import PreparedQuery, checked_parameter_values, finalize_parameters


def _named_key_refusal(refusal: NonAdditiveRefusal, config: Any) -> NonAdditiveRefusal:
    """Name the key when every key column is a dimension of the caller's view."""
    if not refusal.dimensions:
        return refusal
    try:
        measure = next(row for row in config.measures if row.id == refusal.details["measure_id"])
        entity = next(row for row in config.entities if row.id == measure.entity)
        key = list(measure.row_grain or entity.key_columns)
        key_dimensions = [
            dimension
            for dimension in config.dimensions
            if dimension.entity == measure.entity and dimension.column in key
        ]
        if not set(refusal.columns) <= {dimension.column for dimension in key_dimensions}:
            return refusal
    except Exception:  # noqa: BLE001 — a key that cannot be checked is not named
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


_SELECT_PATH = re.compile(r"\bselect\[(\d+)\]")


def _caller_select_paths(exc: SemanticLayerError, notes: Any) -> SemanticLayerError:
    """The error with each ``select[i]`` the engine wrote as the caller's index: a dimension
    the call lifted from select into group_by (a call note) is not in the engine's select."""
    lifted = sorted(
        int(match.group(1))
        for note in notes or []
        if "group_by" in (note.get("canonical") or {})
        and (match := re.fullmatch(r"select\[(\d+)\]", str(note.get("path", ""))))
    )
    if not lifted:
        return exc

    def caller(match: re.Match[str]) -> str:
        index = int(match.group(1))
        for at in lifted:
            index += at <= index
        return f"select[{index}]"

    def remap(value: Any) -> Any:
        if isinstance(value, str):
            return _SELECT_PATH.sub(caller, value)
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, dict):
            return {key: remap(item) for key, item in value.items()}
        return value

    return SemanticLayerError(exc.code, remap(str(exc)), details=remap(dict(exc.details or {})))


def _enrich_runtime_error(
    exc: SemanticLayerError, config: Any, notes: Any = ()
) -> SemanticLayerError:
    """Run every applicable diagnostics enricher over a runtime error, on the caller's view,
    with its select paths at the caller's indexes (``notes``, the call's).

    Each enricher is a no-op when its code doesn't match, so we can
    chain them safely. Keeping this in one place means new enrichers
    only need to be added here, not at every catch site.
    """
    exc = visible_view.public_error(exc, config)
    if isinstance(exc, NonAdditiveRefusal):
        exc = _named_key_refusal(exc, config)
    exc = _caller_select_paths(exc, notes)
    exc = enrich_object_not_found(exc, config)
    exc = enrich_expression_ast_error(exc, config)
    # The caller the request pinned; without one, a hint assumes every policy's caller.
    view = visible_view.pinned()
    caller = view.policy_context if view is not None else None
    scope = None
    if caller is not None:
        scope = {key: caller.get(key, "") for key in ("environment", "audience")}
        scope["roles"] = caller.get("roles", ())
    exc = enrich_column_expression(exc, config, lambda: kept_column_names(config, scope))
    exc = enrich_path_not_found(exc, config)
    # A route refusal leaves the engine here: its options grouped as the answer shows them.
    return grouped_refusal(exc, config)


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


_LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def _routes_offered(call: Any) -> Iterator[None]:
    """An ``AMBIGUOUS_PATH`` refusal offers one complete call per route option."""
    try:
        yield
    except SemanticLayerError as exc:
        if exc.code == "AMBIGUOUS_PATH":
            route_options(call, exc.details)
        raise


# A seed call's keys in the engine's names (``having`` and entity filters read as
# ``metric_filters``, ``routes`` as ``route_decisions``).
_SEED_KEYS = {
    "select": "select",
    "group_by": "group_by",
    "where": "where",
    "having": "metric_filters",
    "time": "time",
    "order_by": "order_by",
    "limit": "limit",
    "routes": "route_decisions",
    "observation_scope": "observation_scope",
}


def _context_mapping(context: Any) -> dict[str, Any]:
    """A request's identity as the engine's policy mapping (empty for a local caller)."""
    if context is None:
        return {}
    if isinstance(context, RequestContext):
        return context.to_policy_context()
    return context_from_policy_context(dict(context)).to_policy_context()


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
    warehouse: str,
    table: str,
    column: str,
    *,
    entity: str,
    dimension: str,
    visibility: Any = None,
    connection_kind: str = "",
) -> PreparedQuery:
    """``SELECT MIN(column), MAX(column) FROM table``, rendered as compiled SQL is, and held to
    the request's row filters (``visibility``) as every statement it runs is.

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
    if visibility is not None:
        from .row_filters import apply_row_filters

        probe = apply_row_filters(probe, visibility)
    dialect = dialect_for_warehouse(warehouse)
    rendered, slots = render_with_slots(probe, dialect=dialect)
    return finalize_parameters(
        replace(dialect.prepare_query(rendered), parameters=slots), connection_kind
    )


def _data_coverage_probe(
    adapter: Any,
    config: Any,
    *,
    warehouse: str,
    root_entity: str,
    temporal_role: str,
    limits: dict[str, Any],
    visibility: Any = None,
    policy_context: Mapping[str, Any] | None = None,
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
            visibility=visibility,
            connection_kind=config.package.connection.kind if visibility is not None else "",
        )
        rows = _adapter_query(adapter, query, limits=limits, policy_context=policy_context)
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


def _compiled_cte_row(runtime, compiled, payload, columns):
    """Read the settlement's own CTEs (``columns``: each CTE's columns), preserving bound row
    filters and request limits."""
    from .sql_ast import SqlField, SqlIdentifier, SqlJoin, SqlSelect, SqlTableRef

    probes = sorted(columns)
    probe = SqlSelect(
        select=[
            SqlField(SqlIdentifier([name, column]), f"{name}__{column}")
            for name in probes
            for column in columns[name]
        ],
        from_table=SqlTableRef(probes[0]),
        joins=[SqlJoin("CROSS", SqlTableRef(name)) for name in probes[1:]],
        ctes=[
            cte
            for cte in [*compiled["sql_ast"].ctes, *compiled.get("balance_coverage", ())]
            if cte.name in probes or cte.name in {f"{p}_rows" for p in probes}
        ],
    )
    return _probe_row(runtime, compiled, payload, probe)


def _probe_row(runtime, compiled, payload, probe) -> dict[str, Any]:
    """The first row of ``probe``, run as the answer's own SQL is: held to its row filters
    again (after its relation CTEs are attached), its slots read from its render, and the
    request's limits kept."""
    from .relation_pipelines import attach_relation_ctes
    from .row_filters import apply_row_filters

    probe = attach_relation_ctes(runtime._config, probe)
    visibility = compiled.get("row_filter_visibility")
    if visibility is not None:
        probe = apply_row_filters(probe, visibility)
    dialect = dialect_for_warehouse(runtime.warehouse)
    rendered, slots = render_with_slots(probe, dialect=dialect)
    prepared = replace(dialect.prepare_query(rendered), parameters=slots)
    prepared = finalize_parameters(prepared, runtime._config.package.connection.kind)
    with runtime._query_lock:
        rows = _adapter_query(
            runtime._get_adapter(),
            prepared,
            limits=_normalize_query_limits(
                (payload or {}).get("limits"), _time_zone(runtime._config, compiled)
            ),
            policy_context=_policy_context(payload or {}),
        )
    return rows[0] if rows else {}


def _closing_reads(runtime, compiled, payload) -> list[dict[str, Any]]:
    """For each balance read on its closing day, its probe's row (keys case-folded), or no
    counts when the read failed (a policy's refusal is raised). Each distinct probe runs once,
    whatever reads share it."""
    rows: list[tuple[Any, dict[str, Any]]] = []
    reads = []
    for read in compiled.get("closing_reads") or ():
        row = next((row for probe, row in rows if probe == read["probe"]), None)
        if row is None:
            try:
                row = _probe_row(runtime, compiled, payload, read["probe"])
            except Exception as exc:
                if isinstance(exc, SemanticLayerError) and exc.code == "POLICY_DENIED":
                    raise
                row = {}
            rows.append((read["probe"], row))
        counts = {key.casefold(): value for key, value in row.items()}
        reads.append({**counts, **{key: read[key] for key in ("measure", "outputs", "nested")}})
    return reads


def _coverage_read(runtime, compiled, payload) -> dict[str, Any]:
    """The loaded coverage the answer's guard read, once: each coverage CTE's first and last
    loaded bucket and the instant (UTC) of its last row at or before ``now``, and each declared
    edge's first uncertified bucket and loaded-through bound.

    A declared edge's ``data_through`` is the last instant it certifies (UTC), with its
    ``edge_source`` (``loaded`` or ``now``) and how the answer names it (``declared``); a
    coverage CTE that only declared edges read keeps its last row as ``last_row`` instead.
    ``{}`` when the SQL reads no coverage or the read fails: a failed disclosure read never
    changes the answer or its warnings, but a policy's refusal of it is raised.
    """
    by_alias, _ = _guard_probe_names(compiled, "coverage_")
    edges, _ = _guard_probe_names(compiled, "edge_")
    balances = [item["probes"] for item in compiled.get("balance_edges", ())]
    probes = set().union(*by_alias.values(), *balances)
    if not probes:
        return {}
    columns = {
        **dict.fromkeys(probes, ("loaded_from", "loaded_to", "data_through")),
        **dict.fromkeys(
            set().union(*edges.values()), ("loaded_before", "loaded_through", "edge_source")
        ),
    }
    try:
        coverage = _compiled_cte_row(runtime, compiled, payload, columns)
    except Exception as exc:
        if isinstance(exc, SemanticLayerError) and exc.code == "POLICY_DENIED":
            raise
        return {}
    zone = ZoneInfo(_time_zone(runtime._config, compiled) or "UTC")
    ctes = {cte.name: cte.query for cte in compiled["sql_ast"].ctes}
    for edge in set().union(*edges.values()):
        through = _wall_time(coverage.get(f"{edge}__loaded_through"))
        coverage[f"{edge}__data_through"] = (
            (through - timedelta(microseconds=1))
            .replace(tzinfo=zone)
            .astimezone(UTC)
            .replace(tzinfo=None)
            if through is not None
            else None
        )
        coverage[f"{edge}__declared"] = (
            load_log_name(runtime._config, ctes[f"{edge}_rows"])
            if coverage.get(f"{edge}__edge_source") == "loaded"
            else "now"
        )
    for probe in set().union(*by_alias.values()):
        if all(edges.get(alias) for alias, names in by_alias.items() if probe in names):
            coverage[f"{probe}__last_row"] = coverage.pop(f"{probe}__data_through", None)
    return coverage


def _wall_time(value: Any) -> datetime | None:
    """A declared edge's wall time as a warehouse returns it: a timestamp, a date (its
    midnight) or ISO text; None for anything else (no edge is claimed)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).replace(tzinfo=None)
        except ValueError:
            return None
    return None


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
        _, by_output = _guard_probe_names(compiled, "observed_")
        probes = sorted(set().union(*(by_output.get(name, set()) for name in outputs)))
        if probes:
            seen = _compiled_cte_row(runtime, compiled, payload, dict.fromkeys(probes, ("seen",)))
            outputs = {
                name: item
                for name, item in outputs.items()
                if not by_output.get(name)
                or not seen
                or any(not seen.get(f"{p}__seen") for p in by_output[name])
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


# A filter value list can be long: the missing-value warning names this many and lists this many.
_NAMED_MISSES = 5
_LISTED_MISSES = 10


def _filter_value_warnings(runtime: Runtime, compiled, payload) -> list[dict[str, Any]]:
    """Probe string filter literals that can otherwise read a confident zero.

    Dataset-scoped where filters and retained series' aggregate filters use the
    same existence probe under warehouse equality and the caller's policy context.
    """
    from .compiler_parts.bind import _bound_filter_clauses
    from .runtime_parts.limits import max_valid_values_limit

    config = runtime._config
    plan = compiled["logical_plan"]
    query = plan.query
    filters = (
        every_filter(query.get("where")) if observation_scope(query, config) == "dataset" else []
    )
    retained = set(compiled.get("retained_filtered_series", []))
    for row in plan.measure_plans:
        if row.bound_measure.alias in retained:
            filters.extend(_bound_filter_clauses(row.bound_measure, config))
    if not filters:
        return []
    dimensions = {row.id: row for row in config.dimensions}
    probe = {"version": 1, "select": [], "observation_scope": "query"}
    for key in ("policy_context", "limits", "request_id"):
        if key in payload:
            probe[key] = payload[key]

    held: set[str] = set()

    def values(field: str, where: list[dict[str, Any]], limit: int) -> list[str] | None:
        try:
            result = runtime.query_internal(
                {**probe, "group_by": [field], "where": where, "limit": limit}
            )
        except SemanticLayerError as exc:
            if exc.code == "POLICY_DENIED" and str(exc.details.get("reason", "")).startswith(
                "row_filter_"
            ):
                # A row filter holds the listing (the model no filter reaches): the answered
                # query says nothing about the value rather than call it unverified.
                held.add(field)
            return None
        return [str(row[field]) for row in result["rows"] if row.get(field) is not None]

    misses: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    checked: set[tuple[str, str]] = set()
    for item in filters:
        field, raw = str(item["field"]), item.get("value")
        literals = [v for v in (raw if isinstance(raw, list) else [raw]) if isinstance(v, str)]
        op = operators.word_of(item.get("op", "is"))
        if not literals or field not in dimensions or op not in {"is", "not"}:
            continue
        for literal in literals:
            if (field, literal) in checked:
                continue
            checked.add((field, literal))
            found = (
                values(field, [{"field": field, "op": "is", "value": literal}], 1)
                if dimensions[field].groupable
                else None
            )
            if found is None:
                if field not in held:
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
            "This query",
            miss["dimension"],
            miss["value"],
            known[miss["dimension"]],
            dimensions[miss["dimension"]].data_type,
        )
        miss.update(message=message, suggestion=suggestion)
    warnings = []
    if misses:
        # The warning names a few misses and counts the rest, whatever the filter lists.
        named = [miss.pop("message") for miss in misses][:_NAMED_MISSES]
        rest = len(misses) - len(named)
        warnings.append(
            semantic_issue(
                code="FILTER_VALUE_NOT_FOUND",
                message="; ".join(named)
                + (f"; and {rest} more values match nothing" if rest else ""),
                severity="warning",
                stage="execution",
                details={"filters": misses[:_LISTED_MISSES], "missing_count": len(misses)},
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


def _unknown_rows_kept_warnings(runtime: Runtime, compiled, payload) -> list[dict[str, Any]]:
    """Say how many rows each ``not`` (or ``not_`` text word) kept because their value is unknown.

    ``not`` keeps unknown rows, so ``not web`` includes the rows with no channel. A probe
    replaces the condition with ``missing: true`` under the query's other filters, window,
    entity filters and policy context, and counts the rows of the answer's source entity it
    reads. Nothing is said when it reads none or cannot be read.
    """
    from .compiler_parts.paths import _entity_key_dimension_ids
    from .runtime_parts.limits import max_valid_values_limit

    config = runtime._config
    plan = compiled["logical_plan"]
    query = plan.query
    where = list(query.get("where") or [])
    negated = [
        (index, item)
        for index, item in enumerate(where)
        if not is_child_group(item) and operators.word_of(item.get("op")) in operators.NEGATED_WORDS
    ]
    if not negated or not plan.measure_plans:
        return []
    source = plan.measure_plans[0].source_entity
    # A row-grain source has no key to tell its rows apart by, so nothing is said.
    if not any(row.id == source and row.key_columns for row in config.entities):
        return []
    keys = _entity_key_dimension_ids(source, config)
    time = {key: value for key, value in dict(query.get("time") or {}).items() if key != "fill"}
    probe: dict[str, Any] = {
        "version": 1,
        "select": list(query.get("select") or []),
        "group_by": keys,
        "observation_scope": "query",
        "limit": max_valid_values_limit(),
        **({"time": time} if time else {}),
    }
    entity_filters = [
        row
        for row in list(query.get("metric_filters") or [])
        if dict(row.get("expression") or {}).get("kind") == "metric_predicate"
    ]
    if entity_filters:
        probe["metric_filters"] = entity_filters
    for key in ("policy_context", "limits", "request_id", "route_decisions"):
        if key in payload:
            probe[key] = payload[key]
    dimensions = {row.id: row for row in config.dimensions}
    warnings = []
    for index, item in negated:
        field, word = str(item["field"]), operators.word_of(item.get("op"))
        unknown = {"field": field, "op": "missing", "value": True}
        try:
            result = runtime.query_internal(
                {**probe, "where": [*where[:index], unknown, *where[index + 1 :]]}
            )
        except SemanticLayerError:
            continue
        rows = len({tuple(row.get(key) for key in keys) for row in result["rows"]})
        if not rows:
            continue
        dimension = dimensions.get(field)
        values = [None, *(getattr(dimension, "unknown_values", None) or [])]
        warnings.append(
            semantic_issue(
                code="UNKNOWN_ROWS_KEPT",
                message=(
                    f"'{word}' keeps rows whose {field} is unknown: the answer includes "
                    f"{rows}{'+' if result.get('truncated') else ''} with no value. Add "
                    f'{{"field": "{field}", "op": "missing", "value": false}} to leave them out.'
                ),
                severity="warning",
                stage="execution",
                details={"field": field, "rows": rows, "values": values},
                object_ids=[field],
            )
        )
    return warnings


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
        self._cache_lock = RLock()
        self._generation = 0
        self._replace_base(config)
        self.warehouse = str(self._config.package.warehouse or "duckdb").strip().lower() or "duckdb"
        self.db_path = (
            self._resolve_asset_path(self._config.package.default_db, kind="default_db")
            if self.warehouse == "duckdb"
            else ""
        )
        self.adapter: WarehouseAdapter | None = None
        self._seed_warnings: list[dict[str, Any]] = []
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
        self._package_examples: list[tuple[str, dict[str, Any]]] | None = None

    def _get_package_examples(self) -> list[tuple[str, dict[str, Any]]]:
        """Load the same authored entries project validation certifies, once per generation."""
        from pathlib import Path

        from .yaml_loader import load_named_entries

        with self._cache_lock:
            if self._package_examples is None:
                self._package_examples = (
                    load_named_entries(
                        Path(self.package_root) / "examples",
                        plural_key="examples",
                        singular_key="example",
                    )
                    if self.source_path
                    else []
                )
            return self._package_examples

    @property
    def snapshot(self) -> LoadedPackageSnapshot:
        return self._snapshot

    @property
    def config(self):
        """An isolated configuration view; use from_config or reload to replace semantics."""
        return self.snapshot.config

    @property
    def _config(self) -> Any:
        """The caller's visible view inside a request scope, else the whole package
        (``package_config``)."""
        return self._view().config

    @_config.setter
    def _config(self, config: Any) -> None:
        self._replace_base(config)

    @property
    def registry(self) -> Registry:
        return self._view().registry

    def _view(self) -> visible_view.ViewEntry:
        pinned = visible_view.pinned_view(self)
        return pinned.entry if pinned is not None else self._views[frozenset(), frozenset()]

    def _replace_base(self, config: Any) -> None:
        """Serve ``config`` as the whole package (``package_config``); every view of the
        previous one is dropped with the caches computed from it."""
        from .row_filter_visibility import check_shared

        check_shared(config)
        with self._cache_lock:
            self.package_config = config
            self._generation += 1
            # Keyed by the caller's hidden objects and withheld value domains.
            self._views: dict[tuple[frozenset[str], frozenset[str]], visible_view.ViewEntry] = {
                (frozenset(), frozenset()): visible_view.ViewEntry(config, Registry(config))
            }

    def view_for(self, context: Any) -> Any:
        """The caller's visible view, for code outside a request scope (transport errors)."""
        with self.request_scope():
            return visible_view.view_for(self, context).config

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
        # Before probing or seeding: either would read a missing duckdb as a bad file.
        require_duckdb()
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
            self._replace_base(new_config)
            self.warehouse = (
                str(self._config.package.warehouse or "duckdb").strip().lower() or "duckdb"
            )
            self.db_path = (
                self._resolve_asset_path(self._config.package.default_db, kind="default_db")
                if self.warehouse == "duckdb"
                else ""
            )
            self._manifest = None
            self._package_examples = None
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
        view = self._view()
        with self._cache_lock:
            if view.catalog is None:
                dialect = dialect_for_warehouse(self.warehouse)
                view.catalog = {
                    "package": asdict(self._config.package),
                    "objects": [asdict(obj) for obj in self.registry.list_objects()],
                    "compiler": {
                        "dialect": dialect.name,
                        "warehouse_capabilities": dialect.capabilities(),
                        "runtime_expression_kinds": [
                            "metric_ref",
                            "measure",
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
            return deepcopy(view.catalog)

    @runtime_request_scope
    def _get_catalog_search_index(self) -> CatalogSearchIndex:
        """Return the immutable text index of the caller's view of this generation.

        The enclosing plan/discover operation pins the view with ``request_scope``. The
        cache lock only coordinates the first builder; policy-sensitive availability is
        evaluated later for every request and is never stored in this index.
        """
        view = self._view()
        with self._cache_lock:
            if view.search_index is None:
                view.search_index = CatalogSearchIndex.from_config(view.config)
            return view.search_index

    def resolve(self, term: str, *, kind: str = "") -> dict[str, Any]:
        key = (term, kind)
        view = self._view()
        with self._cache_lock:
            if key not in view.resolved:
                resolved = view.registry.resolve(term, kind=kind)
                obj = dict(resolved.get("object", {}) or {})
                obj_kind = str(obj.get("kind", ""))
                if obj_kind in {"metric", "measure"}:
                    payload = dict(obj.get("payload", {}) or {})
                    payload.update(_metric_payload(view.config, str(obj.get("id", "")), obj_kind))
                    obj["payload"] = payload
                    resolved = {**resolved, "object": obj}
                view.resolved[key] = deepcopy(resolved)
            return deepcopy(view.resolved[key])

    def _compile(
        self,
        payload: dict[str, Any],
        *,
        policy_context: dict[str, str],
        binding: BoundQuery | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        if binding is not None:
            # After the policy gate, so a policy that refuses the call outright speaks first.
            refuse_hidden_governors(self._config, binding.plan.query)
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
            render_profile=str(payload.get("sql_profile", "audit") or "audit"),
            policy_context=dict(policy_context),
            aggregate_routing=aggregate_routing_enabled(),
            visibility=self._view().hidden,
        )
        # Lock policy: hold the cache lock only across the in-memory get/put
        # operations. The compile itself (compile_query) runs unlocked so that
        # concurrent requests with different cache keys do not serialize behind
        # a single in-flight cold compile. Two threads racing on the same key
        # will each compile and the last writer wins — duplicate work but no
        # correctness issue (the LRU cache already deep-copies on get and put,
        # see cache.py:33,37).
        with self._cache_lock:
            cached = self._compile_cache.get(key)
        # A plan whose loaded coverage caps at the request's ``now`` serves that ``now`` only.
        now = (
            binding.coverage_now
            if binding is not None
            else CoverageClock(parse_now((payload.get("policy_context") or {}).get("now"))).literal
        )
        if cached is not None and cached.compiled.get("coverage_now") not in ("", now):
            cached = None
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
        refuse_closing_day_zone(self._config, compiled)
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
        with self._cache_lock:
            self._compile_cache.put(key, CachedCompilation(compiled=compiled))
        return compiled

    def engine_payload(
        self,
        query: Any,
        *,
        now: Any = None,
        context: Any = None,
        verbosity: str | None = None,
        sql_profile: str | None = None,
        limits: dict[str, Any] | None = None,
        request_id: str | None = None,
        debug: bool = False,
        partial: bool = False,
    ) -> dict[str, Any]:
        """The engine's form of a caller's call, with the request's identity and options.

        The call is read once, by :func:`semantic_rails.query_call.read_call`; identity comes
        only from ``context`` (a :class:`RequestContext`, or its mapping for a trusted local
        caller) and ``now`` only from the argument. The call is read in the caller's visible
        view, so a hidden id is refused exactly like an unknown one, before any definition of it
        (its aggregations, its clock) is read.
        """
        instant = parse_now(now)
        policy = _context_mapping(context)
        clock = self._default_clock
        granted = policy.get("metric_allowlist") is not None
        if granted:
            metrics = frozenset(policy["metric_allowlist"])

            def clock(object_id: str) -> str:
                # A granted metric's clock comes from its whole definition (its measures).
                return self._default_clock(object_id) if object_id in metrics else ""

        with self.request_scope():
            # The request boundary's order: the environment first, then the caller's view.
            check_request_environment(self.package_config, str(policy.get("environment") or ""))
            with visible_view.request_view(self, policy):
                # A metric grant reads the call in what it grants, so an id outside it answers
                # exactly like an id the package lacks, wherever the reading looks it up.
                config = visible_view.grant_view(self._config, policy) if granted else self._config
                # A joined call (``parts``) reads each part as a call (``read_joined_call``).
                joined = not partial and isinstance(query, Mapping) and "parts" in query
                internal, notes = (read_joined_call if joined else read_call)(
                    query,
                    config=config,
                    now=instant,
                    default_clock=clock,
                    text_dimension=text_dimension,
                    column_dimensions=dimensions_over_column,
                    **({} if joined else {"partial": partial}),
                )
        payload: dict[str, Any] = {**internal, "policy_context": policy}
        payload["policy_context"]["now"] = format_now(instant)
        options = {
            "verbosity": verbosity,
            "sql_profile": sql_profile,
            "limits": limits,
            "request_id": request_id,
        }
        payload.update({key: value for key, value in options.items() if value is not None})
        if debug:
            payload["debug"] = True
        if notes:
            payload["_call_notes"] = notes
        return payload

    def engine_seed(self, seed: Any, *, now: Any = None) -> dict[str, Any]:
        """A caller's seed call (plan, discover, inspect, build-options, valid-values) in the
        engine's form: only the keys the caller set, with the request's identity and ``now``."""
        call = dict(seed or {})
        context = dict(call.pop("policy_context", None) or {})
        instant = now if now not in (None, "") else context.pop("now", None)
        internal = self.engine_payload(call, now=instant, context=context or None, partial=True)
        kept = {_SEED_KEYS[key] for key in call if key in _SEED_KEYS}
        if any(isinstance(item, dict) and "having" in item for item in call.get("where") or []):
            kept.add("metric_filters")
        if internal.get("group_by"):
            kept.add("group_by")  # a dimension the call lifted from select
        return {
            key: value
            for key, value in internal.items()
            if key in kept or key in {"policy_context", "verbosity", "sql_profile"}
        }

    def _default_clock(self, object_id: str) -> str:
        try:
            return _object_default_query_temporal_role(self._config, object_id)
        except SemanticLayerError:
            return ""

    def validate(
        self, query: Any, *, now: Any = None, context: Any = None, **options: Any
    ) -> dict[str, Any]:
        """Check a call without running it: the answer's shape, policies and SQL plan."""
        try:
            payload = self.engine_payload(query, now=now, context=context, **options)
        except SemanticLayerError as exc:
            issue = lean_issue(exception_issue(exc, stage="validate"))
            hints = list(issue.get("recovery_hints") or [])
            return {
                "ok": False,
                "status": "error",
                "errors": [issue],
                **({"recovery_hints": hints} if hints else {}),
            }
        result = self.validate_internal(payload)
        for error in result.get("errors") or []:
            if error.get("code") == "AMBIGUOUS_PATH":
                route_options(query, error["details"])
        return result

    def compile(
        self, query: Any, *, now: Any = None, context: Any = None, **options: Any
    ) -> dict[str, Any]:
        """The SQL and plan a call compiles to."""
        payload = self.engine_payload(query, now=now, context=context, **options)
        with _routes_offered(query):
            return self.compile_internal(payload)

    def query(
        self, query: Any, *, now: Any = None, context: Any = None, **options: Any
    ) -> dict[str, Any]:
        """Answer a call."""
        payload = self.engine_payload(query, now=now, context=context, **options)
        with _routes_offered(query):
            return self.query_internal(payload)

    @runtime_request_scope
    def validate_internal(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .runtime_parts.answer_warnings import _compiled_warnings

        started = time.perf_counter()
        verbosity = resolve_verbosity(payload)
        sql_profile = resolve_sql_profile(payload)
        policy_context = _policy_context(payload)
        try:
            refusal = _scope_refusal(payload)
            if refusal is not None:
                raise refusal
            if "parts" in payload:
                from .runtime_parts.joined_call import joined_check

                return joined_check(self, payload, kind="validate", started=started)
            policy_effects, compiled = self._enforced_compile(payload, policy_context)
            report = ValidationReport(
                version=2,
                ok=True,
                warnings=[],
                disabled_options=list(compiled["logical_plan"].disabled_options),
                logical_plan=asdict(compiled["logical_plan"]),
                explain=asdict(compiled["explain"]),
            )
            out = asdict(report)
            out["status"] = "ok"
            out["warnings"] = _compiled_warnings(self._config, compiled, payload)
            out["errors"] = []
            out["normalized_query"] = compiled["explain"].normalized_query
            out["recovery_hints"] = []
            out.update(self._answer_block(compiled, payload, policy_effects, policy_context))
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
            _withhold_values(out, policy_effects, self.package_config)
            if verbosity == "full":
                out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
                out["performance_plan"] = asdict(compiled["performance_plan"])
            out["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return apply_response_verbosity(
                finish(out, payload=payload, compiled=compiled),
                verbosity=verbosity,
                sql_profile=sql_profile,
                kind="validate",
            )
        except SemanticLayerError as exc:
            if "parts" not in payload:  # a joined call's part refusals are already its own
                exc = self._with_lookalike_measures(exc, payload, policy_context)
                exc = _enrich_runtime_error(exc, self._config, payload.get("_call_notes"))
            issue = exception_issue(exc, stage="validate")
            report = ValidationReport(
                version=2,
                ok=False,
                errors=[issue],
                disabled_options=list(exc.details.get("disabled_options", [])),
            )
            out = asdict(report)
            out["status"] = "error"
            out["recovery_hints"] = list(issue.get("recovery_hints", []))
            out["authoring_hints"] = list(exc.details.get("authoring_hints", []) or [])
            out["query_ir_hints"] = list(
                exc.details.get("query_ir_hints", list(issue.get("recovery_hints", [])) or [])
            )
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
                finish(out, payload=payload),
                verbosity=verbosity,
                sql_profile=sql_profile,
                kind="validate",
            )

    @runtime_request_scope
    def compile_internal(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .runtime_parts.answer_warnings import _compiled_warnings

        refusal = _scope_refusal(payload)
        if refusal is not None:
            raise refusal
        if "parts" in payload:
            from .runtime_parts.joined_call import joined_check

            return joined_check(self, payload, kind="compile")
        verbosity = resolve_verbosity(payload)
        sql_profile = resolve_sql_profile(payload)
        policy_context = _policy_context(payload)
        try:
            policy_effects, compiled = self._enforced_compile(payload, policy_context)
        except SemanticLayerError as exc:
            enriched = self._with_lookalike_measures(exc, payload, policy_context)
            raise _enrich_runtime_error(enriched, self._config, payload.get("_call_notes")) from exc
        out = {
            "ok": True,
            "status": "ok",
            "errors": [],
            "warnings": _compiled_warnings(self._config, compiled, payload),
            "recovery_hints": [],
            "authoring_hints": [],
            "query_ir_hints": [],
            **self._answer_block(compiled, payload, policy_effects, policy_context),
            "hop_profile": _hop_profile(self._config, compiled),
            "normalized_query": compiled["explain"].normalized_query,
            "logical_plan": asdict(compiled["logical_plan"]),
            "sql_plan": asdict(compiled["sql_ast"]),
            "explain": asdict(compiled["explain"]),
            **compile_response_metadata(self, payload, compiled),
        }
        _withhold_values(out, policy_effects, self.package_config)
        return apply_response_verbosity(
            finish(out, payload=payload, compiled=compiled),
            verbosity=verbosity,
            sql_profile=sql_profile,
            kind="compile",
        )

    @runtime_request_scope
    def query_internal(
        self, payload: dict[str, Any], *, value_page: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an engine payload. ``value_page`` (valid-values' ``column``, ``search`` and
        ``offset``) runs a value listing as one page of its matching values
        (:func:`semantic_rails.compiler.value_page`)."""
        refusal = _scope_refusal(payload)
        if refusal is not None:
            raise refusal
        if "parts" in payload:
            from .runtime_parts.joined_call import joined_query

            return joined_query(self, payload)
        verbosity = resolve_verbosity(payload)
        sql_profile = resolve_sql_profile(payload)
        policy_context = _policy_context(payload)
        try:
            policy_effects, compiled = self._enforced_compile(payload, policy_context)
            if value_page is not None:
                compiled = page_values(compiled, self._config, **value_page)
        except SemanticLayerError as exc:
            enriched = self._with_lookalike_measures(exc, payload, policy_context)
            raise _enrich_runtime_error(enriched, self._config, payload.get("_call_notes")) from exc
        zone = _time_zone(self._config, compiled)
        # Per-request resource limits (statement_timeout_ms, max_rows) flow
        # from the request envelope through to the warehouse adapter. Hosted
        # operators use this to enforce per-tenant policies without forking;
        # local users typically leave `limits` unset.
        limits = _normalize_query_limits(payload.get("limits"), zone)
        totaled = compiled["logical_plan"].query.get("totals") is not None
        rows, hidden, limits_warnings, tie_warnings = self._run_statement(
            compiled, payload, policy_context, limits, totaled=totaled
        )
        out = self._answer(
            compiled,
            payload,
            rows,
            hidden,
            policy_effects=policy_effects,
            policy_context=policy_context,
            limits=limits,
            call_warnings=[*limits_warnings, *tie_warnings, *self._seed_warnings],
            verbosity=verbosity,
        )
        return apply_response_verbosity(
            finish(out, payload=payload, compiled=compiled),
            verbosity=verbosity,
            sql_profile=sql_profile,
            kind="execute",
        )

    def _request_limits(self, payload: dict[str, Any], zone: str) -> dict[str, Any]:
        """The request's ``limits`` for a statement whose clock reads ``zone``."""
        return _normalize_query_limits(payload.get("limits"), zone)

    def _refusal(
        self, exc: SemanticLayerError, payload: dict[str, Any], policy_context: dict[str, Any]
    ) -> SemanticLayerError:
        """A compile refusal of ``payload`` as its caller reads it: the measures it may have
        meant, and every diagnostics enricher over the call's own notes."""
        enriched = self._with_lookalike_measures(exc, payload, policy_context)
        return _enrich_runtime_error(enriched, self._config, payload.get("_call_notes"))

    def _run_statement(
        self,
        compiled: dict[str, Any],
        payload: dict[str, Any],
        policy_context: dict[str, Any],
        limits: dict[str, Any],
        *,
        totaled: bool,
    ) -> tuple[Any, dict[str, list[Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Run ``compiled``'s statement once, under the request's ``limits``: its rows and hidden
        columns (``split_hidden``), and the warnings about how it ran (a statement timeout the
        adapter can't honour, ties at the limit). ``totaled``: the rows include total rows,
        which follow the call's own and are never cut."""
        limit = call_rows(compiled["sql_ast"]).limit
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
                cap = limits.get("max_rows") if probe is not None else None
                rows = _adapter_query(
                    adapter,
                    probe if probe is not None else compiled["prepared_query"],
                    # The boundary probe's extra row takes no slot of the transport's cap.
                    limits={**limits, "max_rows": cap + 1} if cap else limits,
                    policy_context=policy_context,
                )
            if probe is not None:
                from .top_n import limit_rows

                # Total rows (``total_over`` set) follow the call's own rows and are never cut.
                own = [row for row in rows if not totaled or row.get(TOTAL_OVER) is None]
                assert limit is not None  # a boundary probe exists only under a limit
                cut, tie_warnings = limit_rows(own, limit, compiled["limit_order_keys"])
                kept = [*cut, *(row for row in rows if totaled and row.get(TOTAL_OVER) is not None)]
                truncated = bool(getattr(rows, "truncated", False))
                if cap and len(kept) > cap:
                    kept, truncated = kept[:cap], True
                rows = QueryRows(kept, truncated=truncated)
            rows, hidden = split_hidden(rows, compiled)
        except Exception as exc:
            if isinstance(exc, SemanticLayerError) and exc.code != "QUERY_EXECUTION_ERROR":
                raise
            details = _query_execution_error_details(
                engine=self.warehouse_engine,
                sql=compiled["sql"],
                payload=payload,
                policy_context=policy_context,
                extra=exc.details if isinstance(exc, SemanticLayerError) else None,
                seed_warnings=self._seed_warnings,
            )
            value = temporal_cast_error_value(exc)
            if value is not None:
                dimensions = {dim.id: dim for dim in self._config.dimensions}
                paths = [
                    f"where[{index}].value"
                    for index, item in enumerate(payload.get("where") or [])
                    if (dimension := dimensions.get(item.get("field"))) is not None
                    and dimension.data_type.lower() in {"date", "timestamp", "datetime"}
                    and value
                    in (
                        item["value"]
                        if isinstance(item.get("value"), list)
                        else [item.get("value")]
                    )
                ]
                if len(paths) == 1:
                    details["path"] = paths[0]
            raise query_execution_error(details) from exc
        return rows, hidden, limits_warnings, tie_warnings

    def _answer(
        self,
        compiled: dict[str, Any],
        payload: dict[str, Any],
        rows: Any,
        hidden: dict[str, list[Any]],
        *,
        policy_effects: list[dict[str, Any]],
        policy_context: dict[str, Any],
        limits: dict[str, Any],
        call_warnings: list[dict[str, Any]],
        verbosity: str,
    ) -> dict[str, Any]:
        """The answer to a call whose statement ran (``rows``, ``hidden``), before ``finish``
        and verbosity: its rows, warnings and every field it states. ``call_warnings`` follow
        the call's own (how the statement ran)."""
        from .runtime_parts.answer_warnings import (
            _compiled_warnings,
            _no_data_yet_warnings,
            _partial_data_warnings,
            _zero_after_last_row_notes,
            closing_day_disclosures,
        )
        from .runtime_parts.zero_filled import zero_filled_groups_notes

        zone = _time_zone(self._config, compiled)
        now = _answer_now(payload)
        withheld = _withheld_columns(policy_effects)
        mark_partial_periods(rows, compiled, now=now, zone=zone or "UTC")
        # Disclosures about the call's rows read only those: total rows are the call at
        # coarser grains, and their rolled-up NULLs are no group of the call's.
        own_rows = (
            QueryRows(
                [row for row in rows if row.get(TOTAL_OVER) is None],
                truncated=bool(getattr(rows, "truncated", False)),
            )
            if compiled["logical_plan"].query.get("totals") is not None
            else rows
        )
        coverage = _coverage_read(self, compiled, payload) if compiled["logical_plan"].time else {}
        coverage_warnings = _no_data_yet_warnings(
            compiled, own_rows, payload, coverage, zone=zone or "UTC", excluded_outputs=withheld
        )
        coverage_outputs = [
            name for warning in coverage_warnings for name in warning["details"]["outputs"]
        ]
        closing_days, closing_warnings = closing_day_disclosures(
            _closing_reads(self, compiled, payload), withheld
        )
        out: dict[str, Any] = {
            "ok": True,
            **result_rows(
                rows,
                output_columns=output_columns(self._config, compiled),
                zone=zone,
                counts=counted_outputs(self._config, compiled),
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
                *coverage_warnings,
                *_partial_data_warnings(compiled, own_rows, coverage, now=now, zone=zone or "UTC"),
                *_zero_after_last_row_notes(
                    compiled, coverage, now=now, zone=zone or "UTC", excluded_outputs=withheld
                ),
                *_no_data_in_scope_warnings(
                    compiled,
                    own_rows,
                    excluded_outputs=[*withheld, *coverage_outputs],
                    dataset=observed_outside_filters(compiled["logical_plan"].query, self._config),
                    runtime=self,
                    payload=payload,
                ),
                *zero_filled_groups_notes(
                    self._config, compiled, rows, hidden, excluded_outputs=withheld
                ),
                *closing_warnings,
                *_filter_value_warnings(self, compiled, payload),
                *_unknown_rows_kept_warnings(self, compiled, payload),
                *call_warnings,
            ],
            "recovery_hints": [],
            **self._answer_block(
                compiled,
                payload,
                policy_effects,
                policy_context,
                coverage=coverage,
                rows=rows,
                hidden=hidden,
            ),
            "hop_profile": _hop_profile(self._config, compiled),
            "normalized_query": compiled["explain"].normalized_query,
        }
        out["assumptions"].extend(closing_days)
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
        out["warnings"].extend(
            filtered_series_warnings(
                self, compiled, payload, out["rows"], truncated=out["truncated"]
            )
        )
        # Data-sparseness diagnostic: when a query returns zero rows AND
        # the request applied a time filter, the most common cause is
        # "data not present in this window" — not a query bug. The
        # reviewer's cross-clock conversion returned 0 rows because the
        # storefront_session fact only spans Sept 2016, but there was
        # no signal to distinguish that from a silent row-drop. Surface
        # the applied time bounds and point at inspect for freshness.
        if not own_rows and not coverage_warnings:
            time_block = dict(payload.get("time", {}) or {})
            has_time_filter = (
                time_block.get("start") or time_block.get("end") or time_block.get("temporal_role")
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
                for key in ("start", "end"):
                    if time_block.get(key):
                        applied[key] = time_block[key]
                # Probe the root entity's time column for actual data
                # coverage. Best-effort: failure returns {} and the warning
                # still ships. The probe only fires on the zero-row path
                # (we already paid for the original query) so the
                # round-three "signal only, no probes" rule still holds.
                # Under a row filter the probe reads only the caller's visible rows.
                with self._query_lock:
                    actual_data_coverage = _data_coverage_probe(
                        self._get_adapter(),
                        self._config,
                        warehouse=self.warehouse,
                        root_entity=root_entity,
                        temporal_role=str(time_block.get("temporal_role", "") or ""),
                        limits=limits,
                        visibility=compiled.get("row_filter_visibility"),
                        policy_context=policy_context,
                    )
                # The resolved bounds show where the filter landed: a relative window
                # resolves against now, which can fall far outside historical data.
                requested_window = _requested_window(compiled, payload)
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
                through = _covered_through(out.get("window") or {}, actual_data_coverage)
                out["warnings"].append(
                    {
                        "code": "EMPTY_RESULT_WINDOW",
                        "severity": "warning",
                        "message": (
                            "No group has rows in this window. The data covers it "
                            f"(through {through})."
                            if through
                            else "Query returned zero rows under the applied "
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
        _withhold_values(out, policy_effects, self.package_config)
        visible_rows = [
            {key: row[key] for key in visible}
            for row, visible in zip(rows, out["rows"], strict=True)
        ]
        out["warnings"] = _history_result_warnings(
            self._config, compiled, visible_rows, out["warnings"], truncated=out["truncated"]
        )
        if verbosity == "full":
            out["physical_plan"] = asdict(compiled["physical_plan"])
            out["performance_plan"] = asdict(compiled["performance_plan"])
            out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
        # First, where an agent reads it before the rows: which model each value comes from.
        # A metric grant's response never carries it (``resource_access`` keeps its own keys).
        return {"ok": True, "read_from": read_from(self._config, compiled, out), **out}

    def _answer_block(
        self,
        compiled,
        payload,
        policy_effects,
        policy_context,
        *,
        coverage=None,
        rows=None,
        hidden=None,
    ) -> dict[str, Any]:
        """The answer's fields every validate, compile and execute response states, in order:
        its window and assumptions, methodology, freshness, public policy effects, identity and
        provenance."""
        out = answer_fields(
            self._config,
            compiled,
            now=_answer_now(payload),
            zone=_time_zone(self._config, compiled) or "UTC",
            coverage=coverage,
            rows=rows,
        )
        out["assumptions"].extend(call_assumptions(self._config, compiled, payload or {}, hidden))
        freshness_rows = _freshness_by_leaf(self._config, compiled)
        public_effects = visible_view.public_effects(policy_effects, self._config)
        return {
            **out,
            "methodology_hints": _methodology_hints(self._config, payload, compiled),
            "freshness_by_leaf": freshness_rows,
            "freshness_as_of": _freshness_as_of(freshness_rows),
            "policy_effects": public_effects,
            "request_context": request_context_payload(policy_context),
            "provenance_summary": provenance_summary(
                self._config, compiled["logical_plan"], policy_effects=public_effects
            ),
        }

    def _enforced_compile(
        self, payload: dict[str, Any], policy_context: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Bind the query, enforce the package's policies on every object it reads, then compile
        it: the policy effects and the compiled query."""
        binding = self._bind(payload, policy_context)
        policy_effects = enforce_query_policies(
            self.package_config,
            binding.object_ids,
            **policy_identity(policy_context),
            query=payload,
            binding=binding,
        )
        return policy_effects, self._compile(
            payload, policy_context=policy_context, binding=binding
        )

    def _with_lookalike_measures(
        self, exc: SemanticLayerError, payload: dict[str, Any], policy_context: dict[str, Any]
    ) -> SemanticLayerError:
        """A ``PATH_NOT_FOUND`` for measures on its start, with the same-named measures of other
        models that answer the call as it stands (``details.lookalike_measures``).

        Each candidate (``path_gap.lookalike_swaps``) is the call with the look-alikes in place
        of its measures, the same group_by, where and time, on the look-alike's own clock when
        the call's clock isn't one of its own. It is kept only when it compiles here: in this
        request's scope, on the caller's view, under the caller's identity and policies, as the
        call itself did. Nothing is offered when no check can run that way: a resource grant
        (whose refusals name nothing), or ``plan`` validating its own drafts. The warehouse is
        never read; a candidate's compile is the call's own work for another measure.
        """
        details = dict(exc.details or {})
        if (
            exc.code != "PATH_NOT_FOUND"
            or lookalikes_off()
            or policy_context.get("metric_allowlist") is not None
            or details.get("reason") == "excluded_by_decision"
        ):
            return exc
        config = self._config
        swaps = lookalike_swaps(config, call_measures(payload), str(details.get("start") or ""))
        measures = {row.id: row for row in config.measures}
        clock = str(dict(payload.get("time") or {}).get("temporal_role") or "")
        rows: list[dict[str, Any]] = []
        for entity, pairs in swaps:
            own = {self._default_clock(new) for new in pairs.values()}
            keep = not clock or all(
                clock in measures[new].compatible_temporal_roles for new in pairs.values()
            )
            if not keep and (len(own) != 1 or not next(iter(own))):
                continue  # Its measures have no one clock of their own to read the window by.
            role = "" if keep else next(iter(own))
            try:
                self._enforced_compile(swapped_call(payload, pairs, role), policy_context)
            except Exception:  # noqa: BLE001 — a look-alike the call can't compile answers nothing
                continue
            columns = renamed_columns(payload, pairs)
            rows.append(
                {
                    "entity": entity,
                    "measures": pairs,
                    **({"columns": columns} if columns else {}),
                    **({"temporal_role": role} if role else {}),
                }
            )
        if not rows:
            return exc
        return SemanticLayerError(
            exc.code, str(exc), details={**details, "lookalike_measures": rows}
        )

    def _bind(self, payload: dict[str, Any], policy_context: dict[str, Any]) -> BoundQuery:
        """Bind the caller's query on their view; row filters and policies read the base."""
        filters = row_filters_for_context(self.package_config, policy_context)

        def check_policies(option: dict[str, Any], binding: BoundQuery) -> None:
            # A child-scope reading is offered only if this request's policy gate passes it.
            enforce_query_policies(
                self.package_config,
                binding.object_ids,
                **policy_identity(policy_context),
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
            self.package_config,
            binding,
            rebind=bind,
            **policy_identity(policy_context),
        )

    def _segment_policy_effects(
        self, segment_id: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """The segment's own effects, enforced on the base and stated for the caller."""
        effects = enforce_query_policies(
            self.package_config,
            [segment_id],
            **policy_identity(context),
        )
        return visible_view.public_effects(effects, self._config)

    @runtime_request_scope
    def segment_validate(
        self, segment_id: str, *, policy_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        context = context_from_policy_context(policy_context).to_policy_context()
        try:
            normalized, validation = self._segment_call(segment_id, context, self.validate_internal)
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
            exc = _enrich_runtime_error(exc, self._config)
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
                errors=[issue],
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
        return self._segment_call(segment_id, context, self.compile_internal)[1]

    def _segment_call(
        self,
        segment_id: str,
        context: dict[str, Any],
        run: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[Any, dict[str, Any]]:
        """Run the segment's query (with its preview dimensions) through ``run``, and say which
        segment it is and the policies that apply to it."""
        normalized = normalize_segment(self._config, segment_id)
        segment_policy_effects = self._segment_policy_effects(normalized.id, context)
        derived_query = build_segment_query(normalized, include_preview_dimensions=True)
        if context:
            derived_query["policy_context"] = context
        out = run(derived_query)
        out.update(
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
        return normalized, out

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
        normalized = normalize_segment(self._config, segment_id)
        segment_policy_effects = self._segment_policy_effects(normalized.id, context)
        preview_query = build_segment_query(
            normalized, include_preview_dimensions=True, limit=limit
        )
        membership_query = build_segment_query(normalized, include_preview_dimensions=False)
        if context:
            preview_query["policy_context"] = context
            membership_query["policy_context"] = context
        query_policy_effects: list[dict[str, Any]] = []
        for derived in (preview_query, membership_query):
            binding = bind_query(self._config, None, derived)
            for effect in enforce_query_policies(
                self.package_config,
                sorted(binding.object_ids),
                **policy_identity(context),
                query=derived,
                binding=binding,
            ):
                if effect not in query_policy_effects:
                    query_policy_effects.append(effect)
        filters = row_filters_for_context(self.package_config, context)
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
                    seed_warnings=self._seed_warnings,
                )
            ) from exc
        # The preview's rows, without the columns its SQL carries for the engine alone.
        visible_rows = strip_segment_preview_metric(list(split_hidden(rows, preview_compiled)[0]))
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
                counts=counted_outputs(self._config, preview_compiled),
            ),
            "preview_row_count": len(visible_rows),
            "member_count": member_count,
            "policy_effects": [
                *segment_policy_effects,
                *visible_view.public_effects(query_policy_effects, self._config),
            ],
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
