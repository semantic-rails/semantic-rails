"""Trust guarantees for the human CLI.

A command never answers from a package the person didn't choose. `ask`
keeps the engine's warnings, says when rows were cut off, and prints numbers
people can read.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import select
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails import dev_cli
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError

REPO_ROOT = Path(__file__).resolve().parents[2]
QUERY = '{"version": 1, "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}}]}'


class _TTYBuffer(io.StringIO):
    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return True


@pytest.fixture
def nowhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An empty working directory and home: no flag, package directory or profile."""

    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, SEMANTIC_RAILS_HOME=str(tmp_path / "home"), PYTHONPATH=str(REPO_ROOT))
    env.pop("NO_COLOR", None)
    monkeypatch.chdir(work)
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", env["SEMANTIC_RAILS_HOME"])
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        cwd=Path.cwd(),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "args",
    [
        ("ask", "total amount by event type", "--run"),
        ("ls",),
        ("project", "status"),
        ("repl",),
    ],
)
def test_human_commands_without_a_chosen_package_fail_with_guidance(
    nowhere: dict[str, str], args: tuple[str, ...]
) -> None:
    proc = _run(nowhere, *args)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "error [INVALID_CONFIG]: No package selected. Choose one:" in proc.stderr
    assert "--path <package-dir>" in proc.stderr
    assert "semantic-rails init my_package" in proc.stderr
    assert "--package jaffle_shop" in proc.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("ask", "total amount by event type", "--run", "--json"),
        ("plan", "--intent", "total amount by event type"),
        ("query", "--query-json", QUERY),
        ("catalog",),
        ("mcp", "doctor"),
    ],
)
def test_json_commands_without_a_chosen_package_return_a_structured_error(
    nowhere: dict[str, str], args: tuple[str, ...]
) -> None:
    proc = _run(nowhere, *args)

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_CONFIG"
    assert payload["error"]["details"]["reason"] == "no_package_selected"
    # Nothing was answered from the bundled package.
    assert "measure.jaffle" not in proc.stdout


def test_setup_reports_no_package_instead_of_checking_the_bundled_one(
    nowhere: dict[str, str],
) -> None:
    report = dev_cli.setup_report(
        argparse.Namespace(package="", path="", checks="parse", server=False)
    )

    check = next(row for row in report["checks"] if row["name"] == "package_parse")
    assert check["summary"] == "no package selected"


def _answer(monkeypatch: pytest.MonkeyPatch, *replies: str | type[BaseException]) -> list[str]:
    prompts: list[str] = []
    pending = list(replies)

    def reply(prompt: str = "") -> str:
        prompts.append(prompt)
        value = pending.pop(0)
        if isinstance(value, type):
            raise value
        return value

    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(dev_cli.sys, "stdout", _TTYBuffer())
    monkeypatch.setattr("builtins.input", reply)
    return prompts


