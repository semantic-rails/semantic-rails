"""Shared harness for the cross-warehouse conformance suite.

Defines :class:`IntegrationTarget` (one per warehouse, discovered from
``targets/``), the query battery (every committed worked example plus
every executable query in the jaffle_shop package's own test suites),
runtime construction per target, and row ordering for comparisons. Public
result types must agree; only column casing and finite-number tolerances
are normalized.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, SeedSpec

from .fixture import JaffleFixture
from .loaders import FixtureLoader

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE_DIR = REPO_ROOT / "configs" / "semantic_rails" / "jaffle_shop"
EXAMPLES_DIR = REPO_ROOT / "examples"

# Package test suites whose `query:` blocks join the battery. The
# error_codes suite is excluded by design — it asserts failures.
PACKAGE_TEST_SUITES = ("core", "advanced")

# Cases collected from examples/package tests that cannot EXECUTE, with
# the reason. Everything else must run on every warehouse.
EXCLUDED_BATTERY_CASES: dict[str, str] = {
    # Validate/compile-only worked example: per-row event-anchored
    # windows are blocked at execution until the SQL lowering ships.
    "example_anchored_window_cohort_retention": "compile-only (lowering not shipped)",
    # Intentional-failure package test (asserts a duplicate-alias error).
    "core_duplicate_alias_rejected": "asserts a validation error, not rows",
}


@dataclass(frozen=True)
class IntegrationTarget:
    """Everything the conformance suite needs to run one warehouse.

    A new warehouse adds one module ``targets/<warehouse>.py`` exposing
    ``TARGET = IntegrationTarget(...)``. The conformance suite
    discovers it automatically and a registry-coverage test fails if a
    registered warehouse has no target module.
    """

    warehouse: str
    connection_kind: str = ""
    connection_options: Mapping[str, str] = field(default_factory=dict)
    # Env vars that must be present (non-empty) for this target to run;
    # otherwise its tests SKIP with a message naming the variables.
    required_env: tuple[str, ...] = ()
    # adapter -> FixtureLoader. None only for the reference target,
    # whose database IS the fixture.
    make_loader: Callable[[Any], FixtureLoader] | None = None
    is_reference: bool = False
    notes: str = ""

    def missing_env(self) -> tuple[str, ...]:
        return tuple(name for name in self.required_env if not os.environ.get(name, "").strip())


def discover_targets() -> dict[str, IntegrationTarget]:
    """Import every ``targets/<warehouse>.py`` and collect its TARGET."""
    from . import targets as targets_pkg

    found: dict[str, IntegrationTarget] = {}
    for module_info in pkgutil.iter_modules(targets_pkg.__path__):
        module = importlib.import_module(f"{targets_pkg.__name__}.{module_info.name}")
        target = getattr(module, "TARGET", None)
        if isinstance(target, IntegrationTarget):
            found[target.warehouse] = target
    return found


def build_runtime(target: IntegrationTarget, fixture: JaffleFixture) -> Runtime:
    """Build a Runtime for the jaffle_shop package pointed at the target."""
    config = load_package_config(str(PACKAGE_DIR))
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
        source_path=str(PACKAGE_DIR),
        package_id=config.package.package_id,
    )


@dataclass(frozen=True)
class BatteryCase:
    name: str
    payload: dict[str, Any]


def load_battery() -> tuple[BatteryCase, ...]:
    cases: list[BatteryCase] = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        cases.append(BatteryCase(name=f"example_{path.stem}", payload=json.loads(path.read_text())))
    for suite in PACKAGE_TEST_SUITES:
        data = yaml.safe_load((PACKAGE_DIR / "tests" / f"{suite}.yml").read_text())
        for test_name, spec in (data.get("tests") or {}).items():
            query = (spec or {}).get("query")
            if isinstance(query, dict):
                cases.append(BatteryCase(name=f"{suite}_{test_name}", payload=query))
    return tuple(case for case in cases if case.name not in EXCLUDED_BATTERY_CASES)


# ---------------------------------------------------------------------------
# Row ordering — preserve the public result types from every driver.
# ---------------------------------------------------------------------------

_FLOAT_SORT_DIGITS = 6


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [{str(key).lower(): val for key, val in row.items()} for row in rows]

    def sort_key(row: dict[str, Any]) -> str:
        rounded = {
            key: (round(val, _FLOAT_SORT_DIGITS) if isinstance(val, float) else val)
            for key, val in row.items()
        }
        return json.dumps(rounded, sort_keys=True, allow_nan=False)

    return sorted(normalized, key=sort_key)


def _values_match(expected: Any, actual: Any) -> bool:
    # JSON has one number type, but booleans and numeric-looking strings
    # are distinct. Never coerce text or erase timestamp awareness.
    expected_number = type(expected) in (int, float)
    actual_number = type(actual) in (int, float)
    if expected_number and actual_number:
        if type(expected) is int and type(actual) is int:
            return expected == actual
        return math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-9)
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _values_match(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _values_match(left, right) for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


def assert_rows_match(
    reference: list[dict[str, Any]], actual: list[dict[str, Any]], *, context: str
) -> None:
    """Assert two normalized row lists are equivalent (float-tolerant)."""
    assert len(reference) == len(actual), (
        f"{context}: row count mismatch — reference={len(reference)} actual={len(actual)}\n"
        f"reference[:3]={reference[:3]}\nactual[:3]={actual[:3]}"
    )
    for idx, (ref_row, act_row) in enumerate(zip(reference, actual, strict=True)):
        assert set(ref_row) == set(act_row), (
            f"{context}: row {idx} column mismatch — "
            f"reference={sorted(ref_row)} actual={sorted(act_row)}"
        )
        for key in ref_row:
            assert _values_match(ref_row[key], act_row[key]), (
                f"{context}: row {idx} value mismatch for '{key}' — "
                f"reference={ref_row[key]!r} actual={act_row[key]!r}\n"
                f"reference row={ref_row}\nactual row={act_row}"
            )


def assert_column_types_match(
    reference: dict[str, Any], actual: dict[str, Any], *, context: str
) -> None:
    """Column metadata distinguishes equal strings with different logical types."""
    assert {key.lower(): value for key, value in reference["column_types"].items()} == {
        key.lower(): value for key, value in actual["column_types"].items()
    }, f"{context}: result column types differ"


def run_battery_case(runtime: Runtime, case: BatteryCase) -> list[dict[str, Any]]:
    result = runtime.query(case.payload)
    assert result.get("ok", True), f"{case.name}: query failed — {result}"
    return normalize_rows(result.get("rows") or [])
