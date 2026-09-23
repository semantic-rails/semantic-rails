"""Unit tests for mf2sr/filter_parser.py.

Focused on the BETWEEN extension; the older regex arms (boolean dim,
NOT dim, IN list, IS NOT NULL, metric predicate) have indirect coverage
through tests/mf2sr/test_translate.py end-to-end. These tests pin the
new BETWEEN / NOT BETWEEN translations and confirm we don't regress on
the existing arms.
"""

from __future__ import annotations

import pytest

from mf2sr.filter_parser import filter_clauses, parse_filter


def test_parse_between_numeric_bounds():
    result = parse_filter("{{ Dimension('order__total_cents') }} BETWEEN 100 AND 500")
    assert result == {
        "kind": "between",
        "expr": {"kind": "column", "column": "total_cents"},
        "low": {"kind": "literal", "value": 100},
        "high": {"kind": "literal", "value": 500},
    }


def test_parse_not_between_numeric_bounds():
    result = parse_filter("{{ Dimension('order__total_cents') }} NOT BETWEEN 0 AND 50")
    assert result == {
        "kind": "not_between",
        "expr": {"kind": "column", "column": "total_cents"},
        "low": {"kind": "literal", "value": 0},
        "high": {"kind": "literal", "value": 50},
    }


def test_parse_between_quoted_string_bounds():
    """Date / string ranges via single-quoted SQL literals."""
    result = parse_filter(
        "{{ Dimension('order__placed_at') }} BETWEEN '2024-01-01' AND '2024-12-31'"
    )
    assert result == {
        "kind": "between",
        "expr": {"kind": "column", "column": "placed_at"},
        "low": {"kind": "literal", "value": "2024-01-01"},
        "high": {"kind": "literal", "value": "2024-12-31"},
    }


def test_parse_between_float_bounds():
    result = parse_filter("{{ Dimension('product__weight_kg') }} BETWEEN 0.5 AND 2.5")
    assert result == {
        "kind": "between",
        "expr": {"kind": "column", "column": "weight_kg"},
        "low": {"kind": "literal", "value": 0.5},
        "high": {"kind": "literal", "value": 2.5},
    }


def test_parse_between_negative_lower_bound():
    result = parse_filter("{{ Dimension('metric__delta_pct') }} BETWEEN -10 AND 10")
    assert result is not None
    assert result["kind"] == "between"
    assert result["low"]["value"] == -10
    assert result["high"]["value"] == 10


def test_parse_between_case_insensitive_keyword():
    """MetricFlow YAML sometimes writes filters with lowercase SQL
    keywords. The regex must be case-insensitive."""
    result = parse_filter("{{ Dimension('x__y') }} between 1 and 5")
    assert result is not None
    assert result["kind"] == "between"


def test_parse_between_strips_entity_prefix_like_other_arms():
    """``Dimension('booking__nights')`` → column ``nights``, not
    ``booking__nights`` — matches the existing convention used by the
    boolean-dim and IN arms (the Semantic Rails planner re-binds via
    the entity declaration)."""
    result = parse_filter("{{ Dimension('booking__nights') }} BETWEEN 1 AND 7")
    assert result["expr"]["column"] == "nights"


# ---- regression: existing arms still match correctly ----


def test_existing_boolean_dim_still_parses():
    assert parse_filter("{{ Dimension('booking__is_instant') }}") == {
        "kind": "comparison",
        "op": "=",
        "left": {"kind": "column", "column": "is_instant"},
        "right": {"kind": "literal", "value": True},
    }


def test_existing_in_list_still_parses():
    result = parse_filter("{{ Dimension('user__home_state') }} IN ('CA', 'HI')")
    assert result == {
        "kind": "in",
        "expr": {"kind": "column", "column": "home_state"},
        "values": [
            {"kind": "literal", "value": "CA"},
            {"kind": "literal", "value": "HI"},
        ],
    }


