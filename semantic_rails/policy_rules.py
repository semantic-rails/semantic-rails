"""The closed policy kind/action contract shared by loading and evaluation."""

from collections.abc import Iterable, Mapping
from typing import Any

from .errors import SemanticLayerError
from .schema import PackageConfig, SemanticPolicyConfig

# Authored action -> runtime effect. Empty actions are legal only for kinds
# with an implicit effect; row filters are enforced separately on base scans.
POLICY_ACTIONS = {
    "package_release": {"": "label", "label": "label"},
    "object_visibility": {"hidden": "hidden", "visible_only": "visible_only"},
    "object_access": {"deny": "deny", "redact": "redact", "withhold_values": "withhold_values"},
    "protected_object": {"": "protected", "protected": "protected"},
    "metric_constraint": {"": "constrain", "constrain": "constrain"},
    "row_filter": {"": ""},
}
DEFAULT_MAX_RANK = 10
MAX_RANK = 100
# Besides object_ids, roles, audiences and environments, a visible_only policy takes only
# its action and rationale text: an ignored key could narrow whom it names.
VISIBLE_ONLY_KEYS = {"action", "visibility", "rule", "rationale", "description"}


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
    if allowed[action] == "visible_only":
        _check_visible_only(policy)
    if allowed[action] == "hidden" and not _names(policy.object_ids):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"policy '{policy.id}' with action 'hidden' takes non-empty object_ids.",
        )
    return allowed[action]


def _names(values: Iterable[str] | None) -> set[str]:
    return {str(value).strip().lower() for value in list(values or []) if str(value).strip()}


def _check_visible_only(policy: SemanticPolicyConfig) -> None:
    """The objects and whom they are visible to; an empty list would name everyone."""
    extra = sorted(set(policy_config(policy)) - VISIBLE_ONLY_KEYS)
    if extra or not _names(policy.object_ids) or not _names([*policy.roles, *policy.audiences]):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"policy '{policy.id}' with action 'visible_only' takes non-empty object_ids and "
            "roles and/or audiences"
            + (f", and no other keys (got {', '.join(extra)})" if extra else "")
            + ".",
        )


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


def check_request_environment(config: PackageConfig, environment: str) -> None:
    """A supplied environment must be declared before any policy is evaluated.

    The boundary refuses early; evaluator guards cover bypasses (direct calls, positional payloads).
    """
    declared = list(config.package.environments or [])
    if environment and environment not in declared:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Request environment {environment!r} is not declared for this package. "
            f"Declared environments: {', '.join(declared) or 'none'}.",
            details={"environment": environment, "allowed_environments": declared},
        )


def role_scope_matches(allowed: Iterable[str], roles: Iterable[str] | None) -> bool:
    allowed_set = _names(allowed)
    return not allowed_set or bool(allowed_set & _names(roles))


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


def hidden_policy_ids(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> set[str]:
    """Objects a matching ``hidden`` policy lists; ``visible_view.hidden_object_ids`` is the
    complete set."""
    check_request_environment(config, environment)
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


def visible_only_listed(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> set[str]:
    """Objects an in-force ``visible_only`` policy keeps from this context, before dependents.

    In force: no ``environments``, or the context's environment is listed or blank.
    Eligible: one of ``roles`` when any are listed, and
    the audience when ``audiences`` are listed. An object listed by several policies needs all.
    """
    check_request_environment(config, environment)
    listed: set[str] = set()
    for policy in config.semantic_policies:
        if policy_action(policy) != "visible_only":
            continue
        if environment and not context_scope_matches(policy.environments, environment):
            continue
        if not (
            role_scope_matches(policy.roles, roles)
            and context_scope_matches(policy.audiences, audience)
        ):
            listed.update(policy.object_ids)
    return listed
