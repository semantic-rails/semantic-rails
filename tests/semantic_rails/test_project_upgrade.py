"""`semantic-rails project upgrade` and the Architect `upgrade_project` tool share one service."""

from __future__ import annotations

import asyncio
import functools
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_transactions import ProjectFileUpdate, ProjectTransaction
from semantic_rails.cli import app
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from semantic_rails.upgrade import service
from semantic_rails.upgrade.model import Edit, Finding, Option, PackageFiles, Rule
from semantic_rails.upgrade.registry import RULES

ROOT = Path(__file__).resolve().parents[2]
META = "    meta: {owner_team: analytics, review_priority: low, change_risk: low}\n"
# A package written for an earlier release: parent rollups under defaults.measure and
# null_behavior on two metrics, with a comment on every block the upgrade edits.
LEGACY = {
    "package.yml": (
        "schema_version: 1\n"
        "package:\n"
        "  id: shop\n"
        "  namespace: shop\n"
        "  warehouse: duckdb\n"
        "  default_db: data/shop.duckdb\n"
        "  seed: {kind: external}\n"
        "  schema_strict: true\n"
        "defaults:\n"
        "  # Parent rollups, as the earlier release declared them.\n"
        "  measure:\n"
        "    subject_entity: self\n"
        "    aggregation_entity: self\n"
        "  time:\n"
        "    timezone: UTC\n"
        "    default_query_axis: false\n"
    ),
    "graph.yml": (
        "graph:\n"
        "  entities:\n"
        "    event: {label: Event, key: [event_id], model: events, allowed_as_root: true}\n"
    ),
    "models/events.yml": (
        "model:\n"
        "  id: events\n"
        "  label: Events\n"
        "  relation: raw_events\n"
        "  entities:\n"
        "    event: {}\n"
        "  times:\n"
        "    occurred_at:\n"
        "      label: Occurred At\n"
        "      column: occurred_at\n"
        "      kind: timestamp\n"
        "      class: event_time\n"
        "      default: true\n"
        "  dimensions:\n"
        "    event_type: {label: Event Type, kind: categorical}\n"
        "  measures:\n"
        "    event_count:\n"
        "      label: Event count\n"
        "      kind: entity_count\n"
        "      entity_key: event_id\n"
        "      accumulation: {kind: event}\n"
        "      value_type: count\n"
        "  " + META + "    total_amount:\n"
        "      label: Total amount\n"
        "      kind: aggregate\n"
        "      expr: amount\n"
        "      default_agg: sum\n"
        "      accumulation: {kind: flow}\n"
        "      value_type: number\n"
        "  " + META
    ),
    "metrics/core.yml": (
        "metrics:\n"
        "  total_amount:\n"
        "    label: Total amount\n"
        "    kind: aggregate\n"
        "    measure: total_amount\n"
        "    value_type: number\n" + META + "  # Amount per event.\n"
        "  amount_per_event:\n"
        "    label: Amount per event\n"
        "    kind: ratio\n"
        "    numerator: total_amount\n"
        "    denominator: event_count\n"
        "    null_behavior: null_if_zero\n"
        "    value_type: number\n" + META + "  # Events per event, kept for a dashboard.\n"
        "  events_per_event:\n"
        "    label: Events per event\n"
        "    kind: ratio\n"
        "    numerator: event_count\n"
        "    denominator: event_count\n"
        "    null_behavior: null_if_zero\n"
        "    value_type: number\n" + META
    ),
    "examples/core.yml": (
        "examples:\n"
        "  amount_by_type:\n"
        "    question: Amount per event by type\n"
        "    query:\n"
        "      version: 1\n"
        "      select:\n"
        "      - {expression: {metric: metric.shop.amount_per_event}, as: amount_per_event}\n"
        "      group_by: [dimension.shop_event_event_type]\n"
    ),
    "tests/core.yml": (
        "tests:\n"
        "  count_by_day:\n"
        "    kind: query_row_count_bounds\n"
        "    query:\n"
        "      version: 1\n"
        "      select:\n"
        "      - {expression: {measure: measure.shop.event_count}, as: row_count}\n"
        "      time: {temporal_role: temporal_role.shop_event_occurred_at, grain: day}\n"
        "    min_rows: 1\n"
    ),
}
UPGRADED = {
    **LEGACY,
    "package.yml": LEGACY["package.yml"].replace(
        "  measure:\n    subject_entity: self\n    aggregation_entity: self\n", ""
    ),
    "metrics/core.yml": LEGACY["metrics/core.yml"].replace("    null_behavior: null_if_zero\n", ""),
}


