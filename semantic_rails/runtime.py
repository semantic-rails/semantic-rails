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
import inspect
import json
import os
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from functools import wraps
from threading import Condition, RLock, get_ident
from typing import Any

from .ast import normalize_query
from .cache import (
    CachedCompilation,
    CompiledSqlCache,
    LruCompiledSqlCache,
    compilation_cache_key,
    package_fingerprint,
)
from .catalog_search import CatalogSearchIndex
from .caveats import caveat_warnings
from .compiler import compile_query
from .config import (
    ensure_contained_package_path,
    get_package_config,
    get_package_path,
    load_package_config,
    package_root_for_source,
    project_managed_source,
    repo_root,
    resolve_repo_path,
)
from .db import (
    Database,
    WarehouseAdapter,
    create_warehouse_adapter,
    load_csv_dir_to_duckdb,
    seed_db,
)
from .diagnostics import (
    enrich_expression_ast_error,
    enrich_object_not_found,
    enrich_path_not_found,
    exception_issue,
    history_warning_payload,
    provenance_summary,
    rewrite_warning_payload,
    semantic_issue,
)
from .dialects import dialect_for_warehouse
from .errors import SemanticLayerError
from .expressions import expr_to_dict
from .fanout import build_hop_profile
from .ir import ValidationReport
from .policies import enforce_query_policies, query_policy_effects
from .registry import Registry
from .request_context import context_from_policy_context, request_context_payload
from .runtime_parts.responses import (
    apply_response_verbosity,
    compile_response_metadata,
    resolve_sql_profile,
    resolve_verbosity,
)
from .scope import classify_question
from .segments import build_segment_query, normalize_segment, strip_segment_preview_metric

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


def _enrich_runtime_error(exc: SemanticLayerError, config: Any) -> SemanticLayerError:
    """Run every applicable diagnostics enricher over a runtime error.

    Each enricher is a no-op when its code doesn't match, so we can
    chain them safely. Keeping this in one place means new enrichers
    only need to be added here, not at every catch site.
    """
    exc = enrich_object_not_found(exc, config)
    exc = enrich_expression_ast_error(exc, config)
    exc = enrich_path_not_found(exc, config)
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
        with runtime.request_scope():
            return operation(runtime, *args, **kwargs)

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


_DEFAULT_PATH_PREFERENCE = 100


def _path_alternates_warnings(config, logical_plan) -> list[dict[str, Any]]:
    """Warn when fewest-hops alone decided between semantically different
    join routes and the author never expressed a preference.

    Fires only when (a) more than one candidate path reached the target,
    (b) the runner-up has a different hop count (equal-score ties already
    raise AMBIGUOUS_PATH), (c) no relationship on either route carries a
    non-default ``path_preference``, and (d) no ``path_preferences`` pin
    covers the pair. Adding a shortcut relationship to a package can
    silently reroute existing queries; this warning is the tripwire.
    """
    relationships = {row.id: row for row in config.relationships}
    pinned = {(row.source_entity, row.target_entity) for row in config.path_preferences}
    root_entity = str(getattr(logical_plan, "root_entity", "") or "")
    selected = dict(getattr(logical_plan, "selected_paths", {}) or {})
    warnings: list[dict[str, Any]] = []
    for target, candidates in sorted(
        dict(getattr(logical_plan, "candidate_paths", {}) or {}).items()
    ):
        if len(candidates) < 2:
            continue
        chosen = list(selected.get(target) or candidates[0])
        runner_up = next((list(path) for path in candidates if list(path) != chosen), None)
        if runner_up is None or len(runner_up) == len(chosen):
            continue
        if (root_entity, target) in pinned:
            continue
        involved = set(chosen) | set(runner_up)
        if any(
            relationships[rel_id].path_preference != _DEFAULT_PATH_PREFERENCE
            for rel_id in involved
            if rel_id in relationships
        ):
            continue
        warnings.append(
            semantic_issue(
                code="PATH_ALTERNATES_UNPINNED",
                message=(
                    f"Join route from '{root_entity}' to '{target}' was chosen by hop count "
                    "alone; an alternate route exists and no path preference is declared. "
                    "The routes may have different semantics (e.g. role-playing foreign keys)."
                ),
                severity="warning",
                stage="planning",
                details={
                    "root_entity": root_entity,
                    "target_entity": target,
                    "chosen_path": chosen,
                    "alternate_path": runner_up,
                    "hint": (
                        "Declare path_preferences for this entity pair, or set "
                        "path_preference on the intended relationship, to pin the route."
                    ),
                },
                object_ids=[target],
            )
        )
    return warnings


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


