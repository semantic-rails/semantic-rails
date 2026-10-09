"""Loading a package runs the authoring check validate-config reports, on every entry point.

Each refused row below used to load and serve: the loader ignored the key, or fell back from
the value, so the package behaved differently from what it says.
"""

import asyncio
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.config import _merge_package_dir
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.package_snapshot import capture_package_source
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


def _all(*edits):
    return lambda doc: [edit(doc) for edit in edits]


def _rollup(**columns):
    """A monthly rollup of orders that binds ``columns``."""
    return _set(
        "models",
        "orders",
        "variants",
        "monthly",
        relation="shop_order_monthly",
        grain={"time": "month", "entities": []},
        time={"role": "ordered_at", "column": "month_start"},
        columns=columns,
    )


_ORDER_CUSTOMER = "relationship.orders_customer"


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
    "model-defaults": (
        _set("models", "orders", defaults={"time": {"timezone": "America/New_York"}}),
        "model 'orders' has unknown key 'defaults'",
    ),
    # A rollup binding is checked whatever its name resolves to; the loader reads it either way.
    "measure-id-binding-key": (
        _all(
            _set("models", "orders", "measures", "revenue_usd", id="rev_total"),
            _rollup(rev_total={"column": "rev", "agregation": "max"}),
        ),
        "variant 'monthly' column 'rev_total' has unknown key 'agregation'",
    ),
    "foreign-key-binding-key": (
        _rollup(customer_id={"column": "customer_id", "pth": [_ORDER_CUSTOMER]}),
        "variant 'monthly' column 'customer_id' has unknown key 'pth'",
    ),
    "key-column-binding-measure-key": (
        _rollup(order_id={"column": "order_id", "rollup": "additive"}),
        "variant 'monthly' column 'order_id' has unknown key 'rollup'",
    ),
    "dimension-id-binding-key": (
        _all(
            _set("models", "orders", "dimensions", "channel", id="order_channel"),
            _rollup(order_channel={"column": "channel", "pth": [_ORDER_CUSTOMER]}),
        ),
        "variant 'monthly' column 'order_channel' has unknown key 'pth'",
    ),
    # A name that binds nothing is read for nothing: the measure falls back to its own column.
    "unresolved-binding-key": (
        _rollup(mystery={"column": "mystery", "agregation": "max"}),
        "variant 'monthly' column 'mystery' names no measure, dimension or key column",
    ),
    "misspelled-binding-name": (
        _rollup(revenue_ud={"column": "rev", "aggregation": "max"}),
        "variant 'monthly' column 'revenue_ud' names no measure, dimension or key column of the "
        "model — it is ignored by the loader, so this would silently change behavior; did you "
        "mean 'revenue_usd'?",
    ),
    # `as:` replaces the `id:`, so the loader reads a binding by the `as:` value only.
    "measure-id-binding-overridden-by-as": (
        _all(
            _set(
                "models",
                "orders",
                "measures",
                "revenue_usd",
                id="measure.shop.rev_a",
                **{"as": "measure.shop.rev_b"},
            ),
            _rollup(**{"measure.shop.rev_a": {"column": "rev", "aggregation": "max"}}),
        ),
        "variant 'monthly' column 'measure.shop.rev_a' names no measure, dimension or key column",
    ),
    "dimension-id-binding-overridden-by-as": (
        _all(
            _set(
                "models",
                "orders",
                "dimensions",
                "channel",
                id="order_channel_a",
                **{"as": "order_channel_b"},
            ),
            _rollup(order_channel_a={"column": "channel"}),
        ),
        "variant 'monthly' column 'order_channel_a' names no measure, dimension or key column",
    ),
    "misspelled-binding-id": (
        _rollup(**{"measure.shop.revenue_ud": {"column": "rev", "aggregation": "max"}}),
        "column 'measure.shop.revenue_ud' names no measure, dimension or key column of the model "
        "— it is ignored by the loader, so this would silently change behavior; did you mean "
        "'measure.shop.revenue_usd'",
    ),
    "measure-accumulation-key": (
        _set(
            "models",
            "orders",
            "measures",
            "revenue_usd",
            accumulation={"kind": "stock", "snapshto": "last"},
        ),
        "measure 'revenue_usd' accumulation has unknown key 'snapshto' — unknown keys are ignored "
        "by the loader, so this would silently change behavior; did you mean 'snapshot'?",
    ),
    "defaults-measure-accumulation-key": (
        _set("defaults", "measure", accumulation={"kind": "stock", "snapshto": "last"}),
        "defaults.measure accumulation has unknown key 'snapshto'",
    ),
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


