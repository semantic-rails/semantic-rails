"""Authoring-shape checks — silent mistakes must fail loudly.

A blind-author evaluation planted realistic mistakes in a fresh ``init``
package and found four that every validator accepted silently: a typo'd
key (``agg:`` for ``default_agg:``) was ignored, a scalar ``domain:``
was iterated character-wise into a corrupt value set, an invalid time
``class:`` fell through to default behavior, and a model ``grain:`` that
matched no entity key silently mis-detected the primary entity. These
tests pin the fixes: each mistake now produces a located, actionable
error from ``validate_runtime_package`` for SINGLE-FILE packages (the
directory form already ran the enum checks; the single-file form —
``init``'s output — skipped them entirely).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from semantic_rails.cli.commands.package import cmd_init
from semantic_rails.config import load_package_config
from semantic_rails.config_parts.package_loader import normalize_package
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.conftest import copy_package_config


@pytest.fixture()
def starter_package(tmp_path: Path) -> Path:
    target = tmp_path / "shape_shop"
    cmd_init(
        argparse.Namespace(output=str(target), package_id="shape_shop", namespace="", force=False)
    )
    return target / "package.yml"


def _mutated(starter_package: Path, old: str, new: str) -> Path:
    text = starter_package.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old!r} not in starter package"
    starter_package.write_text(text.replace(old, new, 1), encoding="utf-8")
    return starter_package


def _errors(path: Path) -> list[str]:
    return validate_runtime_package(path)


def test_unknown_measure_key_is_rejected(starter_package: Path) -> None:
    """`agg:` instead of `default_agg:` was silently ignored."""
    path = _mutated(starter_package, "default_agg: sum", "agg: sum")
    errors = _errors(path)
    assert any("unknown key 'agg'" in e for e in errors), errors
    assert any("default_agg" in e for e in errors), "must list the valid keys"


@pytest.mark.parametrize("key", ["subject_entity", "aggregation_entity"])
@pytest.mark.parametrize("location", ["measure", "defaults"])
@pytest.mark.parametrize("layout", ["single_file", "directory"])
def test_parent_rollup_measure_keys_are_unknown(
    starter_package: Path, key: str, location: str, layout: str
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    if location == "defaults":
        raw.setdefault("defaults", {}).setdefault("measure", {})[key] = "self"
    else:
        measure = next(iter(raw["models"]["orders"]["measures"].values()))
        measure[key] = "self"
    if layout == "directory":
        (starter_package.parent / "graph.yml").write_text(
            yaml.safe_dump({"graph": raw.pop("graph")}, sort_keys=False), encoding="utf-8"
        )
        models_dir = starter_package.parent / "models"
        models_dir.mkdir()
        (models_dir / "models.yml").write_text(
            yaml.safe_dump({"models": raw.pop("models")}, sort_keys=False), encoding="utf-8"
        )
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path = starter_package.parent if layout == "directory" else starter_package
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(path))
    assert exc.value.code == "INVALID_CONFIG"
    message = str(exc.value)
    if location == "defaults":
        assert message.count(f"defaults.measure.{key}") == 1
        assert "delete this line; parent-rollup declarations were removed" in message
        assert "measure '" not in message
    else:
        assert "unknown keys" in message and key in message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("rollup_safe_aggregations", ["sum"], id="populated"),
        pytest.param("rollup_safe_aggregations", None, id="null"),
        pytest.param("rollup_safe", {"forward": ["sum", "count"]}, id="forward"),
        pytest.param("rollup_safe", ["sum", "count"], id="list"),
        pytest.param("rollup_safe", {"reverse": ["count_distinct"]}, id="reverse"),
        pytest.param("rollup_safe", None, id="rollup-null"),
    ],
)
@pytest.mark.parametrize("layout", ["single_file", "directory"])
def test_removed_join_key_is_rejected_before_graph_override(
    tmp_path: Path, key: str, value: object, layout: str
) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop")
    orders_path = package / "models" / "core" / "orders.yml"
    raw = yaml.safe_load(orders_path.read_text(encoding="utf-8"))
    raw["model"]["joins"] = {"customer": {key: value}}
    orders_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    if layout == "single_file":
        from semantic_rails.config import _load_package_source

        raw = _load_package_source(str(package))
        package = package / "package.yml"
        package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(package))
    assert exc.value.code == "INVALID_CONFIG"
    assert "models.orders.joins.customer" in str(exc.value)
    assert key in str(exc.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("rollup_safe", ["sum"], id="list"),
        pytest.param("rollup_safe", {"forward": ["sum"]}, id="forward"),
        pytest.param("rollup_safe", {"reverse": ["count_distinct"]}, id="reverse"),
        pytest.param("rollup_safe", None, id="null"),
        pytest.param("rollup_safe_aggregations", ["sum"], id="aggregations-populated"),
        pytest.param("rollup_safe_aggregations", None, id="aggregations-null"),
    ],
)
@pytest.mark.parametrize("layout", ["single_file", "directory"])
@pytest.mark.parametrize("has_relationships", [True, False], ids=["with-joins", "without-joins"])
def test_unconsumed_relationship_default_rollup_is_rejected(
    starter_package: Path, key: str, value: object, layout: str, has_relationships: bool
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    raw.setdefault("defaults", {}).setdefault("relationship", {})[key] = value
    if not has_relationships:
        # Defaults must be refused even when there is no join to inherit them.
        raw["models"] = {"customers": raw["models"]["customers"]}
        raw["graph"]["entities"] = {"customer": raw["graph"]["entities"]["customer"]}
        raw["graph"]["relationships"] = {}
        raw["metrics"] = {}
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path = starter_package.parent if layout == "directory" else starter_package
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(path))
    assert exc.value.code == "INVALID_CONFIG"
    assert f"defaults.relationship.{key}" in str(exc.value)


@pytest.mark.parametrize(
    ("location", "value"),
    [
        pytest.param("graph", {"forward": ["sum", "count"]}, id="forward"),
        pytest.param("graph", ["sum", "count"], id="list"),
        pytest.param("graph", [], id="empty-list"),
        pytest.param("graph", None, id="null"),
        pytest.param("graph", "sum", id="scalar"),
        pytest.param("graph", {"reverse": [], "typo": []}, id="unknown-key"),
        pytest.param("defaults", ["sum", "count"], id="relationship-defaults"),
        pytest.param("join", ["sum", "count"], id="model-join"),
    ],
)
@pytest.mark.parametrize("layout", ["single_file", "directory"])
def test_removed_relationship_rollup_forms_fail_loading(
    starter_package: Path, location: str, value: object, layout: str
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    if location == "graph":
        raw["graph"]["relationships"] = {
            "orders_customer": {"entities": ["order", "customer"], "rollup_safe": value}
        }
    elif location == "defaults":
        raw.setdefault("defaults", {}).setdefault("relationship", {})[
            "rollup_safe_aggregations"
        ] = value
    else:
        raw["models"]["orders"]["joins"] = {"customer": {"rollup_safe_aggregations": value}}
    if layout == "directory":
        (starter_package.parent / "graph.yml").write_text(
            yaml.safe_dump({"graph": raw.pop("graph")}, sort_keys=False), encoding="utf-8"
        )
        models_dir = starter_package.parent / "models"
        models_dir.mkdir()
        (models_dir / "models.yml").write_text(
            yaml.safe_dump({"models": raw.pop("models")}, sort_keys=False), encoding="utf-8"
        )
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path = starter_package.parent if layout == "directory" else starter_package
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(path))
    assert exc.value.code == "INVALID_CONFIG"
    relationship = {
        "graph": "orders_customer",
        "defaults": "defaults.relationship.rollup_safe_aggregations",
        "join": "models.orders.joins.customer",
    }[location]
    assert relationship in str(exc.value)
    assert "rollup_safe" in str(exc.value)


def test_unknown_top_level_key_with_close_match_is_rejected(starter_package: Path) -> None:
    """`modles:` produced only 'models must not be empty'."""
    path = _mutated(starter_package, "\nmodels:", "\nmodles:")
    errors = _errors(path)
    assert any("'modles'" in e and "did you mean 'models'" in e for e in errors), errors


def test_scalar_domain_is_rejected(starter_package: Path) -> None:
    """`domain: new` was list()-ed into the value set ['n', 'e', 'w']."""
    path = _mutated(starter_package, "domain: [new, repeat]", "domain: new")
    errors = _errors(path)
    assert any("domain must be a list" in e for e in errors), errors


def test_invalid_time_class_is_rejected_in_single_file_form(starter_package: Path) -> None:
    """`class: event` passed because the enum checks only ran for
    directory packages — single-file parity."""
    path = _mutated(starter_package, "class: event_time", "class: event")
    errors = _errors(path)
    assert any("unknown class 'event'" in e and "event_time" in e for e in errors), errors


def test_grain_that_matches_no_entity_key_is_rejected(starter_package: Path) -> None:
    """A typo'd grain column silently fell back to first-entity primary
    detection."""
    path = _mutated(starter_package, "grain: [customer_id]", "grain: [customerid]")
    errors = _errors(path)
    assert any("grain" in e and "customerid" in e and "customer_id" in e for e in errors), errors


@pytest.mark.parametrize("key_source", ["graph", "expr", "primary", "empty_block"])
@pytest.mark.parametrize("surface", ["validate", "load"])
def test_grain_does_not_select_primary_when_identity_is_authored(
    starter_package: Path, key_source: str, surface: str
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    raw["models"]["customers"]["grain"] = ["authored_row_id"]
    raw["models"]["customers"]["entity"] = "customer"
    if key_source == "expr":
        raw["models"]["customers"]["entities"]["customer"] = {"expr": "renamed_customer_id"}
    elif key_source == "primary":
        raw["graph"]["entities"]["customer"].pop("key")
        raw["models"]["customers"]["keys"] = {"primary": ["customer_id"]}
    elif key_source == "empty_block":
        raw["models"]["customers"]["entities"] = {}
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    expected_key = "renamed_customer_id" if key_source == "expr" else "customer_id"
    if surface == "validate":
        assert any(
            "grain" in error and "authored_row_id" in error and expected_key in error
            for error in _errors(starter_package)
        )
        return
    # Loading directly bypasses raw-shape validation, so the normalizer must refuse too.
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(starter_package))
    assert exc.value.code == "INVALID_CONFIG"
    assert all(text in str(exc.value) for text in ("'customers'", "'customer'", expected_key))


@pytest.mark.parametrize("key_source", ["graph", "expr", "primary", "empty_block"])
@pytest.mark.parametrize("grain", [["authored_row_id"], ["order_id"]])
def test_explicit_graph_binding_preserves_identity_and_separate_row_grain(
    starter_package: Path, key_source: str, grain: list[str]
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    raw["graph"]["entities"]["customer"]["model"] = " customers "
    model = raw["models"]["customers"]
    model["grain"] = grain
    model["entities"]["order"] = {}
    model["measures"] = {
        "customer_rows": {
            "kind": "aggregate",
            "expr": "1",
            "accumulation": {"kind": "flow"},
            "value_type": "count",
            "publish": False,
        }
    }
    expected_key = "customer_id"
    if key_source == "expr":
        model["entities"]["customer"] = {"expr": "renamed_customer_id"}
        expected_key = "renamed_customer_id"
    elif key_source == "primary":
        raw["graph"]["entities"]["customer"].pop("key")
        model["keys"] = {"primary": ["customer_id"]}
        raw["models"]["orders"]["entities"]["customer"] = {"expr": "customer_id"}
    elif key_source == "empty_block":
        model["entities"] = {}
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert not _errors(starter_package)
    # Direct loading also bypasses raw-shape validation. Neither path may borrow
    # order's key or use the row grain to replace the graph's explicit identity.
    config = load_package_config(str(starter_package))
    normalized = normalize_package(raw)["models"]["customers"]
    assert normalized["entity"] == "customer"
    assert normalized["keys"]["primary"] == [expected_key]
    customer = next(entity for entity in config.entities if entity.id.endswith("_customer"))
    assert customer.key == ["customer_id"]
    assert customer.table == "shop_customer"
    measures = [measure for measure in config.measures if measure.entity == customer.id]
    assert measures
    assert all(measure.row_grain == grain for measure in measures)


def test_grain_matching_foreign_key_does_not_override_authored_primary(
    starter_package: Path,
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    model = raw["models"]["customers"]
    model["entity"] = "customer"
    model["entities"]["order"] = {}
    model["grain"] = ["order_id"]
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert any("grain" in error and "customer_id" in error for error in _errors(starter_package))
    with pytest.raises(SemanticLayerError, match="primary entity 'customer'") as exc:
        load_package_config(str(starter_package))
    assert exc.value.code == "INVALID_CONFIG"


def test_grain_matching_primary_expr_override_is_accepted(starter_package: Path) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    model = raw["models"]["customers"]
    model["entity"] = "customer"
    model["entities"]["customer"] = {"expr": "renamed_customer_id"}
    model["grain"] = ["renamed_customer_id"]
    starter_package.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert not any("grain" in error for error in _errors(starter_package))
    load_package_config(str(starter_package))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A direct field on a ratio metric, and a key inside an `expression:` block.
        (
            "denominator: order_count\n",
            "denominator: order_count\n    null_behavior: null_if_zero\n",
        ),
        (
            "    kind: ratio\n    numerator: revenue_usd\n    denominator: order_count\n",
            "    kind: derived\n    expression:\n      kind: ratio\n      null_behavior: null_if_zero\n"
            "      numerator: {metric: revenue_usd}\n      denominator: {metric: order_count}\n",
        ),
    ],
    ids=["direct_field", "expression_key"],
)
def test_removed_null_behavior_key_fails_to_load_with_one_message(
    starter_package: Path, old: str, new: str
) -> None:
    """`null_behavior:` used to pick how a ratio or a sum read an empty group; the engine now
    settles that itself, so a package that still writes it fails to load and says why."""
    path = _mutated(starter_package, old, new)
    with pytest.raises(SemanticLayerError, match="`null_behavior` was removed; delete the line"):
        load_package_config(str(path.parent))
    assert any("`null_behavior` was removed; delete the line" in e for e in _errors(path))


def test_ratio_operand_typo_names_metric_and_field(starter_package: Path) -> None:
    """A bad ratio denominator surfaced as a late compiler error
    ('Unknown metric recipe') with no location; it must now fail at parse
    naming the metric, the field, and close matches."""
    path = _mutated(starter_package, "denominator: order_count", "denominator: order_cnt")
    errors = _errors(path)
    assert any(
        "metric 'aov_usd'" in e and "denominator 'order_cnt'" in e and "order_count" in e
        for e in errors
    ), errors


def test_metric_without_expression_names_kind_requirements(starter_package: Path) -> None:
    """A metric whose kind produces no expression said only 'Expression
    requires a kind' with no object context."""
    path = _mutated(
        starter_package,
        "kind: aggregate\n    measure: revenue_usd",
        "type: simple\n    measure: revenue_usd",
    )
    errors = _errors(path)
    assert any("metric 'revenue_usd'" in e and "unknown key 'type'" in e for e in errors), errors
    assert any("produced no expression" in e or "missing required field" in e for e in errors), (
        errors
    )


