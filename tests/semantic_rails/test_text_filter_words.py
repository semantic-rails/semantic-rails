"""One LIKE rule everywhere: a LIKE, NOT LIKE or ILIKE is refused in `where`, in an entity
filter, in an inline condition and in a `comparison` expression, and its hint names the text
word for the pattern's shape ('X%' starts_with, '%X' ends_with, '%X%' contains, the `not_` forms
for NOT LIKE), never `contains` for a prefix or a suffix. The hinted call is valid.

A text word on a result (`having`) is refused, an arithmetic op the engine does not read is
refused at its path with the closest op, and plan never drafts `contains` for a prefix or a
suffix."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from semantic_rails import operators
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

NOW = "2017-09-04T12:00:00Z"
ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
COLUMN = {"kind": "column", "column": "store_name", "entity": "entity.jaffle_store"}
IGNORES_CASE = ", which ignores case."
KEEPS_UNKNOWN = ", which ignores case and keeps rows with no value."


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_snapshot(load_package_snapshot(path))
    yield opened(runtime)
    runtime.close()


def _error(runtime: Runtime, call: dict[str, Any]) -> dict[str, Any]:
    result = runtime.validate(call, now=NOW)
    assert result["ok"] is False, result
    return result["errors"][0]


def _hint(error: dict[str, Any]) -> str:
    (hint,) = error["recovery_hints"]
    return str(hint["message"])


def _where(op: str, value: Any) -> dict[str, Any]:
    return {"select": [ORDERS], "where": [{"field": STORE, "op": op, "value": value}]}


def _entity_filter(op: str, value: Any) -> dict[str, Any]:
    condition = {"field": "dimension.jaffle_item_product_name", "op": op, "value": value}
    return {"select": [ORDERS], "where": [{"entity": "entity.jaffle_item", "any": [condition]}]}


def _aggregate_filter(op: str, value: Any) -> dict[str, Any]:
    condition = {"field": STORE, "op": op, "value": value}
    expression = {"kind": "aggregate", "measure": ORDERS, "filter": {"all": [condition]}}
    return {"select": [{"expression": expression, "as": "n"}]}


def _scoped(op: str, value: Any) -> dict[str, Any]:
    condition = {"field": STORE, "op": op, "value": value}
    expression = {"kind": "scoped_aggregate", "measure": ORDERS, "where": [condition]}
    return {"select": [{"expression": expression, "as": "n"}]}


def _aggregate_if(op: str, value: Any) -> dict[str, Any]:
    condition = {
        "kind": "comparison",
        "op": op,
        "left": COLUMN,
        "right": {"kind": "literal", "value": value},
    }
    expression = {"kind": "aggregate_if", "aggregation": "count", "condition": condition}
    return {"select": [{"expression": expression, "as": "n"}]}


# Each surface: the call, the path of the op, and the hinted fragment for a word and text.
SURFACES = {
    "where": (_where, "where[0].op", '{{"op": "{word}", "value": "{text}"}}'),
    "entity-filter": (_entity_filter, "where[0].any[0].op", '{{"op": "{word}", "value": "{text}"}}'),
    "aggregate-filter": (
        _aggregate_filter,
        "select[0].expression.filter.all[0].op",
        '{{"op": "{word}", "value": "{text}"}}',
    ),
    "scoped-where": (_scoped, "select[0].expression.where[0].op", '{{"op": "{word}", "value": "{text}"}}'),
    "aggregate-if": (
        _aggregate_if,
        "select[0].expression.condition.op",
        '{{"op": "{word}", "right": {{"kind": "literal", "value": "{text}"}}}}',
    ),
}  # fmt: skip
SHAPES = [
    ("LIKE", "Phil%", "starts_with", "Phil", IGNORES_CASE),
    ("like", "%lyn", "ends_with", "lyn", IGNORES_CASE),
    ("ILIKE", "%cisco%", "contains", "cisco", IGNORES_CASE),
    ("Not Like", "Phil%", "not_starts_with", "Phil", KEEPS_UNKNOWN),
    ("NOT  ILIKE", "%lyn", "not_ends_with", "lyn", KEEPS_UNKNOWN),
    ("not like", "%cisco%", "not_contains", "cisco", KEEPS_UNKNOWN),
]


def _with_op(call: dict[str, Any], path: str, op: str) -> dict[str, Any]:
    """``call`` with the op at ``path`` (``a[0].b.op``) replaced."""
    fixed = copy.deepcopy(call)
    node: Any = fixed
    for part in path.removesuffix(".op").replace("]", "").replace("[", ".").split("."):
        node = node[int(part)] if part.isdigit() else node[part]
    node["op"] = op
    return fixed


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(("op", "pattern", "word", "text", "says"), SHAPES)
def test_like_is_refused_everywhere_with_the_word_for_its_pattern(
    runtime, surface, op, pattern, word, text, says
):
    build, path, fragment = SURFACES[surface]
    call = build(op, pattern)
    error = _error(runtime, call)
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == path
    hint = _hint(error)
    assert hint == f"Write {fragment.format(word=word, text=text)}{says}"
    if word.removeprefix("not_") != "contains":
        assert "contains" not in hint
    # The hinted word, with the hinted text, is a valid call on the same surface.
    if surface == "aggregate-if":
        fixed = build(word, text)
    else:
        fixed = _with_op(build(op, text), path, word)
    assert runtime.validate(fixed, now=NOW)["ok"] is True, word


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    ("op", "pattern"), [("LIKE", "Phil_delphia"), ("ilike", "%a%n%"), ("not like", "B%n")]
)
def test_a_like_pattern_no_word_says_hints_the_exact_values(runtime, surface, op, pattern):
    build, path, _ = SURFACES[surface]
    error = _error(runtime, build(op, pattern))
    assert error["details"]["path"] == path
    hint = _hint(error)
    exact = "not" if op.lower().startswith("not") else "is"
    shown = (
        f'{{"kind": "{"not_in" if exact == "not" else "in"}", "expr": <the column>, "values": [...]}}'
        if surface == "aggregate-if"
        else f'{{"op": "{exact}", "value": [...]}}'
    )
    assert hint == (
        f"Write the exact values, {shown}, which valid-values lists: no filter word says "
        f'{json.dumps(pattern)} ("_" or an inner "%").'
    )
    for word in ("contains", "starts_with", "ends_with"):
        assert word not in hint


def test_a_like_with_no_wildcard_hints_the_exact_values_without_a_reason(runtime):
    hint = _hint(_error(runtime, _where("LIKE", "Brooklyn")))
    assert hint == (
        'Write the exact values, {"op": "is", "value": [...]}, which valid-values lists: no '
        'filter word says "Brooklyn".'
    )


def test_a_like_with_no_pattern_names_every_shape(runtime):
    expression = {
        "kind": "aggregate_if",
        "aggregation": "count",
        "condition": {"kind": "comparison", "op": "LIKE", "left": COLUMN, "right": COLUMN},
    }
    hint = _hint(_error(runtime, {"select": [{"expression": expression, "as": "n"}]}))
    assert "starts_with for 'X%', ends_with for '%X' or contains for '%X%'" in hint


def test_like_in_a_binary_expression_is_refused_the_same_way(runtime):
    expression = {"kind": "binary", "op": "LIKE", "left": ORDERS, "right": ORDERS}
    error = _error(runtime, {"select": [{"expression": expression, "as": "n"}]})
    assert error["details"]["path"] == "select[0].expression.op"
    assert error["recovery_hints"][0]["code"] == "USE_FILTER_WORD"


def test_a_comparison_text_word_needs_a_text_literal(runtime):
    expression = _aggregate_if("starts_with", "Phil")["select"][0]["expression"]
    expression["condition"]["right"] = {"kind": "literal", "value": 7}
    error = _error(runtime, {"select": [{"expression": expression, "as": "n"}]})
    assert error["details"]["path"] == "select[0].expression.condition.right"


@pytest.mark.parametrize(
    ("op", "closest"),
    [("div", ["divide"]), ("mul", ["multiply"]), ("SUB", ["subtract"]), ("mod", [])],
)
def test_an_unknown_arithmetic_op_is_refused_at_its_path_with_the_closest(runtime, op, closest):
    inner = {"kind": "arithmetic", "op": op, "left": ORDERS, "right": ORDERS}
    expression = {"kind": "arithmetic", "op": "add", "left": ORDERS, "right": inner}
    error = _error(runtime, {"select": [{"expression": expression, "as": "n"}]})
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "select[0].expression.right.op"
    assert error["details"]["allowed"] == ["add", "subtract", "multiply", "divide"]
    assert error["details"]["closest_matches"] == closest
    hint = _hint(error)
    if closest:
        assert hint.startswith(f"Did you mean '{closest[0]}'?")
    assert "add, subtract, multiply or divide" in hint


@pytest.mark.parametrize("op", ["+", "minus", "Times", "DIVIDE"])
def test_the_arithmetic_spellings_the_engine_reads_stay_valid(runtime, op):
    expression = {"kind": "arithmetic", "op": op, "left": ORDERS, "right": ORDERS}
    assert runtime.validate({"select": [{"expression": expression, "as": "n"}]}, now=NOW)["ok"]


def test_every_text_word_describes_itself():
    assert [operators.describe("Store", word, "Ph") for word in operators.TEXT_WORDS] == [
        "Store contains Ph",
        "Store starts with Ph",
        "Store ends with Ph",
        "Store does not contain Ph",
        "Store does not start with Ph",
        "Store does not end with Ph",
    ]


@pytest.mark.parametrize("op", ["contains", "starts_with", "not_ends_with"])
def test_a_text_word_on_a_result_is_refused(runtime, op):
    call = {
        "select": [{"expression": ORDERS, "as": "orders"}],
        "group_by": [STORE],
        "having": [{"field": "orders", "op": op, "value": "1"}],
    }
    error = _error(runtime, call)
    assert error["details"]["path"] == "having[0].op"


@pytest.mark.parametrize(
    "intent",
    [
        "order count for customers whose name starts with K",
        "order count for customers beginning with K",
        "items sold for products ending in tea",
        "revenue for stores with a name ending with n",
    ],
)
def test_plan_never_drafts_contains_for_a_prefix_or_suffix(runtime, intent):
    from semantic_rails.planner.plan import plan_payload

    payload = plan_payload(runtime, intent=intent, detail="full")
    drafted = json.dumps(payload, default=str)
    assert '"op": "contains"' not in drafted
    assert payload["status"] != "ok"


@pytest.mark.parametrize(
    ("field", "data_type"),
    [
        ("dimension.jaffle_order_customer_order_number", "integer"),
        ("dimension.jaffle_store_tax_rate", "number"),
        ("dimension.jaffle_order_is_large_order", "boolean"),
        ("dimension.jaffle_order_ordered_at", "timestamp"),
    ],
)
def test_a_text_word_on_a_dimension_that_is_not_text_is_refused(runtime, field, data_type):
    error = _error(
        runtime, {"select": [ORDERS], "where": [{"field": field, "op": "contains", "value": "1"}]}
    )
    assert error["details"]["path"] == "where[0].op"
    assert data_type in error["message"]


@pytest.mark.parametrize("data_type", ["id", "integer", "number", "date", "timestamp", "boolean"])
def test_each_type_that_is_not_text_names_the_words_that_compare_it(data_type):
    from semantic_rails.errors import SemanticLayerError

    with pytest.raises(SemanticLayerError) as caught:
        operators.check_text_operand("starts_with", data_type, "where[0].op", "dimension.x")
    hint = caught.value.details["recovery_hints"][0]["message"]
    assert not any(word in hint for word in operators.TEXT_WORDS)
    operators.check_text_operand("is", data_type, "where[0].op", "dimension.x")
    operators.check_text_operand("starts_with", "string", "where[0].op", "dimension.x")
    operators.check_text_operand("starts_with", "", "where[0].op", "dimension.x")


def test_the_refused_letters_are_every_letter_whose_lowercase_changes_length():
    """Each letter whose lowercase is another number of code points or UTF-8 bytes, and the
    lowercase of each such capital, is refused, with Greek sigma, sharp s and dotless i; the
    common letters k, a with ring and omega stay readable (their odd capitals are refused)."""
    import sys

    unstable = set()
    for point in range(sys.maxunicode + 1):
        letter = chr(point)
        lower = letter.lower()
        if lower != letter and (len(lower) != 1 or len(lower.encode()) != len(letter.encode())):
            unstable.add(letter)
            if len(lower) == 1 and lower not in "kåω":
                unstable.add(lower)
    expected = unstable | set("Σσςßı̇")
    refused = set(operators.UNSTABLE_CASE_LETTERS)
    assert expected <= refused
    # The list is Unicode 16's; an older Python's tables have no case pair for the rest.
    assert all(letter.lower() == letter.upper() == letter for letter in refused - expected)


@pytest.mark.parametrize(
    ("warehouse", "column", "found"),
    [
        ("duckdb", "store_name", True),
        ("duckdb", "STORE_NAME", True),  # unquoted: every warehouse but ClickHouse folds it
        ("snowflake", "Store_Name", True),
        ("clickhouse", "STORE_NAME", False),  # ClickHouse names are case-sensitive
        ("clickhouse", "store_name", True),
        ("duckdb", "store_namé", False),  # quoted: only itself, never a Unicode casefold
    ],
)
def test_a_column_is_named_by_the_warehouses_identifier_rules(warehouse, column, found):
    from dataclasses import replace

    from semantic_rails.compiler_parts.paths import dimensions_over_column
    from semantic_rails.config import load_package_config

    config = load_package_config("configs/semantic_rails/jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    dims = dimensions_over_column("entity.jaffle_store", column, config)
    assert [dim.id for dim in dims] == (["dimension.jaffle_store_name"] if found else [])


# --- Parity by refusal: letters some supported warehouse lowers differently ----------------

PARITY_REFUSED = {
    "Ⲁ": "Coptic capital alfa (ClickHouse 24.8 leaves U+2000-U+2FFF as it is)",
    "ⲁ": "Coptic small alfa",
    "Ⰰ": "Glagolitic capital az",
    "Ⱡ": "Latin capital L with double bar (Latin Extended-C)",
    "ⱡ": "Latin small l with double bar",
    "Ⅰ": "Roman numeral one",
    "Ⓐ": "circled capital A",
    "ⴀ": "Georgian small an (Nuskhuri)",
    "Ⴀ": "Georgian capital an, whose small letter is in U+2000-U+2FFF",
    "Ꟍ": "Latin capital S with diagonal stroke (case pair new in Unicode 16)",
    "ꟍ": "Latin small s with diagonal stroke",
    "Ᲊ": "Cyrillic capital TJE (Unicode 16)",
    "\U00010570": "Vithkuqi capital A (Unicode 14)",
    "\U00010d50": "Garay capital A (Unicode 16)",
}
READABLE = ["a", "É", "é", "ω", "Ω", "k", "å", "я", "Я", "ა", "東京", "10%", "N/A", "’", "€"]


@pytest.mark.parametrize("letter", PARITY_REFUSED)
def test_a_letter_some_warehouse_lowers_differently_is_refused_everywhere(letter):
    from semantic_rails.errors import SemanticLayerError

    with pytest.raises(SemanticLayerError) as caught:
        operators.check_text_letters(f"x{letter}y", "where[0].value")
    assert caught.value.details["path"] == "where[0].value"


@pytest.mark.parametrize("text", READABLE)
def test_text_every_warehouse_lowers_alike_is_read(text):
    operators.check_text_letters(text, "where[0].value")


def test_the_refused_letters_follow_the_pinned_rule():
    """Generated from unicodedata: every cased letter in U+2000-U+2FFF, or whose lower or upper
    case is there (ClickHouse 24.8's lowerUTF8 copies that range as it is), is refused; and
    every refused letter is cased here, or unassigned in this Python's older Unicode tables (a
    case pair added after Unicode 13.0), or the combining dot above."""
    import sys
    import unicodedata

    refused = operators.UNSTABLE_CASE_LETTERS

    def in_range(text: str) -> bool:
        return len(text) == 1 and 0x2000 <= ord(text) <= 0x2FFF

    for point in range(sys.maxunicode + 1):
        letter = chr(point)
        if 0xD800 <= point <= 0xDFFF or (letter.lower() == letter == letter.upper()):
            continue
        if in_range(letter) or in_range(letter.lower()) or in_range(letter.upper()):
            assert letter in refused, f"U+{point:04X}"
    for letter in refused:
        cased = letter.lower() != letter or letter.upper() != letter
        assert cased or unicodedata.category(letter) == "Cn" or letter == "̇", letter
