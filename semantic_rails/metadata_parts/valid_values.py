"""Valid-values public metadata surface."""

from __future__ import annotations

import contextlib
import os
from typing import Any

from ..ast import normalize_query
from ..compiler import compile_query
from ..errors import SemanticLayerError
from ..policies import hidden_object_ids
from ..request_context import context_from_policy_context
from ..runtime import Runtime, runtime_request_scope
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
MAX_VALID_VALUES_LIMIT = 1_000
MAX_VALID_VALUES_OFFSET = 100_000


def _env_cap(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def max_valid_values_limit() -> int:
    return _env_cap("SEMANTIC_RAILS_MAX_VALID_VALUES_LIMIT", MAX_VALID_VALUES_LIMIT)


def max_valid_values_offset() -> int:
    return _env_cap("SEMANTIC_RAILS_MAX_VALID_VALUES_OFFSET", MAX_VALID_VALUES_OFFSET)


def _policy_context(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = dict((payload or {}).get("policy_context", {}) or {})
    return context_from_policy_context(context).to_policy_context()


def _query_state(query: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "version",
        "select",
        "group_by",
        "where",
        "metric_filters",
        "time",
        "temporal_role_overrides",
        "path_policy",
        "order_by",
        "limit",
        "debug",
        "explain",
        "export",
    ]
    state = {key: query[key] for key in keys if key in query}
    with contextlib.suppress(SemanticLayerError):
        state["normalized_query"] = normalize_query(dict(query)).to_dict()
    return state


def _anchor_measure_id(runtime: Runtime, dimension: str, query: dict[str, Any] | None) -> str:
    probe = dict(query or {})
    probe_where = list(probe.get("where", []) or [])
    probe_time = dict(probe.get("time", {}) or {}) or None
    reasons: list[dict[str, Any]] = []
    for measure in runtime.config.measures:
        candidate = {
            "select": [
                {
                    "expression": {
                        "measure": measure.id,
                        "aggregation": measure.default_aggregation,
                    },
                    "as": "anchor",
                }
            ],
            "group_by": [dimension],
            "where": probe_where,
        }
        if probe_time is not None:
            candidate["time"] = probe_time
        try:
            compile_query(runtime.config, runtime.registry, candidate)
            return measure.id
        except SemanticLayerError as exc:
            reasons.append({"measure": measure.id, "code": exc.code, "message": str(exc)})
    raise SemanticLayerError(
        "NO_VALID_VALUES_SOURCE",
        f"No valid values source for '{dimension}'",
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
    config = runtime.config
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

    anchor_measure_id = _anchor_measure_id(runtime, dimension_id, query)
    query_payload = dict(query or {})
    anchor_measure = next(measure for measure in config.measures if measure.id == anchor_measure_id)
    query_payload["select"] = [
        {
            "expression": {
                "measure": anchor_measure_id,
                "aggregation": anchor_measure.default_aggregation,
            },
            "as": "anchor",
        }
    ]
    query_payload["group_by"] = [dimension_id]
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
        "query_state": _query_state(query_payload),
        "selection": {"dimension_id": dimension_id, "entity": dim.entity},
        "selection_context": {"dimension_id": dimension_id, "entity": dim.entity},
        "value_domain_id": domain.id if domain is not None else "",
        "value_source_type": "live_query",
        "estimated_cost": "query",
        "anchor_measure": anchor_measure_id,
        "provenance": {"anchor_measure": anchor_measure_id},
    }


__all__ = ["valid_values_payload"]
