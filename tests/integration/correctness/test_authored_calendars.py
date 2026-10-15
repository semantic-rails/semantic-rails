"""A non-default calendar, or a clock bound to one, is refused wherever it would bucket."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import _load_package_source, _parse_package
from semantic_rails.config_parts.package_loader import normalize_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry

from .conftest import SHOP
from .test_correctness import ORDERS, REVENUE, ROLE, _backend

WINDOW = {"start": "2024-05-06", "end": "2024-05-20"}


def _fiscal_orders(runtime):
    """The shop package with its orders bound to the fiscal calendar, as authored."""
    raw = _load_package_source(str(SHOP))
    raw["models"]["orders"]["calendar_id"] = "fiscal"
    authored = _parse_package(normalize_package(raw), path=str(SHOP))
    return replace(authored, package=runtime.config.package, aggregate_relations=[])


def _query(runtime, **time):
    """The engine's form of a call over the window; a call never names a calendar."""
    call = {
        "select": [{"expression": ORDERS, "as": "orders"}, {"expression": REVENUE, "as": "rev"}],
        "time": {"temporal_role": ROLE, **WINDOW, **time},
    }
    return runtime.engine_payload(call)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["quarter", "week", "day", "to_date"])
def test_a_fiscal_clock_refuses_before_any_sql(request, backend_name, grain):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    config = _fiscal_orders(runtime)
    if grain == "to_date":
        to_date = {"kind": "period_to_date", "input": REVENUE, "period": "year"}
        query = runtime.engine_payload(
            {
                "select": [{"expression": to_date, "as": "to_date"}],
                "time": {"temporal_role": ROLE, "grain": "month"},
            }
        )
    else:
        query = _query(runtime, grain=grain)
    with pytest.raises(SemanticLayerError) as refused:
        compile_query(config, Registry(config), query)

    assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    assert refused.value.details["reason"] == "calendar_not_supported_yet"
    assert refused.value.details["calendar_id"] == "fiscal"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_a_fiscal_clock_without_a_grain_answers_its_exact_days(request, backend_name):
    # Nothing buckets, so the binding changes nothing: the window's rows, summed.
    backend = _backend(request, backend_name)
    runtime = backend.runtimes["utc_authored"]
    config = _fiscal_orders(runtime)
    compiled = compile_query(config, Registry(config), _query(runtime))
    reference = backend.reference(
        "SELECT COUNT(DISTINCT order_id), SUM(amount) FROM orders "
        "WHERE ordered_at >= TIMESTAMP '2024-05-06' AND ordered_at < TIMESTAMP '2024-05-20'"
    )
    rows = runtime._get_adapter().query(compiled["sql"])

    assert [(row["orders"], row["rev"]) for row in rows] == [tuple(reference[0])]
