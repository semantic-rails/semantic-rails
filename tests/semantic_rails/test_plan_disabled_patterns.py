"""A3+I1+I8: per-package ``planner.disabled_patterns`` config.

Loaded from the package YAML under a ``planner:`` sub-block:

::

    package:
      package_id: jaffle_shop
      ...
      planner:
        disabled_patterns:
          - qualified_metric_rollup

The orchestrator filters ``PATTERNS`` by name before iterating; the
field defaults to ``[]`` so existing packages see no behavior change.

These tests load a real (tweaked) package YAML via a fixture rather
than mutating ``runtime.config`` in-memory — that path exercises the
config loader and survives any future Runtime-side caching of
config-derived state (improvement #1).
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import resolve_repo_path
from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.orchestrator import _active_patterns
from semantic_rails.planner.patterns import PATTERNS
from semantic_rails.runtime import Runtime

_QUALIFIED_INTENT = "top 3 stores by revenue with at least 4 distinct customers"


@pytest.fixture()
def runtime_with_planner_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[list[str] | None], Runtime]:
    """Build a Runtime from the jaffle_shop package with an injected
    ``planner:`` block in the package YAML.

    Mirrors the production load path so the test exercises the same
    loader the runtime would on a real deploy.
    """

    def _build(disabled_patterns: list[str] | None) -> Runtime:
        package_id = "jaffle_shop"
        run_dir = tmp_path / package_id / uuid.uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=True)
        src_dir = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
        target_dir = run_dir / package_id
        shutil.copytree(src_dir, target_dir)

        package_path = target_dir / "package.yml"
        raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
        pkg = dict(raw.get("package", {}) or {})
        default_db = target_dir / f"{package_id}.duckdb"
        pkg["default_db"] = f"{package_id}.duckdb"
        if disabled_patterns is not None:
            pkg["planner"] = {"disabled_patterns": list(disabled_patterns)}
        raw["package"] = pkg
        package_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

        seeded = Path(resolve_repo_path("data/jaffle_shop.duckdb"))
        if seeded.exists():
            shutil.copy2(seeded, default_db)
        monkeypatch.setattr(
            config_module, "list_package_paths", lambda: {package_id: str(target_dir)}
        )
        return Runtime(package_id)

    return _build


def test_disabled_patterns_defaults_to_empty(runtime_with_planner_block) -> None:
    """No planner block in the package YAML means the full registry is active."""

    runtime = runtime_with_planner_block(None)
    try:
        assert runtime.config.package.planner.disabled_patterns == []
        assert _active_patterns(runtime) == list(PATTERNS)
    finally:
        runtime.close()


def test_disabled_pattern_filters_plan_surface(runtime_with_planner_block) -> None:
    """A pattern listed in ``package.planner.disabled_patterns`` is
    skipped by both compose and the public plan payload.
    """

    runtime = runtime_with_planner_block(["qualified_metric_rollup"])
    try:
        active = {p.name for p in _active_patterns(runtime)}
        assert "qualified_metric_rollup" not in active

        result = compose(runtime, _QUALIFIED_INTENT)
        assert result.pattern != "qualified_metric_rollup"

        planned = plan_payload(runtime, intent=_QUALIFIED_INTENT, detail="full")
        assert planned["best"]["pattern"] != "qualified_metric_rollup"
    finally:
        runtime.close()


def test_unknown_disabled_pattern_names_are_silently_ignored(
    runtime_with_planner_block,
) -> None:
    """A name that doesn't correspond to any registered pattern must
    not raise — it's just a no-op filter.
    """

    runtime = runtime_with_planner_block(["this_pattern_does_not_exist"])
    try:
        assert len(_active_patterns(runtime)) == len(PATTERNS)
        result = compose(runtime, _QUALIFIED_INTENT)
        assert result.pattern == "qualified_metric_rollup"
    finally:
        runtime.close()
