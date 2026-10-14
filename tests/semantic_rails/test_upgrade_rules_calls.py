"""`project upgrade` rewrites example and test queries written in the previous call shape."""

from __future__ import annotations

import json

import pytest

from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.registry import RULES
from tests.semantic_rails.test_project_upgrade import (
    UPGRADED,
    _architect,
    _cli,
    _contents,
    _package,
)

NOW = "2024-07-05T00:00:00Z"
# Examples and tests written for the previous call shape, with comments the upgrade keeps.
PREVIOUS = {
    **UPGRADED,
    "examples/core.yml": (
        "examples:\n"
        "  # Written for the previous call shape.\n"
        "  amount_by_type:\n"
        "    question: Amount per event by type\n"
        "    query:\n"
        "      version: 1\n"
        "      select:\n"
        "      - expression: {kind: metric, metric: metric.shop.amount_per_event}\n"
        "        as: amount_per_event\n"
        "      - {dimension: dimension.shop_event_event_type}\n"
        "      where:\n"
        "      - {field: dimension.shop_event_event_type, op: LIKE, value: 'sign%'}\n"
        f"      policy_context: {{now: '{NOW}'}}\n"
        "    # One row per type.\n"
        "    expected_shape: {min_rows: 1}\n"
        "  amount_last_week:\n"
        "    query:\n"
        "      select: [{measure: measure.shop.total_amount}]\n"
        "      time: {temporal_role: temporal_role.shop_event_occurred_at, grain: day,"
        " range: {last: {unit: week, value: 1}}}\n"
        "      order_by: [{field: time, direction: DESC}]\n"
        "    expected_shape:\n"
        "      columns: [temporal_role.shop_event_occurred_at__day, total_amount]\n"
    ),
    "tests/core.yml": (
        "tests:\n"
        "  count_by_day:\n"
        "    kind: query_returns_columns\n"
        "    query:\n"
        "      select:\n"
        "      - {expression: {measure: measure.shop.event_count}, as: row_count}\n"
        "      time: {temporal_role: temporal_role.shop_event_occurred_at, grain: day}\n"
        "    columns: [temporal_role.shop_event_occurred_at__day, row_count]\n"
        "  daily_amounts:\n"
        "    kind: query_matches_snapshot\n"
        "    query:\n"
        "      select: [{measure: measure.shop.total_amount}]\n"
        "      time: {temporal_role: temporal_role.shop_event_occurred_at, grain: day}\n"
        "    expected_rows:\n"
        "    - {temporal_role.shop_event_occurred_at__day: '2024-01-01', total_amount: 3}\n"
        "    - {temporal_role.shop_event_occurred_at__day: '2024-01-02', total_amount: 4}\n"
        "  # A refusal test keeps the call it expects to be refused.\n"
        "  overrides_are_refused:\n"
        "    kind: validate_fails_with_code\n"
        "    code: INVALID_QUERY\n"
        "    query:\n"
        "      select: [{measure: measure.shop.event_count}]\n"
        "      temporal_role_overrides: {measure.shop.event_count: temporal_role.x}\n"
    ),
}
CURRENT = {
    **PREVIOUS,
    "examples/core.yml": (
        "examples:\n"
        "  # Written for the previous call shape.\n"
        "  amount_by_type:\n"
        "    question: Amount per event by type\n"
        "    query:\n"
        "      select:\n"
        "      - metric.shop.amount_per_event\n"
        "      group_by:\n"
        "      - dimension.shop_event_event_type\n"
        "      where:\n"
        "      - {field: dimension.shop_event_event_type, op: starts_with, value: sign}\n"
        f"    now: '{NOW}'\n"
        "    # One row per type.\n"
        "    expected_shape: {min_rows: 1}\n"
        "  amount_last_week:\n"
        "    query:\n"
        "      select: [measure.shop.total_amount]\n"
        "      time: {temporal_role: temporal_role.shop_event_occurred_at, grain: day,"
        " last: {unit: week, value: 1}}\n"
        "      order_by: [{field: period, direction: desc}]\n"
        "    expected_shape:\n"
        "      columns: [period, total_amount]\n"
    ),
    "tests/core.yml": PREVIOUS["tests/core.yml"]
    .replace(
        "      select:\n      - {expression: {measure: measure.shop.event_count}, as: row_count}\n",
        "      select:\n      - {expression: measure.shop.event_count, as: row_count}\n",
    )
    .replace(
        "    columns: [temporal_role.shop_event_occurred_at__day, row_count]\n",
        "    columns: [period, row_count]\n",
    )
    .replace(
        "      select: [{measure: measure.shop.total_amount}]\n",
        "      select: [measure.shop.total_amount]\n",
    )
    .replace("{temporal_role.shop_event_occurred_at__day: ", "{period: "),
}


def _compiles(project) -> None:
    runtime = Runtime.from_snapshot(load_package_snapshot(project))
    try:
        for file, path, query in PackageFiles(project).queries():
            if path[1] != "overrides_are_refused":
                assert runtime.compile(dict(query), now=NOW)["rendered_sql"], (file, path)
    finally:
        runtime.close()


