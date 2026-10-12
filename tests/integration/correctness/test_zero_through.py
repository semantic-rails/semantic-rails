"""A declared edge (``zero_through``) says what its answer's zeros and NULLs rest on.

The rows are frozen in ``shop/tests/answers.yml`` with independent reference SQL (the quiet
loaded week, the same week without a load log, weeks across the load log, a stray row after
it, an empty load log, an always-loaded table); these tests check what each answer says. A
small package of its own checks row filters, a load log a caller can't see, and a DATE mark
read in New York across the end of daylight saving time.
"""

from dataclasses import replace
from uuid import uuid4

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SeedSpec, SemanticPolicyConfig

from .answer_ledger import load_entries
from .conftest import _rows, _runtime
from .test_correctness import _backend

ENTRIES = dict(load_entries())
BACKENDS = ["duckdb", "postgres"]


def _answer(request, backend_name, case_id, **call):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    spec = ENTRIES[f"shop/{case_id}"]
    return runtime.query(spec["query"], now=spec["now"], **call)


def _warnings(result, code):
    return [row for row in result.get("warnings", []) if row["code"] == code]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_quiet_loaded_week_names_its_load_log(request, backend_name):
    result = _answer(request, backend_name, "a-quiet-week-the-load-log-covers-reads-zero")
    assert result["window"]["data_through"] == "2024-09-29"
    assert result["window"]["data_through_source"] == "ticket_loads.through_day"
    assert not _warnings(result, "NO_DATA_YET") and not _warnings(result, "PARTIAL_DATA")


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_without_a_load_log_the_last_row_is_the_edge_and_the_answer_says_how_to_declare_one(
    request, backend_name
):
    result = _answer(
        request, backend_name, "a-quiet-week-after-the-last-row-without-a-load-log-reads-null"
    )
    assert result["window"]["data_through"] == "2024-09-11"
    assert "data_through_source" not in result["window"]
    [warning] = _warnings(result, "NO_DATA_YET")
    assert warning["details"]["measures"] == [
        {"id": "measure.shop.tickets_by_last_row", "edge": "2024-09-11", "edge_source": "last_row"}
    ]
    assert warning["message"] == (
        "No data yet for measure.shop.tickets_by_last_row: its last row is dated 2024-09-11. "
        "Nothing says this data is loaded past it, so the window starting 2024-09-23 reads "
        "NULL, not 0. If the load ran and nothing happened, declare zero_through "
        '(PACKAGE_AUTHORING.md, "Empty periods").'
    )


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "case_id",
    [
        "weeks-read-zero-through-the-load-log-then-null",
        "a-stray-row-after-the-load-log-keeps-its-week",
    ],
)
def test_weeks_after_the_load_log_say_loaded_through(request, backend_name, case_id):
    result = _answer(request, backend_name, case_id)
    assert result["window"]["data_through"] == "2024-09-29"
    [warning] = _warnings(result, "NO_DATA_YET")
    measures = ENTRIES[f"shop/{case_id}"]["query"]["select"]
    assert warning["details"]["measures"] == [
        {"id": measure, "edge": "2024-09-29", "edge_source": "loaded"} for measure in measures
    ]
    assert warning["message"] == " ".join(
        f"No data yet for {measure}: its data is loaded through 2024-09-29, so later weeks "
        "read NULL, not 0."
        for measure in measures
    )


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_an_empty_load_log_says_its_date_is_unavailable(request, backend_name):
    result = _answer(request, backend_name, "a-quiet-week-under-an-empty-load-log-reads-null")
    assert "data_through" not in result["window"]
    [warning] = _warnings(result, "NO_DATA_YET")
    assert warning["details"]["measures"] == [
        {"id": "measure.shop.audited_tickets", "edge": None, "edge_source": "loaded"}
    ]
    assert "loaded-through date is unavailable" in warning["message"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_an_always_loaded_table_notes_its_zero_after_the_last_row(request, backend_name):
    result = _answer(request, backend_name, "a-quiet-week-of-an-always-loaded-table-reads-zero")
    assert result["window"]["data_through"] == "2024-09-30"
    assert result["window"]["data_through_source"] == "now"
    assert _warnings(result, "ZERO_AFTER_LAST_ROW") == [
        {
            "code": "ZERO_AFTER_LAST_ROW",
            "message": "measure.shop.tickets_always_loaded reads 0 after its last row "
            "(2024-09-11) because the package says this table is always loaded; a stalled "
            "load would also read 0.",
            "severity": "info",
            "stage": "execution",
            "details": {
                "measures": [{"id": "measure.shop.tickets_always_loaded", "last_row": "2024-09-11"}]
            },
            "object_ids": ["measure.shop.tickets_always_loaded"],
        }
    ]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_last_nights_load_covers_the_month_so_far_but_not_today(request, backend_name):
    """A load through yesterday leaves only today's unfinished part unloaded: the month so far
    reads 0 and says it is so far, today's own day reads NULL, and a load a whole day older
    keeps the month NULL."""
    so_far = _answer(
        request, backend_name, "a-quiet-month-so-far-after-last-nights-load-reads-zero"
    )
    assert so_far["window"]["data_through"] == "2024-11-14"
    assert so_far["window"]["data_through_source"] == "ticket_exports.through_day"
    assert not so_far.get("warnings")
    assert [row["code"] for row in so_far["assumptions"]] == ["WINDOW_TOTAL", "PARTIAL_PERIOD"]
    stalled = _answer(request, backend_name, "a-quiet-month-so-far-after-a-stalled-load-reads-null")
    [warning] = _warnings(stalled, "NO_DATA_YET")
    assert warning["message"] == (
        "No data yet for measure.shop.exported_tickets: its data is loaded through 2024-11-14, "
        "so this window's total reads NULL, not 0."
    )
    assert _warnings(stalled, "PARTIAL_DATA")[0]["details"] == {
        "through": "2024-11-14",
        "days_covered": 14,
        "days": 16,
    }
    week = _answer(
        request, backend_name, "today-in-a-week-by-day-after-last-nights-load-reads-null"
    )
    [warning] = _warnings(week, "NO_DATA_YET")
    assert warning["details"]["measures"] == [
        {"id": "measure.shop.exported_tickets", "edge": "2024-11-14", "edge_source": "loaded"}
    ]
    assert not _warnings(week, "PARTIAL_DATA")


def test_a_caller_who_cant_see_the_load_log_reads_loaded(packages):
    base = _runtime(packages["utc_authored"])
    config, source_path = base.config, base.source_path
    base.close()
    hidden = SemanticPolicyConfig(
        id="policy.shop.load_log",
        kind="object_visibility",
        action="hidden",
        object_ids=["entity.shop_ticket_load"],
        roles=["support"],
    )
    config = replace(config, semantic_policies=[*config.semantic_policies, hidden])
    runtime = Runtime.from_config(config, source_path=source_path)
    try:
        spec = ENTRIES["shop/weeks-read-zero-through-the-load-log-then-null"]
        support = runtime.query(
            spec["query"], now=spec["now"], context=RequestContext(roles=("support",))
        )
        other = runtime.query(spec["query"], now=spec["now"])
    finally:
        runtime.close()
    assert support["rows"] == other["rows"]
    assert support["window"]["data_through_source"] == "loaded"
    assert other["window"]["data_through_source"] == "ticket_loads.through_day"
    assert "ticket_loads" not in str(support["window"]) + str(support.get("warnings"))


# A package of its own: events read under a caller row filter, and a load log.
NOW = "2024-11-05T12:00:00Z"


@pytest.fixture
def logged(request, backend_name, tmp_path):
    """Events on Oct 30 (one per caller) and a load log through Nov 2, read on a clock in
    New York; ``request.param`` names the field the events model declares as its edge."""
    backend = _backend(request, backend_name)
    events, loads = f"events_{uuid4().hex}", f"loads_{uuid4().hex}"
    documents = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "logged",
                "namespace": "logged",
                "warehouse": "duckdb",
                "default_db": "logged.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "event": {"key": ["event_id"], "model": "events"},
                    "load": {"key": ["run_id"], "model": "loads"},
                }
            }
        },
        "models/events.yml": {
            "model": {
                "id": "events",
                "relation": events,
                "entities": {"event": {}},
                "zero_through": {"loaded": f"loads.{request.param}"},
                "times": {
                    "event_at": {
                        "column": "event_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "timezone": "America/New_York",
                        "column_timezone": "UTC",
                        "default": True,
                    }
                },
                "dimensions": {"caller": {"kind": "categorical"}},
                "measures": {
                    "event_count": {
                        "kind": "entity_count",
                        "entity_key": "event_id",
                        "accumulation": {"kind": "flow"},
                    }
                },
            }
        },
        "models/loads.yml": {
            "model": {
                "id": "loads",
                "relation": loads,
                "entities": {"load": {}},
                "times": {
                    "through_day": {"column": "through_day", "kind": "date"},
                    "finished_at": {"column": "finished_at", "kind": "timestamp"},
                },
                "dimensions": {"team": {"kind": "categorical"}},
            }
        },
        "policies.yml": {
            "semantic_policies": [
                {
                    "id": "policy.logged.caller",
                    "kind": "row_filter",
                    "audiences": ["caller"],
                    "dimension": "dimension.logged_event_caller",
                    "attribute": "caller",
                },
                {
                    "id": "policy.logged.team",
                    "kind": "row_filter",
                    "audiences": ["auditor"],
                    "dimension": "dimension.logged_load_team",
                    "attribute": "team",
                },
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
        package = replace(
            config.package,
            warehouse="postgres",
            default_db="",
            seed=SeedSpec(),
            connection=original.connection,
        )
        config = replace(config, package=package)
    # Nov 2 ends at 04:00 UTC on Nov 3 in New York (still daylight time); the finished_at
    # mark is that instant. Daylight time ends at 06:00 UTC on Nov 3.
    seed = (
        f"CREATE TABLE {events} (event_id INTEGER, caller VARCHAR, event_at TIMESTAMP);"
        f"CREATE TABLE {loads} (run_id INTEGER, through_day DATE, finished_at TIMESTAMP, "
        "team VARCHAR);"
        f"INSERT INTO {events} VALUES (1, 'early', TIMESTAMP '2024-10-30 12:00:00'), "
        "(2, 'late', TIMESTAMP '2024-10-30 13:00:00');"
        f"INSERT INTO {loads} VALUES (1, DATE '2024-11-02', TIMESTAMP '2024-11-03 04:00:00', 'a');"
    )
    if backend_name == "duckdb":
        with duckdb.connect(str(tmp_path / "logged.duckdb")) as conn:
            conn.execute(seed)
    runtime = Runtime.from_config(config, source_path=str(tmp_path))
    if backend_name == "postgres":
        _rows(runtime, seed)
    try:
        yield runtime
    finally:
        if backend_name == "postgres":
            _rows(runtime, f"DROP TABLE {events}; DROP TABLE {loads}")
        runtime.close()


DAYS = {"select": ["measure.logged.event_count"], "time": {"start": "2024-11-01", "end": "2024-11-05", "grain": "day"}}  # fmt: skip


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("logged", ["through_day", "finished_at"], indirect=True)
def test_the_mark_ends_at_the_next_local_midnight_across_daylight_time(logged):
    """A DATE mark is loaded through the end of its day in the clock's zone, and a timestamp
    mark at its instant: both end November 2 in New York, the night daylight time ends."""
    result = logged.query(DAYS, now=NOW)
    assert [(row["period"], row["event_count"]) for row in result["rows"]] == [
        ("2024-11-01", 0),
        ("2024-11-02", 0),
        ("2024-11-03", None),
        ("2024-11-04", None),
    ]
    assert result["window"]["data_through"] == "2024-11-02"


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("logged", ["through_day"], indirect=True)
def test_a_row_filtered_caller_reads_the_load_log_whole(logged):
    """The load log says how far the whole load ran: a caller's row filter limits the events
    it counts, never that date."""
    caller = RequestContext(audience="caller", attributes={"caller": "early"})
    result = logged.query({**DAYS, "time": {"start": "2024-10-28", "end": "2024-11-05"}}, now=NOW, context=caller)  # fmt: skip
    assert result["rows"] == [{"event_count": 1}]
    result = logged.query(DAYS, now=NOW, context=caller)
    assert [row["event_count"] for row in result["rows"]] == [0, 0, None, None]
    assert result["window"]["data_through"] == "2024-11-02"
    assert "data_through_source" in result["window"]


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("logged", ["through_day"], indirect=True)
def test_a_load_log_under_a_row_filter_is_refused_never_read_unfiltered(logged):
    auditor = RequestContext(audience="auditor", attributes={"team": "a"})
    with pytest.raises(SemanticLayerError) as raised:
        logged.query(DAYS, now=NOW, context=auditor)
    assert raised.value.code == "POLICY_DENIED"
    assert raised.value.details["reason"] == "row_filter_on_load_log"
