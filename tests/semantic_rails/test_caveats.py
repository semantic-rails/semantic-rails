"""Package-authored caveats: which queries they surface on, and that they never change an answer."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import diff_package_report, package_manifest
from semantic_rails.runtime import Runtime
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