def _ask_args(**overrides: Any) -> argparse.Namespace:
    values = {
        "question": ["monthly revenue by store"],
        "package": "",
        "path": "",
        "run": True,
        "compile": False,
        "limit": 20,
        "json": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize("reply", ["", "n", EOFError, KeyboardInterrupt])
def test_interactive_ask_answers_nothing_unless_the_sample_package_is_confirmed(
    nowhere: dict[str, str], monkeypatch: pytest.MonkeyPatch, reply: str | type[BaseException]
) -> None:
    prompts = _answer(monkeypatch, reply)
    monkeypatch.setattr(dev_cli, "ask_report", lambda *_a, **_k: pytest.fail("answered"))

    with pytest.raises(SemanticLayerError) as exc:
        dev_cli.cmd_ask(_ask_args())

    assert exc.value.details["reason"] == "no_package_selected"
    assert prompts == ["Use the bundled `jaffle_shop` sample package? [y/N]: "]
    notice = dev_cli.sys.stdout.getvalue()
    assert "No package selected." in notice
    assert "sample data, not yours" in notice


def test_interactive_ask_uses_the_sample_package_after_confirmation(
    nowhere: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, "y")
    used: list[PackageReference] = []
    monkeypatch.setattr(dev_cli, "ask_report", lambda ref, **_k: used.append(ref) or {"ok": True})
    monkeypatch.setattr(dev_cli, "_print_ask_report", lambda _report: None)

    dev_cli.cmd_ask(_ask_args())

    assert [ref.package_id for ref in used] == ["jaffle_shop"]


@pytest.mark.parametrize("command", ["ls", "project validate"])
def test_interactive_listing_and_validation_also_ask_first(
    nowhere: dict[str, str], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    prompts = _answer(monkeypatch, "n")
    args = argparse.Namespace(package="", path="", json=False)
    run = dev_cli.cmd_ls if command == "ls" else dev_cli.cmd_project_validate

    with pytest.raises(SemanticLayerError):
        run(args)

    assert prompts == ["Use the bundled `jaffle_shop` sample package? [y/N]: "]


def test_local_profile_is_used_without_a_prompt(
    nowhere: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from semantic_rails.local_config import init_local_profile

    prompts = _answer(monkeypatch)
    project = dev_cli.create_project_report(
        package_id="profile_pkg", workspace_root=str(tmp_path), run_checks=False
    )["project_path"]
    init_local_profile(package_path=project)

    assert dev_cli.default_package_ref(interactive=True).source_path == project
    assert prompts == []


def test_json_mode_and_chosen_packages_never_prompt(
    nowhere: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = _answer(monkeypatch)
    with pytest.raises(SemanticLayerError):
        dev_cli.cmd_ask(_ask_args(json=True))

    project = dev_cli.create_project_report(
        package_id="cwd_pkg", workspace_root=str(tmp_path), run_checks=False
    )["project_path"]
    monkeypatch.chdir(Path(project) / "models")
    assert dev_cli.default_package_ref(interactive=True).source_path == project
    assert prompts == []


def test_repl_without_a_package_asks_before_opening_the_sample_package(
    nowhere: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = _answer(monkeypatch, "", "y", "exit")
    with pytest.raises(SemanticLayerError):
        dev_cli.run_interactive_shell()

    dev_cli.run_interactive_shell()

    assert prompts[:2] == ["Use the bundled `jaffle_shop` sample package? [y/N]: "] * 2
    assert "package  jaffle_shop (bundled sample package, not your data)" in (
        dev_cli.sys.stdout.getvalue()
    )


ASK = ("ask", "monthly revenue by store", "--run", "--limit", "2")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
@pytest.mark.parametrize(("args", "reply", "code"), [(ASK, "", 1), (ASK, "y", 0), ((), "", 1)])
def test_real_terminal_confirmation_gates_the_sample_package(
    nowhere: dict[str, str], args: tuple[str, ...], reply: str, code: int
) -> None:
    primary, secondary = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "semantic_rails", *args],
        stdin=secondary,
        stdout=secondary,
        stderr=secondary,
        env=nowhere,
    )
    os.close(secondary)
    output, answered, deadline = b"", False, time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            if not select.select([primary], [], [], 0.5)[0]:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(primary, 4096)
            except OSError:  # Linux reports EIO once the child closes the terminal
                break
            if not chunk:
                break
            output += chunk
            if not answered and b"[y/N]: " in output:
                os.write(primary, reply.encode() + b"\n")
                answered = True
        returncode = proc.wait(timeout=30)
    finally:
        os.close(primary)
        if proc.poll() is None:
            proc.kill()
    text = output.decode(errors="replace")

    assert returncode == code, text
    assert "No package selected." in text
    if code:
        assert "Choose one:" in text
        assert "Rows:" not in text
    else:
        assert "Package: jaffle_shop (bundled sample package, not your data)" in text
        assert "Rows: 2 (stopped at the 2-row limit; more rows match)" in text


def test_ask_names_the_bundled_package_and_formats_numbers(
    nowhere: dict[str, str],
) -> None:
    proc = _run(nowhere, "ask", "--package", "jaffle_shop", "monthly revenue by store", "--run")

    assert proc.returncode == 0, proc.stderr
    assert "Package: jaffle_shop (bundled sample package, not your data)" in proc.stdout
    assert "Store name" in proc.stdout and "Order time (month)" in proc.stdout
    assert re.search(r"\| +\d{1,3}(,\d{3})*\.\d{2}\n", proc.stdout), proc.stdout
    assert not re.search(r"\d\.\d{5,}", proc.stdout), "float noise leaked into human output"


def test_ask_json_reports_an_exact_row_limit(nowhere: dict[str, str]) -> None:
    def ask(limit: str) -> dict[str, Any]:
        args = ("ask", "--package", "jaffle_shop", "monthly revenue by store", "--run")
        proc = _run(nowhere, *args, "--limit", limit, "--json")
        assert proc.returncode == 0, proc.stderr
        return dict(json.loads(proc.stdout))

    cut, full = ask("2"), ask("0")

    assert cut["package"]["bundled"] is True
    assert (cut["result"]["row_count"], cut["result"]["truncated"]) == (2, True)
    assert full["result"]["truncated"] is False
    assert full["result"]["row_count"] > 2


class _StubRuntime:
    package_id = "stub"
    warehouse = "duckdb"

    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(payload)
        return {
            "ok": True,
            "rows": [{"orders": 3}],
            "row_count": 1,
            "truncated": True,
            "output_columns": [],
            "warnings": [{"code": "EMPTY_RESULT_WINDOW", "message": "No data in window."}],
            "assumptions": ["Treated a blank grain as month."],
        }

    def close(self) -> None:
        pass


@pytest.mark.parametrize(("planned", "sent"), [({}, 6), ({"limit": 3}, 3), ({"limit": 50}, 6)])
def test_ask_limits_sql_to_one_extra_row_and_fences_at_the_limit(
    monkeypatch: pytest.MonkeyPatch, planned: dict[str, int], sent: int
) -> None:
    runtime = _StubRuntime()
    query = {"select": [{"expression": {"measure": "measure.orders"}}], **planned}
    monkeypatch.setattr(dev_cli, "_runtime_from_ref", lambda _ref: runtime)
    monkeypatch.setattr(
        dev_cli, "plan_payload", lambda *_a, **_k: {"ok": True, "best": {"query_ir": query}}
    )
    monkeypatch.setattr(dev_cli, "resolve_catalog", lambda *_a, **_k: {})

    dev_cli.ask_report(
        PackageReference(source_path="/nowhere"), question="q", execute=True, limit=5
    )

    assert runtime.queries == [{**query, "limit": sent, "limits": {"max_rows": 5}}]


class _LimitingRuntime(_StubRuntime):
    """Twelve matching rows; honours the SQL limit and the row fence like the engine."""

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(payload)
        rows = [{"orders": index} for index in range(12)][: payload.get("limit") or 12]
        fence = dict(payload.get("limits", {}) or {}).get("max_rows")
        truncated = fence is not None and len(rows) > fence
        rows = rows[:fence] if truncated else rows
        return {"ok": True, "rows": rows, "row_count": len(rows), "truncated": truncated}


@pytest.mark.parametrize(
    ("limit", "rows_line", "hint"),
    [
        (
            2,
            "Rows: 2 (stopped at the 2-row limit; more rows match)",
            "(the planned query's own limit of 5 rows still applies)",
        ),
        (0, "Rows: 5 (the planned query itself returns at most 5 rows)", None),
    ],
)
def test_ask_separates_a_planned_limit_from_the_cli_cap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    limit: int,
    rows_line: str,
    hint: str | None,
) -> None:
    query = {"select": [{"expression": {"measure": "measure.orders"}}], "limit": 5}
    monkeypatch.setattr(dev_cli, "_runtime_from_ref", lambda _ref: _LimitingRuntime())
    monkeypatch.setattr(
        dev_cli, "plan_payload", lambda *_a, **_k: {"ok": True, "best": {"query_ir": query}}
    )
    monkeypatch.setattr(dev_cli, "resolve_catalog", lambda *_a, **_k: {})

    report = dev_cli.ask_report(
        PackageReference(source_path="/nowhere"), question="q", execute=True, limit=limit
    )
    dev_cli._print_ask_report(report)

    output = capsys.readouterr().out
    assert report["result"]["planned_limit"] == 5
    assert rows_line in output
    assert (hint in output) if hint else ("To lift" not in output)


def test_ask_keeps_engine_warnings_and_says_how_to_fetch_every_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = _StubRuntime()
    plan = {
        "ok": True,
        "best": {
            "query_ir": {"select": [{"expression": {"measure": "measure.orders"}}]},
            "resolved": [{"id": "measure.orders", "label": "Orders"}],
        },
    }
    monkeypatch.setattr(dev_cli, "_runtime_from_ref", lambda _ref: runtime)
    monkeypatch.setattr(dev_cli, "plan_payload", lambda *_a, **_k: plan)
    monkeypatch.setattr(dev_cli, "resolve_catalog", lambda *_a, **_k: {})

    report = dev_cli.ask_report(
        PackageReference(source_path="/nowhere"), question="orders", execute=True, limit=5
    )
    dev_cli._print_ask_report(report)

    assert report["result"]["truncated"] is True
    output = capsys.readouterr().out
    assert "Rows: 1 (stopped at the 5-row limit; more rows match)" in output
    assert (
        "To lift the 5-row cap, run: semantic-rails ask --path /nowhere orders --run --limit 0\n"
        in output
    )
    assert "Warnings:\n  EMPTY_RESULT_WINDOW: No data in window.\n" in output
    assert "  Treated a blank grain as month.\n" in output


def test_ask_prints_a_warning_repeated_for_each_measure_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    aligned = {
        "code": "REWRITE_APPLIED",
        "message": "Metric leaf uses its own compatible time role and is aligned to the "
        "requested query grain",
    }

    class _RatioRuntime(_StubRuntime):
        def query(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                **super().query(payload),
                "warnings": [
                    {**aligned, "object_ids": ["measure.revenue"]},
                    {**aligned, "object_ids": ["measure.order_count"]},
                ],
            }

    plan = {"ok": True, "best": {"query_ir": {"select": [{"expression": {"metric": "m.aov"}}]}}}
    monkeypatch.setattr(dev_cli, "_runtime_from_ref", lambda _ref: _RatioRuntime())
    monkeypatch.setattr(dev_cli, "plan_payload", lambda *_a, **_k: plan)

    report = dev_cli.ask_report(
        PackageReference(source_path="/nowhere"), question="aov", execute=True
    )
    dev_cli._print_ask_report(report)

    output = capsys.readouterr().out
    assert output.count("REWRITE_APPLIED: Metric leaf uses its own compatible time role") == 1
    assert len(report["result"]["warnings"]) == 2  # --json keeps every warning


def test_table_formats_numbers_for_people() -> None:
    rows = [
        {
            "store": "x" * 60,
            "revenue": 18053.280000000024,
            "orders": 1234567,
            "share": 0.0034,
            "margin": Decimal("-0.001"),
            "open": True,
            "note": None,
        },
        {
            "store": "Philadelphia",
            "revenue": 5,
            "orders": 7,
            "share": 0.25,
            "margin": Decimal("12.5"),
            "open": False,
            "note": "two\nlines",
        },
    ]
    columns = [
        {"field": "store", "display_label": "Store name", "type": "string"},
        {"field": "revenue", "display_label": "Revenue", "type": "currency"},
    ]

    header, rule, first, second = dev_cli._table_lines(rows, columns)

    assert header.split(" | ") == [
        "Store name".ljust(40),
        "  Revenue",
        "   orders",
        "  share",
        "  margin",
        "open ",
        "note",
    ]
    assert set(rule) <= {"-", "+"}
    assert first.split(" | ") == [
        "x" * 37 + "...",
        "18,053.28",
        "1,234,567",
        "0.00340",
        "-0.00100",
        "true ",
        "NULL",
    ]
    assert second.split(" | ") == [
        "Philadelphia".ljust(40),
        "     5.00",
        "        7",
        "0.25000",
        "12.50000",
        "false",
        "two lines",
    ]


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        (-0.0, 2, "0.00"),
        (-0.001, 2, "-0.001"),
        (1e-7, 6, "1e-07"),
        (-1e-7, 6, "-1e-07"),
        (Decimal("-2E-8"), 6, "-2e-8"),
        (9007199254740993, 2, "9,007,199,254,740,993.00"),
        (float("nan"), 2, "nan"),
        (float("-inf"), 2, "-inf"),
        (12345678901234567890, 0, "12,345,678,901,234,567,890"),
        (Decimal("1234.5"), 2, "1,234.50"),
    ],
)
def test_number_edge_cases(value: Any, decimals: int, expected: str) -> None:
    assert dev_cli._format_number(value, decimals) == expected


