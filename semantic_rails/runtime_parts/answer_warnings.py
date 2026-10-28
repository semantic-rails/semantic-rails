"""Warnings and disclosures stated beside a query answer's rows."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import visible_view
from ..answer import _time_zone, data_through
from ..ast import TIME_AXIS, normalize_query, plain_filters, time_output_alias
from ..caveats import caveat_warnings
from ..clock import UNITS, engine_window, local_date, local_now, period_end
from ..compiler import read_routes
from ..compiler_parts.empty_groups import GUARDED_BASE, base_reads, sql_nodes
from ..compiler_parts.indexes import get_package_analysis
from ..compiler_parts.paths import _leaf_time_role
from ..diagnostics import rewrite_warning_payload, semantic_issue
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..fanout import (
    caller_basis,
    entity_label,
    package_route,
    pass_through_disclosure,
    reads_end_of_day,
    route_hops,
)
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
        # Gated on the package's basis; the warning states the caller's view of it.
        basis = caller_basis(config, start, target, resolution)
        disclosure = resolution.routes[0] == path and pass_through_disclosure(
            config, start, target, path, where, route_basis=basis
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


def _validity_moments(config, relationship: str) -> tuple[str, str]:
    """When a hop into the relationship's validity window reads a version, said of each row
    and of the rows: their time, or the end of their day (``as_of: end_of_day``)."""
    rel = get_package_analysis(config).relationships[relationship]
    if reads_end_of_day(rel):
        return "the end of each row's day", "the end of their day"
    return "each row's time", "their time"


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
        each, their = _validity_moments(config, hops[0][0])
        grouped = f"{label}: the {version} valid at {each}; rows with none read an empty"
        filtered = f"{label} filter: rows with no {version} valid at {their} are left out by"
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
    root_key = getattr(get_package_analysis(config).entities.get(root), "key_columns", ())
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
        their = _validity_moments(config, details["relationships"][0])[1]
        message = f"{share} in the empty {label} group: no {version} was valid at {their}, "
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


CLOSING_DAY_CODES = ("BALANCE_CLOSING_DAY", "BALANCE_SERIES_MISSING_ON_CLOSING_DAY")


def closing_day_disclosures(
    reads: list[dict[str, Any]], excluded_outputs: Iterable[str] = ()
) -> tuple[list[dict], list[dict]]:
    """What each balance read on its closing day says: the day it read (an assumption), and,
    when series seen earlier in a period had no snapshot on its closing day, how many (a
    warning). The engine can't tell a series that stopped, which is right to leave out, from
    one that takes its snapshots on other days or a table that records only changes, which
    are undercounted, so it never leaves them out silently. ``reads`` comes from
    ``runtime._closing_reads``, nested reads included; one whose probe failed is still
    disclosed, with no day. A read only for outputs whose values a policy withholds says
    nothing, nor does a read whose owner is unknown (``outputs`` None) beside any withheld
    output: it might be for that one."""
    assumptions: list[dict] = []
    warnings: list[dict] = []
    excluded = set(excluded_outputs)
    for read in reads:
        day = _iso_day(read.get("closing_day"))
        owned = read["outputs"]
        outputs = None if owned is None else [name for name in owned if name not in excluded]
        withheld = bool(excluded) if owned is None else bool(owned) and not outputs
        if ("periods" in read and not day) or withheld:
            continue  # nothing read on a closing day, or maybe only for withheld values
        measure = read["measure"]
        entry = {
            "measure": measure,
            "outputs": outputs,
            "nested": read["nested"],
            "closing_day": day,
            "periods": int(read.get("periods") or 0),
        }
        assumptions.append(_closing_assumption(measure, entry))
        missing = int(read.get("series_missing") or 0)
        if day and not missing:
            continue
        details = {
            **entry,
            "closing_day": _iso_day(read.get("missing_day")),
            "checked": bool(day),
            "series": int(read.get("series") or 0),
            "series_missing": missing,
            "periods_missing": int(read.get("periods_missing") or 0),
        }
        warnings.append(_missing_warning(measure, details, [measure]))
    return assumptions, warnings


def granted_closing_day(entry: dict, columns: list[dict], permitted: set[str]) -> dict | None:
    """A closing-day disclosure as a grant sees it: as is where its measure is granted (and,
    for a read whose owner is unknown, every column of the answer is); where only an output
    reading it is, with the day and counts but named by those outputs' ids; else nothing,
    never named by a column that doesn't read it. Any other entry is returned as is."""
    code = entry.get("code")
    if code not in CLOSING_DAY_CODES:
        return entry
    fields = entry if code == "BALANCE_CLOSING_DAY" else entry["details"]
    ids = {column.get("field"): column.get("semantic_id") for column in columns}
    if fields["measure"] in permitted:
        unknown = fields["outputs"] is None
        return None if unknown and not set(ids.values()) <= permitted else entry
    outputs = [name for name in fields["outputs"] or () if ids.get(name) in permitted]
    if not outputs:
        return None
    granted = sorted({str(ids[name]) for name in outputs})
    hidden = {"code", "message", "measure"}
    shown = {**{k: v for k, v in fields.items() if k not in hidden}, "outputs": outputs}
    subject = f"the balance read by {', '.join(granted)}"
    if code == "BALANCE_CLOSING_DAY":
        return _closing_assumption(subject, shown)
    return _missing_warning(subject, shown, granted)


