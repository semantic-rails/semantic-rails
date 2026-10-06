"""Contract test: every code in ERROR_CODES has a raise site, and every
raise-site code lives in ERROR_CODES.

Catches:
- Dead codes (declared but never raised) — this kept happening pre-launch.
- Typos in raise statements (referenced code not in the set).

Walks `semantic_rails/` source files and matches error constructions and
subclass initialization via AST.
"""

from __future__ import annotations

import ast
from itertools import product
from pathlib import Path

import pytest

from semantic_rails.errors import ERROR_CODES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOT = REPO_ROOT / "semantic_rails"


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


def _collect_raised_codes() -> dict[str, list[str]]:
    """Return {code -> [file:line, ...]} for every SemanticLayerError("CODE", ...)
    construction site. Counts both `raise SemanticLayerError(...)` and
    `return SemanticLayerError(...)` patterns — the latter is the deferred-raise
    idiom (e.g., runtime.py:124 returns an error for the caller to raise).
    Also counts `super().__init__("CODE", ...)` in SemanticLayerError subclasses.
    Literal conditional expressions and f-strings count each possible code.
    """
    raised: dict[str, list[str]] = {}
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
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name != "SemanticLayerError" and node not in subclass_initializers:
                continue
            if not node.args:
                continue
            site = f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}"
            for code in _literal_strings(node.args[0]):
                raised.setdefault(code, []).append(site)
    return raised


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
    raised = _collect_raised_codes()
    assert raised == {code: ["semantic_rails/sample.py:1"] for code in expected}


@pytest.fixture(scope="module")
def raised_codes() -> dict[str, list[str]]:
    return _collect_raised_codes()


def test_every_declared_code_has_a_raise_site(raised_codes):
    dead = sorted(ERROR_CODES - raised_codes.keys())
    assert not dead, (
        f"ERROR_CODES contains codes with no raise site (dead surface): {dead}. "
        f"Either wire them up to a runtime check or drop them from errors.py."
    )


def test_every_raise_site_uses_a_declared_code(raised_codes):
    typos = sorted(raised_codes.keys() - ERROR_CODES)
    if typos:
        sample_sites = {code: raised_codes[code][:2] for code in typos}
        pytest.fail(
            f"Raise statements use codes not declared in ERROR_CODES: {typos}. "
            f"Sample sites: {sample_sites}. Add to ERROR_CODES or fix the typo."
        )


def test_rollup_unsafe_has_a_compiler_raise_site(raised_codes):
    assert any(
        site.startswith("semantic_rails/compiler.py:")
        for site in raised_codes.get("ROLLUP_UNSAFE", [])
    )
