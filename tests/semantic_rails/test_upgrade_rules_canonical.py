"""Golden policy rewrites, precedence choices and idempotence."""

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


@pytest.mark.parametrize("option", ["flat", "nested"])
def test_conflicting_policy_values_require_a_choice(tmp_path, option):
    source = tmp_path / "pkg.yml"
    source.write_text(
        "semantic_policies:\n- id: policy.test\n  kind: object_access\n"
        "  action: withhold_values\n  max_rank: 3\n  config: {max_rank: 5}\n"
    )
    files = PackageFiles(source)
    pending = plan(files, POLICY_RULES, {})
    assert len(pending.pending) == 1 and not pending.files
    finding = pending.pending[0]
    result = plan(files, POLICY_RULES, {files.choice_key(finding): option})
    row = safe_load(result.files["pkg.yml"])["semantic_policies"][0]
    assert row["max_rank"] == (3 if option == "flat" else 5)
    assert "config" not in row
    assert (
        plan(PackageFiles(source, contents={**files.contents, **result.files}), RULES, {}).findings
        == ()
    )


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
