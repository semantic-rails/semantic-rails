"""Coverage disclosures preserve sparse-event answers and the caller's row scope."""

import json
from dataclasses import replace
from uuid import uuid4

import duckdb
import pytest
import yaml

from semantic_rails import runtime as runtime_module
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SeedSpec

from .conftest import _rows
from .test_correctness import _backend

MEASURE = "measure.events.new_workspaces"
METRIC = "metric.events.new_workspaces"
ROLE = "temporal_role.events_event_event_at"


@pytest.fixture
def sparse_runtime(request, backend_name, tmp_path):
    backend = _backend(request, backend_name)
    events, loads = f"events_{uuid4().hex}", f"loads_{uuid4().hex}"
    documents = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "events",
                "namespace": "events",
                "warehouse": "duckdb",
                "default_db": "events.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "event": {"key": ["event_id"], "model": "events"},
                }
            }
        },
        "models/events.yml": {
            "model": {
                "id": "events",
                "relation": events,
                "entities": {"event": {}},
                "times": {
                    "event_at": {
                        "column": "event_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default": True,
                    }
                },
                "dimensions": {"caller": {"kind": "categorical"}},
                "measures": {
                    "new_workspaces": {
                        "kind": "entity_count",
                        "entity_key": "event_id",
                        "accumulation": {"kind": "flow"},
                        "rollup": "additive",
                    }
                },
            }
        },
        "policies.yml": {
            "semantic_policies": [
                {
                    "id": "policy.events.caller",
                    "kind": "row_filter",
                    "audiences": ["caller"],
                    "dimension": "dimension.events_event_caller",
                    "attribute": "caller",
                }
            ]
        },
    }
    for name, document in documents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
    config = load_package_config(str(tmp_path))
    if backend_name == "postgres":
        original = backend.runtimes["utc_implicit"].config.package
        config = replace(
            config,
            package=replace(
                config.package,
                warehouse="postgres",
                default_db="",
                seed=SeedSpec(),
                connection=original.connection,
            ),
        )
    seed = (
        f"CREATE TABLE {events} (event_id INTEGER, caller VARCHAR, event_at TIMESTAMP);"
        f"CREATE TABLE {loads} AS SELECT d AS through_day FROM "
        "generate_series(TIMESTAMP '2026-09-01', TIMESTAMP '2026-09-30', INTERVAL '1 day') t(d);"
        f"INSERT INTO {events} VALUES (1, 'early', TIMESTAMP '2026-09-12');"
    )
    if backend_name == "duckdb":
        with duckdb.connect(str(tmp_path / "events.duckdb")) as conn:
            conn.execute(seed)
    rt = Runtime.from_config(config, source_path=str(tmp_path))
    if backend_name == "postgres":
        _rows(rt, seed)
    try:
        yield rt, events, loads
    finally:
        if backend_name == "postgres":
            _rows(rt, f"DROP TABLE {events}; DROP TABLE {loads}")
        rt.close()


def _query(*, grain="week", start="2026-09-21", end="2026-09-28", caller=None, **extra):
    time = {"temporal_role": ROLE, "start": start, "end": end}
    if grain:
        time.update(grain=grain, fill=True)
    return {
        "select": [{"expression": {"measure": MEASURE}, "as": "value"}],
        "time": time,
        "policy_context": (
            RequestContext(audience="caller", attributes={"caller": caller})
            if caller
            else RequestContext()
        ).to_policy_context(),
        **extra,
    }


def _warning(result):
    warnings = [row for row in result["warnings"] if row["code"] == "NO_DATA_YET"]
    assert len(warnings) == 1
    assert not any(
        row["code"] in {"NO_DATA_IN_SCOPE", "EMPTY_RESULT_WINDOW"} for row in result["warnings"]
    )
    return warnings[0]


