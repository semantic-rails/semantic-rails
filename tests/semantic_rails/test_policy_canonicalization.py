"""Temporary evaluator-read proof for the loader used to verify upgrade rules."""

from types import SimpleNamespace

import pytest

from semantic_rails.config import _canonical_policy_config
from semantic_rails.policies import package_release_labels
from semantic_rails.policy_rules import _policy_rationale, authored_policy_action, policy_config
from semantic_rails.schema import SemanticPolicyConfig


@pytest.mark.parametrize(
    ("kind", "action", "rationale", "fields"),
    [
        ("object_access", " Deny ", "", {}),
        *[
            ("object_access", "", "", {"config": {key: value}})
            for key in ("action", "visibility", "rationale", "rule", "description")
            for value in ("", None, False, "deny")
        ],
        *[
            ("object_access", "", "", {key: value})
            for key in ("visibility", "rule", "description")
            for value in ("", None, False, "deny")
        ],
        *[
            ("object_access", "deny", "finance", {"config": {key: ["external"]}})
            for key in ("id", "kind", "object_ids", "audiences", "environments", "roles")
        ],
        (
            "object_access",
            "deny",
            "finance",
            {"config": {"action": "deny", "rationale": "finance"}},
        ),
        (
            "object_access",
            "deny",
            "finance",
            {
                "visibility": "hidden",
                "rule": "sales",
                "config": {"action": "withhold_values", "rationale": "operations"},
            },
        ),
        ("object_access", "withhold_values", "", {"config": {"max_rank": 3}}),
        ("object_access", "withhold_values", "", {"max_rank": 3, "config": {"max_rank": 5}}),
        *[
            ("metric_constraint", "", "", {"config": {key: value}})
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
        *[
            ("package_release", action, "", fields)
            for action in ("", "label", " Label ")
            for fields in (
                {},
                {"label": ""},
                {"config": {"label": "stable"}},
                {"config": {"action": "label"}},
            )
        ],
        (
            "row_filter",
            "",
            "",
            {"config": {"dimension": "customer", "attribute": "tenant"}, "rule": "tenant"},
        ),
    ],
)
def test_canonical_policy_preserves_every_evaluator_read(kind, action, rationale, fields):
    row = SemanticPolicyConfig(
        "policy.test",
        kind,
        action=action,
        rationale=rationale,
        config=fields,
        object_ids=["measure.revenue"],
        audiences=["finance"],
        environments=["production"],
        roles=["analyst"],
    )

    def reads(policy):
        values = policy.config if policy.kind == "row_filter" else policy_config(policy)
        return (
            {
                key: value
                for key, value in values.items()
                if key not in {"action", "visibility", "rule", "description", "rationale"}
            },
            authored_policy_action(policy),
            _policy_rationale(policy),
            policy.object_ids,
            policy.audiences,
            policy.environments,
            policy.roles,
            package_release_labels(SimpleNamespace(semantic_policies=[policy])),
        )

    assert reads(row) == reads(_canonical_policy_config(row))
