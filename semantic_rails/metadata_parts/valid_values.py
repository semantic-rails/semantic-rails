"""Valid-values public metadata surface."""

from __future__ import annotations

import contextlib
from typing import Any

from ..ast import normalize_query
from ..compiler import (
    _extend_config_with_synthetic_measures,
    compile_query,
    query_route_rows,
    read_routes,
)
from ..compiler_parts.bind import lift_conditional_aggregates
from ..compiler_parts.grain_recovery import _query_measure_ids
from ..errors import SemanticLayerError
from ..policies import (
    enforce_query_policies,
    hidden_object_ids,
    policy_effects_for_object,
    row_filters_for_context,
    withheld_measure_ids,
)
from ..request_context import context_from_policy_context
from ..runtime import Runtime, runtime_request_scope
from ..runtime_parts.limits import (
    MAX_VALID_VALUES_LIMIT as MAX_VALID_VALUES_LIMIT,
)
from ..runtime_parts.limits import (
    MAX_VALID_VALUES_OFFSET as MAX_VALID_VALUES_OFFSET,
)
from ..runtime_parts.limits import (
    max_valid_values_limit as max_valid_values_limit,
)
from ..runtime_parts.limits import (
    max_valid_values_offset as max_valid_values_offset,
)
from ..schema import MeasureConfig, PackageConfig
from ..temporal_support import validate_temporal_support
from .path_coverage import (
    _declared_value_rows,
    _filter_value_rows,
    _valid_values_lookup_hint,
    _value_domain_for_dimension,
)