def _mutate(rt, sql):
    if rt.warehouse == "duckdb":
        rt.close()
        with duckdb.connect(rt.db_path) as conn:
            conn.execute(sql)
    else:
        _rows(rt, sql)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["week", "day", ""])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_past_coverage_names_its_bucket_without_changing_sql_or_rows(
    sparse_runtime,
    monkeypatch,
    grain,
    verbosity,
):
    rt, events, loads = sparse_runtime
    probes = []
    prepared_calls = []
    original_query = runtime_module._adapter_query

    def record(adapter, prepared, **kwargs):
        prepared_calls.append(prepared)
        return original_query(adapter, prepared, **kwargs)

    monkeypatch.setattr(runtime_module, "_adapter_query", record)
    monkeypatch.setattr(runtime_module, "_data_coverage_probe", lambda *a, **k: probes.append(1))
    query = _query(grain=grain, verbosity=verbosity)
    result = rt.query(query)
    warning = _warning(result)
    edge = {"week": "2026-09-07", "day": "2026-09-12", "": "2026-09-21"}[grain]
    source = "last_bucket" if grain else "before_window"
    assert warning["details"]["outputs"] == ["value"]
    assert warning["details"]["measures"] == [{"id": MEASURE, "edge": edge, "edge_source": source}]
    assert MEASURE in warning["message"] and edge in warning["message"]
    assert "loaded through" not in warning["message"]
    assert probes == []
    assert _rows(rt, f"SELECT COUNT(*) FROM {loads}") == [(30,)]
    assert _rows(
        rt,
        f"SELECT COUNT(*) FROM {events} WHERE caller='early' "
        "AND event_at >= TIMESTAMP '2026-09-21'",
    ) == [(0,)]
    assert result["row_count"] == (1 if grain == "week" else 7 if grain else 0)
    assert all(row["value"] is None for row in result["rows"])
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "_no_data_yet_warnings", lambda *a, **k: [])
        next_main = len(prepared_calls)
        before = rt.query(query)
    assert prepared_calls[0] == prepared_calls[next_main]
    assert result["rows"] == before["rows"]
    if verbosity != "minimal":
        assert result["rendered_sql"] == before["rendered_sql"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain,days", [("week", [12, 13]), ("", [12, 13, 20])])
def test_moving_an_event_within_the_bucket_has_identical_warning(sparse_runtime, grain, days):
    rt, events, _ = sparse_runtime
    warnings = []
    for day in days:
        _mutate(rt, f"UPDATE {events} SET event_at=TIMESTAMP '2026-09-{day}' WHERE caller='early'")
        warnings.append(json.dumps(_warning(rt.query(_query(grain=grain))), sort_keys=True))
    assert len(set(warnings)) == 1


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["week", ""])
@pytest.mark.parametrize("caller,expected", [("early", True), ("late", False), ("missing", True)])
def test_coverage_uses_only_the_callers_visible_rows(sparse_runtime, grain, caller, expected):
    rt, events, _ = sparse_runtime
    _mutate(rt, f"INSERT INTO {events} VALUES (2, 'late', TIMESTAMP '2026-09-23')")
    query = _query(grain=grain, caller=caller)
    if grain:
        query["time"].pop("fill")
        query["select"][0]["expression"].update(
            kind="aggregate",
            filter={
                "all": [
                    {
                        "field": "dimension.events_event_caller",
                        "op": "!=",
                        "value": "never",
                    }
                ]
            },
        )
    result = rt.query(query)
    assert any(row["code"] == "NO_DATA_YET" for row in result["warnings"]) is expected
    if expected:
        warning = _warning(result)
        if caller == "missing":
            assert warning["details"]["measures"] == [
                {"id": MEASURE, "edge": None, "edge_source": "last_bucket"}
            ]
            assert "2026-09-12" not in warning["message"] and "2026-09-23" not in warning["message"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_a_series_straddling_the_edge_warns_even_with_a_populated_bucket(sparse_runtime):
    rt, _, _ = sparse_runtime
    result = rt.query(_query(start="2026-09-07"))
    assert [row["value"] for row in result["rows"]] == [1, None, None]
    assert _warning(result)["details"]["measures"][0]["edge"] == "2026-09-07"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("case", ["after_window", "failed_read", "no_coverage"])
def test_unproven_coverage_keeps_the_existing_empty_window_warning(
    sparse_runtime, monkeypatch, case
):
    rt, _, _ = sparse_runtime
    query = _query(grain="", start="2026-09-01", end="2026-09-07")
    if case == "failed_read":
        query = _query(grain="")

        def fail(*args, **kwargs):
            raise RuntimeError("coverage unavailable")

        monkeypatch.setattr(runtime_module, "_compiled_cte_row", fail)
    elif case == "no_coverage":
        query["time"].update(grain="week")  # An unfilled single-leaf series has no coverage.
    result = rt.query(query)
    assert not any(row["code"] == "NO_DATA_YET" for row in result["warnings"])
    assert any(row["code"] == "EMPTY_RESULT_WINDOW" for row in result["warnings"])
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "_no_data_yet_warnings", lambda *a, **k: [])
        assert result["warnings"] == rt.query(query)["warnings"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_two_row_filtered_series_get_their_own_bucket_edges(sparse_runtime):
    rt, events, _ = sparse_runtime
    _mutate(rt, f"INSERT INTO {events} VALUES (2, 'late', TIMESTAMP '2026-09-23')")
    for caller, edge in [("early", "2026-09-07"), ("late", "2026-09-21")]:
        query = _query(start="2026-09-28", end="2026-10-05", caller=caller)
        query["time"].pop("fill")
        query["select"][0]["expression"].update(
            kind="aggregate",
            filter={
                "all": [
                    {
                        "field": "dimension.events_event_caller",
                        "op": "!=",
                        "value": "never",
                    }
                ]
            },
        )
        warning = _warning(rt.query(query))
        assert warning["details"]["measures"] == [
            {"id": MEASURE, "edge": edge, "edge_source": "last_bucket"}
        ]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("verbosity", ["minimal", "full"])
def test_a_metric_grant_never_exposes_its_ungranted_measure(sparse_runtime, verbosity):
    rt, _, _ = sparse_runtime
    query = _query(verbosity=verbosity)
    query["select"] = [{"expression": {"metric": METRIC}, "as": "value"}]
    query["policy_context"].update(metric_allowlist=[METRIC], dimension_allowlist=[ROLE])
    result = rt.query(query)
    assert result["ok"]
    assert MEASURE not in json.dumps(result)
