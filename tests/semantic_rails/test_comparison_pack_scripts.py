"""Claims and labels the comparison pack publishes are generated from its own checks."""

from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import re
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2] / "comparisons" / "semantic_layers" / "shared" / "scripts"
)


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load("validate_output_consistency")
generator = _load("generate_comparison_contracts")
rubric = _load("apply_rubric")

LAYERS = generator.LAYER_ORDER
SHARED = [f"q0{n}_shared" for n in range(1, 8)]
TARGETED = [f"q{n:02d}_targeted" for n in range(8, 17)]
BYPASS = {"q11_targeted": ["lifetime_order_count"], "q12_targeted": ["lifetime_spend_cents"]}


def _question(qid: str, status: str = "matched") -> dict[str, Any]:
    return {
        "question_id": qid,
        "slice": "shared" if qid in SHARED else "semantic_rails_targeted",
        "comparison_status": status,
        "comparable_layers": list(LAYERS),
        "current_layers": list(LAYERS),
        "layer_statuses": dict.fromkeys(LAYERS, "native"),
    }


def _report(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in ("matched", "mismatched", "not_comparable")}
    by_slice: dict[str, dict[str, Any]] = {}
    for item in items:
        counts[item["comparison_status"]] += 1
        entry = by_slice.setdefault(
            item["slice"],
            {"questions": 0, **dict.fromkeys(counts, 0), "mismatched_questions": []},
        )
        entry["questions"] += 1
        entry[item["comparison_status"]] += 1
        if item["comparison_status"] == "mismatched":
            entry["mismatched_questions"].append(item["question_id"])
    return {
        "reference_layer": "answer_key",
        "summary": counts,
        "summary_by_slice": by_slice,
        "questions": items,
        "stale_layers": {},
    }


def _claims(
    items: list[dict[str, Any]],
    labels: dict[str, str] | None = None,
    stale: dict[str, Any] | None = None,
    layer_fields: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    questions = [
        {
            "id": item["question_id"],
            "title": item["question_id"],
            "bypass_columns": BYPASS.get(item["question_id"], []),
        }
        for item in items
    ]
    slice_ids: dict[str, list[str]] = {}
    for item in items:
        slice_ids.setdefault(item["slice"], []).append(item["question_id"])
    layers = [
        {
            "id": layer,
            "label": generator.LAYER_META[layer]["label"],
            "captured": generator.LAYER_META[layer]["captured"],
            "dataset": "stale" if layer in (stale or {}) else "current",
            **(layer_fields or {}).get(layer, {}),
            "questions": [
                {
                    "question_id": item["question_id"],
                    "support_status": (labels or {}).get(layer, "precomputed"),
                }
                for item in items
            ],
        }
        for layer in LAYERS
    ]
    report = _report(items)
    report["stale_layers"] = stale or {}
    return generator.claim_findings(report, questions, slice_ids, layers)


def test_agreement_groups_agree_pairwise_despite_non_transitive_tolerance() -> None:
    def rows(value: str) -> list[dict[str, Any]]:
        return [{"orders": Decimal(value)}]

    groups = validator._agreement_groups(
        {
            "semantic_rails": rows("10"),
            "metricflow": rows("1"),
            "cube": rows("1.0000009"),
            "malloy": rows("0.9999991"),
        }
    )
    by_layer = {
        "semantic_rails": "10",
        "metricflow": "1",
        "cube": "1.0000009",
        "malloy": "0.9999991",
    }
    for group in groups:
        for left, right in itertools.combinations(group, 2):
            assert validator._rows_equal(rows(by_layer[left]), rows(by_layer[right]))[0], group
    assert sorted(map(sorted, groups)) == [["cube", "metricflow"], ["malloy"], ["semantic_rails"]]


def test_headline_reports_mismatches_and_who_disagrees() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    for item in (items[6], items[15]):
        item["comparison_status"] = "mismatched"
        item["agreement_groups"] = [["semantic_rails"], [layer for layer in LAYERS[1:]]]
    claims = _claims(items)
    assert claims[0].startswith(
        "On 14 of 16 questions, all 6 layers return the independent answer key's normalized "
        "outputs, with numbers matching within 1e-6."
    )
    assert "On 2, at least one layer differs: q07" in claims[0]
    assert claims[1] == (
        "On q07 and q16: MetricFlow, Cube, Malloy, Snowflake Semantic Views and KtX agree "
        "with each other; Semantic Rails differs."
    )
    slices = next(claim for claim in claims if claim.startswith("Shared questions"))
    assert "Shared questions (q01-q07): 6 of 7 match." in slices
    assert "Semantic-Rails-targeted questions (q08-q16): 8 of 9 match." in slices


def test_claims_refuse_a_report_not_checked_against_the_answer_key() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    report = _report(items)
    report["reference_layer"] = "semantic_rails"
    with pytest.raises(SystemExit, match="against the answer key"):
        generator.claim_findings(report, [], {}, [])


def test_headline_when_everything_matches() -> None:
    claims = _claims([_question(qid) for qid in SHARED + TARGETED])
    assert claims[0] == (
        "On all 16 questions, all 6 layers return the independent answer key's normalized "
        "outputs, with numbers matching within 1e-6."
    )
    assert not any(claim.startswith("On q") for claim in claims)


def test_unsupported_layer_is_named_and_empty_slices_are_skipped() -> None:
    items = [_question(qid) for qid in SHARED]
    unsupported = copy.deepcopy(items[2])
    unsupported["layer_statuses"]["malloy"] = "unsupported"
    unsupported["comparable_layers"].remove("malloy")
    unsupported["current_layers"].remove("malloy")
    items[2] = unsupported
    claims = _claims(items)
    assert "the layers that executed them on the current dataset" in claims[0]
    assert "Malloy did not execute q03." in claims
    assert not any("were chosen to exercise features" in claim for claim in claims)


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (["q01_a", "q02_b", "q03_c"], "q01-q03"),
        (["q03_c", "q01_a", "q04_d", "q05_e"], "q01, q03-q05"),
        (["q07_a"], "q07"),
    ],
)
def test_id_range_compacts_runs(ids: list[str], expected: str) -> None:
    assert generator.id_range(ids) == expected


