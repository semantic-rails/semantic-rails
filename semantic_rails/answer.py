"""The answer envelope: what a query, validate or compile response says beside its rows.

``answer_fields`` reads from the compiled call (and, for a query, its rows and loaded
coverage) what every answer states: ``window``, ``clock``, ``observation_scope`` (always, since
it decides 0 or NULL), ``routes`` (only when a route was chosen) and the assumptions the
call's time and entity filters imply. ``finish`` completes a response: a warning whose code
the registry lists as an assumption is stated under ``assumptions``, the echoed query is the
call, and empty fields go. Verbosity trims only afterwards
(``runtime_parts.responses.apply_response_verbosity``) and never removes a warning, an
assumption or a route.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import visible_view
from .ast import PERIOD_PARTIAL, TIME_AXIS, every_filter, normalize_query
from .caveats import _expr_children
from .clock import (
    describe_window,
    engine_window,
    format_now,
    local_date,
    parse_now,
    period_is_partial,
)
from .compiler import observed_only_groups, observed_periods_reason, read_routes
from .compiler_parts.empty_groups import observation_scope
from .compiler_parts.listings import is_listing_plan, listing_config
from .compiler_parts.parent_values import PARENT_VALUE
from .diagnostics import lean_issue, semantic_issue
from .errors import SemanticLayerError
from .expressions import collect_object_references, expr_to_dict
from .fanout import (
    build_hop_profile,
    offered_rows,
    package_route,
    query_route_decisions,
    route_note,
    route_reading,
)
from .issues import ASSUMPTION, CODES
from .query_call import call_from_query, call_where
from .request_context import context_from_policy_context

# Said when a time window with no grain was aggregated as one total.
WINDOW_TOTAL_MESSAGE = (
    "The time window had no grain, so it was aggregated as one total over the window with no "
    "time column. Set time.grain to get one row per period."
)

_OVER_MEANING = {
    "all_time": "over all of its rows, ignoring the query window",
    "window": "over its rows inside the query window",
    "period": "per output period",
}
_EMPTY: tuple[Any, ...] = (None, "", [], {})
# An empty result is still an answer.
_KEPT_EMPTY = frozenset({"rows"})


def mark_partial_periods(
    rows: list[dict[str, Any]], compiled: Mapping[str, Any], *, now: datetime, zone: str
) -> None:
    """``period_partial`` on each row of a grain series, right after ``period``: whether the
    row's period ends after ``now`` in the clock's zone."""
    grain = (compiled["logical_plan"].time or {}).get("grain")
    if not grain:
        return
    for row in rows:
        period = row.get(TIME_AXIS)
        if isinstance(period, datetime) and period.utcoffset() is not None:
            period = period.astimezone(ZoneInfo(zone))
        marked: dict[str, Any] = {}
        for key, value in row.items():
            if key != PERIOD_PARTIAL:
                marked[key] = value
            if key == TIME_AXIS:
                marked[PERIOD_PARTIAL] = period_is_partial(period, grain, now, zone)
        row.clear()
        row.update(marked)


def answer_fields(
    config: Any,
    compiled: Mapping[str, Any],
    *,
    now: datetime,
    zone: str,
    coverage: Mapping[str, Any] | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What an answer's time, routes and filters mean, said on every response.

    ``window`` ({start, end, now, partial, data_through}, only the keys that apply) whenever a
    window or a grain series was resolved; ``data_through`` is the date, in the clock's zone, of
    the last row at or before ``now`` that the answer's coverage read (``coverage``), so it is
    known only once the query ran on a warehouse with loaded coverage. ``clock`` whenever a
    time-bearing measure was read. A ``this`` window is assumed partial ("October 2026 so far
    (8 of 31 days)").
    """
    plan = compiled["logical_plan"]
    time = plan.time or {}
    out: dict[str, Any] = {}
    assumptions: list[dict[str, Any]] = []
    if time.get("window_total"):
        assumptions.append({"code": "WINDOW_TOTAL", "message": WINDOW_TOTAL_MESSAGE})
    assumptions.extend(_entity_filter_over(plan.query))
    if time.get("grain") or time.get("start") is not None or time.get("end") is not None:
        window = engine_window(time, now=now, timezone=zone)
        # A ``this`` window, or one with only a start, ends at now: state that instant with its
        # offset, as ``now`` is.
        end = _instant(now, zone) if "this" in window.spec or time.get("to_now") else window.end
        shown = {"start": window.start, "end": end, "now": format_now(now)}
        out["window"] = {key: value for key, value in shown.items() if value is not None}
        if window.start is not None or window.end is not None:
            out["window"]["partial"] = window.partial
        elif rows:
            out["window"]["partial"] = any(row.get(PERIOD_PARTIAL) for row in rows)
        through, source = data_through(coverage, zone)
        if through is not None:
            out["window"]["data_through"] = through
            if source:
                out["window"]["data_through_source"] = source
        if "this" in window.spec:
            text = describe_window(window, now=now, timezone=zone)
            assumptions.append({"code": "PARTIAL_PERIOD", "message": text})
        query = normalize_query(plan.query, config=config)
        if held := observed_only_groups(query, config):
            assumptions.append(
                {
                    "code": "OBSERVED_PERIODS_ONLY",
                    "message": (
                        f"The rows are grouped by a date or time dimension ({held[0]}), so the "
                        "window's empty periods are not filled: only rows with data are shown."
                    ),
                    "group_by": held,
                }
            )
        elif reason := observed_periods_reason(query, config):
            assumptions.append(
                {
                    "code": "OBSERVED_PERIODS_ONLY",
                    "message": (
                        f"The window's empty periods are not filled because {reason}: only "
                        "periods with data are shown."
                    ),
                    "reason": reason,
                }
            )
    clocks = (
        {time["temporal_role"]}
        if time.get("temporal_role")
        else {row.temporal_role for row in plan.bound_measures if row.temporal_role}
    )
    if len(clocks) == 1:
        out["clock"] = clocks.pop()
    out["observation_scope"] = observation_scope(plan.query, config)
    # A listing's routes are its own (``compiler_parts.listings``).
    routed = listing_config(config) if is_listing_plan(plan) else config
    out["routes"] = routes_taken(routed, compiled)
    out["assumptions"] = assumptions
    return out


