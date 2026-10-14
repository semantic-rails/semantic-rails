"""Authoring errors say where the problem is and what to write instead.

Each case is a refusal an author met while building a package from scratch: the message must name
the object (model and measure, example or test, the path asked for) and the change that fixes it,
in the words of the surface the author is on.
"""

from __future__ import annotations

import asyncio
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ProjectSpec, create_project
from semantic_rails.cli.output import _print_project_validation
from semantic_rails.cli.reports import project_validation_report
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import expr_to_dict, parse_config_expression
from semantic_rails.fanout import _plural
from semantic_rails.package_tools import run_examples_report, run_package_tests_report
from semantic_rails.route_census import route_census
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

MODEL = "models/core/events.yml"
BOOKED = "temporal_role.growth_event_booked_at"


def _starter(tmp_path: Path) -> Path:
    report = create_project("growth", ProjectSpec(package_id="growth"), workspace_root=tmp_path)
    assert report.report["ok"] is True, report.report
    return tmp_path / "growth"


def _edit(project: Path, relative: str, edit: Any) -> None:
    path = project / relative
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _measure(project: Path, key: str, **fields: Any) -> None:
    _edit(project, MODEL, lambda doc: doc["model"]["measures"][key].update(fields))


def _parse_errors(project: Path) -> list[str]:
    report, _ = parse_config_report(PackageReference(source_path=str(project)))
    return [error["message"] for error in report["errors"]]


# -- measure expressions -----------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2.5])
def test_a_numeric_measure_expr_is_the_literal_it_spells(tmp_path, value):
    project = _starter(tmp_path)
    _measure(project, "total_amount", expr=value)

    config = load_package_config(str(project))

    (measure,) = [row for row in config.measures if row.id == "measure.growth.total_amount"]
    assert expr_to_dict(measure.expr) == expr_to_dict(parse_config_expression(str(value)))


@pytest.mark.parametrize(
    "value", [True, ["amount"], {"kind": "bogus"}, float("inf"), float("-inf"), float("nan")]
)
def test_an_unreadable_measure_expr_names_its_model_and_measure(tmp_path, value):
    project = _starter(tmp_path)
    _measure(project, "total_amount", expr=value)

    (message,) = _parse_errors(project)

    assert "model 'events' measure 'total_amount'" in message
    assert "expr" in message
    assert "config expression must be an object" not in message


# -- a package with no metrics ------------------------------------------------------------


def test_a_package_without_metrics_is_shown_the_entry_to_add(tmp_path):
    project = _starter(tmp_path)
    (project / "metrics" / "core.yml").unlink()

    (message,) = _parse_errors(project)

    assert "metrics:" in message and "metric recipes" not in message
    entry = re.search(r"`(metrics: .*)`", message)
    assert entry is not None, message
    (project / "metrics" / "core.yml").write_text(entry.group(1) + "\n", encoding="utf-8")
    assert _parse_errors(project) == []


# -- a role the measure isn't timed by ----------------------------------------------------


def _with_second_role(tmp_path: Path) -> Runtime:
    project = _starter(tmp_path)

    def add_role(document: dict[str, Any]) -> None:
        document["model"]["times"]["booked_at"] = {
            "label": "Booked At",
            "column": "occurred_at",
            "kind": "timestamp",
            "class": "event_time",
        }

    _edit(project, MODEL, add_role)
    return Runtime.from_path(str(project))


@pytest.mark.parametrize("selected", ["measure.growth.total_amount", "metric.growth.total_amount"])
def test_an_incompatible_role_names_the_measure_and_its_times_list(tmp_path, selected):
    runtime = _with_second_role(tmp_path)
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.compile(
                {"select": [selected], "time": {"temporal_role": BOOKED, "grain": "day"}}
            )
    finally:
        runtime.close()

    error = raised.value
    assert error.code == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert "measure.growth.total_amount" in str(error) and selected in str(error)
    assert "times:" in str(error)
    assert "expression 'measure'" not in str(error)
    assert error.details["measures"] == ["measure.growth.total_amount"]
    assert error.details["compatible"] == ["temporal_role.growth_event_occurred_at"]


