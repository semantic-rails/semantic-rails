"""One default per model and unsupported advisory keys are load-time invariants."""

from copy import deepcopy

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata import catalog_payload, inspect_payload
from semantic_rails.runtime import Runtime
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import copy_package_config, write_single_file_package
from tests.semantic_rails.test_upgrade_rules_authoring import _write_defaults

REFUSED = [
    (("models", "orders"), "default_time", "ordered_at"),
    (("models", "orders", "times", "ordered_at"), "default_query_axis", True),
    (("defaults", "time"), "default_query_axis", False),
    (("defaults", "time"), "default", True),
    (("defaults", "time"), "preferred_filter_ops", ["="]),
    (("defaults", "dimension"), "preferred_filter_ops", ["="]),
    (("models", "orders", "dimensions", "channel"), "preferred_filter_ops", ["="]),
    (("models", "orders", "times", "ordered_at"), "preferred_filter_ops", ["="]),
    *[
        (path, key, ["legacy"])
        for path in [
            ("models", "orders", "measures", "revenue_usd"),
            ("defaults", "measure"),
            ("metrics", "revenue_usd"),
        ]
        for key in ("clock_variants", "comparison_peers")
    ],
    *[
        (("models", "orders", "measures", "revenue_usd", "publish"), key, ["legacy"])
        for key in ("clock_variants", "comparison_peers", "preferred_filter_ops")
    ],
]


@pytest.mark.parametrize("layout", ["single-file", "directory"])
@pytest.mark.parametrize(("path", "key", "value"), REFUSED)
def test_unsupported_forms_refuse_at_load(tmp_path, layout, path, key, value):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    row = doc
    for part in path:
        row = row.setdefault(part, {})
    row[key] = value
    package = _write_defaults(source, doc, doc.get("defaults", {}), layout)
    # A measure's publish: is true or false; any mapping is refused, whatever it holds.
    refusal = "publish must be true or false" if path[-1] == "publish" else key
    with pytest.raises(SemanticLayerError, match=refusal) as exc:
        load_package_config(str(package))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("layout", ["single-file", "directory"])
def test_inherited_default_refuses_with_two_unmarked_roles(tmp_path, layout):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    times = doc["models"]["orders"]["times"]
    times["ordered_at"].pop("default")
    times["fulfilled_at"] = {**deepcopy(times["ordered_at"]), "column": "fulfilled_at"}
    doc["defaults"]["time"]["default"] = True
    package = _write_defaults(source, doc, doc["defaults"], layout)
    with pytest.raises(SemanticLayerError, match="defaults.time.*default") as exc:
        load_package_config(str(package))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("reverse", [False, True])
def test_two_defaults_refuse_independent_of_role_order(tmp_path, reverse):
    source = write_single_file_package(tmp_path / "project")
    doc = safe_load(source.read_bytes())
    times = doc["models"]["orders"]["times"]
    times["fulfilled_at"] = {**deepcopy(times["ordered_at"]), "column": "fulfilled_at"}
    if reverse:
        doc["models"]["orders"]["times"] = dict(reversed(list(times.items())))
    source.write_text(yaml.safe_dump(doc))
    with pytest.raises(SemanticLayerError, match="multiple default: true") as exc:
        load_package_config(str(source))
    assert exc.value.code == "INVALID_CONFIG"


def test_defaults_on_separate_models_sharing_an_entity_load(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop")
    config = load_package_config(str(package))
    defaults = {role.id for role in config.temporal_roles if role.default_query_time_axis}
    assert {
        "temporal_role.jaffle_daily_metric_day",
        "temporal_role.jaffle_monthly_metric_month",
        "temporal_role.jaffle_order_time",
    } <= defaults
    engine = Runtime.from_path(str(package))
    try:
        payloads = [catalog_payload(engine, view="full", verbosity="full")]
        payloads.extend(
            inspect_payload(engine, object_id=object_id, verbosity="full")
            for object_id in ["measure.jaffle.revenue_usd", "metric.sales.aov_usd"]
        )
        for payload in payloads:
            text = str(payload)
            assert all(
                key not in text
                for key in ("clock_variants", "comparison_peers", "preferred_filter_ops")
            )
    finally:
        engine.close()
