"""The fill contract counts period rows, including dated NULL values."""

from datetime import date, datetime

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime_parts.fill_gaps import enforce_fill_contract

ROLE = "temporal_role.order_time"
KEY = f"{ROLE}__week"
QUERY = {
    "time": {
        "temporal_role": ROLE,
        "grain": "week",
        "fill": True,
        "start": "2018-08-20",
        "end": "2018-09-03",
    }
}


@pytest.mark.parametrize(
    "keys,missing",
    [
        (["2018-08-27"], ["2018-08-20"]),
        ([date(2018, 8, 20)], ["2018-08-27"]),
        ([datetime(2018, 8, 20)], ["2018-08-27"]),
        (["2018-08-20T00:00:00Z"], ["2018-08-27"]),
        ([], ["2018-08-20", "2018-08-27"]),
    ],
)
def test_missing_periods(keys, missing):
    with pytest.raises(SemanticLayerError) as raised:
        enforce_fill_contract(
            QUERY, [{KEY: key, "value": None} for key in keys], truncated=False, zone="UTC"
        )
    assert raised.value.code == "FILL_INCOMPLETE"
    assert raised.value.details == {
        "missing_periods": missing,
        "calendar": "default",
        "hint": "extend the authored calendar through 2018-09-03",
    }


@pytest.mark.parametrize(
    "keys",
    [
        ["2018-08-20", "2018-08-27"],
        ["2018-08-20T00:00:00", "2018-08-27T00:00:00"],
        ["2018-08-20", "2018-08-27", "2018-08-27"],
    ],
)
def test_present_null_rows(keys):
    enforce_fill_contract(
        QUERY, [{KEY: key, "value": None} for key in keys], truncated=False, zone="UTC"
    )


@pytest.mark.parametrize(
    "keys,missing,returned",
    [
        (["2018-08-19"], ["2018-08-20", "2018-08-27"], 1),
        (["2018-08-19", "2018-08-26"], ["2018-08-20", "2018-08-27"], 2),  # Sunday weeks, short
        (["2018-08-19", "2018-08-26", "2018-09-02"], ["2018-08-20", "2018-08-27"], 3),
        ([None, "2018-08-27"], ["2018-08-20"], 1),
        ([None, "2018-08-20", "2018-08-27"], [], 2),
        (["2018-08-20", "2018-08-27", "2018-09-03"], [], 3),
        (["not a date", "2018-08-20", "2018-08-27"], [], 2),
    ],
)
def test_keys_other_than_the_computed_buckets_are_unverifiable(keys, missing, returned):
    with pytest.raises(SemanticLayerError) as raised:
        enforce_fill_contract(QUERY, [{KEY: key} for key in keys], truncated=False, zone="UTC")
    assert raised.value.code == "FILL_INCOMPLETE"
    assert raised.value.details["reason"] == "unverifiable"
    assert raised.value.details["missing_periods"] == missing
    assert raised.value.details["expected_periods"] == 2
    assert raised.value.details["returned_periods"] == returned


@pytest.mark.parametrize(
    "bounds,unverifiable",
    [
        ({"end": "2018-08-27T00:00:00.0000001"}, ["end"]),
        ({"end": "2018-08-27T00:00:00,1234567+00:00"}, ["end"]),
        ({"start": "2018-08-20T00:00:00.0000001"}, ["start"]),
        ({"start": "2018-W34-1T00:00:00.0000001"}, ["start"]),
        ({"start": "08/20/2018", "end": "next week"}, ["start", "end"]),
    ],
)
def test_bounds_that_do_not_parse_exactly_refuse(bounds, unverifiable):
    query = {"time": {**QUERY["time"], **bounds}}
    # Even rows covering every bucket the truncated bounds would compute never answer.
    rows = [{KEY: key} for key in ["2018-08-20", "2018-08-27"]]
    with pytest.raises(SemanticLayerError) as raised:
        enforce_fill_contract(query, rows, truncated=False, zone="UTC")
    assert raised.value.code == "FILL_INCOMPLETE"
    assert raised.value.details["reason"] == "unverifiable"
    assert raised.value.details["bounds"] == unverifiable


@pytest.mark.parametrize(
    "end,keys",
    [
        ("2018-08-27T00:00:00.000001", ["2018-08-20", "2018-08-27"]),
        ("2018-08-27T00:00:00.0000000", ["2018-08-20"]),
    ],
)
def test_six_fractional_digits_are_exact(end, keys):
    query = {"time": {**QUERY["time"], "end": end}}
    enforce_fill_contract(query, [{KEY: key} for key in keys], truncated=False, zone="UTC")
    with pytest.raises(SemanticLayerError):
        enforce_fill_contract(query, [{KEY: key} for key in keys[:-1]], truncated=False, zone="UTC")


@pytest.mark.parametrize(
    "patch,truncated",
    [
        ({"metric_filters": [{"op": ">", "value": 0}]}, False),
        ({"limit": 1}, False),
        ({"group_by": ["dimension.store"]}, False),
        ({}, True),
        ({"time": {**QUERY["time"], "fill": False}}, False),
        ({"time": {**QUERY["time"], "calendar_id": "fiscal"}}, False),
        ({"time": {**QUERY["time"], "end": None}}, False),
    ],
)
def test_row_removing_queries_are_exempt(patch, truncated):
    enforce_fill_contract({**QUERY, **patch}, [], truncated=truncated, zone="UTC")


@pytest.mark.parametrize(
    "grain,start,end,keys",
    [
        ("day", "2018-08-20T12:00:00", "2018-08-21T12:00:00", ["2018-08-20", "2018-08-21"]),
        ("month", "2018-01-15", "2018-03-01", ["2018-01-01", "2018-02-01"]),
        ("quarter", "2018-02-01", "2018-07-01", ["2018-01-01", "2018-04-01"]),
        ("year", "2018-02-01", "2020-01-01", ["2018-01-01", "2019-01-01"]),
    ],
)
def test_other_grains_and_partial_bounds(grain, start, end, keys):
    query = {"time": {**QUERY["time"], "grain": grain, "start": start, "end": end}}
    enforce_fill_contract(
        query, [{f"{ROLE}__{grain}": key} for key in keys], truncated=False, zone="UTC"
    )
    with pytest.raises(SemanticLayerError):
        enforce_fill_contract(query, [{f"{ROLE}__{grain}": keys[0]}], truncated=False, zone="UTC")


@pytest.mark.parametrize("end", ["2018-08-20T12:00:00", "2018-08-20T08:00:00"])
def test_an_empty_window_requires_no_periods(end):
    query = {"time": {**QUERY["time"], "start": "2018-08-20T12:00:00", "end": end}}
    enforce_fill_contract(query, [], truncated=False, zone="UTC")
    with pytest.raises(SemanticLayerError) as raised:
        enforce_fill_contract(query, [{KEY: "2018-08-20"}], truncated=False, zone="UTC")
    assert raised.value.details["reason"] == "unverifiable"


def test_aware_keys_use_the_role_zone():
    enforce_fill_contract(
        QUERY,
        [{KEY: "2018-08-20T07:00:00Z"}, {KEY: "2018-08-27T07:00:00Z"}],
        truncated=False,
        zone="America/Los_Angeles",
    )


@pytest.mark.parametrize("calendar", [None, "", "default", " DEFAULT "])
def test_default_calendar_aliases_are_checked(calendar):
    query = {"time": {**QUERY["time"], "calendar_id": calendar}}
    with pytest.raises(SemanticLayerError) as exc:
        enforce_fill_contract(query, [], truncated=False, zone="UTC")
    assert exc.value.code == "FILL_INCOMPLETE"
