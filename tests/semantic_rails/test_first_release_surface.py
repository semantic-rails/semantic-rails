from __future__ import annotations

from pathlib import Path

from scripts.benchmark_plan import _scorecard_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_scorecard_markdown_is_release_evidence_not_competitor_claim():
    rows = [
        {
            "name": "orders_by_month",
            "expected": "answer",
            "status": "ok",
            "pattern": "metric_by_dimension_rollup",
            "actionable": True,
            "best_bytes": 100,
            "full_bytes": 200,
            "plan_ms": 1.0,
            "compile_cache_hit": True,
            "compile_ms": 0.5,
            "compile_attempted": True,
            "missing_expected_ids": [],
            "missing_expected_query_patch": [],
        },
        {
            "name": "off_topic",
            "expected": "reject",
            "status": "out_of_scope",
            "pattern": "",
            "actionable": True,
            "best_bytes": 50,
            "full_bytes": 60,
            "plan_ms": 0.5,
            "compile_cache_hit": False,
            "compile_ms": 0.0,
            "compile_attempted": False,
            "missing_expected_ids": [],
            "missing_expected_query_patch": [],
        },
    ]
    markdown = _scorecard_markdown(
        rows,
        max_best_bytes=6500,
        max_cache_hit_p95_ms=25.0,
    )

    assert "Agentic Governance Benchmark Scorecard" in markdown
    assert "not a competitor benchmark" in markdown
    assert "actionable expected answers: 1/1" in markdown
    assert "| orders_by_month | answer | ok | metric_by_dimension_rollup |" in markdown


def test_first_release_docs_are_linked_from_public_indexes():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    docs_index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    for required in (
        "docs/AGENT_QUICKSTART.md",
        "docs/BENCHMARK_EVIDENCE.md",
    ):
        assert required in readme

    for required in (
        "AGENT_QUICKSTART.md",
        "BENCHMARK_EVIDENCE.md",
    ):
        assert required in docs_index