def test_key_column_binding_with_path_loads(tmp_path):
    customer = {"column": "customer_id", "path": [_ORDER_CUSTOMER]}
    source = _package(tmp_path, _rollup(revenue_usd="revenue_usd", customer_id=customer))
    runtime = Runtime.from_path(str(source))
    try:
        (rollup,) = runtime._config.aggregate_relations
        assert rollup.dimension_columns["dimension.shop_order_customer_id"] == "customer_id"
    finally:
        runtime.close()


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


def _directory(tmp_path: Path, files: dict) -> Path:
    """The bundled directory package, each ``files`` entry rebuilt from its current document."""
    package = copy_package_config(tmp_path, "jaffle_shop")
    for name, build in files.items():
        target = package / name
        doc = yaml.safe_load(target.read_text(encoding="utf-8")) if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(build(doc), sort_keys=False), encoding="utf-8")
    return package


_ORDERS_FILE = "models/core/orders.yml"
_NO_WRAPPER = {
    # package.yml's own `defaults:` would be a second error: defaults.yml replaces it.
    "package.yml": lambda doc: {key: value for key, value in doc.items() if key != "defaults"},
    "defaults.yml": lambda _: {"time": {"timezone": "America/New_York"}},
}
_GRAPH_ALIASES = {"graph.yml": lambda doc: {**doc, "aliases": {"buyer": "customer"}}}
_MODEL_METRICS = {
    _ORDERS_FILE: lambda doc: {
        **doc,
        "metrics": {"orders_total": {"kind": "aggregate", "measure": "order_count"}},
    }
}
# name: (files rewritten in the directory package, what the refusal names)
DIRECTORY_REFUSED = {
    "block-file-without-wrapper": (
        _NO_WRAPPER,
        "defaults.yml has unknown key 'time' — unknown keys are ignored by the loader, so this "
        "would silently change behavior; write this file's contents under a top-level "
        "'defaults' key",
    ),
    "block-file-sibling": (
        _GRAPH_ALIASES,
        "graph.yml has unknown key 'aliases' — unknown keys are ignored by the loader, so this "
        "would silently change behavior; the loader reads only 'graph' from this file",
    ),
    "model-file-sibling": (
        _MODEL_METRICS,
        "orders.yml has unknown key 'metrics' — unknown keys are ignored by the loader, so this "
        "would silently change behavior; the loader reads only 'model' from this file",
    ),
}


@pytest.mark.parametrize(("files", "expected"), DIRECTORY_REFUSED.values(), ids=DIRECTORY_REFUSED)
def test_directory_refuses_root_keys_the_loader_drops(tmp_path, files, expected):
    package = _directory(tmp_path, files)
    with pytest.raises(SemanticLayerError) as refused:
        Runtime.from_path(str(package))
    assert refused.value.code == "INVALID_CONFIG"
    (error,) = refused.value.details["errors"]
    assert error.startswith(str(package)) and error.endswith(expected)
    # validate-config's split-layout check also names a top-level aliases registry.
    assert [row for row in validate_runtime_package(package) if "aliases registry" not in row] == [
        error
    ]


def test_directory_lists_each_dropped_root_key(tmp_path):
    package = _directory(tmp_path, {**_NO_WRAPPER, **_GRAPH_ALIASES, **_MODEL_METRICS})
    errors = [row for row in validate_runtime_package(package) if " has unknown key " in row]
    assert [error.split(" has unknown key ")[1].split(" ")[0] for error in errors] == [
        "'time'",
        "'aliases'",
        "'metrics'",
    ]


