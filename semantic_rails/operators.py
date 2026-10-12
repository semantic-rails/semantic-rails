"""The query's filter operators: one table, one meaning per word.

Every reader of a filter asks this module what its operator means: the query parser, the SQL
lowering, the planner's keep/exclude reading, the clarification complements, the policy
checks and the value probes. A filter is ``{field, op, value}`` with one of the words below.

"Unknown" is SQL NULL (or a value the dimension declares in ``unknown_values``, which the
lowering reads as NULL). ``not`` keeps unknown rows; every other comparison drops them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import SemanticLayerError


@dataclass(frozen=True)
class Operator:
    word: str
    # "scalar_or_list" (is, not), "scalar" (ranges), "text" (contains), "boolean" (missing).
    value_shape: str
    # Whether a row whose value is unknown matches.
    keeps_unknown: bool
    # The exact complement (same rows, unknowns included), or "" when there is none.
    complement: str
    phrase: str


OPERATORS: dict[str, Operator] = {
    row.word: row
    for row in (
        Operator("is", "scalar_or_list", False, "not", "only"),
        Operator("not", "scalar_or_list", True, "is", "excluding"),
        Operator("gt", "scalar", False, "", "over"),
        Operator("gte", "scalar", False, "", "at least"),
        Operator("lt", "scalar", False, "", "under"),
        Operator("lte", "scalar", False, "", "at most"),
        Operator("contains", "text", False, "", "containing"),
        # ``missing: true`` matches only unknown rows; ``missing: false`` only known ones.
        Operator("missing", "boolean", True, "missing", "with no"),
    )
}
WORDS: tuple[str, ...] = tuple(OPERATORS)
RANGE_WORDS = frozenset({"gt", "gte", "lt", "lte"})
# A comparison of two known values (an aggregate against a computed threshold), in SQL.
COMPARISON_SQL = {"is": "=", "not": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

# SQL spellings a caller may send; each takes its word's meaning (``!=`` keeps unknowns).
ALIASES: dict[str, str] = {
    "=": "is",
    "==": "is",
    "eq": "is",
    "in": "is",
    "!=": "not",
    "<>": "not",
    "neq": "not",
    "not in": "not",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
}
# Spellings with no alias: the refusal shows the word form instead.
_SPELLING_HINTS: dict[str, str] = {
    "is null": '{"op": "missing", "value": true}',
    "is_null": '{"op": "missing", "value": true}',
    "is not null": '{"op": "missing", "value": false}',
    "is_not_null": '{"op": "missing", "value": false}',
    "not_null": '{"op": "missing", "value": false}',
    "is distinct from": '{"op": "not", "value": <value>}',
    "like": '{"op": "contains", "value": "<text>"}',
    "ilike": '{"op": "contains", "value": "<text>"}',
    "not like": "a 'not' with the exact values, or an entity filter",
    "between": 'two conditions, {"op": "gte", ...} and {"op": "lt", ...}',
}
# Engine-internal spellings: the lowering's own null tests and null-safe comparisons.
_INTERNAL_ALIASES: dict[str, tuple[str, Any]] = {
    "is null": ("missing", True),
    "is not null": ("missing", False),
    "is distinct from": ("not", None),
}


def _compact(raw: Any) -> str:
    return " ".join(str(raw if raw is not None else "").strip().lower().split())


def invalid_op(raw: Any, path: str) -> SemanticLayerError:
    spelling = _compact(raw)
    hint = _SPELLING_HINTS.get(spelling)
    message = f"{path} must be one of {list(WORDS)}" + (f"; write {hint}" if hint else "")
    return SemanticLayerError(
        "INVALID_QUERY",
        message,
        details={
            "path": path,
            "received_value": raw,
            "allowed": list(WORDS),
            "recovery_hints": [
                {
                    "code": "USE_FILTER_WORD",
                    "message": (
                        f"Write {hint}." if hint else f"Filters use the words {list(WORDS)}."
                    ),
                }
            ],
        },
    )


def operator_word(raw: Any, *, path: str = "where.op") -> str:
    """The word for a filter op: a word or one of its SQL aliases, any case."""
    spelling = _compact(raw)
    if spelling in OPERATORS:
        return spelling
    if spelling in ALIASES:
        return ALIASES[spelling]
    raise invalid_op(raw, path)


def word_of(raw: Any) -> str:
    """The word for an op the parser already read (a word or an alias); other text as it is."""
    spelling = _compact(raw)
    return ALIASES.get(spelling, spelling)


def _invalid_value(path: str, message: str, hint: str) -> SemanticLayerError:
    return SemanticLayerError(
        "INVALID_QUERY",
        f"{path}: {message}",
        details={
            "path": path,
            "recovery_hints": [{"code": "USE_FILTER_VALUE_SHAPE", "message": hint}],
        },
    )


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and value is not None


def check_value(word: str, value: Any, *, path: str = "where.value") -> Any:
    """The canonical value for ``word``: refuses null, empty lists and wrong shapes.

    A one-element list is the same value as its scalar for ``is``/``not``.
    """
    shape = OPERATORS[word].value_shape
    if shape == "boolean":
        if not isinstance(value, bool):
            raise _invalid_value(
                path,
                "'missing' takes true or false",
                "missing: true keeps rows with no value; missing: false keeps rows with one.",
            )
        return value
    if value is None:
        raise _invalid_value(
            path,
            f"'{word}' cannot compare against null",
            'To keep rows with no value write {"op": "missing", "value": true}.',
        )
    if isinstance(value, (list, tuple)):
        if shape != "scalar_or_list":
            raise _invalid_value(
                path,
                f"'{word}' compares against one value, got a list",
                "Pass one value, or use 'is' / 'not' with a list.",
            )
        items = list(value)
        if not items:
            raise _invalid_value(
                path,
                "an empty list matches nothing",
                'Name at least one value, or write {"op": "missing", "value": true}.',
            )
        for index, item in enumerate(items):
            if item is None:
                raise _invalid_value(
                    f"{path}[{index}]",
                    "a list holds no null",
                    'For rows with no value add {"op": "missing", "value": true}.',
                )
            if not _is_scalar(item):
                raise _invalid_value(
                    f"{path}[{index}]", "list items are strings, numbers or booleans", ""
                )
        return items[0] if len(items) == 1 else items
    if not _is_scalar(value):
        raise _invalid_value(
            path,
            "a filter value is a string, number or boolean",
            "Compute a threshold in its own query and pass the number here.",
        )
    if shape == "text" and not isinstance(value, str):
        raise _invalid_value(path, "'contains' takes text", "Pass the text to look for.")
    return value


def normalize_condition(
    op: Any, value: Any, *, path: str, internal: bool = False
) -> tuple[str, Any]:
    """``(word, value)`` for a condition. ``internal`` also reads the engine's null tests."""
    spelling = _compact(op)
    if internal and spelling in _INTERNAL_ALIASES:
        word, fixed = _INTERNAL_ALIASES[spelling]
        if word == "missing":
            return word, fixed
        spelling = word
    if internal and value is None and spelling in {"=", "==", "is"}:
        return "missing", True
    if internal and value is None and spelling in {"!=", "<>", "not"}:
        return "missing", False
    if internal and spelling == "is" and isinstance(value, bool):
        return "is", value
    word = operator_word(spelling if internal else op, path=f"{path}.op")
    if internal and word in {"is", "not"} and isinstance(value, (list, tuple)) and not value:
        # Engine-built membership over no values: vacuously false (is) or true (not).
        return word, []
    return word, check_value(word, value, path=f"{path}.value")


