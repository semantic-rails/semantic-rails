"""Claims and labels the comparison pack publishes are generated from its own checks."""

from __future__ import annotations

import copy
import importlib.util
import io
import itertools
import json
import os
import re
import subprocess
import sys
import urllib.error
from collections import Counter
from contextlib import closing
from decimal import Decimal
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2] / "comparisons" / "semantic_layers" / "shared" / "scripts"
)


def _load(name: str, path: Path | None = None) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path or SCRIPTS / f"{name}.py")
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
REPORT = SCRIPTS.parent / "results" / "validation" / "output_consistency.json"
MATRIX = SCRIPTS.parent / "capability_matrix.json"
CUBE_ORDERS = "cubes:\n  - name: orders\n    sql_table: comparison_orders\n"
CUBE_SQL = {"sql": {"sql": ["select 1", []]}}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _labels() -> dict[str, Any]:
    """The committed rubric labels, by layer and question id."""
    return _json(rubric.OUTPUT_PATH)["labels"]


def _write(root: Path, files: dict[str, Any]) -> Path:
    """Write files under root, creating directories; content that isn't text is JSON."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
    return root


def _edit_json(path: Path, **changes: Any) -> None:
    _write(path.parent, {path.name: _json(path) | changes})


def _variants() -> list[str]:
    return sorted(q["id"] for q in generator.load_questions() if q["scope_level"] == "variant")


@pytest.fixture
def pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch pack and repository root for the rubric's detectors."""
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.fixture(scope="module")
def contracts() -> tuple[dict[str, Any], dict[str, Any]]:
    return generator.build_contracts()


def _question(qid: str, status: str = "matched") -> dict[str, Any]:
    return {
        "question_id": qid,
        "slice": "shared" if qid in SHARED else "semantic_rails_targeted",
        "comparison_status": status,
        "comparable_layers": list(LAYERS),
        "current_layers": list(LAYERS),
        "layer_statuses": dict.fromkeys(LAYERS, "native"),
    }


def _report(items: list[dict[str, Any]], stale: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "stale_layers": stale or {},
    }


def _claims(
    items: list[dict[str, Any]],
    labels: dict[str, str] | None = None,
    stale: dict[str, Any] | None = None,
    layer_fields: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    ids = [item["question_id"] for item in items]
    questions = [{"id": qid, "title": qid, "bypass_columns": BYPASS.get(qid, [])} for qid in ids]
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
                {"question_id": qid, "support_status": (labels or {}).get(layer, "precomputed")}
                for qid in ids
            ],
        }
        for layer in LAYERS
    ]
    return generator.claim_findings(_report(items, stale), questions, slice_ids, layers)


def test_agreement_groups_agree_pairwise_despite_non_transitive_tolerance() -> None:
    def rows(value: str) -> list[dict[str, Any]]:
        return [{"orders": Decimal(value)}]

    by_layer = {
        "semantic_rails": "10",
        "metricflow": "1",
        "cube": "1.0000009",
        "malloy": "0.9999991",
    }
    groups = validator._agreement_groups({layer: rows(value) for layer, value in by_layer.items()})
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


def test_headline_and_bypass_sentences_when_everything_matches() -> None:
    items = [_question(qid) for qid in SHARED + TARGETED]
    claims = _claims(items)
    assert claims[0] == (
        "On all 16 questions, all 6 layers return the independent answer key's normalized "
        "outputs, with numbers matching within 1e-6."
    )
    assert not any(claim.startswith("On q") for claim in claims)
    # The bypass sentence names every layer's label.
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


def _load_runner(layer: str) -> ModuleType:
    return _load(f"{layer}_runner", SCRIPTS.parents[1] / layer / "scripts" / "run_questions.py")


def test_snowflake_run_records_only_one_agreed_fingerprint() -> None:
    loaded = _load_runner("snowflake_semantic_views").loaded_fingerprint
    assert loaded([{"FINGERPRINT": "abc"}]) == "abc"
    assert loaded([{"FINGERPRINT": "abc"}, {"FINGERPRINT": "abc"}]) == "abc"
    assert loaded([{"FINGERPRINT": "abc"}, {"FINGERPRINT": "def"}]) is None
    assert loaded([]) is None
    assert loaded([{"FINGERPRINT": None}]) is None


