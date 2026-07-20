"""Meta-key contract for free-form ``meta:`` blocks on measures/metrics.

Authors can declare an optional ``defaults.meta.<target>.fields`` block in
``package.yml`` that lists the allowed meta keys (and their types) for
measures, metrics, and relations. Unknown keys do NOT raise — they emit
**warnings** through :func:`validate_meta_payload`, which the package
compiler surfaces via ``_compiled_package_warnings``.

This mirrors :mod:`semantic_rails.operational` in shape (same field types,
same ``allow_unknown_fields`` toggle), but is strictly advisory. When no
contract is declared (the common case today), payloads pass through
untouched — load-bearing meta keys (e.g. MNPI gates, rollup_sketch) keep
working.
"""

from __future__ import annotations

from typing import Any

from .errors import SemanticLayerError

SUPPORTED_META_TARGETS = {"measure", "metric", "relation"}
SUPPORTED_META_TYPES = {"string", "string_list", "boolean", "enum"}


def load_meta_contract(defaults: dict[str, Any], *, path: str) -> dict[str, Any]:
    """Parse ``defaults.meta`` into a normalized contract dict.

    Returns ``{}`` when no contract is declared. Raises ``INVALID_CONFIG``
    only for malformed contract declarations (the contract itself must
    be well-formed); unknown payload keys produce warnings, not errors.
    """
    raw = defaults.get("meta")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: defaults.meta must be a mapping")
    contract: dict[str, Any] = {}
    for target, target_spec_raw in dict(raw).items():
        target_name = str(target)
        if target_name not in SUPPORTED_META_TARGETS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: defaults.meta target '{target_name}' is not supported",
            )
        if not isinstance(target_spec_raw, dict):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: defaults.meta.{target_name} must be a mapping",
            )
        target_spec = dict(target_spec_raw)
        fields_raw = target_spec.get("fields", {})
        if not isinstance(fields_raw, dict):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: defaults.meta.{target_name}.fields must be a mapping",
            )
        fields: dict[str, Any] = {}
        for field_name, field_spec_raw in dict(fields_raw).items():
            field_key = str(field_name)
            if not isinstance(field_spec_raw, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: defaults.meta.{target_name}.fields.{field_key} must be a mapping",
                )
            field_spec = dict(field_spec_raw)
            field_type = str(field_spec.get("type", "")).strip()
            if field_type not in SUPPORTED_META_TYPES:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: defaults.meta.{target_name}.fields.{field_key} has unsupported type '{field_type}'",
                )
            normalized: dict[str, Any] = {"type": field_type}
            if field_type == "enum":
                values = [str(value) for value in list(field_spec.get("values", []) or [])]
                if not values:
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"{path}: defaults.meta.{target_name}.fields.{field_key} enum must declare values",
                    )
                normalized["values"] = values
            fields[field_key] = normalized
        contract[target_name] = {
            "allow_unknown_fields": bool(target_spec.get("allow_unknown_fields", False)),
            "fields": fields,
        }
    return contract


def validate_meta_payload(
    contract: dict[str, Any],
    target: str,
    payload: dict[str, Any],
    *,
    path: str,
) -> list[str]:
    """Compare a meta payload against the contract; return warning strings.

    Never raises. When the contract is empty for ``target``, returns ``[]``
    (no contract == no opinion). With a contract:
      * unknown key + ``allow_unknown_fields: false`` → 1 warning
      * type mismatch → 1 warning
      * enum value not in declared values → 1 warning
    """
    target_contract = dict(contract.get(target, {}) or {})
    if not target_contract:
        return []
    fields = dict(target_contract.get("fields", {}) or {})
    allow_unknown = bool(target_contract.get("allow_unknown_fields", False))
    warnings: list[str] = []
    for key, value in dict(payload or {}).items():
        field_spec = fields.get(str(key))
        if field_spec is None:
            if not allow_unknown:
                warnings.append(f"{path}.{key} is not declared in defaults.meta.{target}.fields")
            continue
        warning = _validate_field_value(value, field_spec=dict(field_spec), path=f"{path}.{key}")
        if warning:
            warnings.append(warning)
    return warnings


def _validate_field_value(value: Any, *, field_spec: dict[str, Any], path: str) -> str | None:
    field_type = str(field_spec.get("type", ""))
    if field_type == "string":
        if not isinstance(value, str):
            return f"{path} should be a string"
        return None
    if field_type == "string_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return f"{path} should be a list of strings"
        return None
    if field_type == "boolean":
        if not isinstance(value, bool):
            return f"{path} should be a boolean"
        return None
    if field_type == "enum":
        if not isinstance(value, str):
            return f"{path} should be a string"
        allowed = list(field_spec.get("values", []) or [])
        if value not in allowed:
            return f"{path} value '{value}' is not in declared values {allowed}"
        return None
    return None
