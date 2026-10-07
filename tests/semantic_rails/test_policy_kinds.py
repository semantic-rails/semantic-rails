from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.policies import (
    enforce_query_policies,
    policy_effects_for_object,
)
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from semantic_rails.visible_view import hidden_object_ids

MEASURE = "measure.jaffle.revenue_usd"
INVALID_POLICIES = [
    ("object_acess", "deny", "unknown kind"),
    ("plan_constraint", "", "unknown kind"),
    ("object_access", "block", "unsupported action"),
    ("object_access", "", "unsupported action"),
    ("object_visibility", "deny", "unsupported action"),
    ("object_visibility", "", "unsupported action"),
    ("object_visibility", "visible", "'visible_only'"),
    ("package_release", "deny", "unsupported action"),
    ("protected_object", "deny", "unsupported action"),
    ("metric_constraint", "deny", "unsupported action"),
    ("row_filter", "deny", "unsupported keys"),
]
VALID_POLICIES = [
    ("object_access", "deny"),
    ("object_access", "redact"),
    ("object_access", "withhold_values"),
    ("object_visibility", "hidden"),
    ("package_release", ""),
    ("package_release", "label"),
    ("protected_object", ""),
    ("protected_object", "protected"),
    ("metric_constraint", ""),
    ("metric_constraint", "constrain"),
    ("row_filter", ""),
]


@pytest.mark.parametrize(
    "path",
    [
        "configs/semantic_rails/jaffle_shop",
        "configs/semantic_rails/tpch_sf1_showcase",
        "configs/examples/semantic_rails_package_starter.yml",
    ],
)
def test_shipped_packages_validate(path):
    assert validate_runtime_package(Path(path)) == []


@pytest.fixture(scope="module")
def starter():
    raw = yaml.safe_load(
        Path("configs/examples/semantic_rails_package_starter.yml").read_text(encoding="utf-8")
    )
    # Default mode must not auto-publish duplicates of the explicit metrics.
    for model in raw["models"].values():
        for measure in model.get("measures", {}).values():
            measure["publish"] = False
    return raw


def write_policy_package(tmp_path, starter, strict, kind, action, location="action", **fields):
    raw = deepcopy(starter)
    raw["package"]["schema_strict"] = strict
    policy = {"id": "policy.test", "kind": kind, **fields}
    if location == "action":
        policy["action"] = action
    else:
        policy["config"] = {location: action}
    if kind == "package_release":
        policy.setdefault("config", {})["label"] = "stable"
    if kind == "row_filter":
        policy.update(dimension="dimension.shop_customer_customer_type", attribute="customer_type")
    if kind == "object_visibility":
        # Both visibility actions list the objects they hide.
        policy.setdefault("object_ids", ["metric.shop.revenue_usd"])
    raw["semantic_policies"] = [policy]
    path = tmp_path / "package.yml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(("kind", "action", "problem"), INVALID_POLICIES)
def test_package_validation_rejects_invalid_policies(
    tmp_path, starter, strict, kind, action, problem
):
    path = write_policy_package(tmp_path, starter, strict, kind, action)
    errors = validate_runtime_package(path)
    assert any(problem in error and "policy.test" in error for error in errors), errors
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(path))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(("kind", "action"), VALID_POLICIES)
def test_valid_policy_kinds_and_actions_load(tmp_path, starter, strict, kind, action):
    path = write_policy_package(tmp_path, starter, strict, kind, action)
    assert validate_runtime_package(path) == []
    assert load_package_config(str(path)).semantic_policies[0].kind == kind


@pytest.mark.parametrize("location", ["action", "visibility"])
@pytest.mark.parametrize("action", ["hidden", "block"])
def test_nested_policy_actions_are_validated(tmp_path, starter, location, action):
    path = write_policy_package(tmp_path, starter, False, "object_visibility", action, location)
    errors = validate_runtime_package(path)
    assert bool(errors) == (action == "block"), errors


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(("kind", "action", "problem"), INVALID_POLICIES[:3])
def test_directory_package_rejects_invalid_policy(tmp_path, starter, strict, kind, action, problem):
    path = tmp_path / "shop_starter"
    path.mkdir()
    package_file = write_policy_package(path, starter, strict, "object_visibility", "hidden")
    raw = yaml.safe_load(package_file.read_text(encoding="utf-8"))
    graph = raw.pop("graph")
    models = raw.pop("models")
    for spec in graph["entities"].values():
        spec["model"] = next(
            key for key, model in models.items() if model["grain"] == [spec["key"]]
        )
        spec["key"] = [spec["key"]]
    (path / "graph.yml").write_text(yaml.safe_dump({"graph": graph}), encoding="utf-8")
    (path / "models").mkdir()
    for key, model in models.items():
        model.pop("grain")
        (path / "models" / f"{key}.yml").write_text(
            yaml.safe_dump({"model": {"id": key, **model}}), encoding="utf-8"
        )
    package_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert validate_runtime_package(path) == []
    (path / "policies.yml").write_text(
        yaml.safe_dump(
            {"semantic_policies": [{"id": "policy.test", "kind": kind, "action": action}]}
        ),
        encoding="utf-8",
    )
    errors = validate_runtime_package(path)
    assert any(problem in error and "policy.test" in error for error in errors), errors


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config("configs/semantic_rails/jaffle_shop"), semantic_policies=[])


