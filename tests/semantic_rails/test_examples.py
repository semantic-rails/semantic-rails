"""Regression tests for the published worked-example IR files under
``examples/``.

These examples are the durable artifacts callers reference instead of
trial-and-erroring against the runtime. Each file must:

1. Validate against the published JSON Schema
   (``schemas/query_ir.v1.json``).
2. Pass ``Runtime.validate()`` with ``ok: true`` against the active
   jaffle_shop package.
3. Compile to non-empty ``rendered_sql``.

If a future change makes any committed example fail any of these
checks, the test breaks before the docs go stale.

``jsonschema`` is a dev dependency; when it isn't importable we still
exercise validate/compile against the runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SCHEMA_PATH = REPO_ROOT / "schemas" / "query_ir.v1.json"


def _load_examples() -> list[tuple[str, dict[str, Any]]]:
    if not EXAMPLES_DIR.is_dir():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        out.append((path.name, json.loads(path.read_text())))
    return out


EXAMPLES = _load_examples()
EXAMPLE_IDS = [name for name, _ in EXAMPLES]


@pytest.fixture(scope="module")
def runtime() -> Runtime:
    rt = Runtime("jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


def test_examples_directory_is_not_empty() -> None:
    assert EXAMPLES, (
        "examples/ must contain at least one worked-example IR — "
        "callers reference these instead of trial-and-erroring."
    )


@pytest.mark.parametrize(("name", "ir"), EXAMPLES, ids=EXAMPLE_IDS)
def test_example_validates_against_schema(name: str, ir: dict[str, Any]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(ir), key=lambda e: list(e.absolute_path))
    assert not errors, f"examples/{name} does not satisfy schemas/query_ir.v1.json: " + "; ".join(
        f"{list(e.absolute_path)}: {e.message[:120]}" for e in errors[:5]
    )


# The Phase 5 (plan hardening) target IR shape compiles today by
# hand-authoring — the test does NOT depend on the Phase 5 plan
# changes landing. If a future refactor of the runtime breaks this
# hand-authored shape before Phase 5's emitter is updated, mark this
# parametrize entry as xfail with reason="Phase 5 not yet merged" and
# the suite stays green until the emitter catches up.
def _ir_contract_only(ir: dict[str, Any]) -> bool:
    """``_status: "ir_contract_only"`` marks examples that demonstrate
    a locked IR shape whose SQL lowering has not yet shipped. The
    validate/compile assertions soften to "the runtime returns a
    structured envelope" rather than "execution succeeds" so the
    example can advertise the contract immediately.
    """
    return str(ir.get("_status", "")).strip() == "ir_contract_only"


@pytest.mark.parametrize(("name", "ir"), EXAMPLES, ids=EXAMPLE_IDS)
def test_example_validates_against_runtime(runtime: Runtime, name: str, ir: dict[str, Any]) -> None:
    report = runtime.validate(ir)
    if _ir_contract_only(ir):
        # IR-contract-only examples surface a structured error envelope
        # (e.g. INVALID_ANCHOR_ROLE with ``feature_status: ir_contract_only``).
        # That is the right behaviour today — assert the envelope shape
        # so the example stays honest as the feature matures.
        errors = report.get("errors") or []
        assert errors, (
            f"examples/{name} marked _status=ir_contract_only but validate returned ok=True"
        )
        return
    assert report.get("ok") is True, (
        f"examples/{name} failed runtime validate: errors={report.get('errors')!r}"
    )


@pytest.mark.parametrize(("name", "ir"), EXAMPLES, ids=EXAMPLE_IDS)
def test_example_compiles_to_non_empty_sql(runtime: Runtime, name: str, ir: dict[str, Any]) -> None:
    if _ir_contract_only(ir):
        # Compile re-raises a structured ``SemanticLayerError`` for
        # IR-contract-only examples. Catch it so the example stays in
        # the regular regression suite — if the runtime ever silently
        # returned (or returned a non-structured error), this test
        # would fail loudly.
        from semantic_rails.errors import SemanticLayerError

        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.compile(ir)
        details = excinfo.value.details or {}
        assert str(details.get("feature_status", "")) == "ir_contract_only", (
            f"examples/{name} compile raised but with unexpected details: {details!r}"
        )
        return
    compiled = runtime.compile(ir)
    sql = str(compiled.get("rendered_sql") or "")
    assert sql.strip(), (
        f"examples/{name} compiled to empty rendered_sql — "
        f"compile output keys: {list(compiled.keys())}"
    )


def test_first_run_revenue_by_store_example_executes_as_store_rollup(runtime_factory):
    """Spot-check the shipped revenue-by-store example: it must compile
    AND execute to the expected store-level rollup shape (no time axis,
    2 rows for the 2 jaffle stores). This guards against silent
    breakage of the shape advertised in the first-run example."""
    query_path = REPO_ROOT / "examples" / "jaffle_shop_revenue_by_store.json"
    query = json.loads(query_path.read_text(encoding="utf-8"))

    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(query)
    finally:
        runtime.close()

    assert query.get("time") is None
    assert result["row_count"] == 2
    assert list(result["rows"][0]) == ["dimension.jaffle_store_name", "revenue_usd"]