def _package(workspace: Path, files: dict[str, str] = LEGACY) -> Path:
    project = workspace / "shop"
    for name, text in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    return project


def _contents(project: Path) -> dict[str, str]:
    return {
        path.relative_to(project).as_posix(): path.read_text()
        for path in sorted(project.rglob("*"))
        if path.is_file()
    }


def _cli(monkeypatch, capsys, cwd: Path, *args: str) -> tuple[int, str]:
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "argv", ["semantic-rails", "project", *args])
    try:
        app.main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _architect(workspace: Path, name: str, **arguments) -> dict:
    server = create_architect_mcp_server(workspace_root=workspace)
    _, structured = asyncio.run(server.call_tool(name, arguments))
    return structured


def _with_rules(monkeypatch, rules: tuple[Rule, ...]) -> None:
    """Route both surfaces to ``rules``, so a test-local rule runs end to end."""
    upgrade = functools.partial(service.upgrade_project, rules=rules)
    monkeypatch.setattr("semantic_rails.cli.commands.project.upgrade_project", upgrade)
    monkeypatch.setattr("semantic_rails.architect_mcp.upgrade_project_service", upgrade)


@pytest.mark.parametrize("surface", ["cli", "architect"])
def test_package_from_an_earlier_release_upgrades_in_one_certified_change(
    tmp_path, monkeypatch, capsys, surface
):
    project = _package(tmp_path)
    before = _contents(project)

    if surface == "cli":
        code, text = _cli(monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project))
        assert code == 0
        assert "certified: 2 rules for forms this engine refuses" in text
        assert "-    null_behavior: null_if_zero\n" in text and "-  measure:\n" in text
        assert "--write" in text
    else:
        report = _architect(tmp_path, "upgrade_project", project_path=str(project))
        assert report["status"] == "preview" and report["proof"]["tier"] == "certified"
        assert {row["id"]: row["tier"] for row in report["rules"]} == {
            "null-behavior": "certified",
            "measure-parent-rollup": "certified",
        }
        assert [hit["line"] for hit in report["rules"][0]["hits"]] == [14, 23]
    assert _contents(project) == before

    if surface == "cli":
        code, text = _cli(
            monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write"
        )
        assert code == 0, text
    else:
        report = _architect(
            tmp_path,
            "upgrade_project",
            project_path=str(project),
            dry_run=False,
            expected_revision=report["revision"],
            idempotency_key="upgrade-once",
        )
    assert _contents(project) == UPGRADED
    assert all(
        line in _contents(project)[name]
        for name, text in LEGACY.items()
        for line in text.splitlines()
        if line.lstrip().startswith("#")
    )
    load_package_snapshot(project)
    if surface == "architect":
        assert report["status"] == "upgraded"
        assert report["proof"] == {
            "tier": "certified",
            "baseline": "after_certified_rules",
            "fingerprint_before": report["proof"]["fingerprint_after"],
            "fingerprint_after": report["proof"]["fingerprint_after"],
            "masks": [],
            "examples": 1,
            "tests": 1,
        }
        assert report["next_actions"] == []

    if surface == "cli":
        code, text = _cli(monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project))
        assert (code, "Status: up_to_date") == (0, text.splitlines()[1])
    else:
        report = _architect(tmp_path, "upgrade_project", project_path=str(project))
        assert report["status"] == "up_to_date" and report["ok"] is True


def test_cli_and_architect_return_the_same_report(tmp_path, monkeypatch, capsys):
    cli_project, architect_project = _package(tmp_path / "cli"), _package(tmp_path / "mcp")
    code, text = _cli(
        monkeypatch, capsys, tmp_path, "upgrade", "--path", str(cli_project), "--json"
    )
    assert code == 0, text
    cli = json.loads(text)
    architect = _architect(tmp_path / "mcp", "upgrade_project", project_path=str(architect_project))

    keys = ("ok", "status", "rules", "proof", "choices_pending", "next_actions", "changed_files")
    assert {key: cli[key] for key in keys} == {key: architect[key] for key in keys}
    assert [row["diff"] for row in cli["changes"]] == [row["diff"] for row in architect["changes"]]