def _run_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snowflake: dict) -> dict:
    """Run the output check on a three-layer, one-question pack whose Snowflake run is given."""
    layers = ["semantic_rails", "ktx", "snowflake_semantic_views"]
    maps = {layer: {"q01_x": {"month": "order_month", "orders": "orders"}} for layer in layers}
    files: dict[str, Any] = {
        "questions.yml": "questions:\n  - id: q01_x\n    title: X\n    scope_level: required\n",
        "results/oracle/q01_x.json": [{"month": "2016-09-01", "orders": 5}],
        "oracle/q01_x.sql": "SELECT DATE '2016-09-01' AS month, 5 AS orders",
        "column_maps.yml": maps,
    }
    for layer in layers:
        snow = layer == "snowflake_semantic_views"
        rows = [{"order_month": "2016-09-01", "orders": snowflake["orders"] if snow else 5}]
        files[f"results/{layer}/q01_x.json"] = {"rows": rows} if layer == "semantic_rails" else rows
        question = {
            "question_id": "q01_x",
            "status": snowflake.get("status", "executed") if snow else "executed",
            "result_path": f"results/{layer}/q01_x.json",
        }
        files[f"results/{layer}/summary.json"] = {
            "dataset_fingerprint": snowflake["fingerprint"] if snow else "fp-now",
            "generated_at": "2026-09-23T00:00:00+00:00",
            "questions": [question],
        }
    _write(tmp_path, files)
    key_summary = {
        "dataset_fingerprint": "fp-now",
        "answer_key_fingerprint": validator.answer_key_fingerprint(
            tmp_path / "oracle", tmp_path / "questions.yml"
        ),
        "questions": [{"question_id": "q01_x", "result_path": "results/oracle/q01_x.json"}],
    }
    _write(tmp_path, {"results/oracle/summary.json": key_summary})
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
    report = _json(tmp_path / "validation" / "output_consistency.json")
    report["markdown"] = (tmp_path / "validation" / "output_consistency.md").read_text()
    return report


def test_fresh_snowflake_run_rejoins_the_comparison(tmp_path, monkeypatch) -> None:
    fresh = {"fingerprint": "fp-now", "orders": 5, "status": "workaround"}
    matching = _run_validator(tmp_path / "a", monkeypatch, fresh)
    assert matching["stale_layers"] == {}
    assert matching["summary"]["matched"] == 1
    # The report records only whether each layer ran, not how.
    assert matching["questions"][0]["layer_statuses"]["snowflake_semantic_views"] == "executed"
    mismatching = _run_validator(
        tmp_path / "b", monkeypatch, {"fingerprint": "fp-now", "orders": 6}
    )
    assert mismatching["summary"]["mismatched"] == 1
    assert mismatching["questions"][0]["mismatches"][0]["layer"] == "snowflake_semantic_views"


def test_an_answer_key_from_older_data_is_refused(tmp_path, monkeypatch) -> None:
    assert _run_validator(tmp_path, monkeypatch, {"fingerprint": "fp-now", "orders": 5})
    _edit_json(tmp_path / "results" / "oracle" / "summary.json", dataset_fingerprint="fp-old")
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
    runner = _load_runner("snowflake_semantic_views")
    _write(tmp_path, {"query_examples.sql": f"-- q01_orders_by_month\n{sql}\n"})

    def snow_sql(*, query=None, file_path=None):
        if query and "broken" in query:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="SQL compilation error")
        rows = [{"FINGERPRINT": "fp-now"}] if query == runner.DATASET_SQL else []
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SHARED_RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(runner, "QUERY_EXAMPLES_PATH", tmp_path / "query_examples.sql")
    monkeypatch.setattr(runner, "run_snow_sql", snow_sql)
    monkeypatch.setattr(
        runner, "run_command", lambda args, **_: subprocess.CompletedProcess(args, 0, "", "")
    )
    runner.main()
    return json.loads((tmp_path / "results" / "summary.json").read_text())


def test_snowflake_runner_records_the_fingerprint_and_a_failed_question(
    tmp_path, monkeypatch
) -> None:
    summary = _run_snowflake_runner(tmp_path / "ok", monkeypatch, "SELECT 1;")
    assert summary["dataset_fingerprint"] == "fp-now"
    assert summary["questions"][0]["status"] == "executed"
    # A question that fails is listed as unsupported, with Snowflake's reason.
    [entry] = _run_snowflake_runner(tmp_path / "broken", monkeypatch, "SELECT broken;")["questions"]
    assert entry["question_id"] == "q01_orders_by_month"
    assert entry["status"] == "unsupported"
    assert entry["reason"] == "SQL compilation error"


def test_cube_runs_with_only_the_environment_node_needs(monkeypatch) -> None:
    cube = _load_runner("cube")
    for key in ("CUBEJS_DEV_MODE", "CUBEJS_TESSERACT_SQL_PLANNER", "CUBE_DUCKDB_PATH", "PORT"):
        monkeypatch.setenv(key, "x")
    env = cube._server_environment()
    assert set(env) <= {"PATH", "HOME", "TMPDIR", "CUBEJS_API_SECRET"}
    assert env["PATH"] == os.environ["PATH"]  # Popen finds `node` through the child's PATH
    assert env["CUBEJS_API_SECRET"] == cube.API_SECRET


def test_cube_is_killed_when_it_ignores_the_stop_signal() -> None:
    calls = []

    def wait(timeout: float | None = None) -> None:
        calls.append("wait")
        if timeout:
            raise subprocess.TimeoutExpired("node", timeout)

    terminate, kill = lambda: calls.append("terminate"), lambda: calls.append("kill")
    _load_runner("cube")._stop(SimpleNamespace(terminate=terminate, kill=kill, wait=wait))
    assert calls == ["terminate", "wait", "kill", "wait"]


