"""valid-values reads every value of a dimension, not a window of its first hundred.

Fixture ``vendors/``: 236 vendors named Vendor 001 to Vendor 236, and one with no name. A search
finds a name wherever it sorts, pages reach the last name, ``has_more`` turns false only on the
last page, and ``total_count`` is the exact count once a page holds the last match (null
before that), on DuckDB and Postgres alike.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.runtime import Runtime

from .conftest import _postgres, _rows, _runtime, _strict

VENDORS = Path(__file__).resolve().parent / "vendors"
SEED = (VENDORS / "data" / "seed.sql").read_text(encoding="utf-8")
NAME = "dimension.vendors_vendor_name"


class Vendors:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def names(self, needle: str = "") -> list[str]:
        """Every known name holding ``needle`` (any case), in order: the reference."""
        rows = _rows(
            self.runtime,
            "SELECT vendor_name FROM vendors WHERE vendor_name IS NOT NULL ORDER BY vendor_name",
        )
        return [name for (name,) in rows if needle.lower() in name.lower()]

    def page(self, **arguments: Any) -> dict[str, Any]:
        return valid_values_payload(
            self.runtime, dimension_id=NAME, allow_live_query=True, **arguments
        )


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def vendors(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Vendors]:
    package = tmp_path_factory.mktemp("vendors") / "vendors"
    shutil.copytree(VENDORS, package)
    if request.param == "duckdb":
        runtime = _runtime(package)
        try:
            _rows(runtime, "SELECT 1")  # the fixture owns the connection
            yield Vendors(runtime)
        finally:
            runtime.close()
        return
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run through with_postgres.sh)")
    schema = f"sr_vendors_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }
    admin = _postgres(package, options)
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    runtime = _postgres(package, {**options, "schema": schema})
    try:
        _rows(runtime, SEED)
        yield Vendors(runtime)
    finally:
        runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


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
