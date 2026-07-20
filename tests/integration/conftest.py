"""Conformance-suite fixtures.

Targets are discovered from ``targets/``; a target whose required env
vars are missing SKIPS (so un-provisioned warehouses — e.g. Redshift —
keep the suite green). Connection failures also skip by default with a
loud message, because locally that usually means ``make warehouses-up``
hasn't run; set ``SR_INTEGRATION_STRICT=1`` to turn those into hard
failures (CI posture).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# The reference runtime points default_db at the content-addressed
# fixture cache, which lives outside the package root by design.
os.environ.setdefault("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")

from .fixture import JaffleFixture, build_jaffle_fixture
from .harness import IntegrationTarget, build_runtime, discover_targets, load_battery


def _strict() -> bool:
    return os.environ.get("SR_INTEGRATION_STRICT", "").strip() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def jaffle_fixture() -> JaffleFixture:
    return build_jaffle_fixture()


def _target_params() -> list[Any]:
    params = []
    for name, target in sorted(discover_targets().items()):
        marks = []
        missing = target.missing_env()
        if missing:
            marks.append(pytest.mark.skip(reason=f"{name}: missing env {', '.join(missing)}"))
        params.append(pytest.param(target, id=name, marks=marks))
    return params


@pytest.fixture(scope="session", params=_target_params())
def target(request: pytest.FixtureRequest) -> IntegrationTarget:
    return request.param


@pytest.fixture(scope="session")
def target_runtime(target: IntegrationTarget, jaffle_fixture: JaffleFixture):
    """A Runtime for the target with the fixture loaded (idempotent)."""
    runtime = build_runtime(target, jaffle_fixture)
    if target.make_loader is not None:
        adapter = runtime._get_adapter()  # noqa: SLF001 - deliberate: loaders reuse the production connection path
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
def reference_results(jaffle_fixture: JaffleFixture) -> dict[str, list[dict[str, Any]]]:
    """The full battery executed once on the DuckDB reference target."""
    from .harness import normalize_rows

    reference = discover_targets()["duckdb"]
    runtime = build_runtime(reference, jaffle_fixture)
    try:
        results: dict[str, list[dict[str, Any]]] = {}
        for case in load_battery():
            payload = runtime.query(case.payload)
            assert payload.get("ok", True), (
                f"reference (duckdb) failed battery case {case.name}: {payload}"
            )
            results[case.name] = normalize_rows(payload.get("rows") or [])
        return results
    finally:
        runtime.close()
