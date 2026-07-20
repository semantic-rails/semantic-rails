"""Contract test: every code in ERROR_CODES has a raise site, and every
raise-site code lives in ERROR_CODES.

Catches:
- Dead codes (declared but never raised) — this kept happening pre-launch.
- Typos in raise statements (referenced code not in the set).

Walks `semantic_rails/` source files and grep-matches the canonical
`raise SemanticLayerError("CODE", ...)` shape via AST.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from semantic_rails.errors import ERROR_CODES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOT = REPO_ROOT / "semantic_rails"


def _collect_raised_codes() -> dict[str, list[str]]:
    """Return {code -> [file:line, ...]} for every SemanticLayerError("CODE", ...)
    construction site. Counts both `raise SemanticLayerError(...)` and
    `return SemanticLayerError(...)` patterns — the latter is the deferred-raise
    idiom (e.g., runtime.py:124 returns an error for the caller to raise).
    """
    raised: dict[str, list[str]] = {}
    for py_file in SOURCE_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
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
            if name != "SemanticLayerError":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            code = node.args[0].value
            if not isinstance(code, str):
                continue
            site = f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}"
            raised.setdefault(code, []).append(site)
    return raised


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
