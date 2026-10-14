"""Regression tests for the published worked-example query calls under
``examples/``.

These examples are the durable artifacts callers reference instead of
trial-and-erroring against the runtime. Each file must:

1. Validate against the published JSON Schema
   (``schemas/query.v2.json``).
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
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SCHEMA_PATH = REPO_ROOT / "schemas" / "query.v2.json"


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
        yield opened(rt)
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
    assert not errors, f"examples/{name} does not satisfy schemas/query.v2.json: " + "; ".join(
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


# --- Query calls in the docs -------------------------------------------------------------
#
# Every query call a doc shows is a call the engine reads: a ```json block (or a REST body or
# tool arguments holding one under "query"), a call dict in a ```python block, a JSON call in a
# ```bash command, and a package example or test `query:` in a ```yaml block. Each one must
# satisfy schemas/query.v2.json and pass the front door; a call whose ids are all jaffle_shop
# objects must also validate on jaffle_shop. A ```json call followed by a ```jsonc block that
# opens with `// <CODE>` shows a refusal or clarification: it must be refused with that code.
# Other ```jsonc blocks are annotated illustrations (answers, options) and are not read.

DOCS = (
    "README.md",
    "examples/README.md",
    "docs/QUERY_CALL.md",
    "docs/QUERY_API.md",
    "docs/MCP_INTERFACE.md",
    "docs/AGENT_QUICKSTART.md",
    "docs/AGENT_API_PATH.md",
    "docs/EMBEDDING.md",
    "docs/CONTRACTS.md",
    "docs/ARCHITECTURE.md",
    "docs/PACKAGE_AUTHORING.md",
    "docs/CAPABILITIES.md",
)
DOC_NOW = "2017-09-04T12:00:00Z"
_FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.M | re.S)
_IDS = re.compile(r"\b(?:measure|metric|dimension|entity|temporal_role|relationship)\.[\w.]+")
_REFUSED = re.compile(r"\A\s*//\s*([A-Z][A-Z_]+[A-Z])\b")


def _is_call(node: Any) -> bool:
    from semantic_rails.query_call import FIELDS, IGNORED

    return (
        isinstance(node, dict)
        and bool(node)
        and all(key in FIELDS or key in IGNORED or str(key).startswith("_") for key in node)
        and any(key in node for key in ("select", "group_by", "time"))
    )


def _calls_in(node: Any) -> list[dict[str, Any]]:
    """The calls a parsed example holds: itself, or any call nested in it (a ``query``)."""
    if _is_call(node):
        return [node]
    values = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    return [call for value in values for call in _calls_in(value)]


def _yaml_calls(node: Any) -> list[tuple[dict[str, Any], str]]:
    """``(call, refusal code or "")`` for each call in a YAML example: a package test of kind
    ``validate_fails_with_code`` declares the refusal its call gets."""
    if isinstance(node, dict) and node.get("kind") == "validate_fails_with_code":
        return [(call, str(node.get("code") or "")) for call in _calls_in(node.get("query"))]
    if _is_call(node):
        return [(node, "")]
    values = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    return [pair for value in values for pair in _yaml_calls(value)]


def _python_calls(source: str) -> list[dict[str, Any]]:
    import ast

    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            try:
                value = ast.literal_eval(node)
            except ValueError:
                continue
            out.extend([value] if _is_call(value) else [])
    return out


def _bash_calls(source: str) -> list[dict[str, Any]]:
    out = []
    for text in re.findall(r"'(\{.*?\})'", source, re.S):
        try:
            out.extend(_calls_in(json.loads(text)))
        except ValueError:
            continue
    return out


def _doc_calls() -> list[tuple[str, dict[str, Any], str]]:
    """``(doc:line, call, refusal code or "")`` for every call the docs show."""
    found: list[tuple[str, dict[str, Any], str]] = []
    for doc in DOCS:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        fences = list(_FENCE.finditer(text))
        for index, match in enumerate(fences):
            language, body = match.group(1), match.group(2)
            line = text.count("\n", 0, match.start()) + 1
            code = ""
            if language == "json":
                calls = _calls_in(json.loads(body))
                after = fences[index + 1] if index + 1 < len(fences) else None
                if after is not None and after.group(1) == "jsonc":
                    refused = _REFUSED.match(after.group(2))
                    code = refused.group(1) if refused else ""
            elif language == "yaml":
                pairs = _yaml_calls(yaml.safe_load(body))
                found.extend((f"{doc}:{line}", call, refused) for call, refused in pairs)
                continue
            elif language == "python":
                calls = _python_calls(body)
            elif language == "bash":
                calls = _bash_calls(body)
            else:
                continue
            found.extend((f"{doc}:{line}", call, code) for call in calls)
    return found


DOC_CALLS = _doc_calls()


def test_query_call_reference_shows_its_calls() -> None:
    shown = [where for where, _, _ in DOC_CALLS if where.startswith("docs/QUERY_CALL.md")]
    assert len(shown) >= 25, shown


@pytest.mark.parametrize(
    ("where", "call", "code"), DOC_CALLS, ids=[where for where, _, _ in DOC_CALLS]
)
def test_doc_call_is_a_valid_call(
    runtime: Runtime, where: str, call: dict[str, Any], code: str
) -> None:
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.query_call import read_call

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(call))
    assert not errors, f"{where}: {[(list(e.absolute_path), e.message[:120]) for e in errors[:3]]}"
    try:
        read_call(call, now=DOC_NOW)
    except SemanticLayerError as exc:
        assert exc.code == code, f"{where}: {exc.code}: {exc}"
        return
    config = runtime.package_config
    known = {
        row.id
        for rows in (
            config.measures,
            config.metric_recipes,
            config.dimensions,
            config.entities,
            config.temporal_roles,
            config.relationships,
        )
        for row in rows
    }
    if not set(_IDS.findall(json.dumps(call))) <= known:
        return
    report = runtime.validate(call, now=DOC_NOW)
    if code:
        assert [error["code"] for error in report["errors"]][:1] == [code], where
    else:
        assert report.get("ok") is True, f"{where}: {report.get('errors')!r}"