def test_columns_never_round_a_nonzero_value_to_zero_or_a_big_int_through_float() -> None:
    assert dev_cli._format_column([1e-7, -1e-7], column_type="number") == (
        ["1.00e-07", "-1.00e-07"],
        True,
    )
    assert dev_cli._format_column([9007199254740993, 0.5], column_type="number") == (
        ["9,007,199,254,740,993.000", "0.500"],
        True,
    )
    assert dev_cli._format_column([10**309, 0.5], column_type="number") == (
        [f"{10**309:,}.000", "0.500"],
        True,
    )
    assert dev_cli._format_column([9007199254740993], column_type="currency") == (
        ["9,007,199,254,740,993.00"],
        True,
    )


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        # Up to six decimals keep three significant digits on the smallest value...
        ([0.00012, 0.5], ["0.000120", "0.500000"]),
        ([Decimal("0.00012"), Decimal("0.5")], ["0.000120", "0.500000"]),
        # ...past that, fixed decimals would drop them, so the column prints significant digits.
        ([0.000012, 0.5], ["1.20e-05", "0.500"]),
        ([Decimal("0.000012"), Decimal("0.5")], ["1.20e-5", "0.500"]),
        ([5.1e-7, 9.6e-7, 1.5e-6, 5e-7], ["5.10e-07", "9.60e-07", "1.50e-06", "5.00e-07"]),
        # Integers stay exact and larger values keep two decimals in such a column.
        (
            [1.5e-6, 0.25, 12, 1234.5678, 10**20, None],
            ["1.50e-06", "0.250", "12", "1,234.57", "100,000,000,000,000,000,000", "NULL"],
        ),
        # A rounded fraction never looks like an exact integer.
        ([1.5e-6, 0.9996, 1], ["1.50e-06", "1.000", "1"]),
        # Values that aren't finite print as they are, before any comparison.
        ([Decimal("sNaN"), Decimal("NaN"), Decimal("5.1E-7")], ["snan", "nan", "5.10e-7"]),
        ([float("nan"), float("-inf"), 1e-7], ["nan", "-inf", "1.00e-07"]),
    ],
)
def test_small_values_keep_their_significant_digits(column: list[Any], expected: list[str]) -> None:
    assert dev_cli._format_column(column, column_type="number") == (expected, True)


