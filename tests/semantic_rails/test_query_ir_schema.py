"""Regression test for the published Query IR JSON Schema.

The schema at ``schemas/query_ir.v1.json`` is the durable contract callers
reference instead of trial-and-erroring against the runtime. This test
runs it (via ``jsonschema``, Draft 2020-12) against every committed IR in
the benchmark corpus, the comparison fixtures, and the published example
file. If a future change drifts the schema away from the runtime, this
fails before the contract goes stale.

``jsonschema`` is a dev dependency; if it isn't importable in the
environment, the test is skipped rather than failing — the contract
artifact still ships, and the structural tests below catch drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "query_ir.v1.json"
PREVIEW_V2_SCHEMA_PATH = REPO_ROOT / "schemas" / "query_ir.preview.v2.json"


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def _all_committed_irs() -> list[tuple[str, dict[str, Any]]]:
    """Collect every committed Query IR shape under the repo. Each entry
    is ``(source_label, ir_dict)`` so a schema regression points at the
    file the contract first drifted from.
    """
    irs: list[tuple[str, dict[str, Any]]] = []
    # Comparison fixtures — these are the canonical "real" IRs that the
    # comparisons benchmark runs against MetricFlow, Cube, Malloy, and
    # Snowflake Semantic Views.
    comparison_dir = REPO_ROOT / "comparisons" / "semantic_layers" / "semantic_rails" / "queries"
    if comparison_dir.is_dir():
        for path in sorted(comparison_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            irs.append((str(path.relative_to(REPO_ROOT)), payload))
    # Published examples bundled with the repo.
    examples_dir = REPO_ROOT / "examples"
    if examples_dir.is_dir():
        for path in sorted(examples_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            irs.append((str(path.relative_to(REPO_ROOT)), payload))
    # Blind-agent benchmark corpus — the ``blocked_case`` carries a full
    # query, and each ``builder_cases`` entry carries a ``partial_query``.
    corpus_path = REPO_ROOT / "scripts" / "blind_agent_corpus.json"
    if corpus_path.is_file():
        corpus = json.loads(corpus_path.read_text())
        blocked = corpus.get("blocked_case", {})
        if isinstance(blocked, dict) and isinstance(blocked.get("query"), dict):
            irs.append(
                (f"{corpus_path.relative_to(REPO_ROOT)}::blocked_case.query", blocked["query"])
            )
        for case in corpus.get("builder_cases", []) or []:
            if not isinstance(case, dict):
                continue
            partial = case.get("partial_query")
            if isinstance(partial, dict):
                label = f"{corpus_path.relative_to(REPO_ROOT)}::builder_cases[{case.get('name', '?')}].partial_query"
                irs.append((label, partial))
    return irs


def _require_jsonschema():
    try:
        import jsonschema  # noqa: F401
    except ImportError:  # pragma: no cover — graceful skip on lean envs
        pytest.skip("jsonschema is not installed in this environment")
    return jsonschema


def test_schema_file_exists_and_parses() -> None:
    assert SCHEMA_PATH.is_file(), f"missing {SCHEMA_PATH}"
    schema = _load_schema()
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.get("$id"), "schema must declare an $id"
    assert "$defs" in schema, "schema must use $defs for reuse"


def test_schema_is_a_valid_2020_12_schema() -> None:
    jsonschema = _require_jsonschema()
    # Will raise ``SchemaError`` if the schema is itself invalid.
    jsonschema.Draft202012Validator.check_schema(_load_schema())


def test_every_committed_ir_validates_against_schema() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    irs = _all_committed_irs()
    assert irs, "no committed IRs found — corpus regression"

    failures: list[str] = []
    for label, ir in irs:
        errors = sorted(validator.iter_errors(ir), key=lambda e: list(e.absolute_path))
        if errors:
            lines = [f"  - {list(e.absolute_path)}: {e.message[:200]}" for e in errors[:5]]
            failures.append(f"{label}\n" + "\n".join(lines))
    if failures:
        pytest.fail(
            "Query IR schema diverged from committed IRs — fix the schema, not the IRs:\n"
            + "\n\n".join(failures)
        )


def test_obviously_invalid_ir_fails_schema_validation() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    # ``select`` must be an array — a string is rejected.
    bad = {"version": 1, "select": "not_a_list"}
    errors = list(validator.iter_errors(bad))
    assert errors, "schema should reject select=not_a_list"


def test_obviously_invalid_order_by_shape_fails_schema_validation() -> None:
    """The reviewer's failing case: a select-style expression in
    ``order_by`` (missing the required ``field`` key). Must fail schema
    validation up-front — agents don't need to round-trip the runtime
    to discover the right shape.
    """
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    bad = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.revenue"}, "as": "rev"}],
        "order_by": [{"expression": {"measure": "measure.revenue"}, "direction": "DESC"}],
    }
    errors = list(validator.iter_errors(bad))
    assert errors, "schema should reject order_by entries without 'field'"
    # The error should be anchored at the order_by[0] path.
    paths = [list(e.absolute_path) for e in errors]
    assert any(p[:2] == ["order_by", 0] for p in paths), paths


def test_committed_irs_satisfy_stdlib_minimal_shape_checks() -> None:
    """Stdlib-only structural sanity check that runs even when
    ``jsonschema`` isn't installed. This is intentionally a minimal
    superset of what the full schema enforces — its job is to catch
    drift on the most reviewer-impacting shapes (the ``order_by``
    field gotcha, the ``metric_filters`` vs ``having`` gotcha) without
    depending on a runtime validator.
    """
    irs = _all_committed_irs()
    assert irs, "no committed IRs found — corpus regression"
    failures: list[str] = []
    for label, ir in irs:
        if not isinstance(ir, dict):
            failures.append(f"{label}: top-level IR must be an object")
            continue
        if "having" in ir:
            failures.append(f"{label}: top-level 'having' is not supported — use 'metric_filters'")
        for index, item in enumerate(list(ir.get("order_by", []) or [])):
            if not isinstance(item, dict) or "field" not in item:
                failures.append(f"{label}: order_by[{index}] must be {{'field': ...}}")
        for index, item in enumerate(list(ir.get("select", []) or [])):
            if not isinstance(item, dict) or "expression" not in item:
                failures.append(f"{label}: select[{index}] must carry an 'expression'")
    if failures:
        pytest.fail(
            "Committed IRs failed stdlib structural checks:\n  - " + "\n  - ".join(failures)
        )


def test_having_top_level_key_documented_as_unsupported() -> None:
    """``having`` is not part of the schema and the runtime does NOT
    accept it (use ``metric_filters`` instead). Top-level extra keys are
    rejected except underscore-prefixed annotations, so the canonical
    unsupported name MUST not appear in the schema's properties.
    """
    schema = _load_schema()
    assert "having" not in schema.get("properties", {}), (
        "Query IR schema must not declare 'having'. Use 'metric_filters'."
    )


def test_schema_rejects_unknown_top_level_keys_except_annotations() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    bad = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "filters": [],
    }
    assert list(validator.iter_errors(bad)), "schema should reject unknown top-level keys"

    annotated = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "_note": "fixture-only annotation",
    }
    assert not list(validator.iter_errors(annotated)), (
        "schema should allow underscore-prefixed annotations"
    )


def test_schema_rejects_unsupported_query_ir_version() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    bad = {
        "version": 3,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
    }
    errors = list(validator.iter_errors(bad))
    assert errors, "schema should reject unsupported Query IR versions"


def test_stable_v1_and_preview_v2_have_disjoint_version_contracts() -> None:
    jsonschema = _require_jsonschema()
    v1 = jsonschema.Draft202012Validator(_load_schema())
    v2 = jsonschema.Draft202012Validator(json.loads(PREVIEW_V2_SCHEMA_PATH.read_text()))
    base = {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
    }
    assert not list(v1.iter_errors({"version": 1, **base}))
    assert list(v1.iter_errors({"version": 2, **base}))
    assert not list(v2.iter_errors({"version": 2, **base}))
    assert list(v2.iter_errors({"version": 1, **base}))