def test_directory_underscore_root_keys_stay_annotations(tmp_path):
    notes = {"_notes": {"owner": "analytics"}}
    files = {name: lambda doc: {**doc, **notes} for name in ("graph.yml", _ORDERS_FILE)}
    Runtime.from_path(str(_directory(tmp_path, files))).close()


def _package_yml(**blocks):
    return {"package.yml": lambda doc: {**doc, **blocks}}


_NEW_YORK = {"time": {"timezone": "America/New_York"}}
_POLICY = {
    "id": "policy.jaffle.day_required",
    "kind": "metric_constraint",
    "object_ids": ["measure.jaffle.revenue_usd"],
    "required_group_by": ["dimension.jaffle_order_ordered_at"],
}
_DROPPED = "is ignored by the loader, so this would silently change behavior"
# name: (files written in the directory package, the refusal with the package path removed)
DIRECTORY_SILENT_DROPS = {
    "defaults-in-both-files": (
        {
            "package.yml": lambda doc: {**doc, "defaults": {**doc["defaults"], **_NEW_YORK}},
            "defaults.yml": lambda _: {"defaults": {"dimension": {"groupable": True}}},
        },
        f"package.yml declares 'defaults', which defaults.yml replaces — the package.yml block "
        f"{_DROPPED}; keep the block in one of the two files",
    ),
    "graph-in-both-files": (
        _package_yml(graph={"path_policy": {"max_hops": 2}}),
        f"package.yml declares 'graph', which graph.yml replaces — the package.yml block "
        f"{_DROPPED}; keep the block in one of the two files",
    ),
    "model-in-package-and-file": (
        _package_yml(models={"orders": {"relation": "jaffle_order"}}),
        f"models/core/orders.yml defines model 'orders', which package.yml also defines — the "
        f"definition in package.yml {_DROPPED}; keep one definition",
    ),
    "model-in-two-files": (
        {"models/extra/orders.yml": lambda _: {"model": {"id": "orders", "relation": "x"}}},
        f"models/extra/orders.yml defines model 'orders', which models/core/orders.yml also "
        f"defines — the definition in models/core/orders.yml {_DROPPED}; keep one definition",
    ),
    "policies-directory": (
        {"policies/day.yml": lambda _: {"semantic_policies": [_POLICY]}},
        "policies/ is not a package directory — its YAML is ignored by the loader, so this "
        "would silently change behavior; write these policies in policies.yml",
    ),
    "unknown-root-file": (
        {"notes.yml": lambda _: {"owner": "analytics"}},
        "notes.yml is not a package file — its YAML is ignored by the loader, so this would "
        "silently change behavior; move its contents into caveats.yml, defaults.yml, graph.yml, "
        "metrics.yml, package.yml, policies.yml, relations.yml, segments.yml or start its name "
        "with '_'",
    ),
    "yaml-spelling-of-a-block-file": (
        {"defaults.yaml": lambda _: {"defaults": _NEW_YORK}},
        "defaults.yaml is not a package file — its YAML is ignored by the loader, so this would "
        "silently change behavior; rename it defaults.yml",
    ),
    "root-tests-file": (
        {"tests.yml": lambda _: {"tests": {"orders": {"kind": "query_row_count_bounds"}}}},
        "tests.yml is not a package file — its YAML is ignored by the loader, so this would "
        "silently change behavior; write these entries under tests/",
    ),
    "root-examples-file": (
        {"examples.yml": lambda _: {"examples": {"orders": {"question": "Orders?"}}}},
        "examples.yml is not a package file — its YAML is ignored by the loader, so this would "
        "silently change behavior; write these entries under examples/",
    ),
}


@pytest.mark.parametrize(
    ("files", "expected"), DIRECTORY_SILENT_DROPS.values(), ids=DIRECTORY_SILENT_DROPS
)
def test_directory_refuses_input_the_loader_never_reads(tmp_path, files, expected):
    package = _directory(tmp_path, files)
    with pytest.raises(SemanticLayerError) as refused:
        Runtime.from_path(str(package))
    assert refused.value.code == "INVALID_CONFIG"
    (error,) = refused.value.details["errors"]
    assert error.replace(f"{package}/", "") == expected
    assert error in validate_runtime_package(package)