def test_cube_start_stops_at_a_refused_request(monkeypatch) -> None:
    cube = _load_runner("cube")

    def refuse(path: str, query: str | None = None) -> str:
        raise urllib.error.HTTPError(cube.BASE_URL + path, 403, "Forbidden", Message(), None)

    monkeypatch.setattr(cube, "_request", refuse)
    running = SimpleNamespace(poll=lambda: None)
    with pytest.raises(SystemExit, match="HTTP 403"):
        cube._wait_for_meta(running, io.BytesIO())


def test_ktx_refuses_a_cached_wheel_off_the_pin_without_fetching(tmp_path, monkeypatch) -> None:
    ktx = _load_runner("ktx")
    monkeypatch.setattr(ktx, "KTX_DIR", tmp_path)
    (tmp_path / ktx.KTX_WHEEL_NAME).write_bytes(b"not the pinned wheel")
    monkeypatch.setattr(ktx.subprocess, "run", lambda *_, **__: pytest.fail("fetched"))
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(SystemExit, match="pinned sha256"):
        ktx._ktx_wheel(private)
    assert not any(private.iterdir())


def test_cube_sql_excerpts_are_the_generated_sql_and_unassessed_answers_publish_none(
    contracts,
) -> None:
    for layer in contracts[0]["layers"]:
        for entry in layer["questions"]:
            if entry["support_status"] == "not_assessed":
                assert not entry["query_excerpt"] and not entry["sql_excerpt"], entry["question_id"]
            # A model change runs no query, so has no SQL.
            if layer["id"] == "cube" and entry["support_status"] != "requires_model_change":
                assert entry["sql_excerpt"].lstrip().upper().startswith(("SELECT", "WITH")), entry


def test_a_missing_mapped_column_fails_instead_of_being_guessed() -> None:
    rows = [{"ordered_month": "2016-09-01", "orders": 5}]
    columns = {"month": "order_month", "orders": "orders"}
    with pytest.raises(SystemExit, match="'order_month' for 'month' is missing"):
        validator._normalize_rows("q01_orders_by_month", rows, columns)


def test_column_maps_cover_every_field_of_every_answer() -> None:
    maps = validator._load_column_maps()
    for item in _json(REPORT)["questions"]:
        qid = item["question_id"]
        for layer, status in item["layer_statuses"].items():
            # Every layer maps q01-q16; a frozen-model variant only where the layer executed it.
            if item["slice"] != "frozen_model" or status == "executed":
                assert sorted(maps[layer][qid]) == sorted(validator.QUESTION_FIELDS[qid])


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
    committed = _json(oracle.RESULTS_DIR / "summary.json")
    assert committed["answer_key_fingerprint"] == oracle.answer_key_fingerprint(
        oracle.ORACLE_DIR, oracle.QUESTIONS_PATH
    )
    with closing(duckdb.connect(str(tmp_path / "shared.duckdb"), read_only=True)) as con:
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET threads = 1")
        for entry in committed["questions"]:
            qid = entry["question_id"]
            fresh = json.loads(json.dumps(oracle.answer(con, qid), default=str))
            saved = _json(oracle.REPO_ROOT / entry["result_path"])
            equal, detail = validator._rows_equal(
                validator._normalize_rows(qid, saved), validator._normalize_rows(qid, fresh)
            )
            assert equal, (qid, detail)


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
    derived, pipelines = ["derived model orders"], ["relation pipelines"]
    # (model, package.yml additions, other package files, what the detector finds)
    cases = {
        "view": (view, "", {}, []),
        "pipeline": ({"id": "orders", "relation": {"steps": []}}, "", {}, derived),
        "raw_table": ({"id": "orders", "relation": "jaffle_order"}, "", {}, derived),
        "relation_ref": (view | {"relation_ref": "rollup"}, "", {}, derived),
        "variants": (view | {"variants": {"daily": {}}}, "", {}, derived),
        "relations": (view, "", {"relations.yml": "relations:\n  derived: {}\n"}, pipelines),
        "relations_dir": (
            view,
            "",
            {"relations/derived.yml": "relations:\n  derived: {}\n"},
            pipelines,
        ),
        "inline_model": (None, "models:\n  orders:\n    relation: jaffle_order\n", {}, derived),
    }
    _write(tmp_path, {"sql.sql": "select 1"})
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    for name, (model, extra, files, expected) in cases.items():
        package = {"package.yml": f"package:\n  id: package\n{extra}", **files}
        if model is not None:
            package["models/orders.yml"] = {"model": model}
        monkeypatch.setattr(rubric, "SR_PACKAGE", _write(tmp_path / name, package))
        entry = {"question_id": "q_x", "sql_path": "sql.sql"}
        assert rubric.semantic_rails(entry)[0] == expected, name


