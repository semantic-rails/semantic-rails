"""Query upgrades stop when spelling changes cannot preserve the previous answer."""

from __future__ import annotations

import json

import pytest

from semantic_rails.architect_scaffold import dump_project_yaml
from semantic_rails.runtime import Runtime
from semantic_rails.upgrade.v1_to_v2 import FLAG_CODES, REVIEW, translate_query
from tests.semantic_rails.test_project_upgrade import (
    UPGRADED,
    _architect,
    _cli,
    _contents,
    _package,
)
from tests.semantic_rails.test_upgrade_rules_calls import NEW_QUERY, NOW, OLD_QUERY, PREVIOUS

ROLE = "temporal_role.shop_event_occurred_at"
DIM = "dimension.shop_event_event_type"
PREDICATE = {
    "kind": "metric_predicate",
    "entity": "entity.shop_customer",
    "input": {"kind": "measure", "measure": "measure.shop.total_amount"},
    "scope_mode": "contextual",
    "op": ">",
    "value": 50,
}
CONTEXTUAL = {
    **OLD_QUERY,
    "version": 1,
    "group_by": [DIM],
    "time": {"temporal_role": ROLE, "grain": "day"},
    "metric_filters": [{"expression": PREDICATE, "op": "=", "value": True}],
}


@pytest.mark.parametrize(
    "query,reason",
    [
        (CONTEXTUAL, "grouping context and inherited filters"),
        ({**CONTEXTUAL, "group_by": []}, "grouping context and inherited filters"),
        (
            {**CONTEXTUAL, "where": [{"field": DIM, "op": "=", "value": "signup"}]},
            "grouping context and inherited filters",
        ),
        (
            {
                **OLD_QUERY,
                "time": {"temporal_role": ROLE, "grain": "day", "start": "2024-07-01"},
                "policy_context": {"now": NOW},
            },
            "upper bound",
        ),
        (
            {**OLD_QUERY, "where": [{"field": DIM, "op": "LIKE", "value": "sign%"}]},
            "ignore case",
        ),
    ],
    ids=["contextual-group", "contextual-day", "contextual-inherited-filter", "start-only", "like"],
)
def test_unsafe_query_rewrites_stop_and_write_nothing(tmp_path, query, reason):
    files = {
        **UPGRADED,
        "examples/core.yml": dump_project_yaml({"examples": {"amount": {"query": query}}}),
        "tests/core.yml": "tests: {}\n",
    }
    project = _package(tmp_path, files)
    preview = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert preview["status"] == "unverified", preview
    row = next(row for row in preview["rules"] if row["id"] == "query-call-v2")
    assert reason in row["reason"] and "Hint:" in row["reason"]
    assert not preview["changes"]
    report = _architect(
        tmp_path,
        "upgrade_project",
        project_path=str(project),
        dry_run=False,
        expected_revision=preview["revision"],
        idempotency_key="unsafe-query",
    )
    assert not report["ok"], report
    assert _contents(project) == files


def test_like_case_difference_is_a_review_flag_with_a_real_answer_difference(tmp_path):
    import duckdb

    from semantic_rails.package_snapshot import load_package_snapshot

    query = {**OLD_QUERY, "where": [{"field": DIM, "op": "LIKE", "value": "sign%"}]}
    result = translate_query(query)
    assert any(
        flag["code"] == "CONTAINS_CASE_INSENSITIVE" and FLAG_CODES[flag["code"]][0] == REVIEW
        for flag in result.flags
    )
    project = _package(tmp_path, UPGRADED)
    (project / "data").mkdir()
    with duckdb.connect(str(project / "data/shop.duckdb")) as connection:
        connection.execute("CREATE TABLE raw_events (amount INTEGER, event_type VARCHAR)")
        connection.execute("INSERT INTO raw_events VALUES (10, 'signup'), (20, 'SIGNUP')")
        before = connection.execute(
            "SELECT SUM(amount) FROM raw_events WHERE event_type LIKE 'sign%'"
        ).fetchone()[0]
    runtime = Runtime.from_snapshot(load_package_snapshot(project))
    try:
        after = runtime.query(result.query, now=NOW)["rows"][0]["total_amount"]
        assert (before, after) == (10, 30)
    finally:
        runtime.close()


def test_query_preview_reports_translation_without_certifying_answers(
    tmp_path, monkeypatch, capsys
):
    project = _package(tmp_path, PREVIOUS)
    code, text = _cli(monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project))
    assert code == 0, text
    assert "translated: spelling-only query rewrites; review the diff" in text
    assert "certified" not in text
    code, text = _cli(monkeypatch, capsys, tmp_path, "upgrade", "--path", str(project), "--json")
    report = json.loads(text)
    assert code == 0 and report["proof"]["tier"] == "translated", report
    row = next(row for row in report["rules"] if row["id"] == "query-call-v2")
    assert row["tier"] == "translated"
    assert "not run" in row["reason"] and "no before/after answer comparison" in row["reason"]
    assert _contents(project) == PREVIOUS


@pytest.mark.parametrize("field", ["query", "metric_query", "expected_query"])
def test_compile_checks_keep_entry_clock_and_context(tmp_path, monkeypatch, field):
    context = {"audience": "finance"}
    entry = {"now": NOW, "context": context, field: OLD_QUERY}
    section = "examples" if field == "query" else "tests"
    if section == "tests":
        entry.update(kind="metric_equals_query")
        entry["expected_query" if field == "metric_query" else "metric_query"] = NEW_QUERY
    project = _package(
        tmp_path,
        {
            **UPGRADED,
            "examples/core.yml": "examples: {}\n",
            "tests/core.yml": "tests: {}\n",
            f"{section}/core.yml": dump_project_yaml({section: {"amount": entry}}),
        },
    )
    seen = []
    original = Runtime.compile

    def compile_call(self, query, **kwargs):
        if query == NEW_QUERY:
            seen.append(kwargs)
        return original(self, query, **kwargs)

    monkeypatch.setattr(Runtime, "compile", compile_call)
    report = _architect(tmp_path, "upgrade_project", project_path=str(project))
    assert report["status"] == "preview", report
    assert seen and all(call.get("now") == NOW and call.get("context") == context for call in seen)
