"""The lookup default: of two or more routes, the one that reaches one row at every hop answers.

Which route a question means is a business definition, so rungs 1-4 of the route ladder answer
only by a recorded row, the start's own key or the only route (``test_route_resolution.py``).
Rung 5 answers a pair those rungs refuse when exactly one of its routes is a lookup (every hop
reaches one row: a to-one hop, or a hop into a validity window of a relationship with a "1"
side, never a many-to-many one), or when, of several lookups,
exactly one follows every entity's own keys. A route reading "any of" rows is never a default.
It answers only pairs rungs 1-4 refuse, so no answer moves; each answer it gives names the
reading and the routes it was chosen over, never running them.

Answer keys for each default shape:
- a lookup over an "any of" reading: ``test_route_clarification.py``
  (``test_a_lookup_answers_over_a_shorter_fan_out_and_the_fan_out_is_one_switch_away``);
- the own keys among two lookups: ``test_route_resolution.py``
  (``test_adding_a_route_never_changes_an_answer_silently``) and ``test_route_census.py``;
- a lookup through a validity window, and the bundled package's defaults: here;
- a row filter holds a default its routes carry: ``test_row_filters_through_joins.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import eligible_path_targets, package_route, resolve_route
from semantic_rails.runtime import Runtime
from semantic_rails.visible_view import build_view
from tests.semantic_rails.campus_package import write_campus_package
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import CALLER, visibility_policy, with_policies
from tests.semantic_rails.test_route_resolution import (
    ACCOUNT,
    INVOICE,
    INVOICE_BRANCH,
    MEMBERSHIP,
    PRIMARY,
    REGION,
    ROOT,
    TWO_KEYS,
    _write_package,
)

JAFFLE = ROOT / "configs/semantic_rails/jaffle_shop"
SESSION, HISTORY = "entity.jaffle_storefront_session", "entity.jaffle_customer_history"
SESSION_ORDER = [
    "relationship.storefront_sessions_order",
    "relationship.jaffle_order_customer_history",
]
SESSIONS_BY_SEGMENT = {
    "select": [{"expression": "measure.jaffle.session_starts", "as": "sessions"}],
    "group_by": ["dimension.jaffle_customer_history_segment"],
    "time": {"grain": "month"},
}
# Sessions by the segment valid at each session's start: through the session's own order.
BY_SESSION_ORDER = """
SELECT h.customer_segment, DATE_TRUNC('month', s.started_at), COUNT(DISTINCT s.session_id)
FROM jaffle_storefront_session s
LEFT JOIN jaffle_order o ON o.order_id = s.converted_order_id
LEFT JOIN jaffle_customer_history h ON h.customer_id = o.customer_id
  AND h.valid_from <= s.started_at AND (h.valid_to > s.started_at OR h.valid_to IS NULL)
GROUP BY 1, 2
"""


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _sql(runtime: Runtime, sql: str) -> list[tuple]:
    adapter = runtime._get_adapter()  # noqa: SLF001 - the reference shares the connection
    rows = adapter._db.conn.execute(sql).fetchall()  # noqa: SLF001
    return sorted((tuple(row) for row in rows), key=repr)


def _answer(out: dict) -> list[tuple]:
    return sorted(
        (
            (row["dimension.jaffle_customer_history_segment"], row["period"], row["sessions"])
            for row in out["rows"]
        ),
        key=repr,
    )


def test_a_lookup_through_a_validity_window_answers_and_names_its_reading():
    """A session's customer segment: the history row valid at the time, of the session's own
    order (one row at every hop), or of any of its customer's orders. The lookup answers, at
    every verbosity naming its reading and the route it was chosen over, and the route is the
    package's, the same with or without a time window. (On this data the two readings happen
    to count alike; the bank fixture's answer keys show a default and its switch disagreeing.)"""
    runtime = Runtime.from_path(str(JAFFLE))
    try:
        gold = [
            (segment, str(month.date()), sessions)
            for segment, month, sessions in _sql(runtime, BY_SESSION_ORDER)
        ]
        for verbosity in ("minimal", "full"):
            out = runtime.query(SESSIONS_BY_SEGMENT, verbosity=verbosity)
            assert _answer(out) == gold
            (route,) = out["routes"]
            assert (route["via"], route["basis"], route["by"]) == (
                SESSION_ORDER,
                "default",
                "lookup",
            )
            assert route["meaning"] == (
                "the Customer history valid at the time of the Storefront session's Order"
            )
            assert [switch["via"] for switch in route["other_routes"]] == [
                [
                    "relationship.storefront_sessions_customer",
                    "relationship.orders_customer",
                    "relationship.jaffle_order_customer_history",
                ]
            ]
        windowed = {**SESSIONS_BY_SEGMENT, "time": {"grain": "month", "start": "2016-09-01",
                                                   "end": "2016-10-01"}}  # fmt: skip
        assert runtime.query(windowed, verbosity="full")["routes"] == out["routes"]
    finally:
        runtime.close()