def values_of(word: str, value: Any) -> list[Any]:
    """The values an ``is``/``not`` condition names, as a list."""
    if word not in {"is", "not"}:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def rejects_unknown(word: str, value: Any) -> bool:
    """Whether a row whose value is unknown fails the condition."""
    if word == "missing":
        return value is False
    return not OPERATORS[word].keeps_unknown


def exact_complement(word: str, value: Any) -> tuple[str, Any] | None:
    """The condition matching exactly the other rows, unknowns included; None for ranges."""
    if word == "missing":
        return "missing", not value
    complement = OPERATORS[word].complement
    return (complement, value) if complement else None


def pinned_value(op: str, value: Any) -> tuple[bool, Any]:
    """``(True, v)`` when the condition pins the field to the single value ``v``."""
    word = word_of(op)
    if word == "is" and not isinstance(value, (list, tuple)):
        return True, value
    if word == "is" and isinstance(value, (list, tuple)) and len(value) == 1:
        return True, value[0]
    return False, None


# The word a known value fails ``word`` by: ``x op v`` is false exactly where this is true.
_NEGATION = {"is": "not", "not": "is", "gt": "lte", "gte": "lt", "lt": "gte", "lte": "gt"}


def negation(word: str) -> str:
    """The comparison a known value meets when it fails ``word`` (a threshold's other side)."""
    return _NEGATION[word]


def holds(word: str, value: Any, known: Any) -> bool:
    """Whether the known number ``known`` meets the comparison; numbers compare only with
    numbers, and an engine-built membership over no values is false (``is``) or true (``not``)."""

    def number(item: Any) -> bool:
        return isinstance(item, (int, float)) and not isinstance(item, bool)

    if word in {"is", "not"}:
        values = values_of(word, value)
        if not values:
            return word == "not"
        if not all(number(item) for item in values):
            return False
        return (known in values) == (word == "is")
    if word in RANGE_WORDS and number(value):
        return {
            "gt": known > value,
            "gte": known >= value,
            "lt": known < value,
            "lte": known <= value,
        }[word]
    return False


def keeps_values(word: str) -> bool:
    """Whether the condition keeps only the values it names (the planner's "only")."""
    return word == "is"


def excludes_values(word: str) -> bool:
    """Whether the condition leaves out the values it names (the planner's "excluding")."""
    return word == "not"


LIKE_ESCAPE = "\\"


def contains_pattern(text: str) -> str:
    """The case-folded LIKE pattern for ``contains``: literal text, ``%``/``_``/``\\`` escaped."""
    escaped = (
        text.lower()
        .replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def spec_matches(word: str, value: Any, spec: dict[str, Any], *, path: str) -> bool:
    """Whether a condition satisfies a policy's required filter spec.

    A spec names a field and optionally an op (default ``is``) and a value; the condition must
    use the same word and, when the spec names one, the same values.
    """
    spec_word, spec_value = normalize_condition(
        spec.get("op", "is"), spec.get("value", True), path=path, internal=True
    )
    if word != spec_word:
        return False
    if "value" not in spec:
        return True
    if word in {"is", "not"}:
        return sorted(map(str, values_of(word, value))) == sorted(
            map(str, values_of(spec_word, spec_value))
        )
    return bool(value == spec_value)


def describe(label: str, word: str, value: Any) -> str:
    """A condition in words, for answers and clarifications."""
    if word == "missing":
        return f"{label} is unknown" if value else f"{label} is known"
    shown = ", ".join(map(str, values_of(word, value))) if word in {"is", "not"} else value
    text = {
        "is": "is",
        "not": "is not",
        "gt": "is over",
        "gte": "is at least",
        "lt": "is under",
        "lte": "is at most",
        "contains": "contains",
    }[word]
    return f"{label} {text} {shown}"
