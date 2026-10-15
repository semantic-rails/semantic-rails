"""Authored policies, path preferences and semantic caveats."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..errors import SemanticLayerError
from ..policy_rules import policy_action
from ..schema import (
    DEFAULT_PATH_HOP_LIMIT,
    MAX_PATH_HOP_LIMIT,
    EntityConfig,
    PackageConfig,
    PathPolicyConfig,
    PathPreferenceConfig,
    RelationshipConfig,
    SemanticCaveatConfig,
)
from ..temporal_support import _date_key
from .route_rows import RouteRowError, check_route_row, entity_references, require_rows_agree
from .row_values import _ensure_dict_list, _ensure_list

_ALLOWED_CAVEAT_KINDS = frozenset({"business_event", "definition_change", "data_quality"})
_ALLOWED_CAVEAT_SEVERITIES = frozenset({"info", "warning"})


def _require_iso_date(value: Any, *, path: str) -> str:
    text = _date_key(value)
    if not text:
        raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a non-empty ISO date")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise SemanticLayerError("INVALID_CONFIG", f"{path} must be an ISO date") from exc
    return text


def _parse_caveat_time(value: Any, *, path: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"{path}.time must be an object")
    raw = dict(value or {})
    has_point = "at" in raw and str(raw.get("at", "") or "").strip()
    has_range = any(str(raw.get(key, "") or "").strip() for key in ("from", "to"))
    if has_point and has_range:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{path}.time must use either at or from/to, not both"
        )
    if not has_point and not has_range:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}.time must declare at or from/to")
    if has_point:
        return {"at": _require_iso_date(raw.get("at"), path=f"{path}.time.at")}
    out: dict[str, str] = {}
    if str(raw.get("from", "") or "").strip():
        out["from"] = _require_iso_date(raw.get("from"), path=f"{path}.time.from")
    if str(raw.get("to", "") or "").strip():
        out["to"] = _require_iso_date(raw.get("to"), path=f"{path}.time.to")
    if out.get("from") and out.get("to") and out["from"] >= out["to"]:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{path}.time.from must be before {path}.time.to"
        )
    return out


def _parse_caveats(rows: Any, *, path: str) -> list[SemanticCaveatConfig]:
    caveats: list[SemanticCaveatConfig] = []
    for index, row in enumerate(list(rows or [])):
        if not isinstance(row, dict):
            raise SemanticLayerError(
                "INVALID_CONFIG", f"{path}: semantic_caveats[{index}] must be an object"
            )
        row_dict = dict(row or {})
        caveat_path = f"{path}: caveat {row_dict.get('id', index)!r}"
        caveat_id = str(row_dict.get("id", "") or "").strip()
        kind = str(row_dict.get("kind", "") or "").strip().lower()
        message = str(row_dict.get("message", "") or "").strip()
        severity = str(row_dict.get("severity", "warning") or "warning").strip().lower()
        if not caveat_id:
            raise SemanticLayerError("INVALID_CONFIG", f"{caveat_path} must declare id")
        if kind not in _ALLOWED_CAVEAT_KINDS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{caveat_path} kind must be one of {sorted(_ALLOWED_CAVEAT_KINDS)}",
            )
        if not message:
            raise SemanticLayerError("INVALID_CONFIG", f"{caveat_path} must declare message")
        if severity not in _ALLOWED_CAVEAT_SEVERITIES:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{caveat_path} severity must be one of {sorted(_ALLOWED_CAVEAT_SEVERITIES)}",
            )
        caveats.append(
            SemanticCaveatConfig(
                id=caveat_id,
                kind=kind,
                message=message,
                object_ids=_ensure_list(row_dict.get("object_ids")),
                entity_values=_ensure_dict_list(row_dict.get("entity_values")),
                time=_parse_caveat_time(row_dict.get("time"), path=caveat_path),
                audiences=_ensure_list(row_dict.get("audiences")),
                environments=_ensure_list(row_dict.get("environments")),
                severity=severity,
                owner=str(row_dict.get("owner", "") or ""),
                references=_ensure_dict_list(row_dict.get("references")),
            )
        )
    return caveats


def _parse_path_policy(raw: dict[str, Any], *, path: str) -> PathPolicyConfig:
    policy_raw = raw.get("path_policy")
    if policy_raw is None:
        return PathPolicyConfig()
    if not isinstance(policy_raw, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: path_policy must be a mapping (got {type(policy_raw).__name__})",
        )
    try:
        max_hops = int(policy_raw.get("max_hops", DEFAULT_PATH_HOP_LIMIT))
    except (TypeError, ValueError):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: path_policy.max_hops must be an integer",
        ) from None
    if not 1 <= max_hops <= MAX_PATH_HOP_LIMIT:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: path_policy.max_hops must be between 1 and {MAX_PATH_HOP_LIMIT} "
            f"(got {max_hops}). Past {MAX_PATH_HOP_LIMIT} hops, author a mapping table "
            "or aggregate relation instead of a longer join chain.",
        )
    return PathPolicyConfig(max_hops=max_hops)


def _parse_path_preferences(
    raw: dict[str, Any],
    *,
    entities: list[EntityConfig],
    entity_lookup: dict[str, str],
    relationships: list[RelationshipConfig],
    path: str,
) -> list[PathPreferenceConfig]:
    """Parse and validate ``path_preferences`` rows.

    Every row is validated eagerly: unknown entities, unknown relationship
    ids, and paths that don't actually connect source to target are
    INVALID_CONFIG at load time. A pinned path that fails only at query
    time (or worse, is silently dropped) defeats the point of pinning.
    """
    rows = list(raw.get("path_preferences", []) or [])
    if not rows:
        return []
    # An entity by key, name or id: an AMBIGUOUS_PATH refusal's decisions name entity ids.
    references = entity_references(entities, entity_lookup)
    out: list[PathPreferenceConfig] = []
    for row in rows:
        try:
            out.append(
                check_route_row(dict(row or {}), entities=references, relationships=relationships)
            )
        except RouteRowError as exc:
            raise SemanticLayerError("INVALID_CONFIG", f"{path}: path_preferences {exc}") from None
    require_rows_agree({rel.id: rel for rel in relationships}, out, path=path)
    return out


def _package_object_ids(config: PackageConfig) -> set[str]:
    return {
        *[row.id for row in config.entities],
        *[row.id for row in config.dimensions],
        *[row.id for row in config.temporal_roles],
        *[row.id for row in config.relationships],
        *[row.id for row in config.value_domains],
        *[row.id for row in config.measures],
        *[row.id for row in config.metric_recipes],
        *[row.id for row in config.segments],
    }


def _validate_policy_scopes(config: PackageConfig, *, path: str) -> None:
    """Policy and caveat environments are the package's own; visible_only names real objects.

    An environment spelled unlike the one requests carry would never put its row in force."""
    declared = set(config.package.environments or [])
    for label, row_id, environments in [
        *(("policy", row.id, row.environments) for row in config.semantic_policies),
        *(("caveat", row.id, row.environments) for row in config.semantic_caveats),
    ]:
        undeclared = sorted(set(environments) - declared)
        if undeclared:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: {label} {row_id} environments {undeclared} are not declared in "
                f"package.environments ({sorted(declared) or 'none declared'})",
            )
    object_ids = _package_object_ids(config)
    for policy in config.semantic_policies:
        unknown = sorted(set(policy.object_ids) - object_ids)
        if unknown and policy_action(policy) == "visible_only":
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: policy {policy.id} references unknown object_ids {unknown}",
            )


def _validate_caveat_refs(config: PackageConfig, *, path: str) -> None:
    object_ids = _package_object_ids(config)
    entity_ids = {row.id for row in config.entities}
    dimension_ids = {row.id for row in config.dimensions}
    for caveat in config.semantic_caveats:
        if not caveat.object_ids and not caveat.entity_values:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: caveat {caveat.id} must declare object_ids or entity_values",
            )
        for object_id in caveat.object_ids:
            if object_id not in object_ids:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: caveat {caveat.id} references unknown object_id '{object_id}'",
                )
        for index, row in enumerate(caveat.entity_values):
            entity = str(row.get("entity", "") or "").strip()
            dimension = str(row.get("dimension", "") or "").strip()
            if entity not in entity_ids:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: caveat {caveat.id} entity_values[{index}].entity references unknown entity '{entity}'",
                )
            if dimension not in dimension_ids:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: caveat {caveat.id} entity_values[{index}].dimension references unknown dimension '{dimension}'",
                )
            if "value" not in row and "values" not in row:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: caveat {caveat.id} entity_values[{index}] must declare value or values",
                )
