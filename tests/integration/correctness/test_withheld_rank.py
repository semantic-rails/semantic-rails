"""A rank by withheld values names the reference SQL's groups, in its order, without values.

Revenue by customer at stores a and b: 103 (23), 101 (17), then 102 and 106 tie at 9 for
third place, 104 (8), 108 (2) and 105 (0: its one order has no amount). Ties are ordered by
the customer in the rank's direction, so the third place goes to 106 and ascending is the
exact reverse of descending.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

from .test_correctness import REVENUE, STORE, _backend

CUSTOMER = "dimension.shop_order_customer_id"
POLICY = SemanticPolicyConfig(
    id="policy.test.rank_only",
    kind="object_access",
    object_ids=[REVENUE["measure"]],
    action="withhold_values",
    roles=["sales"],
)
REFERENCE = (
    "SELECT o.customer_id FROM orders AS o WHERE o.store_id IN ('a', 'b') "
    "GROUP BY o.customer_id ORDER BY COALESCE(SUM(o.amount), 0) {0}, o.customer_id {0} LIMIT {1}"
)


@contextmanager
def _withheld(runtime: Runtime) -> Iterator[Runtime]:
    runtime._config.semantic_policies.append(POLICY)
    try:
        yield runtime
    finally:
        runtime._config.semantic_policies.remove(POLICY)


def _rank(direction: str, limit: int) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": REVENUE, "as": "revenue"}],
        "group_by": [CUSTOMER],
        "where": [{"field": STORE, "op": "IN", "value": ["a", "b"]}],
        "order_by": [{"field": "revenue", "direction": direction}],
        "limit": limit,
        "policy_context": {"roles": ["sales"]},
    }


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_rank_by_withheld_values_matches_reference(request, backend_name):
    backend = _backend(request, backend_name)
    with _withheld(backend.runtimes["utc_authored"]) as runtime:
        answers = {}
        for direction in ("DESC", "ASC"):
            for limit in (3, 7):
                result = runtime.query(_rank(direction, limit))
                assert result["withheld"] == [REVENUE["measure"]]
                answers[direction, limit] = [row[CUSTOMER] for row in result["rows"]]
                assert all(set(row) == {CUSTOMER} for row in result["rows"])
                reference = backend.reference(REFERENCE.format(direction, limit))
                assert answers[direction, limit] == [row[0] for row in reference]
    assert answers["DESC", 3] == [103, 101, 106]
    assert answers["ASC", 7] == answers["DESC", 7][::-1]