def test_a_detector_that_finds_nothing_fails_closed(tmp_path, monkeypatch) -> None:
    _write(
        tmp_path,
        {
            "models/_models.yml": "models: []\n",
            "sql.txt": "SQL (remove --explain to see data):\nselect 1",
            "package/package.yml": "package:\n  id: package\n",
            "query.json": "{}",
        },
    )
    monkeypatch.setattr(rubric, "MF_MODELS", tmp_path / "models")
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match="found no relations"):
        rubric.metricflow({"question_id": "q_x", "sql_path": "sql.txt"})
    monkeypatch.setattr(rubric, "SR_PACKAGE", tmp_path / "package")
    with pytest.raises(SystemExit, match="found no models"):
        rubric.semantic_rails({"question_id": "q_x"})
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    with pytest.raises(SystemExit, match="found no cubes"):
        rubric.cube({"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.txt"})
    with pytest.raises(SystemExit, match="found no sources"):
        rubric.ktx({"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.txt"})
    _write(
        tmp_path,
        {"malloy/models/jaffle.malloy": "query: q_y is orders -> { aggregate: n is count() }\n"},
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


def test_cube_counts_a_sql_cube_used_only_by_a_filter(pack: Path) -> None:
    query = {
        "measures": ["orders.count"],
        "filters": [{"member": "segments.customer_id", "operator": "set"}],
    }
    _write(
        pack,
        {
            "cube/model/cubes/orders.yml": CUBE_ORDERS,
            "cube/model/cubes/segments.yml": "cubes:\n  - name: segments\n"
            "    sql: select customer_id from comparison_orders group by 1\n",
            "query.json": query,
            "sql.json": CUBE_SQL,
        },
    )
    entry = {"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.json"}
    assert rubric.cube(entry)[0] == ["cube segments"]


def test_metricflow_counts_helper_models_but_not_passthroughs_or_the_time_spine(
    tmp_path, monkeypatch
) -> None:
    _write(
        tmp_path,
        {
            "models/_models.yml": "models:\n  - name: all_days\n    time_spine: {}\n",
            "models/all_days.sql": "select generate_series(1, 3)",
            "models/staging/orders.sql": "select * from comparison_orders",
            "models/staging/helper.sql": "select customer_id from comparison_orders group by 1",
            "sql.txt": 'SQL (remove --explain to see data):\nselect * from "db"."main"."orders", '
            '"db"."main"."helper", "db"."main"."all_days"',
        },
    )
    monkeypatch.setattr(rubric, "MF_MODELS", tmp_path / "models")
    monkeypatch.setattr(rubric, "REPO_ROOT", tmp_path)
    helpers, texts = rubric.metricflow({"sql_path": "sql.txt"})
    assert helpers == ["dbt model helper"]
    assert len(texts) == 2


def test_malloy_counts_the_sql_blocks_its_executed_sql_reads(pack: Path) -> None:
    model = (
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
        "query:  q08_x is orders -> { aggregate: n is count() }\n"
    )
    # Malloy compiles each SQL block the query reads into its SQL; `unused` and `prefix` aren't.
    sql = (
        "SELECT count(1) FROM comparison_orders AS base\n"
        "LEFT JOIN (\n  select customer_id, segment\n  from comparison_customer_history\n) AS h\n"
        "LEFT JOIN (select * from comparison_customers) AS p\n"
        "LEFT JOIN (\nselect order_id from comparison_order_items\n) AS inline"
    )
    _write(pack, {"malloy/models/jaffle.malloy": model, "q08.sql": sql})
    helpers, _ = rubric.malloy({"question_id": "q08_x", "sql_path": "q08.sql"})
    assert helpers == ["SQL source inline", "SQL source segments"]


def test_a_question_without_an_executed_result_is_unsupported(tmp_path, monkeypatch) -> None:
    executed = [{"question_id": qid, "status": "executed"} for qid in ("q01_a", "q03_c")]
    _write(
        tmp_path,
        {
            "shared/questions.yml": "questions:\n  - id: q01_a\n  - id: q02_b\n  - id: q03_c\n",
            "shared/frozen_model.yml": "{}\n",
            "results/fake/summary.json": {"questions": executed},
            "results/fake/unsupported.json": {
                "q03_c": {"status": "unsupported", "reason": "error"}
            },
        },
    )
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(rubric, "FROZEN_MODEL_PATH", tmp_path / "shared" / "frozen_model.yml")
    monkeypatch.setattr(rubric, "DETECTORS", {"fake": ("fake", lambda entry: ([], ["select 1"]))})
    labels = rubric.build_labels()["labels"]["fake"]
    assert {qid: label["label"] for qid, label in labels.items()} == {
        "q01_a": "native",
        "q02_b": "unsupported",
        "q03_c": "unsupported",
    }


def _frozen_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[str]) -> Path:
    """A pack with one q01-q16 question and three frozen-model variants, for two layers."""
    variants = "".join(
        f"  - id: {qid}\n    scope_level: variant\n" for qid in ("q17", "q18", "q19")
    )
    entries = [{"question_id": qid, "status": "executed"} for qid in ["q01", *executed]]
    _write(
        tmp_path,
        {
            "shared/questions.yml": f"questions:\n  - id: q01\n    scope_level: required\n{variants}",
            "results/fake/summary.json": {"questions": entries},
            "results/other/summary.json": {"questions": entries},
            "fake_model/orders.yml": "measures: [revenue]\n",
        },
    )
    monkeypatch.setattr(rubric, "PACK", tmp_path)
    monkeypatch.setattr(rubric, "RESULTS", tmp_path / "results")
    frozen = {
        "fake": {
            "model": ["fake_model"],
            "sha256": rubric.model_digest(["fake_model"]),
            "requires_model_change": {"q18": {"reason": "Set in the model.", "doc": "https://d"}},
        }
    }
    _write(tmp_path, {"frozen_model.yml": frozen})
    monkeypatch.setattr(rubric, "FROZEN_MODEL_PATH", tmp_path / "frozen_model.yml")
    detect = ("fake", lambda entry: ([], ["select 1"]))
    monkeypatch.setattr(rubric, "DETECTORS", {"fake": detect, "other": ("other", detect[1])})
    return tmp_path