def test_measure_without_kind_or_expr_names_both_options(starter_package: Path) -> None:
    """Dropping `kind:` from an entity_count measure said 'missing expr'
    — the wrong diagnosis."""
    path = _mutated(starter_package, "        kind: entity_count\n", "")
    errors = _errors(path)
    assert any("declares neither kind nor expr" in e for e in errors), errors


def test_clean_starter_still_validates(starter_package: Path) -> None:
    assert _errors(starter_package) == []


def test_missing_seed_file_names_the_field_and_paths(starter_package: Path) -> None:
    """A missing seed file raised INTERNAL_ERROR with a raw errno and a
    repo-root path the author never wrote."""
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.runtime import Runtime

    _mutated(starter_package, "source: data/seed_example.sql", "source: data/seed.sql")
    with pytest.raises(SemanticLayerError) as excinfo:
        runtime = Runtime.from_path(str(starter_package))
        try:
            runtime._ensure_db()  # noqa: SLF001 — seeding is the surface under test
        finally:
            runtime.close()
    message = str(excinfo.value)
    assert "package.seed.source" in message
    assert "data/seed.sql" in message
    assert excinfo.value.code == "INVALID_CONFIG"


def test_doctor_passes_a_valid_standalone_package(
    starter_package: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """doctor failed every standalone package on a cwd-relative
    Dockerfile check that carried no message."""
    import json

    from semantic_rails.cli.commands.package import cmd_doctor

    cmd_doctor(argparse.Namespace(package="", path=str(starter_package)))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True, payload
    assert payload["failing_checks"] == []
    dockerfile = next(c for c in payload["checks"] if c["name"] == "dockerfile")
    assert dockerfile["ok"] is True
    assert dockerfile["present"] is False
    assert "informational" in dockerfile["note"]
