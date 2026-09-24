"""scripts/dev refactor-evidence tools: their output contracts and exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.dev import capture_dialect_sql, diff_sql_baseline, function_lengths
from semantic_rails.dialects import supported_warehouses


def _write_json(path: Path, data: object) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_function_lengths_reads_any_source_encoding_and_skips_broken_files(tmp_path, capsys):
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfdef bom():\n    return 1\n")
    (tmp_path / "latin1.py").write_bytes(
        b"# -*- coding: latin-1 -*-\ndef cafe():\n    return 'caf\xe9'\n"
    )
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (tmp_path / "nested.py").write_text(
        "class K:\n    async def m(self):\n        def inner():\n            return 1\n"
        "        return inner\n",
        encoding="utf-8",
    )

    assert function_lengths.main(["--by-name", "--min", "0", str(tmp_path)]) == 0

    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "K.m 4",
        "K.m.inner 2",
        "bom 2",
        "cafe 2",
        "functions: 4, over 150 lines: 0, over 300 lines: 0",
    ]
    assert "skipped" in err and "broken.py" in err


def test_function_lengths_rejects_a_missing_path(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        function_lengths.main([str(tmp_path / "missing")])
    assert excinfo.value.code == 2


def test_dialect_capture_compiles_every_query_for_every_dialect():
    captured = capture_dialect_sql.capture()

    assert sorted(captured) == sorted(supported_warehouses())
    assert capture_dialect_sql.failures(captured) == []
    assert len({len(rows) for rows in captured.values()}) == 1
    # BigQuery legalizes result column names, so its mapping is part of the golden output.
    assert all(row["column_mapping"] for row in captured["bigquery"].values())


def _capture(sql: str = "SELECT 1", error: str = "") -> dict:
    return {
        "duckdb": {"metric.x": {"error": error} if error else {"sql": sql, "column_mapping": []}}
    }


def test_dialect_capture_fails_when_a_statement_does_not_compile(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(capture_dialect_sql, "capture", lambda: _capture(error="X: boom"))
    out_file = tmp_path / "golden.json"

    assert capture_dialect_sql.main([str(out_file)]) == 1

    assert json.loads(out_file.read_text(encoding="utf-8")) == _capture(error="X: boom")
    assert "duckdb: metric.x: X: boom" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("golden", "after", "status"),
    [
        (_capture(), _capture(), 0),
        (_capture(), _capture("SELECT 2"), 1),
        (_capture(error="X: boom"), _capture(error="X: boom"), 1),
    ],
    ids=["identical", "changed", "identical-failures"],
)
def test_dialect_compare(tmp_path, golden, after, status):
    paths = [
        _write_json(tmp_path / "golden.json", golden),
        _write_json(tmp_path / "after.json", after),
    ]

    assert capture_dialect_sql.main(["--compare", *paths]) == status


def _baseline(**slot: object) -> dict:
    return {"metric.x": {"compile": {"sql": "SELECT 1", "warnings": []}, **slot}}


@pytest.mark.parametrize(
    ("golden", "after", "status"),
    [
        (_baseline(query={"rows": [{"a": 1}]}), _baseline(query={"rows": [{"a": 1}]}), 0),
        (_baseline(query={"rows": [{"a": 1}]}), _baseline(query_error="boom"), 1),
        (_baseline(query={"rows": [{"a": 1}]}), {}, 1),
        (_baseline(query_error="old"), _baseline(query={"rows": [{"a": 1}]}), 0),
    ],
    ids=["clean", "query-regressed", "metric-missing", "newly-succeeds"],
)
def test_sql_baseline_diff_fails_on_regressed_metrics(tmp_path, golden, after, status):
    paths = [
        _write_json(tmp_path / "golden.json", golden),
        _write_json(tmp_path / "after.json", after),
    ]

    assert diff_sql_baseline.main(*paths) == status
