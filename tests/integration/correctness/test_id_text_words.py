"""The text words read an id as its text, on DuckDB and Postgres alike: a product code by a
fragment, a UUID by its first characters and a numeric id by its last digits, in a filter and
in a valid-values search, ignoring case.

Fixture ``vendors/``: 236 products by a code (JAF-002 for an even number, SES-001 for an odd
one), 236 shipments by a UUID (the md5 of its number) in the warehouse's own UUID type, and
vendors 1 to 237 by number. Each answer is checked against the ids read back in Python.
"""

from __future__ import annotations

from typing import Any

from .conftest import _rows

SKU = "dimension.vendors_product_sku"
SHIPMENT = "dimension.vendors_shipment_id"
VENDOR = "dimension.vendors_vendor_id"
PRODUCTS = {"expression": "measure.vendors.product_count", "as": "n"}
SHIPMENTS = {"expression": "measure.vendors.shipment_count", "as": "n"}
VENDORS = {"expression": "measure.vendors.vendor_count", "as": "n"}


def _values(out: dict[str, Any]) -> list[Any]:
    return [row["value"] for row in out["values"]]


def _ids(vendors, sql: str) -> list[Any]:
    return sorted(value for (value,) in _rows(vendors.runtime, sql))


def _where(field: str, op: str, value: str, *, select: dict[str, Any]) -> dict[str, Any]:
    return {"select": [select], "where": [{"field": field, "op": op, "value": value}]}


def test_contains_finds_product_codes_by_a_fragment_in_any_case(vendors):
    codes = _ids(vendors, "SELECT sku FROM products")
    expected = [code for code in codes if "f-2" in code.lower()]
    assert len(expected) == 19  # JAF-200 to JAF-236, the even ones
    call = {**_where(SKU, "contains", "f-2", select=PRODUCTS), "group_by": [SKU]}
    rows = vendors.runtime.query(call)["rows"]
    assert sorted(row[SKU] for row in rows) == expected
    out = vendors.page(SKU, search="F-2")
    assert (_values(out), out["total_count"], out["has_more"]) == (expected, 19, False)


def test_an_id_search_pages_and_counts_as_a_text_search_does(vendors):
    codes = _ids(vendors, "SELECT sku FROM products")
    expected = [code for code in codes if "jaf" in code.lower()]
    assert len(expected) == 118
    pages = [vendors.page(SKU, search="Jaf", limit=50, offset=offset) for offset in (0, 50, 100)]
    assert [code for page in pages for code in _values(page)] == expected
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [page["total_count"] for page in pages] == [None, None, 118]


def test_starts_with_finds_a_uuid_by_its_first_characters(vendors):
    # Counted, so no uuid column is read back on either warehouse.
    shipments = _ids(vendors, "SELECT CAST(shipment_id AS VARCHAR) FROM shipments")
    for prefix, count in (("E2C0", 1), ("e2", 3), ("0", 21)):
        assert sum(text.startswith(prefix.lower()) for text in shipments) == count
        [row] = vendors.runtime.query(_where(SHIPMENT, "starts_with", prefix, select=SHIPMENTS))[
            "rows"
        ]
        assert row["n"] == count, prefix


def test_ends_with_reads_a_numeric_id_as_its_digits(vendors):
    numbers = _ids(vendors, "SELECT vendor_id FROM vendors")
    expected = [number for number in numbers if str(number).endswith("03")]
    assert expected == [103, 203]  # 3 is "3", never "003"
    call = {**_where(VENDOR, "ends_with", "03", select=VENDORS), "group_by": [VENDOR]}
    rows = vendors.runtime.query(call)["rows"]
    assert sorted(row[VENDOR] for row in rows) == expected
    out = vendors.page(VENDOR, search="03")
    assert (_values(out), out["total_count"], out["has_more"]) == (expected, 2, False)
