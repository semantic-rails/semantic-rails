"""The query's filter operators: one table, one meaning per word.

Every reader of a filter asks this module what its operator means: the query parser, the SQL
lowering, the planner's keep/exclude reading, the clarification complements, the policy
checks and the value probes. A filter is ``{field, op, value}`` with one of the words below.

"Unknown" is SQL NULL (or a value the dimension declares in ``unknown_values``, which the
lowering reads as NULL). ``not`` and the ``not_`` text words keep unknown rows; every other
comparison drops them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import SemanticLayerError


@dataclass(frozen=True)
class Operator:
    word: str
    # "scalar_or_list" (is, not), "scalar" (ranges), "text" (the text words), "boolean" (missing).
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
        # The text words: case-insensitive and literal; each not_ form keeps unknown rows.
        Operator("contains", "text", False, "not_contains", "containing"),
        Operator("starts_with", "text", False, "not_starts_with", "starting with"),
        Operator("ends_with", "text", False, "not_ends_with", "ending with"),
        Operator("not_contains", "text", True, "contains", "not containing"),
        Operator("not_starts_with", "text", True, "starts_with", "not starting with"),
        Operator("not_ends_with", "text", True, "ends_with", "not ending with"),
        # ``missing: true`` matches only unknown rows; ``missing: false`` only known ones.
        Operator("missing", "boolean", True, "missing", "with no"),
    )
}
WORDS: tuple[str, ...] = tuple(OPERATORS)
RANGE_WORDS = frozenset({"gt", "gte", "lt", "lte"})
TEXT_WORDS: tuple[str, ...] = tuple(w for w, row in OPERATORS.items() if row.value_shape == "text")
# The words that keep the rows whose value is unknown, and say how many they kept.
NEGATED_WORDS = frozenset(w for w, row in OPERATORS.items() if row.keeps_unknown) - {"missing"}
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
# Spellings with no alias: the refusal shows the word form instead (``_spelling_hint`` writes
# the caller's own value into the forms that take one).
_SPELLING_HINTS: dict[str, str] = {
    "is null": '{"op": "missing", "value": true}',
    "is not null": '{"op": "missing", "value": false}',
    "not null": '{"op": "missing", "value": false}',
}
LIKE_SPELLINGS = frozenset({"like", "ilike", "not like", "not ilike"})
# Each LIKE pattern a text word says: the text between its wildcards, with no other wildcard.
_LIKE_SHAPES = (
    ("contains", r"%([^%_\\]+)%"),
    ("starts_with", r"([^%_\\]+)%"),
    ("ends_with", r"%([^%_\\]+)"),
)
_NO_VALUE: Any = object()
# Engine-internal spellings: the lowering's own null tests and null-safe comparisons.
_INTERNAL_ALIASES: dict[str, tuple[str, Any]] = {
    "is null": ("missing", True),
    "is not null": ("missing", False),
    "is distinct from": ("not", None),
}


def _compact(raw: Any) -> str:
    return " ".join(str(raw if raw is not None else "").strip().lower().split())


def _spelling_hint(spelling: str, value: Any) -> str:
    """The word form of a spelling the table does not read, with the caller's value."""

    def shown(item: Any, placeholder: str) -> str:
        return placeholder if item is _NO_VALUE or item is None else json.dumps(item, default=str)

    spelling = spelling.replace("_", " ")
    if spelling in ALIASES:  # not_in
        return f'{{"op": "{ALIASES[spelling]}", "value": {shown(value, "<value>")}}}'
    if spelling == "is distinct from":
        if value is None:
            return _SPELLING_HINTS["is not null"]
        return f'{{"op": "not", "value": {shown(value, "<value>")}}}'
    if spelling in LIKE_SPELLINGS:
        return like_hint(spelling, value)
    if spelling == "between":
        low, high = (
            value if isinstance(value, (list, tuple)) and len(value) == 2 else [_NO_VALUE] * 2
        )
        return (
            f'two conditions, {{"op": "gte", "value": {shown(low, "<low>")}}} and '
            f'{{"op": "lte", "value": {shown(high, "<high>")}}}'
        )
    return _SPELLING_HINTS.get(spelling, "")


def like_hint(spelling: str, pattern: Any, *, operand: str = "value") -> str:
    """The text word a LIKE ``pattern`` says: 'X%' starts_with, '%X' ends_with, '%X%' contains,
    and their ``not_`` forms for a NOT LIKE; never contains for a prefix or a suffix. Any other
    pattern has no word, so the hint is the exact values. ``operand`` is ``right`` for a
    comparison expression."""
    negated = spelling.startswith("not ")
    for word, shape in _LIKE_SHAPES:
        found = re.fullmatch(shape, pattern) if isinstance(pattern, str) else None
        if found:
            shown = json.dumps(found.group(1))
            if operand == "right":
                shown = f'{{"kind": "literal", "value": {shown}}}'
            form = f'{{"op": "{"not_" if negated else ""}{word}", "{operand}": {shown}}}'
            keeps = " and keeps rows with no value" if negated else ""
            return f"{form}, which ignores case{keeps}"
    exact = (
        f'{{"kind": "{"not_in" if negated else "in"}", "expr": <the column>, "values": [...]}}'
        if operand == "right"
        else f'{{"op": "{"not" if negated else "is"}", "value": [...]}}'
    )
    if isinstance(pattern, str):
        inner = "_" in pattern or "%" in pattern.strip("%")
        return (
            f"the exact values, {exact}, which valid-values lists: no filter word says "
            f"{json.dumps(pattern)}" + (' ("_" or an inner "%")' if inner else "")
        )
    return (
        "starts_with for 'X%', ends_with for '%X' or contains for '%X%', each ignoring case "
        f"(their not_ forms for NOT LIKE), or the exact values, {exact}, for any other pattern"
    )


def invalid_op(raw: Any, path: str, value: Any = _NO_VALUE) -> SemanticLayerError:
    spelling = _compact(raw)
    hint = _spelling_hint(spelling, value)
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


def operator_word(raw: Any, *, path: str = "where.op", value: Any = _NO_VALUE) -> str:
    """The word for a filter op: a word or one of its SQL aliases, any case. ``value`` (the
    condition's) fills the refusal's hint."""
    spelling = _compact(raw)
    if spelling in OPERATORS:
        return spelling
    if spelling in ALIASES:
        return ALIASES[spelling]
    raise invalid_op(raw, path, value)


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
        raise _invalid_value(path, f"'{word}' takes text", "Pass the text to look for.")
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
    word = operator_word(spelling if internal else op, path=f"{path}.op", value=value)
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


def like_pattern(word: str, text: str) -> str:
    """The case-folded LIKE pattern for a text word (its ``not_`` form negates the match):
    literal text, ``%``/``_``/``\\`` escaped, with ``%`` on the side or sides the word leaves open."""
    escaped = (
        text.lower()
        .replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    shape = word.removeprefix("not_")
    return f"{'' if shape == 'starts_with' else '%'}{escaped}{'' if shape == 'ends_with' else '%'}"


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
        "starts_with": "starts with",
        "ends_with": "ends with",
        "not_contains": "does not contain",
        "not_starts_with": "does not start with",
        "not_ends_with": "does not end with",
    }[word]
    return f"{label} {text} {shown}"
