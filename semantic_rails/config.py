"""Package discovery and YAML→PackageConfig loader.

Exposes :func:`load_package_config`, :func:`list_package_ids`,
:func:`get_package_path`, and ``repo_root`` helpers. Reads each
package's ``package.yml`` / ``graph.yml`` / ``models/*.yml`` set,
applies operational-contract overlays, parses expressions, and returns
a typed :class:`semantic_rails.schema.PackageConfig`. Caches loaded
configs by path via ``@lru_cache``.
"""

from __future__ import annotations

import os
import re
import sysconfig
from datetime import date
from functools import lru_cache
from typing import Any

from .config_parts.package_loader import normalize_package
from .dialects import (
    connection_option_errors,
    snowflake_native_direct_connect_errors,
    supported_warehouses,
    warehouse_connector,
)
from .errors import SemanticLayerError
from .expressions import parse_config_expression, parse_semantic_expression
from .meta_contract import load_meta_contract
from .operational import (
    load_operational_contract,
    merge_operational_payloads,
    normalize_operational_payload,
    validate_operational_payload,
)
from .schema import (
    DEFAULT_PATH_HOP_LIMIT,
    MAX_PATH_HOP_LIMIT,
    AccumulationConfig,
    AggregateRelationConfig,
    ConnectionSpec,
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MeasureExternalDiscontinuity,
    MeasureValidityWindow,
    MetricConfig,
    PackageConfig,
    PackageMeta,
    PathPolicyConfig,
    PathPreferenceConfig,
    PlannerConfig,
    RelationConfig,
    RelationPipelineStep,
    RelationshipConfig,
    SeedSpec,
    SegmentConfig,
    SemanticCaveatConfig,
    SemanticPolicyConfig,
    TemporalRoleConfig,
    ValueDomainConfig,
    ValueDomainValue,
)
from .yaml_loader import load_yaml_file

__all__ = [
    "AccumulationConfig",
    "AggregateRelationConfig",
    "ConnectionSpec",
    "DimensionConfig",
    "EntityConfig",
    "MeasureConfig",
    "MeasureExternalDiscontinuity",
    "MeasureValidityWindow",
    "MetricConfig",
    "PackageConfig",
    "PackageMeta",
    "PathPreferenceConfig",
    "RelationConfig",
    "RelationPipelineStep",
    "RelationshipConfig",
    "SeedSpec",
    "SegmentConfig",
    "SemanticCaveatConfig",
    "SemanticLayerError",
    "SemanticPolicyConfig",
    "TemporalRoleConfig",
    "ValueDomainConfig",
    "ValueDomainValue",
    "connection_option_errors",
    "ensure_contained_package_path",
    "external_package_paths_allowed",
    "get_package_config",
    "get_package_path",
    "list_package_ids",
    "list_package_paths",
    "load_meta_contract",
    "load_operational_contract",
    "load_package_config",
    "load_yaml_file",
    "merge_operational_payloads",
    "normalize_operational_payload",
    "normalize_package",
    "normalize_package_source_path",
    "package_root_for_source",
    "parse_config_expression",
    "parse_semantic_expression",
    "project_managed_source",
    "repo_root",
    "resolve_repo_path",
    "snowflake_native_direct_connect_errors",
    "supported_warehouses",
    "validate_operational_payload",
    "warehouse_connector",
]


def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _installed_data_root() -> str:
    data_path = sysconfig.get_path("data") or ""
    return os.path.join(data_path, "share", "semantic-rails") if data_path else ""


def _project_roots() -> list[str]:
    roots = [repo_root()]
    installed_root = _installed_data_root()
    if installed_root and installed_root not in roots:
        roots.append(installed_root)
    return roots


def resolve_repo_path(relative_path: str) -> str:
    if not relative_path:
        return repo_root()
    candidates = [os.path.join(root, relative_path) for root in _project_roots()]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def project_managed_source(path: str) -> bool:
    """True when ``path`` lives under one of the managed package roots —
    the repo checkout or the installed ``share/semantic-rails`` data dir.
    Managed sources resolve relative assets via :func:`resolve_repo_path`;
    anything else (a user package dir, a test tmp dir) resolves against
    its own package root."""
    full = os.path.abspath(path)
    for root in _project_roots():
        try:
            if os.path.commonpath([full, root]) == root:
                return True
        except ValueError:
            continue
    return False


def normalize_package_source_path(path: str) -> str:
    return os.path.abspath(path)


def package_root_for_source(path: str) -> str:
    full = normalize_package_source_path(path)
    return full if os.path.isdir(full) else os.path.dirname(full)


def _load_yaml_file(path: str) -> dict[str, Any]:
    return dict(load_yaml_file(path) or {})


def _split_column_ref(value: str) -> tuple[str, str]:
    table, _, column = str(value).strip().partition(".")
    return table, column or table


def _unique_aliases(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for value in group:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
    return out


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value)).strip("_")


def _titleize(value: str) -> str:
    text = str(value or "").replace("_", " ").replace(".", " ").strip()
    return " ".join(part.capitalize() for part in text.split()) or value


def _ensure_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        raw = value.get("columns", value.get("column"))
        if raw is None:
            return []
        return [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else ([] if value is None else [value])
    return [dict(row or {}) for row in rows if isinstance(row, dict)]


_TIME_GRAIN_ORDER = ["transaction", "minute", "hour", "day", "week", "month", "quarter", "year"]


def _coarser_time_grains(grain: str) -> list[str]:
    text = str(grain or "").strip().lower()
    if text not in _TIME_GRAIN_ORDER:
        return [text] if text else []
    if text == "transaction":
        return list(_TIME_GRAIN_ORDER)
    start = _TIME_GRAIN_ORDER.index(text)
    return _TIME_GRAIN_ORDER[start:]


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
        parent_name = str(spec.get("inherits_from", "") or "").strip()
        if parent_name:
            spec = _merge_variant_spec(resolve(parent_name), spec)
        resolving.remove(name)
        resolved[name] = spec
        return spec

    for name in raw:
        resolve(name)
    return resolved


def _key_columns_and_role(value: Any, *, default_role: str) -> tuple[list[str], str]:
    allowed_roles = {"primary", "unique", "foreign", "natural"}
    if isinstance(value, dict):
        columns = _ensure_list(value.get("columns", value.get("column")))
        role = str(value.get("role", default_role) or default_role).strip().lower()
    else:
        columns = _ensure_list(value)
        role = default_role
    if role not in allowed_roles:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"key role must be one of {sorted(allowed_roles)}"
        )
    return columns, role