def test_status_totals_keep_unknown_labels() -> None:
    totals = generator.status_totals(["native", "native", "other"])
    assert totals["native"] == 2 and totals["other"] == 1 and totals["unsupported"] == 0


def test_bypass_sentence_names_every_layers_label() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    claims = _claims(items)
    assert (
        "q11 asks for a rollup that the shared data already holds in `lifetime_order_count`; it "
        "is labeled precomputed for Semantic Rails, MetricFlow, Cube, Malloy, Snowflake Semantic "
        "Views and KtX."
    ) in claims
    assert not any(claim.startswith(("q01 ", "q08 ")) for claim in claims)
    mixed = next(
        claim for claim in _claims(items, {"semantic_rails": "native"}) if claim.startswith("q12 ")
    )
    assert mixed.endswith(
        "labeled native for Semantic Rails; precomputed for MetricFlow, Cube, Malloy, Snowflake "
        "Semantic Views and KtX."
    )


def test_unknown_status_gets_a_note_instead_of_crashing() -> None:
    assert generator.default_note("partial") == "Labeled partial."


def test_size_blocks_carry_no_question_or_label_counts() -> None:
    for layer in LAYERS:
        for block in generator.layer_scale(layer).values():
            assert set(block) == {"models", "files", "loc", "relationships"}


def test_stale_capture_is_reported_apart_from_the_current_count() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    for item in items:
        item["current_layers"] = [layer for layer in LAYERS if layer != "snowflake_semantic_views"]
    differs = ["q07_shared", "q16_targeted"]
    stale = {
        "snowflake_semantic_views": {
            "captured": "2026-04-06T23:05:57-04:00",
            "matched": [
                item["question_id"] for item in items if item["question_id"] not in differs
            ],
            "mismatched": differs,
        }
    }
    claims = _claims(
        items, stale=stale, layer_fields={"snowflake_semantic_views": {"captured": "2026-04-07"}}
    )
    assert claims[0] == (
        "On all 16 questions, the 5 layers checked on the current dataset (Semantic Rails, "
        "MetricFlow, Cube, Malloy and KtX) return the independent answer key's normalized "
        "outputs, with numbers matching within 1e-6."
    )
    assert claims[1] == (
        "Snowflake Semantic Views was captured on 2026-04-07 on an earlier dataset and has not "
        "been re-run, so it is left out of that count. Its capture matches the answer key on 14 "
        "questions and differs on: q07, q16."
    )


