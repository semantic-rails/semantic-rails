"""Loading a package runs the authoring check validate-config reports, on every entry point.

Each refused row below used to load and serve: the loader ignored the key, or fell back from
the value, so the package behaved differently from what it says.
"""

import asyncio
from pathlib import Path

import pytest
import yaml

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, write_single_file_package


def _set(*path, **values):
    def edit(doc):
        for key in path:
            doc = doc.setdefault(key, {})
        doc.update(values)

    return edit


def _add(key, value):
    return lambda doc: doc.update({key: value})


# name: (edit of the starter package, what the refusal names)
REFUSED = {
    "measure-kind": (
        _set("models", "orders", "measures", "revenue_usd", kind="additive"),
        "measure 'revenue_usd' has unknown kind 'additive'",
    ),
    "metric-kind-beside-expression": (
        _set("metrics", "revenue_per_item_usd", kind="arithmetic"),
        "metric 'revenue_per_item_usd' has unknown kind 'arithmetic'",
    ),
    "dimension-kind": (
        _set("models", "orders", "dimensions", "channel", kind="string"),
        "dimension orders.channel has unknown kind 'string'",
    ),
    "time-class": (
        _set("models", "orders", "times", "ordered_at", **{"class": "snapshot_time"}),
        "times orders.ordered_at has unknown class 'snapshot_time'",
    ),
    "segment-membership-key": (
        _add("segments", {"big": {"entity": "customer", "membership": {"filters": []}}}),
        "segment 'big' membership has unknown key 'filters'",
    ),
    "graph-entity-key": (
        _set("graph", "entities", "customer", synonym=["buyer"]),
        "graph entity 'customer' has unknown key 'synonym'",
    ),
    "package-key": (
        _set("package", observation_scope="query"),
        "package block has unknown key 'observation_scope'",
    ),
    "model-entities-entry-key": (
        _set("models", "orders", "entities", "customer", column="customer_id"),
        "model 'orders' entities entry 'customer' has unknown key 'column'",
    ),
    "graph-relationship-key": (
        _set(
            "graph",
            "relationships",
            "order_customer",
            entities=["order", "customer"],
            cardinalty="many_to_one",
        ),
        "graph relationship 'order_customer' has unknown key 'cardinalty'",
    ),
    "caveat-key": (
        _add(
            "semantic_caveats",
            [{"id": "caveat.late", "kind": "data_quality", "message": "Late.", "severty": "info"}],
        ),
        "caveat 'caveat.late' has unknown key 'severty'",
    ),
    "defaults-key": (
        _set("defaults", observation_scop="query"),
        "defaults has unknown key 'observation_scop'",
    ),
    "top-level-key": (_add("rollups", {}), "shop/package.yml has unknown key 'rollups'"),
}
# Refused before too, each by a check of its own; now by the one check, in its words.
CONSOLIDATED = {
    "defaults-time-key": (
        _set("defaults", "time", timezon="UTC"),
        "defaults.time has unknown key 'timezon'",
    ),
    "model-join-key": (
        _set("models", "orders", "joins", "customer", to="customer", path_preference=1),
        "model 'orders' join 'customer' has unknown key 'path_preference'",
    ),
    "path-policy-key": (
        _add("path_policy", {"max_hop": 3}),
        "path_policy has unknown key 'max_hop'",
    ),
    "graph-path-policy-key": (
        _set("graph", "path_policy", max_hop=3),
        "graph path_policy has unknown key 'max_hop'",
    ),
    "caveat-time-key": (
        _add(
            "semantic_caveats",
            [{"id": "caveat.late", "kind": "data_quality", "message": "Late.", "time": {"on": 1}}],
        ),
        "caveat 'caveat.late' time has unknown key 'on'",
    ),
    "rollup-safe-key": (
        _set(
            "graph",
            "relationships",
            "order_customer",
            entities=["order", "customer"],
            rollup_safe={"reverse": [], "forward": ["sum"]},
        ),
        "graph relationship 'order_customer' rollup_safe has unknown key 'forward'",
    ),
}


def _package(tmp_path: Path, *edits) -> Path:
    source = write_single_file_package(tmp_path / "shop")
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    for edit in edits:
        edit(doc)
    source.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return source


ROWS = {**REFUSED, **CONSOLIDATED}


@pytest.mark.parametrize(("edit", "expected"), ROWS.values(), ids=ROWS)
def test_serving_refuses_what_validate_config_refuses(tmp_path, edit, expected):
    source = _package(tmp_path, edit)
    with pytest.raises(SemanticLayerError) as refused:
        Runtime.from_path(str(source))
    assert refused.value.code == "INVALID_CONFIG"
    (error,) = refused.value.details["errors"]
    assert error.startswith(str(source)) and expected in error
    assert str(refused.value) == error
    assert validate_runtime_package(source) == [error]


def test_underscore_keys_stay_annotations(tmp_path):
    Runtime.from_path(str(_package(tmp_path, _add("_notes", {"owner": "analytics"})))).close()


def test_validate_config_lists_each_error_once(tmp_path):
    names = ("graph-relationship-key", "measure-kind")
    source = _package(tmp_path, *(REFUSED[name][0] for name in names))
    errors = validate_runtime_package(source)
    assert len(errors) == 2, errors
    assert all(REFUSED[name][1] in error for name, error in zip(names, errors, strict=True))


def test_every_entry_point_refuses_the_same_package(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop")
    graph = package / "graph.yml"
    doc = yaml.safe_load(graph.read_text(encoding="utf-8"))
    doc["graph"]["relationships"]["orders_customer"]["cardinalty"] = "many_to_one"
    graph.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    messages = []
    for load in (Runtime.from_path, SemanticLayerMCPAdapter.from_path):
        with pytest.raises(SemanticLayerError) as refused:
            load(str(package))
        assert refused.value.code == "INVALID_CONFIG"
        messages.append(refused.value.details["errors"])
    server = create_architect_mcp_server(workspace_root=tmp_path)
    _, report = asyncio.run(
        server.call_tool("validate_project", {"project_path": str(package), "mode": "parse"})
    )
    assert not report["ok"]
    assert {error["code"] for error in report["errors"]} == {"INVALID_CONFIG"}
    messages.append([error["message"] for error in report["errors"]])

    # The same one error, whichever path the package directory was spelled with.
    assert {tuple(row.split(": ", 1)[1] for row in errors) for errors in messages} == {
        (
            "graph relationship 'orders_customer' has unknown key 'cardinalty' — unknown keys "
            "are ignored by the loader, so this would silently change behavior; did you mean "
            "'cardinality'?",
        )
    }