def _metric_as(files):
    for file, path, row in files.metrics():
        if row.get("as") == f"metric.shop.{path[-1]}":
            edit = Edit(file, "delete", (*path, "as"))
            yield Finding(
                "metric-as", file, files.line(file, edit.path), path, "as is the id", (edit,)
            )


def _sum_to_max(files):
    for file, path, row in files.measures():
        if row.get("default_agg") == "sum":
            edit = Edit(file, "replace", (*path, "default_agg"), value="max")
            yield Finding("sum-to-max", file, files.line(file, edit.path), path, "sum", (edit,))


def _other_metric(files):
    for file, path, query in files.queries():
        select = query.get("select") or []
        if select and select[0]["expression"].get("metric") == "metric.shop.amount_per_event":
            edit = Edit(
                file,
                "replace",
                (*path, "select", 0, "expression", "metric"),
                value="metric.shop.total_amount",
            )
            yield Finding("other-metric", file, 1, path, "select", (edit,))


METRIC_AS = Rule("metric-as", "9.9", "same_meaning", "Delete as: equal to the id", _metric_as)
SUM_TO_MAX = Rule("sum-to-max", "9.9", "same_meaning", "Wrong on purpose", _sum_to_max)
OTHER_METRIC = Rule("other-metric", "9.9", "same_meaning", "Wrong on purpose", _other_metric)
AS_FORM = ("  total_amount:\n", "  total_amount:\n    as: metric.shop.total_amount\n")


def test_rule_on_a_form_that_still_loads_is_proven(tmp_path, monkeypatch, capsys):
    files = {**UPGRADED, "metrics/core.yml": UPGRADED["metrics/core.yml"].replace(*AS_FORM, 1)}
    project = _package(tmp_path, files)
    _with_rules(monkeypatch, (*RULES, METRIC_AS))

    code, text = _cli(monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write")

    assert code == 0, text
    assert "proven: fingerprint and 2 example and test queries unchanged" in text
    assert _contents(project) == UPGRADED


@pytest.mark.parametrize("position", ["first", "last"])
def test_legacy_package_cannot_certify_a_meaning_changing_rule(tmp_path, monkeypatch, position):
    project = _package(tmp_path)
    rules = (SUM_TO_MAX, *RULES) if position == "first" else (*RULES, SUM_TO_MAX)
    _with_rules(monkeypatch, rules)
    revision = _architect(tmp_path, "project_status", project_path=str(project))["revision"]

    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=revision,
        idempotency_key="wrong-rule",
    )

    assert report["error"]["code"] == "CONFIG_CONFLICT"
    details = report["error"]["details"]
    assert (details["conflict_kind"], details["rule"]) == (
        "upgrade_not_equivalent",
        "sum-to-max",
    )
    assert _contents(project) == LEGACY


def test_only_retired_rules_form_the_baseline(tmp_path):
    files = {**LEGACY, "metrics/core.yml": LEGACY["metrics/core.yml"].replace(*AS_FORM, 1)}
    project = _package(tmp_path, files)

    report = service.upgrade_project(project, workspace_root=tmp_path, rules=(METRIC_AS, *RULES))

    assert report["proof"]["baseline"] == "after_certified_rules"
    assert {row["id"]: row["tier"] for row in report["rules"]} == {
        "metric-as": "proven",
        "null-behavior": "certified",
        "measure-parent-rollup": "certified",
    }
    assert _contents(project) == files


@pytest.mark.parametrize("surface", ["cli", "architect"])
def test_rule_cannot_certify_an_unrelated_query_failure(tmp_path, monkeypatch, capsys, surface):
    files = {
        **UPGRADED,
        "examples/core.yml": UPGRADED["examples/core.yml"]
        .replace("version: 1", "version: 2")
        .replace("metric.shop.amount_per_event", "metric.shop.missing"),
    }
    project = _package(tmp_path, files)
    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["status"] == "preview"
    assert preview["rules"][0]["tier"] == preview["proof"]["tier"] == "unverified"

    if surface == "cli":
        code, text = _cli(
            monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write", "--json"
        )
        assert code == 1, text
        report = json.loads(text)
    else:
        report = _architect(
            tmp_path,
            "upgrade_project",
            project_path=str(project),
            dry_run=False,
            expected_revision=preview["revision"],
            idempotency_key="unverified-rule",
        )
    assert report["error"]["code"] == "CONFIG_CONFLICT"
    details = report["error"]["details"]
    assert (details["conflict_kind"], details["rule"]) == (
        "upgrade_not_equivalent",
        "query-ir-version",
    )
    assert _contents(project) == files