def test_a_decimal_too_small_for_a_float_prints_instead_of_crashing() -> None:
    rows = [{"ratio": Decimal("1E-400")}, {"ratio": Decimal("-1E-400")}, {"ratio": Decimal("0.25")}]
    columns = [{"field": "ratio", "display_label": "Ratio", "type": "number"}]

    _header, _rule, *cells = dev_cli._table_lines(rows, columns)

    assert [cell.strip() for cell in cells] == ["1.00e-400", "-1.00e-400", "0.250"]


@pytest.mark.parametrize("selection", [("--package", "jaffle_shop"), ("--path", "<bundled>")])
def test_validation_labels_the_sample_package(
    nowhere: dict[str, str], selection: tuple[str, str]
) -> None:
    flag, value = selection
    value = dev_cli.list_package_paths()["jaffle_shop"] if value == "<bundled>" else value

    human = _run(nowhere, "project", "validate", flag, value, "--mode", "parse")
    payload = json.loads(
        _run(nowhere, "project", "validate", flag, value, "--mode", "parse", "--json").stdout
    )

    assert human.returncode == 0, human.stderr
    assert "(bundled sample package, not your data)" in human.stdout.splitlines()[0]
    assert payload["package"]["bundled"] is True


def test_a_registered_package_that_is_not_a_shipped_sample_is_not_labelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mine = dev_cli.create_project_report(
        package_id="my_company", workspace_root=str(tmp_path), run_checks=False
    )["project_path"]
    registered = {**dev_cli.list_package_paths(), "my_company": mine}
    monkeypatch.setattr(dev_cli, "list_package_paths", lambda: registered)
    ref = PackageReference(source_path=mine, package_id="my_company")

    assert not dev_cli._is_bundled_ref(ref)
    assert dev_cli._ref_display(ref) == "my_company"
    assert dev_cli._is_bundled_ref(PackageReference(source_path=registered["jaffle_shop"]))


