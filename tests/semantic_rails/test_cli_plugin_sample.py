"""The command-registration hook, proved with a separately installed sample plugin.

The sample in ``fixtures/cli_plugin`` is laid out the way an installer leaves
a distribution: the package plus a ``.dist-info`` folder whose
``entry_points.txt`` names it in the ``semantic_rails.cli`` group. The CLI runs
in a subprocess with that folder on ``PYTHONPATH``, so entry-point discovery is
the real one, not a monkeypatch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "cli_plugin" / "semantic_rails_cloud_link_sample"
)


def _install(site: Path, distribution: str, module: str, source: Path | str, entry: str) -> None:
    """Lay out one distribution as an installer would: its module and its .dist-info."""

    if isinstance(source, Path):
        shutil.copytree(source, site / module)
    else:
        (site / module).mkdir(parents=True)
        (site / module / "__init__.py").write_text(source, encoding="utf-8")
    info = site / f"{distribution.replace('-', '_')}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        f"[semantic_rails.cli]\n{entry} = {module}:register\n", encoding="utf-8"
    )


@pytest.fixture
def site(tmp_path: Path) -> Path:
    folder = tmp_path / "site-packages"
    folder.mkdir()
    _install(folder, "semantic-rails-cloud-link-sample", SAMPLE.name, SAMPLE, "cloud-link-sample")
    return folder


def _cli(site: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        env={
            **os.environ,
            "PYTHONPATH": f"{site}{os.pathsep}{REPO_ROOT}",
            "SEMANTIC_RAILS_HOME": str(site.parent / "home"),
            **env,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_sample_plugin_adds_a_command_group_an_extension_and_an_import_source(
    site: Path,
) -> None:
    assert "cloud" in _cli(site, "--help").stdout

    linked = _cli(site, "cloud", "link", "--package", "jaffle_shop", "--workspace", "acme")
    assert linked.returncode == 0, linked.stderr
    assert json.loads(linked.stdout)["workspace"] == "acme"
    assert json.loads(linked.stdout)["package"]["id"] == "jaffle_shop"

    extended = _cli(site, "mcp", "setup", "--package", "jaffle_shop", "--sample-client", "zed")
    assert extended.returncode == 0, extended.stderr
    assert json.loads(extended.stdout)["client"] == "zed"

    imported = _cli(
        site,
        "import",
        "--from",
        "sample-json",
        "--source",
        "defs.json",
        "--output",
        "out",
        "--package-id",
        "demo",
    )
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["package_id"] == "demo"


def test_an_extended_command_still_runs_its_built_in_handler(site: Path) -> None:
    with_plugin = _cli(site, "mcp", "setup", "--package", "jaffle_shop", "--json")
    without = _cli(
        site, "mcp", "setup", "--package", "jaffle_shop", "--json", SEMANTIC_RAILS_CLI_PLUGINS="0"
    )

    assert with_plugin.returncode == 0, with_plugin.stderr
    assert json.loads(with_plugin.stdout) == json.loads(without.stdout)


def test_plugins_can_be_switched_off(site: Path) -> None:
    proc = _cli(
        site,
        "cloud",
        "link",
        "--package",
        "jaffle_shop",
        "--workspace",
        "acme",
        SEMANTIC_RAILS_CLI_PLUGINS="0",
    )

    assert proc.returncode == 2
    assert "invalid choice: 'cloud'" in proc.stderr


def test_a_broken_or_clashing_plugin_is_skipped_and_the_rest_still_load(site: Path) -> None:
    _install(
        site,
        "broken-sample",
        "broken_sample",
        "def register(registry):\n    raise RuntimeError('boom')\n",
        "broken",
    )
    _install(
        site,
        "clashing-sample",
        "clashing_sample",
        "def register(registry):\n    registry.add_command(('packages',), lambda args: None)\n",
        "clashing",
    )

    proc = _cli(site, "cloud", "link", "--package", "jaffle_shop", "--workspace", "acme")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["workspace"] == "acme"
    assert "skipped CLI plugin 'broken' from broken-sample: RuntimeError: boom" in proc.stderr
    assert (
        "skipped CLI plugin 'clashing' from clashing-sample: CommandRegistrationError"
        in proc.stderr
    )


def test_registering_a_plugin_prints_nothing(site: Path) -> None:
    proc = _cli(site, "packages")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["packages"]  # stdout stays pure JSON
    assert proc.stderr == ""