def test_no_loadable_baseline_is_reported_as_none(tmp_path):
    files = {
        **LEGACY,
        "package.yml": LEGACY["package.yml"].replace("warehouse: duckdb", "warehouse: unsupported"),
    }
    project = _package(tmp_path, files)

    report = service.upgrade_project(project, workspace_root=tmp_path)

    assert report["proof"]["baseline"] == "none"
    assert report["proof"]["tier"] == "unverified"
    assert _contents(project) == files


def test_cli_write_uses_a_fresh_receipt_after_restoring_legacy_files(tmp_path, monkeypatch, capsys):
    project = _package(tmp_path)
    args = ("upgrade", "--path", str(project), "--write", "--json")
    code, text = _cli(monkeypatch, capsys, tmp_path, *args)
    assert code == 0, text
    first = json.loads(text)
    assert _contents(project) == UPGRADED
    for name, legacy in LEGACY.items():
        (project / name).write_text(legacy)

    code, text = _cli(monkeypatch, capsys, tmp_path, *args)

    assert code == 0, text
    second = json.loads(text)
    assert second["status"] == "upgraded"
    assert second["idempotency_key"] != first["idempotency_key"]
    assert _contents(project) == UPGRADED


@pytest.mark.parametrize("version", [2, "2"])
def test_upgraded_query_version_compiles(tmp_path, monkeypatch, capsys, version):
    files = {
        **UPGRADED,
        "examples/core.yml": UPGRADED["examples/core.yml"].replace(
            "version: 1", f"version: {version!r}"
        ),
    }
    project = _package(tmp_path, files)

    code, text = _cli(
        monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write", "--json"
    )

    assert code == 0, text
    report = json.loads(text)
    assert report["status"] == "upgraded"
    assert report["rules"][0]["tier"] == "certified"
    assert _contents(project) == UPGRADED
    runtime = Runtime.from_snapshot(load_package_snapshot(project))
    try:
        _, _, query = next(PackageFiles(project).queries())
        assert runtime.compile(dict(query))["rendered_sql"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "rules,refused,difference",
    [
        ((METRIC_AS, SUM_TO_MAX), "sum-to-max", "semantic_path"),
        ((OTHER_METRIC, METRIC_AS), "other-metric", "query"),
    ],
)
@pytest.mark.parametrize("surface", ["cli", "architect"])
def test_rule_that_changes_an_answer_is_refused_and_nothing_is_written(
    tmp_path, monkeypatch, capsys, surface, rules, refused, difference
):
    files = {**UPGRADED, "metrics/core.yml": UPGRADED["metrics/core.yml"].replace(*AS_FORM, 1)}
    project = _package(tmp_path, files)
    _with_rules(monkeypatch, (*RULES, *rules))

    if surface == "cli":
        code, text = _cli(
            monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write"
        )
        assert code == 1 and f"Upgrade rule '{refused}' changes what the package answers" in text
    else:
        report = _architect(tmp_path, "upgrade_project", project_path=str(project))
        assert report["error"]["code"] == "CONFIG_CONFLICT"
        details = report["error"]["details"]
        assert (details["conflict_kind"], details["rule"]) == ("upgrade_not_equivalent", refused)
        assert difference in details["difference"]
    assert _contents(project) == files


def _relabel(files):
    for file, path, row in files.metrics():
        if row.get("label") == "Amount per event":
            yield Finding(
                "relabel",
                file,
                files.line(file, (*path, "label")),
                path,
                f"How should '{row['label']}' read?",
                options=tuple(
                    Option(
                        option,
                        label,
                        changes,
                        (Edit(file, "replace", (*path, "label"), value=label),),
                    )
                    for option, label, changes in (
                        ("per-row", "Amount per row", False),
                        ("average", "Average amount", True),
                    )
                ),
            )


RELABEL = Rule("relabel", "9.9", "same_meaning", "Choose a label", _relabel)


