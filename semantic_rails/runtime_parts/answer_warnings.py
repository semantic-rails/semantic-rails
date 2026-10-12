"""Warnings and disclosures stated beside a query answer's rows."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from .. import visible_view
from ..answer import _time_zone
from ..ast import normalize_query, plain_filters, time_output_alias
from ..caveats import caveat_warnings
from ..clock import local_date
from ..compiler import read_routes
from ..compiler_parts.empty_groups import GUARDED_BASE, base_reads, sql_nodes
from ..compiler_parts.indexes import get_package_analysis
from ..compiler_parts.paths import _leaf_time_role
from ..diagnostics import rewrite_warning_payload, semantic_issue
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..fanout import entity_label, package_route, pass_through_disclosure, route_hops
from ..sql_ast import filter_rejects_null
from ..temporal_support import _date_key, _range_intersects
from .disclosures import mixed_time_role_warnings


def _pass_through_notes(config, compiled) -> list[dict[str, Any]]:
    """ROUTE_PASS_THROUGH for each pair the SQL read by the package's only (or inherited)
    route when it passes through undeclared rows (``fanout.pass_through``). A pair the call's
    ``routes`` decided is the caller's own choice and gets none; the answer's ``routes`` names
    every route chosen among several (``answer.routes_taken``)."""
    plan = compiled["logical_plan"]
    decided = {
        (row["source_entity"], row["target_entity"])
        for row in compiled.get("route_decisions") or []
    }
    entity_of = {row.id: row.entity for row in config.dimensions}
    notes: list[dict[str, Any]] = []
    for start, target, path in read_routes(plan, compiled.get("route_choices") or []):
        try:
            resolution = package_route(config, start=start, target=target)
        except SemanticLayerError:
            continue
        if (start, target) in decided or resolution.basis not in {"only_route", "inherited"}:
            continue
        where = plain_filters(plan.query.get("where"))
        where = [item for item in where if entity_of.get(item.get("field")) == target]
        disclosure = resolution.routes[0] == path and pass_through_disclosure(
            config, start, target, path, where, route_basis=resolution.basis
        )
        if disclosure:
            note = semantic_issue(
                code="ROUTE_PASS_THROUGH",
                message=disclosure[0],
                severity="warning",
                stage="planning",
                details=disclosure[1],
                object_ids=[start, target],
            )
            if len(json.dumps(note)) >= 1500:  # long labels: the message still names each fix
                note["details"] = {
                    key: disclosure[1][key] for key in ("route", "through", "route_basis")
                }
            notes.append(note)
    return notes


def _history_warnings(config, logical_plan) -> list[dict[str, Any]]:
    """NULL_PRESERVING_HISTORY per dimension grouped or filtered by through a hop into a
    validity window (``fanout.hop_reach``), naming the hop; a hop out of a history gets none."""
    dimensions = {row.id: row for row in config.dimensions}
    uses = [(dim_id, "grouping") for dim_id in logical_plan.group_by or []]
    uses += [
        (row.get("field"), "filter")
        for row in plain_filters(logical_plan.query.get("where"))
        if filter_rejects_null(row.get("op"), row.get("value"))
    ]
    warnings: list[dict[str, Any]] = []
    for dim_id, use in dict.fromkeys(uses):
        dim = dimensions.get(dim_id)
        path = (logical_plan.selected_paths or {}).get(getattr(dim, "entity", ""), [])
        hops = route_hops(config, logical_plan.root_entity, path)
        hops = [hop for hop in hops if hop[2] == "as_of"]
        if dim is None or not hops:
            continue
        label, version = dim.label or dim.name, entity_label(config, hops[0][1])
        grouped = f"{label}: the {version} valid at each row's time; rows with none read an empty"
        filtered = f"{label} filter: rows with no {version} valid at their time are left out by"
        details = {"use": use, "dimension": dim_id, "entity": hops[0][1]}
        details["relationships"] = [hop[0] for hop in hops]
        warnings.append(
            semantic_issue(
                code="NULL_PRESERVING_HISTORY",
                message=f"{grouped} {label}." if use == "grouping" else f"{filtered} this filter.",
                severity="warning",
                stage="planning",
                details=details,
                object_ids=[dim_id],
            )
        )
    return warnings


def _history_result_warnings(config, compiled, rows, warnings, *, truncated=False) -> list[Any]:
    """``warnings`` with each grouping NULL_PRESERVING_HISTORY read from the returned rows: led
    by the share in the empty group, or dropped from a complete answer without one."""
    plan = compiled["logical_plan"]
    root, measures = plan.root_entity, {row.id: row for row in config.measures}
    root_key = getattr(get_package_analysis(config).entities.get(root), "key", [])
    complete = not (truncated or getattr(rows, "truncated", False))
    complete = complete and compiled["sql_ast"].limit is None
    complete = complete and not plan.query.get("metric_filters")
    visible_outputs = {output for row in rows for output in row}
    single_group = not any(
        hop[2] == "many"
        for dim in config.dimensions
        if dim.id in (plan.group_by or [])
        for hop in route_hops(config, root, (plan.selected_paths or {}).get(dim.entity, []))
    )
    out: list[Any] = []
    for warning in warnings:
        details = warning.get("details") or {}
        grouping = warning["code"] == "NULL_PRESERVING_HISTORY" and details["use"] == "grouping"
        empty = [row for row in rows if grouping and row.get(details["dimension"]) is None]
        if not grouping or not complete:
            out.append(warning)
            continue
        if not empty:
            continue
        dim = next(row for row in config.dimensions if row.id == details["dimension"])
        labels = {}  # a sum or count of the root's rows, which fall in one group each
        for item in plan.query["select"] if single_group else []:
            expression = item["expression"]
            measure = measures.get(expression.get("measure", ""))
            if measure is None or measure.entity != root or expression.get("aggregation"):
                continue
            counted, how = getattr(measure.expr, "column", None), measure.default_aggregation
            output = item.get("as") or measure.id
            if output in visible_outputs and (
                how in {"sum", "count"} or (how == "count_distinct" and counted in root_key)
            ):
                labels[output] = measure.label or measure.id
        totals = [
            {"output": output, "null_value": sum(row.get(output) or 0 for row in empty)}
            | {"total": sum(row.get(output) or 0 for row in rows)}
            for output in labels
        ]
        lead = next((row for row in totals if row["total"]), None)
        share = f"{len(empty)} of {len(rows)} rows are"
        if lead is not None:
            name = labels[lead["output"]]
            name = name if name[1:2].isupper() else name[:1].lower() + name[1:]
            share = f"{lead['null_value']} of {lead['total']} {name} "
            share += f"({round(100 * lead['null_value'] / lead['total'])}%) are"
        label, version = dim.label or dim.name, entity_label(config, details["entity"])
        message = f"{share} in the empty {label} group: no {version} was valid at their time, "
        details = details | {"null_rows": len(empty), "rows": len(rows)}
        if totals:
            details["measures"] = totals
        out.append({**warning, "message": f"{message}or it has no {label}.", "details": details})
    return out


def _time_zone_warnings(config: Any, compiled: dict[str, Any]) -> list[dict[str, Any]]:
    """Name the measures bucketed on a time role whose zone the query doesn't run in."""
    plan = compiled["logical_plan"]
    zone = _time_zone(config, compiled)
    if (
        not zone
        or not plan.time
        or not dialect_for_warehouse(config.package.warehouse).session_zone
    ):
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