def _load_snowflake_runner() -> ModuleType:
    path = SCRIPTS.parents[1] / "snowflake_semantic_views" / "scripts" / "run_questions.py"
    spec = importlib.util.spec_from_file_location("snowflake_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snowflake_run_records_only_one_agreed_fingerprint() -> None:
    loaded = _load_snowflake_runner().loaded_fingerprint
    assert loaded([{"FINGERPRINT": "abc"}]) == "abc"
    assert loaded([{"FINGERPRINT": "abc"}, {"FINGERPRINT": "abc"}]) == "abc"
    assert loaded([{"FINGERPRINT": "abc"}, {"FINGERPRINT": "def"}]) is None
    assert loaded([]) is None
    assert loaded([{"FINGERPRINT": None}]) is None


def _run_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snowflake: dict) -> dict:
    """Run the output check on a three-layer, one-question pack whose Snowflake run is given."""
    layers = ["semantic_rails", "ktx", "snowflake_semantic_views"]
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "questions.yml").write_text(
        "questions:\n  - id: q01_x\n    title: X\n    scope_level: required\n", encoding="utf-8"
    )
    rows = {layer: [{"order_month": "2016-09-01", "orders": 5}] for layer in layers[:2]}
    rows["snowflake_semantic_views"] = [
        {"order_month": "2016-09-01", "orders": snowflake["orders"]}
    ]
    for layer in layers:
        layer_dir = tmp_path / "results" / layer
        layer_dir.mkdir(parents=True)
        payload = {"rows": rows[layer]} if layer == "semantic_rails" else rows[layer]
        (layer_dir / "q01_x.json").write_text(json.dumps(payload), encoding="utf-8")
        fingerprint = snowflake["fingerprint"] if layer == "snowflake_semantic_views" else "fp-now"
        summary = {
            "dataset_fingerprint": fingerprint,
            "generated_at": "2026-09-23T00:00:00+00:00",
            "questions": [
                {
                    "question_id": "q01_x",
                    "status": snowflake.get("status", "executed")
                    if layer == "snowflake_semantic_views"
                    else "executed",
                    "result_path": f"results/{layer}/q01_x.json",
                }
            ],
        }
        (layer_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    key_dir = tmp_path / "results" / "oracle"
    key_dir.mkdir(parents=True)
    (key_dir / "q01_x.json").write_text(
        json.dumps([{"month": "2016-09-01", "orders": 5}]), encoding="utf-8"
    )
    oracle_dir = tmp_path / "oracle"
    oracle_dir.mkdir()
    (oracle_dir / "q01_x.sql").write_text("SELECT DATE '2016-09-01' AS month, 5 AS orders", "utf-8")
    key_summary = {
        "dataset_fingerprint": "fp-now",
        "answer_key_fingerprint": validator.answer_key_fingerprint(
            oracle_dir, tmp_path / "questions.yml"
        ),
        "questions": [{"question_id": "q01_x", "result_path": "results/oracle/q01_x.json"}],
    }
    (key_dir / "summary.json").write_text(json.dumps(key_summary), encoding="utf-8")
    maps = {layer: {"q01_x": {"month": "order_month", "orders": "orders"}} for layer in layers}
    (tmp_path / "column_maps.yml").write_text(json.dumps(maps), encoding="utf-8")
    for name, value in {
        "REPO_ROOT": tmp_path,
        "RESULTS_ROOT": tmp_path / "results",
        "QUESTIONS_PATH": tmp_path / "questions.yml",
        "ORACLE_DIR": tmp_path / "oracle",
        "OUTPUT_DIR": tmp_path / "validation",
        "COLUMN_MAPS_PATH": tmp_path / "column_maps.yml",
        "RUNNABLE_LAYERS": layers,
        "RESULT_DIRS": {layer: layer for layer in layers} | {validator.ANSWER_KEY: "oracle"},
        "QUESTION_FIELDS": {"q01_x": ["month", "orders"]},
        "dataset_fingerprint": lambda: "fp-now",
    }.items():
        monkeypatch.setattr(validator, name, value)
    validator.main()
    report = json.loads((tmp_path / "validation" / "output_consistency.json").read_text())
    report["markdown"] = (tmp_path / "validation" / "output_consistency.md").read_text()
    return report


def test_fresh_snowflake_run_rejoins_the_comparison(tmp_path, monkeypatch) -> None:
    matching = _run_validator(tmp_path / "a", monkeypatch, {"fingerprint": "fp-now", "orders": 5})
    assert matching["stale_layers"] == {}
    assert matching["summary"]["matched"] == 1
    mismatching = _run_validator(
        tmp_path / "b", monkeypatch, {"fingerprint": "fp-now", "orders": 6}
    )
    assert mismatching["summary"]["mismatched"] == 1
    assert mismatching["questions"][0]["mismatches"][0]["layer"] == "snowflake_semantic_views"


def test_the_report_records_only_whether_each_layer_ran(tmp_path, monkeypatch) -> None:
    status = {"fingerprint": "fp-now", "orders": 5, "status": "workaround"}
    report = _run_validator(tmp_path, monkeypatch, status)
    assert report["questions"][0]["layer_statuses"]["snowflake_semantic_views"] == "executed"


def test_an_answer_key_from_older_data_is_refused(tmp_path, monkeypatch) -> None:
    assert _run_validator(tmp_path, monkeypatch, {"fingerprint": "fp-now", "orders": 5})
    summary_path = tmp_path / "results" / "oracle" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["dataset_fingerprint"] = "fp-old"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(SystemExit, match="predates the current dataset"):
        validator.main()


def test_stale_snowflake_capture_is_set_aside_and_reported(tmp_path, monkeypatch) -> None:
    report = _run_validator(tmp_path, monkeypatch, {"fingerprint": None, "orders": 6})
    assert report["summary"]["matched"] == 1
    assert report["stale_layers"]["snowflake_semantic_views"]["mismatched"] == ["q01_x"]
    assert "- Layers compared: `semantic_rails, ktx`" in report["markdown"]
    assert (
        "- Stale capture, not counted: `snowflake_semantic_views` mismatched" in report["markdown"]
    )


def _run_snowflake_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sql: str) -> dict:
    """Run the Snowflake runner offline; a question whose SQL says `broken` fails."""
    runner = _load_snowflake_runner()
    examples = tmp_path / "query_examples.sql"
    examples.write_text(f"-- q01_orders_by_month\n{sql}\n", encoding="utf-8")

    def snow_sql(*, query=None, file_path=None):
        if query and "broken" in query:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="SQL compilation error")
        rows = [{"FINGERPRINT": "fp-now"}] if query == runner.DATASET_SQL else []
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SHARED_RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(runner, "QUERY_EXAMPLES_PATH", examples)
    monkeypatch.setattr(runner, "run_snow_sql", snow_sql)
    monkeypatch.setattr(
        runner, "run_command", lambda args, **_: subprocess.CompletedProcess(args, 0, "", "")
    )
    runner.main()
    return json.loads((tmp_path / "results" / "summary.json").read_text())


