"""Other installed packages add CLI commands through the ``semantic_rails.cli`` entry points.

Each plugin is laid out the way an installer leaves a distribution (a module
plus a ``.dist-info`` naming it in the group), and the CLI runs in a subprocess,
so entry-point discovery is the real one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

GOOD = """\
def add_commands(sub):
    hello = sub.add_parser("hello", help="Say hello")
    hello.add_argument("--name", default="world")
    hello.set_defaults(func=lambda args: print(f"hello {args.name}"))
"""
BROKEN = "def add_commands(sub):\n    sub.add_parser('packages')\n"  # a built-in name
HALF_BUILT = (
    "def add_commands(sub):\n    sub.add_parser('halfbuilt')\n    raise RuntimeError('boom')\n"
)


def _install(site: Path, name: str, source: str) -> None:
    (site / f"{name}.py").write_text(source, encoding="utf-8")
    info = site / f"{name}-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text(f"[semantic_rails.cli]\n{name} = {name}:add_commands\n")


def _cli(site: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        env={**os.environ, "PYTHONPATH": str(site), "SEMANTIC_RAILS_HOME": str(site), **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def test_an_installed_plugin_adds_a_command_and_a_broken_one_is_skipped(tmp_path: Path) -> None:
    _install(tmp_path, "good_plugin", GOOD)
    _install(tmp_path, "broken_plugin", BROKEN)
    _install(tmp_path, "unimportable_plugin", "import no_such_module_for_this_test\n")

    hello = _cli(tmp_path, "hello", "--name", "rails")
    assert (hello.returncode, hello.stdout) == (0, "hello rails\n"), hello.stderr
    assert "skipped CLI plugin 'broken_plugin'" in hello.stderr  # the exception type varies
    assert "conflicting subparser: packages" in hello.stderr
    assert "skipped CLI plugin 'unimportable_plugin': ModuleNotFoundError" in hello.stderr

    packages = _cli(tmp_path, "packages")  # the built-in command still wins
    assert packages.returncode == 0 and '"packages"' in packages.stdout, packages.stderr

    off = _cli(tmp_path, "hello", SEMANTIC_RAILS_CLI_PLUGINS="0")
    assert off.returncode == 2 and "invalid choice: 'hello'" in off.stderr
    assert "skipped" not in off.stderr


def test_a_plugin_that_fails_midway_leaves_no_half_built_command(tmp_path: Path) -> None:
    _install(tmp_path, "half_plugin", HALF_BUILT)

    proc = _cli(tmp_path, "halfbuilt")

    assert proc.returncode == 2 and "invalid choice: 'halfbuilt'" in proc.stderr
    assert "skipped CLI plugin 'half_plugin': RuntimeError: boom" in proc.stderr
    assert "halfbuilt" not in _cli(tmp_path, "--help").stdout
