"""Tests bound every wait, and a hanging test names itself with its stack."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[2]
SUBPROCESS_CALLS = {"run", "check_output", "check_call", "call"}


def unbounded_waits(source: str) -> list[tuple[int, str]]:
    """Return (line, call) for each wait in `source` that has no bound."""
    tree = ast.parse(source)
    modules, functions = {"subprocess"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "subprocess"}
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            functions |= {a.asname or a.name for a in node.names if a.name in SUBPROCESS_CALLS}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func, keywords = node.func, {keyword.arg for keyword in node.keywords}
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if isinstance(func, ast.Attribute):
            module = func.value.id if isinstance(func.value, ast.Name) else None
            is_subprocess = module in modules and name in SUBPROCESS_CALLS
        else:
            is_subprocess = name in functions
        if (
            (is_subprocess and "timeout" not in keywords)
            or (name == "urlopen" and "timeout" not in keywords and len(node.args) < 3)
            or (name == "communicate" and "timeout" not in keywords and len(node.args) < 2)
            or (
                isinstance(func, ast.Attribute)
                and name in {"join", "wait"}
                and not node.args
                and not node.keywords
            )
        ):
            found.append((node.lineno, f"{name}()"))
    return found


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("import subprocess\nsubprocess.run(['git'])", True),
        ("import subprocess\nsubprocess.check_output(['git'], text=True)", True),
        ("import subprocess as sp\nsp.check_call(['git'])", True),
        ("from subprocess import call\ncall(['git'])", True),
        ("import subprocess\nsubprocess.run(['git'], timeout=120)", False),
        ("from subprocess import run\nrun(['git'], timeout=120)", False),
        ("def run(*args): pass\nrun(['git'])", False),
        ("urllib.request.urlopen(url)", True),
        ("urlopen(url, timeout=30)", False),
        ("urlopen(url, None, 30)", False),
        ("thread.join()", True),
        ("thread.join(timeout=60)", False),
        ("', '.join(names)", False),
        ("event.wait()", True),
        ("event.wait(timeout=5)", False),
        ("process.communicate()", True),
        ("process.communicate(timeout=5)", False),
    ],
)
def test_guard_flags_exactly_the_unbounded_waits(source: str, flagged: bool) -> None:
    assert bool(unbounded_waits(source)) is flagged


def test_every_wait_in_the_backend_tests_is_bounded() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line} {call}"
        for directory in ("tests/semantic_rails", "tests/mf2sr")
        for path in sorted((ROOT / directory).rglob("*.py"))
        for line, call in unbounded_waits(path.read_text(encoding="utf-8"))
    ]
    assert not found, "Bound these waits with a timeout:\n" + "\n".join(found)


@pytest.mark.timeout(90)
def test_a_hanging_test_under_xdist_names_itself_with_its_stack(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_sleepy=textwrap.dedent(
            """\
            import time


            def test_sleeps_past_the_limit():
                time.sleep(30)
            """
        )
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    result = pytester.runpytest_subprocess(
        *("-p", "xdist.plugin", "-p", "pytest_timeout", "-p", "no:cacheprovider", "-n", "2"),
        *("-o", "timeout=3", "-o", "timeout_method=thread", "-o", "faulthandler_timeout=1"),
        timeout=60,
    )
    output = result.stdout.str() + result.stderr.str()

    assert result.ret != 0, output
    assert "test_sleepy.py::test_sleeps_past_the_limit" in output
    # pytest's faulthandler dump (the `faulthandler_timeout` path) reaches the controller.
    assert "Timeout (0:00:01)!" in output, output
    assert 'test_sleepy.py", line 5 in test_sleeps_past_the_limit' in output, output
