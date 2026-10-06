"""Diagnose missing filtered-series buckets through the ordinary authorized query path."""

from typing import Any

from ..compiler_parts.sql_lowering import _filtered_series_candidate, _preserves_filtered_series
from ..errors import SemanticLayerError


def filtered_series_warnings(
    runtime: Any, compiled: Any, payload: dict, rows: list[dict], *, truncated: bool
) -> list[dict]:
    plan = compiled["logical_plan"]
    if not plan.time.get("grain"):
        return []
    unsupported = [
        row
        for row in plan.measure_plans
        if _filtered_series_candidate(plan, row, runtime._config)
        and not _preserves_filtered_series(plan, row, runtime._config)
    ]
    if not unsupported:
        return []
    time_key = f"{plan.time['temporal_role']}__{plan.time['grain']}"
    keys = [*plan.group_by, time_key]
    present = {tuple(row.get(key) for key in keys) for row in rows}
    warnings = []
    probed = set()
    for row in unsupported:
        bound = row.bound_measure
        source = (bound.measure_id, bound.aggregation)
        if source in probed:
            continue
        probed.add(source)
        probe = {
            "version": 2,
            "select": [
                {
                    "expression": {"measure": bound.measure_id, "aggregation": bound.aggregation},
                    "as": "source",
                }
            ],
            "time": plan.time,
            "group_by": plan.group_by,
            "where": plan.query.get("where", []),
            "metric_filters": plan.query.get("metric_filters", []),
            "observation_scope": "query",
            "limit": 1001,
            **{key: payload[key] for key in ("policy_context", "limits") if key in payload},
        }
        reason = ""
        dropped = []
        if plan.query.get("limit") is not None or truncated:
            reason = "result_limited"
        elif plan.query.get("metric_filters"):
            reason = "query_population_filter"
        else:
            try:
                result = runtime.query(probe)
                if result["truncated"] or len(result["rows"]) >= 1001:
                    reason = "source_limited"
                else:
                    dropped = [
                        {key: item.get(key) for key in keys}
                        for item in result["rows"]
                        if tuple(item.get(key) for key in keys) not in present
                    ]
            except SemanticLayerError as exc:
                reason = exc.code
            except Exception:
                reason = "source_probe_failed"
        if not reason and not dropped:
            continue
        warnings.append(
            {
                "code": "FILTERED_SERIES_BUCKETS_UNVERIFIED"
                if reason
                else "FILTERED_SERIES_BUCKETS_DROPPED",
                "severity": "warning",
                "stage": "execution",
                "object_ids": [*plan.group_by, plan.time["temporal_role"]],
                "message": (
                    "The authored filter's observed time buckets could not be verified; "
                    "this series may omit zero-valued buckets."
                    if reason
                    else f"The authored filter dropped {len(dropped)} observed time buckets; "
                    "averages over the returned rows omit those buckets."
                ),
                "details": {"dropped_buckets": dropped, "reason": reason},
            }
        )
    return warnings