def _normalize_meta(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _merge_meta(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Shallow key-level merge of model-level meta into a child's meta.

    Child wins per key — authors override the model default at the
    measure/dimension/entity-binding level.
    """
    merged = dict(parent or {})
    merged.update(dict(child or {}))
    return merged


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _parse_validity_windows(value: Any) -> list[MeasureValidityWindow]:
    rows = value if isinstance(value, list) else ([] if value is None else [value])
    out: list[MeasureValidityWindow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            MeasureValidityWindow(
                from_=str(row.get("from", row.get("from_", "")) or ""),
                to=str(row.get("to", "") or ""),
                semantics=str(row.get("semantics", row.get("label", "")) or ""),
            )
        )
    return out


def _parse_external_discontinuities(value: Any) -> list[MeasureExternalDiscontinuity]:
    rows = value if isinstance(value, list) else ([] if value is None else [value])
    out: list[MeasureExternalDiscontinuity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        magnitude = row.get("magnitude_estimate_pct")
        out.append(
            MeasureExternalDiscontinuity(
                from_=str(row.get("from", row.get("from_", "")) or ""),
                to=str(row.get("to", "") or ""),
                what=str(row.get("what", row.get("label", "")) or ""),
                magnitude_estimate_pct=float(magnitude)
                if magnitude is not None and magnitude != ""
                else None,
            )
        )
    return out


_ALLOWED_CAVEAT_KINDS = frozenset({"business_event", "definition_change", "data_quality"})
_ALLOWED_CAVEAT_SEVERITIES = frozenset({"info", "warning"})


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0].split(" ", 1)[0]


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
    allowed = {"at", "from", "to"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}.time contains unsupported keys {unknown}; use at or from/to",
        )
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
                config={
                    key: value
                    for key, value in row_dict.items()
                    if key
                    not in {
                        "id",
                        "kind",
                        "message",
                        "object_ids",
                        "entity_values",
                        "time",
                        "audiences",
                        "environments",
                        "severity",
                        "owner",
                        "references",
                    }
                },
            )
        )
    return caveats


def _normalize_examples(value: Any) -> tuple[list[str], list[dict[str, Any]]]:
    text_examples: list[str] = []
    entries: list[dict[str, Any]] = []
    raw_items = value if isinstance(value, list) else ([] if value is None else [value])
    for raw in raw_items:
        if isinstance(raw, dict):
            entry = dict(raw)
            label = str(entry.get("question") or entry.get("name") or entry.get("id") or "").strip()
            if label:
                text_examples.append(label)
            entries.append(entry)
        else:
            text = str(raw or "").strip()
            if text:
                text_examples.append(text)
    return text_examples, entries


def _merge_package_dir(path: str) -> dict[str, Any]:
    package_path = os.path.join(path, "package.yml")
    if not os.path.isfile(package_path):
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Package directory '{path}' is missing package.yml"
        )

    raw = _load_yaml_file(package_path)
    merged: dict[str, Any] = dict(raw)
    merged.setdefault("defaults", {})
    merged.setdefault("graph", {})
    merged.setdefault("models", {})
    merged.setdefault("relations", {})
    merged.setdefault("metrics", {})
    merged.setdefault("segments", {})

    for filename, key in (
        ("defaults.yml", "defaults"),
        ("graph.yml", "graph"),
        ("relations.yml", "relations"),
        ("metrics.yml", "metrics"),
        ("segments.yml", "segments"),
        ("policies.yml", "semantic_policies"),
        ("caveats.yml", "semantic_caveats"),
    ):
        full = os.path.join(path, filename)
        if os.path.isfile(full):
            doc = _load_yaml_file(full)
            list_keys = {"semantic_policies", "semantic_caveats"}
            value = doc.get(key, doc if key in list_keys else {})
            # `semantic_policies:` and `semantic_caveats:` are lists; every other
            # block is a mapping. Don't wrap a list in `dict(...)` — that
            # silently fails with "update sequence element #0 has length 3".
            if key in list_keys:
                merged[key] = list(value or [])
            else:
                merged[key] = dict(value or {})

    def _yaml_files(root: str) -> list[str]:
        files: list[str] = []
        for current_root, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                if filename.endswith(".yml") or filename.endswith(".yaml"):
                    files.append(os.path.join(current_root, filename))
        return files

    models_dir = os.path.join(path, "models")
    if os.path.isdir(models_dir):
        models = dict(merged.get("models", {}) or {})
        for file_path in _yaml_files(models_dir):
            doc = _load_yaml_file(file_path)
            if "models" in doc:
                model_docs = dict(doc.get("models", {}) or {})
            else:
                model_docs = {
                    str(
                        dict(doc.get("model", doc) or {}).get("id")
                        or os.path.splitext(os.path.basename(file_path))[0]
                    ): dict(doc.get("model", doc) or {})
                }
            for model_key, model_raw in model_docs.items():
                model = dict(model_raw or {})
                model_id = str(
                    model.get("id") or model_key or os.path.splitext(os.path.basename(file_path))[0]
                )
                model["id"] = model_id
                models[model_id] = model
        merged["models"] = models

    relations_dir = os.path.join(path, "relations")
    if os.path.isdir(relations_dir):
        relations = dict(merged.get("relations", {}) or {})
        for file_path in _yaml_files(relations_dir):
            doc = _load_yaml_file(file_path)
            if "relations" in doc:
                relation_docs = dict(doc.get("relations", {}) or {})
            else:
                relation_docs = {
                    str(
                        dict(doc.get("relation", doc) or {}).get("id")
                        or os.path.splitext(os.path.basename(file_path))[0]
                    ): dict(doc.get("relation", doc) or {})
                }
            for relation_key, relation_raw in relation_docs.items():
                relation = dict(relation_raw or {})
                relation_id = str(
                    relation.get("id")
                    or relation_key
                    or os.path.splitext(os.path.basename(file_path))[0]
                )
                relation["id"] = relation_id
                relations[relation_id] = relation
        merged["relations"] = relations

    metrics_dir = os.path.join(path, "metrics")
    if os.path.isdir(metrics_dir):
        metrics = dict(merged.get("metrics", {}) or {})
        for file_path in _yaml_files(metrics_dir):
            doc = _load_yaml_file(file_path)
            if "metrics" in doc:
                metric_docs = dict(doc.get("metrics", {}) or {})
            else:
                metric_docs = {
                    str(
                        dict(doc.get("metric", doc) or {}).get("name")
                        or dict(doc.get("metric", doc) or {}).get("id")
                        or os.path.splitext(os.path.basename(file_path))[0]
                    ): dict(doc.get("metric", doc) or {})
                }
            for metric_key, metric_raw in metric_docs.items():
                metrics[str(metric_key)] = dict(metric_raw or {})
        merged["metrics"] = metrics

    segments_dir = os.path.join(path, "segments")
    if os.path.isdir(segments_dir):
        segments = dict(merged.get("segments", {}) or {})
        for file_path in _yaml_files(segments_dir):
            doc = _load_yaml_file(file_path)
            if "segments" in doc:
                segment_docs = dict(doc.get("segments", {}) or {})
            else:
                segment_docs = {
                    str(
                        dict(doc.get("segment", doc) or {}).get("name")
                        or dict(doc.get("segment", doc) or {}).get("id")
                        or os.path.splitext(os.path.basename(file_path))[0]
                    ): dict(doc.get("segment", doc) or {})
                }
            for segment_key, segment_raw in segment_docs.items():
                segments[str(segment_key)] = dict(segment_raw or {})
        merged["segments"] = segments
    return merged


def _load_package_source(path: str) -> dict[str, Any]:
    if os.path.isdir(path):
        return _merge_package_dir(path)
    return _load_yaml_file(path)


def _translate_metric_direct_fields(
    spec: dict[str, Any],
    *,
    resolve,
    resolve_metric=None,
    classify_ref=None,
    context: str = "",
    suggest=None,
) -> dict[str, Any]:
    """Translate direct named fields per metric kind into the runtime
    expression AST shape.

    Common kinds — `aggregate`, `ratio`, `cumulative`, `rolling`,
    `prior_period`, `period_to_date` — get direct named fields:
        kind: aggregate     → measure: <key>
        kind: ratio         → numerator: <key>, denominator: <key>, null_behavior: <opt>
        kind: cumulative    → measure: <key>, optional window
        kind: rolling       → measure: <key>, window: { ... }
        kind: prior_period  → measure: <key>, offset/period
        kind: period_to_date→ measure: <key>, period: <str>

    Long-tail kinds (`derived`, `conversion`, anything using metric_predicate)
    keep authoring the AST directly under `expression:`. References to other
    metrics/measures use package-relative keys; the loader resolves them via
    the supplied `resolve` callable (measure index) and `resolve_metric`
    callable (metric index, with measure fallback for auto-published metrics).
    Falls back to measure-only resolution when `resolve_metric` is None.
    """
    if resolve_metric is None:
        resolve_metric = resolve
    if not isinstance(spec, dict):
        return spec
    spec = dict(spec)
    if "expression" in spec and spec["expression"]:
        # Author wrote the AST directly. Pass through, but resolve any
        # package-relative refs inside (best-effort).
        spec["expression"] = _resolve_refs_in_ast(
            spec["expression"],
            resolve=resolve,
            resolve_metric=resolve_metric,
        )
        return spec
    kind = str(spec.get("kind", "")).strip().lower()

    if kind in {"aggregate", "semi_additive"}:
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        spec["expression"] = {
            "kind": kind,
            "measure": resolve(measure_ref),
            "aggregation": str(spec.get("aggregation", "")),
        }
        return spec

    if kind == "ratio":
        numerator = spec.get("numerator")
        denominator = spec.get("denominator")
        if numerator is None or denominator is None:
            return spec
        null_behavior = str(spec.get("null_behavior", "null_if_zero"))

        def _ratio_operand(ref: Any, field: str = "operand") -> dict[str, Any]:
            """Wrap a ratio numerator/denominator as a metric ref when it
            resolves to a top-level metric, or as a measure aggregate when
            it resolves to a measure. With auto-publish gone, ratio
            references must point at something that exists; synthesizing a
            metric ID from a measure (the legacy `measure.X.Y → metric.X.Y`
            convention) would produce a dangling reference.
            """
            text = str(ref or "").strip()
            if not text:
                return {"kind": "metric", "metric": text}
            if text.startswith("metric."):
                return {"kind": "metric", "metric": text}
            if text.startswith("measure."):
                return {"kind": "aggregate", "measure": text, "aggregation": ""}
            # When we know the ref classifies (caller passed classify_ref),
            # prefer measure expansion for measure-only keys so auto-publish
            # is not silently re-introduced.
            if classify_ref is not None:
                kind_hit = classify_ref(text)
                if kind_hit == "measure":
                    measure_id = resolve(text)
                    return {"kind": "aggregate", "measure": measure_id, "aggregation": ""}
                if kind_hit == "metric":
                    return {"kind": "metric", "metric": resolve_metric(text)}
            metric_id = resolve_metric(text)
            if metric_id and metric_id.startswith("metric."):
                return {"kind": "metric", "metric": metric_id}
            measure_id = resolve(text)
            if measure_id and measure_id.startswith("measure."):
                return {"kind": "aggregate", "measure": measure_id, "aggregation": ""}
            # Unresolved ref. With authoring context available, fail here —
            # the author gets the metric, the field, and did-you-mean
            # candidates instead of a late "Unknown metric recipe" from the
            # compiler with no location.
            if context:
                suggestions = list(suggest(text) or []) if suggest is not None else []
                hint = f"; closest matches: {', '.join(suggestions)}" if suggestions else ""
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{context}: {field} '{text}' does not match any measure or "
                    f"metric in this package{hint}",
                )
            # Fall through: leave the resolved metric id (likely synthetic)
            # so downstream validation produces a clear OBJECT_NOT_FOUND.
            return {"kind": "metric", "metric": metric_id or text}

        spec["expression"] = {
            "kind": "binary",
            "op": "divide",
            "left": _ratio_operand(numerator, field="numerator"),
            "right": _ratio_operand(denominator, field="denominator"),
            "null_behavior": null_behavior,
        }
        # Default kind: `binary` is converted by _convert_recipe_expr to `arithmetic`.
        return spec

    if kind == "cumulative":
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        # cumulative wraps an aggregate input over the measure.
        inner: dict[str, Any] = {
            "kind": "aggregate",
            "measure": resolve(measure_ref),
            "aggregation": str(spec.get("aggregation", "")),
        }
        out: dict[str, Any] = {"kind": "cumulative", "input": inner}
        if spec.get("partition_by"):
            out["partition_by"] = list(spec.get("partition_by", []) or [])
        if spec.get("order_by"):
            out["order_by"] = str(spec.get("order_by", ""))
        spec["expression"] = out
        return spec

    if kind == "rolling":
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        inner = {
            "kind": "aggregate",
            "measure": resolve(measure_ref),
            "aggregation": str(spec.get("aggregation", "")),
        }
        spec["expression"] = {
            "kind": "rolling",
            "input": inner,
            "window": dict(spec.get("window", {}) or {}),
        }
        return spec

    if kind == "prior_period":
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        inner = {
            "kind": "aggregate",
            "measure": resolve(measure_ref),
            "aggregation": str(spec.get("aggregation", "")),
        }
        spec["expression"] = {
            "kind": "prior_period",
            "input": inner,
            "offset": dict(spec.get("offset", {}) or {}),
        }
        return spec

    if kind == "period_to_date":
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        inner = {
            "kind": "aggregate",
            "measure": resolve(measure_ref),
            "aggregation": str(spec.get("aggregation", "")),
        }
        spec["expression"] = {
            "kind": "period_to_date",
            "input": inner,
            "period": str(spec.get("period", "")),
        }
        return spec

    return spec


def _resolve_metric_ref(value: Any, *, resolve) -> str:
    """Resolve a metric reference to its full id.

    A bare string like `revenue_usd` may refer to a measure (then we wrap
    in metric.<package>.<key> via convention) or a metric. Without a
    metric index, we leave it textual and let downstream validation flag
    invalid refs.

    NOTE: This helper only knows about the measure index. Use the
    `resolve_metric` callable built in `_parse_package` (which consults
    both the metrics index and the measures index, with collision
    detection) when you need full top-level-metric support. Kept here
    for back-compat with callers that have not been threaded through
    yet.
    """
    text = str(value or "").strip()
    if not text:
        return text
    if text.startswith("metric."):
        return text
    # If it resolves as a measure, treat that measure's auto-published
    # metric id (`metric.<ns>.<tail>`) as the canonical metric id.
    measure_id = resolve(text)
    if measure_id and measure_id.startswith("measure."):
        return measure_id.replace("measure.", "metric.", 1)
    return text


def _resolve_refs_in_ast(node: Any, *, resolve, resolve_metric=None) -> Any:
    """Best-effort recursive resolution of package-relative refs inside
    an authored expression AST. Walks `kind: metric` and `kind: aggregate`
    nodes and rewrites the `metric:` / `measure:` field via the
    appropriate resolver.

    `resolve` resolves measure refs (returns measure.<ns>.<key>).
    `resolve_metric` resolves metric refs — falls back to measure-only
    behavior when None for back-compat.
    """
    if resolve_metric is None:

        def resolve_metric(value):
            return _resolve_metric_ref(value, resolve=resolve)

    if isinstance(node, list):
        return [
            _resolve_refs_in_ast(item, resolve=resolve, resolve_metric=resolve_metric)
            for item in node
        ]
    if not isinstance(node, dict):
        return node
    out = dict(node)
    kind = str(out.get("kind", "")).strip().lower()
    if kind == "metric" and "metric" in out:
        out["metric"] = resolve_metric(out["metric"])
    if kind in {"aggregate", "semi_additive"} and "measure" in out:
        out["measure"] = resolve(out["measure"])
    # A bare measure ref (no kind, just {measure: <key>}) appears in
    # metric_predicate inputs and conversion base/converted shorthands.
    # Resolve the key here so package-relative refs survive into runtime.
    if not kind and "measure" in out and "metric" not in out:
        out["measure"] = resolve(out["measure"])
    for key, value in list(out.items()):
        if isinstance(value, (dict, list)):
            out[key] = _resolve_refs_in_ast(value, resolve=resolve, resolve_metric=resolve_metric)
    return out


def _convert_recipe_expr(expr: dict[str, Any]) -> dict[str, Any]:
    kind = str(expr.get("kind", "")).strip()
    if kind in {"aggregate", "semi_additive"}:
        out: dict[str, Any] = {
            "kind": kind,
            "measure": str(expr["measure"]),
            "aggregation": str(expr.get("aggregation", "")),
        }
        if expr.get("temporal_role"):
            out["temporal_role"] = str(expr.get("temporal_role", ""))
        if expr.get("parameters"):
            out["parameters"] = dict(expr.get("parameters", {}) or {})
        if expr.get("filter"):
            out["filter"] = dict(expr.get("filter", {}) or {})
        if expr.get("window"):
            out["window"] = dict(expr.get("window", {}) or {})
        return out
    if kind == "metric":
        return {"kind": "metric", "metric": str(expr["metric"])}
    if kind == "binary":
        op_map = {
            "plus": "add",
            "add": "add",
            "minus": "subtract",
            "subtract": "subtract",
            "multiply": "multiply",
            "divide": "divide",
        }
        out = {
            "kind": "arithmetic",
            "op": op_map.get(str(expr["op"]).lower(), str(expr["op"]).lower()),
            "left": _convert_recipe_expr(dict(expr["left"])),
            "right": _convert_recipe_expr(dict(expr["right"])),
        }
        # Preserve null_behavior across the binary→arithmetic rename. Without
        # this, ratio metrics that author `null_behavior: null_if_zero` lose
        # the safety hint when the loader translates the AST.
        if expr.get("null_behavior") is not None:
            out["null_behavior"] = str(expr.get("null_behavior", ""))
        return out
    if kind == "cumulative":
        out = {"kind": "cumulative", "input": _convert_recipe_expr(dict(expr["input"]))}
        if expr.get("partition_by"):
            out["partition_by"] = list(expr.get("partition_by", []) or [])
        if expr.get("order_by"):
            out["order_by"] = str(expr.get("order_by", ""))
        return out
    if kind == "rolling":
        out = {
            "kind": "rolling",
            "input": _convert_recipe_expr(dict(expr["input"])),
            "window": dict(expr.get("window", {}) or {}),
        }
        if expr.get("partition_by"):
            out["partition_by"] = list(expr.get("partition_by", []) or [])
        return out
    if kind == "prior_period":
        return {
            "kind": "prior_period",
            "input": _convert_recipe_expr(dict(expr["input"])),
            "offset": dict(expr.get("offset", {}) or {}),
        }
    if kind == "period_to_date":
        out = {
            "kind": "period_to_date",
            "input": _convert_recipe_expr(dict(expr["input"])),
            "period": str(expr.get("period", "")),
        }
        if expr.get("partition_by"):
            out["partition_by"] = list(expr.get("partition_by", []) or [])
        return out
    if kind == "metric_predicate":
        out = {
            "kind": "metric_predicate",
            "input": _convert_recipe_expr(dict(expr["input"])),
            "entity": str(expr.get("entity", "")),
            "op": str(expr.get("op", "")),
            "value": expr.get("value"),
        }
        if expr.get("scope_mode") is not None:
            out["scope_mode"] = str(expr.get("scope_mode", ""))
        if expr.get("time_grain") is not None:
            out["time_grain"] = str(expr.get("time_grain", ""))
        if expr.get("time_alignment") is not None:
            out["time_alignment"] = str(expr.get("time_alignment", ""))
        if expr.get("window") is not None:
            out["window"] = dict(expr.get("window", {}) or {})
        return out
    if kind == "conversion":
        out = {
            "kind": "conversion",
            "base": _convert_recipe_expr(dict(expr["base"])),
            "converted": _convert_recipe_expr(dict(expr["converted"])),
            "entity": str(expr.get("entity", "")),
            "window": dict(expr.get("window", {}) or {}),
            "matching_mode": str(expr.get("matching_mode", expr.get("matching", ""))),
            "constant_properties": list(expr.get("constant_properties", []) or []),
        }
        if expr.get("dimension_bindings"):
            out["dimension_bindings"] = {
                str(dim_id): dict(binding or {})
                for dim_id, binding in dict(expr.get("dimension_bindings", {}) or {}).items()
            }
        return out
    if "measure" in expr:
        out = {
            "kind": "measure",
            "measure": str(expr["measure"]),
            "aggregation": str(expr.get("aggregation", "")),
        }
        if expr.get("parameters"):
            out["parameters"] = dict(expr.get("parameters", {}) or {})
        return out
    return dict(expr)


_EXTERNAL_PATHS_ENV = "SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS"


def external_package_paths_allowed() -> bool:
    return os.environ.get(_EXTERNAL_PATHS_ENV, "").strip().lower() in {"1", "true", "yes"}


def ensure_contained_package_path(value: str, *, field: str, path: str = "") -> str:
    """Reject absolute and parent-traversal package asset paths.

    Package YAML is untrusted input once packages are shared: an absolute
    ``default_db`` lets a package create or replace any process-writable
    file, and a ``../`` seed source reads files outside the package root
    (and leaks them into built artifacts). A relative, ``..``-free path
    joined to a root cannot escape that root, so everything else is
    rejected here — the single choke point every loader passes through.
    Operators who intentionally keep assets outside the package can set
    ``SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS=1``.
    """
    text = str(value or "").strip()
    if not text or external_package_paths_allowed():
        return text
    prefix = f"{path}: " if path else ""
    is_absolute = (
        os.path.isabs(text)
        or text.startswith(("/", "\\"))
        or bool(re.match(r"^[A-Za-z]:[/\\]", text))
    )
    if is_absolute:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{prefix}{field} must be a relative path inside the package "
            f"(got absolute path {text!r}); set {_EXTERNAL_PATHS_ENV}=1 to "
            "allow paths outside the package root",
        )
    parts = [part for part in re.split(r"[/\\]+", text) if part]
    if ".." in parts:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{prefix}{field} must not contain '..' path segments "
            f"(got {text!r}); set {_EXTERNAL_PATHS_ENV}=1 to allow paths "
            "outside the package root",
        )
    return text


def _parse_connection_spec(raw: dict[str, Any]) -> ConnectionSpec:
    mapping = dict(raw or {})
    options = dict(mapping.get("options", {}) or {})
    for key, value in mapping.items():
        if key not in {"kind", "name", "options"}:
            options.setdefault(str(key), value)
    return ConnectionSpec(
        kind=str(mapping.get("kind", "")).strip(),
        name=str(mapping.get("name", "")).strip(),
        options=options,
    )


def _parse_package_meta(package_raw: dict[str, Any], *, path: str) -> PackageMeta:
    package = dict(package_raw or {})
    package_id = str(package.get("id", "")).strip()
    if not package_id:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: package.id is required")

    warehouse = str(package.get("warehouse", "duckdb")).strip().lower() or "duckdb"
    connector = warehouse_connector(warehouse)
    if connector is None:
        supported = ", ".join(supported_warehouses())
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: unsupported package.warehouse '{warehouse}' (supported: {supported})",
        )

    connection = _parse_connection_spec(dict(package.get("connection", {}) or {}))
    default_db = ensure_contained_package_path(
        str(package.get("default_db", "")).strip(), field="package.default_db", path=path
    )
    seed_raw = dict(package.get("seed", {}) or {})
    seed = SeedSpec(
        kind=str(seed_raw.get("kind", "")).strip(),
        source=ensure_contained_package_path(
            str(seed_raw.get("source", "")).strip(), field="package.seed.source", path=path
        ),
        post_sql=ensure_contained_package_path(
            str(seed_raw.get("post_sql", "")).strip(), field="package.seed.post_sql", path=path
        ),
        null_strings=_ensure_list(seed_raw.get("null_strings", [""])) or [""],
    )

    if connector.requires_default_db and not default_db:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{path}: duckdb packages must declare package.default_db"
        )
    if connector.requires_seed and (not seed.kind or not seed.source):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: duckdb packages must declare package.seed.kind and package.seed.source",
        )
    if connector.connection_kinds:
        if connection.kind not in connector.connection_kinds:
            allowed = ", ".join(connector.connection_kinds)
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: {warehouse} packages must declare package.connection.kind in [{allowed}]",
            )
        option_errors = connection_option_errors(warehouse, connection.kind, connection.options)
        if option_errors:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: {warehouse} package.connection has invalid options: {'; '.join(option_errors)}",
            )
        if warehouse == "snowflake" and connection.kind == "snowflake_cli" and not connection.name:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: snowflake_cli packages must declare package.connection.name",
            )
        if (
            warehouse == "snowflake"
            and connection.kind == "snowflake_native"
            and not connection.name
        ):
            direct_errors = snowflake_native_direct_connect_errors(connection.options)
            if direct_errors:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: {warehouse} package.connection has invalid options: {'; '.join(direct_errors)}",
                )
        elif connector.requires_connection_name and not connection.name:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: {warehouse} packages must declare package.connection.name",
            )
    elif connection.kind:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: {warehouse} packages do not support package.connection.kind '{connection.kind}'",
        )
    elif connection.options:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: {warehouse} packages do not support package.connection options",
        )

    planner_raw = package.get("planner")
    planner_cfg = PlannerConfig(
        disabled_patterns=_ensure_list(
            planner_raw.get("disabled_patterns") if isinstance(planner_raw, dict) else None
        ),
    )
    return PackageMeta(
        package_id=package_id,
        name=str(package.get("name", package_id)),
        description=str(package.get("description", package_id)),
        warehouse=warehouse,
        default_db=default_db,
        seed=seed,
        connection=connection,
        environments=_ensure_list(package.get("environments")),
        schema_strict=bool(package.get("schema_strict", False)),
        planner=planner_cfg,
    )


_RELATION_STEP_KEYS = {
    "source",
    "select",
    "where",
    "group_by",
    "join",
    "semi_join",
    "anti_join",
    "exclude",
    "union_all",
    "explode",
    "unnest",
    "json_extract",
    "date_spine",
    "state_as_of",
    "window",
    "attribution_join",
}


def _relation_sql_name(relation_id: str) -> str:
    label = relation_id
    if label.startswith("relation."):
        label = label.split("relation.", 1)[1]
    return "rel_" + _slug(label).replace(".", "_")


def _relation_id_for_key(key: str, spec: dict[str, Any], *, namespace: str) -> str:
    explicit = str(spec.get("id", "") or "").strip()
    if explicit.startswith("relation."):
        return explicit
    local = explicit or str(key)
    if local.startswith("relation."):
        return local
    if namespace:
        return f"relation.{namespace}.{_slug(local)}"
    return f"relation.{_slug(local)}"


def _normalize_relation_step(raw: Any) -> RelationPipelineStep:
    if isinstance(raw, str):
        return RelationPipelineStep(kind="source", config={"relation": raw})
    if not isinstance(raw, dict):
        raise SemanticLayerError("INVALID_CONFIG", "Relation pipeline steps must be mappings")
    step = dict(raw or {})
    kind = str(step.get("kind", "") or "").strip()
    config = dict(step.get("config", {}) or {}) if isinstance(step.get("config"), dict) else {}
    if kind:
        config.update({key: value for key, value in step.items() if key not in {"kind", "config"}})
        return RelationPipelineStep(kind=kind, config=config)
    matched = [key for key in step if key in _RELATION_STEP_KEYS]
    if len(matched) != 1:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Relation pipeline step must declare exactly one step kind, got {matched}",
        )
    kind = matched[0]
    value = step[kind]
    config = dict(value) if isinstance(value, dict) else {"value": value}
    config.update({key: value for key, value in step.items() if key != kind})
    return RelationPipelineStep(kind="explode" if kind == "unnest" else kind, config=config)


def _parse_relations(
    raw: dict[str, Any], *, namespace: str, path: str
) -> tuple[list[RelationConfig], dict[str, str]]:
    rows = raw.get("relations", {}) or {}
    if isinstance(rows, list):
        items = [
            (str(dict(row or {}).get("id", index)), dict(row or {}))
            for index, row in enumerate(rows)
        ]
    elif isinstance(rows, dict):
        items = [(str(key), dict(value or {})) for key, value in rows.items()]
    else:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: relations must be a mapping or list")

    relations: list[RelationConfig] = []
    aliases: dict[str, str] = {}
    for key, spec in items:
        relation_id = _relation_id_for_key(key, spec, namespace=namespace)
        steps: list[RelationPipelineStep] = []
        if spec.get("source"):
            steps.append(
                RelationPipelineStep(kind="source", config={"relation": str(spec.get("source"))})
            )
        for step_raw in list(spec.get("steps", []) or []):
            steps.append(_normalize_relation_step(step_raw))
        if spec.get("date_spine"):
            steps.append(
                RelationPipelineStep(
                    kind="date_spine", config=dict(spec.get("date_spine", {}) or {})
                )
            )
        if not steps:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: relation '{key}' must declare source/date_spine or steps",
            )
        output_name = str(
            spec.get("output_name", spec.get("cte", "")) or ""
        ).strip() or _relation_sql_name(relation_id)
        relations.append(
            RelationConfig(
                id=relation_id,
                output_name=output_name,
                steps=steps,
                columns=_ensure_list(spec.get("columns", spec.get("output_columns"))),
                name=str(spec.get("name", key)),
                label=str(spec.get("label", _titleize(key))),
                description=str(spec.get("description", "")),
                meta=_normalize_meta(spec.get("meta")),
            )
        )
        aliases[key] = relation_id
        aliases[relation_id] = relation_id
        aliases[output_name] = relation_id
    return relations, aliases


def _map_dimension_kind(kind: str) -> str:
    mapping = {
        "id": "id",
        "categorical": "string",
        "boolean": "boolean",
        "integer": "integer",
        "continuous": "number",
        "number": "number",
        "percent": "number",
        "currency": "number",
        "date": "date",
        "timestamp": "timestamp",
    }
    return mapping.get(str(kind or "string"), str(kind or "string"))


def _normalize_accumulation(spec: dict[str, Any]) -> AccumulationConfig:
    """Normalize accumulation YAML into AccumulationConfig.

    Accepts both the canonical nested form
        accumulation: {kind: stock, snapshot: end_of_period}
    and the legacy flat form
        accumulation: stock
        snapshot_policy: end_of_period
    The legacy flat form will be rejected once schema_strict lands; for
    now it's normalized transparently.
    """
    raw = spec.get("accumulation", "")
    legacy_snapshot = str(spec.get("snapshot_policy", "")).strip().lower()
    if isinstance(raw, dict):
        kind = str(raw.get("kind", "") or "").strip().lower()
        snapshot = str(raw.get("snapshot", legacy_snapshot) or legacy_snapshot).strip().lower()
        return AccumulationConfig(kind=kind, snapshot=snapshot)
    kind = str(raw or "").strip().lower()
    return AccumulationConfig(kind=kind, snapshot=legacy_snapshot)


def _derive_measure_semantics(spec: dict[str, Any]) -> tuple[str, list[str], list[str], str]:
    accumulation = _normalize_accumulation(spec)
    kind = str(spec.get("kind", "")).strip().lower()
    if kind == "entity_count":
        measure_class = (
            "distinct_population" if accumulation.kind == "population" else "event_count"
        )
        return (
            "count_distinct",
            ["count_distinct"],
            ["sum", "avg", "min", "max", "median", "percentile", "first_value", "last_value"],
            measure_class,
        )
    if accumulation.kind == "stock":
        default_aggregation = (
            "first_value" if accumulation.snapshot == "start_of_period" else "last_value"
        )
        return (
            default_aggregation,
            ["last_value", "first_value", "sum", "avg", "min", "max", "median", "percentile"],
            ["count_distinct"],
            "semi_additive",
        )
    return (
        "sum",
        ["sum", "avg", "min", "max", "median", "percentile"],
        ["count_distinct", "first_value", "last_value"],
        "additive",
    )


def _suggested_aggregations(
    default_aggregation: str, allowed_aggregations: list[str], measure_class: str
) -> list[str]:
    preferred_orders = {
        "semi_additive": [
            "last_value",
            "first_value",
            "sum",
            "avg",
            "percentile",
            "median",
            "min",
            "max",
        ],
        "event_count": ["count_distinct"],
        "distinct_population": ["count_distinct"],
        "additive": ["sum", "avg", "median", "percentile", "min", "max"],
    }
    order = preferred_orders.get(measure_class, [default_aggregation, *allowed_aggregations])
    out: list[str] = []
    for value in [default_aggregation, *order, *allowed_aggregations]:
        text = str(value).strip()
        if text and text in allowed_aggregations and text not in out:
            out.append(text)
    return out


def _default_topics(name: str, fallback: str = "analytics") -> list[str]:
    parts = [
        part
        for part in str(name or "").replace("metric.", "").replace("measure.", "").split(".")
        if part
    ]
    return parts[:2] or [fallback]


def _metric_from_measure(
    measure: MeasureConfig,
    spec: dict[str, Any],
    *,
    operational_contract: dict[str, Any],
    path: str,
) -> MetricConfig | None:
    publish = spec.get("publish", True)
    if publish is False:
        return None
    publish_spec = dict(publish or {}) if isinstance(publish, dict) else {}
    default_metric_name = measure.name or measure.id.split("measure.", 1)[-1]
    metric_id = str(publish_spec.get("id", f"metric.{default_metric_name}"))
    metric_name = str(publish_spec.get("name", default_metric_name))
    metric_label = str(
        publish_spec.get("label", measure.label or _titleize(metric_name.split(".")[-1]))
    )
    kind = "semi_additive" if measure.measure_class == "semi_additive" else "aggregate"
    expr_kind = "semi_additive" if kind == "semi_additive" else "aggregate"
    temporal_role = (
        measure.compatible_temporal_roles[0] if measure.compatible_temporal_roles else ""
    )
    publish_operational = normalize_operational_payload(
        publish_spec.get("operational"),
        contract=operational_contract,
        target="metric",
        path=f"{path} publish.operational",
    )
    metric_operational = validate_operational_payload(
        merge_operational_payloads(measure.operational, publish_operational),
        contract=operational_contract,
        target="metric",
        path=f"{path} auto-published metric operational",
    )
    _, metric_example_entries = _normalize_examples(publish_spec.get("examples"))
    return MetricConfig(
        id=metric_id,
        kind=kind,
        expression=parse_semantic_expression(
            {"kind": expr_kind, "measure": measure.id, "aggregation": measure.default_aggregation},
            context="config",
        ),
        temporal_role=temporal_role,
        compatible_temporal_roles=list(measure.compatible_temporal_roles),
        name=metric_name,
        label=metric_label,
        description=str(publish_spec.get("description", measure.description or metric_label)),
        topics=_ensure_list(publish_spec.get("topics"))
        or list(measure.topics)
        or _default_topics(metric_name),
        comparison_family=str(
            publish_spec.get("comparison_family", spec.get("comparison_family", ""))
        ),
        comparison_mode=str(publish_spec.get("comparison_mode", spec.get("comparison_mode", ""))),
        comparison_peers=_ensure_list(
            publish_spec.get("comparison_peers", spec.get("comparison_peers"))
        ),
        clock_variants=_ensure_list(publish_spec.get("clock_variants", spec.get("clock_variants"))),
        preferred_companion_metrics=_ensure_list(
            publish_spec.get("preferred_companion_metrics", spec.get("preferred_companion_metrics"))
        ),
        operational=metric_operational,
        meta={**dict(measure.meta), **_normalize_meta(publish_spec.get("meta"))},
        example_entries=metric_example_entries or list(measure.example_entries),
        value_type=str(publish_spec.get("value_type") or measure.value_type or "number"),
    )


def _ensure_unique_object_ids(config: PackageConfig, *, path: str) -> None:
    seen: dict[str, str] = {}
    groups = (
        ("entity", config.entities),
        ("dimension", config.dimensions),
        ("temporal_role", config.temporal_roles),
        ("relationship", config.relationships),
        ("value_domain", config.value_domains),
        ("measure", config.measures),
        ("metric", config.metric_recipes),
        ("segment", config.segments),
        ("semantic_policy", config.semantic_policies),
        ("semantic_caveat", config.semantic_caveats),
        ("aggregate_relation", config.aggregate_relations),
        ("relation", config.relations),
    )
    for kind, rows in groups:
        for row in rows:
            object_id = str(getattr(row, "id", "") or "").strip()
            if not object_id:
                continue
            previous_kind = seen.get(object_id)
            if previous_kind is not None:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: duplicate object id '{object_id}' in {previous_kind} and {kind}; object ids must be unique",
                )
            seen[object_id] = kind


def _parse_path_policy(raw: dict[str, Any], *, path: str) -> PathPolicyConfig:
    policy_raw = raw.get("path_policy")
    if policy_raw is None:
        return PathPolicyConfig()
    if not isinstance(policy_raw, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: path_policy must be a mapping (got {type(policy_raw).__name__})",
        )
    unknown = sorted(set(policy_raw) - {"max_hops"})
    if unknown:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: path_policy contains unsupported keys: {', '.join(unknown)}",
            details={"supported": ["max_hops"]},
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
    rel_lookup: dict[str, RelationshipConfig] = {}
    for known_rel in relationships:
        rel_lookup[known_rel.id] = known_rel
        _, _, suffix = known_rel.id.partition(".")
        if suffix:
            rel_lookup.setdefault(suffix, known_rel)
    out: list[PathPreferenceConfig] = []
    for row in rows:
        row_dict = dict(row or {})
        source_ref = str(row_dict.get("source_entity", "")).strip()
        target_ref = str(row_dict.get("target_entity", "")).strip()
        for label, ref in (("source_entity", source_ref), ("target_entity", target_ref)):
            if ref not in entity_lookup:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: path_preferences row references unknown {label} '{ref}'",
                )
        source_entity = entity_lookup[source_ref]
        target_entity = entity_lookup[target_ref]
        preferred = row_dict.get("preferred_paths")
        if preferred is not None:
            paths_raw = list(preferred or [])
            if len(paths_raw) != 1:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: path_preferences for {source_ref} -> {target_ref} must "
                    f"declare exactly one preferred path (got {len(paths_raw)})",
                )
            rel_refs = [str(item) for item in list(paths_raw[0] or [])]
        else:
            rel_refs = [str(item) for item in list(row_dict.get("relationship_path", []) or [])]
        if not rel_refs:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: path_preferences for {source_ref} -> {target_ref} declares an empty path",
            )
        resolved: list[str] = []
        current = source_entity
        for rel_ref in rel_refs:
            rel = rel_lookup.get(rel_ref)
            if rel is None:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: path_preferences for {source_ref} -> {target_ref} references "
                    f"unknown relationship '{rel_ref}'",
                )
            directions = {
                str(item).strip().lower()
                for item in list(rel.allowed_directions or ["forward", "reverse"])
            }
            if current == rel.source_entity and "forward" in directions:
                current = rel.target_entity
            elif current == rel.target_entity and "reverse" in directions:
                current = rel.source_entity
            else:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: path_preferences for {source_ref} -> {target_ref}: relationship "
                    f"'{rel.id}' does not connect from '{current}' (or traversal in that "
                    "direction is not allowed)",
                )
            resolved.append(rel.id)
        if current != target_entity:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: path_preferences path for {source_ref} -> {target_ref} ends at "
                f"'{current}', not the declared target",
            )
        out.append(
            PathPreferenceConfig(
                source_entity=source_entity,
                target_entity=target_entity,
                relationship_path=resolved,
            )
        )
    return out


def _validate_caveat_refs(config: PackageConfig, *, path: str) -> None:
    object_ids = {
        *[row.id for row in config.entities],
        *[row.id for row in config.dimensions],
        *[row.id for row in config.temporal_roles],
        *[row.id for row in config.relationships],
        *[row.id for row in config.value_domains],
        *[row.id for row in config.measures],
        *[row.id for row in config.metric_recipes],
        *[row.id for row in config.segments],
    }
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


def _parse_package(raw: dict[str, Any], *, path: str) -> PackageConfig:
    package_raw = dict(raw.get("package", {}) or {})
    namespace = str(package_raw.get("namespace", package_raw.get("id", "")) or "").strip()
    defaults = dict(raw.get("defaults", {}) or {})
    graph = dict(raw.get("graph", {}) or {})
    graph_entities = dict(graph.get("entities", {}) or {})
    model_rows = dict(raw.get("models", {}) or {})
    metrics_rows = dict(raw.get("metrics", {}) or {})
    segments_rows = dict(raw.get("segments", {}) or {})
    relations, relation_aliases = _parse_relations(raw, namespace=namespace, path=path)
    relations_by_id = {row.id: row for row in relations}

    if not graph_entities:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: graph.entities must not be empty")
    if not model_rows:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: models must not be empty")

    entities: list[EntityConfig] = []
    entity_lookup: dict[str, str] = {}
    model_to_entity: dict[str, str] = {}
    model_rows = {str(k): dict(v or {}) for k, v in model_rows.items()}
    time_calendar_ids_seen: set[str] = set()

    # Split model rows by kind. Fact models declare a single time_entity
    # join and never bind to a graph entity (they are not reachable via
    # inferred multi-hop joins).
    fact_models: dict[str, dict[str, Any]] = {}
    regular_model_rows: dict[str, dict[str, Any]] = {}
    for model_id, model in model_rows.items():
        model_kind = str(model.get("kind", "model") or "model").strip().lower()
        if model_kind == "fact":
            fact_models[model_id] = model
        elif model_kind == "model":
            regular_model_rows[model_id] = model
        else:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' has invalid kind '{model_kind}' "
                f"(allowed: model, fact)",
            )

    for entity_key, entity_spec_raw in graph_entities.items():
        entity_spec = dict(entity_spec_raw or {})
        entity_kind = str(entity_spec.get("kind", "regular") or "regular").strip().lower()
        if entity_kind not in {"regular", "time"}:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' has invalid kind '{entity_kind}' "
                f"(allowed: regular, time)",
            )
        model_id = str(entity_spec.get("model", "")).strip()
        if model_id not in model_rows:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' references unknown model '{model_id}'",
            )
        if model_id in fact_models:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' references fact model "
                f"'{model_id}' — fact models cannot be graph entities",
            )
        model = model_rows[model_id]
        model_keys = dict(model.get("keys", {}) or {})
        key, primary_role = _key_columns_and_role(
            entity_spec.get("key") or model_keys.get("primary") or model.get("grain"),
            default_role="primary",
        )
        if not key:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"{path}: graph entity '{entity_key}' must declare key"
            )
        if entity_kind == "time":
            time_calendar_id = str(model.get("calendar_id", "") or "default")
            if time_calendar_id in time_calendar_ids_seen:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: graph.entities declares more than one kind: time entity for "
                    f"calendar_id '{time_calendar_id}' (only one time entity per calendar is allowed)",
                )
            time_calendar_ids_seen.add(time_calendar_id)
        foreign_keys: dict[str, list[str]] = {}
        foreign_key_roles: dict[str, str] = {}
        for foreign_name, foreign_spec in dict(model_keys.get("foreign", {}) or {}).items():
            foreign_columns, foreign_role = _key_columns_and_role(
                foreign_spec, default_role="foreign"
            )
            foreign_keys[str(foreign_name)] = foreign_columns
            foreign_key_roles[str(foreign_name)] = foreign_role
        entity_id = str(entity_spec.get("id", f"entity.{_slug(entity_key)}"))
        entity_name = str(entity_spec.get("name", entity_key))
        entity_label = str(entity_spec.get("label", _titleize(entity_key)))
        relation_ref = str(model.get("relation_ref", model.get("relation", ""))).strip()
        relation_id = relation_aliases.get(relation_ref, "")
        relation_table = (
            relations_by_id[relation_id].output_name
            if relation_id
            else str(model.get("relation", ""))
        )
        if not relation_table:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' must declare relation or relation_ref",
            )
        entities.append(
            EntityConfig(
                id=entity_id,
                table=relation_table,
                primary_key=key[0],
                kind=entity_kind,
                relation_id=relation_id,
                key=key,
                identifiers=list(key),
                key_roles={column: primary_role for column in key},
                foreign_keys=foreign_keys,
                foreign_key_roles=foreign_key_roles,
                aliases=list(entity_spec.get("synonyms", []) or []),
                name=entity_name,
                label=entity_label,
                description=str(entity_spec.get("description", entity_label)),
                topics=_ensure_list(entity_spec.get("topics")),
                calendar_id=str(model.get("calendar_id", "")),
                allowed_as_root=bool(entity_spec.get("allowed_as_root", True)),
                freshness_source=str(
                    model.get("freshness_source", entity_spec.get("freshness_source", ""))
                ),
                freshness_sla_seconds=_optional_int(
                    model.get("freshness_sla_seconds", entity_spec.get("freshness_sla_seconds"))
                ),
                freshness_as_of=str(
                    model.get("freshness_as_of", entity_spec.get("freshness_as_of", "")) or ""
                ),
                disallowed_names=[
                    str(item).strip()
                    for item in _ensure_list(entity_spec.get("disallowed_names"))
                    if str(item).strip()
                ],
            )
        )
        entity_lookup[str(entity_key)] = entity_id
        entity_lookup[entity_name] = entity_id
        model_to_entity[model_id] = entity_id

    dimensions: list[DimensionConfig] = []
    temporal_roles: list[TemporalRoleConfig] = []
    measures: list[MeasureConfig] = []
    relationships: list[RelationshipConfig] = []
    value_domains: list[ValueDomainConfig] = []
    dimension_lookup: dict[tuple[str, str], str] = {}
    temporal_lookup: dict[tuple[str, str], str] = {}
    measure_lookup: dict[tuple[str, str], str] = {}
    value_domain_ids: dict[tuple[str, str], str] = {}

    dim_defaults = dict(defaults.get("dimension", {}) or {})
    time_defaults = dict(defaults.get("time", {}) or {})
    measure_defaults = dict(defaults.get("measure", {}) or {})
    relationship_defaults = dict(defaults.get("relationship", {}) or {})
    operational_contract = load_operational_contract(defaults, path=path)
    meta_contract = load_meta_contract(defaults, path=path)

    for model_id, model in model_rows.items():
        is_fact = model_id in fact_models
        model_meta = _normalize_meta(model.get("meta"))
        fact_source_relation = ""
        if is_fact:
            # Resolve the declared time entity for this fact model.
            time_entity_ref = str(model.get("time_entity", "") or "").strip()
            if not time_entity_ref:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' must declare time_entity",
                )
            time_entity_id = entity_lookup.get(time_entity_ref)
            if time_entity_id is None:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' references unknown "
                    f"time_entity '{time_entity_ref}'",
                )
            entity_cfg = next(row for row in entities if row.id == time_entity_id)
            if entity_cfg.kind != "time":
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' time_entity must point at "
                    f"a kind:time entity (got '{entity_cfg.kind}')",
                )
            entity_id = time_entity_id
            # Resolve the fact model's own relation (its FROM table) — measures
            # built from this model carry this as `source_relation`.
            fact_relation_ref = str(model.get("relation_ref", model.get("relation", ""))).strip()
            fact_relation_id = relation_aliases.get(fact_relation_ref, "")
            fact_source_relation = (
                relations_by_id[fact_relation_id].output_name
                if fact_relation_id
                else str(model.get("relation", ""))
            )
            if not fact_source_relation:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' must declare relation",
                )
        else:
            entity_id = model_to_entity[model_id]
            entity_cfg = next(row for row in entities if row.id == entity_id)
        model_topics = _ensure_list(model.get("topics"))
        model_operational_defaults = normalize_operational_payload(
            model.get("operational_defaults"),
            contract=operational_contract,
            target="measure",
            path=f"{path}: model '{model_id}' operational_defaults",
        )
        dims = dict(model.get("dimensions", {}) or {})
        times = dict(model.get("times", {}) or {})

        for dim_key, dim_spec_raw in dims.items():
            dim_spec = {**dim_defaults, **dict(dim_spec_raw or {})}
            dim_id = str(dim_spec.get("id", f"dimension.{_slug(entity_cfg.name)}_{_slug(dim_key)}"))
            label = str(dim_spec.get("label", _titleize(dim_key)))
            name = str(dim_spec.get("name", f"{entity_cfg.name}.{dim_key}"))
            kind = str(dim_spec.get("kind", dim_spec.get("type", "categorical")))
            domain_values = list(dim_spec.get("domain", dim_spec.get("valid_values", [])) or [])
            value_domain_id = ""
            if domain_values:
                value_domain_id = str(
                    dim_spec.get(
                        "value_domain_id", f"value_domain.{_slug(entity_cfg.name)}_{_slug(dim_key)}"
                    )
                )
                value_domains.append(
                    ValueDomainConfig(
                        id=value_domain_id,
                        dimensions=[dim_id],
                        values=[
                            ValueDomainValue(
                                value=value.get("value") if isinstance(value, dict) else value,
                                label=str(
                                    value.get("label", value.get("value"))
                                    if isinstance(value, dict)
                                    else value
                                ),
                                aliases=_ensure_list(value.get("aliases", []))
                                if isinstance(value, dict)
                                else [],
                                description=str(
                                    value.get(
                                        "description", value.get("label", value.get("value", ""))
                                    )
                                    if isinstance(value, dict)
                                    else ""
                                ),
                            )
                            for value in domain_values
                        ],
                        name=name,
                        label=label,
                        description=str(dim_spec.get("description", label)),
                    )
                )
                value_domain_ids[(model_id, dim_key)] = value_domain_id
            dimensions.append(
                DimensionConfig(
                    id=dim_id,
                    entity=entity_id,
                    column=str(dim_spec.get("column", dim_key)),
                    data_type=_map_dimension_kind(kind),
                    aliases=list(dim_spec.get("synonyms", []) or []),
                    name=name,
                    label=label,
                    description=str(dim_spec.get("description", label)),
                    semantic_kind=kind,
                    topics=_unique_aliases(model_topics, _ensure_list(dim_spec.get("topics"))),
                    preferred_filter_ops=_ensure_list(dim_spec.get("preferred_filter_ops")),
                    sample_values_strategy=str(dim_spec.get("sample_values_strategy", "")),
                    filterable=bool(dim_spec.get("filterable", True)),
                    groupable=bool(dim_spec.get("groupable", True)),
                    value_domain=value_domain_id,
                )
            )
            dimension_lookup[(model_id, dim_key)] = dim_id

        for time_key, time_spec_raw in times.items():
            time_spec = {**time_defaults, **dict(time_spec_raw or {})}
            dim_id = str(
                time_spec.get(
                    "dimension_id", f"dimension.{_slug(entity_cfg.name)}_{_slug(time_key)}"
                )
            )
            label = str(time_spec.get("label", _titleize(time_key)))
            name = str(time_spec.get("name", f"{entity_cfg.name}.{time_key}"))
            if (model_id, time_key) not in dimension_lookup:
                dimensions.append(
                    DimensionConfig(
                        id=dim_id,
                        entity=entity_id,
                        column=str(time_spec.get("column", time_key)),
                        data_type=_map_dimension_kind(time_spec.get("kind", "timestamp")),
                        name=name,
                        label=label,
                        description=str(time_spec.get("description", label)),
                        semantic_kind=str(time_spec.get("kind", "timestamp")),
                        topics=_unique_aliases(model_topics, _ensure_list(time_spec.get("topics"))),
                        preferred_filter_ops=_ensure_list(time_spec.get("preferred_filter_ops")),
                        sample_values_strategy=str(time_spec.get("sample_values_strategy", "")),
                        filterable=bool(time_spec.get("filterable", True)),
                        groupable=bool(time_spec.get("groupable", True)),
                    )
                )
                dimension_lookup[(model_id, time_key)] = dim_id
            temporal_id = str(
                time_spec.get("id", f"temporal_role.{_slug(entity_cfg.name)}_{_slug(time_key)}")
            )
            temporal_roles.append(
                TemporalRoleConfig(
                    id=temporal_id,
                    dimension=dimension_lookup[(model_id, time_key)],
                    temporal_class=str(
                        time_spec.get("class", time_spec.get("temporal_class", "event_time"))
                    ),
                    name=name,
                    label=label,
                    supported_grains=list(
                        time_spec.get(
                            "supported_grains", ["day", "week", "month", "quarter", "year"]
                        )
                    ),
                    default_query_time_axis=bool(
                        time_spec.get("default_query_axis", time_spec.get("default", False))
                    ),
                    timezone=str(time_spec.get("timezone", "UTC")),
                    column_timezone=str(time_spec.get("column_timezone", "") or ""),
                )
            )
            temporal_lookup[(model_id, time_key)] = temporal_id

        default_time = str(model.get("default_time", "")).strip()
        # Authoring sugar: a `times.<key>.default: true` flag replaces the
        # separate `default_time:` field. If multiple flags are set, the
        # multi-default-time validator catches it.
        if not default_time:
            for time_key, time_spec_raw in times.items():
                time_spec = dict(time_spec_raw or {})
                if bool(time_spec.get("default")):
                    default_time = str(time_key)
                    break
        row_grain = _ensure_list(
            model.get("grain") or dict(model.get("keys", {}) or {}).get("primary")
        )
        # Fact models infer grain from the declared time_column (the row's
        # time-key on the fact table).
        if is_fact and not row_grain:
            time_column = str(model.get("time_column", "") or "").strip()
            if time_column:
                row_grain = [time_column]
        for measure_key, measure_spec_raw in dict(model.get("measures", {}) or {}).items():
            raw_measure_spec = dict(measure_spec_raw or {})
            measure_spec = {**measure_defaults, **raw_measure_spec}
            if "primitive" in measure_spec:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: measure '{measure_key}' uses 'primitive:' shorthand which has been removed; expand to explicit 'kind' / 'accumulation' / 'value_type' fields",
                )
            measure_id = str(
                measure_spec.get("id", f"measure.{_slug(entity_cfg.name)}_{_slug(measure_key)}")
            )
            measure_name = str(measure_spec.get("name", f"{entity_cfg.name}.{measure_key}"))
            measure_label = str(measure_spec.get("label", _titleize(measure_key)))
            default_aggregation, allowed_aggregations, invalid_aggregations, measure_class = (
                _derive_measure_semantics(measure_spec)
            )
            # Author-facing override: `default_agg:` is canonical.
            authored_default_agg = measure_spec.get("default_agg")
            if authored_default_agg is not None:
                authored_default_agg_value = str(authored_default_agg).strip().lower()
                if authored_default_agg_value:
                    default_aggregation = authored_default_agg_value
            # Author-facing subtraction: `disallowed_aggregations:` removes the
            # listed aggregations from the derived allowed set.
            disallowed = {
                str(item).strip().lower()
                for item in _ensure_list(measure_spec.get("disallowed_aggregations"))
                if str(item).strip()
            }
            if disallowed:
                allowed_aggregations = [
                    agg for agg in allowed_aggregations if agg.lower() not in disallowed
                ]
                invalid_aggregations = list(invalid_aggregations) + [
                    agg
                    for agg in disallowed
                    if agg not in {a.lower() for a in invalid_aggregations}
                ]
            if default_aggregation and default_aggregation not in allowed_aggregations:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: measure '{measure_key}' has default_agg '{default_aggregation}' which is not in allowed_aggregations {allowed_aggregations}",
                )
            kind = str(measure_spec.get("kind", "")).strip().lower()
            authoring_warnings: list[str] = []
            raw_aggregation = str(raw_measure_spec.get("aggregation", "") or "").strip().lower()
            if raw_aggregation == "count_distinct" and kind != "entity_count":
                authoring_warnings.append(
                    f"measure '{measure_id}' declares aggregation: count_distinct, but measure-level aggregation is ignored for kind '{kind or 'additive'}'; use kind: entity_count with entity_key for count-distinct measures"
                )
            entity_key = _ensure_list(measure_spec.get("entity_key") or row_grain)
            if kind == "entity_count":
                expr_raw = measure_spec.get("expr")
                if expr_raw is None:
                    if len(entity_key) != 1:
                        raise SemanticLayerError(
                            "INVALID_CONFIG",
                            f"{path}: entity_count measure '{measure_key}' requires a single-column entity_key",
                        )
                    expr_raw = {"kind": "column", "column": entity_key[0]}
            else:
                expr_raw = measure_spec.get("expr")
                if expr_raw is None:
                    if not kind:
                        raise SemanticLayerError(
                            "INVALID_CONFIG",
                            f"{path}: measure '{measure_key}' declares neither kind nor expr; "
                            f"declare kind: aggregate with an expr:, or kind: entity_count "
                            f"with an entity_key:",
                        )
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"{path}: measure '{measure_key}' (kind '{kind}') is missing expr",
                    )
            temporal_refs = _ensure_list(
                measure_spec.get("times")
                or measure_spec.get("time")
                or ([default_time] if default_time else [])
            )
            compatible_temporal_roles = [
                temporal_lookup[(model_id, ref)] if (model_id, ref) in temporal_lookup else str(ref)
                for ref in temporal_refs
            ]
            _, structured_example_entries = _normalize_examples(measure_spec.get("examples"))
            measure_topics = _unique_aliases(
                model_topics, _ensure_list(measure_spec.get("topics"))
            ) or _default_topics(measure_name, fallback=model_id)
            default_temporal_role = str(
                measure_spec.get("default_temporal_role")
                or (compatible_temporal_roles[0] if compatible_temporal_roles else "")
            )
            measure_operational = validate_operational_payload(
                merge_operational_payloads(
                    model_operational_defaults,
                    normalize_operational_payload(
                        measure_spec.get("operational"),
                        contract=operational_contract,
                        target="measure",
                        path=f"{path}: measure '{measure_id}' operational",
                    ),
                ),
                contract=operational_contract,
                target="measure",
                path=f"{path}: measure '{measure_id}' operational",
            )
            measure = MeasureConfig(
                id=measure_id,
                entity=entity_id,
                subject_entity=entity_id
                if str(measure_spec.get("subject_entity", "self")) == "self"
                else entity_lookup.get(
                    str(measure_spec.get("subject_entity")), str(measure_spec.get("subject_entity"))
                ),
                aggregation_entity=entity_id
                if str(measure_spec.get("aggregation_entity", "self")) == "self"
                else entity_lookup.get(
                    str(measure_spec.get("aggregation_entity")),
                    str(measure_spec.get("aggregation_entity")),
                ),
                row_grain=list(row_grain),
                source_relation=fact_source_relation,
                expr=parse_config_expression(expr_raw),
                default_aggregation=default_aggregation,
                allowed_aggregations=allowed_aggregations,
                invalid_aggregations=invalid_aggregations,
                measure_class=measure_class,
                accumulation=_normalize_accumulation(measure_spec),
                compatible_temporal_roles=compatible_temporal_roles,
                value_type=str(measure_spec.get("value_type", "number")),
                currency=str(measure_spec.get("currency", "")),
                aliases=list(measure_spec.get("synonyms", []) or []),
                name=measure_name,
                label=measure_label,
                description=str(measure_spec.get("description", measure_label)),
                topics=measure_topics,
                suggested_aggregations=_ensure_list(measure_spec.get("suggested_aggregations"))
                or _suggested_aggregations(
                    default_aggregation, allowed_aggregations, measure_class
                ),
                comparison_family=str(measure_spec.get("comparison_family", "")),
                comparison_mode=str(measure_spec.get("comparison_mode", "")),
                comparison_peers=_ensure_list(measure_spec.get("comparison_peers")),
                clock_variants=_ensure_list(measure_spec.get("clock_variants")),
                preferred_companion_metrics=_ensure_list(
                    measure_spec.get("preferred_companion_metrics")
                ),
                operational=measure_operational,
                default_temporal_role=default_temporal_role,
                meta=_merge_meta(model_meta, _normalize_meta(measure_spec.get("meta"))),
                example_entries=structured_example_entries,
                validity_windows=_parse_validity_windows(measure_spec.get("validity_windows")),
                external_discontinuities=_parse_external_discontinuities(
                    measure_spec.get("external_discontinuities")
                ),
                cross_window_policy=str(
                    measure_spec.get("cross_window_policy", "caveat") or "caveat"
                ),
                authoring_warnings=authoring_warnings,
            )
            measures.append(measure)
            measure_lookup[(model_id, str(measure_key))] = measure_id

        keys = dict(model.get("keys", {}) or {})
        foreign_keys = {}
        foreign_key_roles = {}
        for foreign_name, foreign_spec in dict(keys.get("foreign", {}) or {}).items():
            foreign_columns, foreign_role = _key_columns_and_role(
                foreign_spec, default_role="foreign"
            )
            foreign_keys[str(foreign_name)] = foreign_columns
            foreign_key_roles[str(foreign_name)] = foreign_role
        for edge_key, join_spec_raw in dict(model.get("joins", {}) or {}).items():
            join_spec = {**relationship_defaults, **dict(join_spec_raw or {})}
            target_ref = str(join_spec.get("to", edge_key))
            if target_ref not in entity_lookup:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: join '{model_id}.{edge_key}' references unknown graph entity '{target_ref}'",
                )
            local_cols = _ensure_list(join_spec.get("via") or foreign_keys.get(edge_key))
            target_cfg = next(row for row in entities if row.id == entity_lookup[target_ref])
            target_cols = _ensure_list(join_spec.get("target") or target_cfg.key)
            if len(local_cols) != len(target_cols):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: join '{model_id}.{edge_key}' key width does not match target key",
                )
            local_role = str(
                join_spec.get("source_key_role", foreign_key_roles.get(str(edge_key), "")) or ""
            ).lower()
            if not local_role:
                local_role = (
                    "primary"
                    if all(
                        column in set(entity_cfg.key or [entity_cfg.primary_key])
                        for column in local_cols
                    )
                    else "foreign"
                )
            target_role = str(join_spec.get("target_key_role", "") or "").lower()
            if not target_role:
                target_roles = {
                    target_cfg.key_roles.get(column, "primary") for column in target_cols
                }
                target_role = next(iter(target_roles)) if len(target_roles) == 1 else "natural"
            if "cardinality" in join_spec:
                cardinality = str(join_spec["cardinality"])
            elif local_role in {"primary", "unique"} and target_role in {"primary", "unique"}:
                cardinality = "1:1"
            elif local_role == "foreign" and target_role in {"primary", "unique"}:
                cardinality = "N:1"
            elif local_role in {"primary", "unique"} and target_role == "foreign":
                cardinality = "1:N"
            else:
                cardinality = "M:N"
            safety_default = "safe" if cardinality in {"1:1", "N:1"} else "requires_rewrite"
            safety = str(join_spec.get("safety", safety_default)).lower()
            if safety not in {"safe", "requires_rewrite", "unsafe"}:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: join '{model_id}.{edge_key}' has invalid safety '{safety}' (must be 'safe', 'requires_rewrite', or 'unsafe')",
                )
            relationships.append(
                RelationshipConfig(
                    id=str(
                        join_spec.get("id", f"relationship.{_slug(model_id)}_{_slug(edge_key)}")
                    ),
                    source_entity=entity_id,
                    target_entity=entity_lookup[target_ref],
                    source_column=local_cols[0],
                    target_column=target_cols[0],
                    cardinality=cardinality,
                    safety=safety,
                    source_columns=list(local_cols),
                    target_columns=list(target_cols),
                    source_key_role=local_role,
                    target_key_role=target_role,
                    name=str(join_spec.get("name", f"{entity_cfg.name}_TO_{target_cfg.name}")),
                    label=str(join_spec.get("label", f"{entity_cfg.label} to {target_cfg.label}")),
                    description=str(join_spec.get("description", join_spec.get("label", ""))),
                    path_preference=int(join_spec.get("path_preference", 100) or 100),
                    allowed_directions=list(
                        join_spec.get("traversal", ["forward", "reverse"]) or ["forward", "reverse"]
                    ),
                    temporal_validity={
                        str(k): str(v)
                        for k, v in dict(join_spec.get("temporal_validity", {}) or {}).items()
                    },
                    target_key_type=str(join_spec.get("target_key_type", "primary") or "primary"),
                    join_semantics=str(join_spec.get("join_semantics", "")),
                    rollup_safe_aggregations=_ensure_list(
                        join_spec.get("rollup_safe_aggregations")
                    ),
                    rollup_safe_aggregations_reverse=_ensure_list(
                        join_spec.get("rollup_safe_aggregations_reverse")
                    ),
                )
            )

    metric_recipes_by_id: dict[str, MetricConfig] = {}

    def _add_metric_recipe(metric: MetricConfig, *, source: str) -> None:
        existing = metric_recipes_by_id.get(metric.id)
        if existing is not None:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: duplicate metric id '{metric.id}' in {existing.name or existing.id} and {source}; metric ids must be unique",
            )
        metric_recipes_by_id[metric.id] = metric

    # Auto-publish path: measures auto-create metric records for legacy
    # packages. Disabled under schema_strict: true, where every metric must
    # be authored explicitly.
    schema_strict = bool(package_raw.get("schema_strict", False))
    if not schema_strict:
        for model_id, model in model_rows.items():
            for measure_key, measure_spec_raw in dict(model.get("measures", {}) or {}).items():
                measure_id = str(
                    dict(measure_spec_raw or {}).get(
                        "id",
                        f"measure.{_slug(next(row.name for row in entities if row.id == model_to_entity[model_id]))}_{_slug(measure_key)}",
                    )
                )
                measure = next(row for row in measures if row.id == measure_id)
                metric = _metric_from_measure(
                    measure,
                    dict(measure_spec_raw or {}),
                    operational_contract=operational_contract,
                    path=f"{path}: measure '{measure_id}'",
                )
                if metric is not None:
                    _add_metric_recipe(metric, source=f"measure '{measure_id}' publish")

    # Build a measure key/name → measure_id index so metric authoring can
    # use package-relative keys (`revenue_usd`) instead of fully qualified
    # IDs (`measure.jaffle.revenue_usd`).
    measure_key_index: dict[str, str] = {}
    for measure in measures:
        measure_key_index[measure.id] = measure.id
        if measure.name:
            measure_key_index[measure.name] = measure.id
        # Tail of the id (after `measure.<ns>.`) is the local key.
        tail = measure.id.split(".", 2)[-1] if measure.id.startswith("measure.") else ""
        if tail:
            measure_key_index[tail] = measure.id
    # Add the local-key index from the YAML side (the YAML mapping keys
    # under `measures:`).
    for _model_id, model in model_rows.items():
        for measure_key in dict(model.get("measures", {}) or {}):
            # Match against a measure already in `measures` whose tail matches
            # this key (best-effort).
            if str(measure_key) in measure_key_index:
                continue
            for measure in measures:
                tail = measure.id.split(".", 2)[-1] if measure.id.startswith("measure.") else ""
                if tail == str(measure_key):
                    measure_key_index[str(measure_key)] = measure.id
                    break

    def _resolve_measure_ref(ref: Any, *, model_id: str = "") -> str:
        text = str(ref or "").strip()
        if not text:
            return text
        if model_id and (model_id, text) in measure_lookup:
            return measure_lookup[(model_id, text)]
        if text in measure_key_index:
            return measure_key_index[text]
        return text  # leave as-is for downstream validation

    # Build a metric key/name → metric_id index so metric authoring can refer
    # to top-level metrics (not just measures) via package-relative keys.
    # Sources: metrics_rows (with id auto-derived by the package loader) and
    # metric.name when authored.
    metric_key_index: dict[str, str] = {}
    for metric_key, metric_spec_raw in metrics_rows.items():
        spec = dict(metric_spec_raw or {})
        metric_id = str(
            spec.get(
                "id",
                f"metric.{namespace}.{_slug(str(metric_key))}"
                if namespace
                else f"metric.{_slug(str(metric_key))}",
            )
        )
        metric_key_index[metric_id] = metric_id
        local_key = str(metric_key)
        # Local key (YAML mapping key under `metrics:`).
        if local_key and local_key not in metric_key_index:
            metric_key_index[local_key] = metric_id
        # Authored name (often namespace-qualified, e.g. `synth.premium_share`).
        name = str(spec.get("name", "") or "")
        if name and name not in metric_key_index:
            metric_key_index[name] = metric_id
        # Tail of the id (after `metric.<ns>.`) is also a valid local handle.
        if metric_id.startswith("metric."):
            tail = metric_id.split(".", 2)[-1]
            if tail and tail not in metric_key_index:
                metric_key_index[tail] = metric_id

    def _resolve_metric_ref_full(ref: Any) -> str:
        """Resolve a metric reference to a fully-qualified id.

        Resolution order:
          1. Already fully-qualified (`metric.<ns>.<key>`) — return as-is.
          2. Top-level metric (from the `metrics:` block) — preferred.
          3. Measure (auto-published as a metric via convention) — fallback.

        If the same key matches BOTH a top-level metric and a measure, we
        raise a clear ambiguity error pointing the author at both
        candidates and asking them to use the fully-qualified id.
        """
        text = str(ref or "").strip()
        if not text:
            return text
        if text.startswith("metric."):
            return text
        metric_hit = metric_key_index.get(text)
        measure_hit_id = measure_key_index.get(text)
        if metric_hit and measure_hit_id and measure_hit_id.startswith("measure."):
            measure_as_metric = measure_hit_id.replace("measure.", "metric.", 1)
            if metric_hit != measure_as_metric:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    (
                        f"{path}: ambiguous metric reference '{text}' — matches "
                        f"both top-level metric '{metric_hit}' and measure "
                        f"'{measure_hit_id}' (also reachable at "
                        f"'{measure_as_metric}'); use the fully-qualified id "
                        f"to disambiguate"
                    ),
                )
        if metric_hit:
            return metric_hit
        if measure_hit_id and measure_hit_id.startswith("measure."):
            return measure_hit_id.replace("measure.", "metric.", 1)
        return text  # leave as-is for downstream validation

    for metric_key, metric_spec_raw in metrics_rows.items():
        spec = dict(metric_spec_raw or {})
        if "primitive" in spec:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: metric '{metric_key}' uses 'primitive:' shorthand which has been removed; expand to explicit 'kind' / 'comparison_mode' fields",
            )

        # Translate direct named fields per metric kind into the runtime
        # expression AST shape. Authors can still write the AST directly
        # under `expression:` for long-tail kinds (`derived`, `conversion`).
        def _classify_ref(text: str) -> str:
            """Tells the ratio-operand expander whether a key resolves to
            a top-level metric or a measure (or neither). Returns one of
            'metric', 'measure', or '' when no match was found.
            """
            text = str(text or "").strip()
            if not text or text.startswith("metric.") or text.startswith("measure."):
                return ""
            if text in metric_key_index:
                return "metric"
            if text in measure_key_index:
                return "measure"
            return ""

        def _suggest_refs(text: str) -> list[str]:
            from difflib import get_close_matches

            candidates = sorted(
                key
                for key in (*measure_key_index, *metric_key_index)
                if "." not in key  # offer the short authoring keys, not full ids
            )
            return get_close_matches(str(text), candidates, n=3, cutoff=0.5)

        spec = _translate_metric_direct_fields(
            spec,
            resolve=_resolve_measure_ref,
            resolve_metric=_resolve_metric_ref_full,
            classify_ref=_classify_ref,
            context=f"{path}: metric '{metric_key}'",
            suggest=_suggest_refs,
        )
        expression = _convert_recipe_expr(dict(spec.get("expression", {}) or {}))
        if not expression:
            metric_kind = str(spec.get("kind", "") or "").strip().lower()
            requirement_by_kind = {
                "aggregate": "a measure: field",
                "semi_additive": "a measure: field",
                "cumulative": "a measure: field",
                "rolling": "a measure: field",
                "prior_period": "a measure: field",
                "period_to_date": "a measure: field",
                "ratio": "numerator: and denominator: fields",
                "derived": "an expression: block",
                "conversion": "an expression: block",
            }
            requirement = requirement_by_kind.get(metric_kind)
            if requirement is None:
                detail = (
                    f"unknown kind '{metric_kind}'; valid kinds: "
                    f"{', '.join(sorted(requirement_by_kind))}"
                    if metric_kind
                    else "it declares no kind:; valid kinds: "
                    + ", ".join(sorted(requirement_by_kind))
                )
            else:
                detail = f"kind '{metric_kind}' requires {requirement}"
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: metric '{metric_key}' produced no expression — {detail}",
            )
        temporal_role = str(spec.get("temporal_role", spec.get("time", ""))).strip()
        metric_id = str(spec.get("id", f"metric.{metric_key}"))
        _, metric_example_entries = _normalize_examples(spec.get("examples"))
        metric_compatible_temporal_roles = list(
            spec.get("compatible_temporal_roles", [temporal_role] if temporal_role else []) or []
        )
        metric = MetricConfig(
            id=metric_id,
            kind=str(spec.get("kind", "derived")),
            expression=parse_semantic_expression(expression, context="config"),
            temporal_role=temporal_role,
            compatible_temporal_roles=metric_compatible_temporal_roles,
            filter_spec=dict(spec.get("expression", {}) or {}).get("filter", {}) or {},
            window_spec=dict(spec.get("expression", {}) or {}).get("window", {}) or {},
            name=str(spec.get("name", metric_key)),
            label=str(spec.get("label", _titleize(metric_key))),
            description=str(spec.get("description", spec.get("label", ""))),
            topics=_ensure_list(spec.get("topics"))
            or _default_topics(str(spec.get("name", metric_key))),
            comparison_family=str(spec.get("comparison_family", "")),
            comparison_mode=str(spec.get("comparison_mode", "")),
            comparison_peers=_ensure_list(spec.get("comparison_peers")),
            clock_variants=_ensure_list(spec.get("clock_variants")),
            preferred_companion_metrics=_ensure_list(spec.get("preferred_companion_metrics")),
            operational=validate_operational_payload(
                normalize_operational_payload(
                    spec.get("operational"),
                    contract=operational_contract,
                    target="metric",
                    path=f"{path}: metric '{metric_id}' operational",
                ),
                contract=operational_contract,
                target="metric",
                path=f"{path}: metric '{metric_id}' operational",
            ),
            meta=_normalize_meta(spec.get("meta")),
            example_entries=metric_example_entries,
            value_type=str(spec.get("value_type", "number") or "number"),
        )
        _add_metric_recipe(metric, source=f"metric '{metric_key}'")

    # Defined once with the per-segment ``model_id`` threaded as an arg
    # rather than closed over. The previous form defined this function
    # inside the per-segment loop, closing over ``model_id`` — correct
    # by accident (immediate invocation in the same iteration) but a
    # well-known late-binding footgun if the function were ever stored
    # and called later. (The mainline ruff/mypy pass landed a parallel
    # default-arg fix; this hoisted form supersedes it.)
    def _resolve_segment_dimension_ref(value: Any, model_id_for_segment: str) -> str:
        ref = str(value or "").strip()
        if not ref:
            return ref
        if ref.startswith("dimension."):
            return ref
        if model_id_for_segment and (model_id_for_segment, ref) in dimension_lookup:
            return dimension_lookup[(model_id_for_segment, ref)]
        return ref

    segments: list[SegmentConfig] = []
    for segment_key, segment_spec_raw in segments_rows.items():
        spec = dict(segment_spec_raw or {})
        segment_id = str(spec.get("id", f"segment.{segment_key}"))
        entity_ref = str(spec.get("entity", "")).strip()
        entity_id = entity_lookup.get(entity_ref, entity_ref)
        membership = dict(spec.get("membership", {}) or {})
        model_id = next((key for key, value in model_to_entity.items() if value == entity_id), "")

        preview_dimensions = [
            _resolve_segment_dimension_ref(value, model_id)
            for value in _ensure_list(spec.get("preview_dimensions"))
        ]
        segments.append(
            SegmentConfig(
                id=segment_id,
                entity=entity_id,
                basis_metric=str(spec.get("basis_metric", spec.get("metric", ""))),
                preview_dimensions=preview_dimensions,
                where=list(membership.get("where", []) or []),
                metric_filters=list(membership.get("metric_filters", []) or []),
                time=dict(membership.get("time", {}) or {}),
                temporal_role_overrides={
                    str(k): str(v)
                    for k, v in dict(membership.get("temporal_role_overrides", {}) or {}).items()
                },
                path_policy=dict(membership.get("path_policy", {}) or {}),
                aliases=list(spec.get("synonyms", []) or []),
                name=str(spec.get("name", segment_id)),
                label=str(spec.get("label", _titleize(segment_key))),
                description=str(spec.get("description", spec.get("label", ""))),
                topics=_ensure_list(spec.get("topics"))
                or _default_topics(str(spec.get("name", segment_id)), fallback="segments"),
            )
        )

    policies = []
    for row in list(raw.get("semantic_policies", []) or []):
        row_dict = dict(row or {})
        policies.append(
            SemanticPolicyConfig(
                id=str(row_dict["id"]),
                kind=str(row_dict["kind"]),
                config={
                    key: value
                    for key, value in row_dict.items()
                    if key
                    not in {
                        "id",
                        "kind",
                        "object_ids",
                        "audiences",
                        "environments",
                        "roles",
                        "action",
                        "rationale",
                    }
                },
                object_ids=_ensure_list(row_dict.get("object_ids")),
                audiences=_ensure_list(row_dict.get("audiences")),
                environments=_ensure_list(row_dict.get("environments")),
                roles=_ensure_list(row_dict.get("roles")),
                action=str(row_dict.get("action", "")),
                rationale=str(row_dict.get("rationale", row_dict.get("rule", ""))),
            )
        )

    caveats = _parse_caveats(raw.get("semantic_caveats", []), path=path)

    entity_ids = {entity.id for entity in entities}
    measure_ids = {measure.id for measure in measures}
    dimension_ids = {dimension.id for dimension in dimensions}
    temporal_role_ids = {role.id for role in temporal_roles}
    model_measure_keys: dict[str, set[str]] = {
        model_id: set(dict(model.get("measures", {}) or {}))
        for model_id, model in model_rows.items()
    }
    model_dimension_keys: dict[str, set[str]] = {
        model_id: set(dict(model.get("dimensions", {}) or {}))
        for model_id, model in model_rows.items()
    }

    def _model_default_time_key(model: dict[str, Any]) -> str:
        text = str(model.get("default_time", "") or "").strip()
        if text:
            return text
        for time_key, time_spec_raw in dict(model.get("times", {}) or {}).items():
            if bool(dict(time_spec_raw or {}).get("default")):
                return str(time_key)
        times = dict(model.get("times", {}) or {})
        return str(next(iter(times), ""))

    def _resolve_dimension_ref(ref: Any, *, model_id: str = "") -> str:
        text = str(ref or "").strip()
        if not text:
            return ""
        if text in dimension_ids:
            return text
        if model_id and (model_id, text) in dimension_lookup:
            return dimension_lookup[(model_id, text)]
        return text

    def _resolve_entity_ref(ref: Any) -> str:
        text = str(ref or "").strip()
        return entity_lookup.get(text, text)

    def _aggregate_from_row(
        row_dict: dict[str, Any], *, model_id: str = ""
    ) -> AggregateRelationConfig:
        source_entity_ref = str(row_dict.get("source_entity", row_dict.get("entity", ""))).strip()
        source_entity = _resolve_entity_ref(source_entity_ref)
        if source_entity and source_entity not in entity_ids:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{row_dict.get('id', '')}' references unknown source_entity '{source_entity_ref}'",
            )
        raw_measure_bindings = row_dict.get("measures")
        measure_columns: dict[str, str] = {}
        measure_rollups: dict[str, str] = {}
        measure_aggregations: dict[str, str] = {}
        if isinstance(raw_measure_bindings, dict):
            measures_list: list[str] = []
            for measure_ref, binding_raw in raw_measure_bindings.items():
                measure_id = _resolve_measure_ref(measure_ref, model_id=model_id)
                measures_list.append(measure_id)
                binding = dict(binding_raw or {}) if isinstance(binding_raw, dict) else {}
                column = str(
                    binding.get("column", binding_raw if not isinstance(binding_raw, dict) else "")
                    or ""
                ).strip()
                if column:
                    measure_columns[measure_id] = column
                measure_rollups[measure_id] = str(binding.get("rollup", "") or "").strip()
                measure_aggregations[measure_id] = str(binding.get("aggregation", "") or "").strip()
        else:
            measures_list = [
                _resolve_measure_ref(item, model_id=model_id)
                for item in _ensure_list(raw_measure_bindings)
            ]
        raw_dimension_bindings = row_dict.get("dimensions")
        dimension_columns: dict[str, str] = {}
        if isinstance(raw_dimension_bindings, dict):
            dimensions_list: list[str] = []
            for dim_ref, binding_raw in raw_dimension_bindings.items():
                dim_id = _resolve_dimension_ref(dim_ref, model_id=model_id)
                dimensions_list.append(dim_id)
                binding = dict(binding_raw or {}) if isinstance(binding_raw, dict) else {}
                column = str(
                    binding.get("column", binding_raw if not isinstance(binding_raw, dict) else "")
                    or ""
                ).strip()
                if column:
                    dimension_columns[dim_id] = column
        else:
            dimensions_list = [
                _resolve_dimension_ref(item, model_id=model_id)
                for item in _ensure_list(raw_dimension_bindings)
            ]
        grain = str(row_dict.get("grain", row_dict.get("time_grain", ""))).strip().lower()
        eligible = _ensure_list(row_dict.get("eligible_time_grains")) or _coarser_time_grains(grain)
        relation_id = str(
            row_dict.get(
                "id",
                f"aggregate_relation.{_slug(str(row_dict.get('relation', 'aggregate')))}",
            )
        )
        source = str(row_dict.get("source", "default") or "default")
        if source != "default":
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' declares source '{source}', but MVP aggregate routing only supports source: default",
            )
        unknown_measures = sorted(
            item for item in measures_list if item and item not in measure_ids
        )
        if unknown_measures:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown measures {unknown_measures}",
            )
        unknown_dimensions = sorted(
            item for item in dimensions_list if item and item not in dimension_ids
        )
        if unknown_dimensions:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown dimensions {unknown_dimensions}",
            )
        temporal_role = str(row_dict.get("temporal_role", ""))
        if temporal_role and temporal_role not in temporal_role_ids:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown temporal_role '{temporal_role}'",
            )
        entity_grain = [
            _resolve_entity_ref(item) for item in _ensure_list(row_dict.get("entity_grain"))
        ]
        unknown_entity_grain = sorted(
            item for item in entity_grain if item and item not in entity_ids
        )
        if unknown_entity_grain:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown entity_grain entries {unknown_entity_grain}",
            )
        return AggregateRelationConfig(
            id=relation_id,
            relation=str(row_dict.get("relation", "")),
            source_entity=source_entity,
            measures=[item for item in measures_list if item],
            dimensions=[item for item in dimensions_list if item],
            temporal_role=temporal_role,
            grain=grain,
            entity_grain=entity_grain,
            filters=dict(row_dict.get("filters", {}) or {}),
            description=str(row_dict.get("description", "")),
            freshness_source=str(row_dict.get("freshness_source", "")),
            freshness_sla_seconds=_optional_int(row_dict.get("freshness_sla_seconds")),
            freshness_as_of=str(row_dict.get("freshness_as_of", "") or ""),
            model_id=str(row_dict.get("model_id", model_id) or ""),
            variant_id=str(row_dict.get("variant_id", "") or ""),
            source=source,
            time_column=str(row_dict.get("time_column", "") or ""),
            eligible_time_grains=[
                str(item).strip().lower() for item in eligible if str(item).strip()
            ],
            measure_columns=measure_columns,
            measure_rollups=measure_rollups,
            measure_aggregations=measure_aggregations,
            dimension_columns=dimension_columns,
            excluded_entities=[
                _resolve_entity_ref(item)
                for item in _ensure_list(row_dict.get("excluded_entities"))
            ],
            excluded_dimensions=[
                _resolve_dimension_ref(item, model_id=model_id)
                for item in _ensure_list(row_dict.get("excluded_dimensions"))
            ],
            selection_priority=int(row_dict.get("selection_priority", 0) or 0),
            equivalence_kind=str(row_dict.get("equivalence_kind", "") or ""),
        )

    def _aggregate_rows_from_model_variants() -> list[AggregateRelationConfig]:
        rows: list[AggregateRelationConfig] = []
        for model_id, model in model_rows.items():
            variants_raw = dict(model.get("variants", {}) or {})
            if not variants_raw:
                continue
            variants = _resolve_variant_specs(variants_raw, path=path, model_id=model_id)
            source_entity = model_to_entity.get(model_id, "")
            if not source_entity:
                continue
            default_time_key = _model_default_time_key(model)
            base_time_spec = dict(
                dict(model.get("times", {}) or {}).get(default_time_key, {}) or {}
            )
            for variant_id, variant in variants.items():
                grain_spec = (
                    dict(variant.get("grain", {}) or {})
                    if isinstance(variant.get("grain"), dict)
                    else {}
                )
                time_grain = (
                    str(
                        grain_spec.get("time", variant.get("time_grain", variant.get("grain", "")))
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if not time_grain or time_grain == "transaction":
                    continue
                relation = str(variant.get("relation", "") or "").strip()
                if not relation:
                    continue
                excludes = dict(variant.get("excludes", {}) or {})
                excluded_dimensions = {
                    _resolve_dimension_ref(item, model_id=model_id)
                    for item in _ensure_list(excludes.get("dimensions"))
                }
                excluded_entities = {
                    _resolve_entity_ref(item) for item in _ensure_list(excludes.get("entities"))
                }
                excluded_measures = {
                    _resolve_measure_ref(item, model_id=model_id)
                    for item in _ensure_list(excludes.get("measures"))
                }
                columns = dict(variant.get("columns", {}) or {})
                measure_bindings: dict[str, dict[str, str]] = {}
                for measure_key in sorted(model_measure_keys.get(model_id, set())):
                    measure_id = _resolve_measure_ref(measure_key, model_id=model_id)
                    if measure_id in excluded_measures:
                        continue
                    raw_measure_spec = dict(
                        dict(model.get("measures", {}) or {}).get(measure_key, {}) or {}
                    )
                    raw_column = columns.get(measure_id, columns.get(measure_key, measure_key))
                    if isinstance(raw_column, dict):
                        column = str(raw_column.get("column", "") or "").strip()
                        rollup = str(
                            raw_column.get("rollup", raw_measure_spec.get("rollup", "")) or ""
                        ).strip()
                        aggregation = str(raw_column.get("aggregation", "sum") or "sum").strip()
                    else:
                        column = str(raw_column or "").strip()
                        rollup = str(
                            raw_measure_spec.get("rollup", "additive") or "additive"
                        ).strip()
                        aggregation = "sum"
                    measure_bindings[measure_id] = {
                        "column": column,
                        "rollup": rollup,
                        "aggregation": aggregation,
                    }
                dimension_bindings: dict[str, dict[str, str]] = {}
                for dim_key in sorted(model_dimension_keys.get(model_id, set())):
                    dim_id = _resolve_dimension_ref(dim_key, model_id=model_id)
                    dim_cfg = next((row for row in dimensions if row.id == dim_id), None)
                    raw_dim_spec = dict(
                        dict(model.get("dimensions", {}) or {}).get(dim_key, {}) or {}
                    )
                    if dim_id in excluded_dimensions:
                        continue
                    if (
                        str(raw_dim_spec.get("kind", "") or "").lower() == "id"
                        and dim_key not in columns
                        and dim_id not in columns
                    ):
                        continue
                    raw_column = columns.get(dim_id, columns.get(dim_key, ""))
                    column = str(
                        raw_column or getattr(dim_cfg, "column", dim_key) or dim_key
                    ).strip()
                    dimension_bindings[dim_id] = {"column": column}
                time_spec = dict(variant.get("time", {}) or {})
                role_ref = str(
                    time_spec.get("role", variant.get("temporal_role", default_time_key)) or ""
                ).strip()
                temporal_role = temporal_lookup.get((model_id, role_ref), role_ref)
                time_column = str(
                    time_spec.get(
                        "column",
                        variant.get("time_column", base_time_spec.get("column", default_time_key)),
                    )
                    or ""
                ).strip()
                selection = dict(variant.get("selection", {}) or {})
                equivalence = dict(variant.get("equivalence", {}) or {})
                rows.append(
                    _aggregate_from_row(
                        {
                            "id": str(
                                variant.get(
                                    "id",
                                    f"aggregate_relation.{_slug(model_id)}_{_slug(variant_id)}",
                                )
                            ),
                            "relation": relation,
                            "source_entity": source_entity,
                            "temporal_role": temporal_role,
                            "time_column": time_column,
                            "time_grain": time_grain,
                            "entity_grain": _ensure_list(grain_spec.get("entities")),
                            "measures": measure_bindings,
                            "dimensions": dimension_bindings,
                            "eligible_time_grains": _ensure_list(
                                variant.get("eligible_time_grains")
                            )
                            or _coarser_time_grains(time_grain),
                            "excluded_entities": list(excluded_entities),
                            "excluded_dimensions": list(excluded_dimensions),
                            "selection_priority": int(selection.get("priority", 0) or 0),
                            "equivalence_kind": str(equivalence.get("kind", "exact") or "exact"),
                            "model_id": model_id,
                            "variant_id": variant_id,
                            "source": str(variant.get("source", "default") or "default"),
                            "description": str(variant.get("description", "") or ""),
                            "freshness_source": str(variant.get("freshness_source", "") or ""),
                            "freshness_sla_seconds": variant.get("freshness_sla_seconds"),
                            "freshness_as_of": str(variant.get("freshness_as_of", "") or ""),
                        },
                        model_id=model_id,
                    )
                )
        return rows

    aggregate_relations: list[AggregateRelationConfig] = []
    aggregate_relations.extend(_aggregate_rows_from_model_variants())
    for row in list(raw.get("aggregate_relations", []) or []):
        aggregate_relations.append(_aggregate_from_row(dict(row or {})))

    config = PackageConfig(
        version=1,
        package=_parse_package_meta(package_raw, path=path),
        entities=entities,
        dimensions=dimensions,
        temporal_roles=temporal_roles,
        relationships=relationships,
        value_domains=value_domains,
        measures=measures,
        metric_recipes=sorted(metric_recipes_by_id.values(), key=lambda row: row.id),
        segments=sorted(segments, key=lambda row: row.id),
        path_preferences=_parse_path_preferences(
            raw, entity_lookup=entity_lookup, relationships=relationships, path=path
        ),
        path_policy=_parse_path_policy(raw, path=path),
        semantic_policies=policies,
        semantic_caveats=caveats,
        aggregate_relations=aggregate_relations,
        relations=relations,
        operational_contract=operational_contract,
        meta_contract=meta_contract,
    )
    _ensure_unique_object_ids(config, path=path)
    _validate_caveat_refs(config, path=path)
    return config


def load_package_config(path: str) -> PackageConfig:
    raw = _load_package_source(path)
    version = int(raw.get("schema_version", 0))
    if version != 1:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: schema_version must be 1 (got {version!r})",
        )
    return _parse_package(normalize_package(raw), path=path)


@lru_cache(maxsize=1)
def _cached_package_paths() -> dict[str, str]:
    out: dict[str, str] = {}
    checked: list[str] = []
    for root in _project_roots():
        directory = os.path.join(root, "configs", "semantic_rails")
        checked.append(directory)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            full = os.path.join(directory, name)
            if os.path.isdir(full) and os.path.isfile(os.path.join(full, "package.yml")):
                out.setdefault(name, full)
            elif name.endswith(".yml") or name.endswith(".yaml"):
                package_id = os.path.splitext(name)[0]
                out.setdefault(package_id, full)
    if not out:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Missing package directory: {', '.join(checked)}"
        )
    return out


def list_package_paths() -> dict[str, str]:
    return dict(_cached_package_paths())


list_package_paths.cache_clear = _cached_package_paths.cache_clear  # type: ignore[attr-defined]


def list_package_ids() -> list[str]:
    return sorted(list_package_paths().keys())


def get_package_path(package_id: str) -> str:
    paths = list_package_paths()
    if package_id not in paths:
        raise SemanticLayerError("INVALID_CONFIG", f"Unknown package '{package_id}'")
    return paths[package_id]


def get_package_config(package_id: str) -> PackageConfig:
    return load_package_config(get_package_path(package_id))
