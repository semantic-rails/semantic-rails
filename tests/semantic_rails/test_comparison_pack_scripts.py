"""Claims and labels the comparison pack publishes are generated from its own checks."""

from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import subprocess
import sys
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
            "label": generator.LAYER_META[layer]["label"],
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
    claims = _claims(items, stale=stale)
    assert claims[0] == (
        "On all 16 questions, the 5 layers run on the current dataset return the independent "
        "answer key's normalized outputs, with numbers matching within 1e-6."
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
                    "status": "executed",
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
    cases = {
        "view": ("comparison_orders", ""),
        "pipeline": ({"steps": []}, ""),
        "raw_table": ("jaffle_order", ""),
        "relations": ("comparison_orders", "relations:\n  derived: {}\n"),
    }
    found = {}
    for name, (relation, extra) in cases.items():
        package = tmp_path / name
        (package / "models").mkdir(parents=True)
        (package / "package.yml").write_text(f"package:\n  id: package\n{extra}", "utf-8")
        model = {"model": {"id": "orders", "relation": relation}}
        (package / "models" / "orders.yml").write_text(json.dumps(model), encoding="utf-8")
        monkeypatch.setattr(rubric, "SR_PACKAGE", package)
        found[name] = rubric.semantic_rails({})[0]
    assert found == {
        "view": [],
        "pipeline": ["derived model orders"],
        "raw_table": ["derived model orders"],
        "relations": ["relation pipelines"],
    }


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


def test_malloy_counts_sql_sources_reached_through_joins(tmp_path, monkeypatch) -> None:
    models = tmp_path / "malloy" / "models"
    models.mkdir(parents=True)
    (models / "jaffle.malloy").write_text(
        "source: customers is jaffle.table('comparison_customers') extend {}\n"
        'source: segments is jaffle.sql("""select 1""") extend {}\n'
        "source: orders is jaffle.table('comparison_orders') extend {\n"
        "  join_one: segments on true\n  join_one: customers on true\n}\n"
        "query: q08_x is orders -> { aggregate: n is count() }\n",
        encoding="utf-8",
    )
    (tmp_path / "q08.sql").write_text("select 1", encoding="utf-8")
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    helpers, _ = rubric.malloy({"question_id": "q08_x", "sql_path": "q08.sql"})
    assert helpers == ["SQL source segments"]


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


def test_every_layer_reading_a_rollup_column_is_labeled_precomputed() -> None:
    labels = json.loads(rubric.OUTPUT_PATH.read_text(encoding="utf-8"))["labels"]
    for layer in LAYERS:
        for qid in (
            "q11_repeat_customer_orders_by_store_by_month",
            "q12_orders_by_month_with_lifetime_spend_500_filter",
        ):
            assert labels[layer][qid]["label"] == "precomputed", (layer, qid)


def test_semantic_rails_provenance_covers_queries_runner_and_questions() -> None:
    path = SCRIPTS.parents[1] / "semantic_rails" / "scripts" / "run_questions.py"
    spec = importlib.util.spec_from_file_location("semantic_rails_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module._provenance()) == {
        "semantic_rails_commit",
        "semantic_rails_tree",
        "package_tree",
        "layer_tree",
        "questions_blob",
        "inputs_modified",
    }