def test_pending_choice_stops_the_write_and_names_its_flag(tmp_path, monkeypatch, capsys):
    project = _package(tmp_path, UPGRADED)
    _with_rules(monkeypatch, (*RULES, RELABEL))
    args = ("upgrade", "--path", str(project))

    code, text = _cli(monkeypatch, capsys, tmp_path, *args, "--write")
    assert code == 2 and _contents(project) == UPGRADED
    [flag] = re.findall(r"--choose (\S+): Average amount \(changes answers\)", text)
    assert flag.strip("'").endswith("=average")

    code, text = _cli(
        monkeypatch, capsys, tmp_path, *args, f"--choose={flag.strip(chr(39))}", "--write"
    )
    assert code == 0, text
    assert "label: Average amount" in _contents(project)["metrics/core.yml"]
    assert "= average: changes answers by your choice" in text


def test_pending_choice_through_the_architect_writes_nothing(tmp_path, monkeypatch):
    project = _package(tmp_path, UPGRADED)
    _with_rules(monkeypatch, (*RULES, RELABEL))
    status = _architect(tmp_path, "project_status", project_path=str(project))

    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=status["revision"],
        idempotency_key="choose-later",
    )

    assert (report["ok"], report["status"]) == (False, "choices_pending")
    [pending] = report["choices_pending"]
    assert [option["changes_answers"] for option in pending["options"]] == [False, True]
    assert _contents(project) == UPGRADED


def test_architect_write_needs_a_revision_and_key(tmp_path):
    project = _package(tmp_path)

    report = _architect(tmp_path, "upgrade_project", project_path=str(project), dry_run=False)

    assert report["error"]["code"] == "INVALID_MCP_ARGUMENTS"
    assert _contents(project) == LEGACY


@pytest.mark.parametrize("refusal", ["expected_revision", "idempotency_key", "stale_revision"])
def test_current_package_checks_write_arguments_and_revision_before_planning(tmp_path, refusal):
    project = _package(tmp_path, UPGRADED)
    revision = _architect(tmp_path, "project_status", project_path=str(project))["revision"]
    arguments = {"expected_revision": revision, "idempotency_key": "current-package"}
    if refusal == "stale_revision":
        arguments["expected_revision"] = "old-revision"
    else:
        arguments[refusal] = ""

    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        choices={"unknown-rule:object": "unknown-option"},
        **arguments,
    )

    if refusal == "stale_revision":
        assert report["error"]["code"] == "CONFIG_CONFLICT"
        assert report["error"]["details"]["conflict_kind"] == refusal
    else:
        assert report["error"]["code"] == "INVALID_MCP_ARGUMENTS"
        assert report["error"]["details"]["argument"] == refusal
    assert _contents(project) == UPGRADED


@pytest.mark.parametrize("intervening_edit", [False, True])
@pytest.mark.parametrize("same_intent", [True, False])
def test_architect_retry_uses_the_original_receipt_before_planning(
    tmp_path, intervening_edit, same_intent
):
    project = _package(tmp_path)
    revision = _architect(tmp_path, "project_status", project_path=str(project))["revision"]
    arguments = {
        "project_path": str(project),
        "dry_run": False,
        "expected_revision": revision,
        "idempotency_key": "upgrade-retry",
    }
    first = _architect(tmp_path, "upgrade_project", **arguments)
    assert first["status"] == "upgraded"
    if intervening_edit:
        path = project / "examples/core.yml"
        path.write_text(path.read_text().replace("version: 1", "version: 2"))
    before = _contents(project)
    if not same_intent:
        arguments["choices"] = {"unknown-rule:object": "unknown-option"}

    report = _architect(tmp_path, "upgrade_project", **arguments)

    if same_intent:
        assert report["status"] == "replayed"
        assert report["original_status"] == "upgraded"
        assert report["changes"] == first["changes"]
        assert report["proof"] == first["proof"]
        assert report["rules"] == first["rules"]
    else:
        assert report["error"]["code"] == "CONFIG_CONFLICT"
        assert report["error"]["details"]["conflict_kind"] == "idempotency_key_reuse"
    assert _contents(project) == before


def test_fresh_architect_write_on_a_current_package_is_up_to_date(tmp_path):
    project = _package(tmp_path, UPGRADED)
    revision = _architect(tmp_path, "project_status", project_path=str(project))["revision"]

    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=revision,
        idempotency_key="current-package",
    )

    assert report["status"] == "up_to_date"
    assert _contents(project) == UPGRADED


