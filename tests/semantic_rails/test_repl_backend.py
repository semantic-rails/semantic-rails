"""Prompt backends for the REPL wizards (``semantic_rails.repl.backend``).

Plain ``input()`` prompts are the stdlib fallback; arrow-key pickers come with
``semantic-rails[repl]``. Wizards ask through one interface, so both backends
drive the same flows and a cancel never writes files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from rich.console import Console

from semantic_rails.cli import scaffold
from semantic_rails.errors import SemanticLayerError
from semantic_rails.repl import authoring, backend, shell
from semantic_rails.repl.backend import Cancelled, PickerBackend, PlainBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
OPTIONS = [("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie")]


@pytest.fixture(autouse=True)
def _unpinned() -> Iterator[None]:
    backend.set_backend(None)
    yield
    backend.set_backend(None)


def _answers(monkeypatch: pytest.MonkeyPatch, *replies: str | type[BaseException]) -> list[str]:
    prompts: list[str] = []
    pending = list(replies)

    def reply(prompt: str = "") -> str:
        prompts.append(prompt)
        value = pending.pop(0)
        if isinstance(value, type):
            raise value
        return value

    monkeypatch.setattr("builtins.input", reply)
    return prompts


class _Terminal:
    """A real pseudo-terminal end, so selection sees a genuine TTY."""

    def __init__(self) -> None:
        self._fd, self._other = os.openpty()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        os.close(self._fd)
        os.close(self._other)


@pytest.fixture
def terminal() -> Iterator[_Terminal]:
    if sys.platform == "win32":
        pytest.skip("needs a POSIX pseudo-terminal")
    end = _Terminal()
    yield end
    end.close()


def test_pickers_need_a_real_terminal_and_the_repl_extra(
    monkeypatch: pytest.MonkeyPatch, terminal: _Terminal
) -> None:
    monkeypatch.delenv(backend.UI_ENV, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    fake_tty = SimpleNamespace(isatty=lambda: True)  # no file descriptor behind it

    assert backend.select_backend(terminal, terminal).name == "pickers"
    assert backend.select_backend(fake_tty, fake_tty).name == "plain"
    monkeypatch.setenv("TERM", "dumb")
    assert backend.select_backend(terminal, terminal).name == "plain"

    # Without semantic-rails[repl], a real terminal falls back to plain prompts.
    monkeypatch.setenv("TERM", "xterm-256color")
    find_spec = backend.importlib.util.find_spec
    monkeypatch.setattr(
        backend.importlib.util,
        "find_spec",
        lambda name, *rest: None if name in {"questionary", "rich"} else find_spec(name, *rest),
    )
    assert not backend.pickers_available()
    assert backend.select_backend(terminal, terminal).name == "plain"


def test_plain_can_be_forced_and_pickers_explain_why_they_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, terminal: _Terminal
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv(backend.UI_ENV, "plain")
    assert backend.select_backend(terminal, terminal).name == "plain"

    monkeypatch.setenv(backend.UI_ENV, "pickers")
    monkeypatch.setattr(backend, "pickers_available", lambda: False)
    with pytest.raises(SemanticLayerError, match=r"pip install 'semantic-rails\[repl\]'"):
        backend.select_backend(terminal, terminal)

    monkeypatch.setenv(backend.UI_ENV, "fancy")
    with pytest.raises(SemanticLayerError) as exc:
        backend.select_backend(terminal, terminal)
    assert exc.value.details["reason"] == "unknown_ui_mode"


def test_a_pinned_backend_wins_until_unpinned() -> None:
    pinned = PlainBackend()
    backend.set_backend(pinned)
    assert backend.current_backend() is pinned
    backend.set_backend(None)
    assert backend.current_backend() is not pinned


def test_plain_prompts_keep_their_text_defaults_and_cancel_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = PlainBackend()
    prompts = _answers(monkeypatch, "", "Customers", "", "n", "4", "b", "1, c", "cancel", EOFError)

    assert plain.text("Model key", default="orders") == "orders"
    assert plain.text("Business label") == "Customers"
    assert plain.confirm("Create this model?", default=False) is False
    assert plain.confirm("Continue?", default=True) is False
    assert plain.choose("Metric recipe", OPTIONS, default="a") == "b"  # "4" is retried
    assert plain.multi_choose("Columns", OPTIONS, defaults=["c"]) == ["a", "c"]
    with pytest.raises(Cancelled):
        plain.text("Anything")
    with pytest.raises(Cancelled):
        plain.confirm("Anything?", default=True)

    assert prompts == [
        "Model key [orders]: ",
        "Business label: ",
        "Create this model? [y/N]: ",
        "Continue? [Y/n]: ",
        "Choose [a]: ",
        "Choose [a]: ",
        "Choose numbers or names, comma separated [3]: ",
        "Anything: ",
        "Anything? [Y/n]: ",
    ]
    output = capsys.readouterr().out
    assert "  1. Alpha (recommended)\n  2. Bravo\n" in output
    assert "Choose a number or one of: a, b, c" in output
    assert "  [ ] 1. Alpha\n  [ ] 2. Bravo\n  [x] 3. Charlie\n" in output


@pytest.mark.parametrize(
    ("answers", "default", "expected"),
    [
        (("",), "IN", "IN"),
        (("",), "NOT IN", "NOT IN"),
        (("in",), "NOT IN", "IN"),
        (("nOt In",), "IN", "NOT IN"),
        (("invalid", "2"), "IN", "NOT IN"),
    ],
)
def test_plain_choice_returns_canonical_values_for_defaults_typed_case_and_numbers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: tuple[str, ...],
    default: str,
    expected: str,
) -> None:
    prompts = _answers(monkeypatch, *answers)
    options = [("IN", "is one of"), ("NOT IN", "is not one of")]

    assert PlainBackend().choose("Keep the rows where it", options, default=default) == expected
    assert prompts == [f"Choose [{default}]: "] * len(answers)
    if "invalid" in answers:
        assert "Choose a number or one of: IN, NOT IN" in capsys.readouterr().out


def test_plain_choice_requires_unique_case_folded_match_and_keeps_exact_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    choices = [("IN", "Upper"), ("in", "Lower")]
    _answers(monkeypatch, "")
    assert PlainBackend().choose("Case collision", choices, default="IN") == "IN"

    prompts = _answers(monkeypatch, "iN", "2")
    assert PlainBackend().choose("Case collision", choices, default="IN") == "in"
    assert prompts == ["Choose [IN]: ", "Choose [IN]: "]
    assert "Ambiguous choice; use an exact value or a menu number" in capsys.readouterr().out

    _answers(monkeypatch, "in")
    assert PlainBackend().choose("Case collision", choices, default="IN") == "in"


def test_plain_choice_offers_no_missing_default_and_preserves_numeric_menu_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _answers(monkeypatch, "", "2")
    assert PlainBackend().choose("Pick", OPTIONS, default="missing") == "b"
    assert prompts == ["Choose: ", "Choose: "]

    choices = [("2", "Numeric key at position one"), ("other", "Second choice")]
    _answers(monkeypatch, "")
    assert PlainBackend().choose("Pick", choices, default="2") == "2"
    _answers(monkeypatch, "2")
    assert PlainBackend().choose("Pick", choices, default="2") == "other"
    _answers(monkeypatch, "cancel")
    with pytest.raises(Cancelled):
        PlainBackend().choose("Pick", OPTIONS)


def _picker(keys: str, ask: Callable[[PickerBackend], Any]) -> Any:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return ask(PickerBackend(input=pipe, output=DummyOutput()))


DOWN, ENTER, CTRL_C, CLEAR_LINE = "\x1b[B", "\r", "\x03", "\x15"
CTRL_A, CTRL_D = "\x01", "\x04"


def _picker_within(keys: str, ask: Callable[[PickerBackend], Any], seconds: float = 10) -> Any:
    """Like ``_picker``, but fails instead of hanging when the keys leave a prompt open."""

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    outcome: dict[str, Any] = {}
    with create_pipe_input() as pipe:

        def run() -> None:
            try:
                outcome["value"] = ask(PickerBackend(input=pipe, output=DummyOutput()))
            except BaseException as exc:  # re-raised in the test's thread
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        pipe.send_text(keys)
        thread.join(seconds)
        waiting = thread.is_alive()
    assert not waiting, f"the prompt was still waiting for input after {keys!r}"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@pytest.mark.parametrize(
    ("keys", "ask", "expected"),
    [
        (ENTER, lambda b: b.choose("Pick", OPTIONS, default="b"), "b"),
        (DOWN + ENTER, lambda b: b.choose("Pick", OPTIONS, default="a"), "b"),
        (
            "number 17" + ENTER,
            lambda b: b.choose("Pick", [(f"v{i}", f"Value number {i}") for i in range(20)]),
            "v17",
        ),
        (ENTER, lambda b: b.text("Model key", default="orders"), "orders"),
        # Typing replaces the default rather than appending to it.
        ("customers" + ENTER, lambda b: b.text("Model key", default="orders"), "customers"),
        ("y" + ENTER, lambda b: b.confirm("Create?", default=False), True),
        (ENTER, lambda b: b.confirm("Create?", default=False), False),
        # The Enter after `y` confirms it; it does not answer the next question.
        (
            "y" + ENTER + "y" + ENTER,
            lambda b: (
                b.confirm("Default clock?", default=True),
                b.confirm("Create?", default=False),
            ),
            (True, True),
        ),
        (
            " " + DOWN + " " + ENTER,
            lambda b: b.multi_choose("Cols", OPTIONS, defaults=["c"]),
            ["a", "b", "c"],
        ),
        # A cancel word that filters to a real option picks that option.
        (
            "cancel" + ENTER,
            lambda b: b.choose("Status", [("placed", "Placed orders"), ("x", "Cancelled orders")]),
            "x",
        ),
    ],
)
def test_pickers_answer_from_keystrokes(keys: str, ask: Any, expected: Any) -> None:
    assert _picker(keys, ask) == expected


@pytest.mark.parametrize(
    ("keys", "ask"),
    [
        (CTRL_C, lambda b: b.choose("Pick", OPTIONS)),
        (CTRL_C, lambda b: b.text("Model key")),
        (CTRL_C, lambda b: b.confirm("Create?", default=True)),
        (CTRL_C, lambda b: b.multi_choose("Cols", OPTIONS)),
        ("cancel" + ENTER, lambda b: b.text("Model key", default="order_value")),
        ("cancel" + ENTER, lambda b: b.choose("Pick", OPTIONS, default="b")),
        ("quit" + ENTER, lambda b: b.choose("Pick", [(f"v{i}", f"Value {i}") for i in range(20)])),
    ],
)
def test_pickers_cancel_like_plain_prompts(keys: str, ask: Any) -> None:
    with pytest.raises(Cancelled):
        _picker_within(keys, ask)


@pytest.mark.parametrize(
    "ask",
    [
        lambda b: b.choose("Pick", OPTIONS),
        lambda b: b.choose("Pick", [(f"v{i}", f"Value number {i}") for i in range(20)]),
        lambda b: b.multi_choose("Cols", OPTIONS, defaults=["c"]),
        lambda b: b.text("Model key"),
        lambda b: b.text("Model key", default="orders"),
        lambda b: b.confirm("Create?", default=True),
    ],
)
def test_ctrl_d_cancels_every_unedited_picker(ask: Any) -> None:
    with pytest.raises(Cancelled):
        _picker_within(CTRL_D, ask)


def test_ctrl_d_edits_text_once_it_is_edited() -> None:
    # With the cursor moved into the text, Ctrl-D deletes the next character.
    keys = "orders" + CTRL_A + CTRL_D + ENTER
    assert _picker_within(keys, lambda b: b.text("Key", default="x")) == "rders"


class _Recorder:
    """A backend double that records what the wizards ask."""

    name = "recorder"

    def __init__(self, *, filters_long_lists: bool, answer: str) -> None:
        self.filters_long_lists = filters_long_lists
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []

    def text(self, label: str, *, default: str = "") -> str:
        self.calls.append(("text", label))
        return default

    def choose(self, label: str, options: Any, *, default: str = "") -> str:
        self.calls.append(("choose", [value for value, _ in options]))
        return self.answer

    def confirm(self, label: str, *, default: bool) -> bool:
        return default

    def multi_choose(self, label: str, options: Any, *, defaults: Any = ()) -> list[str]:
        return list(defaults)

    def show_yaml(self, payload: Any) -> None:
        self.calls.append(("yaml", payload))


def test_long_lists_go_straight_to_a_filtering_picker() -> None:
    rows = [{"key": f"metric_{i}", "kind": "metric", "label": f"Metric {i}"} for i in range(20)]
    picker = _Recorder(filters_long_lists=True, answer="20")
    backend.set_backend(picker)

    assert authoring._select_inventory_item("Numerator", rows)["key"] == "metric_19"
    assert picker.calls == [("choose", [str(n) for n in range(1, 21)])]  # no search prompt first

    plain = _Recorder(filters_long_lists=False, answer="1")
    backend.set_backend(plain)
    authoring._select_inventory_item("Numerator", rows)
    assert plain.calls[0] == (
        "text",
        "Filter numerator (20 choices; type words, or Enter for a short list)",
    )


def test_authoring_previews_go_through_the_backend(tmp_path: Path) -> None:
    project_path = Path(
        scaffold.create_project_report(
            package_id="preview_core", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )
    recorder = _Recorder(filters_long_lists=True, answer="cancel")
    backend.set_backend(recorder)
    project = authoring.ArchitectProject(project_path, workspace_root=tmp_path)

    with pytest.raises(Cancelled):
        authoring._apply_authoring_change(
            project,
            authoring.PackageReference(source_path=str(project_path)),
            set(),
            kind="metric",
            key="revenue",
            label="Revenue",
            existing=None,
            target="metrics/core/revenue.yml",
            preview={"metrics": {"revenue": {"kind": "aggregate"}}},
            apply=lambda: pytest.fail("confirm defaults to No, so nothing is written"),
            next_action="",
        )

    assert ("yaml", {"metrics": {"revenue": {"kind": "aggregate"}}}) in recorder.calls


@pytest.mark.parametrize("width", [30, 40, 80])
def test_picker_preview_shows_every_character_at_terminal_width(width: int) -> None:
    payload = {
        "expr": "gross_revenue_amount - refunds_amount",
        "filter": "customer_region == Northeast and refunds_amount >= 100",
        "identifier": "customer_segment_with_extremely_long_identifier_123456789",
    }
    output = StringIO()
    picker = PickerBackend()
    picker._console = Console(file=output, width=width, force_terminal=False, color_system=None)

    picker.show_yaml(payload)

    expected = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).rstrip()
    # Rich may wrap a token between characters; ignore only layout whitespace.
    assert re.sub(r"\s+", "", output.getvalue()) == re.sub(r"\s+", "", expected)
    assert "gross_revenue_amount" in output.getvalue()
    assert "refunds_amount" in output.getvalue()


def test_repl_banner_says_which_prompts_are_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    backend.set_backend(PlainBackend())
    monkeypatch.setattr(shell, "pickers_available", lambda: False)
    assert "pip install 'semantic-rails[repl]'" in shell._prompt_style()

    monkeypatch.setattr(shell, "pickers_available", lambda: True)
    assert shell._prompt_style() == "line prompts"

    backend.set_backend(PickerBackend())
    assert shell._prompt_style().startswith("arrow-key pickers")


# pexpect forks a pseudo-terminal; do it from a fresh single-threaded interpreter,
# because forking pytest's multi-threaded process can deadlock the child.
def _journey(script: str, *args: str, **env: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0 and "journey ok" in proc.stdout, proc.stdout + proc.stderr
    return proc.stdout


PICKER_JOURNEY = r"""
import sys, pexpect
repl = pexpect.spawn(
    sys.executable, ["-m", "semantic_rails", "repl", "--path", sys.argv[1]],
    dimensions=(30, 100), encoding="utf-8", timeout=60,
)
for step in ("arrow-key pickers", "›"):
    repl.expect(step)
