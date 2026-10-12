"""The query call: the caller's spelling, read once into the engine's query.

A call has nine fields: ``select``, ``group_by``, ``where``, ``having``, ``time``,
``order_by``, ``limit``, ``routes`` and ``observation_scope``. This module is the only reader
of that spelling. It checks the call against the tables below (the published schema is
generated from the same tables), resolves the time window against ``now``, and hands the
compiler its own form, which :func:`semantic_rails.ast.normalize_query` reads.

Identity never travels in a call: it comes from credentials. ``now`` and the rendering
options (``max_rows``, ``verbosity``, ...) are arguments beside the call.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from difflib import get_close_matches
from typing import Any

from . import operators
from .clock import UNITS, largest_last, parse_now, resolve_window
from .errors import SemanticLayerError
from .schema import OBSERVATION_SCOPES, PackageConfig

SCHEMA_ID = "https://semantic-rails.com/schemas/query.v2.json"

# Keys that say who is asking; identity comes only from credentials or ``context``.
IDENTITY_KEYS: frozenset[str] = frozenset({"policy_context"})


def identity_claim_path(node: Any, prefix: str = "") -> str | None:
    """The path of the shallowest identity key anywhere in ``node`` (annotations included),
    or None: ``policy_context``, ``query.policy_context``, ``query._note.policy_context``."""
    pending: list[tuple[str, Any]] = [(prefix, node)]
    while pending:
        nested: list[tuple[str, Any]] = []
        for path, value in pending:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    child = f"{path}.{key}" if path else str(key)
                    if key in IDENTITY_KEYS:
                        return child
                    nested.append((child, item))
            elif isinstance(value, (list, tuple)):
                nested.extend((f"{path}[{index}]", item) for index, item in enumerate(value))
        pending = nested
    return None


FIELDS: tuple[str, ...] = (
    "select",
    "group_by",
    "where",
    "having",
    "time",
    "order_by",
    "limit",
    "routes",
    "observation_scope",
)
TIME_KEYS: tuple[str, ...] = ("temporal_role", "grain", "start", "end", "last", "this", "fill")
GRAINS: tuple[str, ...] = UNITS
LAST_KEYS: tuple[str, ...] = ("unit", "value")
DIRECTIONS: tuple[str, ...] = ("asc", "desc")
ORDER_KEYS: tuple[str, ...] = ("field", "direction")
ROUTE_KEYS: tuple[str, ...] = ("from", "to", "via")
CONDITION_KEYS: tuple[str, ...] = ("field", "op", "value")
SELECT_KEYS: tuple[str, ...] = ("expression", "as")
ENTITY_FILTER_KEYS: tuple[str, ...] = ("entity", "any", "none", "having", "over")
# How an entity filter's aggregate is taken, per entity and never grouped by the query's
# other dimensions: over every row, over the rows in the query window, or per output period.
OVER: tuple[str, ...] = ("all_time", "window", "period")
REFERENCE_PREFIXES: tuple[str, ...] = ("measure.", "metric.")
TIME_AXIS = "period"

# Keys that are not part of a call, with where each one went.
RETIRED: dict[str, str] = {
    "policy_context": (
        "Identity never travels in the call: pass it as the `context` argument (HTTP and MCP "
        "read it from credentials), and `now` beside the query."
    ),
    "metric_filters": (
        "Filter results with `having: [{field, op, value}]`, or entities with an entity "
        "filter in `where`."
    ),
    "route_decisions": "Write `routes: [{from, to, via}]`.",
    "temporal_role_overrides": "Pin a measure's clock in a package metric (`temporal_role:`).",
    "limits": "Pass `max_rows` as a tool argument.",
    "explain": "Ask the `execute` tool with mode `sql` for the SQL.",
    "export": "Removed.",
    "request_id": "Send it as the X-Request-Id header (HTTP) or in MCP `_meta`.",
}
# Arguments of the tools, never keys of the call.
TOOL_ARGUMENTS: frozenset[str] = frozenset(
    {"now", "mode", "max_rows", "row_format", "verbosity", "sql_profile", "debug"}
)
IGNORED: frozenset[str] = frozenset({"version"})
# Keys callers send for one of the call's fields.
KEY_SYNONYMS: dict[str, str] = {
    "filter": "where",
    "filters": "where",
    "dimensions": "group_by",
    "groupby": "group_by",
    "measures": "select",
    "metrics": "select",
    "orderby": "order_by",
    "sort": "order_by",
    "route": "routes",
}
# Keys of an inline kind that name another expression; a reference there is an id string.
EXPRESSION_KEYS: frozenset[str] = frozenset(
    {
        "expression",
        "input",
        "left",
        "right",
        "numerator",
        "denominator",
        "base",
        "converted",
        "condition",
        "expr",
        "low",
        "high",
        "date",
        "null_value",
    }
)
EXPRESSION_LISTS: frozenset[str] = frozenset({"args", "values"})
# The over words of a predicate, in the engine's own scope terms.
_PREDICATE_SCOPES: dict[str, dict[str, str]] = {
    "all_time": {"scope_mode": "entity_only"},
    "window": {"scope_mode": "entity_only", "time_alignment": "query_window"},
    "period": {"scope_mode": "entity_period"},
}
_PREDICATE_RETIRED = ("scope_mode", "time_grain", "time_alignment")
# The words that compare an aggregate: an entity's value is never text to search or unknown.
_AGGREGATE_WORDS = ("is", "not", "gt", "gte", "lt", "lte")


V2_POINTER = f"See the query schema at {SCHEMA_ID}."

# Inline kinds a call may not use, and keys of a kind that left the call.
RETIRED_KINDS: frozenset[str] = frozenset({"metric", "measure_ref"})
RETIRED_KIND_KEYS: dict[str, frozenset[str]] = {
    "prior_period": frozenset({"measure", "grain", "aggregation"}),
    "metric_predicate": frozenset(_PREDICATE_RETIRED),
}
ADDED_KIND_KEYS: dict[str, frozenset[str]] = {"metric_predicate": frozenset({"over"})}
# The keys each kind cannot do without (the published schema's ``required``).
KIND_REQUIRED: dict[str, tuple[str, ...]] = {
    "measure": ("measure",),
    "aggregate": ("measure",),
    "semi_additive": ("measure",),
    "scoped_aggregate": ("measure",),
    "column": ("column",),
    "literal": ("value",),
    "arithmetic": ("op", "left", "right"),
    "binary": ("op", "left", "right"),
    "comparison": ("op", "left", "right"),
    "boolean": ("op", "args"),
    "call": ("name",),
    "date_add": ("unit", "value", "date"),
    "in": ("values",),
    "not_in": ("values",),
    "between": ("expr", "low", "high"),
    "not_between": ("expr", "low", "high"),
    "nullif": ("value", "null_value"),
    "case": ("whens",),
    "cumulative": ("input",),
    "rolling": ("input", "window"),
    "prior_period": ("input", "offset"),
    "period_to_date": ("input", "period"),
    "metric_predicate": ("entity", "input", "op", "value", "over"),
    "aggregate_if": ("aggregation", "condition"),
    "ratio": ("numerator", "denominator"),
    "share": ("input", "over"),
    "entity_value": ("entity", "input"),
    "distribution": ("function", "over"),
    "conversion": ("base", "converted", "entity", "window", "matching_mode"),
}


def call_kinds() -> dict[str, tuple[str, ...]]:
    """Each inline kind a call may use, with its keys: the parser's table, as the call spells it."""
    from .expressions import _VALID_KEYS_BY_KIND

    return {
        kind: tuple(
            sorted(
                (set(keys) - RETIRED_KIND_KEYS.get(kind, frozenset()))
                | ADDED_KIND_KEYS.get(kind, frozenset())
            )
        )
        for kind, keys in sorted(_VALID_KEYS_BY_KIND.items())
        if kind not in RETIRED_KINDS
    }