def _collect_expr_object_ids(expr_payload: dict[str, Any]) -> list[str]:
    object_ids: list[str] = []
    kind = str(expr_payload.get("kind", "") or "")
    if "measure" in expr_payload:
        object_ids.append(str(expr_payload.get("measure", "")))
    if "metric" in expr_payload:
        object_ids.append(str(expr_payload.get("metric", "")))
    for key in ("left", "right", "input", "base", "converted", "value", "null_value"):
        child = expr_payload.get(key)
        if isinstance(child, dict):
            object_ids.extend(_collect_expr_object_ids(child))
    for key in ("args",):
        for child in list(expr_payload.get(key, []) or []):
            if isinstance(child, dict):
                object_ids.extend(_collect_expr_object_ids(child))
    for item in list(expr_payload.get("whens", []) or []):
        if isinstance(item, dict):
            if isinstance(item.get("when"), dict):
                object_ids.extend(_collect_expr_object_ids(dict(item["when"])))
            if isinstance(item.get("then"), dict):
                object_ids.extend(_collect_expr_object_ids(dict(item["then"])))
    if kind == "metric_predicate":
        object_ids.append(str(expr_payload.get("entity", "")))
    return [item for item in object_ids if item]


_OBJECT_REF_KEYS = ("measure", "metric", "field", "entity", "basis_metric")


def _collect_spec_object_ids(spec: Any) -> list[str]:
    """Object ids referenced anywhere inside a recipe's filter/window spec.

    These specs are free-form nested mappings (``where`` entries, nested
    ``metric_filters``, partition keys), so this walks them generically
    rather than enumerating shapes — an unrecognized shape should
    over-collect and deny, never under-collect and allow.
    """
    object_ids: list[str] = []
    if isinstance(spec, dict):
        for key, value in spec.items():
            if key in _OBJECT_REF_KEYS and isinstance(value, str) and value:
                object_ids.append(value)
            else:
                object_ids.extend(_collect_spec_object_ids(value))
    elif isinstance(spec, (list, tuple)):
        for item in spec:
            object_ids.extend(_collect_spec_object_ids(item))
    return object_ids


def _expand_object_id_closure(config: Any, object_ids: list[str]) -> list[str]:
    """Follow metric recipes so a metric cannot launder a governed measure.

    Policies are declared against the objects an author governs — usually
    measures. A query that names a *metric* touches those measures just as
    surely, but names none of them, so matching on the query's syntactic
    ids alone lets any curated metric walk straight through ``deny``,
    ``redact`` and ``metric_constraint``. Enforcement therefore runs over
    the transitive closure: every measure, metric and entity the named
    objects actually resolve to.
    """
    recipes = {row.id: row for row in config.metric_recipes}
    seen: set[str] = set()
    ordered: list[str] = []
    pending = list(object_ids)
    while pending:
        object_id = pending.pop(0)
        if not object_id or object_id in seen:
            continue
        seen.add(object_id)
        ordered.append(object_id)
        recipe = recipes.get(object_id)
        if recipe is None:
            continue
        # Recipes can reference other recipes; the `seen` guard makes a
        # cyclic or diamond-shaped definition terminate.
        pending.extend(_collect_expr_object_ids(expr_to_dict(recipe.expression)))
        pending.extend(_collect_spec_object_ids(recipe.filter_spec))
        pending.extend(_collect_spec_object_ids(recipe.window_spec))
        if recipe.temporal_role:
            pending.append(recipe.temporal_role)
    return ordered


