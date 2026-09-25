"""plan picks the measure the question names, and says so when it can't tell.

"revenue" matches Revenue and Item Revenue Cents equally by score. The one the
question names wins; when it names none of the tied measures, plan returns
``low_confidence`` with the candidates instead of the first label.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime


def _runtime(tmp_path: Path, measures: dict[str, str]) -> Runtime:
    """A one-model package whose sum measures have these keys and labels."""

    (tmp_path / "models").mkdir()
    (tmp_path / "data" / "shop_csv").mkdir(parents=True)
    (tmp_path / "data" / "shop_csv" / "orders.csv").write_text(
        "order_id,ordered_at,amount\n1,2026-01-01T09:00:00,100\n", encoding="utf-8"
    )
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "shop",
                "namespace": "shop",
                "name": "shop",
                "warehouse": "duckdb",
                "default_db": "data/shop.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/shop_csv"},
            },
        },
        "graph.yml": {"graph": {"entities": {"order": {"key": ["order_id"], "model": "orders"}}}},
        "models/orders.yml": {
            "model": {
                "id": "orders",
                "relation": "orders",
                "entities": {"order": {}},
                "times": {
                    "ordered_at": {"column": "ordered_at", "kind": "timestamp", "default": True}
                },
                "measures": {
                    key: {
                        "label": label,
                        "kind": "aggregate",
                        "expr": "amount",
                        "default_agg": "sum",
                    }
                    for key, label in measures.items()
                },
            }
        },
    }
    for name, body in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return Runtime.from_path(str(tmp_path))


REVENUE_AND_ITEMS = {"revenue": "Revenue", "item_revenue_cents": "Item Revenue Cents"}


@pytest.mark.parametrize(
    ("question", "measure", "unmatched"),
    [
        ("What is revenue by month?", "revenue", []),
        ("What is completed revenue by month?", "revenue", ["completed"]),
        ("item revenue by month", "item_revenue_cents", []),
        ("item revenue cents by month", "item_revenue_cents", []),
    ],
)
def test_the_named_measure_wins_a_tie(
    tmp_path: Path, question: str, measure: str, unmatched: list[str]
) -> None:
    runtime = _runtime(tmp_path, REVENUE_AND_ITEMS)
    try:
        plan = plan_payload(runtime, intent=question)
    finally:
        runtime.close()

    assert plan["status"] == "ok", plan.get("why")
    [select] = plan["best"]["query_ir"]["select"]
    # A package's measures come with same-named metrics; either answers.
    assert (select["expression"].get("measure") or select["expression"]["metric"]).endswith(
        f"shop.{measure}"
    )
    assert [
        term for row in plan.get("warnings", []) for term in row["details"]["terms"]
    ] == unmatched


def test_a_tie_the_question_names_no_side_of_is_low_confidence(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, {"gross_revenue": "Gross Revenue", "net_revenue": "Net Revenue"})
    try:
        plan = plan_payload(runtime, intent="What is revenue by month?")
    finally:
        runtime.close()

    assert plan["status"] == "low_confidence"
    [gap] = plan["why"]["details"]["gaps"]
    assert gap["kind"] == "subject_ambiguous"
    assert gap["expected"]["candidates"] == [
        "measure.shop.gross_revenue",
        "measure.shop.net_revenue",
    ]
    assert (
        "Gross Revenue (measure.shop.gross_revenue)" in plan["why"]["recovery_hints"][0]["message"]
    )
