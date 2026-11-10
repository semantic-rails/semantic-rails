"""Package-authored caveats: which queries they surface on, that they never change an answer,
that a caveat's so_what (what the reader should do) reaches its warning, and that a package test
(``caveat_applies``) pins whether a call states a caveat."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import semantic_payload
from semantic_rails.package_tools import (
    diff_package_report,
    package_manifest,
    run_package_tests_report,
)
from semantic_rails.runtime import Runtime
from semantic_rails.visible_view import build_view
from tests.semantic_rails.conftest import copy_package_config, opened

ORDER_TIME = "temporal_role.jaffle_order_time"
REVENUE = "measure.jaffle.revenue_usd"
STORE_NAME = "dimension.jaffle_store_name"


def _write_caveats(package_dir: Path, rows: list[dict]) -> None:
    (package_dir / "caveats.yml").write_text(
        yaml.safe_dump({"semantic_caveats": rows}, sort_keys=False),
        encoding="utf-8",
    )


def _revenue_query(*, time: dict | None = None, prior: int = 0, end: str = "", **extra) -> dict:
    """Revenue for March 2017 by month, or over ``time``, or beside its value ``prior`` months
    earlier on a monthly series ending at ``end``."""
    select = [{"expression": REVENUE, "as": "revenue"}]
    if prior:
        offset = {"unit": "month", "value": prior}
        select.append(
            {
                "expression": {"kind": "prior_period", "input": REVENUE, "offset": offset},
                "as": "prior",
            }
        )
        time = {"temporal_role": ORDER_TIME, "grain": "month", "end": end}
    if time is None:
        time = {
            "temporal_role": ORDER_TIME,
            "grain": "month",
            "start": "2017-03-01",
            "end": "2017-04-01",
        }
    return {"select": select, **({"time": time} if time else {}), **extra}


def _revenue_caveat(**overrides) -> dict:
    row = {
        "id": "caveat.test.revenue_march",
        "kind": "business_event",
        "message": "March pricing event affects revenue interpretation.",
        "object_ids": [REVENUE],
        "time": {"from": "2017-03-01", "to": "2017-04-01"},
        "severity": "warning",
    }
    row.update(overrides)
    return row


# Caveats match one by one, so one package holds them all and each check names its caveat.
REVENUE_MARCH = [f"caveat.test.revenue_march_{index}" for index in range(6)]
POINT = "caveat.test.point"
BROOKLYN = "caveat.test.brooklyn"
INTERNAL = "caveat.test.internal_production"
CAVEATS = [
    *(_revenue_caveat(id=caveat_id) for caveat_id in REVENUE_MARCH),
    _revenue_caveat(id=POINT, time={"at": "2017-03-01"}),
    {
        "id": BROOKLYN,
        "kind": "business_event",
        "message": "Brooklyn had a partial operating period.",
        "object_ids": [STORE_NAME],
        "entity_values": [
            {"entity": "entity.jaffle_store", "dimension": STORE_NAME, "value": "Brooklyn"}
        ],
        "time": {"from": "2017-03-01", "to": "2017-04-01"},
    },
    _revenue_caveat(id=INTERNAL, audiences=["internal"], environments=["production"]),
]
CAVEAT_IDS = [row["id"] for row in CAVEATS]
FIRST = REVENUE_MARCH[0]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """jaffle_shop with CAVEATS in place of its own, shared: every call here only reads."""
    package_dir = copy_package_config(
        tmp_path_factory.mktemp("caveats"), "jaffle_shop", preseed_db=True
    )
    _write_caveats(package_dir, CAVEATS)
    rt = Runtime.from_path(str(package_dir))
    try:
        yield opened(rt)
    finally:
        rt.close()


def _applied(payload: dict) -> list[dict]:
    return [
        row
        for row in payload.get("warnings", []) or []
        if row.get("code") == "SEMANTIC_CAVEAT_APPLIED"
    ]


def _rounded_rows(rows: list[dict]) -> list[dict]:
    return [
        {key: round(value, 6) if isinstance(value, float) else value for key, value in row.items()}
        for row in rows
    ]


def test_jaffle_caveat_fixture_loads() -> None:
    config = load_package_config("configs/semantic_rails/jaffle_shop")

    assert len(config.semantic_caveats) == 1
    caveat = config.semantic_caveats[0]
    assert caveat.kind == "business_event"
    assert caveat.entity_values[0]["value"] == "Brooklyn"


def test_invalid_caveat_kind_time_and_object_refs_fail(tmp_path: Path) -> None:
    package_dir = copy_package_config(tmp_path, "jaffle_shop")
    _write_caveats(package_dir, [_revenue_caveat(kind="pricing_change")])
    with pytest.raises(SemanticLayerError, match="kind must be one of"):
        load_package_config(str(package_dir))

    _write_caveats(package_dir, [_revenue_caveat(time={"at": "2017-03-01", "from": "2017-03-01"})])
    with pytest.raises(SemanticLayerError, match="either at or from/to"):
        load_package_config(str(package_dir))

    _write_caveats(package_dir, [_revenue_caveat(object_ids=["measure.jaffle.nope"])])
    with pytest.raises(SemanticLayerError, match="unknown object_id"):
        load_package_config(str(package_dir))


def _case(name, *, present=(), absent=(), call="validate", context=None, verbosity=None, **query):
    """One call, named by the situation, its call and its verbosity."""
    exact = verbosity is not None  # every matching caveat, and nothing else, exactly once
    case = (call, _revenue_query(**query), context, verbosity, set(present), set(absent), exact)
    return pytest.param(*case, id="-".join(filter(None, [name, call, verbosity])))


INTERNAL_PRODUCTION = {"audience": "internal", "environment": "production"}
CASES = [
    # A relevant caveat surfaces on validate, compile and query; at every verbosity every
    # matching caveat is stated once, the point inside the window too.
    *(_case("relevant", call=call, present=[FIRST]) for call in ("compile", "query")),
    *(
        _case("relevant", verbosity=verbosity, present=[*REVENUE_MARCH, POINT])
        for verbosity in ("minimal", "compact", "full")
    ),
    # A time-bound caveat needs a query time; a broad scalar window outweighs a short caveat.
    _case("time-bound-without-time", time={}, absent=CAVEAT_IDS),
    _case(
        "time-bound-broad-window",
        time={"temporal_role": ORDER_TIME, "start": "2017-01-01", "end": "2017-12-31"},
        absent=[FIRST],
    ),
    # The range is half-open at both ends.
    *(
        _case(
            f"half-open-{edge}",
            time={"temporal_role": ORDER_TIME, "start": start, "end": end},
            absent=[FIRST],
        )
        for edge, start, end in [
            ("before", "2017-02-01", "2017-03-01"),
            ("after", "2017-04-01", "2017-05-01"),
        ]
    ),
    # An entity-value caveat needs its value in play: grouped or filtered, not every revenue.
    _case("entity-value-ungrouped", absent=[BROOKLYN]),
    _case("entity-value-grouped", group_by=[STORE_NAME], present=[BROOKLYN]),
    *(
        _case(
            f"entity-value-filtered-{store.lower()}",
            where=[{"field": STORE_NAME, "op": "is", "value": store}],
            **{key: [BROOKLYN]},
        )
        for store, key in [("Brooklyn", "present"), ("Philadelphia", "absent")]
    ),
    _case("scope-without-context", absent=[INTERNAL]),
    _case(
        "scope-external", context={**INTERNAL_PRODUCTION, "audience": "external"}, absent=[INTERNAL]
    ),
    _case("scope-internal-production", context=INTERNAL_PRODUCTION, present=[INTERNAL]),
    # A comparison whose periods cross the point, or bookend the range, gets the caveat; a
    # longer series whose compared periods stay on one side of the range does not.
    _case("comparison-crosses-point", prior=1, end="2017-04-01", present=[POINT]),
    _case("comparison-bookends-range", prior=2, end="2017-05-01", present=[FIRST]),
    _case("comparison-one-side", prior=1, end="2017-07-01", absent=[FIRST]),
]


@pytest.mark.parametrize(
    ("call", "query", "context", "verbosity", "present", "absent", "exact"), CASES
)
def test_caveats_surface_where_they_apply(
    runtime: Runtime, call, query, context, verbosity, present, absent, exact
) -> None:
    kwargs = {"verbosity": verbosity} if verbosity else {}
    payload = getattr(runtime, call)(query, **({"context": context} if context else {}), **kwargs)
    applied = _applied(payload)
    ids = Counter(row["details"]["caveat_id"] for row in applied)
    # No caveat is stated twice.
    assert all(count == 1 for count in ids.values()), ids
    if exact:
        assert ids == Counter(present)
    assert present <= set(ids)
    assert not absent & set(ids)
    if FIRST in present:
        (first,) = [row for row in applied if row["details"]["caveat_id"] == FIRST]
        assert first["details"]["kind"] == "business_event"


def test_caveats_do_not_change_sql_or_rows(runtime: Runtime, tmp_path: Path) -> None:
    without_caveat_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    (without_caveat_dir / "caveats.yml").unlink()
    without_caveat = Runtime.from_path(str(without_caveat_dir))
    try:
        query, now = _revenue_query(), "2026-10-01T00:00:00Z"
        assert (
            runtime.compile(query, now=now)["rendered_sql"]
            == without_caveat.compile(query, now=now)["rendered_sql"]
        )
        assert _rounded_rows(runtime.query(query, now=now)["rows"]) == _rounded_rows(
            without_caveat.query(query, now=now)["rows"]
        )
    finally:
        without_caveat.close()


def test_manifest_counts_and_diff_tracks_caveats_as_metadata_only(tmp_path: Path) -> None:
    base_dir = copy_package_config(tmp_path, "jaffle_shop")
    changed_dir = copy_package_config(tmp_path, "jaffle_shop")
    _write_caveats(
        changed_dir,
        [
            _revenue_caveat(),
            _revenue_caveat(id="caveat.test.second_row", message="Second advisory row."),
        ],
    )

    manifest = package_manifest(resolve_package_reference(path=str(changed_dir)))
    diff = diff_package_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )

    assert manifest["summary"]["semantic_caveats"] == 2
    caveat_changes = [row for row in diff["changes"] if row["kind"] == "caveats"]
    assert {(row["object_id"], row["change_type"]) for row in caveat_changes} == {
        ("caveat.jaffle.brooklyn_soft_launch_mar_2017", "removed"),
        ("caveat.test.revenue_march", "added"),
        ("caveat.test.second_row", "added"),
    }
    assert all(row["behavior_change"] is False for row in caveat_changes)
    assert diff["summary"]["behavior_changes"] == 0


# so_what, and caveat_applies package tests.
MARCH = {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "start": "2017-03-01",
    "end": "2017-04-01",
}
SAID = "caveat.test.with_so_what"
UNSAID = "caveat.test.without_so_what"
SO_WHAT = "Compare March revenue with list prices restored, or name the event."
SO_WHAT_CAVEATS = [
    {
        "id": SAID,
        "kind": "business_event",
        "message": "A March pricing event lowered revenue.",
        "so_what": f"  {SO_WHAT}  ",
        "owner": "pricing_team",
        "object_ids": [REVENUE],
        "time": {"from": "2017-03-01", "to": "2017-04-01"},
    },
    {
        "id": UNSAID,
        "kind": "data_quality",
        "message": "Store names were backfilled.",
        "object_ids": [STORE_NAME],
        "severity": "info",
    },
]


@pytest.fixture(scope="module")
def so_what_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    package = copy_package_config(
        tmp_path_factory.mktemp("so_what"), "jaffle_shop", preseed_db=True
    )
    _write_caveats(package, SO_WHAT_CAVEATS)
    return package


@pytest.fixture(scope="module")
def so_what_runtime(so_what_package: Path) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(so_what_package))
    try:
        yield opened(rt)
    finally:
        rt.close()


def _stated(runtime: Runtime, verbosity: str | None) -> dict[str, dict]:
    query = {"select": [REVENUE], "group_by": [STORE_NAME], "time": MARCH}
    payload = runtime.validate(query, **({"verbosity": verbosity} if verbosity else {}))
    return {
        row["details"]["caveat_id"]: row["details"]
        for row in payload["warnings"]
        if row["code"] == "SEMANTIC_CAVEAT_APPLIED"
    }


def test_so_what_loads_stripped(so_what_package: Path) -> None:
    config = load_package_config(str(so_what_package))
    by_id = {row.id: row for row in config.semantic_caveats}
    assert by_id[SAID].so_what == SO_WHAT
    assert by_id[UNSAID].so_what == ""


@pytest.mark.parametrize("verbosity", [None, "compact", "full"])
def test_warning_carries_so_what_and_owner_by_default(so_what_runtime: Runtime, verbosity) -> None:
    stated = _stated(so_what_runtime, verbosity)
    assert stated[SAID]["so_what"] == SO_WHAT
    assert stated[SAID]["owner"] == "pricing_team"


def test_compact_leaves_out_what_the_caveat_does_not_say(so_what_runtime: Runtime) -> None:
    stated = _stated(so_what_runtime, "compact")
    assert "so_what" not in stated[UNSAID]
    assert "owner" not in stated[UNSAID]


def test_minimal_keeps_its_ids_only(so_what_runtime: Runtime) -> None:
    stated = _stated(so_what_runtime, "minimal")
    assert "so_what" not in stated[SAID]
    assert "owner" not in stated[SAID]


def test_a_non_text_so_what_is_refused(tmp_path: Path) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop")
    _write_caveats(package, [{**SO_WHAT_CAVEATS[0], "so_what": ["compare", "later"]}])
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    assert raised.value.code == "INVALID_CONFIG"
    assert f"caveat '{SAID}' so_what must be a string (what the reader should do)" in str(
        raised.value
    )


def test_an_unauthored_so_what_keeps_the_package_fingerprint(so_what_package: Path) -> None:
    caveats = semantic_payload(load_package_config(str(so_what_package)))["semantic_caveats"]
    by_id = {row["id"]: row for row in caveats}
    assert "so_what" not in by_id[UNSAID]
    assert by_id[SAID]["so_what"] == SO_WHAT


# caveat_applies package tests: each entry, and the code and message its author sees.
def _applies_test(caveat: str | None = SAID, *, query: dict | None = None, **extra) -> dict:
    spec = {
        "kind": "caveat_applies",
        "query": query or {"select": [REVENUE], "time": MARCH},
        **({"caveat": caveat} if caveat is not None else {}),
    }
    return {**spec, **extra}


APRIL = {**MARCH, "start": "2017-04-01", "end": "2017-05-01"}
APPLIES_TESTS = {
    "applies": _applies_test(),
    "does_not_apply": _applies_test(query={"select": [REVENUE], "time": APRIL}, applies=False),
    "expected_but_absent": _applies_test(query={"select": [REVENUE], "time": APRIL}),
    "unexpected_but_stated": _applies_test(applies=False),
    "no_caveat": _applies_test(None),
    "unknown_caveat": _applies_test("caveat.test.nope"),
    "applies_not_boolean": _applies_test(applies="no"),
    "refused_query": _applies_test(
        query={"select": ["measure.jaffle.nope"], "time": MARCH}, applies=False
    ),
}


@pytest.fixture(scope="module")
def applies_results(so_what_package: Path) -> dict[str, dict]:
    (so_what_package / "tests" / "caveat_applies.yml").write_text(
        yaml.safe_dump({"tests": APPLIES_TESTS}), encoding="utf-8"
    )
    report = run_package_tests_report(resolve_package_reference(path=str(so_what_package)))
    return {row["id"]: row for row in report["tests"] if row["id"] in APPLIES_TESTS}


@pytest.mark.parametrize("test_id", ["applies", "does_not_apply"])
def test_caveat_applies_passes(applies_results: dict[str, dict], test_id: str) -> None:
    assert applies_results[test_id]["ok"] is True, applies_results[test_id]


@pytest.mark.parametrize(
    ("test_id", "code", "message"),
    [
        (
            "expected_but_absent",
            "CAVEAT_APPLIES_MISMATCH",
            f"caveat '{SAID}' did not apply to the query",
        ),
        (
            "unexpected_but_stated",
            "CAVEAT_APPLIES_MISMATCH",
            f"caveat '{SAID}' applied to the query, which the test says it must not",
        ),
        (
            "no_caveat",
            "INVALID_CONFIG",
            "a caveat_applies test must name the caveat it checks in caveat",
        ),
        (
            "unknown_caveat",
            "INVALID_CONFIG",
            "caveat_applies names caveat 'caveat.test.nope', which the package does not declare",
        ),
        (
            "applies_not_boolean",
            "INVALID_CONFIG",
            "a caveat_applies test's applies must be true or false",
        ),
    ],
)
def test_caveat_applies_fails_with_what_the_author_sees(
    applies_results: dict[str, dict], test_id: str, code: str, message: str
) -> None:
    error = applies_results[test_id]["error"]
    assert applies_results[test_id]["ok"] is False
    assert (error["code"], error["message"]) == (code, message)


def test_caveat_applies_lists_what_was_stated(applies_results: dict[str, dict]) -> None:
    details = applies_results["unexpected_but_stated"]["error"]["details"]
    assert details["applied_caveats"] == [SAID]
    assert details["expected_applies"] is False
    assert applies_results["unknown_caveat"]["error"]["details"]["known_caveats"] == sorted(
        [SAID, UNSAID]
    )


def test_a_refused_query_fails_even_the_negative_form(applies_results: dict[str, dict]) -> None:
    error = applies_results["refused_query"]["error"]
    assert applies_results["refused_query"]["ok"] is False
    assert error["code"] == "OBJECT_NOT_FOUND"
    assert "measure.jaffle.nope" in error["message"]
    assert error["details"]["caveat"] == SAID


def test_a_so_what_naming_a_hidden_object_hides_its_caveat(so_what_package: Path) -> None:
    hidden = "measure.jaffle.order_count"
    config = load_package_config(str(so_what_package))
    named = [
        replace(row, so_what=f"Read {hidden} instead.") if row.id == SAID else row
        for row in config.semantic_caveats
    ]
    view = build_view(replace(config, semantic_caveats=named), frozenset({hidden}))
    assert [row.id for row in view.semantic_caveats] == [UNSAID]
    # The same caveat is shown while nothing it says is hidden.
    shown = build_view(config, frozenset({hidden}))
    assert {row.id for row in shown.semantic_caveats} == {SAID, UNSAID}
