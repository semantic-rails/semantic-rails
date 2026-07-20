from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..dialects import dialect_for_warehouse
from ..expressions import expr_to_dict

_VALID_VERBOSITIES: tuple[str, ...] = ("minimal", "compact", "full")
_VALID_SQL_PROFILES: tuple[str, ...] = ("audit", "compact", "debug", "off")

# Keys preserved on validate/explain responses when verbosity="minimal".
_MINIMAL_KEYS_NONEXECUTE: frozenset[str] = frozenset({"ok", "errors", "warnings"})

# Keys preserved on compile responses when verbosity="minimal" — same as
# validate but plus `rendered_sql` so callers asking for the cheapest
# compile result still get the SQL they actually came to fetch. The blind
# agent benchmark caught that the previous minimal compile output had
# everything stripped EXCEPT the SQL — making "minimal" useless for the
# tool whose product is SQL.
_MINIMAL_KEYS_COMPILE: frozenset[str] = frozenset({"ok", "errors", "warnings", "rendered_sql"})

# Keys preserved on execute responses when verbosity="minimal".
_MINIMAL_KEYS_EXECUTE: frozenset[str] = frozenset(
    {"ok", "rows", "row_count", "truncated", "errors", "warnings"}
)

# Heavy keys dropped at the top-level response when verbosity="compact".
_COMPACT_DROP_KEYS: frozenset[str] = frozenset(
    {
        "physical_plan",
        "semantic_summary",
        "performance_plan",
        "compile_stats",
    }
)


def resolve_verbosity(payload: dict[str, Any] | None) -> str:
    """Resolve the verbosity level for a validate/compile/explain/execute call.

    Defaults to ``compact`` — the new default per the Phase 1 plan. Unknown
    values fall back to ``compact`` rather than erroring so a typo in the
    request envelope never blocks the response.
    """
    raw = ""
    if isinstance(payload, dict):
        raw = str(payload.get("verbosity", "") or "").strip().lower()
    if raw not in _VALID_VERBOSITIES:
        return "compact"
    return raw


def resolve_sql_profile(payload: dict[str, Any] | None) -> str:
    """Resolve the sql_profile for a validate/compile/explain/execute call.

    Recognises the new ``off`` value alongside the existing ``audit`` /
    ``compact`` / ``debug``. Unknown values fall back to ``audit``.
    """
    raw = ""
    if isinstance(payload, dict):
        raw = (
            str(payload.get("sql_profile", payload.get("render_profile", "")) or "").strip().lower()
        )
    if raw not in _VALID_SQL_PROFILES:
        return "audit"
    return raw


