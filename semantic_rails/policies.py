"""Visibility / access policy enforcement.

Evaluates each declared ``semantic_policy`` against the resolved
request context (``environment``, ``audience``) and returns either a
list of "applied" effects (for HTTP/MCP response payloads) or the set
of hidden object ids that ``catalog`` / ``discover`` / ``inspect``
should filter out. :func:`enforce_query_policies` is the runtime's gate
on ``validate`` / ``compile`` / ``execute``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .ast import normalize_query
from .errors import SemanticLayerError
from .expressions import expr_to_dict
from .schema import PackageConfig, SemanticPolicyConfig


def policy_effects_for_object(
    config: PackageConfig,
    object_id: str,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for policy in config.semantic_policies:
        if not _policy_matches(
            policy,
            object_id=object_id,
            environment=environment,
            audience=audience,
            roles=roles,
        ):
            continue
        action = _policy_action(policy)
        if not action:
            continue
        effects.append(_base_policy_effect(policy, action=action))
    return effects


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
        if _policy_matches(
            policy,
            object_id=object_id,
            environment=environment,
            audience=audience,
            roles=roles,
        )
        and _policy_action(policy) == "hidden"
    }


def query_policy_effects(
    config: PackageConfig,
    object_ids: Iterable[str],
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
    query: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for object_id in list(dict.fromkeys(str(item) for item in object_ids if str(item).strip())):
        for policy in config.semantic_policies:
            if not _policy_matches(
                policy,
                object_id=object_id,
                environment=environment,
                audience=audience,
                roles=roles,
            ):
                continue
            action = _policy_action(policy)
            if not action:
                continue
            if policy.kind == "metric_constraint" and query is not None:
                effects.append(_metric_constraint_effect(policy, query))
            else:
                effects.append(_base_policy_effect(policy, action=action))
    # Dedupe by (policy_id, kind, action). Package-wide policies — e.g.
    # release-label — attach to every referenced object and would
    # otherwise return N copies of the same effect for an N-object
    # query. Merge object_ids across duplicates so callers still see
    # which objects the policy bound. Caught by the blind-agent
    # benchmark, which saw `policy.jaffle.release_label` repeated 6x.
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for effect in effects:
        key = (
            str(effect.get("policy_id", "")),
            str(effect.get("kind", "")),
            str(effect.get("action", "")),
        )
        if key not in deduped:
            deduped[key] = dict(effect)
            deduped[key]["object_ids"] = list(effect.get("object_ids", []) or [])
            continue
        existing_ids = list(deduped[key].get("object_ids", []) or [])
        for object_id in list(effect.get("object_ids", []) or []):
            if object_id not in existing_ids:
                existing_ids.append(object_id)
        deduped[key]["object_ids"] = existing_ids
        existing_violations = list(deduped[key].get("violations", []) or [])
        for violation in list(effect.get("violations", []) or []):
            if violation not in existing_violations:
                existing_violations.append(violation)
        if existing_violations:
            deduped[key]["violations"] = existing_violations
    return list(deduped.values())


def enforce_query_policies(
    config: PackageConfig,
    object_ids: Iterable[str],
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
    query: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    effects = query_policy_effects(
        config,
        object_ids,
        environment=environment,
        audience=audience,
        roles=roles,
        query=query,
    )
    blocking = [row for row in effects if row["action"] in {"deny", "redact", "hidden"}]
    if blocking:
        raise SemanticLayerError(
            "POLICY_DENIED",
            "Query references a semantic object blocked by policy.",
            details={
                "blocked_objects": sorted(
                    {object_id for row in blocking for object_id in row.get("object_ids", [])}
                ),
                "policy_effects": blocking,
                "policy_violations": [
                    violation
                    for row in blocking
                    for violation in list(row.get("violations", []) or [])
                ],
            },
        )
    return effects


def package_release_labels(config: PackageConfig) -> list[str]:
    labels = []
    for policy in config.semantic_policies:
        if policy.kind == "package_release":
            policy_config = _policy_config(policy)
            label = str(policy_config.get("label", "") or policy.action or "").strip()
            if label:
                labels.append(label)
    return list(dict.fromkeys(labels))


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


def _policy_matches(
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


def _policy_action(policy: SemanticPolicyConfig) -> str:
    policy_config = _policy_config(policy)
    action = (
        str(policy.action or policy_config.get("action", "") or policy_config.get("visibility", ""))
        .strip()
        .lower()
    )
    if policy.kind == "object_visibility" and action in {"hidden", "visible"}:
        return action
    if policy.kind == "object_access" and action in {"deny", "redact"}:
        return action
    if policy.kind == "protected_object":
        return "protected"
    if policy.kind == "package_release":
        return "label"
    if policy.kind == "metric_constraint":
        return "constrain"
    return action


def _policy_config(policy: SemanticPolicyConfig) -> dict[str, Any]:
    """Return top-level policy config with legacy nested ``config:`` flattened."""
    out: dict[str, Any] = {}
    nested = policy.config.get("config") if isinstance(policy.config, dict) else None
    if isinstance(nested, Mapping):
        out.update(dict(nested))
    out.update({key: value for key, value in dict(policy.config or {}).items() if key != "config"})
    return out


def _policy_rationale(policy: SemanticPolicyConfig) -> str:
    policy_config = _policy_config(policy)
    return str(
        policy.rationale
        or policy_config.get("rule", "")
        or policy_config.get("rationale", "")
        or policy_config.get("description", "")
    )


def _base_policy_effect(policy: SemanticPolicyConfig, *, action: str) -> dict[str, Any]:
    effect: dict[str, Any] = {
        "policy_id": policy.id,
        "kind": policy.kind,
        "action": action,
        "rationale": _policy_rationale(policy),
        "object_ids": list(policy.object_ids),
        "audiences": list(policy.audiences),
        "environments": list(policy.environments),
        "roles": list(policy.roles),
    }
    if policy.kind == "metric_constraint":
        effect["constraints"] = _metric_constraint_summary(policy)
    return effect


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _config_str_list(policy_config: Mapping[str, Any], key: str) -> list[str]:
    return [text for item in _as_list(policy_config.get(key)) if (text := str(item).strip())]


def _metric_constraint_summary(policy: SemanticPolicyConfig) -> dict[str, Any]:
    policy_config = _policy_config(policy)
    summary_keys = (
        "required_group_by",
        "allowed_group_by",
        "required_where",
        "allowed_where",
        "allow_metric_filters",
        "allowed_metric_filter_entities",
        "allowed_metric_filter_metrics",
        "allowed_temporal_roles",
    )
    return {key: policy_config[key] for key in summary_keys if key in policy_config}


def _metric_constraint_effect(
    policy: SemanticPolicyConfig, query_payload: Mapping[str, Any]
) -> dict[str, Any]:
    violations = _metric_constraint_violations(policy, query_payload)
    effect = _base_policy_effect(policy, action="deny" if violations else "constrain")
    if violations:
        effect["violations"] = violations
    return effect


def _metric_constraint_violations(
    policy: SemanticPolicyConfig, query_payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    policy_config = _policy_config(policy)
    query = normalize_query(dict(query_payload or {}))
    violations: list[dict[str, Any]] = []

    if "required_group_by" in policy_config:
        required = _config_str_list(policy_config, "required_group_by")
        missing = [field for field in required if field not in set(query.group_by)]
        if missing:
            violations.append(
                {
                    "kind": "missing_required_group_by",
                    "missing": missing,
                    "present_group_by": list(query.group_by),
                }
            )

    if "allowed_group_by" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_group_by"))
        disallowed = [field for field in query.group_by if field not in allowed]
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_group_by",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                }
            )

    where_rows = [
        {"field": item.field, "op": item.op, "value": item.value} for item in list(query.where)
    ]
    if "required_where" in policy_config:
        required_filters = _normalize_where_specs(policy_config.get("required_where"))
        missing_filters = [
            spec
            for spec in required_filters
            if not any(_where_spec_matches(row, spec) for row in where_rows)
        ]
        if missing_filters:
            violations.append(
                {
                    "kind": "missing_required_where",
                    "missing": missing_filters,
                    "present_where": where_rows,
                }
            )

    if "allowed_where" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_where"))
        disallowed = [row["field"] for row in where_rows if row["field"] not in allowed]
        if disallowed:
            violations.append(
                {"kind": "disallowed_where", "disallowed": disallowed, "allowed": sorted(allowed)}
            )

    if "allowed_temporal_roles" in policy_config and query.time is not None:
        allowed = set(_config_str_list(policy_config, "allowed_temporal_roles"))
        if query.time.temporal_role and query.time.temporal_role not in allowed:
            violations.append(
                {
                    "kind": "disallowed_temporal_role",
                    "temporal_role": query.time.temporal_role,
                    "allowed": sorted(allowed),
                }
            )

    metric_filter_refs = _metric_filter_refs(query)
    if policy_config.get("allow_metric_filters") is False and metric_filter_refs:
        violations.append(
            {
                "kind": "metric_filters_not_allowed",
                "metric_filter_refs": metric_filter_refs,
            }
        )
    if "allowed_metric_filter_entities" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_metric_filter_entities"))
        disallowed = sorted(set(metric_filter_refs.get("entities", [])) - allowed)
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_metric_filter_entity",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                }
            )
    if "allowed_metric_filter_metrics" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_metric_filter_metrics"))
        disallowed = sorted(set(metric_filter_refs.get("metrics", [])) - allowed)
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_metric_filter_metric",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                }
            )

    return violations


def _normalize_where_specs(value: Any) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            spec = dict(item)
            field = str(spec.get("field", spec.get("dimension", "")) or "").strip()
            if field:
                spec["field"] = field
                specs.append(spec)
            continue
        field = str(item or "").strip()
        if field:
            specs.append({"field": field})
    return specs


def _where_spec_matches(row: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    if str(row.get("field", "")) != str(spec.get("field", "")):
        return False
    if "op" in spec and str(row.get("op", "")).upper() != str(spec.get("op", "")).upper():
        return False
    return "value" not in spec or row.get("value") == spec.get("value")


def _metric_filter_refs(query: Any) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {"entities": [], "metrics": [], "measures": []}
    for metric_filter in list(getattr(query, "metric_filters", []) or []):
        if metric_filter.expression is None:
            continue
        for object_id in _collect_expr_refs(expr_to_dict(metric_filter.expression)):
            if object_id.startswith("entity.") and object_id not in refs["entities"]:
                refs["entities"].append(object_id)
            elif object_id.startswith("metric.") and object_id not in refs["metrics"]:
                refs["metrics"].append(object_id)
            elif object_id.startswith("measure.") and object_id not in refs["measures"]:
                refs["measures"].append(object_id)
    return {key: value for key, value in refs.items() if value}


def _collect_expr_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, Mapping):
        for key in ("entity", "measure", "metric"):
            text = str(value.get(key, "") or "").strip()
            if text:
                refs.append(text)
        for child in value.values():
            refs.extend(_collect_expr_refs(child))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_expr_refs(item))
    return list(dict.fromkeys(refs))
