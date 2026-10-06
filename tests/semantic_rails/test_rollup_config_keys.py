"""Removed rollup settings must refuse loading instead of enabling routing."""

from pathlib import Path

import pytest
import yaml

from semantic_rails.config import _load_package_source, load_package_config
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.test_model_physical_variants import _MONTHLY, _rollup_package


@pytest.mark.parametrize("layout", ["directory", "single_file"])
@pytest.mark.parametrize("location", ["variant", "inherited_variant", "base_variant", "aggregate"])
@pytest.mark.parametrize("value", [True, False, "yes"])
def test_removed_rollup_setting_is_an_unknown_key(tmp_path: Path, layout, location, value):
    variants = {}
    if location == "aggregate":
        variants = {}
    elif location == "base_variant":
        variants = {"tx": {"relation": "order_fact", "requires_certification": value}}
    else:
        variants = {"monthly": {**_MONTHLY, "requires_certification": value}}
        if location == "inherited_variant":
            variants["quarterly"] = {"inherits_from": "monthly", "grain": {"time": "quarter"}}
    source = tmp_path / "p"
    _rollup_package(source, variants)
    if location == "aggregate":
        package_file = source / "package.yml"
        package = yaml.safe_load(package_file.read_text(encoding="utf-8"))
        package["aggregate_relations"] = [{"requires_certification": value}]
        package_file.write_text(yaml.safe_dump(package), encoding="utf-8")
    if layout == "single_file":
        raw = _load_package_source(str(source))
        source = tmp_path / "package.yml"
        source.write_text(yaml.safe_dump(raw), encoding="utf-8")

    refusal = (
        "declare rollups under the model's `variants:`"
        if location == "aggregate"
        else "unknown keys.*requires_certification"
    )
    with pytest.raises(SemanticLayerError, match=refusal) as caught:
        load_package_config(str(source))
    assert caught.value.code == "INVALID_CONFIG"
