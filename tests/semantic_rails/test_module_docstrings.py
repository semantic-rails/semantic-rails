"""Module-level docstring contract.

Audit finding I2: 34 modules in ``semantic_rails/`` shipped without
docstrings. For an agent-first library where the runtime surface is
the intended audience for introspection, an undocumented module list
is the wrong place to under-invest. This test pins the contract: every
top-level module in ``semantic_rails/`` must declare its responsibility
and main entry points.

Implementation submodules under ``semantic_rails/*_parts/`` are
deliberately exempt — they're treated as private and the parent
public modules carry their documentation.
"""

from __future__ import annotations

import importlib
import pkgutil

import semantic_rails

PUBLIC_MODULES = sorted(
    name
    for finder, name, ispkg in pkgutil.iter_modules(semantic_rails.__path__)
    if not ispkg  # skip *_parts subpackages
)


def test_every_public_module_has_a_docstring():
    missing: list[str] = []
    for name in PUBLIC_MODULES:
        module = importlib.import_module(f"semantic_rails.{name}")
        doc = (module.__doc__ or "").strip()
        if not doc:
            missing.append(name)
    assert not missing, (
        "Modules missing top-level docstrings (agent-first lib should "
        f"document its public surface): {missing}"
    )


def test_module_docstrings_are_non_trivial():
    """A one-word docstring would defeat the purpose — require a real
    sentence so the documentation actually carries information."""
    too_short: list[tuple[str, int]] = []
    for name in PUBLIC_MODULES:
        module = importlib.import_module(f"semantic_rails.{name}")
        doc = (module.__doc__ or "").strip()
        if len(doc) < 60:
            too_short.append((name, len(doc)))
    assert not too_short, (
        "Module docstrings should describe responsibility + entry points "
        f"(min 60 chars): {too_short}"
    )


def test_init_module_has_docstring():
    """``semantic_rails/__init__.py`` is also part of the public surface."""
    assert semantic_rails.__doc__, "semantic_rails/__init__.py needs a docstring"
    assert len(semantic_rails.__doc__.strip()) >= 60
