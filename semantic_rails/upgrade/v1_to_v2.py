"""Translate a query call written in the previous shape (v1) into the current call (v2).

``translate_query(query)`` returns ``Result(query, call, flags)``:

- ``query`` is the current call;
- ``call`` holds what left the body, the arguments beside a call (``now``, ``context``,
  ``max_rows``, ``row_format``, ``mode``, ``verbosity``, ``sql_profile``, ``limits``,
  ``request_id`` and ``debug``);
- ``flags`` are ``{code, path, detail}`` dicts. ``FLAG_CODES`` says, per code, whether the site
  was kept in its previous form (``kept``), translated with a meaning a person should check
  (``review``), or translated with a note (``info``).

A site is translated only when the current form means the same thing, apart from the sites a
``review`` flag names. Any other site is left exactly as it was, with a ``kept`` flag saying
why: the translator never guesses, and never mutates its input. The previous shape includes
the spellings the MCP boundary accepted (``div``, ``operands``, tool arguments inside the query,
route options) and the expression spellings the engine has since retired (``kind: binary``,
``measure_ref``, ``null_behavior``, a query's ``path_policy``).

``previous_call`` says whether a package's example or test query is written in the previous
shape; ``project upgrade`` rewrites those with this translator (``rules_calls``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import operators
from ..errors import SemanticLayerError
from ..expressions import _VALID_KEYS_BY_KIND, ARITHMETIC_OPS, _expression_children
from ..query_call import ADDED_KIND_KEYS, EXPRESSION_KEYS, EXPRESSION_LISTS, TIME_KEYS, read_call

KEPT = "kept"
REVIEW = "review"
INFO = "info"

FLAG_CODES: dict[str, tuple[str, str]] = {
    # Sites left in their previous form.
    "EXPORT_RETIRED": (KEPT, "export: true has no current form"),
    "NEEDS_PACKAGE_METRIC": (KEPT, "temporal_role_overrides becomes a package metric"),
    "CONTEXTUAL_NO_PERIOD": (KEPT, "a contextual predicate in a query without time.grain"),
    "PREDICATE_TIME_GRAIN": (KEPT, "a predicate time_grain has no current form"),
    "UNTRANSLATABLE_OP": (KEPT, "an op (or op and value) with no exact current word"),
    "EMPTY_LIST_VALUE": (KEPT, "an empty list value is refused"),
    "NULL_IN_LIST": (KEPT, "a null inside a list value is refused"),
    "HAVING_NEEDS_SELECT": (
        KEPT,
        "a metric filter whose expression is no reference or select item",
    ),
    "PREDICATE_INPUT_NOT_REF": (KEPT, "an entity filter needs a reference or a selected alias"),
    "PREDICATE_NOT_ENTITY_FILTER": (KEPT, "a predicate metric filter with no entity-filter form"),
    "SELECT_DIMENSION_REFUSED": (KEPT, "a select dimension the previous engine refused"),
    "CALENDAR_ID": (KEPT, "a non-default calendar_id has no current form"),
    "GRAIN_RETIRED": (KEPT, "grain hour or minute is retired"),
    "TIME_NEEDS_GRAIN_OR_WINDOW": (KEPT, "time with only temporal_role is refused"),
    "LIMIT_ZERO": (KEPT, "limit 0 is refused"),
    "UNTRANSLATABLE_ORDER_BY": (KEPT, "an order_by field with no current form"),
    "ROLE_KEY_RAW": (KEPT, "a raw temporal_role key without a day to year grain"),
    "PERIOD_KEY_CONFLICT": (KEPT, "a row or list would hold 'period' twice"),
    "ROUTE_OPTION_ID": (KEPT, "a route option id answers one refusal"),
    "UNKNOWN_KEY": (KEPT, "a key of neither shape"),
    "UNKNOWN_KIND": (KEPT, "an expression kind the previous parser does not know"),
    "INVALID_V1_SHAPE": (KEPT, "a shape the previous parser refused"),
    # Sites translated; a person checks the meaning.
    "NOT_KEEPS_UNKNOWNS": (REVIEW, "'not' keeps the NULL rows the previous op dropped"),
    "CONTAINS_CASE_INSENSITIVE": (REVIEW, "the text words ignore case; LIKE did not"),
    "ALIGNMENT_COLLAPSED": (REVIEW, "a predicate time_alignment folded into an over word"),
    # Sites translated with a note.
    "NAMED_WINDOW": (INFO, "a named window, which answers every period"),
    "NAMED_WINDOW_FILL_DROPPED": (INFO, "fill on a named window is implied"),
    "EXPLAIN_DROPPED": (INFO, "explain: true left the body"),
    "REF_POSITION_UNLISTED": (INFO, "a reference in a slot the translation leaves as it is"),
    "RETIRED_KEY_DROPPED": (INFO, "a retired key (null_behavior, path_policy) was deleted"),
}

# Expression spellings the engine retired, in the form the expression-arithmetic rule writes.
RENAMED_KINDS = {"binary": "arithmetic", "measure_ref": "measure"}
# Retired keys by kind, as (retired, current); the current key wins when both appear.
RENAMED_KEYS = {
    "conversion": ("matching", "matching_mode"),
    "in": ("left", "expr"),
    "not_in": ("left", "expr"),
}

_ROLE_GRAIN_KEY = re.compile(r"^temporal_role\..+__(?:day|week|month|quarter|year)$")
_ROLE_KEY = re.compile(r"^temporal_role\.\S+$")

# The arguments beside a call, in the order ``Result.call`` lists them; all but ``now`` and
# ``context`` (which came from ``policy_context``) could sit inside the previous call.
_CALL_ORDER = (
    "now",
    "context",
    "max_rows",
    "row_format",
    "mode",
    "verbosity",
    "sql_profile",
    "limits",
    "request_id",
    "debug",
)
_CALL_KEYS = frozenset(_CALL_ORDER[2:])

_V1_TIME_KEYS = frozenset(
    {"temporal_role", "grain", "start", "end", "fill", "calendar_id", "range"}
)

# The keys each kind takes: the parser's table, with the current additions.
_KIND_KEYS: dict[str, frozenset[str]] = {
    kind: frozenset(keys) - {"kind"} | ADDED_KIND_KEYS.get(kind, frozenset())
    for kind, keys in _VALID_KEYS_BY_KIND.items()
}
# Slots that hold an operand, as the current reader reads them: a reference there is its id.
_OPERAND_KEYS = EXPRESSION_KEYS | {"else"}
_KIND_OPERANDS = {
    "aggregate_if": "value",
    "nullif": "value",
    "date_add": "value",
    "distribution": "over",
}
_LOWERCASE_OP_KINDS = frozenset({"arithmetic", "boolean", "comparison"})
# The op spellings the previous MCP boundary rewrote, by kind.
_OP_ALIASES = {
    "arithmetic": {"sub": "subtract", "mul": "multiply", "div": "divide"},
    "comparison": {
        "eq": "=",
        "equals": "=",
        "neq": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "is_not_null": "IS NOT NULL",
    },
}
_MEASURE_OVERRIDES = ("aggregation", "temporal_role", "parameters")

_PREDICATE_KEYS = frozenset(
    {
        "input",
        "metric",
        "measure",
        "entity",
        "op",
        "value",
        "scope_mode",
        "time_grain",
        "time_alignment",
        "window",
        "over",
    }
)
_OVER_WORDS = frozenset({"all_time", "window", "period"})

_MISSING = object()


@dataclass
class Result:
    query: Any
    call: dict[str, Any] = field(default_factory=dict)
    flags: list[dict[str, str]] = field(default_factory=list)

    @property
    def kept_flags(self) -> list[dict[str, str]]:
        return [flag for flag in self.flags if FLAG_CODES[flag["code"]][0] == KEPT]


class _Ctx:
    def __init__(self) -> None:
        self.flags: list[dict[str, str]] = []
        # The enclosing query's grain ("" when absent).
        self.grain = ""
        # (translated expression, alias) per select item.
        self.select_exprs: list[tuple[Any, Any]] = []

    def flag(self, code: str, path: str, detail: str) -> None:
        assert code in FLAG_CODES, code
        self.flags.append({"code": code, "path": path, "detail": detail})

    def sub(self) -> _Ctx:
        """A context whose flags are kept only if the site translates."""
        sub = _Ctx()
        sub.grain, sub.select_exprs = self.grain, self.select_exprs
        return sub

    def kept_since(self, count: int) -> bool:
        return any(FLAG_CODES[flag["code"]][0] == KEPT for flag in self.flags[count:])


def _last_segment(ref: Any) -> str | None:
    return ref.split(".")[-1] if isinstance(ref, str) else None


# --- Retired spellings -------------------------------------------------------------------


def _respelled(node: Any, path: str, ctx: _Ctx) -> Any:
    """``node`` with each retired spelling in its current form; at the top (``path`` "") a
    call's ``path_policy`` goes too."""
    if isinstance(node, list):
        return [_respelled(item, f"{path}[{index}]", ctx) for index, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    children = {key for key, _ in _expression_children(node)}
    kind = str(node.get("kind", "")).strip()
    old, new = RENAMED_KEYS.get(kind, ("", ""))
    out: dict[str, Any] = {}
    for key, value in node.items():
        at = f"{path}.{key}" if path else str(key)
        if key == "null_behavior" or (not path and key == "path_policy"):
            ctx.flag("RETIRED_KEY_DROPPED", at, f"{key} was retired; it was deleted")
        elif not (key == old and new in node):
            out[new if key == old else key] = (
                _respelled(value, at, ctx) if key in children else value
            )
    if kind in RENAMED_KINDS:
        out["kind"] = RENAMED_KINDS[kind]
    return out


# --- Operators ---------------------------------------------------------------------------


def _norm_op(op: str) -> str:
    return " ".join(op.upper().split())


_EQ = frozenset({"=", "==", "EQ", "EQUALS"})
_NEQ = frozenset({"!=", "<>", "NEQ"})
_ORDER = {
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "GT": "gt",
    "GTE": "gte",
    "LT": "lt",
    "LTE": "lte",
}
_IS_NULL = frozenset({"IS NULL", "IS_NULL"})
_IS_NOT_NULL = frozenset({"IS NOT NULL", "IS_NOT_NULL", "NOT_NULL"})


def _check_list(value: list[Any], path: str, ctx: _Ctx) -> bool:
    if not value:
        ctx.flag("EMPTY_LIST_VALUE", path, "an empty list; say 'missing' or drop the condition")
        return False
    if any(item is None for item in value):
        ctx.flag("NULL_IN_LIST", path, "a null inside the list; split it into a 'missing' item")
        return False
    if any(isinstance(item, (dict, list)) for item in value):
        ctx.flag("INVALID_V1_SHAPE", path, "list elements must be scalars")
        return False
    return True


def _like(norm: str, pattern: Any, path: str, ctx: _Ctx) -> tuple[str, Any] | None:
    """The text word for a LIKE or NOT LIKE ``pattern``, flagged; None (flagged) for none."""
    said = operators.like_word(norm.lower(), pattern)
    if said is None:
        ctx.flag(
            "UNTRANSLATABLE_OP",
            f"{path}.op",
            f"{norm} {pattern!r} has no text word ('X%', '%X' or '%X%'); write the exact "
            "values with is",
        )
        return None
    ctx.flag(
        "CONTAINS_CASE_INSENSITIVE",
        path,
        f"{norm} {pattern!r} became {said[0]}, which ignores case",
    )
    if norm == "NOT LIKE":
        ctx.flag("NOT_KEEPS_UNKNOWNS", path, f"{said[0]} keeps the NULL rows NOT LIKE dropped")
    return said


def _op(
    op: Any, value: Any, path: str, ctx: _Ctx, *, null_tests: bool = True
) -> tuple[str, Any] | None:
    """The current ``(op, value)`` for a previous filter op and value, or None (flagged) to keep
    the site. ``value`` is ``_MISSING`` when the key is absent, which the previous shape read
    as null."""
    if op is _MISSING:
        op = "="  # the previous default
    if not isinstance(op, str):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.op", f"op must be a string, got {type(op).__name__}")
        return None
    raw_value = None if value is _MISSING else value
    if isinstance(raw_value, dict):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.value", "a filter value must be a scalar or a list")
        return None
    norm = _norm_op(op)

    def missing(flag_value: bool) -> tuple[str, Any] | None:
        if not null_tests:
            ctx.flag("UNTRANSLATABLE_OP", f"{path}.op", f"{op!r} tests for null here")
            return None
        return "missing", flag_value

    def needs_scalar(word: str, *, keeps_unknowns: bool = False) -> tuple[str, Any] | None:
        if isinstance(raw_value, list):
            ctx.flag("UNTRANSLATABLE_OP", f"{path}.value", f"{op!r} takes one value, got a list")
            return None
        if keeps_unknowns:
            ctx.flag("NOT_KEEPS_UNKNOWNS", path, f"{op!r} became 'not'; NULL rows are now kept")
        return word, value

    # The current words, and the previous null-safe IS forms that share their spelling.
    if norm == "IS":
        if raw_value is None:
            return missing(True)
        if op != "is" and not isinstance(raw_value, bool):
            ctx.flag("INVALID_V1_SHAPE", f"{path}.value", f"{op!r} took null or a boolean")
            return None
        return "is", value
    if norm == "IS NOT":
        if raw_value is None:
            return missing(False)
        if isinstance(raw_value, bool):
            return "not", value
        ctx.flag("UNTRANSLATABLE_OP", f"{path}.op", "IS NOT takes null or a boolean")
        return None
    if norm == "NOT" and op == "not":
        if raw_value is None:
            ctx.flag("INVALID_V1_SHAPE", f"{path}.value", "'not' needs a value")
            return None
        return "not", value
    if norm == "MISSING" and op == "missing":
        if isinstance(raw_value, bool):
            return "missing", value
        ctx.flag("INVALID_V1_SHAPE", f"{path}.value", "'missing' takes a boolean")
        return None
    if op in operators.TEXT_WORDS:
        return op, value
    if norm in _EQ:
        return missing(True) if raw_value is None else needs_scalar("is")
    if norm in _NEQ:
        return missing(False) if raw_value is None else needs_scalar("not", keeps_unknowns=True)
    if norm in {"IN", "NOT IN"}:
        word = "is" if norm == "IN" else "not"
        if raw_value is None:
            ctx.flag("UNTRANSLATABLE_OP", f"{path}.value", f"{op!r} with a null value was refused")
            return None
        if isinstance(raw_value, list) and not _check_list(raw_value, f"{path}.value", ctx):
            return None
        if word == "not":
            ctx.flag("NOT_KEEPS_UNKNOWNS", path, f"{op!r} became 'not'; NULL rows are now kept")
        return word, value
    if norm == "IS DISTINCT FROM":
        return missing(False) if raw_value is None else needs_scalar("not")
    if norm == "IS NOT DISTINCT FROM":
        return missing(True) if raw_value is None else needs_scalar("is")
    if norm in _ORDER:
        if raw_value is None:
            ctx.flag("UNTRANSLATABLE_OP", f"{path}.value", f"{op!r} against null was refused")
            return None
        if isinstance(raw_value, list):
            ctx.flag("UNTRANSLATABLE_OP", f"{path}.value", f"{op!r} takes one value, got a list")
            return None
        return _ORDER[norm], value
    if norm in _IS_NULL:
        return missing(True)
    if norm in _IS_NOT_NULL:
        return missing(False)
    if norm in {"LIKE", "NOT LIKE"}:
        return _like(norm, raw_value, path, ctx)
    ctx.flag("UNTRANSLATABLE_OP", f"{path}.op", f"no current word for {op!r}")
    return None


def _with_op(item: dict[str, Any], op_word: str, value: Any, *, op_after: str) -> dict[str, Any]:
    """``item`` with ``op`` and ``value`` set, keeping key order (``op`` after ``op_after``)."""
    out: dict[str, Any] = {}
    placed = False
    for key, old in item.items():
        if key == "op":
            out["op"] = op_word
            placed = True
        elif key == "value":
            if not placed:
                out["op"] = op_word
                placed = True
            out["value"] = value
        else:
            out[key] = old
            if key == op_after and "op" not in item and not placed:
                out["op"] = op_word
                placed = True
    if not placed:
        out["op"] = op_word
    out.setdefault("value", value)
    return out


def _condition(item: Any, path: str, ctx: _Ctx, *, dimension_key: bool = False) -> Any:
    """A ``{field, op, value}`` row condition (``{dimension, ...}`` too in aggregate filters)."""
    if not isinstance(item, dict):
        ctx.flag("INVALID_V1_SHAPE", path, "a condition must be an object")
        return item
    if dimension_key and "dimension" in item and "field" not in item:
        # Inline aggregate filters took {dimension, op, value}, read as field.
        unknown = sorted(set(item) - {"dimension", "op", "value"})
        if unknown:
            ctx.flag("INVALID_V1_SHAPE", path, f"unsupported keys {unknown}")
            return item
        as_field = {("field" if key == "dimension" else key): value for key, value in item.items()}
        out = _condition(as_field, path, ctx)
        return item if out is as_field else out
    unknown = sorted(set(item) - {"field", "op", "value"})
    if unknown:
        ctx.flag("INVALID_V1_SHAPE", path, f"unsupported keys {unknown}")
        return item
    name = item.get("field")
    if name is None or (isinstance(name, str) and not name.strip()):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.field", "a condition needs a field")
        return item
    mapped = _op(item.get("op", _MISSING), item.get("value", _MISSING), path, ctx)
    if mapped is None:
        return item
    return _with_op(item, *mapped, op_after="field")


def _value_filter(item: Any, path: str, ctx: _Ctx) -> Any:
    """An entity_value ``{kind?: value_filter, op, value}`` filter on the per-entity value."""
    if not isinstance(item, dict) or set(item) - {"kind", "op", "value"}:
        ctx.flag("INVALID_V1_SHAPE", path, "a value filter takes kind, op and value")
        return item
    mapped = _op(item.get("op", _MISSING), item.get("value", _MISSING), path, ctx)
    if mapped is None:
        return item
    return _with_op(item, *mapped, op_after="kind")


# --- References and expressions ----------------------------------------------------------


def _measure_like(expr: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    """A measure (kindless or ``measure``): its id, or the override object."""
    unknown = sorted(set(expr) - {"kind", "measure", *_MEASURE_OVERRIDES})
    measure = expr.get("measure")
    if unknown:
        ctx.flag("INVALID_V1_SHAPE", path, f"a measure reference with unsupported keys {unknown}")
        return expr
    if not isinstance(measure, str) or not measure.strip():
        ctx.flag("INVALID_V1_SHAPE", f"{path}.measure", "a measure id is a non-empty string")
        return expr
    overrides = {
        key: expr[key]
        for key in _MEASURE_OVERRIDES
        if key in expr
        and not (key == "parameters" and expr[key] in (None, {}))
        and not (isinstance(expr[key], str) and not expr[key].strip())
    }
    return (
        {"kind": "measure", "measure": measure.strip(), **overrides}
        if overrides
        else measure.strip()
    )


def _metric_ref(expr: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    metric = expr.get("metric")
    if set(expr) - {"kind", "metric"} or not isinstance(metric, str) or not metric.strip():
        ctx.flag("INVALID_V1_SHAPE", path, "a metric reference takes only 'metric'")
        return expr
    return metric.strip()


def _expr(expr: Any, path: str, ctx: _Ctx) -> Any:
    """Translate an expression in an operand slot."""
    if not isinstance(expr, dict):
        return expr
    kind = expr.get("kind", _MISSING)
    if kind is _MISSING:
        if "measure" in expr:
            return _measure_like(expr, path, ctx)
        if "metric" in expr:
            return _metric_ref(expr, path, ctx)
        ctx.flag("INVALID_V1_SHAPE", path, "an expression needs a kind")
        return expr
    if not isinstance(kind, str):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.kind", "kind is not a string")
        return expr
    kind = kind.strip()
    if kind == "measure":
        return _measure_like(expr, path, ctx)
    if kind == "metric":
        return _metric_ref(expr, path, ctx)
    if kind not in _KIND_KEYS:
        ctx.flag("UNKNOWN_KIND", f"{path}.kind", f"unknown kind {expr['kind']!r}")
        return expr
    lists = {"operands", "terms"} if kind == "arithmetic" else set()
    unknown = sorted(set(expr) - _KIND_KEYS[kind] - lists - {"kind"})
    if unknown:
        ctx.flag("INVALID_V1_SHAPE", path, f"kind {kind!r} with unsupported keys {unknown}")
        return expr
    if lists & set(expr):
        folded = _folded(expr, path, ctx)
        out = folded if folded is None else _generic(folded, kind, path, ctx)
        return expr if out is folded else out  # a kept arithmetic stays unfolded
    if kind == "prior_period":
        return _prior_period(expr, path, ctx)
    if kind == "metric_predicate":
        return _predicate(expr, path, ctx)
    if kind == "scoped_aggregate":
        return _scoped_aggregate(expr, path, ctx)
    if kind in ("aggregate", "semi_additive"):
        return _aggregate(expr, path, ctx)
    if kind == "entity_value":
        return _entity_value(expr, path, ctx)
    return _generic(expr, kind, path, ctx)


def _folded(expr: dict[str, Any], path: str, ctx: _Ctx) -> dict[str, Any] | None:
    """An arithmetic with an ``operands`` or ``terms`` list, folded left into ``left`` and
    ``right`` as the previous MCP boundary did; None (flagged) for a list it refused."""
    keys = set(expr) & {"operands", "terms", "left", "right"}
    key = next(iter(keys))
    operands = expr[key]
    if len(keys) != 1 or not isinstance(operands, list) or len(operands) < 2:
        ctx.flag("INVALID_V1_SHAPE", path, "an arithmetic takes one list of two or more operands")
        return None
    left = operands[0]
    for right in operands[1:-1]:
        left = {"kind": "arithmetic", "op": expr.get("op"), "left": left, "right": right}
    out = {name: value for name, value in expr.items() if name != key}
    return {**out, "left": left, "right": operands[-1]}


def _generic(expr: dict[str, Any], kind: str, path: str, ctx: _Ctx) -> Any:
    out: dict[str, Any] = {}
    for key, value in expr.items():
        at = f"{path}.{key}"
        if key in _OPERAND_KEYS or _KIND_OPERANDS.get(kind) == key:
            out[key] = _expr(value, at, ctx)
        elif key in EXPRESSION_LISTS and isinstance(value, list):
            out[key] = [_expr(item, f"{at}[{index}]", ctx) for index, item in enumerate(value)]
        elif key == "whens" and isinstance(value, list):
            out[key] = [
                {part: _expr(item, f"{at}[{index}].{part}", ctx) for part, item in when.items()}
                if isinstance(when, dict)
                else when
                for index, when in enumerate(value)
            ]
        elif key == "op" and kind in _LOWERCASE_OP_KINDS and isinstance(value, str):
            spelling = " ".join(value.lower().split())
            out[key] = _OP_ALIASES.get(kind, {}).get(spelling, spelling)
            if kind == "arithmetic" and out[key] not in ARITHMETIC_OPS:
                ctx.flag("UNTRANSLATABLE_OP", at, f"no arithmetic op {value!r}")
                return expr
        else:
            if key != "value" and _has_v1_ref(value):
                ctx.flag(
                    "REF_POSITION_UNLISTED", at, f"a reference inside {kind}.{key} is left as it is"
                )
            out[key] = value
    if kind == "comparison" and out.get("op") in {"ilike", "not ilike"}:
        ctx.flag("UNTRANSLATABLE_OP", f"{path}.op", f"no current word for {expr['op']!r}")
        return expr
    if kind == "comparison" and out.get("op") in {"like", "not like"}:
        right: dict[str, Any] = out["right"] if isinstance(out.get("right"), dict) else {}
        pattern = right.get("value") if right.get("kind") == "literal" else None
        said = _like(out["op"].upper(), pattern, path, ctx)
        if said is None:
            return expr
        out["op"], out["right"] = said[0], {**right, "value": said[1]}
    return out


def _has_v1_ref(value: Any) -> bool:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind in ("metric", "measure") or (
            kind is None and ("metric" in value or "measure" in value)
        ):
            return True
        return any(_has_v1_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_v1_ref(item) for item in value)
    return False


def _prior_period(expr: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    measure = expr.get("measure", _MISSING)
    offset = expr.get("offset", _MISSING)
    if measure is not _MISSING and not isinstance(offset, dict):
        # The previous shorthand {kind, measure, offset: <int>, grain, aggregation?}.
        if not isinstance(measure, str) or not measure.strip() or "input" in expr:
            ctx.flag("INVALID_V1_SHAPE", path, "the prior_period shorthand needs one measure id")
            return expr
        offset = -1 if offset is _MISSING else offset
        if isinstance(offset, bool) or not isinstance(offset, int) or offset == 0:
            ctx.flag(
                "INVALID_V1_SHAPE", f"{path}.offset", "the shorthand offset is a non-zero integer"
            )
            return expr
        grain = expr.get("grain")
        if not isinstance(grain, str) or not grain.strip():
            ctx.flag("INVALID_V1_SHAPE", f"{path}.grain", "the shorthand needs a grain")
            return expr
        aggregation = expr.get("aggregation", "")
        source: Any = measure.strip()
        if not (isinstance(aggregation, str) and not aggregation.strip()):
            source = {"kind": "measure", "measure": measure.strip(), "aggregation": aggregation}
        return {
            "kind": "prior_period",
            "input": source,
            "offset": {"unit": grain.strip(), "value": abs(offset)},
        }
    stray = sorted({"measure", "grain", "aggregation"} & set(expr))
    if stray:
        ctx.flag("INVALID_V1_SHAPE", path, f"prior_period with an offset object ignores {stray}")
        return expr
    return _generic(expr, "prior_period", path, ctx)


def _scoped_aggregate(expr: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    out: dict[str, Any] = {}
    for key, value in expr.items():
        at = f"{path}.{key}"
        if key == "where" and isinstance(value, list):
            out[key] = [_condition(item, f"{at}[{index}]", ctx) for index, item in enumerate(value)]
        elif key == "predicates" and isinstance(value, list):
            out[key] = [_predicate(item, f"{at}[{index}]", ctx) for index, item in enumerate(value)]
        else:
            out[key] = value
    return out


def _aggregate(expr: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    rows = expr.get("filter")
    if not isinstance(rows, dict) or set(rows) != {"all"} or not isinstance(rows["all"], list):
        return expr
    clauses = []
    for index, clause in enumerate(rows["all"]):
        at = f"{path}.filter.all[{index}]"
        if (
            isinstance(clause, dict)
            and set(clause) == {"expression"}
            and isinstance(clause["expression"], dict)
        ):
            clauses.append(
                {"expression": _predicate(clause["expression"], f"{at}.expression", ctx)}
            )
        else:
            clauses.append(_condition(clause, at, ctx, dimension_key=True))
    return {**expr, "filter": {"all": clauses}}


def _entity_value(expr: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    out: dict[str, Any] = {}
    for key, value in expr.items():
        at = f"{path}.{key}"
        if key == "input":
            out[key] = _expr(value, at, ctx)
        elif key == "where" and isinstance(value, list):
            out[key] = [
                _value_filter(item, f"{at}[{index}]", ctx) for index, item in enumerate(value)
            ]
        else:
            out[key] = value
    return out


def _over_word(pred: dict[str, Any], path: str, ctx: _Ctx) -> str | None:
    """The ``over`` word for a previous predicate's scope_mode, time_grain and time_alignment."""
    scope = str(pred.get("scope_mode", "") or "").strip() or "contextual"
    grain = str(pred.get("time_grain", "") or "").strip()
    alignment = str(pred.get("time_alignment", "") or "").strip()
    if grain:
        ctx.flag(
            "PREDICATE_TIME_GRAIN",
            f"{path}.time_grain",
            f"time_grain {grain!r} has no current form",
        )
        return None
    if scope == "entity_only":
        if not alignment:
            return "all_time"
        if alignment == "query_window":
            return "window"
        if alignment == "rolling_window_in_period":
            ctx.flag(
                "ALIGNMENT_COLLAPSED",
                f"{path}.time_alignment",
                "rolling_window_in_period became over: window",
            )
            return "window"
        ctx.flag("INVALID_V1_SHAPE", f"{path}.time_alignment", f"entity_only with {alignment!r}")
        return None
    if scope == "contextual":
        if alignment not in ("", "same_query_period"):
            ctx.flag("INVALID_V1_SHAPE", f"{path}.time_alignment", f"contextual with {alignment!r}")
            return None
        if not ctx.grain:
            ctx.flag(
                "CONTEXTUAL_NO_PERIOD",
                path,
                "a contextual predicate needs time.grain to become over: period",
            )
            return None
        if alignment == "same_query_period":
            ctx.flag(
                "ALIGNMENT_COLLAPSED",
                f"{path}.time_alignment",
                "same_query_period became over: period",
            )
        return "period"
    ctx.flag("INVALID_V1_SHAPE", f"{path}.scope_mode", f"unknown scope_mode {scope!r}")
    return None


def _predicate(pred: Any, path: str, ctx: _Ctx) -> Any:
    """A ``metric_predicate`` expression or a scoped_aggregate predicate row."""
    if not isinstance(pred, dict):
        ctx.flag("INVALID_V1_SHAPE", path, "a predicate must be an object")
        return pred
    allowed = _KIND_KEYS["metric_predicate"] if "kind" in pred else _PREDICATE_KEYS
    unknown = sorted(set(pred) - allowed - {"kind"})
    if unknown or ("kind" in pred and pred["kind"] != "metric_predicate"):
        ctx.flag(
            "INVALID_V1_SHAPE", path, f"a predicate with unsupported keys {unknown or ['kind']}"
        )
        return pred
    if not ({"input", "metric", "measure"} & set(pred)) or ("kind" in pred and "input" not in pred):
        ctx.flag("INVALID_V1_SHAPE", path, "a predicate needs an input")
        return pred
    scope_keys = {"scope_mode", "time_grain", "time_alignment"} & set(pred)
    if "over" in pred:
        if pred["over"] not in _OVER_WORDS or scope_keys:
            ctx.flag("INVALID_V1_SHAPE", f"{path}.over", "over mixed with the previous scope keys")
            return pred
        over = pred["over"]
    else:
        over = _over_word(pred, path, ctx)
        if over is None:
            return pred
    if "op" not in pred:
        ctx.flag("INVALID_V1_SHAPE", f"{path}.op", "a predicate needs an op")
        return pred
    mapped = _op(pred["op"], pred.get("value", _MISSING), path, ctx, null_tests=False)
    if mapped is None:
        return pred
    out: dict[str, Any] = {}
    for key, old in pred.items():
        if key in scope_keys or key == "over":
            out.setdefault("over", over)
        elif key == "op":
            out["op"] = mapped[0]
        elif key == "value":
            out["value"] = mapped[1]
        elif key == "input":
            out["input"] = _expr(old, f"{path}.input", ctx)
        else:
            out[key] = old
    out.setdefault("over", over)
    return out


# --- Select ------------------------------------------------------------------------------


def _dimension_of(item: dict[str, Any]) -> tuple[Any, list[str]] | None:
    """For a select dimension item: (dimension id, keys refused beside it), else None."""
    if "expression" in item:
        expr = item["expression"]
        if (
            isinstance(expr, dict)
            and set(expr) - {"kind"} == {"dimension"}
            and expr.get("kind", "dimension") in ("dimension", "group", "ref")
        ):
            return expr["dimension"], sorted(set(item) - {"expression"})
        return None
    if "dimension" in item:
        extra = sorted(set(item) - {"dimension", "kind"})
        if "kind" in item and item["kind"] not in ("dimension", "group", "ref"):
            extra.append("kind")
        return item["dimension"], extra
    return None


def _bare_select_expression(item: dict[str, Any]) -> dict[str, Any] | None:
    keys = set(item) - {"as"}
    if keys == {"metric"}:
        return {"kind": "metric", "metric": item["metric"]}
    if "measure" in keys and keys <= {"measure", "aggregation"}:
        return {"kind": "measure", **{key: item[key] for key in item if key != "as"}}
    return None


def _select_item(item: Any, path: str, position: int, ctx: _Ctx) -> tuple[Any, Any]:
    """(translated item, its column name) for a select item that is not a dimension."""
    if isinstance(item, str):
        return item, _last_segment(item)
    if not isinstance(item, dict):
        ctx.flag("INVALID_V1_SHAPE", path, "a select item must be an object")
        return item, None
    if "expression" in item:
        named = sorted({"metric", "measure", "dimension"} & set(item))
        unknown = sorted(set(item) - {"expression", "as"})
        if named or unknown:
            ctx.flag("INVALID_V1_SHAPE", path, f"a select item with keys {named or unknown}")
            return item, None
        raw_expr = item["expression"]
    else:
        raw_expr = _bare_select_expression(item)
        if raw_expr is None:
            ctx.flag("INVALID_V1_SHAPE", path, f"a select item with keys {sorted(item)}")
            return item, None
    alias = item.get("as", _MISSING)
    if alias is not _MISSING and not isinstance(alias, str):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.as", "an alias must be a string")
        return item, None
    alias_text = alias.strip() if isinstance(alias, str) else ""
    before = len(ctx.flags)
    expr = _expr(raw_expr, f"{path}.expression", ctx)
    if ctx.kept_since(before) and expr == raw_expr:
        return item, alias_text or None
    if isinstance(expr, dict) and expr.get("kind") == "metric_predicate":
        ctx.flag("INVALID_V1_SHAPE", path, "a metric_predicate cannot be selected")
        return item, None
    if isinstance(expr, str):
        default = _last_segment(expr)
        if not alias_text or alias_text == default:
            return expr, default
        return {"expression": expr, "as": alias_text}, alias_text
    # The previous shape named a measure's column by the measure's last segment and any other
    # unaliased kind expr_<n>; the current one names every unaliased kind expr_<n>, so the
    # column's name is written.
    fallback = _last_segment(expr.get("measure")) if expr.get("kind") == "measure" else None
    final_alias = alias_text or fallback or f"expr_{position + 1}"
    return {"expression": expr, "as": final_alias}, final_alias


def _translate_select(
    select: list[Any], group_by: Any, path: str, ctx: _Ctx
) -> tuple[list[Any], list[Any]]:
    """(new select list, dimensions lifted into group_by)."""
    plain_group_by = [_group_by_id(item) for item in group_by] if isinstance(group_by, list) else []
    out: list[Any] = []
    lifted: list[Any] = []
    position = 0
    for index, item in enumerate(select):
        at = f"{path}[{index}]"
        dimension = _dimension_of(item) if isinstance(item, dict) else None
        if dimension is None:
            new, alias = _select_item(item, at, position, ctx)
            out.append(new)
            if alias is not None:
                ctx.select_exprs.append((_select_expression(new), alias))
            position += 1
            continue
        dimension_id, extra = dimension
        refusal = (
            "a dimension id must be a string"
            if not isinstance(dimension_id, str) or not dimension_id.strip()
            else f"the previous engine refused a select dimension with {extra}"
            if extra
            else f"{dimension_id.strip()} is absent from a non-empty group_by"
            if plain_group_by and dimension_id.strip() not in plain_group_by
            else ""
        )
        if refusal:
            code = (
                "INVALID_V1_SHAPE"
                if refusal.startswith("a dimension")
                else "SELECT_DIMENSION_REFUSED"
            )
            ctx.flag(code, at, refusal)
            out.append(item)
            position += 1
        elif dimension_id.strip() not in (*plain_group_by, *lifted):
            lifted.append(dimension_id.strip())
    return out, lifted


def _select_expression(item: Any) -> Any:
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and "expression" in item and set(item) <= {"expression", "as"}:
        return item["expression"]
    return _MISSING


def _group_by_id(item: Any) -> Any:
    if isinstance(item, dict):
        for key in ("dimension", "field"):
            if key in item:
                return item[key].strip() if isinstance(item[key], str) else item[key]
    return item


def _translate_group_by(group_by: Any, path: str, ctx: _Ctx) -> Any:
    if not isinstance(group_by, list):
        ctx.flag("INVALID_V1_SHAPE", path, "group_by must be a list")
        return group_by
    out = []
    for index, item in enumerate(group_by):
        if isinstance(item, dict) and (len(item) != 1 or _group_by_id(item) is item):
            ctx.flag("INVALID_V1_SHAPE", f"{path}[{index}]", "a group_by object is {dimension: id}")
            out.append(item)
        else:
            out.append(_group_by_id(item))
    return out


# --- Time, order_by and routes ------------------------------------------------------------


def _translate_time(time: Any, path: str, ctx: _Ctx) -> Any:
    """The new time block, or _MISSING to drop the key."""
    if time is None or time == {}:
        return _MISSING
    if not isinstance(time, dict):
        ctx.flag("INVALID_V1_SHAPE", path, "time must be an object")
        return time
    for key in sorted(set(time) - _V1_TIME_KEYS - set(TIME_KEYS)):
        ctx.flag("UNKNOWN_KEY", f"{path}.{key}", f"time key {key!r} is of neither shape")
    grain = time.get("grain", "")
    ctx.grain = grain.strip() if isinstance(grain, str) else ""
    if "range" in time and {"start", "end", "last", "this"} & set(time):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.range", "range beside start or end was refused")
        return time
    span = time.get("range", _MISSING)
    if span is not _MISSING and not (isinstance(span, dict) and set(span) == {"last"}):
        ctx.flag("INVALID_V1_SHAPE", f"{path}.range", "range takes only {last: {unit, value}}")
        return time
    start, end = time.get("start"), time.get("end")
    named = (
        (start is not None and end is not None)
        or span is not _MISSING
        or "last" in time
        or "this" in time
    )
    out: dict[str, Any] = {}
    for key, value in time.items():
        at = f"{path}.{key}"
        if key == "grain":
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if isinstance(value, str) and value.strip().lower() in ("hour", "minute"):
                ctx.flag("GRAIN_RETIRED", at, f"grain {value!r} is retired")
            out[key] = value
        elif key == "range":
            out["last"] = value["last"]
        elif key == "calendar_id":
            if value is None or (
                isinstance(value, str) and value.strip().lower() in ("", "default")
            ):
                continue
            ctx.flag("CALENDAR_ID", at, f"calendar_id {value!r} has no current form")
            out[key] = value
        elif key == "fill" and named:
            if value:
                ctx.flag("NAMED_WINDOW_FILL_DROPPED", at, "fill is implied by a named window")
        elif not (key in ("start", "end") and value is None):
            out[key] = value
    if not out.get("grain") and not {"start", "end", "last", "this"} & set(out):
        ctx.flag("TIME_NEEDS_GRAIN_OR_WINDOW", path, "time with only temporal_role is refused")
    if start is not None and end is not None:
        ctx.flag("NAMED_WINDOW", path, "start and end: a named window, which answers every period")
    elif span is not _MISSING:
        ctx.flag(
            "NAMED_WINDOW",
            path,
            "range.last became last: a named window, which answers every period",
        )
    return out


def _time_alias(time: Any) -> str | None:
    """The previous time column for a time block, or None when unknown."""
    if not isinstance(time, dict) or not isinstance(time.get("temporal_role"), str):
        return None
    grain = time.get("grain") or ""
    if not isinstance(grain, str):
        return None
    role = time["temporal_role"].strip()
    return f"{role}__{grain.strip()}" if grain.strip() else role


def _translate_order_by(order_by: Any, time: Any, path: str, ctx: _Ctx) -> Any:
    if not isinstance(order_by, list):
        ctx.flag("INVALID_V1_SHAPE", path, "order_by must be a list")
        return order_by
    time_alias = _time_alias(time)
    out = []
    for index, item in enumerate(order_by):
        at = f"{path}[{index}]"
        if not isinstance(item, dict):
            ctx.flag("INVALID_V1_SHAPE", at, "an order_by item must be an object")
            out.append(item)
            continue
        unknown = sorted(set(item) - {"field", "direction"})
        if unknown:
            ctx.flag("UNKNOWN_KEY", at, f"order_by keys {unknown} were ignored and are now refused")
            out.append(item)
            continue
        name = item.get("field")
        if name == "time":
            if not time:
                ctx.flag("INVALID_V1_SHAPE", f"{at}.field", "order_by 'time' without a time axis")
                out.append(item)
                continue
            if time_alias is not None and not _ROLE_GRAIN_KEY.match(time_alias):
                ctx.flag(
                    "UNTRANSLATABLE_ORDER_BY",
                    f"{at}.field",
                    "'time' with no grain sorts raw timestamps",
                )
                out.append(item)
                continue
            name = "period"
        elif isinstance(name, str) and _ROLE_GRAIN_KEY.match(name):
            if time_alias is not None and name != time_alias:
                ctx.flag(
                    "UNTRANSLATABLE_ORDER_BY",
                    f"{at}.field",
                    f"{name} is not this query's time axis",
                )
                out.append(item)
                continue
            name = "period"
        elif isinstance(name, str) and _ROLE_KEY.match(name):
            ctx.flag("ROLE_KEY_RAW", f"{at}.field", f"{name} names a time column with no grain")
            out.append(item)
            continue
        out.append(
            {
                key: name
                if key == "field"
                else value.strip().lower()
                if key == "direction" and isinstance(value, str)
                else value
                for key, value in item.items()
            }
        )
    return out


def _route(row: Any, path: str, ctx: _Ctx) -> dict[str, Any] | None:
    """A route decision row, or a route option's decision, as ``{from, to, via}``."""
    if isinstance(row, str):
        ctx.flag(
            "ROUTE_OPTION_ID",
            path,
            f"a route option id ({row!r}) answers one refusal; write the option's route "
            "as {from, to, via}",
        )
        return None
    if isinstance(row, dict) and "decision" in row:
        decision = row["decision"]
        if (
            set(row) - {"id", "meaning", "relationship_path", "decision", "conflicts_with"}
            or not isinstance(decision, dict)
            or (
                "relationship_path" in row
                and row["relationship_path"] != decision.get("relationship_path")
            )
        ):
            ctx.flag("INVALID_V1_SHAPE", path, "a conflicting or malformed route option")
            return None
        row = decision
    if (
        not isinstance(row, dict)
        or set(row) - {"source_entity", "target_entity", "relationship_path", "label"}
        or not {"source_entity", "target_entity", "relationship_path"} <= set(row)
    ):
        ctx.flag(
            "INVALID_V1_SHAPE",
            path,
            "a route decision needs source_entity, target_entity, relationship_path",
        )
        return None
    return {
        "from": row["source_entity"],
        "to": row["target_entity"],
        "via": row["relationship_path"],
    }


def _translate_routes(rows: Any, path: str, ctx: _Ctx) -> list[Any] | None:
    if not isinstance(rows, list):
        ctx.flag("INVALID_V1_SHAPE", path, "route_decisions must be a list")
        return None
    routes = [_route(row, f"{path}[{index}]", ctx) for index, row in enumerate(rows)]
    return None if None in routes else routes


# --- Metric filters ----------------------------------------------------------------------


def _metric_filters(items: Any, path: str, ctx: _Ctx) -> tuple[list[Any], list[Any], list[Any]]:
    """(having items, entity filters for where, metric_filters items kept as they are)."""
    having: list[Any] = []
    entity_filters: list[Any] = []
    kept: list[Any] = []
    if not isinstance(items, list):
        ctx.flag("INVALID_V1_SHAPE", path, "metric_filters must be a list")
        return having, entity_filters, [items]
    for index, item in enumerate(items):
        at = f"{path}[{index}]"
        if (
            not isinstance(item, dict)
            or set(item) - {"expression", "op", "value"}
            or "expression" not in item
        ):
            ctx.flag("INVALID_V1_SHAPE", at, "a metric filter is {expression, op, value}")
            kept.append(item)
            continue
        expr, op, value = item["expression"], item.get("op", _MISSING), item.get("value", _MISSING)
        if isinstance(expr, dict) and expr.get("kind") == "metric_predicate":
            new = _predicate_filter(expr, op, value, at, ctx)
        else:
            new = _having_item(expr, op, value, at, ctx)
        if new is None:
            kept.append(item)
        else:
            (entity_filters if "entity" in new else having).append(new)
    return having, entity_filters, kept


def _select_alias_for(expr: Any, ctx: _Ctx) -> Any:
    return next((alias for selected, alias in ctx.select_exprs if selected == expr), None)


def _having_item(expr: Any, op: Any, value: Any, path: str, ctx: _Ctx) -> dict[str, Any] | None:
    """A plain metric filter as a ``having`` condition, or None (flagged) to keep it."""
    sub = ctx.sub()
    new_expr = _expr(expr, f"{path}.expression", sub)
    if sub.kept_since(0):
        ctx.flags.extend(sub.flags)
        return None
    name = new_expr if isinstance(new_expr, str) else _select_alias_for(new_expr, ctx)
    if name is None:
        ctx.flag(
            "HAVING_NEEDS_SELECT",
            path,
            "the filter expression is no reference and matches no select item",
        )
        return None
    mapped = _op(op, value, path, ctx)
    if mapped is None:
        return None
    ctx.flags.extend(sub.flags)
    return {"field": name, "op": mapped[0], "value": mapped[1]}


def _predicate_filter(
    expr: dict[str, Any], op: Any, value: Any, path: str, ctx: _Ctx
) -> dict[str, Any] | None:
    """A metric_predicate metric filter as a where entity filter, or None (flagged) to keep it."""
    op = "=" if op is _MISSING else op
    if not (isinstance(op, str) and op.strip() == "=" and value is True):
        ctx.flag(
            "INVALID_V1_SHAPE", path, "a metric_predicate filter must be op '=' and value true"
        )
        return None
    at = f"{path}.expression"
    if "window" in expr:
        ctx.flag("PREDICATE_NOT_ENTITY_FILTER", f"{at}.window", "an entity filter has no window")
        return None
    if isinstance(expr.get("value"), (dict, list)):
        ctx.flag(
            "PREDICATE_NOT_ENTITY_FILTER",
            f"{at}.value",
            "an entity filter takes a scalar threshold",
        )
        return None
    sub = ctx.sub()
    pred = _predicate(expr, at, sub)
    if pred is expr and sub.kept_since(0):
        ctx.flags.extend(sub.flags)
        return None
    new_input = pred.get("input", _MISSING)
    name = new_input if isinstance(new_input, str) else None
    if name is None and new_input is not _MISSING:
        name = _select_alias_for(new_input, ctx)
    if name is None:
        ctx.flag(
            "PREDICATE_INPUT_NOT_REF",
            f"{at}.input",
            "the input is no reference and matches no select item",
        )
        return None
    ctx.flags.extend(sub.flags)
    return {
        "entity": pred.get("entity"),
        "having": [{"field": name, "op": pred["op"], "value": pred["value"]}],
        "over": pred["over"],
    }


# --- Where -------------------------------------------------------------------------------


def _where_item(item: Any, path: str, ctx: _Ctx) -> Any:
    if isinstance(item, dict) and "child" in item:
        return _child_group(item, path, ctx)
    if isinstance(item, dict) and "entity" in item:
        return _entity_filter(item, path, ctx)
    return _condition(item, path, ctx)


def _conditions(items: Any, path: str, ctx: _Ctx) -> tuple[Any, bool]:
    """A list of conditions translated; the bool says every one translated."""
    if not isinstance(items, list) or not items:
        ctx.flag("INVALID_V1_SHAPE", path, "a non-empty list of conditions")
        return items, False
    before = len(ctx.flags)
    out = [_condition(item, f"{path}[{index}]", ctx) for index, item in enumerate(items)]
    return out, not ctx.kept_since(before)


def _child_group(item: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    child, match = item.get("child"), item.get("match")
    if (
        set(item) - {"child", "match", "where"}
        or not isinstance(child, str)
        or not child.strip()
        or match not in ("any", "none")
    ):
        ctx.flag("INVALID_V1_SHAPE", path, "a child group is {child, match: any|none, where}")
        return item
    conditions, ok = _conditions(item.get("where"), f"{path}.where", ctx)
    return {"entity": child.strip(), match: conditions} if ok else item


def _entity_filter(item: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    """A current entity filter, passed through with its conditions in words."""
    unknown = sorted(set(item) - {"entity", "any", "none", "having", "over"})
    if unknown:
        ctx.flag("INVALID_V1_SHAPE", path, f"an entity filter with keys {unknown}")
        return item
    out: dict[str, Any] = {}
    for key, value in item.items():
        if key in ("any", "none", "having"):
            new, ok = _conditions(value, f"{path}.{key}", ctx)
            out[key] = new if ok else value
        else:
            out[key] = value
    return out


# --- The call ----------------------------------------------------------------------------


def translate_query(query: Any) -> Result:
    """Translate one call in the previous shape into the current call."""
    ctx = _Ctx()
    if not isinstance(query, dict):
        ctx.flag("INVALID_V1_SHAPE", "", "a query must be an object")
        return Result(query=query, flags=ctx.flags)
    query = _respelled(query, "", ctx)
    call: dict[str, Any] = {}

    # Order matters: time sets the grain predicates read; select sets the aliases filters read.
    time_value = _translate_time(query["time"], "time", ctx) if "time" in query else _MISSING
    select_value: Any = _MISSING
    lifted: list[Any] = []
    raw_select = query.get("select")
    if isinstance(raw_select, list):
        select_value, lifted = _translate_select(raw_select, query.get("group_by"), "select", ctx)
    elif raw_select is not None:
        ctx.flag("INVALID_V1_SHAPE", "select", "select must be a list")
        select_value = raw_select
    group_by_value: Any = _MISSING
    if "group_by" in query:
        group_by_value = _translate_group_by(query["group_by"], "group_by", ctx)
    if lifted:
        base = group_by_value if isinstance(group_by_value, list) else []
        group_by_value = [*base, *lifted]

    where_value: Any = _MISSING
    raw_where = query.get("where")
    if isinstance(raw_where, list):
        where_value = [
            _where_item(item, f"where[{index}]", ctx) for index, item in enumerate(raw_where)
        ]
    elif raw_where is not None:
        ctx.flag("INVALID_V1_SHAPE", "where", "where must be a list")
        where_value = raw_where

    having: Any = []
    if "having" in query:
        if isinstance(query["having"], list):
            having = [
                _condition(item, f"having[{index}]", ctx)
                for index, item in enumerate(query["having"])
            ]
        else:
            ctx.flag("INVALID_V1_SHAPE", "having", "having must be a list")
            having = query["having"]
    entity_filters: list[Any] = []
    kept_metric_filters: list[Any] = []
    if query.get("metric_filters") not in (None, []):
        more_having, entity_filters, kept_metric_filters = _metric_filters(
            query["metric_filters"], "metric_filters", ctx
        )
        if isinstance(having, list):
            having.extend(more_having)
    if entity_filters:
        where_value = [*(where_value if isinstance(where_value, list) else []), *entity_filters]

    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not _MISSING:
            out[key] = value

    for key, value in query.items():
        if key == "select":
            put("select", select_value)
            if "group_by" not in query:
                put("group_by", group_by_value)
        elif key == "group_by":
            put("group_by", group_by_value)
        elif key == "where":
            put("where", where_value)
        elif key == "metric_filters":
            if "where" not in query:
                put("where", where_value)
            if having and "having" not in query:
                out["having"] = having
            if kept_metric_filters:
                out["metric_filters"] = kept_metric_filters if isinstance(value, list) else value
        elif key == "having":
            out["having"] = having
        elif key == "time":
            put("time", time_value)
        elif key == "order_by":
            out[key] = _translate_order_by(value, query.get("time"), "order_by", ctx)
        elif key == "route_decisions":
            routes = _translate_routes(value, "route_decisions", ctx)
            if routes is None:
                out[key] = value
            elif routes:
                out["routes"] = routes
        elif key == "temporal_role_overrides":
            if value not in (None, {}):
                ctx.flag("NEEDS_PACKAGE_METRIC", key, "pin the clock with a package metric instead")
                out[key] = value
        elif key == "policy_context":
            if not isinstance(value, dict):
                if value is not None:
                    ctx.flag("INVALID_V1_SHAPE", key, "policy_context is not an object")
                    out[key] = value
                continue
            for name, item in value.items():
                if name != "now":
                    call.setdefault("context", {})[name] = item
                elif item is not None and not (isinstance(item, str) and not item.strip()):
                    call["now"] = item
        elif key in _CALL_KEYS:
            call[key] = value
        elif key == "explain":
            if value:
                ctx.flag("EXPLAIN_DROPPED", key, "explain: true left the body")
        elif key == "export":
            if value not in (False, None):
                ctx.flag("EXPORT_RETIRED", key, "export has no current form")
                out[key] = value
        elif key == "limit":
            if value is not None:
                if value == 0 and not isinstance(value, bool):
                    ctx.flag("LIMIT_ZERO", key, "limit 0 is refused")
                out[key] = value
        elif key in ("observation_scope", "routes"):
            if value is not None:
                out[key] = value
        elif key != "version":
            if not str(key).startswith("_"):
                ctx.flag("UNKNOWN_KEY", str(key), f"top-level key {key!r} is of neither shape")
            out[key] = value
    if "select" not in query and "group_by" not in query:
        put("group_by", group_by_value)
    if where_value is not _MISSING and "where" not in out:
        out["where"] = where_value
    if having and "having" not in out:
        out["having"] = having
    ordered = {key: call[key] for key in _CALL_ORDER if key in call}
    return Result(query=out, call=ordered, flags=ctx.flags)


# --- Output keys -------------------------------------------------------------------------


def rename_output_key(key: Any) -> Any:
    """``temporal_role.<role>__<grain>`` (grain day to year) becomes ``period``."""
    return "period" if isinstance(key, str) and _ROLE_GRAIN_KEY.match(key) else key


def _role_key_flag(key: Any, path: str, flags: list[dict[str, str]]) -> None:
    if isinstance(key, str) and _ROLE_KEY.match(key) and not _ROLE_GRAIN_KEY.match(key):
        flags.append(
            {"code": "ROLE_KEY_RAW", "path": path, "detail": f"{key} has no day to year grain"}
        )


def translate_rows(rows: Any, path: str = "expected_rows") -> tuple[Any, list[dict[str, str]]]:
    """Expected rows with their time key renamed: (rows, flags)."""
    flags: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return rows, flags
    out = []
    for index, row in enumerate(rows):
        at = f"{path}[{index}]"
        if not isinstance(row, dict):
            out.append(row)
            continue
        for key in row:
            _role_key_flag(key, f"{at}.{key}", flags)
        renamed = [rename_output_key(key) for key in row]
        if renamed.count("period") > 1:
            flags.append(
                {"code": "PERIOD_KEY_CONFLICT", "path": at, "detail": "two keys would be 'period'"}
            )
            out.append(row)
            continue
        out.append({rename_output_key(key): value for key, value in row.items()})
    return out, flags


def translate_columns(columns: Any, path: str = "columns") -> tuple[Any, list[dict[str, str]]]:
    """A ``columns`` list with its time column renamed: (columns, flags)."""
    flags: list[dict[str, str]] = []
    if not isinstance(columns, list):
        return columns, flags
    for index, name in enumerate(columns):
        _role_key_flag(name, f"{path}[{index}]", flags)
    renamed = [rename_output_key(name) for name in columns]
    if renamed.count("period") > 1:
        flags.append(
            {"code": "PERIOD_KEY_CONFLICT", "path": path, "detail": "two columns would be 'period'"}
        )
        return columns, flags
    return renamed, flags


# --- Package queries ---------------------------------------------------------------------

# A shape check reads no clock, so any instant does.
_CHECKED_AT = datetime(2026, 1, 1, tzinfo=UTC)
# Flags that say a query is of neither shape, rather than in the previous one.
_NEITHER_SHAPE = frozenset({"INVALID_V1_SHAPE", "UNKNOWN_KEY", "UNKNOWN_KIND"})


def refusal(query: Any) -> SemanticLayerError | None:
    """The current reader's refusal of ``query`` as a call (its shape only), or None."""
    try:
        read_call(query, now=_CHECKED_AT)
    except SemanticLayerError as exc:
        return exc
    return None


def previous_call(query: Any) -> Result | None:
    """The translation of a package query written in the previous call shape. None for a query
    the current reader reads once its retired spellings are rewritten (the upgrade rules for
    those spellings own it), and for one of neither shape."""
    respelled = _respelled(query, "", _Ctx())
    if not isinstance(query, dict) or refusal(respelled) is None:
        return None
    result = translate_query(query)
    kept = {flag["code"] for flag in result.kept_flags}
    if result.query == respelled and not result.call and kept <= _NEITHER_SHAPE:
        return None
    return result
