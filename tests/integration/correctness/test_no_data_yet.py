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
# Every call is pinned to one instant, after the seeded September events.
NOW = "2026-10-05T06:00:00Z"


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
                        "supported_grains": ["hour", "day", "week", "month", "quarter", "year"],
                        "default": True,
                    }
                },
                "dimensions": {"caller": {"kind": "categorical"}},
                "measures": {
                    "amount": {
                        "kind": "aggregate",
                        "expr": "amount",
                        "accumulation": {"kind": "flow"},
                        "rollup": "additive",
                    },
                    "new_workspaces": {
                        "kind": "entity_count",
                        "entity_key": "event_id",
                        "accumulation": {"kind": "flow"},
                        "rollup": "additive",
                    },
                },
            }
        },
        "metrics.yml": {
            "metrics": {
                "new_workspaces": {
                    "kind": "aggregate",
                    "measure": "new_workspaces",
                    "aggregation": "count_distinct",
                    "label": "New Workspaces",
                    "description": "New Workspaces",
                    "value_type": "number",
                    "temporal_role": ROLE,
                }
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
        f"CREATE TABLE {events} (event_id INTEGER, caller VARCHAR, event_at TIMESTAMP, amount INTEGER);"
        f"CREATE TABLE {loads} AS SELECT d AS through_day FROM "
        "generate_series(TIMESTAMP '2026-09-01', TIMESTAMP '2026-09-30', INTERVAL '1 day') t(d);"
        f"INSERT INTO {events} VALUES (1, 'early', TIMESTAMP '2026-09-12', NULL);"
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


def _query(*, grain="week", start="2026-09-21", end="2026-09-28"):
    time = {"temporal_role": ROLE, "start": start, "end": end}
    if grain:
        time["grain"] = grain
    return {"select": [{"expression": MEASURE, "as": "value"}], "time": time}


def _caller(caller):
    return RequestContext(audience="caller", attributes={"caller": caller})


def _counting_callers_but(caller):
    """The measure over every caller but one: an aggregate with its own filter."""
    condition = {"field": "dimension.events_event_caller", "op": "not", "value": caller}
    return {"kind": "aggregate", "measure": MEASURE, "filter": {"all": [condition]}}


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
    query = _query(grain=grain)
    result = rt.query(query, verbosity=verbosity, now=NOW)
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
    # The named window answers its one week, its seven days, or its one total.
    assert result["row_count"] == (7 if grain == "day" else 1)
    assert all(row["value"] is None for row in result["rows"])
    assert result["window"]["data_through"] == "2026-09-12"
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "_no_data_yet_warnings", lambda *a, **k: [])
        next_main = len(prepared_calls)
        before = rt.query(query, verbosity=verbosity, now=NOW)
    assert prepared_calls[0] == prepared_calls[next_main]
    assert result["rows"] == before["rows"]
    if verbosity != "minimal":
        assert result["rendered_sql"] == before["rendered_sql"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain,days", [("week", [12, 13]), ("", [12, 13, 20])])
def test_moving_an_event_within_the_bucket_has_identical_warning(sparse_runtime, grain, days):
    """A series names the last bucket with data; a window total, whose bucket is the time
    before the window, names the last day with data, as its window's data_through does."""
    rt, events, _ = sparse_runtime
    warnings = []
    for day in days:
        _mutate(rt, f"UPDATE {events} SET event_at=TIMESTAMP '2026-09-{day}' WHERE caller='early'")
        result = rt.query(_query(grain=grain), now=NOW)
        assert result["window"]["data_through"] == f"2026-09-{day}"
        warning = _warning(result)
        if not grain:
            assert f"runs through 2026-09-{day}," in warning["message"]
            warning["message"] = warning["message"].replace(f"2026-09-{day}", "<day>")
        warnings.append(json.dumps(warning, sort_keys=True))
    assert len(set(warnings)) == 1


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["week", ""])
@pytest.mark.parametrize("caller", ["early", "late", "missing"])
def test_coverage_uses_only_the_callers_visible_rows(sparse_runtime, grain, caller):
    rt, events, _ = sparse_runtime
    _mutate(rt, f"INSERT INTO {events} VALUES (2, 'late', TIMESTAMP '2026-09-23', NULL)")
    query = _query(grain=grain)
    if grain:
        query["select"][0]["expression"] = _counting_callers_but("never")
    result = rt.query(query, context=_caller(caller), now=NOW)
    # The window's week (or total) follows the early caller's last row: no data yet for them.
    expected = caller in {"missing", "early"}
    assert any(row["code"] == "NO_DATA_YET" for row in result["warnings"]) is expected
    if expected:
        warning = _warning(result)
        if caller == "missing":
            assert warning["details"]["measures"] == [
                {"id": MEASURE, "edge": None, "edge_source": None}
            ]
            assert "2026-09-12" not in warning["message"] and "2026-09-23" not in warning["message"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_a_series_straddling_the_edge_warns_even_with_a_populated_bucket(sparse_runtime):
    rt, _, _ = sparse_runtime
    result = rt.query(_query(start="2026-09-07"), now=NOW)
    assert [row["value"] for row in result["rows"]] == [1, None, None]
    assert _warning(result)["details"]["measures"][0]["edge"] == "2026-09-07"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("case", ["before_data", "failed_read", "before_data_by_week"])
def test_unproven_coverage_never_says_no_data_yet(sparse_runtime, monkeypatch, case):
    """A window before the data, or one whose coverage can't be read, reads NULL with no
    data-through claim: the answer says no data is in scope, never "not yet"."""
    rt, _, _ = sparse_runtime
    query = _query(grain="", start="2026-09-01", end="2026-09-07")
    if case == "failed_read":
        query = _query(grain="")

        def fail(*args, **kwargs):
            raise RuntimeError("coverage unavailable")

        monkeypatch.setattr(runtime_module, "_compiled_cte_row", fail)
    elif case == "before_data_by_week":
        query["time"].update(grain="week")
    result = rt.query(query, now=NOW)
    assert [row["value"] for row in result["rows"]] == [None]
    codes = [row["code"] for row in result["warnings"]]
    assert "NO_DATA_YET" not in codes and "EMPTY_RESULT_WINDOW" not in codes
    assert "NO_DATA_IN_SCOPE" in codes
    assert ("data_through" in result["window"]) is (case != "failed_read")
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "_no_data_yet_warnings", lambda *a, **k: [])
        assert result["warnings"] == rt.query(query, now=NOW)["warnings"]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_a_row_filtered_week_after_the_callers_data_reads_no_data_yet(sparse_runtime):
    """Each caller's edge is its own last visible row: the week after it reads NULL."""
    rt, events, _ = sparse_runtime
    _mutate(rt, f"INSERT INTO {events} VALUES (2, 'late', TIMESTAMP '2026-09-23', NULL)")
    for caller, day, week in [("early", "12", "07"), ("late", "23", "21")]:
        query = _query(start="2026-09-28", end="2026-10-05")
        query["select"][0]["expression"] = _counting_callers_but("never")
        result = rt.query(query, context=_caller(caller), now=NOW)
        assert result["rows"] == [{"period": "2026-09-28", "period_partial": False, "value": None}]
        assert result["window"]["data_through"] == f"2026-09-{day}"
        assert _warning(result)["details"]["measures"] == [
            {"id": MEASURE, "edge": f"2026-09-{week}", "edge_source": "last_bucket"}
        ]


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize(
    "grain,start,end",
    [("week", "2024-09-22", "2024-09-23"), ("day", "2024-09-21T11:00:00", "2024-09-22")],
)
def test_series_disclosure_uses_bucket_precision(sparse_runtime, monkeypatch, grain, start, end):
    rt, events, _ = sparse_runtime
    _mutate(rt, f"UPDATE {events} SET event_at=TIMESTAMP '2024-09-21 10:30:00'")
    query = _query(grain=grain, start=start, end=end)
    query["select"][0]["expression"] = _counting_callers_but("early")
    result = rt.query(query, now=NOW)
    assert result["ok"]
    assert (
        _rows(
            rt,
            f"SELECT MAX(date_trunc('{grain}', event_at)) FROM {events} WHERE event_at <= TIMESTAMP '2024-09-21 10:59:00'",
        )[0][0].isoformat()
        == ({"week": "2024-09-16T00:00:00", "day": "2024-09-21T00:00:00"}[grain])
    )
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "_no_data_yet_warnings", lambda *a, **k: [])
        before = rt.query(query, now=NOW)
    assert result["rows"] == before["rows"]
    assert result["rendered_sql"] == before["rendered_sql"]
    assert not any(row["code"] == "NO_DATA_YET" for row in result["warnings"])
    assert result["warnings"] == before["warnings"]
    # The window's one bucket holds the last row, so no later bucket is claimed: the measure,
    # which counts no early event, simply has no data in scope.
    assert [row["value"] for row in result["rows"]] == [None]
    assert any(row["code"] == "NO_DATA_IN_SCOPE" for row in result["warnings"])


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("verbosity", ["minimal", "full"])
def test_a_metric_grant_never_exposes_its_ungranted_measure(sparse_runtime, verbosity):
    rt, _, _ = sparse_runtime
    query = _query()
    query["select"] = [{"expression": METRIC, "as": "value"}]
    grant = {"metric_allowlist": [METRIC], "dimension_allowlist": [ROLE]}
    result = rt.query(query, verbosity=verbosity, context=grant, now=NOW)
    assert result["ok"]
    assert MEASURE not in json.dumps(result)
