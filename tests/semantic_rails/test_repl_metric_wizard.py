"""The REPL metric wizard, driven by prompt label through the public REPL entry point."""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from semantic_rails.cli import scaffold
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.repl import authoring, backend, shell


class _Script:
    """A prompt backend that answers by label and records the default each prompt offered.

    ``answers`` maps a prompt label to its answer; for a choice, the answer is a
    piece of the option's description. Unlisted prompts take their default.
    """

    name = "script"
    filters_long_lists = True

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.offered: dict[str, Any] = {}
        self.options: dict[str, list[str]] = {}
        self._asked: dict[str, int] = {}

    def _ask(self, label: str) -> Any:
        # A wizard that re-asks the same question forever fails the test instead of hanging.
        self._asked[label] = self._asked.get(label, 0) + 1
        assert self._asked[label] <= 5, f"asked {label!r} more than 5 times"
        return self.answers.get(label)

    def text(self, label: str, *, default: str = "") -> str:
        wanted = self._ask(label)
        self.offered[label] = default
        return default if wanted is None else str(wanted)

    def confirm(self, label: str, *, default: bool) -> bool:
        wanted = self._ask(label)
        self.offered[label] = default
        return default if wanted is None else bool(wanted)

    def choose(self, label: str, options: Sequence[tuple[str, str]], *, default: str = "") -> str:
        wanted = self._ask(label)
        described = dict(options)
        assert not default or default in described, f"{label!r} default {default!r} is not offered"
        self.offered[label] = described.get(default, "")
        self.options[label] = list(described.values())
        if wanted is None:
            return default
        return next(value for value, text in options if str(wanted) in text)

    def multi_choose(
        self, label: str, options: Sequence[tuple[str, str]], *, defaults: Collection[str] = ()
    ) -> list[str]:
        wanted = self._ask(label)
        self.offered[label] = list(defaults)
        self.options[label] = [text for _, text in options]
        if wanted is None:
            return list(defaults)
        return [value for value, text in options if value in wanted or text in wanted]

    def show_yaml(self, payload: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    yield
    backend.set_backend(None)


def _starter(tmp_path: Path) -> Path:
    """The scaffolded starter package: one events model with a default clock."""

    return Path(
        scaffold.create_project_report(
            package_id="shop", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _authored(folder: Path) -> dict[str, bytes]:
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in folder.rglob("*.yml")
        if ".architect" not in path.parts
    }


def _repl(project: Path, line: str, script: _Script | None, undo: list[Any]) -> None:
    if script is not None:
        backend.set_backend(script)
    shell._handle_repl_line(line, PackageReference(source_path=str(project)), undo_stack=undo)


NEW_TAX_MEASURE = {
    "Metric key": "tax_collected",
    "Measure to publish": "Create a new measure first",
    "Model to extend": "events - ",
    "Measure key": "tax",
    "Column or scalar expression (for example amount_cents / 100.0)": "tax_paid",
    "Result type": "Currency",
    "Create this measure?": True,
    "Create this metric?": True,
}
TAX_METRIC = "metrics/core/tax_collected.yml"
EVENTS = "models/core/events.yml"


@pytest.mark.parametrize("edited", [None, EVENTS, TAX_METRIC])
def test_one_undo_takes_back_an_inline_measure_and_its_metric_or_neither(
    tmp_path: Path, edited: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)
    undo: list[Any] = []
    _repl(project, "author metric", _Script(NEW_TAX_MEASURE), undo)

    events = yaml.safe_load((project / EVENTS).read_text("utf-8"))["model"]
    metric = yaml.safe_load((project / TAX_METRIC).read_text("utf-8"))["metrics"]
    assert events["measures"]["tax"]["expr"] == "tax_paid"
    assert metric["tax_collected"]["measure"] == "measure.shop.tax"
    assert len(undo) == 1 and undo[0].report["parse"]["ok"] is True

    if edited:
        # Undo checks both files before restoring either one.
        authored = (project / edited).read_bytes()
        (project / edited).write_bytes(authored + b"\n# external edit\n")
        after_edit = _authored(project)
        _repl(project, "undo", None, undo)
        assert "Undo was not applied" in capsys.readouterr().out
        assert len(undo) == 1 and _authored(project) == after_edit
        (project / edited).write_bytes(authored)

    _repl(project, "undo", None, undo)
    assert undo == [] and _authored(project) == before


@pytest.mark.parametrize("edit_before_cancel", [False, True])
def test_cancelling_the_metric_takes_back_its_inline_measure_unless_edited_since(
    tmp_path: Path, edit_before_cancel: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)

    class _EditThenCancel(_Script):
        def confirm(self, label: str, *, default: bool) -> bool:
            if label == "Create this metric?" and edit_before_cancel:
                (project / EVENTS).write_bytes((project / EVENTS).read_bytes() + b"# edit\n")
            return super().confirm(label, default=default)

    script = _EditThenCancel({**NEW_TAX_MEASURE, "Create this metric?": False})
    undo: list[Any] = []
    _repl(project, "author metric", script, undo)

    output = capsys.readouterr().out
    assert not (project / TAX_METRIC).exists()
    if edit_before_cancel:
        # Nothing is restored, the kept measure is named, and `undo` can still reach it.
        assert "no files changed" not in output
        assert f"conflict {project / EVENTS}" in output and "kept    measure `tax`" in output
        assert len(undo) == 1
        edited = (project / EVENTS).read_bytes()
        (project / EVENTS).write_bytes(edited.removesuffix(b"# edit\n"))
        _repl(project, "undo", None, undo)
    else:
        assert "Authoring cancelled; no files changed." in output
    assert undo == [] and _authored(project) == before


@pytest.mark.parametrize("create_metric", [True, False])
def test_an_unchanged_inline_measure_does_not_block_undo_or_cancel(
    tmp_path: Path, create_metric: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    _repl(project, "author measure", _Script(NEW_TAX_MEASURE), [])
    before_metric = _authored(project)
    undo: list[Any] = []
    manage_tax = {
        **NEW_TAX_MEASURE,
        "Manage and update this existing measure?": True,
        "Column or scalar expression (for example amount_cents / 100.0)": None,
        "Update this measure?": True,
        "Create this metric?": create_metric,
    }

    _repl(project, "author metric", _Script(manage_tax), undo)

    if create_metric:
        assert [part._snapshots == () for part in undo[0].parts] == [True, False]
        _repl(project, "undo", None, undo)
    else:
        assert "Authoring cancelled; no files changed." in capsys.readouterr().out
    assert undo == [] and _authored(project) == before_metric


@pytest.mark.parametrize("create_metric", [True, False])
@pytest.mark.parametrize("external_edit", [False, True])
def test_two_inline_measures_keep_an_edit_made_between_them(
    tmp_path: Path, external_edit: bool, create_metric: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)

    class _TwoMeasures(_Script):
        keys = iter(["tax", "shipping"])
        columns = iter(["tax_paid", "shipping_paid"])

        def text(self, label: str, *, default: str = "") -> str:
            if label == "Measure key":
                return next(self.keys)
            if label.startswith("Column or scalar expression"):
                return next(self.columns)
            return super().text(label, default=default)

        def confirm(self, label: str, *, default: bool) -> bool:
            if label == "Create this measure?" and self._asked.get(label) == 1 and external_edit:
                model = yaml.safe_load((project / EVENTS).read_text("utf-8"))
                model["model"]["label"] = "Externally labeled events"
                _write_yaml(project / EVENTS, model)
            return super().confirm(label, default=default)

    script = _TwoMeasures(
        {
            "Metric key": "tax_to_shipping",
            "Metric recipe": "Ratio",
            "Numerator": "Create a new measure first",
            "Denominator": "Create a new measure first",
            "Model to extend": "events - ",
            "Create this measure?": True,
            "Create this metric?": create_metric,
        }
    )
    undo: list[Any] = []
    _repl(project, "author metric", script, undo)

    metric_path = project / "metrics" / "core" / "tax_to_shipping.yml"
    assert metric_path.exists() is create_metric
    if not external_edit:
        if create_metric:
            _repl(project, "undo", None, undo)
        assert undo == [] and _authored(project) == before
        return
    # The edit between the two measures blocks restoring either one; both stay undoable.
    after = _authored(project)
    if not create_metric:
        output = capsys.readouterr().out
        assert f"conflict {project / EVENTS}" in output
        assert "kept    measure `tax`, measure `shipping`" in output
    _repl(project, "undo", None, undo)
    assert "Undo was not applied" in capsys.readouterr().out
    assert len(undo) == 1 and _authored(project) == after
    assert yaml.safe_load((project / EVENTS).read_text("utf-8"))["model"]["label"] == (
        "Externally labeled events"
    )


def test_a_ratio_refuses_the_same_input_twice(tmp_path: Path) -> None:
    project = _starter(tmp_path)
    before = _authored(project)
    script = _Script(
        {
            "Metric key": "total_per_total",
            "Metric recipe": "Ratio",
            "Numerator": "total_amount - ",
            # Creating the denominator can land on the numerator's own key.
            "Denominator": "Create a new measure first",
            "Model to extend": "events - ",
            "Measure key": "total_amount",
            "Manage and update this existing measure?": True,
            "Update this measure?": True,
        }
    )

    with pytest.raises(SemanticLayerError, match="two distinct measures or metrics"):
        _repl(project, "author metric", script, [])
    assert _authored(project) == before


@pytest.mark.parametrize("interrupt_after", ["Measure", "Metric"])
def test_an_interruption_after_any_commit_restores_every_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupt_after: str,
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)
    interrupted = []

    def interrupt_on_success(*args: Any, **kwargs: Any) -> None:
        if not interrupted and args and str(args[0]).startswith(f"[ok] {interrupt_after}"):
            # The real project transaction has already written the files.
            interrupted.append(_authored(project) != before)
            raise KeyboardInterrupt
        print(*args, **kwargs)

    monkeypatch.setattr(authoring, "print", interrupt_on_success, raising=False)
    undo: list[Any] = []
    _repl(project, "author metric", _Script(NEW_TAX_MEASURE), undo)

    assert interrupted == [True]
    assert undo == [] and _authored(project) == before
    assert "Authoring cancelled; no files changed." in capsys.readouterr().out
