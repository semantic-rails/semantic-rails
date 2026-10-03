"""The closed policy kind/action contract shared by loading and evaluation."""

from collections.abc import Iterable, Mapping
from types import EllipsisType
from typing import Any

from .errors import SemanticLayerError
from .schema import PackageConfig, SemanticPolicyConfig

# Authored action -> runtime effect. Empty actions are legal only for kinds
# with an implicit effect; row filters are enforced separately on base scans.
POLICY_ACTIONS = {
    "package_release": {"": "label", "label": "label"},
    "object_visibility": {"hidden": "hidden", "visible": "visible"},
    "object_access": {"deny": "deny", "redact": "redact", "withhold_values": "withhold_values"},
    "protected_object": {"": "protected", "protected": "protected"},
    "metric_constraint": {"": "constrain", "constrain": "constrain"},
    "row_filter": {"": ""},
}
DEFAULT_MAX_RANK = 10
MAX_RANK = 100


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
    if allowed[action] == "withhold_values":
        withheld_max_rank(policy)
    return allowed[action]


def withheld_max_rank(policy: SemanticPolicyConfig) -> int:
    """The most rows a rank by a withheld object may return: ``config.max_rank``, 1 to 100."""
    value = policy_config(policy).get("max_rank", DEFAULT_MAX_RANK)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RANK:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"policy '{policy.id}' max_rank must be an integer from 1 to {MAX_RANK}; "
            f"got {value!r}.",
        )
    return value


def context_scope_matches(allowed: Iterable[str], value: str) -> bool:
    """Shared audience/environment gate for policies and caveats.

    An empty ``allowed`` list matches any context; a non-empty list
    requires the context value to be present and listed. Keeping this
    in one place stops policy and caveat scoping from drifting apart.
    """
    allowed_set = set(allowed or [])
    return not allowed_set or (bool(value) and value in allowed_set)


def role_scope_matches(allowed: Iterable[str], roles: Iterable[str] | None) -> bool:
    allowed_set = {str(role).strip().lower() for role in list(allowed or []) if str(role).strip()}
    if not allowed_set:
        return True
    role_set = {str(role).strip().lower() for role in list(roles or []) if str(role).strip()}
    return bool(allowed_set & role_set)


def policy_matches(
    policy: SemanticPolicyConfig,
    *,
    object_id: str,
    environment: str,
    audience: str,
    roles: Iterable[str] | None = None,
) -> bool:
    if policy.object_ids and object_id not in set(policy.object_ids):
        return False
    if not context_scope_matches(policy.environments, environment):
        return False
    if not context_scope_matches(policy.audiences, audience):
        return False
    return role_scope_matches(policy.roles, roles)


def hidden_object_ids(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> set[str]:
    return {
        object_id
        for policy in config.semantic_policies
        for object_id in list(policy.object_ids or [])
        if policy_matches(
            policy,
            object_id=object_id,
            environment=environment,
            audience=audience,
            roles=roles,
        )
        and policy_action(policy) == "hidden"
    }


def visible_object_ids(
    config: PackageConfig,
    object_ids: Iterable[str],
    *,
    hidden_ids: frozenset[str] | None | EllipsisType = ...,
) -> list[str]:
    """Filter resolved candidates; uncertain visibility withholds every alternative."""
    if isinstance(hidden_ids, EllipsisType):
        try:
            hidden_ids = frozenset(hidden_object_ids(config))
        except Exception:  # noqa: BLE001 — uncertain visibility cannot authorize disclosure
            hidden_ids = None
    return [
        object_id
        for object_id in object_ids
        if hidden_ids is not None and object_id not in hidden_ids
    ]