def refuse_closing_day_zone(config: Any, compiled: dict[str, Any]) -> None:
    """A balance closes on its clock's day bucket, which DuckDB and Postgres compute for a
    zone-aware column in the session's zone: the zone the query runs in (UTC without a time
    block). A closing-day read on a timestamp clock in another zone could close on another
    day, so it is refused; a DATE clock (``zone`` "") never depends on the session."""
    if not dialect_for_warehouse(config.package.warehouse).session_zone:
        return
    zone = _time_zone(config, compiled)
    for read in compiled.get("closing_reads") or ():
        if read["zone"] and read["zone"] != zone:
            raise SemanticLayerError(
                "MIXED_TIME_ROLES",
                f"{read['measure']} is a balance read on its closing day in {read['zone']}, "
                f"its clock's zone, but this query runs in {zone or 'UTC'}, so its day could "
                "be another. Give the call a time window on the balance's own clock "
                "(time.temporal_role) so it runs in that zone.",
                details={
                    "reason": "closing_day_zone",
                    "measure": read["measure"],
                    "clock_zone": read["zone"],
                    "time_zone": zone,
                },
            )


def _closing_assumption(subject: str, entry: dict) -> dict:
    day, periods = entry["closing_day"], entry["periods"]
    where = " (in an entity filter, predicate or branch of the call)" if entry["nested"] else ""
    when = (
        f"on its closing day, {day}, the last day with a snapshot"
        if periods == 1
        else f"on each period's closing day, its last day with a snapshot (the latest is {day})"
        if day
        else "on each period's closing day, its last day with a snapshot"
    )
    return {
        "code": "BALANCE_CLOSING_DAY",
        "message": f"{subject}{where} is read {when}: a series with no snapshot that day is "
        "not counted, never carried forward from an earlier day.",
        **entry,
    }


def _missing_warning(subject: str, details: dict, object_ids: list[str]) -> dict:
    missing, seen, missed = details["series_missing"], details["series"], details["closing_day"]
    periods, spans = details["periods"], details["periods_missing"]
    found = (
        f"Whether every series of {subject} had a snapshot on its closing day couldn't be checked."
        if not details["checked"]
        else f"{missing} of {seen} series of {subject} had a snapshot earlier in the period "
        f"but none on its closing day, {missed}, so they are not counted."
        if periods == 1
        else f"In {spans} of {periods} periods, series of {subject} had a snapshot earlier in "
        f"the period but none on its closing day ({missing} of the {seen} series read across "
        f"the periods; the latest such day is {missed}), so they are not counted."
    )
    return semantic_issue(
        code="BALANCE_SERIES_MISSING_ON_CLOSING_DAY",
        message=f"{found} A series that stopped is right to leave out; one that takes its "
        "snapshots on other days, or a table that records only changes, is undercounted. "
        "Group by the clock's day to see each day's snapshots; model a change table as a "
        "state_as_of relation.",
        severity="warning",
        stage="execution",
        details=details,
        object_ids=object_ids,
    )


