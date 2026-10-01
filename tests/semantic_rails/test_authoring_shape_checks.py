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
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError


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


@pytest.mark.parametrize("primary_source", ["graph", "entity"])
@pytest.mark.parametrize("key_source", ["graph", "expr", "primary", "empty_block"])
@pytest.mark.parametrize("surface", ["validate", "load"])
def test_grain_does_not_select_primary_when_identity_is_authored(
    starter_package: Path, primary_source: str, key_source: str, surface: str
) -> None:
    raw = yaml.safe_load(starter_package.read_text(encoding="utf-8"))
    raw["models"]["customers"]["grain"] = ["authored_row_id"]
    if primary_source == "graph":
        raw["graph"]["entities"]["customer"]["model"] = "customers"
    else:
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