repl.sendline("author")
repl.expect("What do you want to create or update")
repl.send("\r")  # accept the recommended kind
repl.expect("key")
repl.send("\x03")  # Ctrl-C
repl.expect("Authoring cancelled; no files changed.")
repl.expect("›")
repl.sendline("exit")
repl.expect(pexpect.EOF)
print("journey ok")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
def test_repl_authoring_runs_on_pickers_in_a_real_terminal(tmp_path: Path) -> None:
    project_path = Path(
        scaffold.create_project_report(
            package_id="picker_core", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )
    before = {p: p.read_bytes() for p in project_path.rglob("*") if p.is_file()}

    _journey(
        PICKER_JOURNEY,
        str(project_path),
        SEMANTIC_RAILS_HOME=str(tmp_path / "home"),
        SEMANTIC_RAILS_UI="pickers",
        TERM="xterm-256color",
    )

    assert {p: p.read_bytes() for p in project_path.rglob("*") if p.is_file()} == before


# Each command line, then what the REPL printed before its next prompt.
COMMAND_JOURNEY = r"""
import sys, pexpect
repl = pexpect.spawn(
    sys.executable, ["-m", "semantic_rails", "repl", "--package", "jaffle_shop"],
    dimensions=(50, 250), encoding="utf-8", timeout=60,
)
repl.expect_exact("› ")
for line in sys.argv[1:]:
    repl.sendline(line)
    repl.expect_exact("› ")
    print(f"<<{line}>>{repl.before}")
repl.sendline("exit")
repl.expect(pexpect.EOF)
print("journey ok")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
def test_repl_commands_answer_a_new_user_in_a_line_terminal(tmp_path: Path) -> None:
    lines = [
        "help ls",
        "ls",
        "ls measure --limit 0",
        "ls --bogus",
        "run revenue by store and by calendar month",
    ]
    out = _journey(COMMAND_JOURNEY, *lines, SEMANTIC_RAILS_HOME=str(tmp_path), TERM="dumb")
    shown = dict(zip(lines, re.split(r"<<[^>]+>>", out)[1:], strict=True))

    assert "ls [kind] [search]" in shown["help ls"] and "author" not in shown["help ls"]
    # A bare ls of a large package counts objects by kind instead of 30 dimensions.
    assert re.search(r"\([1-9]\d* metric, [1-9]\d* measure", shown["ls"])
    assert "dimension:" not in shown["ls"]
    assert "List one kind with `ls <kind> [search]`" in shown["ls"]
    # The REPL takes the flags its own hint suggests instead of searching for them.
    assert re.search(r"[1-9]\d* measure object", shown["ls measure --limit 0"])
    assert "Search" not in shown["ls measure --limit 0"]
    assert "..." not in shown["ls measure --limit 0"]
    assert "Usage: ls [kind] [search] [--limit N] [--json]" in shown["ls --bogus"]
    run = shown["run revenue by store and by calendar month"]
    assert "MIXED_GRAIN_INVALID" in run and "    Try: Group by time" in run
