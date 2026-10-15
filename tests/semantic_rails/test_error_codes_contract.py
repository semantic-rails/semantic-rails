"""Contract test: the code table in ``semantic_rails.issues`` and the codes the engine writes agree.

Every code written anywhere — a ``SemanticLayerError``, a ``code=`` argument, a ``"code"`` key
of an issue, a hint or a report row, a helper that takes a code positionally, or the default of
a helper's ``code`` parameter — is listed in ``issues.CODES``, and every listed code is written
somewhere.

Catches:
- Dead codes (declared but never written) — this kept happening pre-launch.
- Typos and unregistered warnings or hints (written code not in the table).

Walks `semantic_rails/` source files via AST.
"""

from __future__ import annotations

import ast
import re
from itertools import product
from pathlib import Path

import pytest

from semantic_rails.errors import ERROR_CODES
from semantic_rails.issues import CODES, REFUSAL, severity
from semantic_rails.mcp import _UNKNOWN_ARG_WARNING_CODE

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOT = REPO_ROOT / "semantic_rails"
CODE = re.compile(r"^[A-Z][A-Z0-9_]+$")
# Helpers that take a code positionally, with its argument index.
POSITIONAL = {"SemanticLayerError": 0, "issue": 0, "_error_payload": 0, "_test_result": 2, "add": 1}