def data_through(coverage: Mapping[str, Any] | None, zone: str) -> tuple[str | None, str]:
    """The answer's edge: the date, in the clock's zone, of the earliest edge among the coverage
    the answer read (None when it read none), and how it is known: "" for a last row at or
    before ``now``, else a declared edge's name (``now``, or the load log as the package
    declares it, ``loaded`` where the caller doesn't see it)."""
    edges = [
        (_utc(value), str((coverage or {}).get(key.removesuffix("data_through") + "declared", "")))
        for key, value in (coverage or {}).items()
        if key.endswith("__data_through") and isinstance(value, datetime)
    ]
    if not edges:
        return None, ""
    instant, source = min(edges, key=lambda edge: edge[0])
    return local_date(instant, zone), source


def _utc(instant: datetime) -> datetime:
    """``instant`` as naive UTC, as coverage reads it."""
    return instant if instant.tzinfo is None else instant.astimezone(UTC).replace(tzinfo=None)


def load_log_name(config: Any, rows: Any) -> str:
    """The load log a declared edge's mark reads (``rows``: ``MAX`` of its column over its
    relation), as the package declares it, where the caller's view shows that column: else
    ``loaded``, which names nothing hidden."""
    relation = rows.from_table.name
    column = rows.select[0].expression.args[0].parts[-1]
    owners = {row.id for row in config.entities if row.table == relation}
    shown = any(row.entity in owners and row.column == column for row in config.dimensions)
    names = {
        row.zero_through.name
        for row in config.measures
        if row.zero_through is not None
        and (row.zero_through.relation, row.zero_through.column) == (relation, column)
    }
    return names.pop() if shown and len(names) == 1 and "" not in names else "loaded"


def _instant(now: datetime, zone: str) -> str:
    """``now`` as wall time in the clock's zone with its offset (``Z`` for UTC)."""
    local = now.astimezone(ZoneInfo(zone)).isoformat()
    return local.replace("+00:00", "Z")