def test_frozen_model_labels_come_first_on_the_variants(tmp_path, monkeypatch) -> None:
    _frozen_pack(tmp_path, monkeypatch, executed=["q17"])
    labels = rubric.build_labels()["labels"]
    assert {qid: label["label"] for qid, label in labels["fake"].items()} == {
        "q01": "native",
        "q17": "native",
        "q18": "requires_model_change",
        "q19": "unsupported",  # neither answered nor declared: a failed attempt
    }
    assert labels["fake"]["q18"]["evidence"] == ["Set in the model.", "https://d"]
    # A layer frozen_model.yml doesn't list keeps its q01-q16 labels and isn't assessed on the rest.
    assert {qid: label["label"] for qid, label in labels["other"].items()} == {
        "q01": "native",
        **dict.fromkeys(("q17", "q18", "q19"), "not_assessed"),
    }


def test_a_declared_model_change_the_layer_executed_is_refused(tmp_path, monkeypatch) -> None:
    _frozen_pack(tmp_path, monkeypatch, executed=["q18"])
    with pytest.raises(SystemExit, match="q18 is declared requires_model_change"):
        rubric.build_labels()


def test_a_frozen_model_workaround_must_say_why(tmp_path, monkeypatch) -> None:
    _frozen_pack(tmp_path, monkeypatch, executed=["q17"])
    detect = ("fake", lambda entry: (["SQL API window function"], ["select 1"]))
    monkeypatch.setattr(rubric, "DETECTORS", {"fake": detect})
    with pytest.raises(SystemExit, match="q17 is a workaround, but frozen_model.yml doesn't say"):
        rubric.build_labels()
    frozen = _json(tmp_path / "frozen_model.yml")
    frozen["fake"]["workaround"] = {"q17": {"reason": "SQL around a query.", "doc": "https://w"}}
    _write(tmp_path, {"frozen_model.yml": frozen})
    assert rubric.build_labels()["labels"]["fake"]["q17"]["evidence"] == [
        "SQL API window function",
        "SQL around a query.",
        "https://w",
    ]


def test_a_changed_model_stops_the_rubric(tmp_path, monkeypatch) -> None:
    pack = _frozen_pack(tmp_path, monkeypatch, executed=[])
    (pack / "fake_model" / "orders.yml").write_text("measures: [revenue, large_revenue]\n", "utf-8")
    with pytest.raises(SystemExit, match="fake: its model .* changed"):
        rubric.build_labels()


def test_cube_sql_api_queries_around_a_cube_query_are_hand_written(pack: Path) -> None:
    _write(pack, {"cube/model/cubes/orders.yml": CUBE_ORDERS, "sql.json": CUBE_SQL})
    member = (
        "SELECT DATE_TRUNC('month', ordered_at) AS m, MEASURE(revenue) AS r FROM orders GROUP BY 1"
    )
    flat = "SELECT DATE_TRUNC('month', ordered_at) AS m, {} FROM orders GROUP BY 1"
    derived, other = "SQL API query over a derived table", "SQL API HAVING, FILTER or UNION"
    # (SQL API query, what the detector finds)
    cases = {
        "plain.sql": (member, []),
        "aggregate.sql": (flat.format("AVG(revenue), MAX(revenue)"), []),
        "derived.sql": (f"SELECT m, SUM(r) FROM ({member}) AS t WHERE r > 5 GROUP BY 1", [derived]),
        "window.sql": (
            f"SELECT m, LAG(r) OVER (ORDER BY m) FROM ( {member} ) AS t",
            [derived, "SQL API window function"],
        ),
        "case.sql": (
            flat.format("SUM(CASE WHEN revenue >= 50 THEN revenue END)"),
            ["SQL API CASE expression"],
        ),
        "having.sql": (flat.format("MEASURE(revenue)") + " HAVING MEASURE(revenue) > 5", [other]),
        "filter.sql": (flat.format("SUM(revenue) FILTER (WHERE revenue >= 50)"), [other]),
        "union.sql": (f"{member} UNION ALL {member}", [other]),
        # EXTRACT(... FROM column) names no cube.
        "extract.sql": (flat.format("EXTRACT(EPOCH FROM ordered_at)"), []),
    }
    _write(pack, {name: sql for name, (sql, _) in cases.items()})
    for name, (_, expected) in cases.items():
        entry = {"question_id": "q_x", "query_path": name, "sql_path": "sql.json"}
        assert rubric.cube(entry)[0] == expected, name


