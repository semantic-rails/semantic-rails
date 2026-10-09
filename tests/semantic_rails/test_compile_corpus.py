"""The compile-corpus record is deterministic and its compare names what changed."""

import json

from scripts.dev import compile_corpus

LEDGER = "tests/integration/correctness/shop/tests::"
SAMPLE = [
    "examples/jaffle_shop_revenue_by_store.json",
    "examples/anchored_window_cohort_retention.json",  # refused: contract only
    f"{compile_corpus.SNAPSHOTS}::SNAPSHOT_CASES[rolling]",  # engine form
    f"{LEDGER}shop/ny_authored-revenue_by_month",  # a ledger case on a package variant
]
DIALECTS = ("bigquery", "duckdb")


def test_corpus_draws_on_every_source():
    ids = [query.id for query in compile_corpus.corpus()]

    assert len(ids) == len(set(ids))
    sources = [
        "configs/semantic_rails/jaffle_shop/examples::",
        "configs/semantic_rails/jaffle_shop/tests::",
        LEDGER,
        f"{compile_corpus.COMPARISON}/queries/",
        "examples/",
        f"{compile_corpus.BLIND_CORPUS}::",
        f"{compile_corpus.SNAPSHOTS}::",
        f"{compile_corpus.BASELINE}::",
    ]
    assert all(any(name.startswith(source) for name in ids) for source in sources)


def test_record_is_byte_identical_and_compare_names_query_dialect_and_field(tmp_path, capsys):
    first = compile_corpus.record(dialects=DIALECTS, only=SAMPLE, jobs=1)
    second = compile_corpus.record(dialects=DIALECTS, only=SAMPLE, jobs=1)

    assert compile_corpus.dumps(first) == compile_corpus.dumps(second)
    assert sorted(first["queries"]) == sorted(SAMPLE)
    assert first["summary"] == {
        "queries": 4,
        "results": 8,
        "compiled": 6,
        "refused": 2,
        "crashed": 0,
        "by_dialect": {name: {"compiled": 3, "refused": 1, "crashed": 0} for name in DIALECTS},
    }
    results = first["queries"][SAMPLE[0]]["results"]
    assert results["bigquery"]["rendered_sql"] != results["duckdb"]["rendered_sql"]
    assert first["queries"][SAMPLE[1]]["results"]["duckdb"] == {
        "refusal": {"code": "INVALID_ANCHOR_ROLE", "path": ""}
    }

    changed = json.loads(compile_corpus.dumps(first))
    changed["queries"][SAMPLE[0]]["results"]["bigquery"]["warnings"].append("NEW_WARNING")
    changed["queries"][SAMPLE[2]]["results"]["duckdb"] = {"refusal": {"code": "X", "path": ""}}
    golden, after = tmp_path / "golden.json", tmp_path / "after.json"
    golden.write_text(compile_corpus.dumps(first), encoding="utf-8")
    after.write_text(compile_corpus.dumps(changed), encoding="utf-8")

    assert [row[:3] for row in compile_corpus.differences(first, changed)] == [
        (SAMPLE[0], "bigquery", "warnings"),
        (SAMPLE[2], "duckdb", "outcome"),
    ]
    assert compile_corpus.compare(str(golden), str(golden)) == 0
    assert compile_corpus.compare(str(golden), str(after), brief=True) == 1
    assert f"CHANGED {SAMPLE[0]} [bigquery] warnings" in capsys.readouterr().out