def test_unrecognized_filter_returns_none():
    """Anything outside the supported shapes returns ``None`` so the
    translator can warn-and-skip rather than emit a wrong filter."""
    assert parse_filter("some random SQL that we don't parse") is None
    assert parse_filter("") is None
    assert parse_filter("   ") is None


DIMENSIONS: dict[str, str | None] = {
    "order__status": "dimension.shop_order_status",
    "order__is_first_order": "dimension.shop_order_is_first_order",
    "order__ordered_at": None,  # a time dimension
    "customer__country": "dimension.shop_customer_country",
}


def test_filter_clauses_name_dimensions_by_id():
    status = "dimension.shop_order_status"
    assert filter_clauses("{{ Dimension('order__status') }} IN ('a', 'b')", DIMENSIONS) == (
        [{"field": status, "op": "in", "value": ["a", "b"]}],
        "",
    )
    assert filter_clauses("NOT {{ Dimension('order__is_first_order') }}", DIMENSIONS) == (
        [{"field": "dimension.shop_order_is_first_order", "op": "=", "value": False}],
        "",
    )
    assert filter_clauses("{{ Dimension('order__status') }} BETWEEN 'a' AND 'm'", DIMENSIONS) == (
        [
            {"field": status, "op": ">=", "value": "a"},
            {"field": status, "op": "<=", "value": "m"},
        ],
        "",
    )


def test_filter_clauses_compare_and_exclude():
    status = "dimension.shop_order_status"
    assert filter_clauses("{{ Dimension('order__status') }} = 'it''s'", DIMENSIONS) == (
        [{"field": status, "op": "=", "value": "it's"}],
        "",
    )
    assert filter_clauses("{{ Dimension('order__status') }} <> 3", DIMENSIONS) == (
        [{"field": status, "op": "!=", "value": 3}],
        "",
    )
    assert filter_clauses("{{ Dimension('order__status') }} NOT IN ('a', 'b')", DIMENSIONS) == (
        [{"field": status, "op": "not in", "value": ["a", "b"]}],
        "",
    )


def test_in_lists_keep_quoted_commas_and_reject_anything_else():
    assert filter_clauses(
        "{{ Dimension('customer__country') }} IN ('Washington, D.C.', 'NL')", DIMENSIONS
    ) == (
        [
            {
                "field": "dimension.shop_customer_country",
                "op": "in",
                "value": ["Washington, D.C.", "NL"],
            }
        ],
        "",
    )
    assert parse_filter("{{ Dimension('order__status') }} IN ('a') OR (status IN ('b'))") is None
    assert parse_filter("{{ Dimension('order__status') }} IN ()") is None


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{{ Dimension('order__status') }} NOT BETWEEN 'a' AND 'm'", "NOT BETWEEN"),
        ("{{ Metric('orders', group_by=['order']) }} > 2", "metric predicate"),
        ("{{ Entity('order') }} IS NOT NULL", "tests an entity"),
        ("{{ Dimension('order__nope') }} IN ('x')", "`order__nope` is not a dimension"),
        ("{{ Dimension('status') }} IN ('x')", "`status` is not a dimension"),
        # MetricFlow names a dimension with one entity; a join path is entity_path=[...].
        ("{{ Dimension('order__customer__country') }} IN ('NL')", "is not a dimension"),
        (
            "{{ Dimension('customer__country', entity_path=['order']) }} IN ('NL')",
            "entity_path",
        ),
        (
            "{{ Dimension('order__ordered_at') }} BETWEEN '2024-01-01' AND '2024-02-01'",
            "time dimension `order__ordered_at`",
        ),
        ("{{ Dimension('order__status') }} LIKE 'x%'", "could not parse"),
    ],
)
def test_filter_clauses_say_why_a_filter_cannot_be_written(text, reason):
    clauses, problem = filter_clauses(text, DIMENSIONS)
    assert clauses == []
    assert reason in problem