def test_snowflake_runner_records_the_loaded_fingerprint(tmp_path, monkeypatch) -> None:
    summary = _run_snowflake_runner(tmp_path, monkeypatch, "SELECT 1;")
    assert summary["dataset_fingerprint"] == "fp-now"
    assert summary["questions"][0]["status"] == "executed"


def test_snowflake_runner_lists_a_failed_question_as_unsupported(tmp_path, monkeypatch) -> None:
    summary = _run_snowflake_runner(tmp_path, monkeypatch, "SELECT broken;")
    [entry] = summary["questions"]
    assert entry["question_id"] == "q01_orders_by_month"
    assert entry["status"] == "unsupported"
    assert entry["reason"] == "SQL compilation error"


def test_a_missing_mapped_column_fails_instead_of_being_guessed() -> None:
    rows = [{"ordered_month": "2016-09-01", "orders": 5}]
    columns = {"month": "order_month", "orders": "orders"}
    with pytest.raises(SystemExit, match="'order_month' for 'month' is missing"):
        validator._normalize_rows("q01_orders_by_month", rows, columns)


def test_column_maps_cover_every_field_of_every_layer() -> None:
    maps = validator._load_column_maps()
    for layer in validator.RUNNABLE_LAYERS:
        for question_id, fields in validator.QUESTION_FIELDS.items():
            assert sorted(maps[layer][question_id]) == sorted(fields)


def test_every_question_has_an_answer_key_query() -> None:
    oracle = SCRIPTS.parent / "oracle"
    assert sorted(path.stem for path in oracle.glob("*.sql")) == sorted(validator.QUESTION_FIELDS)


def test_a_changed_answer_key_query_invalidates_its_cached_answers(tmp_path, monkeypatch) -> None:
    assert _run_validator(tmp_path, monkeypatch, {"fingerprint": "fp-now", "orders": 5})
    (tmp_path / "oracle" / "q01_x.sql").write_text("SELECT 0 AS orders", encoding="utf-8")
    with pytest.raises(SystemExit, match="queries or the questions changed"):
        validator.main()


def test_answer_key_queries_reproduce_the_committed_answers(tmp_path, monkeypatch) -> None:
    import duckdb

    bootstrap = _load("bootstrap_shared_duckdb")
    oracle = _load("run_oracle")
    monkeypatch.setattr(bootstrap, "DB_PATH", tmp_path / "shared.duckdb")
    bootstrap.main()
    committed = json.loads((oracle.RESULTS_DIR / "summary.json").read_text(encoding="utf-8"))
    assert committed["answer_key_fingerprint"] == oracle.answer_key_fingerprint(
        oracle.ORACLE_DIR, oracle.QUESTIONS_PATH
    )
    con = duckdb.connect(str(tmp_path / "shared.duckdb"), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET threads = 1")
        for entry in committed["questions"]:
            qid = entry["question_id"]
            fresh = json.loads(json.dumps(oracle.answer(con, qid), default=str))
            saved = json.loads((oracle.REPO_ROOT / entry["result_path"]).read_text("utf-8"))
            equal, detail = validator._rows_equal(
                validator._normalize_rows(qid, saved), validator._normalize_rows(qid, fresh)
            )
            assert equal, (qid, detail)
    finally:
        con.close()


def test_rubric_rules_apply_in_order() -> None:
    bypass = ["lifetime_order_count"]
    reads = ["select lifetime_order_count from comparison_customers"]
    assert rubric.decide(False, ["cube x"], reads, bypass)["label"] == "unsupported"
    assert rubric.decide(True, ["cube x"], reads, bypass) == {
        "label": "precomputed",
        "evidence": ["reads lifetime_order_count"],
    }
    assert rubric.decide(True, ["cube x"], ["select 1"], bypass) == {
        "label": "workaround",
        "evidence": ["cube x"],
    }
    assert rubric.decide(True, [], ["select 1"], bypass) == {"label": "native", "evidence": []}


def test_a_bypass_column_matches_only_as_a_whole_name() -> None:
    assert rubric.reads_column("where c.LIFETIME_ORDER_COUNT > 1", "lifetime_order_count")
    assert not rubric.reads_column("select lifetime_order_count_band", "lifetime_order_count")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("{{ config(materialized='view') }}\n\nselect *\nfrom comparison_orders\n", True),
        ("-- passthrough\nSELECT * FROM comparison_orders;", True),
        ("select * from comparison_orders where status = 'completed'", False),
        ("select * from jaffle_order", False),
    ],
)
def test_only_a_bare_view_passthrough_is_not_hand_written(sql: str, expected: bool) -> None:
    assert rubric.is_passthrough(sql) is expected