# Each expression shape the capabilities surface documents: its name, what it does, and an
# example call fragment.
EXPRESSION_SHAPES: tuple[dict[str, Any], ...] = (
    {
        "name": "aggregate",
        "description": (
            "a measure with a statistic the package allows; a plain measure or metric is "
            "its id string"
        ),
        "example": {"kind": "measure", "measure": "measure.<id>", "aggregation": "sum"},
    },
    {
        "name": "metric",
        "description": "governed metric recipe: its id string is the expression",
        "example": "metric.<id>",
    },
    {
        "name": "prior_period",
        "description": "the input one or more periods earlier; needs time.grain",
        "example": {
            "kind": "prior_period",
            "input": "measure.<id>",
            "offset": {"unit": "month", "value": 1},
        },
    },
    {
        "name": "rolling",
        "description": "rolling window. window is an object {unit, value} — not an int.",
        "example": {
            "kind": "rolling",
            "input": "measure.<id>",
            "window": {"unit": "day", "value": 7},
        },
    },
    {
        "name": "cumulative",
        "description": (
            "running total. grain comes from the call's time.grain — "
            "cumulative itself takes only input + optional partition_by/window_scope."
        ),
        "example": {"kind": "cumulative", "input": "measure.<id>"},
    },
    {
        "name": "ratio",
        "description": "derived ratio",
        "example": {
            "kind": "ratio",
            "numerator": "measure.<num>",
            "denominator": "measure.<den>",
        },
    },
    {
        "name": "conversion",
        "description": (
            "event-pair conversion. Anchored to a single entity; window is an "
            "object {unit, value}; matching_mode is required. base/converted "
            "operands accept an aggregate with a 'filter' of dimension "
            "predicates ({all: [{field, op, value}]}) to restrict each side — "
            "e.g. base = orders containing Product A, converted = orders "
            "containing Product B; the filters are applied in the event CTEs "
            "with EXISTS semantics. Operand-level 'window' specs and "
            "expression-valued filter clauses are rejected (use the call's "
            "having or an entity filter for those)."
        ),
        "example": {
            "kind": "conversion",
            "base": {
                "kind": "aggregate",
                "measure": "measure.<start>",
                "filter": {"all": [{"field": "dimension.<id>", "op": "is", "value": "<value>"}]},
            },
            "converted": "measure.<end>",
            "entity": "entity.<id>",
            "window": {"unit": "day", "value": 7},
            "matching_mode": "first_converted_after_base",
        },
    },
    {
        "name": "distribution",
        "description": (
            "distribution view over entity-grain values. `over` must be an "
            "entity_value expression; `function` selects the aggregator "
            "(e.g. percentile)."
        ),
        "example": {
            "kind": "distribution",
            "function": "percentile",
            "over": {
                "kind": "entity_value",
                "entity": "entity.<id>",
                "input": "measure.<id>",
            },
            "p": 0.5,
        },
    },
    {
        "name": "aggregate_if",
        "description": (
            "shorthand for <AGG>(CASE WHEN cond THEN value END). Column refs "
            "inside `condition` / `value` must specify `entity` or `table` "
            "(no surrounding measure to inherit from). It aggregates the rows "
            "of the value's entity; `condition` may also read entities that "
            "entity reaches over many-to-one relationships. A value row with no "
            "match there never satisfies it; a condition such a row could "
            "satisfy (a missing test, OR with the value's own column) is refused. Compiles to "
            "COUNT_IF / SUM_IF natively on Snowflake; portable CASE WHEN "
            "elsewhere. `value` may be omitted when aggregation=count."
        ),
        "example": {
            "kind": "aggregate_if",
            "aggregation": "sum",
            "condition": {
                "kind": "comparison",
                "op": "=",
                "left": {"kind": "column", "column": "<col>", "entity": "entity.<id>"},
                "right": {"kind": "literal", "value": True},
            },
            "value": {"kind": "column", "column": "<col>", "entity": "entity.<id>"},
        },
    },
    {
        "name": "between",
        "description": (
            "range predicate sugar; desugars at parse time to "
            "`expr >= low AND expr <= high`. Use `kind: not_between` or "
            "`negated: true` for the inverted form (lowers to "
            "`expr < low OR expr > high`)."
        ),
        "example": {
            "kind": "between",
            "expr": {"kind": "column", "column": "<col>"},
            "low": {"kind": "literal", "value": 0},
            "high": {"kind": "literal", "value": 100},
        },
    },
)


def refusal(
    path: str, message: str, *, fix: str = "", code: str = "INVALID_QUERY", **details: Any
) -> SemanticLayerError:
    """One error object: the path, what is wrong, and the one hint to follow."""
    hints = [{"code": "FIX_QUERY_CALL", "message": fix}] if fix else []
    return SemanticLayerError(
        code, f"{path}: {message}", details={"path": path, **details, "recovery_hints": hints}
    )


def _closest(name: str, candidates: Any) -> list[str]:
    return get_close_matches(str(name), list(candidates), n=3, cutoff=0.6)


# Keys a caller often sends for the call's own key.
_KEY_FOR: dict[str, str] = {
    "dimension": "field",
    "aggregation_params": "parameters",
    "params": "parameters",
}


def _check_keys(item: Mapping[str, Any], allowed: tuple[str, ...], path: str) -> None:
    unknown = sorted(key for key in item if key not in allowed)
    if unknown:
        key = unknown[0]
        meant = _KEY_FOR.get(key)
        matches = [meant] if meant in allowed else _closest(key, allowed)
        raise refusal(
            f"{path}.{key}",
            f"unknown key; the keys are {list(allowed)}",
            fix=f"Use '{matches[0]}'." if matches else f"Remove '{key}'.",
            supported_keys=list(allowed),
        )


@dataclass(frozen=True)
class CallContext:
    """What reading a call needs beyond the call: the package and the clock."""

    config: PackageConfig | None
    now: datetime
    # The default clock of a selected measure or metric id ("" when it reads no time).
    default_clock: Callable[[str], str] | None = None
    partial: bool = False
    notes: list[dict[str, Any]] = field(default_factory=list)
    # The call's group_by ids (dimensions lifted from select join it) and whether its time
    # has a grain: the columns a share may take its total over.
    group_by: list[str] = field(default_factory=list)
    timed: bool = False