def test_ktx_inline_measures_are_checked_against_the_sources_they_read(pack: Path) -> None:
    _write(
        pack,
        {
            "ktx/sources/orders.yaml": "name: orders\ntable: comparison_orders\n",
            "ktx/sources/facts.yaml": "name: facts\n"
            "sql: select customer_id from comparison_orders group by 1\n",
            "sql.sql": "select 1",
        },
    )
    found = []
    for expr in (
        "sum(case when orders.total >= 5000 then orders.total / 100.0 else 0 end)",
        "max(facts.customer_id)",
    ):
        _write(pack, {"query.json": {"measures": ["orders.revenue", {"name": "x", "expr": expr}]}})
        entry = {"question_id": "q_x", "query_path": "query.json", "sql_path": "sql.sql"}
        found.append(rubric.ktx(entry)[0])
    assert found == [[], ["SQL source facts"]]


def test_malloy_reads_a_variant_query_only_from_its_own_query_file(pack: Path) -> None:
    query = (
        "query: q17_x is orders extend {\n"
        '  join_one: facts is jaffle.sql("""select order_id from comparison_order_items""") on true\n'
        "} -> { aggregate: n is count() }\n"
    )
    query_file = pack / "malloy" / "queries" / "q17_x.malloy"
    _write(
        pack,
        {
            "malloy/models/jaffle.malloy": "source: orders is jaffle.table('comparison_orders')\n",
            "malloy/queries/q17_x.malloy": f'import "../models/jaffle.malloy"\n{query}',
            "q17.sql": "SELECT 1 FROM comparison_orders "
            "LEFT JOIN (select order_id from comparison_order_items)",
        },
    )
    entry = {"question_id": "q17_x", "sql_path": "q17.sql"}
    assert rubric.malloy(entry)[0] == ["SQL source facts"]
    raw = query.replace("aggregate: n is count()", 'aggregate: n is sql_number("count(*)")')
    query_file.write_text(f'import "../models/jaffle.malloy"\n{raw}', encoding="utf-8")
    assert rubric.malloy(entry)[0] == ["SQL source facts", "raw SQL expression in the query"]
    # Anything else here would be model authoring outside the pinned model.
    for extra in (
        "source: orders2 is orders extend {}\n",
        "  source: orders2 is orders extend {}\n",
        'import "../extra.malloy"\n',
        "run: orders -> { aggregate: n is count() }\n",
    ):
        query_file.write_text(f'import "../models/jaffle.malloy"\n{extra}{query}', "utf-8")
        with pytest.raises(SystemExit, match="one import of the model and one query"):
            rubric.malloy(entry)


def test_a_missing_result_fails_the_check_unless_the_question_is_a_variant(
    tmp_path, monkeypatch
) -> None:
    assert _run_validator(tmp_path, monkeypatch, {"fingerprint": "fp-now", "orders": 5})
    _edit_json(tmp_path / "results" / "ktx" / "summary.json", questions=[])
    with pytest.raises(SystemExit, match="ktx has no result for q01_x"):
        validator.main()
    questions = tmp_path / "questions.yml"
    questions.write_text(questions.read_text("utf-8").replace("required", "variant"), "utf-8")
    fingerprint = validator.answer_key_fingerprint(tmp_path / "oracle", questions)
    _edit_json(tmp_path / "results/oracle/summary.json", answer_key_fingerprint=fingerprint)
    validator.main()
    report = _json(tmp_path / "validation" / "output_consistency.json")
    assert report["questions"][0]["layer_statuses"]["ktx"] == "not_run"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('{"schema": [{"name": "m"}, {"name": "n"}]}\n{"data": [["2016-09", "5"]]}\n', None),
        ('{"schema": [{"name": "m"}]}\n{"error": "Planning Error: no"}\n', "Planning Error: no"),
        ('{"data": [["2016-09"]]}\n', "no schema"),
    ],
)
def test_cube_sql_api_rows_come_back_in_the_rest_shape(monkeypatch, body, error) -> None:
    cube = _load_runner("cube")
    monkeypatch.setattr(cube.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body.encode()))
    if error:
        with pytest.raises(cube.SqlApiError, match=error):
            cube._cubesql("SELECT 1")
    else:
        assert json.loads(cube._cubesql("SELECT 1"))["data"] == [{"m": "2016-09", "n": "5"}]
    monkeypatch.setattr(cube, "SQL_API_ROW_LIMIT", 1)
    if not error:  # a result that reaches the row limit may be cut off
        with pytest.raises(cube.SqlApiError, match="row limit"):
            cube._cubesql("SELECT 1")