@pytest.mark.parametrize(("kind", "action", "problem"), INVALID_POLICIES[:-1])
@pytest.mark.parametrize("warm_cache", [False, True])
def test_runtime_refuses_invalid_policy_before_rendering(
    config, monkeypatch, kind, action, problem, warm_cache
):
    policy = SemanticPolicyConfig("policy.test", kind, object_ids=[MEASURE], action=action)
    governed = replace(config, semantic_policies=[] if warm_cache else [policy])
    engine = Runtime.from_config(governed, source_path="configs/semantic_rails/jaffle_shop")
    query = {"select": [{"expression": {"measure": MEASURE}}]}
    if warm_cache:
        assert engine.compile(query)["rendered_sql"]
        engine._config.semantic_policies.append(policy)

    def no_output(*args, **kwargs):
        pytest.fail("invalid policy reached SQL rendering or warehouse access")

    monkeypatch.setattr("semantic_rails.compiler.render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        report = engine.validate(query)
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_CONFIG"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError, match=problem) as exc:
                operation(query)
            assert exc.value.code == "INVALID_CONFIG"
    finally:
        engine.close()


@pytest.mark.parametrize(("kind", "action"), [("object_acess", "deny"), ("object_access", "block")])
@pytest.mark.parametrize(
    "surface", [enforce_query_policies, policy_effects_for_object, hidden_object_ids]
)
def test_direct_policy_evaluation_refuses_invalid_policy(config, kind, action, surface):
    governed = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig("policy.test", kind, object_ids=[MEASURE], action=action)
        ],
    )
    args = (
        ()
        if surface is hidden_object_ids
        else ([MEASURE] if surface is enforce_query_policies else MEASURE,)
    )
    with pytest.raises(SemanticLayerError) as exc:
        surface(governed, *args)
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("kind", ["object_acess", "plan_constraint", *dict(VALID_POLICIES)])
def test_package_schema_has_a_closed_policy_kind_list(starter, kind):
    jsonschema = pytest.importorskip("jsonschema")
    raw = deepcopy(starter)
    raw["semantic_policies"] = [{"id": "policy.test", "kind": kind}]
    errors = list(
        jsonschema.Draft202012Validator(load_contract("package.v1.json")).iter_errors(raw)
    )
    assert bool(errors) == (kind in {"object_acess", "plan_constraint"})


VISIBLE_ONLY = {"object_ids": ["metric.shop.revenue_usd"], "roles": ["finance"]}
MALFORMED_VISIBLE_ONLY = {
    "no_roles_or_audiences": ({"object_ids": VISIBLE_ONLY["object_ids"]}, "roles and/or audiences"),
    "blank_roles": ({**VISIBLE_ONLY, "roles": [" "]}, "roles and/or audiences"),
    "empty_object_ids": ({**VISIBLE_ONLY, "object_ids": []}, "non-empty object_ids"),
    "unknown_object_id": ({**VISIBLE_ONLY, "object_ids": ["metric.shop.revenue"]}, "unknown"),
    "other_key": ({**VISIBLE_ONLY, "except_roles": ["support"]}, "except_roles"),
}


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("name", MALFORMED_VISIBLE_ONLY)
def test_malformed_visible_only_policies_are_refused(tmp_path, starter, strict, name):
    fields, problem = MALFORMED_VISIBLE_ONLY[name]
    path = write_policy_package(
        tmp_path, starter, strict, "object_visibility", "visible_only", **fields
    )
    errors = validate_runtime_package(path)
    assert any(problem in error and "policy.test" in error for error in errors), errors
    with pytest.raises(SemanticLayerError, match=problem) as exc:
        load_package_config(str(path))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("location", ["action", "visibility"])
@pytest.mark.parametrize("scope", [{"roles": ["finance"]}, {"audiences": ["internal"]}])
def test_visible_only_policy_loads(tmp_path, starter, location, scope):
    fields = {"object_ids": VISIBLE_ONLY["object_ids"], **scope}
    path = write_policy_package(
        tmp_path, starter, True, "object_visibility", "visible_only", location, **fields
    )
    assert validate_runtime_package(path) == []
    assert load_package_config(str(path)).semantic_policies[0].object_ids == fields["object_ids"]


# name -> (package environments, the scoped row's section, its environments, refused)
ENVIRONMENT_SCOPES = {
    "policy_undeclared_name": (["production"], "semantic_policies", ["prod"], True),
    "caveat_undeclared_name": (["production"], "semantic_caveats", ["prod"], True),
    "policy_none_declared": ([], "semantic_policies", ["production"], True),
    "caveat_none_declared": ([], "semantic_caveats", ["production"], True),
    "policy_declared": (["production"], "semantic_policies", ["production"], False),
    "caveat_declared": (["production"], "semantic_caveats", ["production"], False),
    "policy_unscoped_none_declared": ([], "semantic_policies", [], False),
    "caveat_unscoped_none_declared": ([], "semantic_caveats", [], False),
}


@pytest.mark.parametrize("name", ENVIRONMENT_SCOPES)
def test_scoped_environments_must_be_declared_by_the_package(tmp_path, starter, name):
    environments, section, scoped, refused = ENVIRONMENT_SCOPES[name]
    raw = deepcopy(starter)
    raw["package"]["environments"] = environments
    row = (
        {"id": "policy.test", "kind": "object_visibility", "action": "hidden"}
        if section == "semantic_policies"
        else {"id": "caveat.test", "kind": "data_quality", "message": "Late data."}
    )
    raw[section] = [{**row, "object_ids": ["metric.shop.revenue_usd"], "environments": scoped}]
    path = tmp_path / "package.yml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    if not refused:
        assert load_package_config(str(path))
        return
    with pytest.raises(SemanticLayerError, match="package.environments") as exc:
        load_package_config(str(path))
    assert exc.value.code == "INVALID_CONFIG"
    assert any("package.environments" in error for error in validate_runtime_package(path))
