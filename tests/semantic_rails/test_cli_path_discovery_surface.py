"""Discovery surface for ``--path`` packages — catalog/discover/inspect.

Query-error recovery hints tell authors to "call discover or catalog",
but those subcommands historically accepted only the registered
``--package`` ids, so authors of ``--path`` packages had no discovery
surface at all. These tests drive the real argparse wiring (via
``python -m semantic_rails``) against a freshly init'd package to pin
``--path`` support on the read-side commands.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if not proc.stdout.strip():
        raise AssertionError(f"CLI produced no stdout. args={args!r} stderr={proc.stderr!r}")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def init_package_yml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("path_discovery") / "my_shop"
    payload = _run_cli("init", "--output", str(target), "--package-id", "my_shop")
    assert payload["ok"] is True, payload
    return target / "package.yml"


def test_catalog_supports_path(init_package_yml: Path) -> None:
    payload = _run_cli("catalog", "--path", str(init_package_yml))
    assert payload["ok"] is True, payload
    catalog = payload["catalog"]
    blob = json.dumps(catalog)
    assert "metric.my_shop.revenue_usd" in blob, (
        "catalog --path must surface the init'd package's own objects"
    )


def test_discover_supports_path(init_package_yml: Path) -> None:
    payload = _run_cli("discover", "--path", str(init_package_yml), "--terms", "revenue")
    assert payload["ok"] is True, payload
    ids = [str(entry.get("id", "")) for entry in payload.get("metrics", [])] + [
        str(entry.get("id", "")) for entry in payload.get("measures", [])
    ]
    assert any("my_shop" in object_id and "revenue" in object_id for object_id in ids), (
        f"discover --path must rank the init'd package's objects; got: {ids}"
    )


def test_inspect_supports_path(init_package_yml: Path) -> None:
    payload = _run_cli(
        "inspect", "--path", str(init_package_yml), "--object-id", "metric.my_shop.revenue_usd"
    )
    assert payload["ok"] is True, payload
    blob = json.dumps(payload)
    assert "metric.my_shop.revenue_usd" in blob


def test_remaining_read_side_commands_accept_path() -> None:
    """valid-values, plan, and build-options must also expose --path —
    a cheap parser-level pin that fails fast if the flag regresses."""
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    for command in ("valid-values", "plan", "build-options", "catalog", "discover", "inspect"):
        help_proc = subprocess.run(
            [sys.executable, "-m", "semantic_rails", command, "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert help_proc.returncode == 0, help_proc.stderr
        assert "--path" in help_proc.stdout, f"{command} must accept --path"
