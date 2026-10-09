"""Regression test for the published query call JSON Schema.

The schema at ``schemas/query.v2.json`` is the durable contract callers
reference instead of trial-and-erroring against the runtime. This test
runs it (via ``jsonschema``, Draft 2020-12) against every committed call in
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

from semantic_rails.query_call import RETIRED

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "query.v2.json"


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def _all_committed_irs() -> list[tuple[str, dict[str, Any]]]:
    """Collect every committed query call under the repo. Each entry
    is ``(source_label, call)`` so a schema regression points at the
    file the contract first drifted from.
    """
    irs: list[tuple[str, dict[str, Any]]] = []
    # Comparison fixtures — these are the canonical "real" calls that the
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
    assert irs, "no committed calls found — corpus regression"

    failures: list[str] = []
    for label, ir in irs:
        errors = sorted(validator.iter_errors(ir), key=lambda e: list(e.absolute_path))
        if errors:
            lines = [f"  - {list(e.absolute_path)}: {e.message[:200]}" for e in errors[:5]]
            failures.append(f"{label}\n" + "\n".join(lines))
    if failures:
        pytest.fail(
            "Query call schema diverged from committed calls — fix the schema, not the calls:\n"
            + "\n\n".join(failures)
        )


def test_obviously_invalid_ir_fails_schema_validation() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    # ``select`` must be an array — a string is rejected.
    bad = {"select": "not_a_list"}
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
        "select": [{"expression": "measure.revenue", "as": "rev"}],
        "order_by": [{"expression": "measure.revenue", "direction": "desc"}],
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
    field gotcha, the retired ``metric_filters`` vs ``having`` gotcha)
    without depending on a runtime validator.
    """
    irs = _all_committed_irs()
    assert irs, "no committed calls found — corpus regression"
    failures: list[str] = []
    for label, ir in irs:
        if not isinstance(ir, dict):
            failures.append(f"{label}: a query call must be an object")
            continue
        for key in sorted(set(ir) & set(RETIRED)):
            failures.append(f"{label}: '{key}' is not part of a query call")
        for index, item in enumerate(list(ir.get("order_by", []) or [])):
            if not isinstance(item, dict) or "field" not in item:
                failures.append(f"{label}: order_by[{index}] must be {{'field': ...}}")
        for index, item in enumerate(list(ir.get("select", []) or [])):
            if not isinstance(item, str) and (
                not isinstance(item, dict) or "expression" not in item
            ):
                failures.append(f"{label}: select[{index}] must be an id or carry an 'expression'")
    if failures:
        pytest.fail(
            "Committed calls failed stdlib structural checks:\n  - " + "\n  - ".join(failures)
        )


def test_retired_top_level_keys_are_not_in_the_schema() -> None:
    """Result filters are ``having``; ``metric_filters`` and the other retired keys are
    not part of the call, so the schema must not declare them."""
    properties = _load_schema().get("properties", {})
    assert "having" in properties
    assert not set(RETIRED) & set(properties), sorted(set(RETIRED) & set(properties))


def test_schema_rejects_unknown_top_level_keys_except_annotations() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    bad = {"select": ["measure.jaffle.order_count"], "filters": []}
    assert list(validator.iter_errors(bad)), "schema should reject unknown top-level keys"

    annotated = {"select": ["measure.jaffle.order_count"], "_note": "fixture-only annotation"}
    assert not list(validator.iter_errors(annotated)), (
        "schema should allow underscore-prefixed annotations"
    )


def test_schema_ignores_the_version_key() -> None:
    jsonschema = _require_jsonschema()
    validator = jsonschema.Draft202012Validator(_load_schema())
    for version in (1, 2):
        call = {"version": version, "select": ["measure.jaffle.order_count"]}
        assert not list(validator.iter_errors(call)), version


def test_schema_takes_an_entity_filter_and_refuses_a_malformed_one() -> None:
    jsonschema = _require_jsonschema()
    beverage = {"field": "dimension.x_item_type", "op": "is", "value": "beverage"}
    group = {"entity": "entity.x_item", "none": [beverage]}
    validator = jsonschema.Draft202012Validator(_load_schema())
    query = {"select": ["measure.x.count"], "where": [beverage, group]}
    assert not list(validator.iter_errors(query))
    for bad in (
        {"entity": "entity.x_item", "all": [beverage]},
        {**group, "none": []},
        {**group, "none": [group]},
        {**group, "any": [beverage]},
        {"entity": "entity.x_item"},
    ):
        assert list(validator.iter_errors({**query, "where": [bad]})), bad