def _iso_day(value: Any) -> str:
    """A warehouse's DATE as ``YYYY-MM-DD`` ("" for none)."""
    return value.isoformat()[:10] if isinstance(value, date) else str(value or "")[:10]


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
    """The probes (CTEs named ``prefix``...) each guarded column reads, by the guard's column
    (``by_alias``), and by the answer's output (``by_output``). A total's output reads its
    coarser call, whose own guard (``<cte>__guarded_base``) and probes stand at the top too."""
    from ..sql_ast import SqlIdentifier

    ctes = {cte.name: cte for cte in compiled["sql_ast"].ctes}
    projection = ctes["projected"].query if "projected" in ctes else compiled["sql_ast"]

    def probes(guard_name: str, start: str) -> dict[str, set[str]]:
        guard = ctes.get(guard_name)
        return (
            {
                field.alias: {
                    node.parts[0]
                    for node in sql_nodes(field.expression)
                    if isinstance(node, SqlIdentifier) and node.parts[0].startswith(start)
                }
                for field in guard.query.select
            }
            if guard is not None
            else {}
        )

    by_alias = probes(GUARDED_BASE, prefix)
    # Each coarser call's columns, by (its CTE, its column): the probes its own guard read.
    nested: dict[tuple[str, str], set[str]] = {}
    for name, cte in ctes.items():
        inner = probes(f"{name}__{GUARDED_BASE}", f"{name}__{prefix}")
        for field in cte.query.select if inner else ():
            read = set().union(*(inner.get(a, set()) for a in base_reads(field.expression)))
            nested[(name, field.alias)] = read
            by_alias[f"{name}.{field.alias}"] = read
    by_output = {
        field.alias: set().union(
            *(by_alias.get(a, set()) for a in base_reads(field.expression)),
            *(
                nested.get((node.parts[0], node.parts[1]), set())
                for node in sql_nodes(field.expression)
                if isinstance(node, SqlIdentifier) and len(node.parts) == 2
            ),
        )
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


def _settled_by(compiled, outputs):
    """Each output's measures, with the coverage CTEs and declared-edge CTEs that settled them."""
    from ..compiler_parts.aliasing import AliasRegistry

    by_alias, by_output = _guard_probe_names(compiled, "coverage_")
    edge_alias, edge_output = _guard_probe_names(compiled, "edge_")
    bound = [row.bound_measure for row in compiled["logical_plan"].measure_plans]
    aliases = AliasRegistry.for_plan(
        compiled["logical_plan"], measure_aliases=[b.alias for b in bound]
    )
    for output, item in outputs.items():
        for measure in item["measures"]:
            if "probes" in item:  # a balance's own coverage: a stock declares no edge
                yield output, measure, set(item["probes"]), set()
                continue
            leaves = [aliases.internal(b.alias) for b in bound if b.measure_id == measure]
            names = set().union(*(by_alias.get(a, set()) for a in leaves))
            edges = set().union(*(edge_alias.get(a, set()) for a in leaves))
            names &= by_output.get(output, set())
            edges &= edge_output.get(output, set())
            # Combined plans can settle the output itself instead of its leaf aliases.
            if not names and len(item["measures"]) == 1:
                names, edges = by_output.get(output, set()), edge_output.get(output, set())
            if names:
                yield output, measure, names, edges


# Said where only the last row bounds a period: how an author states a quiet, loaded table.
DECLARE_EDGE = (
    "If the load ran and nothing happened, declare zero_through (PACKAGE_AUTHORING.md, "
    '"Empty periods").'
)


def _no_data_yet_warnings(compiled, rows, payload, coverage, *, zone="UTC", excluded_outputs=()):
    """Disclose only edges proven by the same coverage that guarded the answer's NULLs: a
    measure's last row, or its declared edge (the load log's loaded-through date, which a
    stray later row never moves, or ``now``)."""
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
    _, by_output = _guard_probe_names(compiled, "coverage_")
    for name, item in outputs.items():
        if "probes" in item:  # a balance's own coverage
            by_output[name] = set(item["probes"])
    probes = sorted(set().union(*(by_output.get(name, set()) for name in outputs)))
    if not probes or any(f"{p}__loaded_to" not in coverage for p in probes):
        return []
    window = _requested_window(compiled, payload)
    time_key = time_output_alias(time)
    affected: list[str] = []
    measures: list[dict[str, Any]] = []
    for output, measure, names, declared in _settled_by(compiled, outputs):
        edges = [coverage[f"{p}__loaded_to"] for p in sorted(names)]
        firsts = [coverage.get(f"{p}__loaded_from") for p in sorted(names)]
        befores = [coverage.get(f"{e}__loaded_before") for e in sorted(declared)]
        if time.get("window_total") and 1 in firsts:
            continue
        if all(value is None for value in edges):
            pass  # No visible rows: an undated disclosure, never another caller's edge.
        elif time.get("window_total"):
            if not window["start"] or not all(
                value == 0 if declared else value == -1
                for value in (befores if declared else edges)
            ):
                continue
        else:
            # Require every mapped leaf to have a dated edge before claiming one.
            firsts = firsts if declared else []
            if not rows or any(value is None for value in [*edges, *firsts]):
                continue
            buckets = [row.get(time_key) for row in rows if row.get(output) is None]
            bounds = [value for value in befores if value is not None]
            values = [*edges, *firsts, *bounds, *buckets]
            # Compare SQL's bucket keys only, never raw bounds or coerced dates.
            if not (
                all(type(value) is date for value in values)
                or (
                    all(type(value) is datetime for value in values)
                    and len({value.utcoffset() is None for value in values}) == 1
                )
            ):
                continue
            # A declared edge certifies buckets before its first uncertified one (none when the
            # load log holds no date); the last row, buckets up to its own.
            if not any(
                bucket >= max(firsts) and (bucket >= min(bounds) if bounds else bool(declared))
                if declared
                else bucket > max(edges)
                for bucket in buckets
            ):
                continue
        edge, source = None, None
        if declared and any(value is not None for value in edges):
            through = [coverage.get(f"{e}__data_through") for e in declared]
            source = coverage.get(f"{min(declared)}__edge_source")
            if all(isinstance(value, datetime) for value in through):
                edge = local_date(min(through), zone)
        elif any(value is not None for value in edges):
            # The edge is the last row's day, read by the same coverage on every leaf.
            through = [coverage.get(f"{p}__data_through") for p in names]
            if not all(isinstance(value, datetime) for value in through):
                continue
            edge, source = local_date(max(through), zone), "last_row"
        detail = {"id": measure, "edge": edge, "edge_source": source}
        if detail not in measures:
            measures.append(detail)
        if output not in affected:
            affected.append(output)
    if not affected:
        return []
    total = time.get("window_total")
    later = (
        f"the window starting {window['start']} reads NULL"
        if total
        else f"later {time.get('grain') or 'period'}s read NULL"
    )
    declared_later = "this window's total reads NULL" if total else later
    # A balance ignores zero_through: its edge is its last snapshot, never carried forward.
    balances = {
        measure
        for item in compiled.get("balance_edges", [])
        if item["output"] in affected
        for measure in item["measures"]
    }
    messages = []
    for item in measures:
        named, day = item["id"], item["edge"]
        if item["edge_source"] == "last_row" and named in balances:
            messages.append(
                f"No data yet for {named}: its last snapshot is dated {day}, so {later}, never "
                "a carried-forward balance."
            )
        elif item["edge_source"] == "last_row":
            messages.append(
                f"No data yet for {named}: its last row is dated {day}. Nothing says this data "
                f"is loaded past it, so {later}, not 0. {DECLARE_EDGE}"
            )
        elif item["edge_source"] == "now" and day is not None:
            messages.append(
                f"No data yet for {named}: it counts as loaded through now ({day}), so "
                f"{declared_later}, not 0."
            )
        elif item["edge_source"] is not None and day is not None:
            messages.append(
                f"No data yet for {named}: its data is loaded through {day}, so "
                f"{declared_later}, not 0."
            )
        elif item["edge_source"] is not None:
            messages.append(
                f"No data yet for {named}: its loaded-through date is unavailable (its load log "
                "holds none), so its empty periods read NULL, not 0."
            )
        else:
            messages.append(
                f"No data yet for {named}, with no dated coverage edge, so it reads NULL, not 0."
            )
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


def _zero_after_last_row_notes(compiled, coverage, *, now, zone="UTC", excluded_outputs=()):
    """ZERO_AFTER_LAST_ROW for each sum or count counted as loaded through ``now``
    (``zero_through: now``) whose answer reaches a day after its last row: those days read 0
    because the package says the table is always loaded, as a stalled load would too."""
    time = compiled["logical_plan"].time or {}
    outputs = {
        item["output"]: item
        for item in compiled.get("zero_outputs", [])
        if item["output"] not in excluded_outputs
    }
    today = local_now(now, zone).date()
    reach = min(_last_day(engine_window(time, now=now, timezone=zone).end, zone) or today, today)
    last_rows: dict[str, str] = {}
    for _, measure, names, declared in _settled_by(compiled, outputs):
        last = [coverage.get(f"{p}__last_row", coverage.get(f"{p}__data_through")) for p in names]
        if (
            any(coverage.get(f"{e}__edge_source") == "now" for e in declared)
            and all(isinstance(value, datetime) for value in last)
            and date.fromisoformat(day := local_date(max(last), zone)) < reach
        ):
            last_rows[measure] = day
    if not last_rows:
        return []
    return [
        semantic_issue(
            code="ZERO_AFTER_LAST_ROW",
            message=" ".join(
                f"{measure} reads 0 after its last row ({day}) because the package says this "
                "table is always loaded; a stalled load would also read 0."
                for measure, day in last_rows.items()
            ),
            severity="info",
            stage="execution",
            details={
                "measures": [{"id": measure, "last_row": day} for measure, day in last_rows.items()]
            },
            object_ids=sorted(last_rows),
        )
    ]


def _partial_data_warnings(compiled, rows, coverage, *, now, zone="UTC"):
    """PARTIAL_DATA for a window total, or each period of a day-or-coarser series, whose span
    holds a whole day (in the clock's zone) after the answer's ``data_through`` and before
    ``now``'s day: its number covers only the days through that edge. A nightly load whose only
    gap is today's unfinished day says nothing, and a span wholly after the edge is
    ``NO_DATA_YET``'s to explain."""
    through, _ = data_through(coverage, zone)
    time = compiled["logical_plan"].time or {}
    grain = str(time.get("grain") or "")
    if through is None or not (time.get("window_total") or grain in UNITS):
        return []
    edge = date.fromisoformat(through)
    today = local_now(now, zone).date()
    window = engine_window(time, now=now, timezone=zone)
    first, last = _first_day(window.start, zone), _last_day(window.end, zone)
    spans: list[tuple[date | None, date, date]] = []
    if time.get("window_total"):
        if first is not None and last is not None:
            spans.append((None, first, last))
    else:
        for period in dict.fromkeys(_period_day(row.get(TIME_AXIS), zone) for row in rows):
            if period is not None:
                try:
                    end = period_end(period, grain) - timedelta(days=1)
                except (ValueError, OverflowError):
                    end = date.max  # the calendar's last period ends with it
                spans.append((period, max(period, first or period), min(end, last or end)))
    warnings = []
    for period, start, end in spans:
        end = min(end, today)
        # A whole day after the edge and before today, inside the span.
        if edge < start or edge >= min(end, today - timedelta(days=1)):
            continue
        days, covered = (end - start).days + 1, (min(edge, end) - start).days + 1
        span = "this total" if period is None else f"the {grain} of {period.isoformat()}"
        whole = "the window's" if period is None else "its"
        warnings.append(
            semantic_issue(
                code="PARTIAL_DATA",
                message=f"Data runs through {through}, so {span} covers {covered} of {whole} {days} days.",
                severity="warning",
                stage="execution",
                details={
                    "through": through,
                    "days_covered": covered,
                    "days": days,
                    **({"period": period.isoformat()} if period is not None else {}),
                },
            )
        )
    return warnings


def _first_day(value: str | None, zone: str) -> date | None:
    return None if value is None else _wall(value, zone).date()


def _last_day(value: str | None, zone: str) -> date | None:
    """The last day an exclusive end reaches: the day before a midnight, else its own day."""
    if value is None:
        return None
    end = _wall(value, zone)
    return end.date() - timedelta(days=1) if end.time() == datetime.min.time() else end.date()


def _wall(value: str, zone: str) -> datetime:
    """A window bound as wall time in the clock's zone: an instant with an offset is
    converted, local text is read as written."""
    bound = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return local_now(bound, zone) if bound.tzinfo is not None else bound


def _period_day(value: Any, zone: str) -> date | None:
    if isinstance(value, datetime):
        return (value.astimezone(ZoneInfo(zone)) if value.utcoffset() is not None else value).date()
    return value if isinstance(value, date) else None


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