@pytest.mark.parametrize("dry_run", [False, True])
def test_rejected_architect_write_and_status_point_at_the_upgrade(tmp_path, dry_run):
    project = _package(tmp_path)
    status = _architect(tmp_path, "project_status", project_path=str(project))
    hint = "3 legacy forms have upgrade rules: call upgrade_project (dry_run)"
    assert status["ok"] is False
    assert any(action.startswith(hint) for action in status["next_actions"])

    report = _architect(
        tmp_path,
        "write_project_files",
        project_path=str(project),
        files=[{"path": "README.md", "content": "Shop package.\n"}],
        expected_revision=status["revision"],
        idempotency_key="readme",
        dry_run=dry_run,
    )

    assert report["status"] == ("preview_invalid" if dry_run else "rolled_back_after_parse_error")
    assert any(action.startswith(hint) for action in report["next_actions"])


def test_project_validate_text_points_at_the_upgrade(tmp_path, monkeypatch, capsys):
    project = _package(tmp_path)

    code, text = _cli(
        monkeypatch, capsys, tmp_path, "validate", "--path", str(project), "--mode", "parse"
    )

    assert code == 1
    assert (
        f"Hint: 3 legacy forms have upgrade rules: run `semantic-rails project upgrade --path {project}`"
        in text
    )


@pytest.mark.parametrize("cwd", ["workspace", "package", "elsewhere"])
def test_cli_never_writes_receipts_inside_the_package(tmp_path, monkeypatch, capsys, cwd):
    workspace = tmp_path / "workspace"
    project = _package(workspace)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    directory = {"workspace": workspace, "package": project, "elsewhere": elsewhere}[cwd]

    code, text = _cli(monkeypatch, capsys, directory, "upgrade", "--path", str(project), "--write")

    assert code == 0, text
    assert _contents(project) == UPGRADED
    receipts = list(tmp_path.rglob("architect-transactions/*/*.json"))
    assert len(receipts) == 1
    assert receipts[0].is_relative_to(workspace / ".semantic-rails")


@pytest.mark.parametrize("cwd", ["parent", "elsewhere"])
def test_single_file_starter_preview_and_apply(tmp_path, monkeypatch, capsys, cwd):
    source = tmp_path / "starter.yml"
    original = (ROOT / "configs/examples/semantic_rails_package_starter.yml").read_text()
    # Exercise the registered retired-form rule in the real starter.
    legacy = original.replace("defaults:\n", "defaults:\n  measure: {subject_entity: self}\n")
    source.write_text(legacy)
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "revenue.yml").write_text(
        "examples:\n  revenue:\n    query:\n"
        "      select: [{expression: {metric: metric.shop.revenue_usd}, as: revenue}]\n"
    )
    directory = tmp_path if cwd == "parent" else tmp_path / "elsewhere"
    directory.mkdir(exist_ok=True)
    args = ("upgrade", "--path", str(source))

    code, text = _cli(monkeypatch, capsys, directory, *args)
    assert code == 0 and "certified" in text
    assert source.read_text() == legacy
    assert not list(tmp_path.rglob("architect-transactions/*/*.json"))
    code, text = _cli(monkeypatch, capsys, directory, *args, "--json")
    preview = json.loads(text)
    assert code == 0 and preview["status"] == "preview"
    assert preview["proof"]["fingerprint_before"] == preview["proof"]["fingerprint_after"]
    assert preview["proof"]["examples"] == 1
    assert [(row["id"], row["tier"]) for row in preview["rules"]] == [
        ("measure-parent-rollup", "certified")
    ]

    code, text = _cli(monkeypatch, capsys, directory, *args, "--write", "--json")
    report = json.loads(text)
    assert code == 0 and report["status"] == "upgraded", text
    assert report["proof"] == preview["proof"]
    assert source.read_text() == original
    snapshot = load_package_snapshot(source)
    assert snapshot.semantic_fingerprint == report["proof"]["fingerprint_after"]
    receipts = list(tmp_path.rglob("architect-transactions/*/*.json"))
    assert len(receipts) == 1
    assert receipts[0].is_relative_to(tmp_path / ".semantic-rails")
    assert not receipts[0].is_relative_to(examples)
    code, text = _cli(monkeypatch, capsys, directory, *args, "--write", "--json")
    assert code == 0 and json.loads(text)["status"] == "up_to_date"