def test_every_assessed_layer_answers_or_declares_each_variant() -> None:
    """No variant is left as a silent failure, and every declared model change says why."""
    labels, frozen, variants = _labels(), rubric.load_frozen_models(), _variants()
    assert len(variants) == 8
    for layer in LAYERS:
        spec = frozen.get(layer)
        declared = (spec or {}).get("requires_model_change", {})
        assert set(declared) <= set(variants), layer
        for qid, reason in declared.items():
            assert reason["reason"].strip() and reason["doc"].startswith("https://"), (layer, qid)
        for qid in variants:
            label = labels[layer][qid]["label"]
            if spec is None:
                assert label == "not_assessed", (layer, qid)
            elif qid in declared:
                assert label == "requires_model_change", (layer, qid)
            else:
                assert label in {"native", "workaround", "precomputed"}, (layer, qid, label)
            if label in {"requires_model_change", "workaround"}:  # it says why, with a link
                assert labels[layer][qid]["evidence"][-1].startswith("https://"), (layer, qid)


def test_the_published_frozen_model_counts_come_from_the_labels() -> None:
    labels, matrix = _labels(), _json(MATRIX)
    variants = [row["question_id"] for row in matrix["rows"] if row["slice"] == "frozen_model"]
    for layer in matrix["layers"]:
        got = [labels[layer["id"]][qid]["label"] for qid in variants]
        frozen = layer["answered_with_model_frozen"]
        if "not_assessed" in got:
            assert frozen is None
            continue
        answered = sum(label in {"native", "workaround", "precomputed"} for label in got)
        assert frozen == {"answered": answered, "questions": 8, **Counter(got)}, layer["id"]
    lead, disclosure = matrix["claims"][:2]
    assert lead.startswith("The 8 frozen-model questions (q17-q24)")
    # The counts follow the pack's fixed layer order, not a ranking, and the set's origin is said.
    counts = lead.split("out of 8: ", 1)[1].split(". ", 1)[0]
    listed = [generator.LAYER_META[layer]["label"] for layer in LAYERS]
    listed = [label for label in listed if f"{label} " in counts]
    assert sorted(listed, key=counts.index) == listed  # every layer in the pack's order
    assert "Semantic Rails authors chose them" in disclosure and "aren't a ranking" in disclosure


def test_committed_labels_are_what_the_rubric_derives() -> None:
    committed = _json(rubric.OUTPUT_PATH)
    derived = rubric.build_labels()
    assert derived["rules"] == committed["rules"]
    assert derived["labels"] == committed["labels"]


def test_published_and_generated_contracts_take_rubric_labels_and_count_current_layers(
    contracts,
) -> None:
    labels, matrix = _labels(), contracts[1]
    for row in _json(MATRIX)["rows"] + matrix["rows"]:
        qid = row["question_id"]
        assert row["statuses"] == {layer: labels[layer][qid]["label"] for layer in LAYERS}
    assert any("(Semantic Rails, MetricFlow, Cube, Malloy and KtX)" in c for c in matrix["claims"])
    stale = [layer["id"] for layer in matrix["layers"] if layer["dataset"] == "stale"]
    assert stale == ["snowflake_semantic_views"]


def test_q11_and_q12_are_precomputed_exactly_where_a_layer_reads_the_rollup_column() -> None:
    labels = _labels()
    reading = {"snowflake_semantic_views", "ktx"}
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
    by_layer = {
        layer: {int(qid[1:3]): label["label"] for qid, label in questions.items()}
        for layer, questions in _labels().items()
    }
    items = _json(REPORT)["questions"]
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
        ("frozen_model", "## Frozen-Model Questions"),
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


def test_semantic_rails_provenance_covers_queries_runner_and_questions() -> None:
    module = _load_runner("semantic_rails")
    assert set(module._provenance()) == {
        "semantic_rails_commit",
        "semantic_rails_tree",
        "engine_release",
        "package_tree",
        "layer_tree",
        "questions_blob",
        "inputs_modified",
    }


def test_semantic_rails_frozen_queries_reproduce_the_answer_key(tmp_path, monkeypatch) -> None:
    from semantic_rails import config
    from semantic_rails.runtime import Runtime

    bootstrap = _load("bootstrap_shared_duckdb")
    runner = _load_runner("semantic_rails")
    monkeypatch.setattr(bootstrap, "DB_PATH", tmp_path / "shared.duckdb")
    bootstrap.main()
    monkeypatch.setattr(
        config, "list_package_paths", lambda: {runner.PACKAGE_ID: str(runner.PACKAGE_DIR)}
    )
    resolve = Runtime._resolve_asset_path
    monkeypatch.setattr(
        Runtime,
        "_resolve_asset_path",
        lambda self, value, *, kind: (
            str(bootstrap.DB_PATH) if kind == "default_db" else resolve(self, value, kind=kind)
        ),
    )
    maps = validator._load_column_maps()["semantic_rails"]
    with closing(Runtime(runner.PACKAGE_ID)) as runtime:
        for qid in _variants():
            query = _json(runner.QUERY_DIR / f"{qid}.json")
            validated = runtime.validate(query)
            assert validated["ok"], (qid, validated.get("errors"))
            fresh = runtime.query(query)
            saved = _json(SCRIPTS.parent / "results" / "oracle" / qid / "result.json")
            equal, detail = validator._rows_equal(
                validator._normalize_rows(qid, saved),
                validator._normalize_rows(qid, fresh["rows"], maps[qid]),
            )
            assert equal, (qid, detail)