def test_directory_lists_each_input_it_never_reads(tmp_path):
    files = {}
    for edits, _ in DIRECTORY_SILENT_DROPS.values():
        files.update(edits)
    files["package.yml"] = lambda doc: {
        **doc,
        "defaults": {**doc["defaults"], **_NEW_YORK},
        "graph": {"path_policy": {"max_hops": 2}},
        "models": {"orders": {"relation": "jaffle_order"}},
    }
    package = _directory(tmp_path, files)
    errors = [row for row in validate_runtime_package(package) if _DROPPED in row]
    assert sorted(error.replace(f"{package}/", "") for error in errors) == sorted(
        expected for _, expected in DIRECTORY_SILENT_DROPS.values()
    )


def test_directory_underscore_root_files_stay_ignored(tmp_path):
    files = {
        "_scratch.yml": lambda _: {"defaults": _NEW_YORK},
        "_drafts/policies.yml": lambda _: {"semantic_policies": [_POLICY]},
    }
    Runtime.from_path(str(_directory(tmp_path, files))).close()


def _linked_directory(tmp_path: Path, link: str, files: dict) -> Path:
    """The bundled directory package with ``link`` a symlink to a directory outside it holding
    ``files``, plus whatever ``link`` held in the package."""
    package = copy_package_config(tmp_path, "jaffle_shop")
    target = tmp_path / "outside" / link
    if (package / link).is_dir():
        target.parent.mkdir(parents=True)
        (package / link).rename(target)
    for name, doc in files.items():
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (package / link).symlink_to(target, target_is_directory=True)
    return package


_LINKED_POLICY = {"day.yml": {"semantic_policies": [_POLICY]}}


@pytest.mark.parametrize(
    ("link", "files"),
    [
        pytest.param("policies", _LINKED_POLICY, id="policies-directory"),
        pytest.param("models", {}, id="models-directory"),
        pytest.param("models/core", {}, id="model-subdirectory"),
        pytest.param("examples", {"orders.yml": {"examples": {}}}, id="examples-directory"),
    ],
)
def test_directory_symlink_is_refused_not_followed(tmp_path, link, files):
    package = _linked_directory(tmp_path, link, files)
    expected = (
        f"{package / link} is a directory symlink — the loader does not follow it, so this "
        "would silently change behavior; copy or link the files instead"
    )
    walked, captured = [], []
    for errors, source in ((walked, None), (captured, capture_package_source(package))):
        with pytest.raises(SemanticLayerError) as refused:
            _merge_package_dir(str(package), captured=source)
        assert refused.value.code == "INVALID_CONFIG"
        errors.extend(refused.value.details["errors"])
    assert walked == captured == [expected]
    with pytest.raises(SemanticLayerError) as refused:
        Runtime.from_path(str(package))
    assert refused.value.details["errors"] == [expected]


def test_directory_symlink_starting_with_underscore_stays_ignored(tmp_path):
    package = _linked_directory(tmp_path, "_shared", _LINKED_POLICY)
    _merge_package_dir(str(package))
    _merge_package_dir(str(package), captured=capture_package_source(package))
    Runtime.from_path(str(package)).close()


_ROLLUPS = """
schema_version: 1
package: {id: rollups, warehouse: duckdb, default_db: rollups.duckdb, seed: {kind: external}}
graph:
  entities:
    orders: {key: order_id}
models:
  orders:
    relation: orders
    times:
      ordered_at: {column: ordered_at, kind: timestamp, class: event_time, default: true}
    measures:
      revenue: {kind: aggregate, expr: amount, default_agg: sum, accumulation: {kind: flow}}
    variants:
      monthly:
        relation: orders_monthly
        grain: {time: month, entities: []}
        time: {role: ordered_at, column: month_start}
"""
_ROLLUP_SEED = """
CREATE TABLE orders AS SELECT * FROM (VALUES
  ('o1', TIMESTAMP '2026-01-03 10:00:00', 40), ('o2', TIMESTAMP '2026-01-20 12:00:00', 25),
  ('o3', TIMESTAMP '2026-02-02 09:00:00', 10), ('o4', TIMESTAMP '2026-02-14 18:00:00', 70),
  ('o5', TIMESTAMP '2026-03-01 08:00:00', 55)
) AS t(order_id, ordered_at, amount);
CREATE TABLE orders_monthly AS
  SELECT date_trunc('month', ordered_at) AS month_start, MAX(amount) AS rev FROM orders GROUP BY 1;
"""
_MAX_REV = {"column": "rev", "aggregation": "max", "holds": "max"}