def test_semantic_rails_derived_relations_count_as_hand_written(tmp_path, monkeypatch) -> None:
    view = {"id": "orders", "relation": "comparison_orders"}
    cases = {
        "view": (view, "", {}),
        "pipeline": ({"id": "orders", "relation": {"steps": []}}, "", {}),
        "raw_table": ({"id": "orders", "relation": "jaffle_order"}, "", {}),
        "relation_ref": (view | {"relation_ref": "rollup"}, "", {}),
        "variants": (view | {"variants": {"daily": {}}}, "", {}),
        "relations": (view, "relations:\n  derived: {}\n", {}),
        "aggregate_relations": (view, "aggregate_relations:\n  - id: rollup\n", {}),
        "relations_dir": (view, "", {"relations/derived.yml": "relation:\n  id: derived\n"}),
        "inline_model": (None, "models:\n  orders:\n    relation: jaffle_order\n", {}),
    }
    (tmp_path / "sql.sql").write_text("select 1", encoding="utf-8")
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    found = {}
    for name, (model, extra, files) in cases.items():
        package = tmp_path / name
        package.mkdir()
        (package / "package.yml").write_text(f"package:\n  id: package\n{extra}", "utf-8")
        if model is not None:
            (package / "models").mkdir()
            (package / "models" / "orders.yml").write_text(json.dumps({"model": model}), "utf-8")
        for relative, text in files.items():
            (package / relative).parent.mkdir(parents=True, exist_ok=True)
            (package / relative).write_text(text, encoding="utf-8")
        monkeypatch.setattr(rubric, "SR_PACKAGE", package)
        found[name] = rubric.semantic_rails({"question_id": "q_x", "sql_path": "sql.sql"})[0]
    derived = ["derived model orders"]
    assert found == {
        "view": [],
        "pipeline": derived,
        "raw_table": derived,
        "relation_ref": derived,
        "variants": derived,
        "relations": ["relation pipelines"],
        "aggregate_relations": ["relation pipelines"],
        "relations_dir": ["relation pipelines"],
        "inline_model": derived,
    }


