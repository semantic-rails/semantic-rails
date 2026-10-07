"""Golden policy rewrites, conflict stops and idempotence."""

import pytest

from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.registry import RULES
from semantic_rails.upgrade.rules_canonical import RULES as POLICY_RULES
from semantic_rails.yaml_loader import safe_load


@pytest.mark.parametrize(
    ("before", "after", "rule"),
    [
        ("  config:\n    label: stable\n", "  label: stable\n", "policy-flat"),
        (
            "  config: {max_rank: 3}\n  action: withhold_values\n",
            "  action: withhold_values\n  max_rank: 3\n",
            "policy-flat",
        ),
        (
            "  visibility: hidden\n  rule: finance only\n",
            "  action: hidden\n  rationale: finance only\n",
            "policy-flat",
        ),
        (
            "  config: {action: hidden, rationale: finance only}\n",
            "  action: hidden\n  rationale: finance only\n",
            "policy-flat",
        ),
        (
            "  description: finance only\n  action: deny\n",
            "  action: deny\n  rationale: finance only\n",
            "policy-flat",
        ),
        (
            "  action: redact # refused access\n",
            "  action: deny # refused access\n",
            "policy-redact-deny",
        ),
    ],
)
def test_policy_golden_rewrite(tmp_path, before, after, rule):
    header = "semantic_policies:\n- id: policy.test\n  kind: object_access\n"
    source = tmp_path / "pkg.yml"
    source.write_text(header + before)
    files = PackageFiles(source)
    result = plan(files, POLICY_RULES, {})
    assert [finding.rule for finding in result.findings] == [rule]
    assert result.files["pkg.yml"].decode() == header + after
    upgraded = PackageFiles(source, contents={**files.contents, **result.files})
    assert plan(upgraded, RULES, {}).findings == ()
    assert all(rule.effect == "same_meaning" and not rule.masks for rule in POLICY_RULES)


@pytest.mark.parametrize(
    "body",
    [
        "- {id: policy.test, kind: object_access, action: redact}\n",
        "- {id: policy.test, kind: object_access, config: {action: redact}}\n",
        "- {id: policy.test, kind: package_release, label: stable, config: {label: stable}}\n",
    ],
)
def test_flow_policy_rewrites_are_idempotent(tmp_path, body):
    source = tmp_path / "pkg.yml"
    source.write_text("semantic_policies:\n" + body)
    files = PackageFiles(source)
    result = plan(files, POLICY_RULES, {})
    assert result.findings and not result.pending
    upgraded = PackageFiles(source, contents={**files.contents, **result.files})
    assert plan(upgraded, RULES, {}).findings == ()


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        *[
            (f"config: {{{key}: []}}", key)
            for key in ("id", "kind", "object_ids", "audiences", "environments", "roles")
        ],
        ("max_rank: 3, config: {max_rank: 5}", "max_rank"),
        ("action: deny, config: {action: withhold_values}", "action"),
        ("action: deny, visibility: withhold_values", "visibility"),
        ("config: {action: deny, visibility: withhold_values}", "visibility"),
        ("rationale: finance, rule: sales", "rule"),
        ("rationale: finance, description: sales", "description"),
        ("rule: finance, config: {rationale: sales}", "rationale"),
        ("description: finance, config: {description: sales}", "description"),
        ("rationale: '', config: {rationale: null}", "rationale"),
        ("config: null", "config"),
    ],
)
def test_conflicting_policy_values_are_stops(tmp_path, fields, key):
    source = tmp_path / "pkg.yml"
    source.write_text(f"semantic_policies:\n- {{id: policy.test, kind: object_access, {fields}}}\n")
    result = plan(PackageFiles(source), POLICY_RULES, {})
    assert len(result.pending) == 1 and not result.files
    finding = result.pending[0]
    assert not finding.edits and not finding.options
    assert "policy.test" in finding.message and key in finding.message


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"config": {}}, {}),
        ({"config": {"action": ""}}, {}),
        ({"config": {"action": False}}, {}),
        ({"config": {"action": None}}, {}),
        ({"config": {"visibility": None}}, {"action": "none"}),
        ({"config": {"visibility": False}}, {"action": "false"}),
        ({"config": {"rationale": None}}, {}),
        ({"config": {"rationale": False}}, {}),
        ({"action": None, "config": {}}, {"action": "none"}),
        ({"action": False, "config": {}}, {"action": "false"}),
        ({"rationale": None, "config": {}}, {"rationale": "None"}),
        ({"rationale": False, "config": {}}, {"rationale": "False"}),
        ({"rule": None}, {"rationale": "None"}),
        ({"description": False}, {"rationale": "False"}),
        ({"config": {"action": "deny"}, "action": "deny"}, {"action": "deny"}),
        (
            {"rationale": "finance", "rule": "finance", "config": {"rationale": "finance"}},
            {"rationale": "finance"},
        ),
        ({"max_rank": 3, "config": {"max_rank": 3}}, {"max_rank": 3}),
        *[
            ({"config": {key: value}}, {key: value})
            for key, value in (
                ("required_group_by", []),
                ("allowed_group_by", []),
                ("required_where", []),
                ("allowed_where", []),
                ("allow_metric_filters", False),
                ("allowed_metric_filter_entities", []),
                ("allowed_metric_filter_metrics", []),
                ("allowed_temporal_roles", []),
            )
        ],
    ],
)
def test_policy_alias_values_and_presence_are_preserved(tmp_path, fields, expected):
    import yaml

    source = tmp_path / "pkg.yml"
    header = {"id": "policy.test", "kind": "metric_constraint"}
    source.write_text(yaml.safe_dump({"semantic_policies": [{**header, **fields}]}))
    files = PackageFiles(source)
    result = plan(files, POLICY_RULES, {})
    assert result.findings and not result.pending
    assert safe_load(result.files["pkg.yml"])["semantic_policies"] == [{**header, **expected}]
    assert not plan(
        PackageFiles(source, contents={**files.contents, **result.files}), POLICY_RULES, {}
    ).findings


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ("config: {}", {}),
        ("label: '', config: {}", {"label": ""}),
        ("action: label, config: {}", {"action": "label"}),
        ("config: {label: stable}", {"label": "stable"}),
        ("label: stable, config: {action: label}", {"label": "stable", "action": "label"}),
        ("config: {action: label}", None),
        ("label: '', config: {action: label}", None),
        ("action: Label, config: {}", None),
    ],
)
def test_release_rewrites_do_not_invent_labels(tmp_path, fields, expected):
    source = tmp_path / "pkg.yml"
    header = {"id": "policy.test", "kind": "package_release"}
    source.write_text(
        f"semantic_policies:\n- {{id: policy.test, kind: package_release, {fields}}}\n"
    )
    result = plan(PackageFiles(source), POLICY_RULES, {})
    if expected is None:
        assert len(result.pending) == 1 and not result.files
        assert not result.pending[0].edits and not result.pending[0].options
        assert "action" in result.pending[0].message
    else:
        assert not result.pending
        assert safe_load(result.files["pkg.yml"])["semantic_policies"] == [{**header, **expected}]


def test_nested_row_filter_is_not_flattened(tmp_path):
    source = tmp_path / "pkg.yml"
    source.write_text(
        "semantic_policies:\n- {id: policy.test, kind: row_filter, config: {dimension: dimension.shop.customer, attribute: customer}}\n"
    )
    assert plan(PackageFiles(source), POLICY_RULES, {}).findings == ()