def _compiled_warnings(
    config, compiled, payload: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = [
        *(rewrite_warning_payload(step) for step in compiled["logical_plan"].rewrite_steps),
        *_history_warnings(config, compiled["logical_plan"]),
        *_measure_validity_warnings(visible_view.base_of(config), compiled["logical_plan"], config),
        *_stock_key_gap_warnings(compiled),
        *_pass_through_notes(config, compiled),
        *_time_zone_warnings(config, compiled),
        *mixed_time_role_warnings(config, compiled["logical_plan"]),
    ]
    if payload is not None:
        # Every check reads the canonical query the compiler saw, not the caller's shorthand.
        canonical, notes = payload, list(payload.get("_call_notes") or [])
        warnings.extend(caveat_warnings(config, compiled, canonical))
        warnings.extend(_expression_normalized_away_warnings(canonical, compiled))
        warnings.extend(_shorthand_normalized_warnings(notes))
    return warnings


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
        compiled_kind = _input_expression_kind(compiled_expr)
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
    norm_query = getattr(logical_plan, "query", {})
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
        compiled_kind = _input_expression_kind(compiled_expr)
        if compiled_kind != original_kind:
            warnings.append(
                semantic_issue(
                    code="EXPRESSION_NORMALIZED_AWAY",
                    message=(
                        f"A having or entity-filter expression with kind={original_kind!r} was "
                        f"normalized to kind={compiled_kind!r}"
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


def _guard_probe_names(compiled, prefix):
    from ..sql_ast import SqlIdentifier

    ctes = {cte.name: cte for cte in compiled["sql_ast"].ctes}
    guard = ctes.get(GUARDED_BASE)
    projection = ctes["projected"].query if "projected" in ctes else compiled["sql_ast"]
    by_alias = (
        {
            field.alias: {
                node.parts[0]
                for node in sql_nodes(field.expression)
                if isinstance(node, SqlIdentifier) and node.parts[0].startswith(prefix)
            }
            for field in guard.query.select
        }
        if guard is not None
        else {}
    )
    by_output = {
        field.alias: set().union(*(by_alias.get(a, set()) for a in base_reads(field.expression)))
        for field in projection.select
    }
    return by_alias, by_output


def _requested_window(compiled, payload):
    time = dict(payload.get("time", {}) or {})
    normalized = dict((compiled["explain"].normalized_query or {}).get("time", {}) or {})
    window: dict[str, Any] = {
        key: str(time.get(key) or normalized.get(key) or "") for key in ("start", "end")
    }
    return window


def _covered_through(window: dict[str, Any], probed: dict[str, str]) -> str:
    """The last loaded day when an answer's window lies inside the loaded data, else "": a
    first row on or before the window's first day, and data through its last day."""
    day = {key: str(window.get(key) or "")[:10] for key in ("start", "end", "now")}
    day.update({key: str(probed.get(key) or "")[:10] for key in ("min", "max")})
    last = str(window.get("data_through") or "") or (day["max"] and min(day["max"], day["now"]))
    try:
        inside = date.fromisoformat(day["min"]) <= date.fromisoformat(day["start"]) and (
            date.fromisoformat(day["end"]) <= date.fromisoformat(last) + timedelta(days=1)
        )
    except ValueError:
        return ""
    return last if inside else ""


def _no_data_yet_warnings(compiled, rows, payload, coverage, *, zone="UTC", excluded_outputs=()):
    """Disclose only edges proven by the same coverage that guarded the answer's NULLs."""
    from ..compiler_parts.aliasing import AliasRegistry

    time = compiled["logical_plan"].time
    outputs = {
        item["output"]: item
        for item in [*compiled.get("zero_outputs", []), *compiled.get("balance_edges", [])]
        if item["output"] not in excluded_outputs
        and (
            any(row.get(item["output"]) is None for row in rows)
            if rows
            else bool(time.get("start") or time.get("end"))
        )
    }
    if not outputs:
        return []
    by_alias, by_output = _guard_probe_names(compiled, "coverage_")
    for name, item in outputs.items():
        if "probes" in item:  # a balance's own coverage
            by_output[name] = set(item["probes"])
    probes = sorted(set().union(*(by_output.get(name, set()) for name in outputs)))
    if not probes or any(f"{p}__loaded_to" not in coverage for p in probes):
        return []
    window = _requested_window(compiled, payload)
    bound = [row.bound_measure for row in compiled["logical_plan"].measure_plans]
    aliases = AliasRegistry.for_plan(
        compiled["logical_plan"], measure_aliases=[b.alias for b in bound]
    )
    time_key = time_output_alias(time)
    affected: list[str] = []
    measures: list[dict[str, Any]] = []
    last_rows: dict[str, str] = {}
    for output, item in outputs.items():
        for measure in item["measures"]:
            names = set(item.get("probes", ())) or set().union(
                *(
                    by_alias.get(aliases.internal(b.alias), set())
                    for b in bound
                    if b.measure_id == measure
                )
            ) & by_output.get(output, set())
            # Combined plans can settle the output itself instead of its leaf aliases.
            if not names and len(item["measures"]) == 1:
                names = by_output.get(output, set())
            if not names:
                continue
            edges = [coverage[f"{p}__loaded_to"] for p in sorted(names)]
            edge, source = None, None
            if time.get("window_total") and any(
                coverage.get(f"{p}__loaded_from") == 1 for p in names
            ):
                continue
            if all(value is None for value in edges):
                pass  # No visible rows: an undated disclosure, never another caller's edge.
            elif time.get("window_total"):
                if not window["start"] or not all(value == -1 for value in edges):
                    continue
                edge, source = window["start"], "before_window"
                through = [coverage.get(f"{p}__data_through") for p in names]
                if all(isinstance(value, datetime) for value in through):
                    last_rows[measure] = local_date(max(through), zone)
            else:
                # Require every mapped leaf to have a dated edge before claiming one.
                if not rows or any(value is None for value in edges):
                    continue
                buckets = [row.get(time_key) for row in rows if row.get(output) is None]
                values = [*edges, *buckets]
                # Compare SQL's bucket keys only, never raw bounds or coerced dates.
                if not (
                    all(type(value) is date for value in values)
                    or (
                        all(type(value) is datetime for value in values)
                        and len({value.utcoffset() is None for value in values}) == 1
                    )
                ):
                    continue
                latest = max(edges)
                if not any(bucket > latest for bucket in buckets):
                    continue
                edge = (
                    latest.date().isoformat()
                    if type(latest) is datetime
                    and time.get("grain") in {"day", "week", "month", "quarter", "year"}
                    else latest.isoformat()
                )
                source = "last_bucket"
            detail = {"id": measure, "edge": edge, "edge_source": source}
            if detail not in measures:
                measures.append(detail)
            if output not in affected:
                affected.append(output)
    if not affected:
        return []
    messages = []
    grain = str(time.get("grain") or "day")
    for item in measures:
        if item["edge_source"] == "before_window":
            last = last_rows.get(item["id"])
            ends = f"its data runs through {last}, " if last else "its data ends "
            reason = f"in this window: {ends}before {item['edge']}, the window start, so the total reads NULL, not 0"
        elif item["edge"] is not None:
            reason = f"after the {grain} of {item['edge']}, the last {grain} with data, so later {grain}s read NULL, not 0"
        else:
            reason = "with no dated coverage edge, so it reads NULL, not 0"
        messages.append(f"No data yet for {item['id']} {reason}.")
    return [
        semantic_issue(
            code="NO_DATA_YET",
            message=" ".join(messages),
            severity="warning",
            stage="execution",
            details={
                "outputs": affected,
                "measures": measures,
                "agent_action": "mention_when_interpreting_results",
            },
            object_ids=sorted({item["id"] for item in measures}),
        )
    ]


def _measure_validity_warnings(config, logical_plan, display_config=None) -> list[dict[str, Any]]:
    visible_view.require_base(config)
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
        display = next(row for row in (display_config or config).measures if row.id == measure.id)
        for index, window in enumerate(measure.validity_windows):
            window_start = _date_key(window.from_)
            window_end = _date_key(window.to)
            if (
                _crosses_boundary(start, end, window_start, window_end)
                and str(measure.cross_window_policy or "caveat").lower() != "refuse"
            ):
                window = display.validity_windows[index]
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
        for index, discontinuity in enumerate(measure.external_discontinuities):
            if _range_intersects(
                start, end, _date_key(discontinuity.from_), _date_key(discontinuity.to)
            ):
                discontinuity = display.external_discontinuities[index]
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