def test_bundled_package_is_recognised_however_it_was_selected(nowhere: dict[str, str]) -> None:
    bundled = dev_cli.list_package_paths()["jaffle_shop"]
    for ref in (
        PackageReference(source_path=bundled, package_id="jaffle_shop"),
        PackageReference(source_path=bundled),
        PackageReference(source_path=str(Path(bundled) / "package.yml")),
    ):
        assert dev_cli._is_bundled_ref(ref), ref
        assert dev_cli._ref_display(ref).endswith("(bundled sample package, not your data)")
    assert not dev_cli._is_bundled_ref(PackageReference(source_path=str(Path.cwd())))

    proc = _run(nowhere, "ask", "--path", bundled, "monthly revenue by store", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["package"]["bundled"] is True


def test_dimension_columns_print_ids_and_years_as_stored() -> None:
    rows = [{"store_id": 1234567, "fiscal_year": 2024, "revenue": 1234.5}]
    columns = [
        {"field": "store_id", "semantic_id": "dimension.store_id", "type": "integer"},
        {"field": "fiscal_year", "semantic_id": "dimension.fiscal_year", "type": "integer"},
        {"field": "revenue", "semantic_id": "measure.revenue", "type": "currency"},
    ]

    header, _rule, row = dev_cli._table_lines(rows, columns)

    assert row.split(" | ") == [" 1234567", "       2024", "1,234.50"]


def test_cells_and_headers_never_send_control_codes_to_the_terminal() -> None:
    rows = [{"note": "a\x1b[31mred\tb\rc"}]
    columns = [{"field": "note", "display_label": "No\x07te"}]

    header, _rule, row = dev_cli._table_lines(rows, columns)

    assert header == "No\\x07te"
    assert row == "a\\x1b[31mred b c"


@pytest.mark.parametrize(
    "value",
    [
        "Café\u00a0Nord",  # no-break space
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",  # Persian with a zero-width non-joiner
        "東京\u3000本店",  # ideographic space
        "\U0001f469\u200d\U0001f4bb",  # emoji joined with ZWJ
        "co\u00adoperate",  # soft hyphen
    ],
)
def test_real_world_text_prints_as_stored(value: str) -> None:
    _header, _rule, row = dev_cli._table_lines([{"key": value}], [])

    assert row == value


def test_c1_controls_and_bidi_overrides_are_escaped() -> None:
    _header, _rule, row = dev_cli._table_lines([{"key": "a\x9bb\x7fc\u202ed"}], [])

    assert row == "a\\x9bb\\x7fc\\u202ed"


def test_long_duplicate_labels_stay_distinct_after_shortening() -> None:
    long_label = "Revenue from very long named product categories"
    rows = [{"a" * 45: 1, "a" * 44 + "b": 2}]
    columns = [{"field": field, "display_label": long_label} for field in rows[0]]

    header = dev_cli._table_lines(rows, columns)[0]

    assert header.split(" | ") == ["a" * 37 + "...", "a" * 37 + "... #2"]


def test_table_headers_show_time_grain_and_disambiguate_duplicate_labels() -> None:
    rows = [{"t__month": "2017-01-01", "a": 1.0, "b": 2.0}]
    columns = [
        {"field": "t__month", "display_label": "Order time", "type": "time"},
        {"field": "a", "display_label": "Revenue", "type": "number"},
        {"field": "b", "display_label": "Revenue", "type": "number"},
    ]

    header = dev_cli._table_lines(rows, columns)[0]

    assert header.split(" | ") == ["Order time (month)", "a", "b"]