def test_frozen_count_discloses_the_engine_and_release_capture() -> None:
    summary = _json(SCRIPTS.parent / "results/semantic_rails/summary.json")
    release = _json(SCRIPTS.parent / "results/semantic_rails/latest_release_frozen_model.json")
    frozen = rubric.load_frozen_models()["semantic_rails"]
    assert release["model_sha256"] == frozen["sha256"]
    assert release["queries_sha256"] == rubric.model_digest(["semantic_rails/queries"])
    # The latest release loads this package version and answers every variant; each answer it
    # executed matches the answer key, and the note gives its count.
    assert "load_error" not in release
    assert [item["question_id"] for item in release["questions"]] == _variants()
    executed = [item for item in release["questions"] if item["status"] == "executed"]
    assert all(item["answer_key_matches"] for item in executed)
    count = f"{len(executed)} of {len(release['questions'])}"
    note = generator.semantic_rails_frozen_version_note()
    tree = summary["semantic_rails_tree"][:7]
    engine = f"an unreleased engine ({summary['semantic_rails_version']}, engine tree {tree})"
    assert f"Semantic Rails' current count uses {engine};" in note
    assert summary["semantic_rails_commit"] not in note  # a branch commit may not survive a squash
    assert f"{release['engine_release']}, answers {count} on the same frozen model" in note
    assert "refuses to load" not in note
    for name in ("capability_matrix.json", "comparison_data.json"):
        contract = _json(SCRIPTS.parent / name)
        assert note in contract.get("claims", contract.get("headline_findings"))[0]
    readme = (SCRIPTS.parents[1] / "README.md").read_text("utf-8")
    assert note in " ".join(readme.split())
    table = readme.split("## Frozen-Model Questions:", 1)[1]
    row = next(line for line in table.splitlines() if line.startswith("| Semantic Rails |"))
    assert f"engine tree `{tree}`" in row and "unreleased" in row
    assert summary["semantic_rails_commit"] not in row
    assert f"{release['engine_release']}: {count}" in row


def _patch_release(monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    """Serve the latest release's frozen-model capture with `change` applied to it."""
    load = generator.load_json

    def patched(path):
        payload = load(path)
        if path.name == "latest_release_frozen_model.json":
            change(payload)
        return payload

    monkeypatch.setattr(generator, "load_json", patched)


def test_release_capture_with_answers_keeps_its_count(monkeypatch) -> None:
    executed = {"status": "executed", "row_count": 12, "answer_key_matches": True}
    refused = {
        "q19_trailing_3_month_revenue_by_month",
        "q20_revenue_and_prior_month_revenue_by_month",
    }

    def answering(payload):
        payload.pop("load_error", None)
        payload["questions"] = [
            {"question_id": qid, "status": "unsupported"}
            if qid in refused
            else {"question_id": qid, **executed}
            for qid in _variants()
        ]

    _patch_release(monkeypatch, answering)
    note = generator.semantic_rails_frozen_version_note()
    release = _json(SCRIPTS.parent / "results/semantic_rails/latest_release_frozen_model.json")
    assert (
        f"{release['engine_release']}, answers 6 of 8 on the same frozen model "
        "(it refuses q19 and q20)." in note
    )
    assert "refuses to load" not in note


def test_release_capture_that_refuses_the_package_claims_no_count(monkeypatch) -> None:
    error = {
        "code": "INVALID_CONFIG",
        "message": "semantic_rails/package: duplicate metric id 'metric.jaffle.revenue_usd' "
        "in jaffle.revenue_usd and metric 'revenue_usd'; metric ids must be unique",
    }

    def refusing(payload):
        payload.update(engine_release="v0.3.1", load_error=error, questions=[])

    _patch_release(monkeypatch, refusing)
    note = generator.semantic_rails_frozen_version_note()
    assert (
        "v0.3.1, refuses to load this package version (INVALID_CONFIG: duplicate metric id "
        "'metric.jaffle.revenue_usd' in jaffle.revenue_usd and metric 'revenue_usd'), so it has "
        "no frozen-model count" in note
    )
    assert not re.search(r"answers \d+ of", note)


def test_release_count_requires_the_same_dataset(monkeypatch) -> None:
    _patch_release(monkeypatch, lambda payload: payload.update(dataset_fingerprint="different"))
    with pytest.raises(SystemExit, match="different dataset"):
        generator.semantic_rails_frozen_version_note()


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
    runner = _load_runner("semantic_rails")
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
