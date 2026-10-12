"""Package checks run every example and test and name each refusal: the entry's id, the
refusal's path and message, and its hint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from semantic_rails.cli import app
from tests.semantic_rails.conftest import copy_package_config

NOW = "2017-09-04T12:00:00Z"
ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
HAVING = (
    'Write "having": [{"field": "order_count", "op": "gt", "value": 5}]. '
    "See the query schema at https://semantic-rails.com/schemas/query.v2.json."
)
RANGE = 'Write "time": {"grain": "month", "last": {"unit": "month", "value": 3}}.'
ENTRIES = {
    "tests": {
        "old_result_filter": {
            "kind": "query_row_count_bounds",
            "query": {
                "select": [ORDERS],
                "group_by": [STORE],
                "metric_filters": [
                    {"expression": {"kind": "measure", "measure": ORDERS}, "op": ">", "value": 5}
                ],
            },
            "min_rows": 1,
        },
        "old_range": {
            "kind": "query_returns_columns",
            "query": {
                "select": [ORDERS],
                "time": {"grain": "month", "range": {"last": {"unit": "month", "value": 3}}},
            },
            "columns": ["period"],
        },
        "orders": {"kind": "query_row_count_bounds", "query": {"select": [ORDERS]}, "min_rows": 1},
    },
    "examples": {
        "old_order_axis": {
            "query": {
                "select": [ORDERS],
                "time": {"grain": "month", "start": "2017-01-01"},
                "order_by": [{"field": "time", "direction": "desc"}],
            }
        },
        "orders": {"query": {"select": [ORDERS]}},
    },
}


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    path = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    for folder, entries in ENTRIES.items():
        for old in (path / folder).glob("*.yml"):
            old.unlink()
        (path / folder / "old.yml").write_text(yaml.safe_dump({folder: entries}))
    return path


def _cli(monkeypatch: pytest.MonkeyPatch, capsys, package: Path, *args: str) -> tuple[int, str]:
    monkeypatch.chdir(package.parent)
    monkeypatch.setattr(sys, "argv", ["semantic-rails", *args, "--path", str(package)])
    try:
        app.main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


def test_test_package_runs_every_test_and_names_each_refusal(monkeypatch, capsys, package):
    code, out = _cli(monkeypatch, capsys, package, "test-package", "--now", NOW)
    report = json.loads(out)
    assert code == 1
    assert report["summary"] == {"tests_total": 3, "passed": 1, "failed": 2}
    refusals = {
        error["details"]["test_id"]: (
            error["details"]["path"],
            error["message"],
            error["details"]["recovery_hints"][0]["message"],
        )
        for error in report["errors"]
    }
    assert refusals == {
        "old_result_filter": (
            "metric_filters",
            "metric_filters: is not part of a query call",
            HAVING,
        ),
        "old_range": ("time.range", "time.range: is retired", RANGE),
    }


@pytest.mark.parametrize(
    ("mode", "lines"),
    [
        ("tests", [
            "[fail] tests passed=1 failed=2 tests_total=3",
            f"  old_result_filter: metric_filters: is not part of a query call\n    hint: {HAVING}",
            f"  old_range: time.range: is retired\n    hint: {RANGE}",
        ]),
        ("examples", [
            "[fail] examples passed=1 failed=1 examples_total=2",
            "  old_order_axis: order_by[0].field: the time axis is named 'period'\n"
            '    hint: Write "period".',
        ]),
    ],
)  # fmt: skip
def test_project_validate_names_each_refused_entry(
    monkeypatch, capsys, package, mode: str, lines: list[str]
):
    code, out = _cli(monkeypatch, capsys, package, "project", "validate", "--mode", mode)
    assert code == 1
    assert [line for line in lines if line not in out] == []


def test_project_upgrade_names_each_refused_example(monkeypatch, capsys, package):
    code, out = _cli(monkeypatch, capsys, package, "project", "upgrade")
    assert code == 0
    assert (
        "Next: Example examples/old.yml:examples.old_order_axis.query does not compile "
        "(INVALID_QUERY): order_by[0].field: the time axis is named 'period' "
        'Hint: Write "period".\n'
    ) in out
    assert "examples.orders" not in out