def _entity_filter_over(query: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each entity filter's ``over``: how each entity's aggregate was taken."""
    return [
        {
            "code": "ENTITY_FILTER_OVER",
            "entity": row["entity"],
            "over": row["over"],
            "message": f"Each {row['entity']}'s aggregate was taken {_OVER_MEANING[row['over']]}.",
        }
        for row in call_where([], query.get("metric_filters"))
    ]


def routes_taken(config: Any, compiled: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each entity pair whose route was chosen among two or more, as ``{from, to, via}``. A pair
    with one route had nothing to choose and is not listed.

    A route the call chose among the options of the pair's refusal names up to three of the
    other routes it offered (``other_routes``, each ready to send as ``routes``; the
    rest counted in ``more_routes``): one-step switches, and no alternative ever runs. A
    compact or full response also says how each route was chosen (``basis``): by the call's
    ``routes`` (with the package's basis it ``replaced``), by the start's own key
    (``own_key``, with the row that would make each other route the default in
    ``alternatives`` when it would load, else the rows it disagrees with in
    ``conflicts_with``), by the package's recorded rows (``recorded``, with the pairs whose
    rows it follows in ``recorded_for`` when it inherits them), or by a lookup default
    (``default``, with the rung that chose it in ``by``, its ``meaning`` and the other routes
    as ``other_routes`` and ``alternatives``). A default's ``meaning`` and ``other_routes`` stay
    at every verbosity: the reading the engine chose is never silent."""
    taken: list[dict[str, Any]] = []
    for row in compiled.get("route_decisions") or []:
        start, target, path = row["source_entity"], row["target_entity"], row["relationship_path"]
        route: dict[str, Any] = {"from": start, "to": target, "via": list(path)}
        offered = _offered(config, start, target) if row["replaced"] == "undecided" else []
        if list(path) in offered:
            others = [other for other in offered if other != list(path)]
            route["other_routes"] = [
                {"from": start, "to": target, "via": list(other)} for other in others[:3]
            ]
            route["more_routes"] = len(others) - 3 if len(others) > 3 else None
        route.update(basis="routes", replaced=row["replaced"])
        taken.append(route)
    seen = {(row["from"], row["to"]) for row in taken}
    for start, target, path in read_routes(
        compiled["logical_plan"], compiled.get("route_choices") or []
    ):
        resolution = None if (start, target) in seen else route_note(config, start, target, path)
        if resolution is None:
            continue
        seen.add((start, target))
        route = {"from": start, "to": target, "via": list(resolution.routes[0])}
        if resolution.basis == "colocated_key":
            alternatives, conflicts = offered_rows(config, start, target, resolution.routes[1:])
            route.update(basis="own_key", alternatives=alternatives, conflicts_with=conflicts)
        elif resolution.basis == "default":
            meaning = route_reading(config, start, resolution.routes[0])
            others = sorted((list(path) for path in resolution.routes[1:]), key=_route_order)
            if not others:
                # The caller's view hides every route the default was chosen over: the reading
                # is stated, and nothing says there were others.
                route["meaning"] = meaning
                taken.append(route)
                continue
            alternatives, conflicts = offered_rows(config, start, target, others)
            route.update(
                basis="default",
                by=resolution.by,
                meaning=meaning,
                other_routes=[{"from": start, "to": target, "via": path} for path in others[:3]],
                more_routes=len(others) - 3 if len(others) > 3 else None,
                alternatives=alternatives,
                conflicts_with=conflicts,
            )
        else:
            inherited = [{"from": source, "to": end} for source, end in resolution.rows]
            route.update(basis="recorded", recorded_for=inherited)
        taken.append(route)
    return [{key: value for key, value in route.items() if value not in _EMPTY} for route in taken]


# What a minimal response keeps of each route; a default also keeps how it was chosen.
MINIMAL_ROUTE_KEYS = ("from", "to", "via", "meaning", "other_routes", "more_routes")
_MINIMAL_DEFAULT_KEYS = ("basis", "by")


def minimal_route(route: Mapping[str, Any]) -> dict[str, Any]:
    """``route`` (an answer's ``routes`` entry) as a minimal response keeps it."""
    keys = (*MINIMAL_ROUTE_KEYS, *(_MINIMAL_DEFAULT_KEYS if "by" in route else ()))
    return {key: route[key] for key in keys if key in route}


def read_from(
    config: Any, compiled: Mapping[str, Any], out: Mapping[str, Any]
) -> dict[str, list[str]]:
    """Which model each value of the answer comes from, by id, as the compiled call reads it:
    ``{entity: [ids]}``.

    Each measure or metric the call selects is listed under its model's entity (under each, when
    a metric reads several), and each group_by and where dimension under its own. When the SQL
    reached a dimension's entity through relationships whose route the response doesn't state,
    those relationship ids follow its values, each once, in the order the SQL read them. A route
    is stated by ``routes`` (chosen among several), a ``ROUTE_PASS_THROUGH`` or ``LISTED_THROUGH``
    note, or a fan-out note's ``paths``. An entity ``routes`` names, with no unstated route into
    it, is left out: the response already says the call reads it. ``config`` is the caller's view, which the call
    compiled on, so nothing here names an object the caller can't see."""
    query = dict(compiled["explain"].normalized_query or {})
    measures = {row.id: row.entity for row in config.measures}
    recipes = {row.id: row for row in config.metric_recipes}
    dimensions = {row.id: row.entity for row in config.dimensions}
    entities = {row.id for row in config.entities}
    relationships = {row.id for row in config.relationships}

    def models(ref: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if ref in measures:
            return {measures[ref]}
        recipe = recipes.get(ref)
        if recipe is None or ref in seen:
            return set()
        inner = collect_object_references(expr_to_dict(recipe.expression))
        return {entity for item in inner for entity in models(item, seen | {ref})}

    values: dict[str, dict[str, None]] = {}
    hops: dict[str, dict[str, None]] = {}
    for item in query.get("select") or []:
        for ref in collect_object_references(dict(item.get("expression") or {})):
            for entity in sorted(models(ref) & entities):
                values.setdefault(entity, {})[ref] = None
    stated = _stated_routes(config, out)
    reads = read_routes(compiled["logical_plan"], compiled.get("route_choices") or [])
    where = [row for row in every_filter(query.get("where")) if isinstance(row, Mapping)]
    fields = [
        *(field for field in query.get("group_by") or [] if isinstance(field, str)),
        *(str(row.get("field") or "") for row in where),
    ]
    for field in dict.fromkeys(fields):
        target = dimensions.get(field)
        if target is None or target not in entities:
            continue
        values.setdefault(target, {})[field] = None
        for start, end, path in reads:
            if end == target and path and (start, end) not in stated and set(path) <= relationships:
                hops.setdefault(target, {}).update(dict.fromkeys(path))
    named = {entity for row in out.get("routes") or [] for entity in (row["from"], row["to"])}
    return {
        entity: [*ids, *hops.get(entity, {})]
        for entity, ids in values.items()
        if entity not in named or hops.get(entity)
    }


def _stated_routes(config: Any, out: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The (from, to) pairs whose route the response already states: its ``routes`` and its
    route notes (``ROUTE_PASS_THROUGH``, ``LISTED_THROUGH``, a fan-out note's ``paths``)."""
    measures = {row.id: row.entity for row in config.measures}
    pairs = {(row["from"], row["to"]) for row in out.get("routes") or []}
    for note in out.get("warnings") or []:
        details = dict(note.get("details") or {})
        if note.get("code") == "ROUTE_PASS_THROUGH":
            pairs.add(tuple(note["object_ids"][:2]))
        elif note.get("code") == "LISTED_THROUGH":
            pairs.update((row["from"], row["to"]) for row in details.get("routes") or [])
        elif note.get("code") == "REWRITE_APPLIED" and isinstance(details.get("paths"), dict):
            start = measures.get(next(iter(note.get("object_ids") or []), ""), "")
            pairs.update((start, target) for target in details["paths"])
    return pairs


def _route_order(path: Sequence[str]) -> tuple[int, Sequence[str]]:
    """Alternatives shortest first, then by relationship ids, whatever the authored order."""
    return len(path), path


def _offered(config: Any, start: str, target: str) -> list[list[str]]:
    """The routes the pair's AMBIGUOUS_PATH refusal offers the caller (none when it has none)."""
    try:
        package_route(config, start=start, target=target)
    except SemanticLayerError as exc:
        if exc.code == "AMBIGUOUS_PATH":
            options = exc.details.get("clarification", {}).get("options", [])
            return [list(option["relationship_path"]) for option in options]
    return []


def finish(
    out: dict[str, Any],
    *,
    payload: Mapping[str, Any],
    compiled: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A response as a caller reads it, before verbosity trims it.

    A warning whose code ``issues.CODES`` lists as an assumption (a disclosure any part of the
    engine writes) moves to ``assumptions`` as ``{code, message, **details}``: registering a
    code there is all a new disclosure needs. ``query`` and ``normalized_query`` echo the call
    (``call_from_query``), never the engine form with its identity and clock. Empty fields go,
    from the response (except ``rows``) and from each warning."""
    warnings = list(out.get("warnings") or [])
    stated = [row for row in warnings if CODES.get(str(row.get("code"))) == ASSUMPTION]
    out["warnings"] = [lean_issue(row) for row in warnings if row not in stated]
    out["errors"] = [lean_issue(row) for row in out.get("errors") or []]
    out["assumptions"] = [
        *(out.get("assumptions") or []),
        *(
            {
                "code": row["code"],
                "message": row.get("message", ""),
                **dict(row.get("details") or {}),
            }
            for row in stated
        ),
    ]
    out["query"] = _call(payload)
    if "normalized_query" in out and compiled is not None:
        out["normalized_query"] = _call(compiled["explain"].normalized_query or {})
    return {key: value for key, value in out.items() if key in _KEPT_EMPTY or value not in _EMPTY}


def _call(query: Mapping[str, Any]) -> dict[str, Any] | None:
    """The call that states ``query``; None when it states none (a refused call)."""
    try:
        return call_from_query(query)
    except (SemanticLayerError, KeyError, TypeError, AttributeError):
        return None


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


def _policy_context(payload: dict[str, Any]) -> dict[str, Any]:
    context = dict(payload.get("policy_context", {}) or {})
    normalized = context_from_policy_context(context).to_policy_context()
    if context.get("now") not in (None, ""):
        normalized["now"] = context.get("now")
    return normalized


def _answer_now(payload: dict[str, Any] | None) -> datetime:
    """The instant relative windows resolved against for this request."""
    return parse_now(_policy_context(payload or {}).get("now"))


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
        # surrogatepass: a malformed string still hashes, so the summary never fails.
        "sql_sha256": (
            hashlib.sha256(body.encode("utf-8", errors="surrogatepass")).hexdigest() if body else ""
        ),
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
    seed_warnings: list[dict[str, Any]] | None = None,
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
    if seed_warnings:
        details["warnings"] = seed_warnings
    return details


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


def _withheld_columns(policy_effects: list[dict[str, Any]]) -> set[str]:
    """The outputs excluded from both value diagnostics and public result metadata."""
    return {row["withheld_column"] for row in policy_effects if row.get("withheld_column")}


def _withhold_values(out: dict[str, Any], policy_effects: list[dict[str, Any]], config) -> None:
    """Drop the column of a rank by withheld values, and name the withheld objects instead."""
    visible_view.require_base(config)
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


def _freshness_by_leaf(config, compiled) -> list[dict[str, Any]]:
    entities = {row.id: row for row in config.entities}
    measures = {row.id: row for row in config.measures}
    aggregate_by_relation = {str(row.relation): row for row in config.aggregate_relations}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    read = []
    for plan in list(getattr(compiled["logical_plan"], "measure_plans", []) or []):
        read.append((plan, plan.source_entity))
        # A parent value's leaf reads the rows' model and takes its value from the measure's.
        measure = measures.get(plan.bound_measure.measure_id)
        if plan.rewrite_strategy == PARENT_VALUE and measure is not None:
            read.append((plan, measure.entity))
    for plan, entity_id in read:
        entity = entities.get(entity_id)
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
    """``expr`` and every node under it, parents first."""
    return [expr, *(row for child in _expr_children(expr) for row in _walk_expr_payload(child))]


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
    return hints