def test_a_detector_that_finds_nothing_fails_closed(tmp_path, monkeypatch) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "_models.yml").write_text("models: []\n", encoding="utf-8")
    (tmp_path / "sql.txt").write_text("SQL (remove --explain to see data):\nselect 1", "utf-8")
    monkeypatch.setattr(rubric, "MF_MODELS", models)
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match="found no relations"):
        rubric.metricflow({"question_id": "q_x", "sql_path": "sql.txt"})
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.yml").write_text("package:\n  id: package\n", encoding="utf-8")
    monkeypatch.setattr(rubric, "SR_PACKAGE", package)
    with pytest.raises(SystemExit, match="found no models"):
        rubric.semantic_rails({"question_id": "q_x"})
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    (tmp_path / "query.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="found no cubes"):
        rubric.cube({"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.txt"})
    with pytest.raises(SystemExit, match="found no sources"):
        rubric.ktx({"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.txt"})
    (tmp_path / "malloy" / "models").mkdir(parents=True)
    (tmp_path / "malloy" / "models" / "jaffle.malloy").write_text(
        "query: q_y is orders -> { aggregate: n is count() }\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="found no named query"):
        rubric.malloy({"question_id": "q_x", "sql_path": "sql.txt"})


@pytest.mark.parametrize("sql_path", [None, "", "missing.sql", "empty.sql"])
def test_an_executed_answer_without_its_sql_fails_closed(tmp_path, monkeypatch, sql_path) -> None:
    # Without its SQL, Semantic Rails q11 would lose the bypass column that labels it precomputed.
    (tmp_path / "empty.sql").write_text("\n", encoding="utf-8")
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    entry = {"question_id": "q11_repeat_customer_orders_by_store_by_month"}
    if sql_path is not None:
        entry["sql_path"] = sql_path
    with pytest.raises(SystemExit, match="q11_repeat.*found no executed SQL"):
        rubric.semantic_rails(entry)


def test_cube_counts_a_sql_cube_used_only_by_a_filter(tmp_path, monkeypatch) -> None:
    cubes = tmp_path / "cube" / "model" / "cubes"
    cubes.mkdir(parents=True)
    (cubes / "orders.yml").write_text(
        "cubes:\n  - name: orders\n    sql_table: comparison_orders\n", encoding="utf-8"
    )
    (cubes / "segments.yml").write_text(
        "cubes:\n  - name: segments\n"
        "    sql: select customer_id from comparison_orders group by 1\n",
        encoding="utf-8",
    )
    query = {
        "measures": ["orders.count"],
        "filters": [{"member": "segments.customer_id", "operator": "set"}],
    }
    (tmp_path / "query.json").write_text(json.dumps(query), encoding="utf-8")
    (tmp_path / "sql.json").write_text(json.dumps({"sql": {"sql": ["select 1", []]}}), "utf-8")
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    entry = {"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.json"}
    assert rubric.cube(entry)[0] == ["cube segments"]


def test_metricflow_counts_helper_models_but_not_passthroughs_or_the_time_spine(
    tmp_path, monkeypatch
) -> None:
    models = tmp_path / "models"
    (models / "staging").mkdir(parents=True)
    (models / "_models.yml").write_text(
        "models:\n  - name: all_days\n    time_spine: {}\n", encoding="utf-8"
    )
    (models / "all_days.sql").write_text("select generate_series(1, 3)", encoding="utf-8")
    (models / "staging" / "orders.sql").write_text("select * from comparison_orders", "utf-8")
    (models / "staging" / "helper.sql").write_text(
        "select customer_id from comparison_orders group by 1", encoding="utf-8"
    )
    (tmp_path / "sql.txt").write_text(
        'SQL (remove --explain to see data):\nselect * from "db"."main"."orders", '
        '"db"."main"."helper", "db"."main"."all_days"',
        encoding="utf-8",
    )
    monkeypatch.setattr(rubric, "MF_MODELS", models)
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    helpers, texts = rubric.metricflow({"sql_path": "sql.txt"})
    assert helpers == ["dbt model helper"]
    assert len(texts) == 2


def test_malloy_counts_the_sql_blocks_its_executed_sql_reads(tmp_path, monkeypatch) -> None:
    models = tmp_path / "malloy" / "models"
    models.mkdir(parents=True)
    (models / "jaffle.malloy").write_text(
        'source:  segments is jaffle.sql("""select customer_id, segment\n'
        '  from comparison_customer_history""") extend {}\n'
        "source: history is segments extend {}\n"
        'source: unused is jaffle.sql("select store_id from comparison_stores")\n'
        # Only part of the SQL of `segments`, which the query reads; this block isn't read.
        'source: prefix is jaffle.sql("select customer_id, segment")\n'
        'source: passthrough is jaffle.sql("select * from comparison_customers")\n'
        "source: orders is jaffle.table('comparison_orders') extend {\n"
        "  join_one: history, passthrough on true\n"
        '  join_one: inline is jaffle.sql("""select order_id from comparison_order_items""")\n'
        "  join_one: unused on true\n}\n"
        "query:  q08_x is orders -> { aggregate: n is count() }\n",
        encoding="utf-8",
    )
    # Malloy compiles each SQL block the query reads into its SQL; `unused` and `prefix` aren't.
    (tmp_path / "q08.sql").write_text(
        "SELECT count(1) FROM comparison_orders AS base\n"
        "LEFT JOIN (\n  select customer_id, segment\n  from comparison_customer_history\n) AS h\n"
        "LEFT JOIN (select * from comparison_customers) AS p\n"
        "LEFT JOIN (\nselect order_id from comparison_order_items\n) AS inline",
        encoding="utf-8",
    )
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    helpers, _ = rubric.malloy({"question_id": "q08_x", "sql_path": "q08.sql"})
    assert helpers == ["SQL source inline", "SQL source segments"]


def test_a_question_without_an_executed_result_is_unsupported(tmp_path, monkeypatch) -> None:
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "questions.yml").write_text(
        "questions:\n  - id: q01_a\n  - id: q02_b\n  - id: q03_c\n", encoding="utf-8"
    )
    results = tmp_path / "results" / "fake"
    results.mkdir(parents=True)
    executed = [{"question_id": qid, "status": "executed"} for qid in ("q01_a", "q03_c")]
    (results / "summary.json").write_text(json.dumps({"questions": executed}), "utf-8")
    failed = {"q03_c": {"status": "unsupported", "reason": "error"}}
    (results / "unsupported.json").write_text(json.dumps(failed), encoding="utf-8")
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(rubric, "DETECTORS", {"fake": ("fake", lambda entry: ([], ["select 1"]))})
    labels = rubric.build_labels()["labels"]["fake"]
    assert {qid: label["label"] for qid, label in labels.items()} == {
        "q01_a": "native",
        "q02_b": "unsupported",
        "q03_c": "unsupported",
    }


def test_committed_labels_are_what_the_rubric_derives() -> None:
    committed = json.loads(rubric.OUTPUT_PATH.read_text(encoding="utf-8"))
    derived = rubric.build_labels()
    assert derived["rules"] == committed["rules"]
    assert derived["labels"] == committed["labels"]


def test_published_labels_come_from_the_rubric() -> None:
    labels = json.loads(rubric.OUTPUT_PATH.read_text(encoding="utf-8"))["labels"]
    matrix = json.loads((SCRIPTS.parent / "capability_matrix.json").read_text(encoding="utf-8"))
    for row in matrix["rows"]:
        qid = row["question_id"]
        assert row["statuses"] == {layer: labels[layer][qid]["label"] for layer in LAYERS}


def test_contracts_take_rubric_labels_and_every_layer_on_current_data_counts() -> None:
    _, matrix = generator.build_contracts()
    labels = json.loads(rubric.OUTPUT_PATH.read_text(encoding="utf-8"))["labels"]
    for row in matrix["rows"]:
        qid = row["question_id"]
        assert row["statuses"] == {layer: labels[layer][qid]["label"] for layer in LAYERS}
    assert "(Semantic Rails, MetricFlow, Cube, Malloy and KtX)" in matrix["claims"][0]
    stale = [layer["id"] for layer in matrix["layers"] if layer["dataset"] == "stale"]
    assert stale == ["snowflake_semantic_views"]


def test_q11_and_q12_are_precomputed_exactly_where_a_layer_reads_the_rollup_column() -> None:
    labels = json.loads(rubric.OUTPUT_PATH.read_text(encoding="utf-8"))["labels"]
    reading = {"semantic_rails", "snowflake_semantic_views", "ktx"}
    for layer in LAYERS:
        for qid, column in (
            ("q11_repeat_customer_orders_by_store_by_month", "lifetime_order_count"),
            ("q12_orders_by_month_with_lifetime_spend_500_filter", "lifetime_spend_cents"),
        ):
            expected = (
                {"label": "precomputed", "evidence": [f"reads {column}"]}
                if layer in reading
                else {"label": "native", "evidence": []}
            )
            assert labels[layer][qid] == expected, (layer, qid)


# Hand-written text naming which questions carry which labels: a layer's headline finding, or
# its README. Each range must name exactly the questions, in its slices, with those labels.
LABEL_STATEMENTS = [
    ("finding", "metricflow", "MetricFlow answers q08-q16 with", {"native"}),
    ("finding", "cube", "Cube answers q08-q16 with", {"native"}),
    ("finding", "malloy", "Malloy answers q08-q16 with", {"native"}),
    ("finding", "snowflake_semantic_views", "Views answers q01-q07 through", {"native"}),
    ("finding", "snowflake_semantic_views", "and q08-q16 as SQL", {"workaround", "precomputed"}),
    ("finding", "ktx", "KtX answers q01-q07 through", {"native"}),
    ("finding", "ktx", "and q08-q16 through SQL-backed", {"workaround", "precomputed"}),
    ("readme", "metricflow", "labels all 16 answers (q01-q16) `native`", {"native"}),
    ("readme", "malloy", "All 16 questions (q01-q16) run through Malloy sources", {"native"}),
    ("readme", "ktx", "`q01`-`q07` use ordinary KtX sources", {"native"}),
    ("readme", "ktx", "`q08`-`q10` and `q13`-`q16` execute through KtX", {"workaround"}),
    ("readme", "ktx", "`q11` and `q12` filter on the precomputed", {"precomputed"}),
]


def _numbers(text: str) -> set[int]:
    ranges = re.findall(r"q(\d\d)(?:-q(\d\d))?", text.replace("`", ""))
    return {n for start, end in ranges for n in range(int(start), int(end or start) + 1)}


def _labels_by_number() -> tuple[dict[str, dict[int, str]], dict[int, str]]:
    labels = json.loads(rubric.OUTPUT_PATH.read_text(encoding="utf-8"))["labels"]
    report = SCRIPTS.parent / "results" / "validation" / "output_consistency.json"
    items = json.loads(report.read_text(encoding="utf-8"))["questions"]
    by_layer = {
        layer: {int(qid[1:3]): label["label"] for qid, label in questions.items()}
        for layer, questions in labels.items()
    }
    return by_layer, {int(item["question_id"][1:3]): item["slice"] for item in items}


def _assert_counts(cell: str, labels: dict[int, str]) -> None:
    found = re.findall(r"(\d+) (\w+)(?: \(([^)]*)\))?", cell.replace("`", ""))
    assert {label: int(count) for count, label, _ in found} == Counter(labels.values()), cell
    for _, label, named in found:
        if named:
            assert _numbers(named) == {n for n, got in labels.items() if got == label}, cell


def test_hand_written_label_statements_match_the_rubric() -> None:
    labels, slices = _labels_by_number()
    checked = set()
    for source, layer, phrase, allowed in LABEL_STATEMENTS:
        if source == "finding":
            name = generator.LAYER_META[layer]["label"]
            text = next(f for f in generator.LAYER_FINDINGS if f.startswith(name))
            checked.add(text)
        else:
            text = (SCRIPTS.parents[1] / layer / "README.md").read_text(encoding="utf-8")
        assert phrase in " ".join(text.split()), phrase
        named = _numbers(phrase)
        scope = {n for n in labels[layer] if slices[n] in {slices[m] for m in named}}
        assert named == {n for n in scope if labels[layer][n] in allowed}, (layer, phrase)
    assert checked == {f for f in generator.LAYER_FINDINGS if re.search(r"\bq\d\d", f)}


def test_hand_written_label_tables_match_the_rubric() -> None:
    labels, slices = _labels_by_number()
    layer_of = {generator.LAYER_META[layer]["label"]: layer for layer in LAYERS}
    readme = (SCRIPTS.parents[1] / "README.md").read_text(encoding="utf-8")
    for slice_name, heading in [
        ("shared", "## Shared Questions"),
        ("semantic_rails_targeted", "## Semantic-Rails-Targeted Questions"),
    ]:
        section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
        rows = re.findall(r"^\| ([^|]+?) \| ([^|]+?) \|", section, flags=re.MULTILINE)
        rows = [(layer_of[name], cell) for name, cell in rows if name in layer_of]
        assert sorted(layer for layer, _ in rows) == sorted(LAYERS)
        for layer, cell in rows:
            _assert_counts(
                cell, {n: got for n, got in labels[layer].items() if slices[n] == slice_name}
            )
    snowflake = (SCRIPTS.parents[1] / "snowflake_semantic_views" / "README.md").read_text("utf-8")
    line = next(line for line in snowflake.splitlines() if line.startswith("- Support labels"))
    _assert_counts(line.split(":", 1)[1], labels["snowflake_semantic_views"])


def _load_semantic_rails_runner() -> ModuleType:
    path = SCRIPTS.parents[1] / "semantic_rails" / "scripts" / "run_questions.py"
    spec = importlib.util.spec_from_file_location("semantic_rails_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_semantic_rails_provenance_covers_queries_runner_and_questions() -> None:
    module = _load_semantic_rails_runner()
    assert set(module._provenance()) == {
        "semantic_rails_commit",
        "semantic_rails_tree",
        "engine_release",
        "package_tree",
        "layer_tree",
        "questions_blob",
        "inputs_modified",
    }


def test_an_unreleased_engine_is_not_labeled_as_the_release() -> None:
    release = {"semantic_rails_version": "0.2.1", "engine_release": "v0.2.1"}
    assert generator.recorded_version("semantic_rails", release) == "0.2.1"
    unreleased = {
        "semantic_rails_version": "0.2.1",
        "engine_release": None,
        "semantic_rails_tree": "2e1b2171d0c4b2926e42866c6337656f48b62ace",
    }
    assert generator.recorded_version("semantic_rails", unreleased) == (
        "0.2.1, not a release (engine tree 2e1b217)"
    )
    no_git = {"semantic_rails_version": "0.2.1", "engine_release": None}
    assert generator.recorded_version("semantic_rails", no_git) == (
        "0.2.1, not a release (engine source not recorded)"
    )


def test_engine_release_needs_the_release_tree_and_a_clean_engine(monkeypatch) -> None:
    runner = _load_semantic_rails_runner()
    monkeypatch.setattr(runner, "version", lambda name: "0.2.1")

    def git_answers(answers: dict[tuple[str, ...], str]):
        def run(cmd, **kwargs):
            out = answers.get(tuple(cmd[3:]), "")
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        return run

    release = {
        ("rev-parse", "HEAD:semantic_rails"): "tree-a",
        ("rev-parse", "v0.2.1:semantic_rails"): "tree-a",
    }
    monkeypatch.setattr(runner.subprocess, "run", git_answers(release))
    assert runner._provenance()["engine_release"] == "v0.2.1"
    later = release | {("rev-parse", "v0.2.1:semantic_rails"): "tree-b"}
    monkeypatch.setattr(runner.subprocess, "run", git_answers(later))
    assert runner._provenance()["engine_release"] is None
    edited = release | {("status", "--porcelain", "--", "semantic_rails"): " M semantic_rails/x.py"}
    monkeypatch.setattr(runner.subprocess, "run", git_answers(edited))
    assert runner._provenance()["engine_release"] is None
