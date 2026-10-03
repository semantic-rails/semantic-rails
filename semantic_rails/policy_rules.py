"""The closed policy kind/action contract shared by loading and evaluation."""

from collections.abc import Mapping
from typing import Any

from .errors import SemanticLayerError
from .schema import SemanticPolicyConfig

# Authored action -> runtime effect. Empty actions are legal only for kinds
# with an implicit effect; row filters are enforced separately on base scans.
POLICY_ACTIONS = {
    "package_release": {"": "label", "label": "label"},
    "object_visibility": {"hidden": "hidden", "visible": "visible"},
    "object_access": {"deny": "deny", "redact": "redact"},
    "protected_object": {"": "protected", "protected": "protected"},
    "metric_constraint": {"": "constrain", "constrain": "constrain"},
    "row_filter": {"": ""},
}


def policy_config(policy: SemanticPolicyConfig) -> dict[str, Any]:
    """Return top-level policy config with legacy nested ``config:`` flattened."""
    out: dict[str, Any] = {}
    nested = policy.config.get("config") if isinstance(policy.config, dict) else None
    if isinstance(nested, Mapping):
        out.update(dict(nested))
    out.update({key: value for key, value in dict(policy.config or {}).items() if key != "config"})
    return out


def policy_action(policy: SemanticPolicyConfig) -> str:
    """Resolve a supported effect, or refuse an invalid policy with INVALID_CONFIG."""
    allowed = POLICY_ACTIONS.get(policy.kind)
    if allowed is None:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"policy '{policy.id}' has unknown kind {policy.kind!r}. "
            f"Valid kinds: {', '.join(POLICY_ACTIONS)}.",
        )
    config = policy_config(policy)
    action = (
        str(policy.action or config.get("action", "") or config.get("visibility", ""))
        .strip()
        .lower()
    )
    if action not in allowed:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"policy '{policy.id}' of kind {policy.kind!r} has unsupported action {action!r}. "
            f"Allowed actions: {', '.join(repr(value) for value in allowed)}.",
        )
    return allowed[action]