def _query_object_ids(payload: dict[str, Any], config: Any = None) -> list[str]:
    """Object ids a query touches, for policy matching.

    Pass ``config`` wherever the result gates access — without it the
    result is only the ids the caller spelled out, which is not what the
    query reads. See :func:`_expand_object_id_closure`.
    """
    query = normalize_query(payload)
    object_ids: list[str] = []
    for select_item in query.select:
        if select_item.expression is not None:
            object_ids.extend(_collect_expr_object_ids(expr_to_dict(select_item.expression)))
    for metric_filter in query.metric_filters:
        if metric_filter.expression is not None:
            object_ids.extend(_collect_expr_object_ids(expr_to_dict(metric_filter.expression)))
    object_ids.extend(list(query.group_by))
    object_ids.extend(item.field for item in query.where)
    if query.time is not None and query.time.temporal_role:
        object_ids.append(query.time.temporal_role)
    object_ids.extend(list(dict(query.temporal_role_overrides).values()))
    deduped = list(dict.fromkeys(item for item in object_ids if item))
    if config is None:
        return deduped
    return _expand_object_id_closure(config, deduped)


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
            if key not in {"sql"}:  # never let extra leak raw sql back in
                details[key] = value
    return details


def _normalize_query_limits(raw: Any) -> dict[str, Any]:
    """Normalize the request envelope's optional `limits` block.

    Recognized keys:
      - `statement_timeout_ms` — positive int, query is aborted after N ms
      - `max_rows` — positive int, result is clipped to N rows

    Unrecognized keys are dropped silently so a future addition does not
    break existing clients. Missing or invalid values yield an empty dict
    (no enforcement).
    """
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, Any] = {}
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


def _adapter_query(adapter: Any, sql: str, *, limits: dict[str, Any]) -> list[dict[str, Any]]:
    """Call ``adapter.query`` with ``limits=`` only when the adapter
    accepts it.

    The built-in DuckDB and Snowflake adapters accept ``limits=`` to
    apply per-request statement timeouts and row caps. Older custom
    adapters with the legacy ``query(sql)`` signature would raise
    ``TypeError: unexpected keyword argument 'limits'`` if we always
    passed it, breaking integrations that worked before the runtime
    grew per-request limits. Detect the signature once and call the
    adapter accordingly. The hosting story relies on this: third-party
    warehouse adapters must keep working without forking the runtime.
    """
    try:
        signature = inspect.signature(adapter.query)
    except (TypeError, ValueError):
        signature = None
    accepts_limits = False
    if signature is not None:
        params = signature.parameters
        if "limits" in params:
            accepts_limits = True
        else:
            accepts_limits = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    rows = adapter.query(sql, limits=limits) if accepts_limits else adapter.query(sql)
    return rows if isinstance(rows, list) else list(rows)


