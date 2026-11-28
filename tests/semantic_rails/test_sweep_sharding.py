"""Where CI runs the whole visible-view sweep, and where it runs a seeded sixth.

The merge queue runs the whole sweep on Python 3.12 only. Its 3.11 and 3.14 shards run
the sixth that the merge group's head commit picks. The nightly runs the whole sweep on
every supported Python, so a break on one version that the merge queue's sixth missed
still shows up within a day.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import test_sharding

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
PR_HEAD = "a" * 40
MERGE_GROUP_HEAD = "b" * 40
_CONTEXT = re.compile(r"\b(?:github|matrix)\.[\w.-]+")


def _evaluate(expression: str, context: dict[str, str | None]):
    """Evaluate a constrained Actions expression. Every context it names must be given
    (None for a value the event lacks), so a new input can't slip past these pins."""
    body = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    translated = _CONTEXT.sub(lambda match: repr(context[match.group(0)]), body)
    translated = translated.replace("&&", " and ").replace("||", " or ")
    return eval(translated, {"__builtins__": {}, "fromJSON": json.loads})


def _context(event: str, python: str | None = None) -> dict[str, str | None]:
    return {
        "github.event_name": event,
        "github.event.pull_request.head.sha": PR_HEAD if event == "pull_request" else None,
        "github.event.merge_group.head_sha": MERGE_GROUP_HEAD if event == "merge_group" else None,
        "matrix.python-version": python,
    }


def _supported() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    prefix = "Programming Language :: Python :: 3."
    return sorted(
        classifier.removeprefix("Programming Language :: Python :: ")
        for classifier in project["project"]["classifiers"]
        if classifier.startswith(prefix)
    )


def _seeds(event: str) -> dict[str, str]:
    """Each backend Python this event runs, and the SR_SWEEP_SEED its shards get."""
    backend = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]["backend"]
    versions = _evaluate(backend["strategy"]["matrix"]["python-version"], _context(event))
    seed = backend["env"]["SR_SWEEP_SEED"]
    return {python: _evaluate(seed, _context(event, python)) for python in versions}


@pytest.mark.parametrize(
    "event, seeds",
    [
        # A pull request runs 3.12 with the sixth its head commit picks.
        ("pull_request", {"3.12": PR_HEAD}),
        # The merge queue runs the whole sweep on 3.12 only, and a sixth on 3.11 and 3.14.
        ("merge_group", {"3.11": MERGE_GROUP_HEAD, "3.12": "", "3.14": MERGE_GROUP_HEAD}),
        # A manual run takes the whole sweep on every Python.
        ("workflow_dispatch", {"3.11": "", "3.12": "", "3.13": "", "3.14": ""}),
    ],
)
def test_each_ci_event_runs_the_sweep_it_should(event, seeds, monkeypatch):
    assert _seeds(event) == seeds
    for seed in seeds.values():
        monkeypatch.setenv("SR_SWEEP_SEED", seed)
        chosen = test_sharding.sweep_slice()
        assert (chosen is None) == (seed == "")
        assert chosen is None or 0 <= chosen < test_sharding.SWEEP_SLICES


def test_the_merge_queue_runs_the_whole_sweep_once():
    whole = [python for python, seed in _seeds("merge_group").items() if not seed]
    assert whole == ["3.12"]


def test_the_nightly_runs_the_whole_sweep_on_every_supported_python():
    workflow = yaml.safe_load((WORKFLOWS / "release-readiness.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    assert trigger["schedule"], "the release-readiness workflow is the nightly"
    suite = workflow["jobs"]["suite"]
    versions = _evaluate(suite["strategy"]["matrix"]["python-version"], _context("schedule"))
    assert versions == _supported()
    # No seed anywhere, so every sweep case runs, and the run takes the whole suite.
    assert "SR_SWEEP_SEED" not in workflow.get("env", {})
    assert "SR_SWEEP_SEED" not in suite.get("env", {})
    runs = [step["run"] for step in suite["steps"] if "pytest" in step.get("run", "")]
    assert runs == ["uv run pytest -q tests/semantic_rails -n auto --dist worksteal"]
    assert all("SR_SWEEP_SEED" not in step.get("env", {}) for step in suite["steps"])
