"""Operational-contract overlays attached to measures and metrics.

Operational contracts are author-declared metadata blocks (owner, SLA,
runbook url, etc.) packages can attach to measures or metrics under
``defaults.operational``. This module loads, validates, normalizes, and
merges those overlays, and is responsible for the ``operational_payload``
that ``inspect`` surfaces.
"""

from __future__ import annotations

from typing import Any

from .errors import SemanticLayerError

SUPPORTED_OPERATIONAL_TARGETS = {"measure", "metric"}
SUPPORTED_OPERATIONAL_TYPES = {"string", "string_list", "boolean", "enum"}


def load_operational_contract(defaults: dict[str, Any], *, path: str) -> dict[str, Any]:
    raw = defaults.get("operational")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{path}: defaults.operational must be a mapping"
        )
    contract: dict[str, Any] = {}
    for target, target_spec_raw in dict(raw).items():
        target_name = str(target)
        if target_name not in SUPPORTED_OPERATIONAL_TARGETS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: defaults.operational target '{target_name}' is not supported",
            )
        if not isinstance(target_spec_raw, dict):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: defaults.operational.{target_name} must be a mapping",
            )
        target_spec = dict(target_spec_raw)
        fields_raw = target_spec.get("fields", {})
        if not isinstance(fields_raw, dict):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: defaults.operational.{target_name}.fields must be a mapping",
            )
        fields: dict[str, Any] = {}
        for field_name, field_spec_raw in dict(fields_raw).items():
            field_key = str(field_name)
            if not isinstance(field_spec_raw, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: defaults.operational.{target_name}.fields.{field_key} must be a mapping",
                )
            field_spec = dict(field_spec_raw)
            field_type = str(field_spec.get("type", "")).strip()
            if field_type not in SUPPORTED_OPERATIONAL_TYPES:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: defaults.operational.{target_name}.fields.{field_key} has unsupported type '{field_type}'",
                )
            normalized = {
                "type": field_type,
                "required": bool(field_spec.get("required", False)),
            }
            if field_type == "enum":
                values = [str(value) for value in list(field_spec.get("values", []) or [])]
                if not values:
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"{path}: defaults.operational.{target_name}.fields.{field_key} enum must declare values",
                    )
                normalized["values"] = values
            fields[field_key] = normalized
        contract[target_name] = {
            "allow_unknown_fields": bool(target_spec.get("allow_unknown_fields", False)),
            "fields": fields,
        }
    return contract


def normalize_operational_payload(
    raw: Any,
    *,
    contract: dict[str, Any],
    target: str,
    path: str,
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a mapping")
    payload = {str(key): value for key, value in dict(raw).items()}
    if payload and target not in contract:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path} is not allowed because defaults.operational.{target} is not declared",
        )
    return payload


def merge_operational_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        for key, value in dict(payload or {}).items():
            merged[str(key)] = list(value) if isinstance(value, list) else value
    return merged


def validate_operational_payload(
    payload: dict[str, Any],
    *,
    contract: dict[str, Any],
    target: str,
    path: str,
) -> dict[str, Any]:
    target_contract = dict(contract.get(target, {}) or {})
    if not target_contract:
        if payload:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path} is not allowed because defaults.operational.{target} is not declared",
            )
        return {}

    fields = dict(target_contract.get("fields", {}) or {})
    allow_unknown_fields = bool(target_contract.get("allow_unknown_fields", False))
    normalized: dict[str, Any] = {}

    for key, value in dict(payload or {}).items():
        field_spec = fields.get(str(key))
        if field_spec is None:
            if not allow_unknown_fields:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}.{key} is not declared in defaults.operational.{target}.fields",
                )
            normalized[str(key)] = value
            continue
        normalized[str(key)] = _validate_field_value(
            value,
            field_spec=dict(field_spec),
            path=f"{path}.{key}",
        )

    missing = [
        key
        for key, field_spec in fields.items()
        if bool(dict(field_spec).get("required", False))
        and _is_missing_value(normalized.get(str(key)))
    ]
    if missing:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path} is missing required operational fields: {', '.join(sorted(missing))}",
        )
    return normalized


def operational_payload(obj: Any) -> dict[str, Any]:
    return dict(getattr(obj, "operational", {}) or {})


def _validate_field_value(value: Any, *, field_spec: dict[str, Any], path: str) -> Any:
    field_type = str(field_spec.get("type", ""))
    if field_type == "string":
        if not isinstance(value, str):
            raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a string")
        return value
    if field_type == "string_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a list of strings")
        return list(value)
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a boolean")
        return value
    if field_type == "enum":
        if not isinstance(value, str):
            raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a string")
        allowed = list(field_spec.get("values", []) or [])
        if value not in allowed:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path} must be one of {', '.join(allowed)}",
            )
        return value
    raise SemanticLayerError(
        "INVALID_CONFIG", f"{path} has unsupported operational type '{field_type}'"
    )


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False