def _data_coverage_probe(
    adapter: Any,
    config: Any,
    *,
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
    lookup fails or the probe raises. Failures are silent — coverage is
    a hint, not a guarantee.
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
        sql = (
            f"SELECT MIN({dim_row.column}) AS min_t, "
            f"MAX({dim_row.column}) AS max_t FROM {entity_row.table}"
        )
        rows = _adapter_query(adapter, sql, limits=limits)
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
        *_path_alternates_warnings(config, compiled["logical_plan"]),
    ]
    if payload is not None:
        warnings.extend(caveat_warnings(config, compiled, payload))
        warnings.extend(_expression_normalized_away_warnings(payload, compiled))
        warnings.extend(_ungrained_time_projection_warnings(payload))
    return warnings


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
                "a bucketed result, OR remove time.temporal_role for a "
                "scalar over the time window."
            ),
            severity="warning",
            stage="validate",
            details={
                "temporal_role": temporal_role,
                "recovery_hints": [
                    {
                        "code": "SET_TIME_GRAIN",
                        "message": (
                            "Add time.grain to bucket the result. Or drop "
                            "time.temporal_role if you want a scalar over "
                            "[time.start, time.end]."
                        ),
                        "suggested_patches": [
                            {"add": {"time.grain": "month"}},
                            {"remove": ["time.temporal_role"]},
                        ],
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
    def __init__(self, package_id: str):
        source_path = get_package_path(package_id)
        # Built-in packages (repo checkout or installed share dir) keep
        # their seeded assets under the project data/ root; anything else
        # registered by id (tests monkeypatching list_package_paths)
        # resolves relative assets against its own package root so a
        # relative default_db never lands in the repo checkout.
        self._init_loaded(
            config=get_package_config(package_id),
            package_id=package_id,
            source_path=source_path,
            prefer_package_root_assets=not project_managed_source(source_path),
        )

    @classmethod
    def from_path(cls, path: str) -> Runtime:
        source_path = os.path.abspath(path)
        return cls.from_config(
            load_package_config(source_path),
            source_path=source_path,
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
        source_path = os.path.abspath(source_path)
        prefer_assets = (
            not _is_repo_managed_source(source_path)
            if prefer_package_root_assets is None
            else prefer_package_root_assets
        )
        runtime = cls.__new__(cls)
        runtime._init_loaded(
            config=config,
            package_id=package_id,
            source_path=source_path,
            prefer_package_root_assets=prefer_assets,
        )
        return runtime

    def _init_loaded(
        self, *, config, package_id: str, source_path: str, prefer_package_root_assets: bool
    ) -> None:
        self.package_id = package_id or config.package.package_id
        self.source_path = source_path
        self.package_root = package_root_for_source(source_path)
        self.prefer_package_root_assets = prefer_package_root_assets
        self.config = config
        self.registry = Registry(self.config)
        self.warehouse = str(self.config.package.warehouse or "duckdb").strip().lower() or "duckdb"
        self.db_path = (
            self._resolve_asset_path(self.config.package.default_db, kind="default_db")
            if self.warehouse == "duckdb"
            else ""
        )
        self.adapter: WarehouseAdapter | None = None
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
        self._package_fingerprint = package_fingerprint(source_path)
        self._compile_cache: CompiledSqlCache = LruCompiledSqlCache(
            maxsize=int(os.environ.get("SEMANTIC_RAILS_COMPILE_CACHE_SIZE", "512"))
        )
        # Lazily loaded on first access; None = not yet looked up,
        # False = looked up and absent/stale (don't retry this call).
        self._manifest: dict[str, Any] | None | bool = None

    @contextlib.contextmanager
    def request_scope(self):
        """Pin a complete shared operation to the current runtime state.

        The lock is re-entrant because planner and metadata operations can
        call governed ``Runtime`` methods internally. Reload therefore waits
        for the outer application operation, not merely for its eventual
        validate/compile/query call.
        """

        with self._state_gate.read():
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
            return package_candidate if self.prefer_package_root_assets else repo_candidate
        if self.prefer_package_root_assets and os.path.exists(package_candidate):
            return package_candidate
        if not os.path.exists(repo_candidate) and os.path.exists(package_candidate):
            return package_candidate
        return repo_candidate

    def _expected_tables(self) -> set[str]:
        return {str(row.table) for row in self.config.entities if str(row.table).strip()}

    def _db_matches_package(self) -> bool:
        if self.warehouse != "duckdb":
            return True
        if not os.path.exists(self.db_path):
            return False
        try:
            db = Database.connect(self.db_path, read_only=True)
            try:
                rows = db.query(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                )
            finally:
                db.close()
        except Exception:
            return False
        existing = {str(row.get("table_name", "")) for row in rows}
        expected = self._expected_tables()
        return expected.issubset(existing)

    def _ensure_db(self) -> None:
        if self.warehouse != "duckdb":
            return
        seed = self.config.package.seed
        if self._db_matches_package():
            return
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
        if seed.kind == "sql_script":
            seed_db(self.db_path, src)
            return
        if seed.kind == "csv_dir_duckdb":
            load_csv_dir_to_duckdb(
                self.db_path,
                src,
                self._resolve_asset_path(seed.post_sql, kind="post_sql") if seed.post_sql else "",
                null_strings=seed.null_strings,
            )
            return
        raise SemanticLayerError("INVALID_CONFIG", f"Unsupported seed kind '{seed.kind}'")

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
            new_config = load_package_config(self.source_path)
            new_fingerprint = package_fingerprint(self.source_path)
            # Replace cached state. Drop the adapter so it reconnects with
            # the refreshed connection config on the next query.
            if self.adapter is not None:
                # noqa: BLE001 — adapter close must not block reload
                with contextlib.suppress(Exception):
                    self.adapter.close()
                self.adapter = None
            self.config = new_config
            self.registry = Registry(self.config)
            self.warehouse = (
                str(self.config.package.warehouse or "duckdb").strip().lower() or "duckdb"
            )
            self.db_path = (
                self._resolve_asset_path(self.config.package.default_db, kind="default_db")
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
                self.adapter = create_warehouse_adapter(self.config.package, db_path=self.db_path)
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
                loaded = load_manifest(self.source_path)
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
                    "package": asdict(self.config.package),
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
                        "runtime_expression_options": {
                            "arithmetic_null_behavior": ["null_propagate", "coalesce_zero"],
                            "ratio_null_behavior": ["null_if_zero"],
                        },
                        "aggregate_relation_candidates": [
                            asdict(row) for row in self.config.aggregate_relations
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
                self._catalog_search_index = CatalogSearchIndex.from_config(self.config)
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
                    payload.update(_metric_payload(self.config, str(obj.get("id", "")), obj_kind))
                    obj["payload"] = payload
                    resolved = {**resolved, "object": obj}
                self._resolve_cache[key] = deepcopy(resolved)
            return deepcopy(self._resolve_cache[key])

    def _query_fingerprint(self, payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload, sort_keys=True, default=str)

    def _compile(
        self, payload: dict[str, Any], *, policy_context: dict[str, str]
    ) -> dict[str, Any]:
        started = time.perf_counter()
        normalized = normalize_query(payload).to_dict()
        key = compilation_cache_key(
            package_hash=self._package_fingerprint,
            normalized_query=normalized,
            warehouse=self.warehouse,
            relation_profile=str(
                self.config.package.connection.name or self.config.package.default_db or ""
            ),
            render_profile=str(
                payload.get("sql_profile", payload.get("render_profile", "audit")) or "audit"
            ),
            policy_context=dict(policy_context),
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
        compiled = compile_query(self.config, self.registry, payload)
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
            object_ids = _query_object_ids(payload, self.config)
            policy_effects = enforce_query_policies(
                self.config,
                object_ids,
                environment=str(policy_context.get("environment", "")),
                audience=str(policy_context.get("audience", "")),
                roles=policy_context.get("roles", []),
                query=payload,
            )
            compiled = self._compile(payload, policy_context=policy_context)
            report = ValidationReport(
                version=2,
                ok=True,
                warnings=[],
                disabled_options=list(compiled["logical_plan"].disabled_options),
                logical_plan=asdict(compiled["logical_plan"]),
                explain=asdict(compiled["explain"]),
            )
            out = asdict(report)
            freshness_rows = _freshness_by_leaf(self.config, compiled)
            out["status"] = "ok"
            out["warnings"] = _compiled_warnings(self.config, compiled, payload)
            out["errors"] = []
            out["query"] = dict(payload)
            out["normalized_query"] = compiled["explain"].normalized_query
            out["recovery_hints"] = []
            out["assumptions"] = []
            out["methodology_hints"] = _methodology_hints(self.config, payload, compiled)
            out["freshness_by_leaf"] = freshness_rows
            out["freshness_as_of"] = _freshness_as_of(freshness_rows)
            out["policy_effects"] = policy_effects
            out["request_context"] = request_context_payload(policy_context)
            out["provenance_summary"] = provenance_summary(
                self.config, compiled["logical_plan"], policy_effects=policy_effects
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
            if verbosity == "full":
                out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
                out["performance_plan"] = asdict(compiled["performance_plan"])
            out["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return apply_response_verbosity(
                out, verbosity=verbosity, sql_profile=sql_profile, kind="validate"
            )
        except SemanticLayerError as exc:
            exc = _enrich_runtime_error(exc, self.config)
            issue = exception_issue(exc, stage="validate")
            report = ValidationReport(
                version=2,
                ok=False,
                errors=[issue],  # type: ignore[list-item]  # exception_issue returns dict; asdict serializes either shape
                disabled_options=list(exc.details.get("disabled_options", [])),
            )
            out = asdict(report)
            out["status"] = "error"
            out["query"] = dict(payload)
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
        object_ids = _query_object_ids(payload, self.config)
        policy_effects = enforce_query_policies(
            self.config,
            object_ids,
            environment=str(policy_context.get("environment", "")),
            audience=str(policy_context.get("audience", "")),
            roles=policy_context.get("roles", []),
            query=payload,
        )
        try:
            compiled = self._compile(payload, policy_context=policy_context)
        except SemanticLayerError as exc:
            raise _enrich_runtime_error(exc, self.config) from exc
        freshness_rows = _freshness_by_leaf(self.config, compiled)
        out = {
            "ok": True,
            "status": "ok",
            "errors": [],
            "warnings": _compiled_warnings(self.config, compiled, payload),
            "recovery_hints": [],
            "authoring_hints": [],
            "query_ir_hints": [],
            "assumptions": [],
            "methodology_hints": _methodology_hints(self.config, payload, compiled),
            "freshness_by_leaf": freshness_rows,
            "freshness_as_of": _freshness_as_of(freshness_rows),
            "policy_effects": policy_effects,
            "request_context": request_context_payload(policy_context),
            "provenance_summary": provenance_summary(
                self.config, compiled["logical_plan"], policy_effects=policy_effects
            ),
            "hop_profile": build_hop_profile(
                self.config,
                root_entity=compiled["logical_plan"].root_entity,
                selected_paths=compiled["logical_plan"].selected_paths,
                candidate_paths=compiled["logical_plan"].candidate_paths,
            ),
            "query": dict(payload),
            "normalized_query": compiled["explain"].normalized_query,
            "logical_plan": asdict(compiled["logical_plan"]),
            "sql_plan": asdict(compiled["sql_ast"]),
            "explain": asdict(compiled["explain"]),
            **compile_response_metadata(self, payload, compiled),
        }
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
        object_ids = _query_object_ids(payload, self.config)
        policy_effects = enforce_query_policies(
            self.config,
            object_ids,
            environment=str(policy_context.get("environment", "")),
            audience=str(policy_context.get("audience", "")),
            roles=policy_context.get("roles", []),
            query=payload,
        )
        try:
            compiled = self._compile(payload, policy_context=policy_context)
        except SemanticLayerError as exc:
            raise _enrich_runtime_error(exc, self.config) from exc
        freshness_rows = _freshness_by_leaf(self.config, compiled)
        # Per-request resource limits (statement_timeout_ms, max_rows) flow
        # from the request envelope through to the warehouse adapter. Hosted
        # operators use this to enforce per-tenant policies without forking;
        # local users typically leave `limits` unset.
        limits = _normalize_query_limits(payload.get("limits"))
        # If the caller asked for a statement_timeout_ms but the adapter
        # can't honor it at the warehouse boundary, surface a warning so
        # the caller learns the limit was best-effort. Without this, the
        # request silently completes on a runaway query and only the
        # `max_rows` post-fetch fence clips the result — the v2 audit
        # called out the "half-fake contract" smell on the DuckDB path.
        limits_warnings: list[dict[str, Any]] = []
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
                rows = _adapter_query(adapter, compiled["sql"], limits=limits)
        except SemanticLayerError:
            raise
        except Exception as exc:
            raise SemanticLayerError(
                "QUERY_EXECUTION_ERROR",
                f"Query execution failed: {exc}",
                details=_query_execution_error_details(
                    engine=self.warehouse_engine,
                    sql=compiled["sql"],
                    payload=payload,
                    policy_context=policy_context,
                ),
            ) from exc
        out: dict[str, Any] = {
            "ok": True,
            "rows": rows,
            "row_count": len(rows),
            "truncated": bool(getattr(rows, "truncated", False)),
            "rendered_sql": compiled["sql"],
            "logical_plan": asdict(compiled["logical_plan"]),
            "sql_plan": asdict(compiled["sql_ast"]),
            "explain": asdict(compiled["explain"]),
            "status": "ok",
            "errors": [],
            "warnings": [*_compiled_warnings(self.config, compiled, payload), *limits_warnings],
            "recovery_hints": [],
            "assumptions": [],
            "methodology_hints": _methodology_hints(self.config, payload, compiled),
            "freshness_by_leaf": freshness_rows,
            "freshness_as_of": _freshness_as_of(freshness_rows),
            "policy_effects": policy_effects,
            "request_context": request_context_payload(policy_context),
            "provenance_summary": provenance_summary(
                self.config, compiled["logical_plan"], policy_effects=policy_effects
            ),
            "hop_profile": build_hop_profile(
                self.config,
                root_entity=compiled["logical_plan"].root_entity,
                selected_paths=compiled["logical_plan"].selected_paths,
                candidate_paths=compiled["logical_plan"].candidate_paths,
            ),
            "query": dict(payload),
            "normalized_query": compiled["explain"].normalized_query,
        }
        metadata = compile_response_metadata(self, payload, compiled)
        execute_keep = {
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
            if has_time_filter:
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
                with self._query_lock:
                    actual_data_coverage = _data_coverage_probe(
                        self._get_adapter(),
                        self.config,
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
        if verbosity == "full":
            out["physical_plan"] = asdict(compiled["physical_plan"])
            out["performance_plan"] = asdict(compiled["performance_plan"])
            out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
        return apply_response_verbosity(
            out, verbosity=verbosity, sql_profile=sql_profile, kind="execute"
        )

    def _segment_policy_effects(
        self, segment_id: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return enforce_query_policies(
            self.config,
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
            normalized = normalize_segment(self.config, segment_id)
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
                    "derived_query": derived_query,
                    "segment_policy_effects": segment_policy_effects,
                    "request_context": request_context_payload(context),
                }
            )
            validation["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return validation
        except SemanticLayerError as exc:
            exc = _enrich_runtime_error(exc, self.config)
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
        normalized = normalize_segment(self.config, segment_id)
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
                "derived_query": derived_query,
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
        normalized = normalize_segment(self.config, segment_id)
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
                self.config,
                _query_object_ids(derived, self.config),
                environment=str(context.get("environment", "")),
                audience=str(context.get("audience", "")),
                roles=context.get("roles", []),
                query=derived,
            ):
                if effect not in query_policy_effects:
                    query_policy_effects.append(effect)
        preview_compiled = compile_query(self.config, self.registry, preview_query)
        membership_compiled = compile_query(self.config, self.registry, membership_query)
        adapter = self._get_adapter()
        try:
            with self._query_lock:
                rows = adapter.query(preview_compiled["sql"])
                count_rows = adapter.query(
                    f'SELECT COUNT(*) AS "member_count" FROM ({membership_compiled["sql"]}) AS "segment_members"'
                )
        except SemanticLayerError:
            raise
        except Exception as exc:
            raise SemanticLayerError(
                "QUERY_EXECUTION_ERROR",
                f"Segment preview failed: {exc}",
                details={"engine": self.warehouse_engine, "segment_id": segment_id},
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
            "rows": visible_rows,
            "preview_row_count": len(visible_rows),
            "member_count": member_count,
            "policy_effects": [*segment_policy_effects, *query_policy_effects],
            "request_context": request_context_payload(context),
            "derived_query": preview_query,
            "rendered_sql": preview_compiled["sql"],
            "logical_plan": asdict(preview_compiled["logical_plan"]),
            "sql_plan": asdict(preview_compiled["sql_ast"]),
            "explain": asdict(preview_compiled["explain"]),
            "query": dict(preview_query),
            "normalized_query": preview_compiled["explain"].normalized_query,
        }