def _literal_strings(node: ast.AST) -> list[str]:
    """Expand only strings whose alternatives are all statically known."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _literal_strings(node.body), _literal_strings(node.orelse)
        return body + orelse if body and orelse else []
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        return _literal_strings(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = [_literal_strings(value) for value in node.values]
        return ["".join(values) for values in product(*parts)]
    return []


def _collect_written_codes() -> dict[str, list[str]]:
    """Return {code -> [file:line, ...]} for every code the engine writes.

    Sites: ``SemanticLayerError("CODE", ...)`` and the other positional helpers (``raise`` or
    ``return``, the deferred-raise idiom), ``super().__init__("CODE", ...)`` in
    SemanticLayerError subclasses, ``code="CODE"`` arguments, ``{"code": "CODE"}`` keys, and a
    ``code`` parameter's default (a helper called without one writes it). Literal conditional
    expressions and f-strings count each possible code.
    """
    written: dict[str, list[str]] = {}

    def add(node: ast.AST, value: ast.AST) -> None:
        site = f"{py_file.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', 0)}"
        for code in _literal_strings(value):
            if CODE.match(code):
                written.setdefault(code, []).append(site)

    for py_file in SOURCE_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        subclass_initializers: set[ast.Call] = set()
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef) or not any(
                isinstance(base, ast.Name) and base.id == "SemanticLayerError" for base in cls.bases
            ):
                continue
            for member in cls.body:
                if not isinstance(member, ast.FunctionDef) or member.name != "__init__":
                    continue
                for call in ast.walk(member):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "__init__"
                        and isinstance(call.func.value, ast.Call)
                        and isinstance(call.func.value.func, ast.Name)
                        and call.func.value.func.id == "super"
                    ):
                        subclass_initializers.add(call)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = node.args
                positional = [*arguments.posonlyargs, *arguments.args]
                defaults = [
                    *zip(
                        positional[len(positional) - len(arguments.defaults) :],
                        arguments.defaults,
                        strict=True,
                    ),
                    *zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True),
                ]
                for argument, default in defaults:
                    if argument.arg == "code" and default is not None:
                        add(node, default)
                continue
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "code":
                        add(node, value)
                continue
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "code":
                    add(node, keyword.value)
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            index = 0 if node in subclass_initializers else POSITIONAL.get(str(name))
            # A one-argument ``add`` is a set's, never a code helper's.
            if index is None or len(node.args) <= index or (name == "add" and len(node.args) < 2):
                continue
            add(node, node.args[index])
    return written


@pytest.mark.parametrize(
    ("construction", "expected"),
    [
        ('SemanticLayerError("POLICY_DENIED", "denied")', {"POLICY_DENIED"}),
        (
            'SemanticLayerError("POLICY_DENIED" if denied else "INVALID_QUERY", "error")',
            {"POLICY_DENIED", "INVALID_QUERY"},
        ),
        (
            "SemanticLayerError(f\"{'CUMULATIVE' if cumulative else 'WINDOWED'}"
            '_TIME_FILTER_UNSUPPORTED", "unsupported")',
            {"CUMULATIVE_TIME_FILTER_UNSUPPORTED", "WINDOWED_TIME_FILTER_UNSUPPORTED"},
        ),
        ('SemanticLayerError(f"{prefix}_UNSUPPORTED", "error")', set()),
        ('SemanticLayerError("POLICY_DENIED" if denied else unknown, "error")', set()),
        ('other("POLICY_DENIED", "error")', set()),
    ],
)
def test_collect_raised_codes_with_literal_alternatives(
    tmp_path, monkeypatch, construction, expected
):
    source_root = tmp_path / "semantic_rails"
    source_root.mkdir()
    (source_root / "sample.py").write_text(f"error = {construction}\nraise error\n")
    monkeypatch.setattr(__name__ + ".REPO_ROOT", tmp_path)
    monkeypatch.setattr(__name__ + ".SOURCE_ROOT", source_root)
    written = _collect_written_codes()
    assert written == {code: ["semantic_rails/sample.py:1"] for code in expected}


@pytest.mark.parametrize(
    ("construction", "expected"),
    [
        ('{"code": "NO_DATA_YET", "severity": "warning"}', {"NO_DATA_YET"}),
        (
            'semantic_issue(code="ROUTE_CHOSEN_BY_QUERY", severity="info")',
            {"ROUTE_CHOSEN_BY_QUERY"},
        ),
        (
            '_test_result(test_id, False, "SNAPSHOT_MISMATCH", "rows differ", {})',
            {"SNAPSHOT_MISMATCH"},
        ),
        ('{"code": "not a code"}', set()),
        ("seen.add('NOT_A_CODE_SITE')", set()),
        # A helper called without a code writes its default.
        (
            'None\ndef refuse(path, message, code="PLAN_DRAFT_INVALID"): pass',
            {"PLAN_DRAFT_INVALID"},
        ),
        ('None\ndef refuse(path, *, code="INVALID_QUERY"): pass', {"INVALID_QUERY"}),
        ('None\ndef refuse(path, kind="NOT_A_CODE_SITE"): pass', set()),
    ],
)
def test_collect_written_codes_from_every_site(tmp_path, monkeypatch, construction, expected):
    source_root = tmp_path / "semantic_rails"
    source_root.mkdir()
    (source_root / "sample.py").write_text(f"value = {construction}\n")
    monkeypatch.setattr(__name__ + ".REPO_ROOT", tmp_path)
    monkeypatch.setattr(__name__ + ".SOURCE_ROOT", source_root)
    assert set(_collect_written_codes()) == expected


@pytest.fixture(scope="module")
def written_codes() -> dict[str, list[str]]:
    written = _collect_written_codes()
    # The MCP adapter names each open tool's unknown-argument warning after the tool.
    for code in _UNKNOWN_ARG_WARNING_CODE.values():
        written.setdefault(code, []).append("semantic_rails/mcp.py")
    return written


def test_every_registered_code_is_written(written_codes):
    dead = sorted(CODES.keys() - written_codes.keys())
    assert not dead, (
        f"issues.CODES lists codes nothing writes (dead surface): {dead}. "
        f"Either wire them up or drop them from semantic_rails/issues.py."
    )


def test_every_written_code_is_registered(written_codes):
    unknown = sorted(written_codes.keys() - CODES.keys())
    if unknown:
        sample_sites = {code: written_codes[code][:2] for code in unknown}
        pytest.fail(
            f"The engine writes codes semantic_rails/issues.py does not list: {unknown}. "
            f"Sample sites: {sample_sites}. Register each with its kind, or fix the typo."
        )


def test_error_codes_are_the_registered_refusals():
    assert {code for code, kind in CODES.items() if kind == REFUSAL} == ERROR_CODES
    assert {severity(code) for code in ERROR_CODES} == {"error"}


def test_rollup_unsafe_has_a_compiler_raise_site(written_codes):
    assert any(
        site.startswith(("semantic_rails/compiler.py:", "semantic_rails/compiler_parts/"))
        for site in written_codes.get("ROLLUP_UNSAFE", [])
    )