def read_call(
    payload: Any,
    *,
    config: PackageConfig | None = None,
    now: Any = None,
    default_clock: Callable[[str], str] | None = None,
    partial: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(engine query, notes)`` for a call. ``partial`` reads a plan's or builder's draft."""
    if not isinstance(payload, Mapping):
        raise refusal(
            "query",
            "a query call is a JSON object",
            fix=V2_POINTER,
            received=type(payload).__name__,
        )
    _check_top_level(payload)
    group_by = _group_by(payload.get("group_by"))
    raw_time = payload.get("time")
    timed = isinstance(raw_time, Mapping) and bool(raw_time.get("grain"))
    context = CallContext(
        config=config,
        now=parse_now(now),
        default_clock=default_clock,
        partial=partial,
        group_by=group_by,
        timed=timed,
    )
    select = _select(payload.get("select"), group_by, context)
    internal: dict[str, Any] = {"select": select, "group_by": group_by}
    where, entity_having = _where(payload.get("where"), context, payload)
    internal["where"] = where
    aliases = {row["as"]: row["expression"] for row in select}
    metric_filters = [*entity_having, *_having(payload.get("having"), aliases)]
    if metric_filters:
        internal["metric_filters"] = metric_filters
    time = _time(payload.get("time"), select, context)
    if time is not None:
        internal["time"] = time
    if "order_by" in payload:
        internal["order_by"] = _order_by(payload.get("order_by"))
    if "limit" in payload:
        internal["limit"] = _limit(payload.get("limit"))
    if payload.get("routes") is not None:
        internal["route_decisions"] = _routes(payload.get("routes"))
    if payload.get("observation_scope") is not None:
        scope = payload["observation_scope"]
        if scope not in OBSERVATION_SCOPES:
            raise refusal(
                "observation_scope",
                f"must be one of {list(OBSERVATION_SCOPES)}",
                fix=f"Use one of {list(OBSERVATION_SCOPES)}.",
            )
        internal["observation_scope"] = scope
    if not partial and not (select or group_by or time):
        raise refusal(
            "select", "a call needs select, group_by or time", fix="Name a measure or metric."
        )
    return internal, context.notes


def engine_where(
    where: Any, *, grain: str = "", config: PackageConfig | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A call's ``where`` in the engine's form: ``(row filters and child groups, entity filters
    as metric_filters)``. For a clarification option the engine re-binds."""
    context = CallContext(config=config, now=parse_now(None))
    return _where(where, context, {"time": {"grain": grain}} if grain else {})


def _check_top_level(payload: Mapping[str, Any]) -> None:
    for key in payload:
        if key in IGNORED or str(key).startswith("_"):
            # Ignored and annotation keys are skipped, but an identity claim inside one is not.
            claim = identity_claim_path(payload[key], str(key))
            if claim is not None:
                raise refusal(claim, "is not part of a query call", fix=RETIRED["policy_context"])
            continue
        if key in FIELDS:
            continue
        if key in RETIRED:
            raise refusal(
                key, "is not part of a query call", fix=f"{_retired_fix(key, payload)} {V2_POINTER}"
            )
        if key in TOOL_ARGUMENTS:
            raise refusal(
                key,
                "is an argument of the tool, not a key of the query",
                fix=f"Pass `{key}` beside `query`.",
            )
        matches = [KEY_SYNONYMS[key]] if key in KEY_SYNONYMS else _closest(key, FIELDS)
        raise refusal(
            key,
            f"unknown key; a call has {list(FIELDS)}",
            fix=f"Use '{matches[0]}'." if matches else f"Remove '{key}'.",
            supported_keys=list(FIELDS),
        )


def _retired_fix(key: str, payload: Mapping[str, Any]) -> str:
    """A retired key's hint: the caller's own value in the call's spelling, when the rewrite
    is mechanical."""
    value = payload[key]
    if key == "metric_filters" and isinstance(value, list) and value:
        return _result_filters_fix(value, payload)
    if (
        key == "route_decisions"
        and isinstance(value, list)
        and value
        and all(
            isinstance(row, Mapping)
            and set(row) <= {"source_entity", "target_entity", "relationship_path", "label"}
            and isinstance(row.get("relationship_path"), list)
            for row in value
        )
    ):
        return f'Write "routes": {json.dumps([route_entry(row) for row in value], default=str)}.'
    if key == "limits" and isinstance(value, Mapping) and set(value) == {"max_rows"}:
        return f"Pass `max_rows: {json.dumps(value['max_rows'], default=str)}` as a tool argument."
    return RETIRED[key]


def _result_filters_fix(items: list[Any], payload: Mapping[str, Any]) -> str:
    """``metric_filters`` as the caller's own ``having`` rows and entity filters; an item with
    no mechanical rewrite keeps the general hint."""
    aliases = _select_aliases(payload.get("select"))
    time = payload.get("time")
    grain = time.get("grain") if isinstance(time, Mapping) else None
    rows: dict[str, list[dict[str, Any]]] = {"having": [], "where": []}
    rest: list[str] = []
    for index, item in enumerate(items):
        try:
            part, row = _result_filter(item, aliases, grain)
        except SemanticLayerError:
            rest.append(f"metric_filters[{index}]")
            continue
        rows[part].append(row)
    fixes = []
    if rows["having"]:
        fixes.append(f'Write "having": {json.dumps(rows["having"], default=str)}.')
    if rows["where"]:
        fixes.append(f'Add to "where": {json.dumps(rows["where"], default=str)}.')
    if rest:
        fixes.append(f"For {', '.join(rest)}: {RETIRED['metric_filters']}")
    return " ".join(fixes)


# A predicate's engine scope, back to its over word (the inverse of ``_PREDICATE_SCOPES``).
_OVER_OF_SCOPE: dict[tuple[str, str], str] = {
    (scope["scope_mode"], scope.get("time_alignment", "")): word
    for word, scope in _PREDICATE_SCOPES.items()
}
_PREDICATE_FILTER_KEYS = frozenset(
    {"kind", "entity", "input", "op", "value", "scope_mode", "time_alignment"}
)


def _result_filter(item: Any, aliases: Mapping[str, Any], grain: Any) -> tuple[str, dict[str, Any]]:
    """One ``metric_filters`` item as ``("having" | "where", row)``, read back by this parser;
    refuses an item with no mechanical rewrite."""
    unknown = refusal("metric_filters", "has no mechanical rewrite")
    if not isinstance(item, Mapping):
        raise unknown
    expression = item.get("expression")
    if isinstance(expression, Mapping) and expression.get("kind") == "metric_predicate":
        # An entity filter keeps the entities whose predicate is true, over an over word.
        scope = (str(expression.get("scope_mode", "")), str(expression.get("time_alignment", "")))
        outer = operators.normalize_condition(
            item.get("op", "is"), item.get("value"), path="metric_filters", internal=True
        )
        if (
            outer != ("is", True)
            or scope not in _OVER_OF_SCOPE
            or not set(expression) <= _PREDICATE_FILTER_KEYS
        ):
            raise unknown
        row = _call_entity_having(expression)
        _entity_filter(row, "where", grain)
        return "where", row
    having = call_having(item, aliases)
    if having is None:
        raise unknown
    _having([having], aliases)
    return "having", having


def _select_aliases(raw: Any) -> dict[str, Any]:
    """Each select item's output name and its expression in the call's spelling (the caller's
    select may still be in an earlier spelling)."""
    aliases: dict[str, Any] = {}
    for item in raw if isinstance(raw, list) else []:
        alias: Any = None
        source = item
        if isinstance(item, Mapping):
            alias = item.get("as")
            source = item.get("expression", {k: v for k, v in item.items() if k != "as"})
        expression = _call_expression(source)
        if alias is None and isinstance(expression, str):
            alias = expression.split(".")[-1]
        if isinstance(alias, str):
            aliases.setdefault(alias, expression)
    return aliases


def _group_by(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise refusal("group_by", "is a list of dimension ids", fix='Write ["dimension.<id>"].')
    out: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise refusal(
                f"group_by[{index}]", "is a dimension id string", fix='Write "dimension.<id>".'
            )
        out.append(item.strip())
    return out


def _reference(raw: str, path: str) -> dict[str, Any]:
    ref = raw.strip()
    if ref.startswith("measure."):
        return {"kind": "measure", "measure": ref}
    if ref.startswith("metric."):
        return {"kind": "metric", "metric": ref}
    raise refusal(
        path,
        f"'{ref}' is not a measure or metric id",
        fix="Name a measure ('measure.<id>') or a metric ('metric.<id>').",
    )


def _select(raw: Any, group_by: list[str], context: CallContext) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise refusal("select", "is a list", fix='Write ["metric.<id>", ...].')
    out: list[dict[str, Any]] = []
    sources: list[tuple[str, Any]] = []
    for index, item in enumerate(raw):
        path = f"select[{index}]"
        if isinstance(item, str) and item.strip().startswith("dimension."):
            dimension = item.strip()
            if group_by and dimension not in group_by:
                raise refusal(
                    path,
                    "a dimension splits the answer; it belongs in group_by",
                    fix=f"Add '{dimension}' to group_by and remove it from select.",
                )
            if dimension not in group_by:
                group_by.append(dimension)
            context.notes.append(
                {"path": path, "received": dimension, "canonical": {"group_by": [dimension]}}
            )
            continue
        if isinstance(item, str):
            expression = _reference(item, path)
            out.append({"expression": expression, "as": item.strip().split(".")[-1]})
            sources.append((path, item))
            continue
        if not isinstance(item, Mapping):
            raise refusal(path, "is an id string or {expression, as}", fix='Write "metric.<id>".')
        named = [
            key for key in ("metric", "measure", "dimension") if isinstance(item.get(key), str)
        ]
        if named and "expression" not in item:
            ref = item[named[0]].strip()
            written = _written({key: value for key, value in item.items() if key != "as"})
            alias = item.get("as")
            if named[0] == "dimension":
                fix = f'Move "{ref}" to group_by.'
            elif written is None or written == ref and alias in (None, ref.split(".")[-1]):
                fix = f'Write "{ref}", or {{"expression": "{ref}", "as": ...}} to name it.'
            else:
                shape = {"expression": written, "as": alias or ref.split(".")[-1]}
                fix = f"Write {json.dumps(shape, default=str)}."
            raise refusal(path, "a reference is the id string itself", fix=fix)
        _check_keys(item, SELECT_KEYS, path)
        if "expression" not in item:
            raise refusal(path, "needs 'expression'", fix='Write {"expression": "metric.<id>"}.')
        expression = _expression(item["expression"], f"{path}.expression", context)
        alias = item.get("as")
        if alias is not None and (not isinstance(alias, str) or not alias.strip()):
            raise refusal(f"{path}.as", "is a non-empty string", fix="Name the output column.")
        if alias is None and isinstance(item["expression"], str):
            alias = str(item["expression"]).strip().split(".")[-1]
        # A kind expression without ``as`` gets the engine's generated name.
        row: dict[str, Any] = {"expression": expression}
        if alias:
            row["as"] = alias.strip()
        out.append(row)
        sources.append((path, item))
    for index, row in enumerate(out):
        row.setdefault("as", f"expr_{index + 1}")
    _refuse_duplicate_columns(out, sources, group_by, context.timed)
    return out


def _refuse_duplicate_columns(
    select: list[dict[str, Any]], sources: list[tuple[str, Any]], group_by: list[str], timed: bool
) -> None:
    """Every answer column is named once: a select item that repeats a name gets its own ``as``."""
    taken = {TIME_AXIS: "time", f"{TIME_AXIS}_partial": "time"} if timed else {}
    for index, dimension in enumerate(group_by):
        if dimension in taken:
            raise refusal(
                f"group_by[{index}]",
                f"repeats '{dimension}'",
                fix=f"Remove group_by[{index}].",
                code="DUPLICATE_OUTPUT_ALIAS",
                alias=dimension,
            )
        taken[dimension] = f"group_by[{index}]"
    for row, (path, item) in zip(select, sources, strict=True):
        alias = row["as"]
        if alias in taken:
            expression = item if isinstance(item, str) else item.get("expression")
            unique = next(
                f"{alias}_{n}" for n in range(2, len(taken) + 3) if f"{alias}_{n}" not in taken
            )
            raise refusal(
                path,
                f"its column '{alias}' is already named by {taken[alias]}",
                fix=f'Name it with "as": {json.dumps({"expression": expression, "as": unique})}.',
                code="DUPLICATE_OUTPUT_ALIAS",
                alias=alias,
            )
        taken[alias] = path


_KINDLESS_FIX = 'A reference is the id string itself, e.g. "metric.<id>" or "measure.<id>".'


def _operand(raw: Any, path: str, context: CallContext) -> Any:
    """An operand inside a kind: a reference or kind is read; a literal passes to the parser."""
    if isinstance(raw, Mapping) or (isinstance(raw, str) and raw.startswith(REFERENCE_PREFIXES)):
        return _expression(raw, path, context)
    return raw


def _expression(raw: Any, path: str, context: CallContext) -> Any:
    """An inline expression with its references expanded to the engine's ref objects."""
    if isinstance(raw, str):
        return _reference(raw, path)
    if not isinstance(raw, Mapping):
        raise refusal(path, "is an id string or an object with 'kind'", fix=_KINDLESS_FIX)
    kind = raw.get("kind")
    if not kind and isinstance(raw.get("dimension"), str):
        raise refusal(
            path,
            "a dimension splits the answer; it is not an expression",
            fix=f'Move "{raw["dimension"].strip()}" to group_by.',
        )
    written = _written(raw) if not kind or kind in RETIRED_KINDS else None
    fix = f"Write {json.dumps(written, default=str)}." if written is not None else ""
    if not kind:
        raise refusal(path, "an expression object names its 'kind'", fix=fix or _KINDLESS_FIX)
    kind = str(kind).strip()
    if kind in RETIRED_KINDS:
        name = raw.get("metric") or raw.get("measure") or "<id>"
        raise refusal(path, f"'{kind}' objects are retired", fix=fix or f'Write "{name}".')
    kinds = call_kinds()
    if kind not in kinds:
        matches = _closest(kind, kinds)
        raise refusal(
            f"{path}.kind",
            f"unknown kind '{kind}'",
            fix=f"Use '{matches[0]}'." if matches else f"Use one of {sorted(kinds)}.",
            supported_kinds=sorted(kinds),
        )
    if kind == "prior_period" and ("measure" in raw or not isinstance(raw.get("offset"), Mapping)):
        shape = json.dumps(_prior_period(raw), default=str)
        raise refusal(path, "prior_period has one spelling", fix=f"Write {shape}.")
    if not (kind == "prior_period" and "measure" in raw):
        _check_keys(raw, tuple(dict.fromkeys(("kind", *kinds[kind]))), path)
    if kind == "share":
        _check_share(raw, path, context)
    node: dict[str, Any] = {}
    for key, value in raw.items():
        child = f"{path}.{key}"
        if (
            key in EXPRESSION_KEYS
            or (key == "over" and kind == "distribution")
            or key == "value"
            and kind in {"aggregate_if", "nullif", "date_add"}
        ):
            node[key] = _operand(value, child, context)
        elif key in EXPRESSION_LISTS and isinstance(value, list):
            node[key] = [
                _operand(item, f"{child}[{index}]", context) for index, item in enumerate(value)
            ]
        elif key == "whens" and isinstance(value, list):
            node[key] = [
                {
                    name: _operand(part, f"{child}[{index}].{name}", context)
                    for name, part in dict(when).items()
                }
                if isinstance(when, Mapping)
                else when
                for index, when in enumerate(value)
            ]
        elif key == "else":
            node[key] = _operand(value, child, context)
        else:
            node[key] = value
    if node.get("aggregation") and "measure" in node:
        _check_aggregation(node, path, context)
    if kind in {"metric_predicate"}:
        node = _predicate(node, path)
    if kind == "scoped_aggregate":
        node["predicates"] = [
            _scoped_predicate(item, f"{path}.predicates[{index}]", context)
            for index, item in enumerate(list(node.get("predicates") or []))
        ]
        node["where"] = [
            _inline_condition(item, f"{path}.where[{index}]", context)
            for index, item in enumerate(list(node.get("where") or []))
        ]
        over = node.pop("over", None)
        if over is not None:
            raise refusal(
                f"{path}.over",
                "is set on each predicate of a scoped_aggregate",
                fix='Move "over" into each item of "predicates".',
            )
    if kind in {"aggregate", "semi_additive"} and isinstance(node.get("filter"), Mapping):
        node["filter"] = {
            key: [
                _inline_condition(item, f"{path}.filter.{key}[{index}]", context)
                for index, item in enumerate(list(value or []))
            ]
            if isinstance(value, list)
            else value
            for key, value in dict(node["filter"]).items()
        }
    return node


# The keys of a reference object: ``{"metric": id}``, or ``{"measure": id}`` with overrides.
_METRIC_REFERENCE = frozenset({"kind", "metric"})
_MEASURE_REFERENCE = frozenset({"kind", "measure", "aggregation", "temporal_role", "parameters"})


def _written(raw: Mapping[str, Any]) -> Any:
    """A reference object as the call writes it: its id, or a ``measure`` kind with its
    overrides; None for any other shape."""
    if not set(raw) <= (_METRIC_REFERENCE if "metric" in raw else _MEASURE_REFERENCE):
        return None
    form = _call_expression(raw)
    if isinstance(form, str) or (isinstance(form, Mapping) and form.get("kind") == "measure"):
        return form
    return None


def _prior_period(raw: Mapping[str, Any]) -> dict[str, Any]:
    """An earlier prior_period spelling, ``{measure, offset: <periods>, grain}``, as the call
    writes it (the earlier spelling read the offset's size, whatever its sign)."""
    measure = {key: raw[key] for key in ("measure", "aggregation") if key in raw}
    source = (
        _written({"kind": "measure", **measure}) if isinstance(raw.get("measure"), str) else None
    )
    offset = raw.get("offset")
    if not isinstance(offset, Mapping):
        size = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
        offset = {"unit": raw.get("grain", "<unit>"), "value": abs(size) or "<periods back>"}
    return {
        "kind": "prior_period",
        "input": source or _call_expression(raw.get("input", "<measure id>")),
        "offset": offset,
    }


def _check_share(raw: Mapping[str, Any], path: str, context: CallContext) -> None:
    """A share reads a measure or metric id, and takes its total over group_by ids or period."""
    columns = [*context.group_by, *([TIME_AXIS] if context.timed else [])]
    if not isinstance(raw.get("input"), str) or not raw["input"].startswith(REFERENCE_PREFIXES):
        raise refusal(
            f"{path}.input",
            "a share's input is a measure or metric id",
            fix='Write "input": "measure.<id>" or "metric.<id>".',
        )
    over = raw.get("over")
    if not isinstance(over, list) or not over:
        raise refusal(
            f"{path}.over",
            "lists the columns the total is taken over",
            fix=f'Write "over": {json.dumps(columns[:1] or [TIME_AXIS])}.',
        )
    for index, column in enumerate(over):
        if column not in columns or column in over[:index]:
            raise refusal(
                f"{path}.over[{index}]",
                f"'{column}' is not a group_by id or the period of this call, once each",
                fix=(
                    f"Use one of {columns}."
                    if columns
                    else "Add the dimension to group_by, or set time.grain to share over period."
                ),
                over_allowed=columns,
            )


_TIME_ORDERED = frozenset({"first_value", "last_value"})


def _check_aggregation(node: dict[str, Any], path: str, context: CallContext) -> None:
    """An ``aggregation`` override names one of the measure's allowed aggregations."""
    measures = context.config.measures if context.config is not None else []
    measure = next((row for row in measures if row.id == node["measure"]), None)
    aggregation = str(node["aggregation"]).strip()
    # A time-ordered statistic is allowed by a clock: the engine's clock rule says so.
    if measure is None or aggregation in {*measure.allowed_aggregations, *_TIME_ORDERED}:
        return
    allowed = list(measure.allowed_aggregations)
    raise refusal(
        f"{path}.aggregation",
        f"'{aggregation}' is not an aggregation of '{measure.id}'; it allows {allowed}",
        fix=(
            f"Use one of {allowed}, or omit aggregation for the default "
            f"({measure.default_aggregation})."
        ),
        code="UNSUPPORTED_AGGREGATION",
        measure=measure.id,
        aggregation=aggregation,
        allowed=allowed,
        default_aggregation=measure.default_aggregation,
    )


def _inline_condition(item: Any, path: str, context: CallContext) -> Any:
    """A condition inside an inline kind: the same words as ``where``."""
    if isinstance(item, Mapping) and "expression" in item:
        return {
            **item,
            "expression": _expression(item["expression"], f"{path}.expression", context),
        }
    if not isinstance(item, Mapping) or "field" not in item and "dimension" not in item:
        return item
    word, value = operators.normalize_condition(item.get("op", "is"), item.get("value"), path=path)
    return {**item, "op": word, "value": value}


def _scoped_predicate(item: Any, path: str, context: CallContext) -> Any:
    """A scoped aggregate's predicate: its input is read like any operand, then its ``over``."""
    if not isinstance(item, Mapping):
        return item
    node = dict(item)
    if "input" in node:
        node["input"] = _operand(node["input"], f"{path}.input", context)
    return _predicate(node, path)


def _predicate(node: dict[str, Any], path: str) -> dict[str, Any]:
    """A predicate's ``over`` word and comparison, in the engine's scope terms."""
    retired = [key for key in _PREDICATE_RETIRED if key in node]
    if retired:
        scope = (str(node.get("scope_mode", "")), str(node.get("time_alignment", "")))
        word = None if "time_grain" in node else _OVER_OF_SCOPE.get(scope)
        raise refusal(
            f"{path}.{retired[0]}",
            "is retired; say how the aggregate is taken with 'over'",
            fix=(
                f'Replace {" and ".join(retired)} with "over": "{word}".'
                if word
                else f'Write "over": one of {list(OVER)}.'
            ),
        )
    over = node.pop("over", None)
    if over is None:
        raise _over_unstated(path, node)
    if over not in OVER:
        raise refusal(
            f"{path}.over", f"must be one of {list(OVER)}", fix=f"Use one of {list(OVER)}."
        )
    if "input" in node and isinstance(node["input"], str):
        node["input"] = _reference(node["input"], f"{path}.input")
    for key in ("metric", "measure"):
        if isinstance(node.get(key), str) and "input" not in node:
            node["input"] = _reference(node.pop(key), f"{path}.{key}")
    op = node.get("op")
    if op is not None:
        word = operators.operator_word(op, path=f"{path}.op")
        if word not in _AGGREGATE_WORDS:
            raise refusal(
                f"{path}.op",
                "compares an aggregate: use is, not, gt, gte, lt or lte",
                fix="Use gt, gte, lt, lte, is or not.",
            )
        node["op"] = word
    return {**node, **_PREDICATE_SCOPES[over]}


def _over_unstated(path: str, node: Mapping[str, Any]) -> SemanticLayerError:
    return refusal(
        f"{path}.over",
        "say over which rows each entity's aggregate is taken",
        code="ENTITY_FILTER_PERIOD_UNSTATED",
        fix=(
            "Add over: all_time (every row of the entity), window (its rows inside the "
            "query window) or period (per output period; needs time.grain)."
        ),
        options=[{"over": word} for word in OVER],
    )


def _condition(item: Any, path: str) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise refusal(
            path,
            "is {field, op, value}",
            fix='Write {"field": "dimension.<id>", "op": "is", "value": "<value>"}.',
        )
    _check_keys(item, CONDITION_KEYS, path)
    field_id = item.get("field")
    if not isinstance(field_id, str) or not field_id.strip():
        raise refusal(f"{path}.field", "is a dimension id", fix='Write "dimension.<id>".')
    if "op" not in item:
        raise refusal(f"{path}.op", "is required", fix=f"Use one of {list(operators.WORDS)}.")
    word, value = operators.normalize_condition(item["op"], item.get("value"), path=path)
    return {"field": field_id.strip(), "op": word, "value": value}


def _where(
    raw: Any, context: CallContext, payload: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        raise refusal("where", "is a list", fix='Write [{"field": ..., "op": ..., "value": ...}].')
    rows: list[dict[str, Any]] = []
    entity_having: list[dict[str, Any]] = []
    grain = (
        (payload.get("time") or {}).get("grain")
        if isinstance(payload.get("time"), Mapping)
        else None
    )
    for index, item in enumerate(raw):
        path = f"where[{index}]"
        if isinstance(item, Mapping) and (
            _CHILD_GROUP_KEYS & set(item) or {"entity", "where"} <= set(item)
        ):
            raise _child_group_refusal(item, path)
        if isinstance(item, Mapping) and "entity" in item:
            group, having = _entity_filter(item, path, grain)
            rows.extend(group)
            entity_having.extend(having)
            continue
        if isinstance(item, Mapping) and str(item.get("field", "")).startswith(REFERENCE_PREFIXES):
            measure = str(item["field"])
            raise refusal(
                f"{path}.field",
                "a measure or metric is not a row filter; filter entities by it instead",
                fix=(
                    'Write {"entity": "<entity id>", "having": [{"field": "'
                    + measure
                    + '", "op": ..., "value": ...}], "over": "all_time" | "window" | "period"}, '
                    'or filter results with "having".'
                ),
            )
        rows.append(_condition(item, path))
    return rows, entity_having


# Keys of the retired child group, ``{child, match, where}`` (or ``child_group``); an entity
# with a ``where`` list is read as one too.
_CHILD_GROUP_KEYS = frozenset({"child", "child_group", "match"})


def _child_group_refusal(item: Mapping[str, Any], path: str) -> SemanticLayerError:
    """A child group is an entity filter now: the same conditions under ``any`` or ``none``."""
    entity = item.get("child") or item.get("entity")
    word = item.get("match") if item.get("match") in {"any", "none"} else "any"
    conditions = item.get("where")
    shape = {
        "entity": entity if isinstance(entity, str) else "entity.<id>",
        word: conditions
        if isinstance(conditions, list) and conditions
        else [{"field": "dimension.<id>", "op": "is", "value": "<value>"}],
    }
    return refusal(
        path,
        "a child group is an entity filter: the entity, then any or none of its rows",
        fix=f"Write {json.dumps(shape)}.",
    )


def _entity_filter(
    item: Mapping[str, Any], path: str, grain: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _check_keys(item, ENTITY_FILTER_KEYS, path)
    entity = item.get("entity")
    if not isinstance(entity, str) or not entity.strip():
        raise refusal(f"{path}.entity", "is an entity id", fix='Write "entity.<id>".')
    parts = [key for key in ("any", "none", "having") if key in item]
    if len(parts) != 1:
        raise refusal(
            path,
            "an entity filter has exactly one of any, none or having",
            fix="Use any (some row matches), none (no row matches) or having (its aggregates).",
        )
    part = parts[0]
    conditions = item.get(part)
    if not isinstance(conditions, list) or not conditions:
        raise refusal(f"{path}.{part}", "is a non-empty list of conditions", fix="Add a condition.")
    if part in {"any", "none"}:
        if "over" in item:
            raise refusal(
                f"{path}.over",
                "belongs beside having (an entity's aggregates)",
                fix="Remove 'over'.",
            )
        where = []
        for index, condition in enumerate(conditions):
            at = f"{path}.{part}[{index}]"
            if isinstance(condition, Mapping) and "entity" in condition:
                raise refusal(at, "entity filters do not nest", fix="Make it its own where item.")
            where.append(_condition(condition, at))
        return [{"child": entity.strip(), "match": part, "where": where}], []
    over = item.get("over")
    if over is None:
        raise _over_unstated(f"{path}", item)
    if over not in OVER:
        raise refusal(
            f"{path}.over", f"must be one of {list(OVER)}", fix=f"Use one of {list(OVER)}."
        )
    if over == "period" and not grain:
        raise refusal(
            f"{path}.over",
            "'period' takes the aggregate per output period, so the call needs time.grain",
            fix='Add "grain" to "time", or use over "window" or "all_time".',
        )
    predicates = []
    for index, condition in enumerate(conditions):
        at = f"{path}.having[{index}]"
        if not isinstance(condition, Mapping):
            raise refusal(at, "is {field, op, value}", fix="Name a measure or metric in field.")
        _check_keys(condition, CONDITION_KEYS, at)
        field_id = str(condition.get("field", "")).strip()
        if not field_id.startswith(REFERENCE_PREFIXES):
            raise refusal(
                f"{at}.field",
                "is a measure or metric of the entity",
                fix="Filter the entity's rows with any / none instead.",
            )
        if condition.get("value") is None:
            # An entity with no rows reads 0, so a null test would keep the wrong entities.
            raise refusal(
                f"{at}.value",
                "compares the entity's aggregate, which is never tested for null",
                fix='For an entity with none, write {"op": "is", "value": 0}.',
            )
        word, value = operators.normalize_condition(
            condition.get("op"), condition.get("value"), path=at
        )
        if word not in _AGGREGATE_WORDS:
            raise refusal(
                f"{at}.op", "compares an aggregate", fix="Use gt, gte, lt, lte, is or not."
            )
        predicates.append(
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": entity.strip(),
                    "input": _reference(field_id, f"{at}.field"),
                    "op": word,
                    "value": value,
                    **_PREDICATE_SCOPES[over],
                },
                "op": "is",
                "value": True,
            }
        )
    return [], predicates


def _having(raw: Any, aliases: Mapping[str, Any]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise refusal(
            "having", "is a list", fix='Write [{"field": "<alias>", "op": "gt", "value": 0}].'
        )
    out = []
    for index, item in enumerate(raw):
        path = f"having[{index}]"
        if not isinstance(item, Mapping):
            raise refusal(path, "is {field, op, value}", fix="Name a select alias in field.")
        _check_keys(item, CONDITION_KEYS, path)
        field_id = str(item.get("field", "")).strip()
        if field_id in aliases:
            expression = aliases[field_id]
        elif field_id.startswith(REFERENCE_PREFIXES):
            expression = _reference(field_id, f"{path}.field")
        else:
            raise refusal(
                f"{path}.field",
                f"'{field_id}' is not a select alias, measure or metric",
                fix=f"Use one of {sorted(aliases)}." if aliases else "Name a measure or metric.",
            )
        word, value = operators.normalize_condition(item.get("op"), item.get("value"), path=path)
        out.append({"expression": expression, "op": word, "value": value})
    return out


def _time(raw: Any, select: list[dict[str, Any]], context: CallContext) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise refusal("time", "is an object", fix='Write {"last": {"unit": "week", "value": 1}}.')
    for key in raw:
        if key == "range":
            bounds = raw["range"] if isinstance(raw["range"], Mapping) else {}
            moved = {k: v for k, v in raw.items() if k not in {"range", "calendar_id"}}
            raise refusal(
                "time.range",
                "is retired",
                fix=(
                    f'Write "time": {json.dumps({**moved, **bounds}, default=str)}.'
                    if bounds and set(bounds) <= {"last", "start", "end"}
                    else 'Write "last": {"unit": ..., "value": ...} directly under time.'
                ),
            )
        if key == "calendar_id":
            raise refusal(
                "time.calendar_id",
                "is retired; the clock's own calendar applies",
                fix="Remove 'calendar_id'.",
            )
    _check_keys(raw, TIME_KEYS, "time")
    windows = [key for key in ("last", "this") if key in raw]
    if len(windows) > 1 or (windows and ("start" in raw or "end" in raw)):
        raise refusal(
            "time",
            "one window spelling: last, this, or start/end",
            fix="Keep one of last, this or start/end.",
        )
    grain = raw.get("grain")
    if grain is not None and grain not in GRAINS:
        raise refusal(
            "time.grain",
            f"must be one of {list(GRAINS)}",
            fix="Omit grain for one total, or use day, week, month, quarter or year.",
        )
    if "last" in raw:
        last = raw["last"]
        if not isinstance(last, Mapping):
            raise refusal("time.last", "is {unit, value}", fix='Write {"unit": "day", "value": 7}.')
        _check_keys(last, LAST_KEYS, "time.last")
        if last.get("unit") not in UNITS:
            raise refusal(
                "time.last.unit",
                f"must be one of {list(UNITS)}",
                fix="Use day, week, month, quarter or year.",
            )
        value = last.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise refusal("time.last.value", "is a positive integer", fix="Use 1 or more.")
    if "this" in raw and raw["this"] not in UNITS:
        raise refusal(
            "time.this",
            f"must be one of {list(UNITS)}",
            fix="Use day, week, month, quarter or year.",
        )
    named = bool(windows) or ("start" in raw and "end" in raw)
    if "fill" in raw:
        if not isinstance(raw["fill"], bool):
            raise refusal("time.fill", "is true or false", fix="Use true or false.")
        if raw["fill"] and (named or not grain):
            raise refusal(
                "time.fill",
                "applies to an open window with a grain; a named window already returns every period",
                fix="Remove 'fill'.",
            )
    if not (grain or windows or "start" in raw or "end" in raw) and not context.partial:
        raise refusal(
            "time",
            "say a grain or a window",
            fix='Add "grain", or a window: "last", "this", or "start"/"end".',
        )
    role = raw.get("temporal_role")
    if role is not None and (not isinstance(role, str) or not role.strip()):
        raise refusal(
            "time.temporal_role", "is a temporal role id", fix='Write "temporal_role.<id>".'
        )
    role = (role or "").strip() or _default_role(select, context)
    # A package with no clocks at all is refused by the engine ("declares no time").
    if (
        not role
        and context.config is not None
        and context.config.temporal_roles
        and not context.partial
    ):
        raise refusal(
            "time.temporal_role",
            "nothing selected names a clock, so say which one",
            fix='Add "temporal_role": "temporal_role.<id>" to time, one of the package\'s clocks.',
        )
    timezone = _role_timezone(role, context.config)
    try:
        window = resolve_window(dict(raw), now=context.now, timezone=timezone)
    except (ValueError, OverflowError) as exc:
        raise _off_calendar(raw, context.now, timezone) from exc
    time: dict[str, Any] = {"temporal_role": role}
    if grain:
        time["grain"] = grain
    if window.start is not None:
        time["start"] = window.start
    if window.end is not None:
        time["end"] = window.end
    if window.end is not None and "end" not in window.spec and not window.named:
        time["to_now"] = True  # only a start: the window runs to now and stays open
    if raw.get("fill"):
        time["fill"] = True
    if "last" in raw:
        time["last_unit"] = raw["last"]["unit"]
        time["last_value"] = int(raw["last"]["value"])
    if "this" in raw:
        time["this"] = raw["this"]
    return time


def _off_calendar(raw: Mapping[str, Any], now: datetime, timezone: str) -> SemanticLayerError:
    """The refusal of a window the calendar (0001-01-01 to 9999-12-31) can't hold."""
    if "last" not in raw:
        return refusal(
            "now",
            "is too near the calendar's first or last day for the clock's time zone",
            fix="Send a now between 0001-01-02 and 9999-12-30.",
        )
    most = largest_last(raw["last"]["unit"], now, timezone)
    return refusal(
        "time.last.value",
        "reaches back before the calendar's first day, 0001-01-01",
        fix=(f"Use at most {most}, or " if most > 0 else "Use ")
        + "a dated window with start and end.",
    )


def _default_role(select: list[dict[str, Any]], context: CallContext) -> str:
    """The selected measures' shared default clock; several clocks hold for the caller's pick."""
    if context.default_clock is None:
        return ""
    clocks: dict[str, list[str]] = {}
    for row in select:
        for ref in _references(row["expression"]):
            role = context.default_clock(ref)
            if role:
                clocks.setdefault(role, []).append(ref)
    if len(clocks) > 1:
        raise refusal(
            "time.temporal_role",
            "the selected measures read different clocks; say which one",
            code="MIXED_TIME_ROLES",
            fix="Add temporal_role to time; each option names one clock.",
            options=[
                {"temporal_role": role, "reads": refs} for role, refs in sorted(clocks.items())
            ],
        )
    return next(iter(clocks), "")


def _references(expression: Any) -> list[str]:
    """The measure and metric ids an expanded expression reads, in order."""
    if isinstance(expression, Mapping):
        if expression.get("kind") == "measure" and isinstance(expression.get("measure"), str):
            return [expression["measure"]]
        if expression.get("kind") == "metric" and isinstance(expression.get("metric"), str):
            return [expression["metric"]]
        if isinstance(expression.get("measure"), str):
            return [expression["measure"]]
        out: list[str] = []
        for value in expression.values():
            out.extend(_references(value))
        return out
    if isinstance(expression, list):
        return [ref for item in expression for ref in _references(item)]
    return []


def _role_timezone(role: str, config: PackageConfig | None) -> str:
    if config is None or not role:
        return "UTC"
    row = next((item for item in config.temporal_roles if item.id == role), None)
    return str(getattr(row, "timezone", "") or "UTC")


def _order_by(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise refusal(
            "order_by", "is a list", fix='Write [{"field": "<alias>", "direction": "desc"}].'
        )
    out = []
    for index, item in enumerate(raw):
        path = f"order_by[{index}]"
        if not isinstance(item, Mapping):
            raise refusal(
                path,
                "is {field, direction}",
                fix='Write {"field": "<alias>", "direction": "desc"}.',
            )
        _check_keys(item, ORDER_KEYS, path)
        field_id = item.get("field")
        # The axis's earlier names: "time", a clock id, or "<clock id>__<grain>".
        if field_id == "time" or str(field_id).startswith("temporal_role."):
            raise refusal(f"{path}.field", "the time axis is named 'period'", fix='Write "period".')
        if not isinstance(field_id, str) or not field_id.strip():
            raise refusal(
                f"{path}.field",
                "names a select alias, a group_by id or period",
                fix="Name a column.",
            )
        direction = item.get("direction", "asc")
        if direction not in DIRECTIONS:
            raise refusal(
                f"{path}.direction",
                "is 'asc' or 'desc'",
                fix=f'Write "{str(direction).lower()}".'
                if str(direction).lower() in DIRECTIONS
                else "Use asc or desc.",
            )
        out.append({"field": field_id.strip(), "direction": direction.upper()})
    return out


def _limit(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise refusal(
            "limit",
            "is a positive integer (the question's top-N)",
            fix="Use 1 or more, or omit limit.",
        )
    return raw


def _routes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise refusal(
            "routes",
            "is a list of {from, to, via}",
            fix="Send the routes an AMBIGUOUS_PATH option names.",
        )
    out = []
    for index, item in enumerate(raw):
        path = f"routes[{index}]"
        if not isinstance(item, Mapping):
            raise refusal(
                path, "is {from, to, via}", fix="Copy a route from the AMBIGUOUS_PATH options."
            )
        _check_keys(item, ROUTE_KEYS, path)
        via = item.get("via")
        if not (
            isinstance(item.get("from"), str)
            and isinstance(item.get("to"), str)
            and isinstance(via, list)
            and via
            and all(isinstance(hop, str) and hop.strip() for hop in via)
        ):
            raise refusal(
                path,
                "names from and to entity ids and a non-empty via list of relationship ids",
                fix="Copy a route from the AMBIGUOUS_PATH options.",
            )
        out.append(
            {
                "source_entity": item["from"].strip(),
                "target_entity": item["to"].strip(),
                "relationship_path": [hop.strip() for hop in via],
            }
        )
    return out


def route_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """A route decision row as the call's ``routes`` entry: ``{from, to, via}``."""
    return {
        "from": row["source_entity"],
        "to": row["target_entity"],
        "via": list(row["relationship_path"]),
    }


def route_options(call: Any, details: dict[str, Any]) -> None:
    """Make each option of an ``AMBIGUOUS_PATH`` refusal a complete call: ``call`` with the
    option's route added to ``routes``, ready to send."""
    options = (details.get("clarification") or {}).get("options") or []
    if not isinstance(call, Mapping) or not options:
        return
    for option in options:
        route = route_entry(option["decision"])
        option["query"] = {**call, "routes": [*list(call.get("routes") or []), route]}
    details["hint"] = (
        "Which route is meant is a business definition. Ask which option the question means, "
        "then send that option's query (this query only), or record the option's decision as "
        "the package default with record_route_decision (a graph.path_preferences row)."
    )


# --- The call an engine query states, for echoes and the planner. ---------------------------


def call_from_query(query: Mapping[str, Any]) -> dict[str, Any]:
    """The call that states an engine query (its own form, as ``NormalizedQuery.to_dict``)."""
    call: dict[str, Any] = {}
    aliases: dict[str, Any] = {}
    select: list[Any] = []
    for row in list(query.get("select") or []):
        expression = _call_expression(row.get("expression") or {})
        alias = str(row.get("as") or "")
        aliases[alias] = expression
        if isinstance(expression, str) and alias == expression.split(".")[-1]:
            select.append(expression)
        else:
            select.append({"expression": expression, "as": alias})
    if select:
        call["select"] = select
    if query.get("group_by"):
        call["group_by"] = list(query["group_by"])
    where = call_where(query.get("where") or [], query.get("metric_filters") or [])
    having: list[Any] = []
    for item in list(query.get("metric_filters") or []):
        if (item.get("expression") or {}).get("kind") == "metric_predicate":
            continue
        row = call_having(item, aliases)
        if row is None:
            raise refusal(
                "having",
                "a result filter names a select alias or a measure or metric",
                fix="Select the expression with an alias and filter on the alias.",
            )
        having.append(row)
    if where:
        call["where"] = where
    if having:
        call["having"] = having
    time = dict(query.get("time") or {})
    if time:
        call["time"] = _call_time(time)
    order = []
    for item in list(query.get("order_by") or []):
        field_id = str(item.get("field") or "")
        if time and field_id in {"time", time.get("temporal_role", ""), _legacy_axis(time)}:
            field_id = TIME_AXIS
        order.append({"field": field_id, "direction": str(item.get("direction", "asc")).lower()})
    if order:
        call["order_by"] = order
    if query.get("limit") is not None:
        call["limit"] = query["limit"]
    if query.get("route_decisions"):
        call["routes"] = [route_entry(row) for row in query["route_decisions"]]
    if query.get("observation_scope"):
        call["observation_scope"] = query["observation_scope"]
    return call


def call_where(where: Any, metric_filters: Any = ()) -> list[dict[str, Any]]:
    """A call's ``where``: the engine's row filters and child groups, then its entity filters."""
    out: list[dict[str, Any]] = []
    for item in list(where or []):
        if "child" in item:
            conditions = [_call_condition(row) for row in item.get("where") or []]
            out.append({"entity": item["child"], item["match"]: conditions})
        else:
            out.append(_call_condition(item))
    for item in list(metric_filters or []):
        expression = item.get("expression") or {}
        if expression.get("kind") == "metric_predicate":
            out.append(_call_entity_having(expression))
    return out


def call_having(item: Mapping[str, Any], aliases: Mapping[str, Any]) -> dict[str, Any] | None:
    """A result filter ``{expression, op, value}`` as a ``having`` row: the select alias whose
    call expression it filters, else its measure or metric id; None when it names neither."""
    expression = _call_expression(item.get("expression") or {})
    matched = next((name for name, expr in aliases.items() if expr == expression), None)
    field_id = matched or (expression if isinstance(expression, str) else None)
    if field_id is None:
        return None
    word, value = operators.normalize_condition(
        item.get("op", "is"), item.get("value"), path="having", internal=True
    )
    return {"field": field_id, "op": word, "value": value}


def _legacy_axis(time: Mapping[str, Any]) -> str:
    grain = time.get("grain")
    return f"{time.get('temporal_role', '')}__{grain}" if grain else ""


def _call_condition(item: Mapping[str, Any]) -> dict[str, Any]:
    word, value = operators.normalize_condition(
        item.get("op", "is"), item.get("value"), path="where", internal=True
    )
    return {"field": item.get("field") or item.get("dimension"), "op": word, "value": value}


def _over_word(scope: str, alignment: str) -> str:
    if scope == "entity_only":
        return "window" if alignment else "all_time"
    return "period"


def _call_entity_having(expression: Mapping[str, Any]) -> dict[str, Any]:
    op = operators.word_of(expression.get("op", "is"))
    condition = {
        "field": _call_expression(expression.get("input")),
        "op": op,
        "value": expression.get("value"),
    }
    return {
        "entity": expression.get("entity"),
        "having": [condition],
        "over": _over_word(
            str(expression.get("scope_mode", "")), str(expression.get("time_alignment", ""))
        ),
    }


def _call_time(time: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if time.get("temporal_role"):
        out["temporal_role"] = time["temporal_role"]
    if time.get("grain"):
        out["grain"] = time["grain"]
    if time.get("last_unit"):
        out["last"] = {"unit": time["last_unit"], "value": int(time.get("last_value") or 1)}
    elif time.get("this"):
        out["this"] = time["this"]
    elif isinstance(time.get("range"), Mapping) and time["range"].get("last"):
        out["last"] = dict(time["range"]["last"])
    else:
        for key in ("start", "end"):
            if time.get(key) is not None and not (key == "end" and time.get("to_now")):
                out[key] = time[key]
    if time.get("fill") and not (
        out.get("last") or out.get("this") or ("start" in out and "end" in out)
    ):
        out["fill"] = True
    return out


def _call_expression(expression: Any) -> Any:
    """An engine expression with references written as id strings."""
    if isinstance(expression, list):
        return [_call_expression(item) for item in expression]
    if not isinstance(expression, Mapping):
        return expression
    kind = expression.get("kind", "")
    if kind == "metric" or (not kind and set(expression) == {"metric"}):
        return expression["metric"]
    if kind in {"measure", "measure_ref", ""} and isinstance(expression.get("measure"), str):
        overrides = {
            key: expression[key]
            for key in ("aggregation", "temporal_role", "parameters")
            if expression.get(key)
        }
        if not overrides:
            return expression["measure"]
        return {"kind": "measure", "measure": expression["measure"], **overrides}
    node: dict[str, Any] = {}
    for key, value in expression.items():
        if (
            key in EXPRESSION_KEYS
            or key in EXPRESSION_LISTS
            or key in {"else", "whens"}
            or (key == "over" and kind == "distribution")
            or (key == "value" and kind in {"aggregate_if", "nullif", "date_add"})
        ):
            node[key] = _call_expression(value)
        else:
            node[key] = value
    if kind == "scoped_aggregate" and node.get("predicates"):
        node["predicates"] = [
            _call_expression({"kind": "metric_predicate", **predicate})
            if isinstance(predicate, Mapping) and "kind" not in predicate
            else _call_expression(predicate)
            for predicate in node["predicates"]
        ]
    if kind == "metric_predicate":
        scope = node.pop("scope_mode", "")
        alignment = node.pop("time_alignment", "")
        node.pop("time_grain", None)
        node["over"] = _over_word(str(scope), str(alignment))
        node["op"] = operators.word_of(node.get("op", "is"))
    return node
