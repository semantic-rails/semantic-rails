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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from difflib import get_close_matches
from typing import Any

from . import operators
from .clock import UNITS, parse_now, resolve_window
from .errors import SemanticLayerError
from .schema import OBSERVATION_SCOPES, PackageConfig

SCHEMA_ID = "https://semantic-rails.com/schemas/query.v2.json"

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
        "Identity comes from your credentials, never from the call; pass `now` as an "
        "argument beside the query."
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
_SQL_COMPARISON = {"is": "=", "not": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_SQL_MEMBERSHIP = {"is": "IN", "not": "NOT IN"}


def _predicate_op(word: str, value: Any) -> str:
    """A predicate's comparison in the engine's spelling; a list compares by membership."""
    if isinstance(value, list) and word in _SQL_MEMBERSHIP:
        return _SQL_MEMBERSHIP[word]
    return _SQL_COMPARISON[word]


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


def _check_keys(item: Mapping[str, Any], allowed: tuple[str, ...], path: str) -> None:
    unknown = sorted(key for key in item if key not in allowed)
    if unknown:
        key = unknown[0]
        matches = _closest(key, allowed)
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
    context = CallContext(
        config=config, now=parse_now(now), default_clock=default_clock, partial=partial
    )
    _check_top_level(payload)
    group_by = _group_by(payload.get("group_by"))
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
        if key in FIELDS or key in IGNORED or str(key).startswith("_"):
            continue
        if key in RETIRED:
            raise refusal(key, "is not part of a query call", fix=f"{RETIRED[key]} {V2_POINTER}")
        if key in TOOL_ARGUMENTS:
            raise refusal(
                key,
                "is an argument of the tool, not a key of the query",
                fix=f"Pass `{key}` beside `query`.",
            )
        matches = _closest(key, FIELDS)
        raise refusal(
            key,
            f"unknown key; a call has {list(FIELDS)}",
            fix=f"Use '{matches[0]}'." if matches else f"Remove '{key}'.",
            supported_keys=list(FIELDS),
        )


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
            continue
        if not isinstance(item, Mapping):
            raise refusal(path, "is an id string or {expression, as}", fix='Write "metric.<id>".')
        _check_keys(item, SELECT_KEYS, path)
        if "expression" not in item:
            raise refusal(path, "needs 'expression'", fix='Write {"expression": "metric.<id>"}.')
        expression = _expression(item["expression"], f"{path}.expression", context)
        alias = item.get("as")
        if alias is not None and (not isinstance(alias, str) or not alias.strip()):
            raise refusal(f"{path}.as", "is a non-empty string", fix="Name the output column.")
        if alias is None:
            alias = (
                str(item["expression"]).strip().split(".")[-1]
                if isinstance(item["expression"], str)
                else (
                    str(expression["measure"]).split(".")[-1]
                    if expression.get("kind") == "measure"
                    else ""
                )
            )
        row: dict[str, Any] = {"expression": expression}
        if alias:
            row["as"] = alias.strip()
        out.append(row)
    for index, row in enumerate(out):
        row.setdefault("as", f"expr_{index + 1}")
    return out


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
    if not kind:
        raise refusal(path, "an expression object names its 'kind'", fix=_KINDLESS_FIX)
    kind = str(kind).strip()
    if kind in RETIRED_KINDS:
        name = raw.get("metric") or raw.get("measure") or "<id>"
        raise refusal(path, f"'{kind}' objects are retired", fix=f'Write "{name}".')
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
        measure = raw.get("measure", "<measure id>")
        unit = raw.get("grain", "<unit>")
        raise refusal(
            path,
            "prior_period has one spelling",
            fix=(
                f'Write {{"kind": "prior_period", "input": "{measure}", '
                f'"offset": {{"unit": "{unit}", "value": 1}}}}.'
            ),
        )
    if not (kind == "prior_period" and "measure" in raw):
        _check_keys(raw, ("kind", *kinds[kind]), path)
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
    for key in _PREDICATE_RETIRED:
        if key in node:
            raise refusal(
                f"{path}.{key}",
                "is retired; say how the aggregate is taken with 'over'",
                fix=f'Write "over": one of {list(OVER)}.',
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
        if word not in _SQL_COMPARISON:
            raise refusal(
                f"{path}.op",
                "compares an aggregate: use is, not, gt, gte, lt or lte",
                fix="Use gt, gte, lt, lte, is or not.",
            )
        node["op"] = _predicate_op(word, node.get("value"))
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
        word, value = operators.normalize_condition(
            condition.get("op"), condition.get("value"), path=at
        )
        if word not in _SQL_COMPARISON:
            raise refusal(
                f"{at}.op", "compares an aggregate", fix="Use gt, gte, lt, lte, is or not."
            )
        predicates.append(
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": entity.strip(),
                    "input": _reference(field_id, f"{at}.field"),
                    "op": _predicate_op(word, value),
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
            raise refusal(
                "time.range",
                "is retired",
                fix='Write "last": {"unit": ..., "value": ...} directly under time.',
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
    timezone = _role_timezone(role, context.config)
    window = resolve_window(dict(raw), now=context.now, timezone=timezone)
    time: dict[str, Any] = {"temporal_role": role}
    if grain:
        time["grain"] = grain
    if window.start is not None:
        time["start"] = window.start
    if window.end is not None:
        time["end"] = window.end
    if raw.get("fill"):
        time["fill"] = True
    if "last" in raw:
        time["last_unit"] = raw["last"]["unit"]
        time["last_value"] = int(raw["last"]["value"])
    if "this" in raw:
        time["this"] = raw["this"]
    return time


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
        if field_id == "time":
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


# --- The call an engine query states, for echoes and the planner. ---------------------------

_SQL_WORDS = {
    "=": "is",
    "!=": "not",
    "<>": "not",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "IN": "is",
    "NOT IN": "not",
}


def call_from_query(query: Mapping[str, Any]) -> dict[str, Any]:
    """The call that states an engine query (its own form, as ``NormalizedQuery.to_dict``)."""
    call: dict[str, Any] = {}
    aliases: dict[str, Any] = {}
    select: list[Any] = []
    for row in list(query.get("select") or []):
        expression = _call_expression(row.get("expression") or {})
        alias = str(row.get("as") or "")
        aliases[alias] = row.get("expression")
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
        expression = item.get("expression") or {}
        if expression.get("kind") == "metric_predicate":
            continue
        matched = next((name for name, expr in aliases.items() if expr == expression), None)
        refs = _call_expression(expression)
        field_id = matched or (refs if isinstance(refs, str) else None)
        if field_id is None:
            raise refusal(
                "having",
                "a result filter names a select alias or a measure or metric",
                fix="Select the expression with an alias and filter on the alias.",
            )
        word, value = operators.normalize_condition(
            item.get("op", "is"), item.get("value"), path="having", internal=True
        )
        having.append({"field": field_id, "op": word, "value": value})
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
        call["routes"] = [
            {
                "from": row["source_entity"],
                "to": row["target_entity"],
                "via": list(row["relationship_path"]),
            }
            for row in query["route_decisions"]
        ]
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
    op = _SQL_WORDS.get(" ".join(str(expression.get("op", "=")).upper().split()), "is")
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
            if time.get(key) is not None:
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
        node["op"] = _SQL_WORDS.get(
            " ".join(str(node.get("op", "=")).upper().split()), node.get("op")
        )
    return node