def test_single_file_choices_use_the_same_cli_flow(tmp_path, monkeypatch, capsys):
    source = tmp_path / "starter.yml"
    original = (ROOT / "configs/examples/semantic_rails_package_starter.yml").read_text()
    source.write_text(original.replace("label: Revenue (USD)", "label: Amount per event"))
    assert "Amount per event" in source.read_text()
    _with_rules(monkeypatch, (RELABEL,))
    args = ("upgrade", "--path", str(source), "--write")
    before = source.read_bytes()
    code, text = _cli(monkeypatch, capsys, tmp_path, *args, "--json")
    report = json.loads(text)
    assert code == 2 and report["status"] == "choices_pending"
    assert source.read_bytes() == before
    key = report["choices_pending"][0]["key"]
    code, text = _cli(monkeypatch, capsys, tmp_path, *args, f"--choose={key}=average")
    assert code == 0 and "= average: changes answers by your choice" in text
    assert "label: Average amount" in source.read_text()
    load_package_snapshot(source)


@pytest.mark.parametrize("layout", ["directory", "single-file"])
def test_unparseable_package_refuses_write_with_the_same_error(tmp_path, layout):
    if layout == "directory":
        source = _package(tmp_path)
        path = source / "package.yml"
    else:
        source = tmp_path / "starter.yml"
        source.write_text(
            (ROOT / "configs/examples/semantic_rails_package_starter.yml")
            .read_text()
            .replace("defaults:\n", "defaults:\n  measure: {subject_entity: self}\n")
        )
        path = source
    path.write_text(path.read_text().replace("warehouse: duckdb", "warehouse: unsupported"))
    before = path.read_bytes()
    with pytest.raises(service.SemanticLayerError) as exc:
        service.upgrade_project(source, workspace_root=tmp_path, dry_run=False)
    assert exc.value.code == "CONFIG_CONFLICT"
    assert exc.value.details["difference"] == {"tier": "unverified"}
    assert path.read_bytes() == before
    assert not list(tmp_path.rglob("architect-transactions/*/*.json"))


def _default_version(files):
    for file, path, query in files.queries():
        if query.get("version") == 1:
            edit = Edit(file, "delete", (*path, "version"))
            yield Finding("default-version", file, 1, path, "1 is the default", (edit,))


@pytest.mark.parametrize("surface", ["cli", "architect"])
@pytest.mark.parametrize("version", ["2", '"2"'])
@pytest.mark.parametrize(
    ("retired_key", "legacy_line"),
    [
        ("query-path-policy", "      path_policy: {max_hops: 2}\n"),
        ("null-behavior", "      null_behavior: null_if_zero\n"),
    ],
)
def test_query_rewrites_are_isolated_after_all_retired_forms(
    tmp_path, monkeypatch, capsys, surface, version, retired_key, legacy_line
):
    files = {
        **UPGRADED,
        "examples/core.yml": UPGRADED["examples/core.yml"].replace(
            "      version: 1\n", f"      version: {version}\n{legacy_line}"
        ),
    }
    project = _package(tmp_path, files)
    if surface == "cli":
        code, text = _cli(
            monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write", "--json"
        )
        assert code == 0, text
        report = json.loads(text)
    else:
        revision = _architect(tmp_path, "project_status", project_path=str(project))["revision"]
        report = _architect(
            tmp_path,
            "upgrade_project",
            project_path=str(project),
            dry_run=False,
            expected_revision=revision,
            idempotency_key="combined-query-rewrites",
        )
    assert report["status"] == "upgraded", report
    assert {row["id"]: row["tier"] for row in report["rules"]} == {
        retired_key: "certified",
        "query-ir-version": "certified",
    }
    assert _contents(project) == UPGRADED
    runtime = Runtime.from_snapshot(load_package_snapshot(project))
    try:
        for _, _, query in PackageFiles(project).queries():
            assert runtime.compile(dict(query))["rendered_sql"]
    finally:
        runtime.close()


