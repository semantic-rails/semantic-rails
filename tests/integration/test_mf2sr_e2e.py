"""mf2sr end-to-end: a translated MetricFlow package runs on EVERY warehouse.

The proof chain:

1. ``mf_jaffle/`` holds a faithful MetricFlow-style jaffle_shop project
   (dbt schema-2 form) whose semantic models point at the SAME fixture
   tables the conformance suite loads into every warehouse
   (``jaffle_order``, ``jaffle_customer``, ``jaffle_item``,
   ``jaffle_store``).
2. A session fixture runs the in-process translator (``mf2sr.translate``
   — the exact function the CLI calls) into a tmp dir and asserts the
   emitted package loads through ``load_package_config``.
3. The same warehouse targets as the conformance suite (the ``target``
   fixture from ``conftest.py``, with its missing-env skip marks) each
   get a Runtime built from the TRANSLATED config, swapped onto that
   warehouse's connection — mirroring ``harness.build_runtime`` but for
   the translated package.
4. A battery of queries over the translated surface (measures, metrics,
   filtered aggregates, ratios, entity-graph hops, time grains) must
   match the DuckDB reference row-for-row on every target.

Fixture data is seeded by the conformance suite's loaders; the loader
call here is the same idempotent ``ensure_loaded`` the conformance
conftest uses, so an already-seeded warehouse costs one marker probe.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mf2sr.translate import translate
from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec

from .fixture import JaffleFixture
from .harness import IntegrationTarget, assert_rows_match, normalize_rows

MF_SOURCE_DIR = Path(__file__).resolve().parent / "mf_jaffle"
PACKAGE_ID = "mf_jaffle"

# Queries over the TRANSLATED package surface — ids below are the ones
# the translator + loader derive from the MetricFlow input (asserted in
# `mf_package_dir` so drift fails loudly at session setup, not row-diff
# time). Covers: plain aggregate metric + entity hop (orders->stores),
# entity_count by time grain, filtered aggregate + sum_boolean by a
# hop to the customer dim, bare count_distinct, ratio, and a second
# fact table (order items).
BATTERY: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "revenue_by_store",
        {
            "version": 1,
            "select": [{"expression": {"metric": "metric.mf_jaffle.revenue"}, "as": "revenue"}],
            "group_by": ["dimension.mf_jaffle_store_store_name"],
        },
    ),
    (
        "order_count_by_month",
        {
            "version": 1,
            "select": [{"expression": {"metric": "metric.mf_jaffle.order_count"}, "as": "orders"}],
            "time": {
                "temporal_role": "temporal_role.mf_jaffle_order_ordered_at",
                "grain": "month",
            },
        },
    ),
    (
        "large_orders_by_customer_type",
        {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.mf_jaffle.large_order_revenue"},
                    "as": "large_order_revenue",
                },
                {
                    "expression": {"metric": "metric.mf_jaffle.large_orders"},
                    "as": "large_orders",
                },
            ],
            "group_by": ["dimension.mf_jaffle_customer_customer_type"],
        },
    ),
    (
        "customers_with_orders_total",
        {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.mf_jaffle.customers_with_orders"},
                    "as": "customers",
                }
            ],
        },
    ),
    (
        "revenue_per_order_by_store",
        {
            "version": 1,
            "select": [
                {
                    "expression": {"metric": "metric.mf_jaffle.revenue_per_order"},
                    "as": "revenue_per_order",
                }
            ],
            "group_by": ["dimension.mf_jaffle_store_store_name"],
        },
    ),
    (
        "item_revenue_by_product_type",
        {
            "version": 1,
            "select": [
                {"expression": {"metric": "metric.mf_jaffle.item_revenue"}, "as": "item_revenue"}
            ],
            "group_by": ["dimension.mf_jaffle_item_product_type"],
        },
    ),
)

EXPECTED_METRIC_IDS = {
    "metric.mf_jaffle.revenue",
    "metric.mf_jaffle.order_count",
    "metric.mf_jaffle.large_orders",
    "metric.mf_jaffle.large_order_revenue",
    "metric.mf_jaffle.customers_with_orders",
    "metric.mf_jaffle.revenue_per_order",
    "metric.mf_jaffle.item_revenue",
}


def _strict() -> bool:
    return os.environ.get("SR_INTEGRATION_STRICT", "").strip() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def mf_package_dir(tmp_path_factory: pytest.TempPathFactory, jaffle_fixture: JaffleFixture) -> Path:
    """Translate ``mf_jaffle/`` in-process and sanity-check the result."""
    out_dir = tmp_path_factory.mktemp("mf2sr_e2e")
    report = translate(
        MF_SOURCE_DIR,
        out_dir,
        package_id=PACKAGE_ID,
        warehouse="duckdb",
        default_db=str(jaffle_fixture.reference_db),
    )
    assert not report.warnings, (
        f"mf2sr translation of {MF_SOURCE_DIR} must be lossless, got warnings: {report.warnings}"
    )
    assert sorted(report.models_emitted) == ["customers", "order_items", "orders", "stores"]

    # DuckDB packages declare a seed script that must exist at load
    # time; the reference db is already fully seeded so it never runs.
    seed_stub = report.package_dir / "data" / f"seed_{PACKAGE_ID}.sql"
    seed_stub.parent.mkdir(parents=True, exist_ok=True)
    seed_stub.touch()

    config = load_package_config(str(report.package_dir))
    metric_ids = {metric.id for metric in config.metric_recipes}
    missing = EXPECTED_METRIC_IDS - metric_ids
    assert not missing, f"translated package is missing metrics: {sorted(missing)}"
    queried_dimensions = {dim for _, payload in BATTERY for dim in payload.get("group_by", [])}
    available_dimensions = {dim.id for dim in config.dimensions}
    assert queried_dimensions <= available_dimensions, (
        f"battery references unknown dimensions: {sorted(queried_dimensions - available_dimensions)}"
    )
    return report.package_dir


def _build_translated_runtime(
    package_dir: Path, target: IntegrationTarget, fixture: JaffleFixture
) -> Runtime:
    """`harness.build_runtime`, but for the translated package."""
    config = load_package_config(str(package_dir))
    if target.is_reference:
        package = replace(config.package, default_db=str(fixture.reference_db))
    else:
        package = replace(
            config.package,
            warehouse=target.warehouse,
            default_db="",
            seed=SeedSpec(),
            connection=ConnectionSpec(
                kind=target.connection_kind,
                name="",
                options=dict(target.connection_options),
            ),
        )
    return Runtime.from_config(
        replace(config, package=package),
        source_path=str(package_dir),
        package_id=config.package.package_id,
    )


@pytest.fixture(scope="session")
def mf_runtime(target: IntegrationTarget, jaffle_fixture: JaffleFixture, mf_package_dir: Path):
    """A Runtime for the translated package on the current target.

    Seeding is idempotent and shared with the conformance suite: if the
    warehouse already carries the fixture (marker-table fingerprint
    matches), ``ensure_loaded`` is a single probe; otherwise it loads
    the same canonical parquet rows the conformance loaders use.
    """
    runtime = _build_translated_runtime(mf_package_dir, target, jaffle_fixture)
    if target.make_loader is not None:
        adapter = runtime._get_adapter()  # noqa: SLF001 - same production connection path as conftest
        loader = target.make_loader(adapter)
        try:
            loader.ensure_loaded(jaffle_fixture)
        except Exception as exc:
            runtime.close()
            if _strict():
                raise
            pytest.skip(
                f"{target.warehouse}: could not reach/load warehouse "
                f"({type(exc).__name__}: {exc}) — is the infra up? "
                "(make warehouses-up; set SR_INTEGRATION_STRICT=1 to fail instead)"
            )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture(scope="session")
def mf_reference_results(
    mf_package_dir: Path, jaffle_fixture: JaffleFixture
) -> dict[str, list[dict[str, Any]]]:
    """The battery executed once on the DuckDB reference target."""
    from .harness import discover_targets

    reference = discover_targets()["duckdb"]
    runtime = _build_translated_runtime(mf_package_dir, reference, jaffle_fixture)
    try:
        results: dict[str, list[dict[str, Any]]] = {}
        for name, payload in BATTERY:
            result = runtime.query(payload)
            assert result.get("ok", True), (
                f"reference (duckdb) failed mf2sr battery case {name}: {result}"
            )
            rows = result.get("rows") or []
            assert rows, f"reference (duckdb) returned no rows for {name}"
            results[name] = normalize_rows(rows)
        return results
    finally:
        runtime.close()


@pytest.mark.parametrize("case_name", [name for name, _ in BATTERY])
def test_mf2sr_battery_parity(
    mf_runtime: Runtime,
    target: IntegrationTarget,
    mf_reference_results: dict[str, list[dict[str, Any]]],
    case_name: str,
) -> None:
    """Every warehouse must reproduce the DuckDB reference rows for the
    translated package's query battery."""
    payload = dict(next(payload for name, payload in BATTERY if name == case_name))
    result = mf_runtime.query(payload)
    assert result.get("ok", True), f"{target.warehouse}/{case_name}: query failed — {result}"
    actual = normalize_rows(result.get("rows") or [])
    assert_rows_match(
        mf_reference_results[case_name],
        actual,
        context=f"{target.warehouse}/{case_name}",
    )