# Hard ceilings on caller-controlled pagination. allow_live_query turns
# limit/offset into a real warehouse scan, so unbounded values let any
# caller force arbitrarily expensive queries on every transport. Operators
# can raise the ceilings via the env vars.
def _policy_context(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = dict((payload or {}).get("policy_context", {}) or {})
    return context_from_policy_context(context).to_policy_context()


def _query_state(query: dict[str, Any], config: PackageConfig) -> dict[str, Any]:
    keys = [
        "version",
        "select",
        "group_by",
        "where",
        "metric_filters",
        "time",
        "temporal_role_overrides",
        "route_decisions",
        "order_by",
        "limit",
        "debug",
        "explain",
        "export",
    ]
    state = {key: query[key] for key in keys if key in query}
    with contextlib.suppress(SemanticLayerError):
        state["normalized_query"] = normalize_query(dict(query), config=config).to_dict()
    return state


def _live_values_query(
    measure: MeasureConfig, dimension: str, query: dict[str, Any] | None
) -> dict[str, Any]:
    """Probing and execution retain the same filters, time roles and other query semantics."""
    payload = {
        **dict(query or {}),
        "select": [
            {
                "expression": {"measure": measure.id, "aggregation": measure.default_aggregation},
                "as": "anchor",
            }
        ],
        "group_by": [dimension],
    }
    for field in ("route_decisions", "order_by", "limit"):
        payload.pop(field, None)
    return payload


def _anchor_measure_id(
    runtime: Runtime, dimension: str, query: dict[str, Any] | None
) -> tuple[str, list[dict[str, Any]]]:
    """Use the query's measures under decisions; drop only pairs the anchor never reads."""
    probe = dict(query or {})
    if "route_decisions" in probe:
        query_route_rows(
            runtime._config,
            probe,
            row_filters=row_filters_for_context(runtime._config, _policy_context(probe)),
        )
    try:
        normalized, synthetic = lift_conditional_aggregates(normalize_query(probe), runtime._config)
        own = _query_measure_ids(
            _extend_config_with_synthetic_measures(runtime._config, synthetic), normalized
        )
    except SemanticLayerError:
        if probe.get("route_decisions"):
            raise
        own = []
    measures = sorted(
        runtime._config.measures, key=lambda row: own.index(row.id) if row.id in own else len(own)
    )
    if probe.get("route_decisions"):
        measures = [row for row in measures if row.id in own]
    # An anchor's values would show beside each listed value, so a withheld one never anchors.
    context = _policy_context(probe)
    scope = {
        "environment": str(context.get("environment", "")),
        "audience": str(context.get("audience", "")),
        "roles": context.get("roles", []),
    }
    unavailable = withheld_measure_ids(runtime._config, **scope) | hidden_object_ids(
        runtime._config, **scope
    )
    denied = {
        row.id
        for row in measures
        if row.id not in unavailable
        and any(
            effect["kind"] == "object_access" and effect["action"] == "deny"
            for effect in policy_effects_for_object(runtime._config, row.id, **scope)
        )
    }
    unavailable.update(denied)
    measures = [row for row in measures if row.id not in unavailable]
    policy_denied = bool(denied)
    reasons: list[dict[str, Any]] = []
    first_refusal: SemanticLayerError | None = None
    for measure in measures:
        decisions = list(probe.get("route_decisions", []) or [])
        dropped: set[tuple[str, str]] = set()
        candidate = _live_values_query(measure, dimension, probe)
        while True:
            try:
                payload = {**candidate, **({"route_decisions": decisions} if decisions else {})}
                binding = runtime._bind(payload, context)
                enforce_query_policies(
                    runtime._config,
                    binding.object_ids,
                    query=payload,
                    binding=binding,
                    **scope,
                )
                compiled = compile_query(
                    runtime._config,
                    runtime.registry,
                    payload,
                    binding=binding,
                )
            except SemanticLayerError as exc:
                if exc.details.get("reason") == "route_decision_unused":
                    dropped.add((exc.details["source_entity"], exc.details["target_entity"]))
                    index = exc.details["path"].removeprefix("route_decisions[").rstrip("]")
                    del decisions[int(index)]
                    continue
                if exc.code == "POLICY_DENIED":
                    policy_denied = True
                    break  # Skipped anchors never appear in source diagnostics.
                reasons.append({"measure": measure.id, "code": exc.code, "message": str(exc)})
                first_refusal = first_refusal or exc
                break
            read = read_routes(compiled["logical_plan"], compiled["route_choices"])
            if dropped.isdisjoint((start, target) for start, target, _ in read):
                return measure.id, decisions
            reasons.append(
                {
                    "measure": measure.id,
                    "code": "INVALID_QUERY",
                    "message": "reads a pair the query's route_decisions decide by another route",
                }
            )
            break
    if probe.get("route_decisions") and first_refusal is not None:
        raise first_refusal
    if policy_denied:
        raise SemanticLayerError(
            "POLICY_DENIED", "Query references a semantic object blocked by policy."
        )
    raise SemanticLayerError(
        "NO_VALID_VALUES_SOURCE",
        f"No valid values source for '{dimension}'; select a measure or metric that can anchor it",
        details={"attempts": reasons},
    )


@runtime_request_scope
def valid_values_payload(
    runtime: Runtime,
    *,
    dimension_id: str,
    query: dict[str, Any] | None = None,
    search: str = "",
    limit: int = 100,
    offset: int = 0,
    include_counts: bool = False,
    allow_live_query: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), max_valid_values_limit()))
    offset = max(0, min(int(offset), max_valid_values_offset()))
    config = runtime._config
    validate_temporal_support(config, query or {})
    policy_context = _policy_context(query)
    hidden_ids = hidden_object_ids(
        config,
        environment=str(policy_context.get("environment", "")),
        audience=str(policy_context.get("audience", "")),
        roles=policy_context.get("roles", []),
    )
    if dimension_id in hidden_ids:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown dimension '{dimension_id}'",
            details={"dimension": dimension_id},
        )
    dim = next((row for row in config.dimensions if row.id == dimension_id), None)
    if dim is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown dimension '{dimension_id}'")
    domain = _value_domain_for_dimension(config, dimension_id)
    if domain is not None and (not allow_live_query or not query):
        values, total_count, has_more = _declared_value_rows(
            domain, search=search, offset=offset, limit=limit
        )
        return {
            "dimension": dimension_id,
            "values": values,
            "total_count": total_count,
            "has_more": has_more,
            "source": "value_domain",
            "query_state": None,
            "selection": {"dimension_id": dimension_id, "entity": dim.entity},
            "selection_context": {"dimension_id": dimension_id, "entity": dim.entity},
            "value_domain_id": domain.id,
            "value_source_type": "declared_domain",
            "estimated_cost": "none",
            "anchor_measure": "",
            "provenance": {"value_domain": domain.id},
        }
    if not allow_live_query:
        hint_message = _valid_values_lookup_hint(False)
        return {
            "ok": False,
            "status": "needs_live_query",
            "dimension": dimension_id,
            "values": [],
            "total_count": 0,
            "has_more": False,
            "source": "none",
            "query_state": None,
            "selection": {"dimension_id": dimension_id, "entity": dim.entity},
            "selection_context": {"dimension_id": dimension_id, "entity": dim.entity},
            "value_domain_id": "",
            "value_source_type": "none",
            "estimated_cost": "none",
            "anchor_measure": "",
            "provenance": {},
            "valid_values_lookup_required": True,
            "valid_values_hint": hint_message,
            "warnings": [
                {
                    "code": "VALID_VALUES_NO_DOMAIN",
                    "severity": "warning",
                    "message": hint_message,
                }
            ],
            "recovery_hints": [
                {
                    "kind": "enable_live_query",
                    "message": hint_message,
                    "patch": {"allow_live_query": True},
                }
            ],
        }

    anchor_measure_id, decisions = _anchor_measure_id(runtime, dimension_id, query)
    anchor_measure = next(measure for measure in config.measures if measure.id == anchor_measure_id)
    query_payload = _live_values_query(anchor_measure, dimension_id, query)
    if decisions:
        query_payload["route_decisions"] = decisions
    query_payload["order_by"] = [{"field": dimension_id, "direction": "ASC"}]
    query_payload["limit"] = max(limit + offset, 100)
    result = runtime.query(query_payload)
    values = []
    for row in result["rows"]:
        if row.get(dimension_id) is None:
            continue
        value_row = {"value": row.get(dimension_id), "label": str(row.get(dimension_id))}
        if include_counts:
            value_row["count"] = row.get("anchor")
        values.append(value_row)
    values, total_count, has_more = _filter_value_rows(
        values, search=search, offset=offset, limit=limit
    )
    return {
        "dimension": dimension_id,
        "values": values,
        "total_count": total_count,
        "has_more": has_more,
        "source": runtime.warehouse_engine,
        "query_state": _query_state(query_payload, runtime._config),
        "selection": {"dimension_id": dimension_id, "entity": dim.entity},
        "selection_context": {"dimension_id": dimension_id, "entity": dim.entity},
        "value_domain_id": domain.id if domain is not None else "",
        "value_source_type": "live_query",
        "estimated_cost": "query",
        "anchor_measure": anchor_measure_id,
        "provenance": {
            "anchor_measure": anchor_measure_id,
            "semantic_fingerprint": runtime.snapshot.semantic_fingerprint,
            "source_fingerprint": runtime.snapshot.source_fingerprint,
        },
    }


__all__ = ["valid_values_payload"]