@pytest.mark.parametrize("surface", ["cli", "architect"])
def test_previous_call_examples_and_tests_are_rewritten(tmp_path, monkeypatch, capsys, surface):
    project = _package(tmp_path, PREVIOUS)

    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["status"] == "preview", preview
    assert [(row["id"], row["tier"]) for row in preview["rules"]] == [
        ("query-call-v2", "certified")
    ]
    hits = {(hit["file"], hit["line"]): hit["message"] for hit in preview["rules"][0]["hits"]}
    assert sorted(hits) == [
        ("examples/core.yml", 5),
        ("examples/core.yml", 17),
        ("tests/core.yml", 4),
        ("tests/core.yml", 11),
    ]
    assert "LIKE 'sign%' became starts_with, which ignores case" in hits[("examples/core.yml", 5)]
    assert _contents(project) == PREVIOUS

    if surface == "cli":
        code, text = _cli(
            monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write", "--json"
        )
        assert code == 0, text
        report = json.loads(text)
    else:
        report = _architect(
            tmp_path,
            "upgrade_project",
            project_path=str(project),
            dry_run=False,
            expected_revision=preview["revision"],
            idempotency_key="previous-calls",
        )
    assert report["status"] == "upgraded", report
    assert _contents(project) == CURRENT
    _compiles(project)
    assert plan(PackageFiles(project), RULES, {}).findings == ()


@pytest.mark.parametrize(
    ("example", "line", "message"),
    [
        (
            "      where: [{field: dimension.shop_event_event_type, op: LIKE, value: 's_gn%'}]\n",
            7,
            "query.where[0].op: LIKE 's_gn%' has no text word",
        ),
        (
            "      route_decisions: [customer_order]\n",
            7,
            "query.route_decisions[0]: a route option id",
        ),
        (
            "      max_rows: 5\n",
            7,
            "query.max_rows: an example or test has no max_rows",
        ),
    ],
)
def test_an_untranslatable_query_stops_the_upgrade_at_its_site(
    tmp_path, monkeypatch, capsys, example, line, message
):
    files = {
        **UPGRADED,
        "examples/core.yml": (
            "examples:\n"
            "  amount:\n"
            "    question: Amount\n"
            "    query:\n"
            "      select:\n"
            "      - {measure: measure.shop.total_amount}\n" + example
        ),
    }
    project = _package(tmp_path, files)

    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["rules"][0]["id"] == "query-call-v2"
    assert preview["rules"][0]["tier"] == preview["proof"]["tier"] == "unverified"
    reason = preview["rules"][0]["reason"]
    assert reason.startswith(f"stop: examples/core.yml:{line} {message}"), reason

    code, text = _cli(monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--write")
    assert code == 1, text
    assert f"examples/core.yml:{line} {message}" in text
    assert _contents(project) == files


def test_retired_spellings_inside_a_previous_call_are_rewritten_with_it(tmp_path):
    files = {
        **UPGRADED,
        "examples/core.yml": (
            "examples:\n"
            "  margin:\n"
            "    query:\n"
            "      path_policy: {max_hops: 2}\n"
            "      select:\n"
            "      - as: margin\n"
            "        expression:\n"
            "          kind: binary\n"
            "          op: sub\n"
            "          left: {kind: measure_ref, measure: measure.shop.total_amount}\n"
            "          right: {metric: metric.shop.total_amount}\n"
            "          null_behavior: zero\n"
            "  current_margin:\n"
            "    query:\n"
            "      select:\n"
            "      - as: margin\n"
            "        expression:\n"
            "          kind: binary\n"
            "          op: subtract\n"
            "          left: measure.shop.total_amount\n"
            "          right: metric.shop.total_amount\n"
        ),
    }
    project = _package(tmp_path, files)

    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))

    assert {row["id"]: [hit["line"] for hit in row["hits"]] for row in preview["rules"]} == {
        "expression-arithmetic": [18],
        "query-call-v2": [3],
    }
    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=preview["revision"],
        idempotency_key="retired-inside",
    )
    assert report["status"] == "upgraded", report
    examples = _contents(project)["examples/core.yml"]
    assert examples.startswith(
        "examples:\n"
        "  margin:\n"
        "    query:\n"
        "      select:\n"
        "      - as: margin\n"
        "        expression:\n"
        "          kind: arithmetic\n"
        "          op: subtract\n"
        "          left: measure.shop.total_amount\n"
        "          right: metric.shop.total_amount\n"
        "  current_margin:\n"
    )
    assert "kind: binary" not in examples
    _compiles(project)


def test_current_calls_and_refusal_tests_are_not_touched(tmp_path):
    project = _package(tmp_path, {**PREVIOUS, "examples/core.yml": UPGRADED["examples/core.yml"]})
    files = PackageFiles(project)

    findings = plan(files, RULES, {}).findings

    assert [(f.rule, f.file, f.path[:2]) for f in findings] == [
        ("query-call-v2", "tests/core.yml", ("tests", "count_by_day")),
        ("query-call-v2", "tests/core.yml", ("tests", "daily_amounts")),
    ]
