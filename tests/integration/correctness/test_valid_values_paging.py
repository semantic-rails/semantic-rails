"""valid-values reads every value of a dimension, not a window of its first hundred.

Fixture ``vendors/``: 236 vendors named Vendor 001 to Vendor 236, and one with no name. A search
finds a name wherever it sorts, pages reach the last name, ``has_more`` turns false only on the
last page, and ``total_count`` is the exact count once a page holds the last match (null
before that), on DuckDB and Postgres alike.
"""

from __future__ import annotations

from typing import Any

import pytest


def _values(out: dict[str, Any]) -> list[str]:
    return [row["value"] for row in out["values"]]


def test_search_finds_a_name_that_sorts_past_the_first_hundred(vendors):
    assert len(vendors.names()) == 236
    out = vendors.page(search="vendor 203")
    assert _values(out) == ["Vendor 203"] == vendors.names("vendor 203")
    assert (out["total_count"], out["has_more"]) == (1, False)


def test_pages_reach_the_last_name_and_say_when_they_do(vendors):
    pages = [vendors.page(limit=100, offset=offset) for offset in (0, 100, 200)]
    assert [name for page in pages for name in _values(page)] == vendors.names()
    assert [page["has_more"] for page in pages] == [True, True, False]
    # A count only once a page holds the last name: before that it would be the window's.
    assert [page["total_count"] for page in pages] == [None, None, 236]


def test_search_pages_through_every_match(vendors):
    matches = vendors.names("vendor 2")  # Vendor 200 to Vendor 236
    assert len(matches) == 37
    first, last = (vendors.page(search="VENDOR 2", limit=20, offset=offset) for offset in (0, 20))
    assert _values(first) + _values(last) == matches
    assert (first["has_more"], first["total_count"]) == (True, None)
    assert (last["has_more"], last["total_count"]) == (False, 37)


@pytest.mark.parametrize("search", ["_", "%", "0%"])
def test_search_reads_wildcards_as_text(vendors, search):
    # No name holds the character, so a wildcard read as LIKE's would match every name.
    out = vendors.page(search=search)
    assert (_values(out), out["total_count"], out["has_more"]) == ([], 0, False)