def _rollup_package(root: Path, fields: dict | None = None, **columns) -> Path:
    """Orders whose monthly rollup binds ``columns``, over a DuckDB file holding both tables;
    ``fields`` are added to the revenue measure."""
    doc = yaml.safe_load(_ROLLUPS)
    doc["models"]["orders"]["measures"]["revenue"].update(fields or {})
    doc["models"]["orders"]["variants"]["monthly"]["columns"] = columns
    root.mkdir(parents=True)
    source = root / "package.yml"
    source.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(root / "rollups.duckdb")) as conn:
        conn.execute(_ROLLUP_SEED)
    return source


def _assert_monthly_max_from_rollup(source: Path, measure: str) -> None:
    """The monthly maximum of ``measure`` is read from the rollup and matches the reference."""
    runtime = Runtime.from_path(str(source))
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate",
                            "measure": measure,
                            "aggregation": "max",
                        },
                        "as": "top",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.rollups_orders_ordered_at",
                    "grain": "month",
                },
            }
        )
    finally:
        runtime.close()
    with duckdb.connect(str(source.parent / "rollups.duckdb"), read_only=True) as conn:
        reference = conn.execute(
            "SELECT MAX(rev) FROM orders_monthly GROUP BY month_start ORDER BY month_start"
        ).fetchall()
    assert reference == [(40,), (70,), (55,)]
    assert [(row["top"],) for row in result["rows"]] == reference
    assert "FROM orders_monthly" in result["rendered_sql"]


def test_rollup_binding_answers_the_reference_once_its_name_resolves(tmp_path):
    misspelled = _rollup_package(tmp_path / "misspelled", revnue=_MAX_REV)
    with pytest.raises(SemanticLayerError, match="column 'revnue' names no measure.*'revenue'"):
        Runtime.from_path(str(misspelled))

    source = _rollup_package(tmp_path / "fixed", revenue=_MAX_REV)
    _assert_monthly_max_from_rollup(source, "measure.rollups.revenue")


def test_rollup_binding_by_an_id_that_as_replaces_is_refused(tmp_path):
    ids = {"id": "measure.rollups.rev_a", "as": "measure.rollups.rev_b"}
    by_id = _rollup_package(tmp_path / "by_id", ids, **{"measure.rollups.rev_a": _MAX_REV})
    with pytest.raises(SemanticLayerError) as refused:
        Runtime.from_path(str(by_id))
    (error,) = refused.value.details["errors"]
    assert "column 'measure.rollups.rev_a' names no measure, dimension or key column" in error
    assert "did you mean 'measure.rollups.rev_b'" in error

    by_as = _rollup_package(tmp_path / "by_as", ids, **{"measure.rollups.rev_b": _MAX_REV})
    _assert_monthly_max_from_rollup(by_as, "measure.rollups.rev_b")


@pytest.mark.parametrize(
    ("columns", "read"),
    [
        pytest.param(
            {"order_id": {"column": "order_key"}},
            ("dimension_columns", "dimension.rollups_orders_order_id", "order_key"),
            id="key-column-of-the-entity-named-after-the-model",
        ),
        pytest.param(
            {"measure.rollups.revenue": {"column": "rev", "holds": "max"}},
            ("measure_columns", "measure.rollups.revenue", "rev"),
            id="measure-by-the-id-the-namespace-gives-it",
        ),
    ],
)
def test_rollup_binding_names_the_loader_reads_still_load(tmp_path, columns, read):
    runtime = Runtime.from_path(str(_rollup_package(tmp_path / "rollups", **columns)))
    try:
        (rollup,) = runtime._config.aggregate_relations
        field, name, column = read
        assert getattr(rollup, field)[name] == column
    finally:
        runtime.close()
