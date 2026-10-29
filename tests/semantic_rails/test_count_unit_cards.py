"""A count measure's card says what it counts: rows of its model, or distinct values of a column.

A count of distinct keys and a count of rows differ wherever a key repeats (customers who ordered
vs their orders), and the two read alike by name. ``count_unit`` is read from the measure's
definition alone, never from its label or description.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.expressions import CaseExpr, CaseWhenExpr, ColumnRefExpr, LiteralExpr
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import inspect_payload
from semantic_rails.metadata_parts.guidance import _count_unit
from semantic_rails.runtime import Runtime

SHOP = Path(__file__).resolve().parents[1] / "integration" / "correctness" / "shop"
ORDER = "entity.jaffle_order"


@pytest.fixture(scope="module")
def jaffle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    from tests.semantic_rails.conftest import copy_package_config

    path = copy_package_config(tmp_path_factory.mktemp("jaffle"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_config(
        load_package_config(str(path)), source_path=str(path), package_id="jaffle_shop"
    )
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    package = tmp_path_factory.mktemp("shop") / "shop"
    shutil.copytree(SHOP, package)
    runtime = Runtime.from_config(
        load_package_config(str(package)), source_path=str(package), package_id="shop"
    )
    yield runtime
    runtime.close()


def _card(runtime: Runtime, object_id: str) -> dict[str, Any]:
    return inspect_payload(runtime, object_id=object_id)["card"]


@pytest.mark.parametrize(
    ("measure", "unit"),
    [
        # The same model's rows, counted by two different keys.
        ("measure.jaffle.order_count", f"distinct order_id across rows of {ORDER}"),
        ("measure.jaffle.ordering_customer_count", f"distinct customer_id across rows of {ORDER}"),
        # A CASE that reads order_id only on the rows its condition keeps.
        (
            "measure.jaffle.large_order_count",
            f"distinct order_id across rows of {ORDER} where its condition holds",
        ),
    ],
)
def test_a_distinct_count_names_the_column_it_counts(jaffle, measure, unit):
    assert _card(jaffle, measure)["count_unit"] == unit


@pytest.mark.parametrize(
    "measure",
    [
        "measure.jaffle.revenue_usd",  # a sum of a value
        "measure.jaffle.lifetime_order_count",  # a sum of a stored count: adds up, counts nothing
        "measure.jaffle.open_store_count_eop",  # a stock of a stored count
    ],
)
def test_a_measure_that_counts_nothing_itself_has_no_count_unit(jaffle, measure):
    assert "count_unit" not in _card(jaffle, measure)


def test_a_row_count_on_a_keyless_model_counts_its_rows(shop):
    scans = "entity.shop_shipment_scans"
    assert _card(shop, "measure.shop.scan_count")["count_unit"] == f"rows of {scans}"
    # Its parents' key, counted once each, not its rows.
    assert _card(shop, "measure.shop.scanned_orders")["count_unit"] == (
        f"distinct order_id across rows of {scans}"
    )


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_the_count_unit_is_on_the_mcp_card_at_every_verbosity(jaffle, verbosity):
    adapter = SemanticLayerMCPAdapter(jaffle)
    response = adapter.call_tool(
        "inspect", {"object_id": "measure.jaffle.ordering_customer_count", "verbosity": verbosity}
    )
    assert response["ok"], response
    assert response["card"]["count_unit"] == f"distinct customer_id across rows of {ORDER}"


def _when(then: Any) -> CaseWhenExpr:
    return CaseWhenExpr(when=ColumnRefExpr("is_large_order"), then=then)


@pytest.mark.parametrize(
    ("aggregation", "expr", "unit"),
    [
        ("sum", LiteralExpr(1), f"rows of {ORDER}"),
        # A sum of 1 on the rows its condition keeps, 0 (or NULL) on the rest.
        (
            "sum",
            CaseExpr([_when(LiteralExpr(1))], LiteralExpr(0)),
            f"rows of {ORDER} where its condition holds",
        ),
        (
            "sum",
            CaseExpr([_when(LiteralExpr(1)), _when(LiteralExpr(1))]),
            f"rows of {ORDER} where its condition holds",
        ),
        ("count", ColumnRefExpr("customer_id"), f"rows of {ORDER} with a non-NULL customer_id"),
        (
            "count_distinct",
            ColumnRefExpr("customer_id", entity="entity.jaffle_customer"),
            f"distinct entity.jaffle_customer.customer_id across rows of {ORDER}",
        ),
        # What counts no single thing says nothing.
        ("sum", LiteralExpr(2), None),
        ("sum", CaseExpr([_when(LiteralExpr(1))], LiteralExpr(2)), None),
        # ELSE 0 is a value a distinct count counts too.
        ("count_distinct", CaseExpr([_when(ColumnRefExpr("order_id"))], LiteralExpr(0)), None),
        (
            "count_distinct",
            CaseExpr([_when(ColumnRefExpr("order_id")), _when(ColumnRefExpr("customer_id"))]),
            None,
        ),
        ("avg", LiteralExpr(1), None),
    ],
)
def test_the_unit_is_read_from_the_definition_alone(jaffle, aggregation, expr, unit):
    measure = next(m for m in jaffle.config.measures if m.id == "measure.jaffle.order_count")
    edited = replace(measure, default_aggregation=aggregation, expr=expr)
    assert _count_unit(edited).get("count_unit") == unit


def test_a_lookup_carries_a_value_and_counts_nothing(jaffle):
    measure = next(m for m in jaffle.config.measures if m.id == "measure.jaffle.order_count")
    assert _count_unit(replace(measure, lookup_from="measure.jaffle.order_count")) == {}
