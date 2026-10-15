"""Discover flags dimension candidates on a different entity than the one the terms imply.

``revenue by store last 90 days`` once ranked ``dimension.jaffle_inventory_date_day`` first
because "store" and "day" both matched its fields, with no sign that it lives on a different
entity than the revenue measures. Discover infers the terms' entity from the top three
measure/metric candidates (when at least two agree) and gives each dimension on another entity
a structured ``cross-entity ...`` match reason (and a small tiebreak penalty). When the top
candidates disagree, no entity is inferred and nothing is flagged.
"""

from __future__ import annotations

import pytest

from semantic_rails.metadata import discover_payload
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_snapshot(load_package_snapshot(path))
    yield opened(runtime)
    runtime.close()


@pytest.mark.parametrize(
    ("terms", "flagged"),
    [
        ("revenue by store last 90 days", {"dimension.jaffle_inventory_date_day"}),
        # The top measures span several entities, so no entity is inferred.
        ("top 3 stores by revenue in the most recent full month", set()),
    ],
)
def test_cross_entity_reason_follows_the_inferred_entity(runtime, terms, flagged) -> None:
    dimensions = discover_payload(runtime, terms=terms, limit=10)["dimensions"]
    assert dimensions
    marked = {
        row["id"]
        for row in dimensions
        if any("cross-entity" in str(reason) for reason in row.get("match_reasons") or [])
    }
    assert flagged <= marked if flagged else not marked
