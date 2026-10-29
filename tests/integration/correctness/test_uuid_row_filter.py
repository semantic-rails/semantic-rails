"""A row filter on a UUID column holds each owner to their own rows, on DuckDB and Postgres
alike, whatever form of the UUID the host holds, and refuses a value that isn't one before any
SQL runs.

Fixture ``vendors/``: 236 shipments, each owned by one of three owners by a UUID in the
warehouse's own UUID type (owners 1 and 2 hold 79, owner 0 holds 78). The package's policy
filters shipments by the caller's trusted ``owner_id`` (``type: uuid``) for the ``owner`` role.
Every answer is a count, checked against the rows read back in Python, so no UUID column is
read back on either warehouse.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError

from .conftest import _rows

SHIPMENT = "dimension.vendors_shipment_id"
SHIPMENTS = {"expression": "measure.vendors.shipment_count", "as": "n"}
COUNT = {"select": [SHIPMENTS]}


def _owner(value: Any) -> RequestContext:
    return RequestContext(roles=("owner",), attributes={"owner_id": value})


def _count(vendors, context: RequestContext, call: dict[str, Any] = COUNT) -> int:
    [row] = vendors.runtime.query(call, context=context)["rows"]
    return row["n"]


def _shipments(vendors) -> dict[str, list[str]]:
    """Each owner's shipment ids, as lower-case text: the reference."""
    owned: dict[str, list[str]] = {}
    sql = "SELECT CAST(owner_id AS VARCHAR), CAST(shipment_id AS VARCHAR) FROM shipments"
    for owner, shipment in _rows(vendors.runtime, sql):
        owned.setdefault(owner, []).append(shipment)
    return owned


def _forms(value: str) -> list[str]:
    """The forms of one UUID a host may hold, all of which name the same owner."""
    digits = value.replace("-", "")
    return [value, value.upper(), "{" + value + "}", digits, "{" + digits.upper() + "}"]


def test_each_owner_counts_only_their_own_shipments_in_any_form_of_their_id(vendors):
    owned = _shipments(vendors)
    assert sorted(len(ids) for ids in owned.values()) == [78, 79, 79]
    for owner, ids in owned.items():
        for form in _forms(owner):
            assert _count(vendors, _owner(form)) == len(ids), form
    # A caller without the role reads every shipment.
    assert _count(vendors, RequestContext()) == 236


def test_a_list_of_owners_counts_the_shipments_of_each(vendors):
    first, second, third = sorted(_shipments(vendors).items())
    assert _count(vendors, _owner([first[0], second[0].upper()])) == len(first[1]) + len(second[1])
    assert _count(vendors, _owner([third[0]])) == len(third[1])


def test_another_owners_shipment_reads_as_one_that_does_not_exist(vendors):
    (mine, own), (_, theirs), _ = sorted(_shipments(vendors).items())
    nobody = "00000000-0000-0000-0000-000000000000"
    assert nobody not in own + theirs

    def counted(shipment: str) -> int:
        where = [{"field": SHIPMENT, "op": "is", "value": shipment}]
        return _count(vendors, _owner(mine), {**COUNT, "where": where})

    assert counted(own[0]) == 1
    assert counted(theirs[0]) == counted(nobody) == 0
    unfiltered = vendors.runtime.query(
        {**COUNT, "where": [{"field": SHIPMENT, "op": "is", "value": theirs[0]}]}
    )
    assert unfiltered["rows"] == [{"n": 1}]


@pytest.mark.parametrize(
    "value",
    [
        "owner-1",
        "cfcd2084-95d5-65ef-66e7-dff9f98764d",  # one digit short
        "{cfcd2084-95d5-65ef-66e7-dff9f98764da",  # an unclosed brace
        " cfcd2084-95d5-65ef-66e7-dff9f98764da",
        7,
        ["cfcd2084-95d5-65ef-66e7-dff9f98764da", "owner-2"],
    ],
)
def test_a_value_that_is_not_a_uuid_is_refused_before_any_sql_runs(vendors, monkeypatch, value):
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the request must not reach the warehouse")

    monkeypatch.setattr(vendors.runtime._get_adapter(), "query_prepared", forbidden)  # noqa: SLF001
    context = _owner(value)
    [error] = vendors.runtime.validate(COUNT, context=context)["errors"]
    with pytest.raises(SemanticLayerError) as caught:
        vendors.runtime.query(COUNT, context=context)
    for code, message, details in (
        (error["code"], error["message"], error["details"]),
        (caught.value.code, str(caught.value), caught.value.details),
    ):
        assert code == "POLICY_DENIED"
        assert message == "Query parameters could not be bound safely for this request."
        assert details == {"reason": "attribute_type_mismatch", "attribute": "owner_id"}
    shown = json.dumps([error, str(caught.value), caught.value.details])
    for item in value if isinstance(value, list) else [value]:
        assert str(item).strip() not in shown