@pytest.mark.parametrize("hidden", [INVOICE_BRANCH[-1], PRIMARY[-1]])
def test_a_view_that_hides_the_default_route_refuses_rather_than_take_another(tmp_path, hidden):
    """A caller's view answers by the whole package's route, never one chosen for the caller:
    hiding a relationship on a default's route refuses the pair (the default is not re-chosen
    among what the caller sees)."""
    base = load_package_config(str(_write_package(tmp_path)))
    pairs = {INVOICE_BRANCH[-1]: (INVOICE, REGION), PRIMARY[-1]: (ACCOUNT, MEMBERSHIP)}
    start, target = pairs[hidden]
    assert hidden in package_route(base, start=start, target=target).routes[0]
    assert package_route(base, start=start, target=target).basis == "default"
    view = build_view(base, frozenset({hidden}))
    with pytest.raises(SemanticLayerError) as refused:
        package_route(view, start=start, target=target)
    assert refused.value.code == "PATH_NOT_FOUND"


def _answered(config, start: str) -> list[str]:
    targets = []
    for entity in sorted(config.entities, key=lambda row: row.id):
        if entity.id == start:
            continue
        try:
            resolve_route(config, start=start, target=entity.id)
        except SemanticLayerError:
            continue
        targets.append(entity.id)
    return targets


@pytest.mark.parametrize("package", ["diamond", "two_keys", "campus", "campus_forward"])
def test_discovery_offers_exactly_the_targets_the_ladder_answers(tmp_path, package):
    """Discovery and hints (``eligible_path_targets``) decide a lookup default over lookups
    alone, without enumerating routes; they offer every target execute answers, and none it
    refuses."""
    path: Path = {
        "diamond": lambda: _write_package(tmp_path),
        "two_keys": lambda: _write_package(tmp_path, relationships=TWO_KEYS),
        "campus": lambda: write_campus_package(tmp_path),
        "campus_forward": lambda: write_campus_package(tmp_path, traversal=["forward"]),
    }[package]()
    hints, ladder = (load_package_config(str(path)) for _ in range(2))
    defaults = 0
    for entity in sorted(ladder.entities, key=lambda row: row.id):
        expected = _answered(ladder, entity.id)
        assert eligible_path_targets(hints, start=entity.id) == expected, entity.id
        defaults += sum(
            resolve_route(ladder, start=entity.id, target=target).basis == "default"
            for target in expected
        )
    assert defaults > 0


def _jaffle(tmp_path: Path, cardinality: str = "many_to_one") -> Path:
    """jaffle_shop, its order-to-customer-history relationship declared ``cardinality``."""
    root = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    graph_path = root / "graph.yml"
    graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    graph["graph"]["relationships"]["order_to_customer_history"]["cardinality"] = cardinality
    graph_path.write_text(yaml.safe_dump(graph, sort_keys=False), encoding="utf-8")
    return root


