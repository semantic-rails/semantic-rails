"""scripts/dev/verify_move.py accepts a pure move and rejects each way a move can change behavior."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "verify_move.py"
_spec = importlib.util.spec_from_file_location("verify_move", SCRIPT)
verify_move = importlib.util.module_from_spec(_spec)
sys.modules["verify_move"] = verify_move
_spec.loader.exec_module(verify_move)


def _d(files):
    return {k: textwrap.dedent(v).lstrip("\n") for k, v in files.items()}


BASE = _d(
    {
        "semantic_rails/__init__.py": "",
        "semantic_rails/a.py": '''
        """Module a."""

        from .c import helper

        __all__ = ["f", "g"]


        def _key(x):
            return x


        def f(x):
            # sort by key
            return sorted(x, key=_key)


        def g(x):
            return helper(x)
        ''',
        "semantic_rails/b.py": '''
        """Module b: its own _key, identical text."""


        def _key(x):
            return x


        def lazy():
            from .a import f

            return f
        ''',
        "semantic_rails/c.py": '''
        """Module c."""


        def helper(x):
            return x
        ''',
        "semantic_rails/shim.py": '''
        """A forwarding shim."""


        def f(*args, **kwargs):
            from .a import f as fn

            return fn(*args, **kwargs)
        ''',
        "tests/test_x.py": "",
    }
)
# The pure move: f and _key go from a.py to a new d.py; a.py imports them back.
MOVED = _d(
    {
        "semantic_rails/a.py": '''
        """Module a."""

        from .c import helper
        from .d import _key, f

        __all__ = ["f", "g"]


        def g(x):
            return helper(x)
        ''',
        "semantic_rails/d.py": '''
        """Module d."""


        def _key(x):
            return x


        def f(x):
            # sort by key
            return sorted(x, key=_key)
        ''',
    }
)


def _repo(tmp_path: Path, head: dict[str, str], base: dict[str, str] = BASE) -> str:
    def write(files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write(base)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    write(head)
    return str(tmp_path)


def _problems(
    tmp_path: Path, head: dict[str, str], base: dict[str, str] = BASE, **kwargs
) -> list[str]:
    return verify_move.verify(_repo(tmp_path, head, base), "HEAD", **kwargs)


def test_a_pure_move_passes(tmp_path):
    assert _problems(tmp_path, MOVED) == []


def test_a_forwarder_may_be_replaced_by_the_definition_it_forwarded_to(tmp_path):
    head = {**MOVED, "semantic_rails/shim.py": '"""A forwarding shim."""\n\nfrom .d import f\n'}
    assert _problems(tmp_path, head) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            {
                "semantic_rails/d.py": MOVED["semantic_rails/d.py"].replace(
                    "sorted(x, ", "sorted(list(x), "
                )
            },
            "changed statement",
            id="changed-body",
        ),
        pytest.param(
            {
                "semantic_rails/d.py": MOVED["semantic_rails/d.py"].replace(
                    "# sort by key", "# sort"
                )
            },
            "changed statement",  # a comment attached to a definition is part of its text
            id="changed-attached-comment",
        ),
        pytest.param(
            {
                "semantic_rails/d.py": '"""Module d."""\n\nfrom .b import _key\n\n\n'
                "def f(x):\n    # sort by key\n    return sorted(x, key=_key)\n",
                "semantic_rails/a.py": MOVED["semantic_rails/a.py"].replace(
                    "from .d import _key, f", "from .d import f\n\n\ndef _key(x):\n    return x\n"
                ),
            },
            "'_key' resolved to",  # same text, but b's _key instead of a's
            id="same-named-helper-from-the-wrong-module",
        ),
        pytest.param(
            {
                "semantic_rails/b.py": BASE["semantic_rails/b.py"].replace(
                    "from .a import f", "from .shim import f"
                )
            },
            None,  # the shim forwards to the same f: allowed
            id="lazy-import-to-a-forwarder-of-the-same-definition",
        ),
        pytest.param(
            {
                "semantic_rails/b.py": BASE["semantic_rails/b.py"].replace(
                    "from .a import f", "from .a import g as f"
                )
            },
            "lazy import 'f'",
            id="lazy-import-repointed",
        ),
        pytest.param(
            {"semantic_rails/d.py": MOVED["semantic_rails/d.py"] + "\n\nX = 1\n"},
            "new or changed statement",
            id="new-statement",
        ),
        pytest.param(
            {
                "semantic_rails/a.py": MOVED["semantic_rails/a.py"].replace(
                    "from .d import _key, f", "from .d import _key"
                )
            },
            "imports semantic_rails.a.f",  # the shim and b import f from a; the re-export is gone
            id="dropped-re-export",
        ),
        pytest.param(
            {"semantic_rails/shim.py": '"""A forwarding shim."""\n\nfrom .a import g as f\n'},
            "forwarder semantic_rails.shim.f was removed",
            id="forwarder-replaced-by-another-definition",
        ),
    ],
)
def test_each_way_a_move_can_change_behavior_fails(tmp_path, change, expected):
    problems = _problems(tmp_path, {**MOVED, **change})
    if expected is None:
        assert problems == []
    else:
        assert any(expected in p for p in problems), problems


def test_a_moved_statement_that_reads_its_module_name_fails(tmp_path):
    base = {
        **BASE,
        "semantic_rails/a.py": BASE["semantic_rails/a.py"].replace(
            "return x\n", "return __name__\n", 1
        ),
    }
    head = {
        **MOVED,
        "semantic_rails/d.py": MOVED["semantic_rails/d.py"].replace(
            "return x\n", "return __name__\n", 1
        ),
    }
    problems = _problems(tmp_path, head, base)
    assert any("reads ['__name__']" in p for p in problems), problems


def test_a_test_patch_on_the_old_path_fails(tmp_path):
    head = {
        **MOVED,
        "tests/test_x.py": 'from semantic_rails import a\n\n\ndef test(monkeypatch):\n    monkeypatch.setattr(a, "_key", str)\n',
    }
    problems = _problems(tmp_path, head)
    assert any("patches semantic_rails.a._key, which moved" in p for p in problems), problems
    assert any("f moved to semantic_rails.d but reads '_key'" in p for p in problems), problems


def test_a_patch_through_a_subpackage_import_fails(tmp_path):
    pkg = {"semantic_rails/pkg/__init__.py": "", "tests/test_x.py": ""}
    base = {**BASE, **pkg, "semantic_rails/pkg/m.py": BASE["semantic_rails/a.py"]}
    head = {
        **pkg,
        "semantic_rails/pkg/m.py": MOVED["semantic_rails/a.py"],
        "semantic_rails/pkg/d.py": MOVED["semantic_rails/d.py"],
        "tests/test_x.py": (
            "from semantic_rails.pkg import m\n\n\n"
            'def test(monkeypatch):\n    monkeypatch.setattr(m, "_key", str)\n'
        ),
    }
    problems = _problems(tmp_path, head, base)
    assert any("patches semantic_rails.pkg.m._key, which moved" in p for p in problems), problems


def test_a_forwarder_moved_to_another_package_depth_fails(tmp_path):
    pkg = {
        "semantic_rails/pkg/__init__.py": "",
        "semantic_rails/pkg/a.py": "def f(x):\n    return x\n",
    }
    head = {
        **pkg,
        "semantic_rails/shim.py": '"""A forwarding shim."""\n',
        "semantic_rails/pkg/shim.py": BASE["semantic_rails/shim.py"],
    }
    problems = _problems(tmp_path, head, {**BASE, **pkg})
    assert any("now forwards to a different definition" in p for p in problems), problems


@pytest.mark.parametrize(("head_module", "fails"), [("c", False), ("b", True)])
def test_a_module_alias_resolves_to_its_module(tmp_path, head_module, fails):
    def aliased(text, module):
        return text.replace("from .c import helper", f"from . import {module} as m").replace(
            "return helper(x)", "return m.helper(x)"
        )

    base = {**BASE, "semantic_rails/a.py": aliased(BASE["semantic_rails/a.py"], "c")}
    head = {**MOVED, "semantic_rails/a.py": aliased(MOVED["semantic_rails/a.py"], head_module)}
    problems = _problems(tmp_path, head, base)
    assert any("'m' resolved to" in p for p in problems) is fails, problems


def test_comment_edits_fail_unless_allowed(tmp_path):
    head = {**MOVED, "semantic_rails/c.py": BASE["semantic_rails/c.py"] + "\n# a new note\n"}
    assert any("comment edits" in p for p in _problems(tmp_path, head))
    assert _problems(tmp_path / "again", head, allow_comment_edits=True) == []