@pytest.mark.parametrize("surface", ["cli", "architect"])
def test_mixed_query_hits_cannot_prove_a_rule_with_an_unrelated_failure(
    tmp_path, monkeypatch, capsys, surface
):
    files = {
        **UPGRADED,
        "examples/core.yml": UPGRADED["examples/core.yml"].replace(
            "metric.shop.amount_per_event", "metric.shop.missing"
        ),
    }
    project = _package(tmp_path, files)
    rule = Rule("default-version", "9.9", "same_meaning", "Delete version: 1", _default_version)
    _with_rules(monkeypatch, (rule,))
    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["status"] == "preview"
    assert preview["rules"][0]["tier"] == preview["proof"]["tier"] == "unverified"
    assert (preview["proof"]["examples"], preview["proof"]["tests"]) == (0, 1)
    if surface == "cli":
        code, text = _cli(
            monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write", "--json"
        )
        assert code == 1, text
        report = json.loads(text)
    else:
        report = _architect(
            tmp_path,
            "upgrade_project",
            project_path=str(project),
            dry_run=False,
            expected_revision=preview["revision"],
            idempotency_key="mixed-query-hits",
        )
    assert report["error"]["code"] == "CONFIG_CONFLICT"
    details = report["error"]["details"]
    assert (details["conflict_kind"], details["rule"]) == (
        "upgrade_not_equivalent",
        "default-version",
    )
    assert _contents(project) == files


def test_next_actions_name_undecided_routes_and_examples_that_fail(tmp_path):
    project = tmp_path / "jaffle_shop"
    shutil.copytree(
        ROOT / "configs/semantic_rails/jaffle_shop",
        project,
        ignore=shutil.ignore_patterns("*.duckdb"),
    )
    (project / "examples/broken.yml").write_text(
        "examples:\n  broken:\n    query:\n      version: 1\n"
        "      select: [{expression: {metric: metric.jaffle.missing}, as: missing}]\n"
    )
    rule = Rule("default-version", "9.9", "same_meaning", "Delete version: 1", _default_version)

    report = service.upgrade_project(project, workspace_root=tmp_path, rules=(rule,))

    assert (report["status"], report["proof"]["tier"]) == ("preview", "unverified")
    assert report["proof"]["examples"] > 10 and report["proof"]["tests"] > 10
    routes, broken = report["next_actions"]
    assert re.match(r"Decide the join route of \d+ entity pairs \(entity\.jaffle_", routes)
    assert broken == (
        "Example examples/broken.yml:examples.broken.query does not compile: OBJECT_NOT_FOUND."
    )


def test_every_rule_is_documented():
    guide = (ROOT / "docs/PACKAGE_AUTHORING.md").read_text()
    assert len({rule.id for rule in RULES}) == len(RULES)
    assert [rule.id for rule in RULES if f"`{rule.id}`" not in guide] == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_single_file_transaction_parse_gate_restores_invalid_updates(tmp_path, dry_run):
    source = tmp_path / "starter.yml"
    original = (ROOT / "configs/examples/semantic_rails_package_starter.yml").read_bytes()
    source.write_bytes(original)
    transaction = ProjectTransaction(tmp_path, workspace_root=tmp_path, package_file=source.name)
    report = transaction.apply(
        [ProjectFileUpdate(source.name, original.replace(b"warehouse: duckdb", b"warehouse: bad"))],
        expected_revision=transaction.current_revision(),
        idempotency_key="invalid-update",
        intent={"operation": "test"},
        dry_run=dry_run,
    ).report
    assert report["status"] == ("preview_invalid" if dry_run else "rolled_back_after_parse_error")
    assert report["errors"][0]["code"] == "INVALID_CONFIG"
    assert source.read_bytes() == original
    receipts = list(tmp_path.rglob("architect-transactions/*/*.json"))
    assert len(receipts) == (0 if dry_run else 1)
    load_package_snapshot(source)


def test_architect_upgrades_a_single_file_and_replays_its_receipt(tmp_path):
    source = tmp_path / "starter.yml"
    original = (ROOT / "configs/examples/semantic_rails_package_starter.yml").read_text()
    source.write_text(
        original.replace("defaults:\n", "defaults:\n  measure: {subject_entity: self}\n")
    )
    preview = _architect(tmp_path, "upgrade_project", project_path=str(source))
    assert preview["status"] == "preview", preview
    arguments = dict(
        project_path=str(source),
        dry_run=False,
        expected_revision=preview["revision"],
        idempotency_key="single-file-upgrade",
    )
    report = _architect(tmp_path, "upgrade_project", **arguments)
    assert report["status"] == "upgraded", report
    assert source.read_text() == original
    replay = _architect(tmp_path, "upgrade_project", **arguments)
    assert replay["status"] == "replayed"
    assert replay["proof"] == report["proof"]
    sibling = tmp_path / "other.yml"
    sibling.write_text(original)
    refused = _architect(tmp_path, "upgrade_project", **{**arguments, "project_path": str(sibling)})
    assert refused["error"]["details"]["conflict_kind"] == "idempotency_key_reuse"
