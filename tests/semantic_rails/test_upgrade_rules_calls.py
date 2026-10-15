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
        "      - {field: dimension.shop_event_event_type, op: IN, value: [signup]}\n"
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
        "      - {field: dimension.shop_event_event_type, op: is, value: [signup]}\n"
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
        ("query-call-v2", "translated")
    ]
    hits = {(hit["file"], hit["line"]): hit["message"] for hit in preview["rules"][0]["hits"]}
    assert sorted(hits) == [
        ("examples/core.yml", 5),
        ("examples/core.yml", 17),
        ("tests/core.yml", 4),
        ("tests/core.yml", 11),
    ]
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


def test_current_calls_are_not_touched(tmp_path):
    project = _package(tmp_path, {**PREVIOUS, "examples/core.yml": UPGRADED["examples/core.yml"]})
    files = PackageFiles(project)

    findings = plan(files, RULES, {}).findings

    assert [(f.rule, f.file, f.path[:2]) for f in findings] == [
        ("query-call-v2", "tests/core.yml", ("tests", "count_by_day")),
        ("query-call-v2", "tests/core.yml", ("tests", "daily_amounts")),
    ]


# Keep this table in sync with the package runner's test-kind registry.
QUERY_FIELDS = {
    "query_returns_columns": ("query",),
    "query_row_count_bounds": ("query",),
    "query_matches_snapshot": ("query",),
    "validate_fails_with_code": ("query",),
    "explain_contains": ("query",),
    "metric_equals_query": ("metric_query", "expected_query"),
}
OLD_QUERY = {"select": [{"measure": "measure.shop.total_amount"}]}
NEW_QUERY = {"select": ["measure.shop.total_amount"]}


def test_query_field_coverage_matches_the_test_kind_registry():
    from semantic_rails.architect_service import _TEST_FIELDS

    assert {
        kind: tuple(key for key in fields if key.endswith("query"))
        for kind, fields in _TEST_FIELDS.items()
    } == QUERY_FIELDS


@pytest.mark.parametrize("kind,fields", QUERY_FIELDS.items())
def test_every_test_kind_rewrites_or_names_each_query(tmp_path, kind, fields):
    from semantic_rails.architect_scaffold import dump_project_yaml

    query = (
        {"select": [{"metric": "metric.shop.missing"}]}
        if kind == "validate_fails_with_code"
        else OLD_QUERY
    )
    spec = {"kind": kind, "code": "OBJECT_NOT_FOUND", **dict.fromkeys(fields, query)}
    files = {
        **UPGRADED,
        "examples/core.yml": "examples: {}\n",
        "tests/core.yml": dump_project_yaml({"tests": {"amount": spec}}),
    }
    project = _package(tmp_path, files)
    result = plan(PackageFiles(project), RULES, {})
    hits = [f for f in result.findings if f.rule == "query-call-v2"]
    assert {f.path[-1] for f in hits} == set(fields)
    if kind == "validate_fails_with_code":
        assert not result.files
        assert all(
            not f.edits
            and "hand" in f.message
            and "OBJECT_NOT_FOUND" in f.message
            and "Hint:" in f.message
            for f in hits
        )
    else:
        rewritten = PackageFiles(
            project, contents={**PackageFiles(project).contents, **result.files}
        )
        spec = rewritten.documents["tests/core.yml"]["tests"]["amount"]
        assert all(spec[field] == NEW_QUERY for field in fields)


