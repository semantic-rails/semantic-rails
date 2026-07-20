"""Cross-warehouse conformance: every adapter must produce IDENTICAL
results for the same semantic queries over the same JaffleShop rows.

One parametrized contract:

- ``test_registry_targets_complete`` — every warehouse registered in
  ``semantic_rails.dialects`` has a target module here, so a new
  dialect is auto-covered the moment it's registered.
- ``test_semantic_surface_path`` — discover → inspect → plan →
  validate → compile → execute against the live warehouse.
- ``test_battery_parity`` — the full query battery (worked examples +
  the package's own test queries: measures, metrics, temporal joins,
  entity-graph hops, metric_predicate, percentiles) matches the DuckDB
  reference row-for-row.
"""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.dialects import supported_warehouses
from semantic_rails.http_core import SemanticHTTPService

from .harness import (
    BatteryCase,
    IntegrationTarget,
    assert_rows_match,
    discover_targets,
    load_battery,
    normalize_rows,
)

BATTERY = load_battery()


def test_registry_targets_complete() -> None:
    targets = discover_targets()
    missing = [name for name in supported_warehouses() if name not in targets]
    assert not missing, (
        f"Registered warehouses without an integration target: {missing}. "
        "Add tests/integration/targets/<warehouse>.py (see docs/ADDING_A_DIALECT.md)."
    )
    unknown = [name for name in targets if name not in supported_warehouses()]
    assert not unknown, f"Integration targets for unregistered warehouses: {unknown}"


def test_battery_is_substantial() -> None:
    # The battery must keep exercising the real semantic surface; if the
    # examples or package tests shrink dramatically, fail loudly.
    assert len(BATTERY) >= 15, f"conformance battery shrank to {len(BATTERY)} cases"


def test_semantic_surface_path(target_runtime, target: IntegrationTarget) -> None:
    """discover → inspect → plan → validate → compile → execute."""
    service = SemanticHTTPService(target_runtime)

    discover, status = service.handle("POST", "/discover", {"terms": "revenue"})
    assert status == 200 and discover.get("ok"), discover
    results: list[dict[str, Any]] = []
    for kind_key in ("measures", "metrics", "dimensions", "segments", "entities"):
        results.extend(entry for entry in discover.get(kind_key) or [] if isinstance(entry, dict))
    assert results, f"discover returned nothing: {discover}"

    object_id = next((str(entry["id"]) for entry in results if entry.get("id")), "")
    assert object_id, f"no inspectable id in discover results: {results[:3]}"

    inspect, status = service.handle("POST", "/inspect", {"object_id": object_id})
    assert status == 200 and inspect.get("ok"), inspect

    plan, status = service.handle("POST", "/plan", {"intent": "total revenue by month"})
    assert status == 200 and plan.get("ok"), plan

    payload = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue_usd"}],
        "group_by": ["dimension.jaffle_store_name"],
        "order_by": [{"field": "dimension.jaffle_store_name", "direction": "ASC"}],
        "limit": 10,
    }
    validated, status = service.handle("POST", "/validate", {"query": payload})
    assert status == 200 and validated.get("ok"), validated

    compiled, status = service.handle("POST", "/compile", {"query": payload})
    assert status == 200 and compiled.get("ok"), compiled
    assert compiled.get("rendered_sql") or compiled.get("sql"), compiled

    executed, status = service.handle("POST", "/query", {"query": payload})
    assert status == 200 and executed.get("ok"), executed
    assert executed.get("rows"), f"execute returned no rows: {executed}"


@pytest.mark.parametrize("case", BATTERY, ids=[case.name for case in BATTERY])
def test_battery_parity(
    target_runtime,
    target: IntegrationTarget,
    case: BatteryCase,
    reference_results: dict[str, list[dict[str, Any]]],
) -> None:
    if target.is_reference:
        pytest.skip("reference target defines the expected rows")
    result = target_runtime.query(case.payload)
    assert result.get("ok", True), f"{target.warehouse}/{case.name}: query failed — {result}"
    actual = normalize_rows(result.get("rows") or [])
    assert_rows_match(
        reference_results[case.name],
        actual,
        context=f"{target.warehouse}/{case.name}",
    )
