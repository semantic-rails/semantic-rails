"""Unit tests for the shared adapter helpers in db_parts.common."""

from __future__ import annotations

from semantic_rails.db_parts.common import (
    float_nullif_divisions,
    map_double_quoted_identifiers,
    option_or_env,
    rewrite_double_quoted_identifiers,
)


def test_rewrites_identifiers_to_backticks():
    sql = 'SELECT base.g1 AS "dimension.jaffle_product_type" FROM t ORDER BY "g1"'
    assert rewrite_double_quoted_identifiers(sql) == (
        "SELECT base.g1 AS `dimension.jaffle_product_type` FROM t ORDER BY `g1`"
    )


def test_leaves_single_quoted_literals_untouched():
    sql = 'SELECT \'a "quoted" word\' AS "alias", \'\' AS "empty" FROM t'
    assert rewrite_double_quoted_identifiers(sql) == (
        "SELECT 'a \"quoted\" word' AS `alias`, '' AS `empty` FROM t"
    )


def test_handles_escaped_quotes_inside_literals_and_identifiers():
    # '' inside a string stays; "" inside an identifier collapses to one ".
    sql = 'SELECT \'it\'\'s "fine"\' AS "odd""name" FROM t'
    assert rewrite_double_quoted_identifiers(sql) == (
        "SELECT 'it''s \"fine\"' AS `odd\"name` FROM t"
    )


def test_noop_without_double_quotes():
    sql = "SELECT a, 'b' FROM t WHERE c = 'd''e'"
    assert rewrite_double_quoted_identifiers(sql) == sql


# -- map_double_quoted_identifiers (the scanning core) -----------------------


def test_map_passes_unescaped_inner_text_and_emits_replacement_verbatim():
    sql = 'SELECT 1 AS "a""b", \'keep "this"\' AS note'
    seen: list[str] = []

    def upper(name: str) -> str:
        seen.append(name)
        return f"[{name.upper()}]"

    assert map_double_quoted_identifiers(sql, upper) == (
        'SELECT 1 AS [A"B], \'keep "this"\' AS note'
    )
    assert seen == ['a"b']


# -- float_nullif_divisions (shared ratio compat pass) ------------------------


def test_float_nullif_divisions_casts_with_the_given_type():
    sql = "SELECT a / NULLIF(b, 0) AS r FROM t"
    assert float_nullif_divisions(sql, cast_type="DOUBLE") == (
        "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) AS r FROM t"
    )
    assert float_nullif_divisions(sql, cast_type="DOUBLE PRECISION") == (
        "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE PRECISION) AS r FROM t"
    )


def test_float_nullif_divisions_recurses_and_skips_literals():
    sql = "SELECT a / NULLIF(b / NULLIF(c, 0), 0) AS r, ' / NULLIF(' AS note FROM t"
    assert float_nullif_divisions(sql, cast_type="DOUBLE") == (
        "SELECT a / CAST(NULLIF(b / CAST(NULLIF(c, 0) AS DOUBLE), 0) AS DOUBLE) AS r, "
        "' / NULLIF(' AS note FROM t"
    )


def test_float_nullif_divisions_leaves_unbalanced_tail_untouched():
    sql = "SELECT a / NULLIF(b, 0 FROM t"
    assert float_nullif_divisions(sql, cast_type="DOUBLE") == sql


# -- option_or_env (literal-or-*_env locator resolution) ----------------------


def test_option_or_env_prefers_the_literal(monkeypatch):
    monkeypatch.setenv("SR_TEST_HOST", "from-env")
    assert option_or_env({"host": "literal", "host_env": "SR_TEST_HOST"}, "host") == "literal"


def test_option_or_env_falls_back_to_env_and_records_missing(monkeypatch):
    monkeypatch.setenv("SR_TEST_HOST", "from-env")
    assert option_or_env({"host_env": "SR_TEST_HOST"}, "host") == "from-env"

    monkeypatch.delenv("SR_TEST_HOST", raising=False)
    missing: list[str] = []
    assert option_or_env({"host_env": "SR_TEST_HOST"}, "host", missing) == ""
    assert missing == ["SR_TEST_HOST"]

    # No literal and no *_env option: empty, and nothing recorded.
    missing = []
    assert option_or_env({}, "host", missing) == ""
    assert missing == []