def test_a_many_to_many_history_is_never_a_lookup(tmp_path):
    """Declared many-to-many, a history's rows valid at one time can overlap (an account with two
    owners at once), and each would count: its validity window doesn't make it a lookup, so the
    session's segment is asked again, not answered by default, and discovery doesn't offer it."""
    config = load_package_config(str(_jaffle(tmp_path, "many_to_many")))
    with pytest.raises(SemanticLayerError) as asked:
        package_route(config, start=SESSION, target=HISTORY)
    assert asked.value.code == "AMBIGUOUS_PATH"
    assert HISTORY not in eligible_path_targets(config, start=SESSION)
    assert package_route(load_package_config(str(JAFFLE)), start=SESSION, target=HISTORY).by == (
        "lookup"
    )


def test_a_view_that_hides_every_alternative_still_states_the_reading(tmp_path):
    """A caller who can't see the session's customer sees one route to the segment: the answer
    says which reading it is, and nothing about routes the caller can't see."""
    root = _jaffle(tmp_path)
    config = with_policies(
        load_package_config(str(root)),
        visibility_policy("hidden", "relationship.storefront_sessions_customer"),
    )
    runtime = Runtime.from_config(config, source_path=str(root))
    try:
        meaning = "the Customer history valid at the time of the Storefront session's Order"
        for verbosity in ("minimal", "full"):
            hidden = runtime.query(SESSIONS_BY_SEGMENT, verbosity=verbosity, context=CALLER)
            assert hidden["routes"] == [
                {"from": SESSION, "to": HISTORY, "via": SESSION_ORDER, "meaning": meaning}
            ]
        seen = runtime.query(SESSIONS_BY_SEGMENT, verbosity="full")
        assert (seen["routes"][0]["basis"], len(seen["routes"][0]["other_routes"])) == (
            "default",
            1,
        )
        assert _answer(hidden) == _answer(seen)
    finally:
        runtime.close()


def _write_offices(root: Path) -> Path:
    """Employees reach a region through their department or their office, and an office names
    its manager: the manager key rules the office route out, so the department answers."""
    pkg = root / "staff"
    (pkg / "models").mkdir(parents=True)
    relationships = {
        "employees_department": ("employee", "department", "department_id"),
        "employees_office": ("employee", "office", "office_id"),
        "departments_region": ("department", "region", "region_id"),
        "offices_region": ("office", "region", "region_id"),
        "offices_manager": ("office", "employee", "manager_id"),
    }
    entities = ("employee", "department", "office", "region")
    documents: dict[str, Any] = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "staff",
                "namespace": "staff",
                "warehouse": "duckdb",
                "default_db": "staff.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    name: {"key": [f"{name}_id"], "model": f"{name}s"} for name in entities
                },
                "relationships": {
                    name: {
                        "entities": [source, target],
                        "cardinality": "many_to_one",
                        "via": [column],
                        "target": [f"{target}_id"],
                    }
                    for name, (source, target, column) in relationships.items()
                },
            }
        },
        "metrics.yml": {
            "metrics": {
                "employees": {
                    "kind": "aggregate",
                    "measure": "measure.staff.employee_count",
                    "value_type": "count",
                    "description": "Employees",
                }
            }
        },
    }
    for name in entities:
        model: dict[str, Any] = {"id": f"{name}s", "relation": f"{name}s", "entities": {name: {}}}
        if name == "employee":
            model["measures"] = {
                "employee_count": {"kind": "entity_count", "entity_key": "employee_id"}
            }
        documents[f"models/{name}s.yml"] = {"model": model}
    for name, document in documents.items():
        (pkg / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return pkg


def test_hints_in_a_view_offer_what_the_view_resolves(tmp_path):
    """A view answers by the whole package's route, so its hints are the whole package's that it
    resolves: hiding the manager key doesn't make the region ambiguous for the caller, whose
    query still reads it through the department."""
    base = load_package_config(str(_write_offices(tmp_path)))
    employee, region = "entity.staff_employee", "entity.staff_region"
    resolution = package_route(base, start=employee, target=region)
    assert (resolution.basis, resolution.by) == ("default", "own_keys")
    view = build_view(base, frozenset({"relationship.offices_manager"}))
    assert package_route(view, start=employee, target=region).routes[0] == resolution.routes[0]
    assert region in eligible_path_targets(view, start=employee)
    assert eligible_path_targets(view, start=employee) == _answered(view, employee)