def _strip_output_column_lineage(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for column in list(columns or []):
        if not isinstance(column, dict):
            stripped.append(column)
            continue
        compact = {key: value for key, value in column.items() if key != "lineage"}
        stripped.append(compact)
    return stripped


def apply_response_verbosity(
    response: dict[str, Any],
    *,
    verbosity: str,
    sql_profile: str,
    kind: str,
) -> dict[str, Any]:
    """Filter a validate/compile/execute response by verbosity + sql_profile.

    ``kind`` is one of ``validate``, ``compile``, ``execute``.
    The execute path preserves ``rows`` / ``row_count`` in minimal mode;
    the other two preserve only ``{ok, errors, warnings}``.

    ``sql_profile="off"`` drops ``rendered_sql`` and ``sql_plan`` regardless
    of verbosity — this lets a caller suppress SQL even when they still
    want the heavy plan introspection that ``verbosity="full"`` provides.
    """
    out = dict(response or {})
    if verbosity == "minimal":
        if kind == "execute":
            allowed = _MINIMAL_KEYS_EXECUTE
        elif kind == "compile":
            allowed = _MINIMAL_KEYS_COMPILE
        else:
            allowed = _MINIMAL_KEYS_NONEXECUTE
        out = {key: value for key, value in out.items() if key in allowed}
    elif verbosity == "compact":
        for key in _COMPACT_DROP_KEYS:
            out.pop(key, None)
        if "output_columns" in out:
            out["output_columns"] = _strip_output_column_lineage(
                list(out.get("output_columns", []) or [])
            )
    # "full" — no top-level filtering beyond sql_profile handling below.

    if sql_profile == "off":
        out.pop("rendered_sql", None)
        out.pop("sql_plan", None)
    return out


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
    for child in list(expr_payload.get("args", []) or []):
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


def _object_label_maps(config: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in [
        *config.dimensions,
        *config.temporal_roles,
        *config.measures,
        *config.metric_recipes,
        *config.entities,
    ]:
        out[row.id] = {
            "display_label": getattr(row, "label", "") or getattr(row, "name", "") or row.id,
            "name": getattr(row, "name", "") or row.id,
            "type": getattr(row, "data_type", getattr(row, "value_type", "")) or "",
            "kind": row.id.split(".", 1)[0],
        }
    return out


def output_columns(config: Any, compiled: dict[str, Any]) -> list[dict[str, Any]]:
    query = dict(compiled["explain"].normalized_query or {})
    labels = _object_label_maps(config)
    columns: list[dict[str, Any]] = []
    for dim_id in list(query.get("group_by", []) or []):
        meta = labels.get(str(dim_id), {})
        columns.append(
            {
                "field": str(dim_id),
                "semantic_id": str(dim_id),
                "display_label": meta.get("display_label", str(dim_id)),
                "sql_alias": str(dim_id),
                "type": meta.get("type", ""),
                "lineage": [str(dim_id)],
            }
        )
    time = dict(query.get("time", {}) or {})
    if time.get("temporal_role"):
        role_id = str(time["temporal_role"])
        field = role_id if not time.get("grain") else f"{role_id}__{time['grain']}"
        meta = labels.get(role_id, {})
        columns.append(
            {
                "field": field,
                "semantic_id": role_id,
                "display_label": meta.get("display_label", field),
                "sql_alias": field,
                "type": "time",
                "lineage": [role_id],
            }
        )
    for item in list(query.get("select", []) or []):
        alias = str(item.get("as", "") or "")
        expr = dict(item.get("expression", {}) or {})
        lineage = list(dict.fromkeys(_collect_expr_object_ids(expr)))
        semantic_id = lineage[0] if len(lineage) == 1 else ""
        meta = labels.get(semantic_id, {})
        value_type = str(meta.get("type", "") or "")
        if not value_type and semantic_id.startswith("metric."):
            recipe = next((row for row in config.metric_recipes if row.id == semantic_id), None)
            if recipe is not None:
                source_ids = _collect_expr_object_ids(expr_to_dict(recipe.expression))
                source_measure = next(
                    (row for row in config.measures if row.id in source_ids), None
                )
                if source_measure is not None:
                    value_type = source_measure.value_type
        columns.append(
            {
                "field": alias,
                "semantic_id": semantic_id,
                "display_label": meta.get("display_label", alias),
                "sql_alias": alias,
                "type": value_type,
                "lineage": lineage,
            }
        )
    return columns


def _sql_complexity(select: Any) -> dict[str, int]:
    cte_count = 0
    join_count = 0

    def walk(node: Any) -> None:
        nonlocal cte_count, join_count
        ctes = list(getattr(node, "ctes", []) or [])
        cte_count += len(ctes)
        join_count += len(list(getattr(node, "joins", []) or []))
        for cte in ctes:
            walk(cte.query)

    walk(select)
    return {"cte_count": cte_count, "join_count": join_count}


def semantic_summary(config: Any, compiled: dict[str, Any]) -> dict[str, Any]:
    query = dict(compiled["explain"].normalized_query or {})
    time = dict(query.get("time", {}) or {})
    qualified_metrics: list[dict[str, Any]] = []
    for item in list(query.get("select", []) or []):
        expr = dict(item.get("expression", {}) or {})
        if expr.get("kind") != "scoped_aggregate":
            continue
        predicates = []
        for predicate in list(expr.get("predicates", []) or []):
            predicate_grain = str(predicate.get("time_grain") or time.get("grain") or "")
            predicates.append(
                {
                    "predicate_metric": str(
                        predicate.get("metric")
                        or predicate.get("measure")
                        or (
                            dict(predicate.get("input", {}) or {}).get("metric")
                            or dict(predicate.get("input", {}) or {}).get("measure")
                        )
                        or ""
                    ),
                    "qualifying_entity": str(predicate.get("entity", "") or ""),
                    "op": str(predicate.get("op", "") or ""),
                    "value": predicate.get("value"),
                    "predicate_grain": predicate_grain,
                    "predicate_grain_explicit": bool(predicate.get("time_grain")),
                    "time_alignment": str(predicate.get("time_alignment", "") or ""),
                }
            )
        if predicates:
            qualified_metrics.append(
                {
                    "alias": str(item.get("as", "") or ""),
                    "target_measure": str(expr.get("measure", "") or ""),
                    "aggregation": str(expr.get("aggregation", "") or ""),
                    "predicates": predicates,
                }
            )

    complexity = _sql_complexity(compiled["sql_ast"])
    rendered_sql = str(compiled.get("sql", "") or "")
    performance = dict(compiled["explain"].performance_plan or {})
    return {
        "time": {
            "temporal_role": str(time.get("temporal_role", "") or ""),
            "grain": str(time.get("grain", "") or ""),
        },
        "rollup_dimensions": [str(item) for item in list(query.get("group_by", []) or [])],
        "qualified_metrics": qualified_metrics,
        "fanout_guard": {
            "risk_level": performance.get("risk_level", ""),
            "semantic_equivalence": performance.get("semantic_equivalence", ""),
            "full_outer_alignments": performance.get("full_outer_alignments", 0),
        },
        "sql_complexity": {
            **complexity,
            "line_count": len([line for line in rendered_sql.splitlines() if line.strip()]),
        },
    }


def _selected_subjects(query: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in list(query.get("select", []) or []):
        if not isinstance(item, dict):
            continue
        expr = item.get("expression")
        if not isinstance(expr, dict):
            continue
        object_id = str(expr.get("metric") or expr.get("measure") or "")
        if object_id and object_id not in seen:
            seen.add(object_id)
            out.append(object_id)
    return out


def _selected_filters(query: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list(query.get("where") or []):
        if not isinstance(row, dict):
            continue
        field = row.get("field") or row.get("dimension")
        if not field:
            continue
        out.append(
            {
                "field": str(field),
                "op": str(row.get("op", "") or ""),
                "value": row.get("value"),
            }
        )
    for row in list(query.get("metric_filters") or []):
        if isinstance(row, dict):
            out.append(
                {
                    "field": "metric_filter",
                    "op": str(row.get("op", "") or ""),
                    "value": row.get("value"),
                }
            )
    return out


def semantic_trace(config: Any, compiled: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    """Build a compact human-facing trace from existing compile metadata."""

    logical_plan = compiled["logical_plan"]
    query = dict(compiled["explain"].normalized_query or {})
    paths = [
        {"target": str(target), "relationships": [str(item) for item in list(path or [])]}
        for target, path in dict(getattr(logical_plan, "selected_paths", {}) or {}).items()
    ]
    subjects = _selected_subjects(query)
    group_by = [str(item) for item in list(query.get("group_by") or [])]
    filters = _selected_filters(query)
    selected: dict[str, Any] = {
        "subjects": subjects,
        "group_by": group_by,
        "filters": filters,
        "paths": paths,
        "root_entity": str(getattr(logical_plan, "root_entity", "") or ""),
        "rewrite_status": str(getattr(logical_plan, "rewrite_strategy", "") or ""),
        "sql_available": bool(compiled.get("sql")),
    }
    target_label = ", ".join(subjects or ["unresolved target"])
    grouping_label = ", ".join(group_by or ["no grouping"])
    path_count = len(paths)
    markdown = (
        "### Semantic Trace\n"
        f"- Target: {target_label}\n"
        f"- Grouping: {grouping_label}\n"
        f"- Filters: {len(filters)}\n"
        f"- Relationship paths: {path_count}\n"
        f"- SQL: {'available' if selected['sql_available'] else 'not included'}"
    )
    return {
        "intent_slots": {
            "target": [],
            "grouping": [],
            "qualification": [],
            "filters": [],
            "time": {},
        },
        "selected": selected,
        "fallback": {"used": False, "semantic_drift": False, "reason": ""},
        "markdown": markdown,
    }


def compile_response_metadata(
    runtime: Any, payload: dict[str, Any], compiled: dict[str, Any]
) -> dict[str, Any]:
    """Build the heavy-metadata envelope shared by validate/compile/explain/execute.

    The output is filtered by ``payload["verbosity"]`` (``minimal`` /
    ``compact`` / ``full``; default ``compact``) and ``payload["sql_profile"]``
    (``audit`` / ``compact`` / ``debug`` / ``off``; default ``audit``).

    - ``verbosity="full"``: every field present (maximal envelope).
    - ``verbosity="compact"`` (default): drops ``physical_plan``,
      ``semantic_summary``, ``performance_plan``, ``compile_stats``,
      and strips ``lineage`` from ``output_columns``.
    - ``verbosity="minimal"``: returns ``{sql_profile}`` only — the caller
      filters the rest of the envelope via :func:`apply_response_verbosity`.
    - ``sql_profile="off"``: drops ``rendered_sql`` entirely (the caller
      separately drops ``sql_plan`` from the top-level envelope).
    """
    sql_profile = resolve_sql_profile(payload)
    verbosity = resolve_verbosity(payload)
    dialect = dialect_for_warehouse(runtime.warehouse)

    out: dict[str, Any] = {
        "sql_profile": sql_profile,
        "warehouse": runtime.warehouse,
        "dialect": dialect.name,
        "warehouse_capabilities": dialect.capabilities(),
    }
    if sql_profile != "off":
        out["rendered_sql"] = compiled["sql"]

    if verbosity == "minimal":
        # The runtime methods will further reduce the top-level response;
        # this helper only needs to keep the SQL-profile echo so the
        # filter step has it to reference if asked.
        return out

    columns = output_columns(runtime.config, compiled)
    if verbosity == "compact":
        out["output_columns"] = _strip_output_column_lineage(columns)
        out["trace"] = semantic_trace(runtime.config, compiled)
        return out

    # verbosity == "full"
    out["output_columns"] = columns
    out["trace"] = semantic_trace(runtime.config, compiled)
    out["compile_stats"] = dict(compiled.get("compile_stats", {}) or {})
    out["semantic_summary"] = semantic_summary(runtime.config, compiled)
    out["performance_plan"] = asdict(compiled["performance_plan"])
    out["physical_plan"] = asdict(compiled["physical_plan"])
    return out