def test_comparison_queries_share_the_translation_check(tmp_path):
    from semantic_rails.architect_scaffold import dump_project_yaml
    from semantic_rails.package_tools import _run_test

    spec = {"kind": "metric_equals_query", "metric_query": OLD_QUERY, "expected_query": OLD_QUERY}
    project = _package(
        tmp_path,
        {
            **UPGRADED,
            "examples/core.yml": "examples: {}\n",
            "tests/core.yml": dump_project_yaml({"tests": {"amount": spec}}),
        },
    )
    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["proof"]["tier"] == "translated", preview
    assert preview["proof"]["fingerprint_before"] == preview["proof"]["fingerprint_after"]
    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=preview["revision"],
        idempotency_key="comparison",
    )
    assert report["status"] == "upgraded", report
    import duckdb

    (project / "data").mkdir()
    with duckdb.connect(str(project / "data/shop.duckdb")) as connection:
        connection.execute(
            "CREATE TABLE raw_events (event_id INTEGER, amount INTEGER, "
            "occurred_at TIMESTAMP, event_type VARCHAR)"
        )
        connection.execute(
            "INSERT INTO raw_events VALUES (1, 3, '2024-01-01', 'signup'), "
            "(2, 4, '2024-01-02', 'purchase')"
        )
        reference_total = connection.execute("SELECT SUM(amount) FROM raw_events").fetchone()[0]
    runtime = Runtime.from_snapshot(load_package_snapshot(project))
    try:
        entry = PackageFiles(project).documents["tests/core.yml"]["tests"]["amount"]
        assert _run_test(runtime, "amount", entry, NOW)["ok"]
        assert runtime.query(entry["metric_query"], now=NOW)["rows"] == [
            {"total_amount": reference_total}
        ]
        assert reference_total == 7
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "query,code",
    [
        ({"select": [{"metric": "metric.shop.missing"}]}, "OBJECT_NOT_FOUND"),
        ({"select": [{"measure": "measure.shop.missing"}]}, "OBJECT_NOT_FOUND"),
        (
            {**OLD_QUERY, "group_by": [{"dimension": "dimension.shop_event_missing"}]},
            "OBJECT_NOT_FOUND",
        ),
        (
            {
                **OLD_QUERY,
                "time": {"temporal_role": "temporal_role.shop_event_missing", "grain": "day"},
            },
            "INVALID_TEMPORAL_ROLE",
        ),
        (
            {
                **OLD_QUERY,
                "time": {
                    "temporal_role": "temporal_role.shop_event_occurred_at",
                    "grain": "quarter",
                    "calendar_id": "fiscal",
                },
            },
            "INVALID_QUERY",
        ),
    ],
    ids=["metric", "measure", "dimension", "clock", "calendar"],
)
def test_refusal_queries_are_explicit_hand_edits_and_write_nothing(tmp_path, query, code):
    from semantic_rails.architect_scaffold import dump_project_yaml

    project = _package(
        tmp_path,
        {
            **UPGRADED,
            "examples/core.yml": "examples: {}\n",
            "tests/core.yml": dump_project_yaml(
                {
                    "tests": {
                        "refusal": {
                            "kind": "validate_fails_with_code",
                            "code": code,
                            "query": query,
                        }
                    }
                }
            ),
        },
    )
    before = _contents(project)
    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["status"] == "unverified", preview
    reason = preview["rules"][0]["reason"]
    assert "tests/core.yml:" in reason and code in reason and "Hint:" in reason
    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=preview["revision"],
        idempotency_key="refusal",
    )
    assert not report["ok"], report
    assert _contents(project) == before
    from semantic_rails.upgrade.v1_to_v2 import translate_query

    runtime = Runtime.from_snapshot(load_package_snapshot(project))
    try:
        validation = runtime.validate(translate_query(query).query, now=NOW)
        assert not validation["ok"] and validation["errors"][0]["code"] == code
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "op,value",
    [("LIKE", "sign%"), ("!=", "signup"), ("NOT IN", ["signup"]), ("NOT LIKE", "%sign%")],
)
def test_answer_changing_translations_require_a_hand_edit(tmp_path, op, value):
    from semantic_rails.architect_scaffold import dump_project_yaml

    query = {
        **OLD_QUERY,
        "where": [{"field": "dimension.shop_event_event_type", "op": op, "value": value}],
    }
    project = _package(
        tmp_path,
        {
            **UPGRADED,
            "tests/core.yml": "tests: {}\n",
            "examples/core.yml": dump_project_yaml({"examples": {"amount": {"query": query}}}),
        },
    )
    before = _contents(project)
    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["status"] == "unverified", preview
    assert "hand" in preview["rules"][0]["reason"]
    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=preview["revision"],
        idempotency_key="changed-meaning",
    )
    assert not report["ok"]
    assert _contents(project) == before


@pytest.mark.parametrize("field", ["query", "metric_query", "expected_query"])
@pytest.mark.parametrize("call", [{"now": NOW}, {"audience": "finance"}])
def test_comparison_call_arguments_need_a_hand_edit(tmp_path, field, call):
    from semantic_rails.architect_scaffold import dump_project_yaml

    old = {**OLD_QUERY, "policy_context": call}
    spec = {
        "kind": "metric_equals_query",
        "metric_query": NEW_QUERY,
        "expected_query": NEW_QUERY,
        field: old,
    }
    if field == "query":
        spec.pop("metric_query")  # The runner accepts query as the comparison's first call.
    project = _package(
        tmp_path,
        {
            **UPGRADED,
            "examples/core.yml": "examples: {}\n",
            "tests/core.yml": dump_project_yaml({"tests": {"amount": spec}}),
        },
    )
    result = plan(PackageFiles(project), RULES, {})
    assert result.pending
    assert not result.files
    assert any("both queries" in f.message and "hand" in f.message for f in result.pending)


@pytest.mark.parametrize("field", ["metric_query", "expected_query"])
def test_proof_refuses_a_changed_comparison_answer(tmp_path, monkeypatch, field):
    from semantic_rails.architect_scaffold import dump_project_yaml
    from semantic_rails.upgrade.model import Edit, Finding, Rule
    from tests.semantic_rails.test_project_upgrade import _with_rules

    def change_answer(files):
        for file, path, query in files.queries():
            if path[-1] == field and query == NEW_QUERY:
                edit = Edit(file, "replace", (*path, "select", 0), value="measure.shop.event_count")
                yield Finding("change-answer", file, 1, path, "select", (edit,))

    _with_rules(
        monkeypatch,
        (Rule("change-answer", "0.3.2", "same_meaning", "Wrong on purpose", change_answer),),
    )
    spec = {"kind": "metric_equals_query", "metric_query": NEW_QUERY, "expected_query": NEW_QUERY}
    project = _package(
        tmp_path,
        {
            **UPGRADED,
            "examples/core.yml": "examples: {}\n",
            "tests/core.yml": dump_project_yaml({"tests": {"amount": spec}}),
        },
    )
    before = _contents(project)
    revision = _architect(tmp_path, "project_status", project_path=str(project))["revision"]
    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=revision,
        idempotency_key="changed-answer",
    )
    assert not report["ok"], report
    assert report["error"]["code"] == "CONFIG_CONFLICT"
    assert report["error"]["details"]["difference"]["query"].endswith(field)
    assert _contents(project) == before