def test_a_role_pinned_on_a_measure_names_its_times_list(tmp_path):
    runtime = _with_second_role(tmp_path)
    pinned = {"kind": "measure", "measure": "measure.growth.total_amount", "temporal_role": BOOKED}
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.compile({"select": [{"expression": pinned, "as": "amount"}]})
    finally:
        runtime.close()

    assert raised.value.code == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert "'measure.growth.total_amount' is not timed by" in str(raised.value)
    assert "times:" in str(raised.value)


# -- examples and tests -------------------------------------------------------------------


def test_a_failing_example_or_test_is_named_with_its_placeholders_filled(tmp_path):
    project = _starter(tmp_path)
    _edit(
        project,
        "examples/core.yml",
        lambda doc: doc["examples"]["starter_total_amount_by_day"]["query"].update(
            select=[{"expression": {"kind": "bogus"}}]
        ),
    )
    _edit(
        project,
        "tests/core.yml",
        lambda doc: doc["tests"]["starter_count_returns_rows"]["query"].update(
            select=[{"expression": {"kind": ""}, "as": "row_count"}]
        ),
    )
    ref = PackageReference(source_path=str(project))

    for report, key, entry in (
        (run_examples_report(ref), "example_id", "starter_total_amount_by_day"),
        (run_package_tests_report(ref), "test_id", "starter_count_returns_rows"),
    ):
        (error,) = report["errors"]
        assert error["details"][key] == entry
        assert "''" not in error["message"] and "select[0]" in error["message"]


# -- the route census on the CLI ------------------------------------------------------------


def test_the_cli_prints_the_path_preferences_rows_for_an_undecided_pair(tmp_path, capsys):
    package = copy_package_config(tmp_path, "jaffle_shop")
    ref = PackageReference(source_path=str(package))
    report = project_validation_report(ref, mode="parse")
    (warning,) = [row for row in report["warnings"] if row["code"] == "ROUTES_UNDECIDED"]
    assert "record_route_decision" not in warning["message"]
    assert "graph.path_preferences" in warning["message"]

    _print_project_validation(report)
    printed = capsys.readouterr().out

    assert "record_route_decision" not in printed
    head, _, rest = printed.partition("graph.path_preferences in graph.yml:\n")
    assert rest, printed
    block = textwrap.dedent("".join(re.match(r"((?:    .*\n)+)", rest).group(1)))
    rows = yaml.safe_load(block)
    first = (rows[0]["source_entity"], rows[0]["target_entity"])
    assert len(rows) > 1 and all(
        (row["source_entity"], row["target_entity"]) == first for row in rows
    )

    def record(document: dict[str, Any]) -> None:
        document["graph"].setdefault("path_preferences", []).append(rows[0])

    _edit(package, "graph.yml", record)
    undecided = route_census(load_package_config(str(package)))["undecided"]
    assert first not in {(row["source_entity"], row["target_entity"]) for row in undecided}


# -- project_status on a path that doesn't exist ----------------------------------------------


@pytest.mark.parametrize("where", ["inside", "outside"])
def test_status_of_a_mistyped_path_says_it_does_not_exist(tmp_path, where):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _starter(workspace)
    server = create_architect_mcp_server(workspace_root=workspace)
    missing = (workspace if where == "inside" else tmp_path) / "growht"

    result = asyncio.run(server.call_tool("project_status", {"project_path": str(missing)}))[1]

    message = result["error"]["message"]
    assert str(missing) in message and "does not exist" in message
    assert "writes" not in message
    assert ("outside" in message) is (where == "outside")


# -- label plurals --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "plural"),
    [
        ("Order", "Orders"),
        ("Category", "Categories"),
        ("Box", "Boxes"),
        ("Branch", "Branches"),
        ("Address", "Addresses"),
        ("Order status", "Order statuses"),
        ("Analysis", "Analyses"),
        ("Iris", "Irises"),
        ("Alias", "Aliases"),
        ("Store rooms", "Store rooms"),
        ("Buildings", "Buildings"),
        ("Rooms (east wing, row grain)", "Rooms (east wing, row grain)"),
        ("Room (east wing)", "Rooms (east wing)"),
    ],
)
def test_a_label_pluralizes_once_and_before_a_parenthetical(label, plural):
    assert _plural(label) == plural
