"""Aggregate relation variants and rollup column semantics."""

from __future__ import annotations

from typing import Any

from ..errors import SemanticLayerError

# What a rollup's measure column may hold per row (`holds:`); see acceleration/selection.py.
_ROLLUP_HOLDS = frozenset({"sum", "min", "max", "count_distinct"})


def _merge_variant_spec(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    merged = dict(parent or {})
    for key, value in dict(child or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key] or {})
            nested.update(dict(value or {}))
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _resolve_variant_specs(
    variants: dict[str, Any], *, path: str, model_id: str
) -> dict[str, dict[str, Any]]:
    raw = {str(key): dict(value or {}) for key, value in dict(variants or {}).items()}
    resolved: dict[str, dict[str, Any]] = {}
    resolving: set[str] = set()

    def resolve(name: str) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        if name not in raw:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' variant '{name}' is referenced but not declared",
            )
        if name in resolving:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' variants contain an inheritance cycle at '{name}'",
            )
        resolving.add(name)
        spec = dict(raw[name])
        label = f"{path}: model '{model_id}' variant '{name}'"
        for key in ("grain", "time", "excludes", "selection", "equivalence"):
            if key in spec and not isinstance(spec[key], dict):
                raise SemanticLayerError("INVALID_CONFIG", f"{label} {key} must be a mapping")
        parent_name = str(spec.get("inherits_from", "") or "").strip()
        if parent_name:
            spec = _merge_variant_spec(resolve(parent_name), spec)
        resolving.remove(name)
        resolved[name] = spec
        return spec

    for name in raw:
        resolve(name)
    return resolved
