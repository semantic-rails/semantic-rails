"""Package authoring health checks — backs ``parse-config`` and ``check``.

Loads a package, runs schema checks, and probes warehouse reachability
where requested. :func:`parse_config_report` is the CLI's
``parse-config`` entry; :func:`validate_config_report` extends it with
the runtime compile-time verifier so authoring CI can fail on the same
errors production would raise.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields, is_dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import yaml

from .ast import NormalizedQuery
from .compiler import (
    _collect_conversion_exprs,
    _conversion_sources,
    _expr_leaf_temporal_role_sets,
    _requires_query_time,
    compile_query,
)
from .config import (
    SEED_KIND_EXTERNAL,
    _merge_package_dir,
    get_package_path,
    load_package_config,
    package_root_for_source,
)
from .config_parts.shape_checks import (
    _METRIC_KEYS,
    _SEGMENT_KEYS,
    _VALID_COMPILED_DIMENSION_KINDS,
    _check_package_shapes,
    _check_typed_field_enums,
    add_error,
)
from .diagnostics import object_id_suggestions, recovery_hints_for_error
from .dialects import (
    connection_option_errors,
    snowflake_native_direct_connect_errors,
    supported_warehouses,
    warehouse_connector,
)
from .errors import SemanticLayerError
from .expressions import (
    AggregateExpr,
    ConversionExpr,
    MetricPredicateExpr,
    _opaque_expression_data,
    expr_to_dict,
    parse_semantic_expression,
    resolve_filter_dimension,
)
from .meta_contract import validate_meta_payload
from .metadata_parts.valid_values import max_valid_values_limit, valid_values_payload
from .package_snapshot import LoadedPackageSnapshot, capture_package_source, load_package_snapshot
from .registry import Registry
from .runtime import Runtime, runtime_request_scope
from .segments import build_segment_query, normalize_segment
from .semantic_collisions import semantic_collision_warnings
from .yaml_loader import safe_load as yaml_safe_load

ProgressFn = Callable[[str], None]


def _connection_options_from_mapping(
    connection: dict[str, Any], path: str, errors: list[str]
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    raw_options = connection.get("options", {}) or {}
    if raw_options:
        option_mapping = expect_mapping(raw_options, f"{path}.options", errors)
        if option_mapping is not None:
            options.update(option_mapping)
    for key, value in connection.items():
        if key not in {"kind", "name", "options"}:
            options.setdefault(str(key), value)
    return options


@dataclass(frozen=True)
class PackageReference:
    source_path: str
    package_id: str = ""

    @property
    def package_root(self) -> str:
        return package_root_for_source(self.source_path)

    @property
    def display_name(self) -> str:
        return self.package_id or self.source_path


def resolve_package_reference(*, package_id: str = "", path: str = "") -> PackageReference:
    package_id = str(package_id or "").strip()
    path = str(path or "").strip()
    if bool(package_id) == bool(path):
        raise SemanticLayerError("INVALID_CONFIG", "Provide exactly one of --package or --path")
    if package_id:
        return PackageReference(source_path=get_package_path(package_id), package_id=package_id)
    source_path = os.path.abspath(path)
    if not os.path.exists(source_path):
        raise SemanticLayerError("INVALID_CONFIG", f"Config path '{path}' does not exist")
    return PackageReference(source_path=source_path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_yaml(path: Path) -> Any:
    return yaml_safe_load(read_text(path))


def expect_mapping(value: Any, path: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        add_error(errors, f"{path} must be a mapping")
        return None
    return value


def expect_list(value: Any, path: str, errors: list[str]) -> list[Any] | None:
    if not isinstance(value, list):
        add_error(errors, f"{path} must be a list")
        return None
    return value


def _load_yaml_safe(path: Path, errors: list[str]) -> Any:
    try:
        return load_yaml(path)
    except yaml.YAMLError as exc:
        add_error(errors, f"{path}: invalid YAML: {exc}")
    except OSError as exc:
        add_error(errors, f"{path}: could not read file: {exc}")
    return None


def _load_model_file(path: Path, errors: list[str]) -> tuple[list[dict[str, Any]], Any]:
    raw = _load_yaml_safe(path, errors)
    root = expect_mapping(raw, path.as_posix(), errors)
    if root is None:
        return [], raw
    if "models" in root:
        models = expect_mapping(root.get("models"), f"{path}.models", errors)
        if models is None:
            return [], raw
        out: list[dict[str, Any]] = []
        for model_id, model_raw in models.items():
            model = expect_mapping(model_raw, f"{path}.models.{model_id}", errors)
            if model is not None:
                out.append(model)
        return out, raw
    model = expect_mapping(root.get("model", root), f"{path}.model", errors)
    return ([model] if model is not None else []), raw


def _load_relation_file(path: Path, errors: list[str]) -> tuple[dict[str, dict[str, Any]], Any]:
    raw = _load_yaml_safe(path, errors)
    root = expect_mapping(raw, path.as_posix(), errors)
    if root is None:
        return {}, raw
    if "relations" in root:
        relations = expect_mapping(root.get("relations"), f"{path}.relations", errors)
        return (
            {str(key): dict(value or {}) for key, value in dict(relations or {}).items()}
            if relations is not None
            else {},
            raw,
        )
    relation = expect_mapping(root.get("relation", root), f"{path}.relation", errors)
    if relation is None:
        return {}, raw
    relation_id = str(relation.get("id", path.stem))
    return {relation_id: relation}, raw


def _candidate_package_id(source_path: Path) -> str:
    return source_path.name if source_path.is_dir() else source_path.stem


def validate_runtime_package(path: Path) -> list[str]:
    errors: list[str] = []
    source_path = Path(path)
    if not source_path.exists():
        add_error(errors, f"missing runtime package: {source_path}")
        return errors

    if source_path.is_dir():
        return _validate_runtime_package_dir(source_path)
    return _validate_runtime_package_file(source_path)


def _validate_runtime_package_file(source_path: Path) -> list[str]:
    errors: list[str] = []
    raw = _load_yaml_safe(source_path, errors)
    if expect_mapping(raw, source_path.as_posix(), errors) is None:
        return errors
    # Single-file parity: run the same raw-YAML authoring checks the
    # directory validator runs (unknown keys, list-typed fields, enum
    # values) before attempting the full load, so shape mistakes surface
    # with targeted messages even when the load itself fails.
    _check_package_shapes(raw, path_label=source_path.as_posix(), errors=errors)
    raw_models = raw.get("models")
    if isinstance(raw_models, dict):
        _check_typed_field_enums(source_path.as_posix(), raw_models, errors)
    try:
        config = load_package_config(str(source_path))
    except Exception as exc:
        add_error(errors, f"{source_path}: failed to load package config: {exc}")
        return errors
    errors.extend(_compiled_package_errors(config, source_path))
    return errors


def _schema_version(package_root: dict[str, Any]) -> Any:
    """schema_version as the loader reads it, which also accepts "1"."""
    try:
        return int(package_root.get("schema_version", 0))
    except (TypeError, ValueError, OverflowError):
        return package_root.get("schema_version")


def _validate_runtime_package_dir(path: Path) -> list[str]:
    errors: list[str] = []
    package_yml = path / "package.yml"
    if not package_yml.exists():
        add_error(errors, f"{package_yml} is missing")
        return errors

    package_root = expect_mapping(
        _load_yaml_safe(package_yml, errors), package_yml.as_posix(), errors
    )
    if package_root is None:
        return errors
    schema_version = _schema_version(package_root)
    if schema_version == 1:
        graph_yml = path / "graph.yml"
        models_dir = path / "models"
        if not graph_yml.exists():
            # A directory holding only package.yml is usually a single-file
            # package (e.g. `semantic-rails init` output) loaded with the
            # directory form by mistake — point at the single-file form.
            add_error(
                errors,
                f"{graph_yml} is missing — found a single-file {package_yml}; "
                f"pass --path {package_yml} to load it",
            )
            return errors
        if not models_dir.is_dir():
            add_error(errors, f"{models_dir} is missing")
            return errors
        errors.extend(_validate_split_package(path, package_root, graph_yml, models_dir))

    try:
        config = load_package_config(str(path))
    except Exception as exc:
        add_error(errors, f"{path}: failed to load package config: {exc}")
        return errors

    if schema_version == 1 and config.package.package_id != path.name:
        add_error(
            errors,
            f"{path}: compiled package ID {config.package.package_id!r} does not match {path.name!r}",
        )
    errors.extend(_compiled_package_errors(config, path))
    return errors


def _validate_split_package(
    path: Path, package_root: dict[str, Any], graph_yml: Path, models_dir: Path
) -> list[str]:
    errors: list[str] = []
    if _schema_version(package_root) != 1:
        add_error(errors, f"{path / 'package.yml'}: schema_version must be 1")
    package = expect_mapping(package_root.get("package"), f"{path / 'package.yml'}.package", errors)
    if package is None:
        return errors
    if package.get("id") != path.name:
        add_error(
            errors, f"{path / 'package.yml'}: package.id must equal directory name {path.name!r}"
        )
    warehouse = str(package.get("warehouse", "duckdb") or "duckdb").strip().lower()
    connector = warehouse_connector(warehouse)
    if connector is None:
        add_error(
            errors,
            f"{path / 'package.yml'}: package.warehouse must be one of {', '.join(supported_warehouses())}",
        )
        connector = warehouse_connector("duckdb")
    if (
        connector
        and connector.requires_default_db
        and not str(package.get("default_db", "")).strip()
    ):
        add_error(
            errors, f"{path / 'package.yml'}: duckdb packages must declare package.default_db"
        )
    if connector and connector.requires_seed:
        seed = expect_mapping(package.get("seed"), f"{path / 'package.yml'}.package.seed", errors)
        if seed is not None:
            seed_kind = str(seed.get("kind", "")).strip()
            if not seed_kind:
                add_error(
                    errors,
                    f"{path / 'package.yml'}: duckdb packages must declare package.seed.kind",
                )
            if seed_kind == SEED_KIND_EXTERNAL:
                if str(seed.get("source", "")).strip() or str(seed.get("post_sql", "")).strip():
                    add_error(
                        errors,
                        f"{path / 'package.yml'}: package.seed.kind 'external' takes no "
                        "source or post_sql",
                    )
            elif not str(seed.get("source", "")).strip():
                add_error(
                    errors,
                    f"{path / 'package.yml'}: duckdb packages must declare package.seed.source",
                )
    if connector and connector.connection_kinds:
        connection = expect_mapping(
            package.get("connection"), f"{path / 'package.yml'}.package.connection", errors
        )
        if connection is not None:
            connection_kind = str(connection.get("kind", "")).strip()
            if connection_kind not in connector.connection_kinds:
                add_error(
                    errors,
                    f"{path / 'package.yml'}: {warehouse} packages must declare package.connection.kind in [{', '.join(connector.connection_kinds)}]",
                )
            connection_options = _connection_options_from_mapping(
                connection, f"{path / 'package.yml'}.package.connection", errors
            )
            for option_error in connection_option_errors(
                warehouse, connection_kind, connection_options
            ):
                add_error(
                    errors,
                    f"{path / 'package.yml'}: {warehouse} package.connection has invalid options: {option_error}",
                )
            connection_name = str(connection.get("name", "")).strip()
            if (
                warehouse == "snowflake"
                and connection_kind == "snowflake_cli"
                and not connection_name
            ):
                add_error(
                    errors,
                    f"{path / 'package.yml'}: snowflake_cli packages must declare package.connection.name",
                )
            elif (
                warehouse == "snowflake"
                and connection_kind == "snowflake_native"
                and not connection_name
            ):
                for direct_error in snowflake_native_direct_connect_errors(connection_options):
                    add_error(
                        errors,
                        f"{path / 'package.yml'}: {warehouse} package.connection has invalid options: {direct_error}",
                    )
            elif connector.requires_connection_name and not connection_name:
                add_error(
                    errors,
                    f"{path / 'package.yml'}: {warehouse} packages must declare package.connection.name",
                )
    elif connector:
        connection = package.get("connection")
        if isinstance(connection, dict) and str(connection.get("kind", "")).strip():
            add_error(
                errors,
                f"{path / 'package.yml'}: {warehouse} packages do not support package.connection.kind '{connection.get('kind')}'",
            )
        elif isinstance(connection, dict) and _connection_options_from_mapping(
            connection, f"{path / 'package.yml'}.package.connection", errors
        ):
            add_error(
                errors,
                f"{path / 'package.yml'}: {warehouse} packages do not support package.connection options",
            )

    graph_root = expect_mapping(_load_yaml_safe(graph_yml, errors), graph_yml.as_posix(), errors)
    if graph_root is None:
        return errors
    graph = expect_mapping(graph_root.get("graph"), f"{graph_yml}.graph", errors)
    if graph is None:
        return errors
    entities = expect_mapping(graph.get("entities"), f"{graph_yml}.graph.entities", errors)
    if entities is None or not entities:
        add_error(errors, f"{graph_yml}.graph.entities must not be empty")
        return errors

    model_files = sorted([*models_dir.rglob("*.yml"), *models_dir.rglob("*.yaml")])
    if not model_files:
        add_error(errors, f"{models_dir} must contain model files")
        return errors

    models: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}
    alias_docs: list[dict[str, Any]] = [package_root, graph_root]
    package_relations = package_root.get("relations")
    if isinstance(package_relations, dict):
        relations.update({str(key): dict(value or {}) for key, value in package_relations.items()})
    relations_yml = path / "relations.yml"
    if relations_yml.exists():
        raw_relations = _load_yaml_safe(relations_yml, errors)
        relation_root = expect_mapping(raw_relations, relations_yml.as_posix(), errors)
        if relation_root is not None:
            relation_rows = expect_mapping(
                relation_root.get("relations", relation_root), f"{relations_yml}.relations", errors
            )
            if relation_rows is not None:
                relations.update(
                    {str(key): dict(value or {}) for key, value in relation_rows.items()}
                )
    relations_dir = path / "relations"
    if relations_dir.is_dir():
        for relation_file in sorted(
            [*relations_dir.rglob("*.yml"), *relations_dir.rglob("*.yaml")]
        ):
            relation_entries, raw = _load_relation_file(relation_file, errors)
            if isinstance(raw, dict):
                alias_docs.append(raw)
            relations.update(relation_entries)
    relation_refs = set(relations)
    for key, relation in relations.items():
        relation_id = str(relation.get("id", key))
        relation_refs.add(relation_id)
        if relation_id.startswith("relation."):
            relation_refs.add(relation_id)
        if (
            not relation.get("source")
            and not relation.get("date_spine")
            and not relation.get("steps")
        ):
            add_error(errors, f"{path}: relation {key!r} must declare source/date_spine or steps")
        if relation.get("source") and relation.get("date_spine"):
            add_error(errors, f"{path}: relation {key!r} cannot declare both source and date_spine")
    for model_file in model_files:
        model_entries, raw = _load_model_file(model_file, errors)
        if isinstance(raw, dict):
            alias_docs.append(raw)
        for model in model_entries:
            model_id = str(model.get("id", "")).strip()
            model_path = (
                f"{model_file}.models.{model_id}"
                if isinstance(raw, dict) and "models" in raw
                else f"{model_file}.model"
            )
            if not model_id:
                add_error(errors, f"{model_file}: model.id is required")
                continue
            models[model_id] = model
            relation_ref = str(model.get("relation_ref", model.get("relation", "")) or "").strip()
            if not relation_ref:
                add_error(
                    errors,
                    f"{model_path}.relation must be declared unless relation_ref is supplied",
                )
            elif relation_ref.startswith("relation.") or relation_ref in relation_refs:
                relation_refs.add(relation_ref)
            # Under the v1 authoring contract, the `entities:` block on a
            # model declares the model's primary entity (and any FK refs).
            # The loader translates `entities:` into the canonical
            # `keys.primary:` form before runtime parsing — so when
            # `entities:` is present and both `keys:` and `grain:` are
            # omitted, that's valid authoring.
            model_kind = str(model.get("kind", "model") or "model").strip().lower()
            if model_kind == "fact":
                # Fact models declare a time_entity + time_column instead
                # of keys/grain — validated by the loader.
                pass
            elif "entities" in model and model.get("keys") is None and model.get("grain") is None:
                pass  # v1 entities-block authoring; loader will derive keys.
            elif model.get("keys") is None:
                grain = expect_list(model.get("grain"), f"{model_path}.grain", errors)
                if grain is not None and not grain:
                    add_error(errors, f"{model_path}.grain must not be empty when keys are omitted")
                continue
            else:
                keys = expect_mapping(model.get("keys"), f"{model_path}.keys", errors)
                if keys is None:
                    continue
                primary = expect_list(keys.get("primary"), f"{model_path}.keys.primary", errors)
                if primary is not None and not primary:
                    add_error(errors, f"{model_path}.keys.primary must not be empty")

    for entity_id, entity in entities.items():
        entity_path = f"{graph_yml}.graph.entities.{entity_id}"
        mapping = expect_mapping(entity, entity_path, errors)
        if mapping is None:
            continue
        entity_key = expect_list(mapping.get("key"), f"{entity_path}.key", errors)
        if entity_key is not None and not entity_key:
            add_error(errors, f"{entity_path}.key must not be empty")
        model_id = str(mapping.get("model", "")).strip()
        if model_id not in models:
            add_error(
                errors, f"{entity_path}.model {model_id!r} does not resolve to a declared model"
            )

    if any("aliases" in doc for doc in alias_docs if isinstance(doc, dict)):
        add_error(errors, f"{path}: package authoring should not use a top-level aliases registry")

    # Always-on enum checks for typed fields. A typo in `kind:` (e.g.
    # `catagorical` for `categorical`) would otherwise be silently stored
    # and yield a broken-but-loadable package — the worst class of
    # authoring bug. Reject unknown values with a clear list of options.
    _check_typed_field_enums(str(path), models, errors)

    # Authoring-shape checks (unknown keys, wrong-typed fields) on the
    # assembled blocks. Metric/segment specs come from the package's
    # metrics/ and segments/ trees.
    metrics_raw = _load_metric_files(path, errors)
    segments_raw = _load_segment_files(path, errors)
    _check_package_shapes(
        {
            "package": package_root.get("package"),
            "graph": graph_root.get("graph"),
            "models": models,
            **_loader_metrics_and_segments(path, errors),
        },
        path_label=str(path),
        errors=errors,
        top_level=False,
    )

    # Strict-mode raw-YAML checks (gated behind package.schema_strict: true).
    if bool(package.get("schema_strict", False)):
        _check_strict_raw_yaml(
            path,
            package_root,
            graph_root,
            models,
            errors,
            metrics=metrics_raw,
            segments=segments_raw,
        )

    return errors


def _loader_metrics_and_segments(path: Path, errors: list[str]) -> dict[str, Any]:
    """The metric and segment specs the loader reads from a package directory.

    Uses the loader's own source capture and merge, so the shape checks see every
    supported layout (specs in package.yml, root metrics.yml and segments.yml, and
    files under metrics/ and segments/: a mapping, a `metric:`/`segment:` wrapper or
    a bare spec), skip the directories the loader skips, and check the copy the
    loader keeps when a key is defined twice. If the merge fails, that is an error:
    the checks can't run, even when a later load succeeds.
    """
    try:
        source = capture_package_source(path)
        merged = _merge_package_dir(source.source_path, captured=source)
    except (
        SemanticLayerError,
        yaml.YAMLError,
        OSError,
        TypeError,
        ValueError,
        AttributeError,
    ) as exc:
        add_error(
            errors, f"{path}: can't read the metric and segment specs to check their keys: {exc}"
        )
        return {"metrics": {}, "segments": {}}
    return {"metrics": merged.get("metrics"), "segments": merged.get("segments")}


def _load_metric_files(
    package_path: Path, errors: list[str]
) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Load every metric YAML under <package>/metrics/ into a flat map of
    metric_key -> (file_path, raw_metric_dict). Used by strict-mode checks
    to inspect authored fields like `topics:`."""
    out: dict[str, tuple[Path, dict[str, Any]]] = {}
    metrics_dir = package_path / "metrics"
    if not metrics_dir.is_dir():
        return out
    for metric_file in sorted([*metrics_dir.rglob("*.yml"), *metrics_dir.rglob("*.yaml")]):
        raw = _load_yaml_safe(metric_file, errors)
        if not isinstance(raw, dict):
            continue
        metrics_block = raw.get("metrics")
        if isinstance(metrics_block, dict):
            for metric_key, metric_raw in metrics_block.items():
                if isinstance(metric_raw, dict):
                    out[str(metric_key)] = (metric_file, metric_raw)
        elif isinstance(raw.get("metric"), dict):
            metric_raw = raw["metric"]
            metric_key = str(metric_raw.get("name") or metric_raw.get("id") or metric_file.stem)
            out[metric_key] = (metric_file, metric_raw)
    return out


def _load_segment_files(
    package_path: Path, errors: list[str]
) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Load segment YAMLs into a flat map of segment_key -> (file, raw)."""
    out: dict[str, tuple[Path, dict[str, Any]]] = {}
    segments_dir = package_path / "segments"
    if not segments_dir.is_dir():
        return out
    for segment_file in sorted([*segments_dir.rglob("*.yml"), *segments_dir.rglob("*.yaml")]):
        raw = _load_yaml_safe(segment_file, errors)
        if not isinstance(raw, dict):
            continue
        segments_block = raw.get("segments")
        if isinstance(segments_block, dict):
            for segment_key, segment_raw in segments_block.items():
                if isinstance(segment_raw, dict):
                    out[str(segment_key)] = (segment_file, segment_raw)
        elif isinstance(raw.get("segment"), dict):
            segment_raw = raw["segment"]
            segment_key = str(segment_raw.get("name") or segment_raw.get("id") or segment_file.stem)
            out[segment_key] = (segment_file, segment_raw)
    return out


_STRICT_LEGACY_KEYS_ON_OBJECTS = {
    # Field name → migration message
    # Applied to dimensions and measures.
    "topics": "topics is metadata-only and dropped in v1; remove it",
    "preferred_filter_ops": "preferred_filter_ops has no planner gating; remove it",
    "clock_variants": "clock_variants is metadata-only and dropped; remove it",
    "comparison_peers": "comparison_peers is advisory-only and dropped; remove it",
    "preferred_companion_metrics": "preferred_companion_metrics is advisory-only and dropped; remove it",
}

# Subset applied to metrics and segments. preferred_companion_metrics
# stays on metrics (advisory governance metadata, see commit ef4c543).
_STRICT_LEGACY_KEYS_ON_METRICS = {
    "topics": "topics is metadata-only and dropped in v1; remove it",
    "preferred_filter_ops": "preferred_filter_ops has no planner gating; remove it",
    "clock_variants": "clock_variants is metadata-only and dropped; remove it",
    "comparison_peers": "comparison_peers is advisory-only and dropped; remove it",
}


def _check_strict_raw_yaml(
    path: Path,
    package_root: dict[str, Any],
    graph_root: dict[str, Any],
    models: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    metrics: dict[str, tuple[Path, dict[str, Any]]] | None = None,
    segments: dict[str, tuple[Path, dict[str, Any]]] | None = None,
) -> None:
    """Reject legacy authoring forms when schema_strict: true.

    Each rejection includes a migration pointer.
    """
    # 2. Top-level `relations:` block in canonical packages
    if package_root.get("relations") not in (None, {}, []):
        add_error(
            errors,
            f"{path / 'package.yml'}: top-level relations: block is experimental; "
            f"omit it from canonical packages.",
        )

    # 3. policies.yml plan_constraint
    for policy in list(package_root.get("semantic_policies") or []):
        if (
            isinstance(policy, dict)
            and str(policy.get("kind", "")).strip().lower() == "plan_constraint"
        ):
            add_error(
                errors,
                f"{path / 'package.yml'}: policy {policy.get('id', '')} has kind "
                f"'plan_constraint' which is a runtime no-op. Drop the policy or "
                f"use a real kind (package_release, object_visibility, "
                f"object_access, protected_object, metric_constraint).",
            )

    # 4. Graph entity strict checks
    graph = graph_root.get("graph", {}) or {}
    for entity_key, entity_raw in (graph.get("entities") or {}).items():
        if not isinstance(entity_raw, dict):
            continue
        if "id" in entity_raw:
            add_error(
                errors,
                f"{path}: graph.entities.{entity_key}.id is auto-derived from key; "
                f"remove it. Use 'as: <full_id>' only when preserving a public ID.",
            )
    # 5. Model strict checks
    for model_id, model in models.items():
        if not isinstance(model, dict):
            continue
        # Authored `id:` on the model
        if "id" in model and str(model.get("id", "")).strip() != model_id:
            add_error(
                errors,
                f"{path}: model {model_id!r} has authored 'id:' which differs from "
                f"the mapping key; under schema_strict the key drives the id.",
            )
        # Authored model.grain when entities: block can derive it
        if "entities" in model and "grain" in model:
            add_error(
                errors,
                f"{path}: model {model_id!r} authors both 'entities:' and 'grain:'. "
                f"Drop 'grain:' — it's derived from the primary entity's key.",
            )
        # Legacy singular `entity:` field (use `entities:` block instead)
        if "entity" in model and "entities" not in model:
            add_error(
                errors,
                f"{path}: model {model_id!r} authors the legacy singular 'entity:' "
                f"field. Use the 'entities:' block instead.",
            )
        # Legacy `joins:` block (use graph.relationships overrides)
        if "joins" in model:
            add_error(
                errors,
                f"{path}: model {model_id!r} authors the legacy 'joins:' block. "
                f"Move overrides to graph.relationships.<name>.",
            )
        # Legacy `keys.foreign:` block (use entities: block)
        keys = model.get("keys") if isinstance(model.get("keys"), dict) else {}
        if "foreign" in (keys or {}):
            add_error(
                errors,
                f"{path}: model {model_id!r} authors keys.foreign: which is "
                f"replaced by the entities: block (FK refs auto-derived).",
            )
        # Authored model.keys.primary when entities: derives it
        if "entities" in model and "primary" in (keys or {}):
            add_error(
                errors,
                f"{path}: model {model_id!r} authors keys.primary: alongside "
                f"entities:; the entities block derives it.",
            )

        # Per-object strict checks (dimensions, measures, metrics)
        for obj_kind, container in (
            ("dimension", model.get("dimensions") or {}),
            ("measure", model.get("measures") or {}),
        ):
            if not isinstance(container, dict):
                continue
            for obj_key, obj_raw in container.items():
                if not isinstance(obj_raw, dict):
                    continue
                # Authored `id:` (use `as:` instead)
                if "id" in obj_raw:
                    add_error(
                        errors,
                        f"{path}: {obj_kind} {model_id}.{obj_key} authors 'id:'. "
                        f"The id is auto-derived from the key; use 'as:' only to "
                        f"preserve a public ID.",
                    )
                # Authored `name:` matching auto-derived (best-effort; we just
                # flag any authored name as redundant under schema_strict).
                # Soft check: if name equals the key or label-derived form, warn.
                # (Skipped in strict-error mode; add as a warning if needed.)
                # Discouraged metadata-only fields
                for legacy_key, message in _STRICT_LEGACY_KEYS_ON_OBJECTS.items():
                    if legacy_key in obj_raw:
                        add_error(
                            errors,
                            f"{path}: {obj_kind} {model_id}.{obj_key}: {message}.",
                        )

        # Measure strict checks
        for measure_key, measure_raw in (model.get("measures") or {}).items():
            if not isinstance(measure_raw, dict):
                continue
            # Require explicit `kind:` on every measure under strict mode.
            # Common values: aggregate, entity_count. Without it, the loader
            # silently infers semantics from accumulation.kind, which makes
            # authoring-intent ambiguous.
            authored_kind = str(measure_raw.get("kind", "")).strip().lower()
            if not authored_kind:
                add_error(
                    errors,
                    f"{path}: measure {model_id}.{measure_key} omits 'kind:'. "
                    f"Declare 'kind: aggregate' (most measures), 'kind: entity_count' "
                    f"(distinct counts), or another canonical kind.",
                )
            # Legacy flat `accumulation: stock` + sibling `snapshot_policy:`
            accumulation = measure_raw.get("accumulation")
            if "snapshot_policy" in measure_raw:
                add_error(
                    errors,
                    f"{path}: measure {model_id}.{measure_key} authors "
                    f"snapshot_policy: alongside accumulation:. Use the nested "
                    f"form: accumulation: {'{ kind: stock, snapshot: ... }'}.",
                )
            # accumulation enum
            if isinstance(accumulation, str) and accumulation.strip().lower() not in {
                "",
                "flow",
                "stock",
                "event",
                "population",
            }:
                add_error(
                    errors,
                    f"{path}: measure {model_id}.{measure_key} has accumulation "
                    f"{accumulation!r}; allowed: flow, stock, event, population.",
                )
            elif isinstance(accumulation, dict):
                acc_kind = str(accumulation.get("kind", "") or "").strip().lower()
                if acc_kind and acc_kind not in {"flow", "stock", "event", "population"}:
                    add_error(
                        errors,
                        f"{path}: measure {model_id}.{measure_key} has "
                        f"accumulation.kind {acc_kind!r}; allowed: flow, stock, "
                        f"event, population.",
                    )

    # 6. Metric strict checks — topics:/clock_variants:/etc. are metadata-only
    # and dropped in v1 (keys outside _METRIC_KEYS are already reported as
    # unknown). preferred_companion_metrics stays; see _STRICT_LEGACY_KEYS_ON_METRICS.
    for metric_key, (metric_path, metric_raw) in (metrics or {}).items():
        for legacy_key, message in _STRICT_LEGACY_KEYS_ON_METRICS.items():
            if legacy_key in metric_raw and legacy_key in _METRIC_KEYS:
                add_error(
                    errors,
                    f"{metric_path}: metric {metric_key!r}: {message}.",
                )

    # 7. Segment strict checks — the same legacy fields, where _SEGMENT_KEYS allows them.
    for segment_key, (segment_path, segment_raw) in (segments or {}).items():
        for legacy_key, message in _STRICT_LEGACY_KEYS_ON_METRICS.items():
            if legacy_key in segment_raw and legacy_key in _SEGMENT_KEYS:
                add_error(
                    errors,
                    f"{segment_path}: segment {segment_key!r}: {message}.",
                )


def _compiled_package_errors(config, source_path: Path) -> list[str]:
    errors: list[str] = []
    if not config.entities:
        add_error(errors, f"{source_path}: compiled package must declare entities")
    if not config.temporal_roles:
        add_error(errors, f"{source_path}: compiled package must declare temporal roles")
    if not config.measures:
        add_error(errors, f"{source_path}: compiled package must declare measures")
    if not config.metric_recipes:
        add_error(errors, f"{source_path}: compiled package must declare metric recipes")

    for entity in config.entities:
        if not entity.key:
            add_error(
                errors, f"{source_path}: entity {entity.id} must declare at least one key column"
            )

    for measure in config.measures:
        prefix = f"{source_path}: public measure {measure.id}"
        if not str(measure.description or "").strip():
            add_error(errors, f"{prefix} must declare a description")
        if not list(measure.topics or []):
            add_error(errors, f"{prefix} must declare topics")

    measure_metric_ids = {
        f"metric.{(measure.name or measure.id.split('measure.', 1)[-1])}"
        for measure in config.measures
    }
    for recipe in config.metric_recipes:
        if recipe.id in measure_metric_ids and recipe.kind in {"aggregate", "semi_additive"}:
            continue
        prefix = f"{source_path}: curated metric {recipe.id}"
        if not str(recipe.description or "").strip():
            add_error(errors, f"{prefix} must declare a description")
        if not list(recipe.topics or []):
            add_error(errors, f"{prefix} must declare topics")

    # Enum check on compiled dimensions: catches monolithic packages and
    # any path that bypasses the raw-YAML split-package walk above.
    # `_map_dimension_kind` falls back to "string" for unknown values, so
    # without this check `kind: catagorical` would silently be stored as
    # `semantic_kind="catagorical"`, `data_type="string"`. `id` is the
    # auto-key kind and not user-authored under v1.
    valid_dim_kinds = _VALID_COMPILED_DIMENSION_KINDS | {"id"}
    entity_kind_by_id = {row.id: row.kind for row in config.entities}
    for dimension in config.dimensions:
        semantic_kind = str(dimension.semantic_kind or "").strip().lower()
        if not semantic_kind:
            continue
        if semantic_kind not in valid_dim_kinds:
            add_error(
                errors,
                f"{source_path}: dimension {dimension.id} has unknown kind "
                f"{dimension.semantic_kind!r}. Valid kinds: "
                f"{', '.join(sorted(_VALID_COMPILED_DIMENSION_KINDS))}.",
            )
            continue
        # Date/timestamp dimensions are reserved for calendar entities
        # (kind: time). On any other entity they're an authoring error:
        # user-facing models should declare time columns under `times:`.
        # The auto-generated paired DimensionConfig from a `times:` block
        # is skipped via the temporal-role lookup.
        if semantic_kind in {"date", "timestamp", "datetime", "time"}:
            owner_kind = entity_kind_by_id.get(dimension.entity, "")
            is_temporal_paired = any(
                role.dimension == dimension.id for role in config.temporal_roles
            )
            if owner_kind != "time" and not is_temporal_paired:
                add_error(
                    errors,
                    f"{source_path}: dimension {dimension.id} has temporal kind "
                    f"{dimension.semantic_kind!r} on a non-time entity. "
                    f"Declare date/timestamp columns under `times:` (not "
                    f"`dimensions:`) so they get a temporal role with grain "
                    f"support and timezone handling.",
                )

    _check_default_query_axis_collisions(config, source_path, errors)
    _check_disallowed_names(config, source_path, errors)
    if getattr(config.package, "schema_strict", False):
        _check_strict_authoring(config, source_path, errors)

    return [*errors, *_reference_errors(config, source_path)]


def _check_strict_authoring(config, source_path: Path, errors: list[str]) -> None:
    """Compiled-side strict-mode checks.

    Each rejection includes a clear migration pointer. Gated behind
    package.schema_strict: true. Raw-YAML checks (e.g., authored `id:`
    on objects, the top-level `relations:` block) live in
    _check_strict_raw_yaml; the compiled-side checks here run
    against the loaded PackageConfig and catch shape concerns the loader
    already normalized away.
    """
    # 1. accumulation kind enum
    allowed_accumulation = {"", "flow", "stock", "event", "population"}
    for measure in config.measures:
        kind = str(getattr(measure.accumulation, "kind", "") or "").strip().lower()
        if kind not in allowed_accumulation:
            add_error(
                errors,
                f"{source_path}: measure {measure.id} has accumulation.kind {kind!r} "
                f"which is not in the strict enum {{flow, stock, event, population}}. "
                f"Use one of those values or remove the accumulation block.",
            )

    # 2. policy.kind: plan_constraint is a runtime no-op; reject in strict mode.
    for policy in config.semantic_policies:
        if str(policy.kind or "").strip().lower() == "plan_constraint":
            add_error(
                errors,
                f"{source_path}: policy {policy.id} has kind 'plan_constraint' "
                f"which is not a runtime-recognized policy kind. Drop the policy "
                f"or use one of: package_release, object_visibility, object_access, "
                f"protected_object, metric_constraint.",
            )


def _check_default_query_axis_collisions(config, source_path: Path, errors: list[str]) -> None:
    """Reject if more than one temporal_role per entity sets default_query_time_axis=True.

    A model can have multiple temporal columns (ordered_at, fulfilled_at, ...)
    but at most one should be the implicit default time axis for queries.
    """
    dim_to_entity = {dim.id: dim.entity for dim in config.dimensions}
    by_entity: dict[str, list[str]] = {}
    for role in config.temporal_roles:
        if not role.default_query_time_axis:
            continue
        entity_id = dim_to_entity.get(role.dimension, "")
        if not entity_id:
            continue
        by_entity.setdefault(entity_id, []).append(role.id)
    for entity_id, role_ids in by_entity.items():
        if len(role_ids) > 1:
            add_error(
                errors,
                f"{source_path}: entity {entity_id} has multiple temporal_roles with "
                f"default_query_axis=true: {sorted(role_ids)}. At most one default time "
                f"axis is allowed per model/entity.",
            )


def _check_disallowed_names(config, source_path: Path, errors: list[str]) -> None:
    """Reject dimensions/measures whose name or column matches an entity's
    `disallowed_names:` list. The escape hatch is to use `expr:` to derive
    the column under a non-disallowed name.
    """
    disallowed_by_entity: dict[str, set[str]] = {}
    for entity in config.entities:
        names = {
            str(name).strip().lower()
            for name in (entity.disallowed_names or [])
            if str(name).strip()
        }
        if names:
            disallowed_by_entity[entity.id] = names

    if not disallowed_by_entity:
        return

    def _check(entity_id: str, name: str, column: str, kind: str, obj_id: str) -> None:
        banned = disallowed_by_entity.get(entity_id)
        if not banned:
            return
        offenders: list[str] = []
        if str(name or "").strip().lower() in banned:
            offenders.append(f"name {name!r}")
        if str(column or "").strip().lower() in banned:
            offenders.append(f"column {column!r}")
        if offenders:
            add_error(
                errors,
                f"{source_path}: {kind} {obj_id} on entity {entity_id} uses disallowed "
                f"{', '.join(offenders)} (entity disallows {sorted(banned)}). "
                f"Rename or use `expr:` to alias the underlying column.",
            )

    for dim in config.dimensions:
        _check(dim.entity, dim.name, dim.column, "dimension", dim.id)
    for measure in config.measures:
        # A measure's "column" surface is its expr when the expression is a bare
        # column reference; otherwise only the name is checked.
        column = ""
        expr = getattr(measure, "expr", None)
        if expr is not None and type(expr).__name__ == "ColumnRefExpr":
            column = str(getattr(expr, "column", "") or "")
        _check(measure.entity, measure.name, column, "measure", measure.id)


def _reference_errors(config, source_path: Path) -> list[str]:
    """References that don't resolve, or can't be served as written: segments and metrics."""
    return [
        *_segment_reference_errors(config, source_path),
        *_metric_reference_errors(config, source_path),
        *_metric_time_role_errors(config, source_path),
        *_conversion_window_errors(config, source_path),
    ]


def _metric_reference_errors(config, source_path: Path) -> list[str]:
    """Metrics that name a measure or metric the package doesn't define.

    The loader keeps a reference it can't resolve as written, so such a package
    parsed and every query of the metric failed with ``Unknown measure``.
    """
    known = {
        "measure": {row.id for row in config.measures},
        "metric": {row.id for row in config.metric_recipes},
    }
    errors: dict[str, None] = {}  # A reference repeated in one metric is reported once.

    def visit(node: Any, metric_id: str) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child, metric_id)
        if not isinstance(node, dict):
            return
        for key, child in node.items():
            if _opaque_expression_data(node, key):
                continue
            # An empty reference names nothing; a scoped predicate then reads its `input`.
            if (
                key in known
                and isinstance(child, str)
                and child.strip()
                and child not in known[key]
            ):
                hints = [row for row in object_id_suggestions(config, child) if row in known[key]]
                message = f"{source_path}: metric {metric_id} references unknown {key} {child!r}"
                errors[message + (f"; did you mean {hints[0]!r}?" if hints else "")] = None
            else:
                visit(child, metric_id)

    for recipe in config.metric_recipes:
        visit(expr_to_dict(recipe.expression), recipe.id)
    return list(errors)


def _metric_time_role_errors(config, source_path: Path) -> list[str]:
    """Metrics whose temporal_role isn't the clock of any of their measures.

    Such a metric still answers: every leaf falls back to its own clock
    (``REWRITE_APPLIED``, ``metric_time_alignment``), but the result is labeled with
    the declared role. A metric that mixes clocks is fine as long as one of its
    measures has the declared one; the planner aligns a measure with a single clock
    on purpose, but refuses one with several clocks, none of them the declared one. A
    conversion is timed by its base operand, as at query time.
    """
    errors: list[str] = []
    # An unknown role is a different mistake: the probes reject it (INVALID_TEMPORAL_ROLE).
    known = {row.id for row in config.temporal_roles}
    query = NormalizedQuery(version=1, select=[])
    for recipe in config.metric_recipes:
        role = str(recipe.temporal_role or "")
        if role not in known:
            continue
        expression = recipe.expression
        if isinstance(expression, ConversionExpr):
            expression = expression.base
        try:
            leaves = _expr_leaf_temporal_role_sets(expression, config, query)
        except SemanticLayerError as exc:
            if exc.details.get("metric_recipe_cycle"):
                add_error(errors, f"{source_path}: metric {recipe.id}: {exc}")
            continue  # Other unresolved expressions fail the compile probes.
        clocks = set().union(*leaves)
        if clocks and role not in clocks:
            add_error(
                errors,
                f"{source_path}: metric {recipe.id} has temporal_role {role!r}, but its "
                f"measures are timed by {', '.join(sorted(clocks))}: queries bucket them by that "
                f"clock and label the result {role!r}. Set temporal_role to one of those.",
            )
            continue
        conversions: list[ConversionExpr] = []
        _collect_conversion_exprs(recipe.expression, config, conversions)
        if conversions:
            continue  # Conversion operands keep their own clock rules.
        for leaf in dict.fromkeys(frozenset(leaf) for leaf in leaves):
            if len(leaf) > 1 and role not in leaf:
                add_error(
                    errors,
                    f"{source_path}: metric {recipe.id} has temporal_role {role!r}, but one of "
                    f"its measures is timed by {', '.join(sorted(leaf))} instead, so queries on "
                    f"{role!r} are refused. Give that measure the clock {role!r}.",
                )
    return errors


def _conversion_window_errors(config, source_path: Path) -> list[str]:
    """Conversion metrics whose window can never apply, which queries refuse.

    Other operand problems depend on the query and are left to the compile probes.
    """
    errors: list[str] = []
    query = NormalizedQuery(version=1, select=[])
    for recipe in config.metric_recipes:
        expression = recipe.expression
        if not isinstance(expression, ConversionExpr):
            continue
        try:
            for operand in (expression.base, expression.converted):
                _expr_leaf_temporal_role_sets(operand, config, query)  # A recipe cycle raises.
            _conversion_sources(expression, config, query)
        except SemanticLayerError as exc:
            if exc.details.get("conversion_single_event"):
                add_error(errors, f"{source_path}: metric {recipe.id}: {exc}")
    return errors


def _segment_reference_errors(config, source_path: Path) -> list[str]:
    """Reject segments that the catalog and segment surfaces cannot serve.

    The loader keeps an unresolved segment ``entity`` as written, so a typo
    such as ``entity.jaffle.customer`` (for ``entity.jaffle_customer``) still
    loads. Catalog, inspect and segment-validate/explain/preview then fail on
    every request. Each segment must name known entities, pass the
    ``normalize_segment`` check that catalog runs, and compile the query that
    ``segment-validate`` derives from it.
    """
    errors: list[str] = []
    if not config.segments:
        return errors
    from difflib import get_close_matches

    entity_ids = {entity.id for entity in config.entities}
    # Ways an author may spell an entity, each mapped to the id to suggest,
    # weakest first so a stronger spelling wins a collision. A label that
    # several entities share is left out.
    label_counts = Counter(entity.label.lower() for entity in config.entities)
    candidates = [
        *((e.label, e.id) for e in config.entities if label_counts[e.label.lower()] == 1),
        *((e.name, e.id) for e in config.entities),
        *((e.id.removeprefix("entity."), e.id) for e in config.entities),
        *((e.id, e.id) for e in config.entities),
    ]
    spellings = {spelling.lower(): entity_id for spelling, entity_id in candidates if spelling}

    def _unknown_entity(ref: str) -> str:
        hints = get_close_matches(ref.lower(), sorted(spellings), n=1, cutoff=0.6)
        return f"unknown entity {ref!r}" + (
            f"; did you mean {spellings[hints[0]]!r}?" if hints else ""
        )

    registry = Registry(config)
    for segment in config.segments:
        prefix = f"{source_path}: segment {segment.id}"
        # The loader maps the segment's own entity key or name to an id;
        # membership predicates are compiled as written, so they need ids.
        unknown: list[str] = []
        if not segment.entity:
            unknown.append("must declare an entity")
        elif segment.entity not in entity_ids:
            unknown.append(f"targets {_unknown_entity(segment.entity)}")
        for ref in dict.fromkeys(_metric_predicate_entities(segment.metric_filters)):
            if ref not in entity_ids:
                unknown.append(f"membership references {_unknown_entity(ref)}")
        for message in unknown:
            add_error(errors, f"{prefix} {message}")
        if unknown:
            continue
        # Report, never raise: the expression parser still raises plain
        # ValueError/TypeError for some malformed values (a non-numeric window).
        try:
            normalized = normalize_segment(config, segment.id)
            query = build_segment_query(normalized, include_preview_dimensions=True)
        except Exception as exc:
            failure = _describe_segment_failure(exc, with_details=True)
            add_error(errors, f"{prefix} is invalid {failure}")
            continue
        try:
            compile_query(config, registry, query)
        except Exception as exc:
            add_error(errors, f"{prefix} query does not compile {_describe_segment_failure(exc)}")
    return errors


def _describe_segment_failure(exc: Exception, *, with_details: bool = False) -> str:
    """``(CODE): message``, optionally with the scalar details the message doesn't name.

    ``normalize_segment``'s messages omit the offending object and its details
    carry it (for example the preview dimension and the entity it belongs to);
    compiler messages name it, and their details are guidance for API callers.
    """
    if not isinstance(exc, SemanticLayerError):
        return f"({type(exc).__name__}): {exc}"
    message = str(exc)
    details = ", ".join(
        f"{key}={value}"
        for key, value in sorted(exc.details.items())
        if with_details
        and key != "segment_id"
        and isinstance(value, str | int | float | bool)
        and str(value) not in message
    )
    return f"({exc.code}): {message}" + (f" [{details}]" if details else "")


def _metric_predicate_entities(metric_filters: list[Any]) -> list[str]:
    """Entity refs of the ``metric_predicate`` nodes in segment membership filters.

    Each filter is parsed as the compiler parses it, so literal payloads stay
    data; a filter that doesn't parse is left for the compile check to report.
    """
    refs: list[str] = []
    for item in metric_filters:
        try:
            expression = parse_semantic_expression(item.get("expression") or {}, context="query")
        except Exception:
            continue
        refs.extend(
            node.entity
            for node in _expression_nodes(expression)
            if isinstance(node, MetricPredicateExpr) and node.entity
        )
    return refs


def _expression_nodes(node: Any) -> Iterator[Any]:
    """Every node of a parsed expression tree, depth first."""
    if is_dataclass(node) and not isinstance(node, type):
        yield node
        for node_field in fields(node):
            yield from _expression_nodes(getattr(node, node_field.name))
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _expression_nodes(item)


def _compiled_package_warnings(config, source_path: Path) -> list[str | dict[str, Any]]:
    warnings: list[str | dict[str, Any]] = []
    if not list(config.package.environments or []):
        warnings.append(f"{source_path}: package does not declare package.environments")
    # (Pseudo-entity smell warning removed — fact models with `kind: fact`
    # are now the supported way to declare time-keyed rollup tables.)
    first_by_definition: dict[str, str] = {}
    for measure in config.measures:
        meta = dict(getattr(measure, "meta", {}) or {})
        prefix = f"{source_path}: public measure {measure.id}"
        # Same rows and value on the same default clock; other clocks may differ.
        definition = json.dumps(
            [
                measure.entity,
                measure.source_relation,
                measure.measure_class,
                measure.default_aggregation,
                measure.default_temporal_role or [*measure.compatible_temporal_roles, ""][0],
                expr_to_dict(measure.expr),
            ],
            sort_keys=True,
            default=str,
        )
        twin = first_by_definition.setdefault(definition, measure.id)
        if twin != measure.id:
            warnings.append(
                f"{prefix} duplicates {twin} (same entity, expression, aggregation and default "
                "clock). Keep one, give it every clock the other has, and point anything that "
                "reads the other at it."
            )
        for warning in list(getattr(measure, "authoring_warnings", []) or []):
            warnings.append(f"{prefix}: {warning}")
        if not str(meta.get("owner_team", "") or "").strip():
            warnings.append(f"{prefix} should declare meta.owner_team")
        if not str(meta.get("review_priority", "") or "").strip():
            warnings.append(f"{prefix} should declare meta.review_priority")
        if not str(meta.get("change_risk", "") or "").strip():
            warnings.append(f"{prefix} should declare meta.change_risk")
        if not str(getattr(measure, "default_temporal_role", "") or "").strip() and list(
            measure.compatible_temporal_roles or []
        ):
            warnings.append(f"{prefix} should declare default_temporal_role explicitly")
    measure_metric_ids = {
        f"metric.{(measure.name or measure.id.split('measure.', 1)[-1])}"
        for measure in config.measures
    }
    for recipe in config.metric_recipes:
        if recipe.id in measure_metric_ids and recipe.kind in {"aggregate", "semi_additive"}:
            continue
        meta = dict(getattr(recipe, "meta", {}) or {})
        prefix = f"{source_path}: curated metric {recipe.id}"
        if not str(meta.get("owner_team", "") or "").strip():
            warnings.append(f"{prefix} should declare meta.owner_team")
        if not str(meta.get("review_priority", "") or "").strip():
            warnings.append(f"{prefix} should declare meta.review_priority")
        if not str(meta.get("change_risk", "") or "").strip():
            warnings.append(f"{prefix} should declare meta.change_risk")
    contract = getattr(config, "meta_contract", {}) or {}
    if contract:
        for measure in config.measures:
            warnings.extend(
                validate_meta_payload(
                    contract,
                    "measure",
                    dict(getattr(measure, "meta", {}) or {}),
                    path=f"{source_path}: measure {measure.id}",
                )
            )
        for recipe in config.metric_recipes:
            warnings.extend(
                validate_meta_payload(
                    contract,
                    "metric",
                    dict(getattr(recipe, "meta", {}) or {}),
                    path=f"{source_path}: metric {recipe.id}",
                )
            )
        for relation in getattr(config, "relations", []) or []:
            warnings.extend(
                validate_meta_payload(
                    contract,
                    "relation",
                    dict(getattr(relation, "meta", {}) or {}),
                    path=f"{source_path}: relation {relation.id}",
                )
            )
    warnings.extend(_semantic_collision_warnings(config, source_path))
    return warnings


def _semantic_collision_warnings(config, source_path: Path) -> list[dict[str, Any]]:
    """Compatibility wrapper for the dedicated collision detector."""
    return semantic_collision_warnings(config, source_path)


def _error_payload(
    code: str, message: str, *, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"code": code, "message": message, "details": dict(details or {})}


def _package_payload(ref: PackageReference, config=None) -> dict[str, Any]:
    package_id = (
        config.package.package_id
        if config is not None
        else ref.package_id or _candidate_package_id(Path(ref.source_path))
    )
    return {"id": package_id, "source_path": ref.source_path}


def parse_config_report(
    ref: PackageReference, *, progress: ProgressFn | None = None
) -> tuple[dict[str, Any], Any | None]:
    report, snapshot = parse_snapshot_report(ref, progress=progress)
    return report, snapshot.config if snapshot else None


def parse_snapshot_report(
    ref: PackageReference, *, progress: ProgressFn | None = None
) -> tuple[dict[str, Any], LoadedPackageSnapshot | None]:
    if progress is not None:
        progress(f"Parsing package: {ref.display_name}")
    snapshot = None
    try:
        source = capture_package_source(ref.source_path)
        messages = validate_runtime_package(Path(ref.source_path))
        if not messages:
            snapshot = load_package_snapshot(ref.source_path)
            if snapshot.source_fingerprint != source.fingerprint:
                messages.append(
                    "Package sources changed during validation; retry after writes complete."
                )
                snapshot = None
    except SemanticLayerError as exc:
        messages = [str(exc)]
    warnings: list[dict[str, Any]] = []
    errors = [_error_payload("INVALID_CONFIG", message) for message in messages]
    config = None
    if not errors:
        assert snapshot is not None
        config = snapshot.config
        for warning in _compiled_package_warnings(config, Path(ref.source_path)):
            if isinstance(warning, dict):
                warnings.append(dict(warning))
            else:
                warnings.append(_error_payload("AUTHORING_PROFILE_WARNING", warning))
    report = {
        "ok": not errors,
        "package": _package_payload(ref, config),
        "summary": {
            "entities": len(config.entities) if config is not None else 0,
            "temporal_roles": len(config.temporal_roles) if config is not None else 0,
            "measures": len(config.measures) if config is not None else 0,
            "metric_recipes": len(config.metric_recipes) if config is not None else 0,
            "warnings": len(warnings),
            "errors": len(errors),
        },
        "warnings": warnings,
        "errors": errors,
    }
    if snapshot is not None:
        report["package_hash"] = snapshot.source_fingerprint
        report["semantic_fingerprint"] = snapshot.semantic_fingerprint
    return report, snapshot


def _probe_query_for_measure(measure) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [
            {
                "expression": {"measure": measure.id, "aggregation": measure.default_aggregation},
                "as": "probe_value",
            }
        ],
        "limit": 0,
    }


def _metric_min_window_unit(expression: Any) -> str:
    """Return the coarsest required window unit ('month', 'quarter', 'year')
    found in a metric expression tree, or '' if none. Used by the probe
    to pick a probe grain that matches `prior_period`/`rolling`/etc.
    window units; without this, a `prior_period(month)` metric probed at
    day grain raises REWRITE_NOT_SUPPORTED."""
    if expression is None:
        return ""
    if isinstance(expression, dict):
        units: list[str] = []
        for key in ("offset", "window"):
            spec = expression.get(key)
            if isinstance(spec, dict):
                unit = str(spec.get("unit", "")).strip().lower()
                if unit:
                    units.append(unit)
        for child_key in (
            "input",
            "left",
            "right",
            "numerator",
            "denominator",
            "base",
            "converted",
        ):
            units.append(_metric_min_window_unit(expression.get(child_key)))
        for arg in expression.get("args", []) or []:
            units.append(_metric_min_window_unit(arg))
        order = ["year", "quarter", "month", "week", "day"]
        coarsest = ""
        for unit in units:
            if unit in order and (not coarsest or order.index(unit) < order.index(coarsest)):
                coarsest = unit
        return coarsest
    # carry `.unit` directly; ``RollingExpr`` also carries `.unit` and
    # `.value` directly (no `.window` attribute exists on the
    # dataclass — see ``semantic_rails/expressions.py:RollingExpr``).
    # The ``window`` branch below only fires for IR payloads that
    # still carry a raw dict (``offset`` and ``window`` map keys on
    # parsed but un-typed expressions).
    units = []
    direct_unit = getattr(expression, "unit", "")
    if isinstance(direct_unit, str) and direct_unit:
        units.append(direct_unit.lower())
    for attr in ("offset", "window"):
        if hasattr(expression, attr):
            spec = getattr(expression, attr)
            if isinstance(spec, dict):
                unit = str(spec.get("unit", "")).strip().lower()
                if unit:
                    units.append(unit)
    for attr in ("input", "left", "right", "numerator", "denominator", "base", "converted"):
        if hasattr(expression, attr):
            units.append(_metric_min_window_unit(getattr(expression, attr)))
    if hasattr(expression, "args"):
        for arg in expression.args or []:
            units.append(_metric_min_window_unit(arg))
    order = ["year", "quarter", "month", "week", "day"]
    coarsest = ""
    for unit in units:
        if unit in order and (not coarsest or order.index(unit) < order.index(coarsest)):
            coarsest = unit
    return coarsest


def _default_time_spec_for_metric(recipe, runtime: Runtime) -> dict[str, Any]:
    role_id = str(recipe.temporal_role or "").strip()
    if not role_id:
        compatible_roles = list(recipe.compatible_temporal_roles or [])
        default_roles = [
            role.id
            for role in runtime._config.temporal_roles
            if role.default_query_time_axis and role.id in compatible_roles
        ]
        if default_roles:
            role_id = default_roles[0]
        elif compatible_roles:
            role_id = compatible_roles[0]
    if not role_id:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Metric recipe '{recipe.id}' requires query.time but has no compatible temporal role",
            details={"metric": recipe.id},
        )
    coarsest_unit = _metric_min_window_unit(recipe.expression)
    grain = coarsest_unit if coarsest_unit in {"month", "quarter", "year"} else "day"
    return {"temporal_role": role_id, "grain": grain}


def _probe_query_for_metric(recipe, runtime: Runtime) -> dict[str, Any]:
    query = {
        "version": 1,
        "select": [{"expression": {"metric": recipe.id}, "as": "probe_value"}],
        "limit": 0,
    }
    if _requires_query_time(recipe.expression, runtime._config):
        query["time"] = _default_time_spec_for_metric(recipe, runtime)
    return query


@runtime_request_scope
def _run_probe(
    runtime: Runtime, *, kind: str, object_id: str, query: dict[str, Any]
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        runtime.query(query)
        return {
            "object_id": object_id,
            "kind": kind,
            "ok": True,
            "query": query,
            "timing_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except SemanticLayerError as exc:
        result = {
            "object_id": object_id,
            "kind": kind,
            "ok": False,
            "query": query,
            "timing_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": _error_payload(exc.code, str(exc), details=exc.details),
        }
        if exc.code == "QUERY_EXECUTION_ERROR":
            # Package-owner diagnostics use the normal compile surface on the
            # same runtime generation. Never change process-wide debug-SQL
            # authorization or inject roles while other requests may be running.
            result["rendered_sql"] = runtime.compile(query)["rendered_sql"]
        return result


def _probe_failure(probe: dict[str, Any]) -> dict[str, Any]:
    kind, object_id = probe["kind"], probe["object_id"]
    details = {
        **dict(probe["error"].get("details", {}) or {}),
        "object_id": object_id,
        "kind": kind,
    }
    if kind == "segment":
        details["segment_id"] = object_id
    failure = {"code": probe["error"]["code"], "message": probe["error"]["message"]}
    hints = recovery_hints_for_error(failure["code"], details)
    return {**failure, "details": details, **({"recovery_hints": hints} if hints else {})}


def _run_metric_probe(recipe, runtime: Runtime) -> dict[str, Any]:
    query = _probe_query_for_metric(recipe, runtime)
    result = _run_probe(runtime, kind="metric", object_id=recipe.id, query=query)
    if result["ok"] or "time" in query:
        return result
    error_code = str(result.get("error", {}).get("code", "") or "")
    if error_code not in {"INVALID_QUERY", "INVALID_TEMPORAL_ROLE", "PREDICATE_GRAIN_UNSAFE"}:
        return result
    retry_query = dict(query)
    try:
        retry_query["time"] = _default_time_spec_for_metric(recipe, runtime)
    except SemanticLayerError as exc:
        return {
            "object_id": recipe.id,
            "kind": "metric",
            "ok": False,
            "query": query,
            "timing_ms": result["timing_ms"],
            "error": _error_payload(exc.code, str(exc), details=exc.details),
        }
    return _run_probe(runtime, kind="metric", object_id=recipe.id, query=retry_query)


# A filter with these operators matches rows only when its literal is a value in the data.
_VALUE_FILTER_OPS = frozenset({"=", "!=", "<>", "IN", "NOT IN"})


def _metric_filter_literals(config) -> Iterator[tuple[str, str, str]]:
    """(metric id, dimension id, literal) for each string a metric filter compares a column to."""
    string_dimensions = {row.id for row in config.dimensions if row.data_type == "string"}
    for recipe in config.metric_recipes:
        for node in _expression_nodes(recipe.expression):
            for clause in node.filter.get("all", []) if isinstance(node, AggregateExpr) else []:
                op = " ".join(str(clause.get("op") or "=").split()).upper()
                if "field" not in clause or op not in _VALUE_FILTER_OPS:
                    continue
                try:
                    dimension_id = resolve_filter_dimension(str(clause["field"]), config)
                except SemanticLayerError:
                    continue  # the metric's probe reports the unknown field
                raw = clause.get("value")
                for literal in raw if isinstance(raw, list) else [raw]:
                    if dimension_id in string_dimensions and isinstance(literal, str):
                        yield recipe.id, dimension_id, literal


def _data_values(runtime: Runtime, dimension_id: str) -> list[str]:
    """Every value of a dimension in the data, or [] when they cannot all be read."""
    limit = max_valid_values_limit()
    try:
        # A query makes valid-values read the data even for a dimension with a
        # declared domain: the filter matches data rows, not the domain.
        payload = valid_values_payload(
            runtime,
            dimension_id=dimension_id,
            query={"version": 1},
            limit=limit,
            allow_live_query=True,
        )
    except SemanticLayerError:
        return []
    # A full page, one slot of which a NULL row can take, may hide more values.
    if payload["total_count"] >= limit - 1:
        return []
    return [str(row["value"]) for row in payload["values"]]


def _filter_value_warnings(runtime: Runtime) -> list[dict[str, Any]]:
    """Warn when a metric filter compares a column to a value its data does not hold.

    ``status = 'Completed'`` over data holding ``completed`` runs, and the metric
    silently comes back empty. This is a warning, not an error: sample data can
    lack a value the production data holds.
    """
    data_values: dict[str, list[str]] = {}
    warnings: list[dict[str, Any]] = []
    for metric_id, dimension_id, literal in _metric_filter_literals(runtime._config):
        if dimension_id not in data_values:
            data_values[dimension_id] = _data_values(runtime, dimension_id)
        values = data_values[dimension_id]
        if not values or literal in values:
            continue
        folded = {value.casefold(): value for value in values}
        close = get_close_matches(literal.casefold(), list(folded), n=1)
        suggestion = folded[close[0]] if close else None
        warnings.append(
            {
                "code": "FILTER_VALUE_NOT_FOUND",
                "severity": "warning",
                "message": (
                    f"Metric {metric_id} filters {dimension_id} on {literal!r}, "
                    "which matches no value in the data"
                    + (f"; did you mean {suggestion!r}?" if suggestion else "")
                ),
                "details": {
                    "object_id": metric_id,
                    "dimension": dimension_id,
                    "value": literal,
                    "suggestion": suggestion,
                },
            }
        )
    return warnings


def validate_config_report(
    ref: PackageReference,
    *,
    progress: ProgressFn | None = None,
    parse_report: dict[str, Any] | None = None,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    snapshot = runtime.snapshot if runtime is not None else None
    if parse_report is None:
        parse_report, snapshot = parse_snapshot_report(ref, progress=progress)
    warnings = list(parse_report["warnings"])
    if not parse_report["ok"]:
        return {
            "ok": False,
            "package": dict(parse_report["package"]),
            "parse": parse_report,
            "summary": {
                "measures_total": 0,
                "metric_recipes_total": 0,
                "probes_total": 0,
                "passed": 0,
                "failed": 0,
                "warnings": len(warnings),
                "errors": len(parse_report["errors"]),
            },
            "warnings": warnings,
            "errors": list(parse_report["errors"]),
            "probes": [],
        }

    should_close_runtime = runtime is None
    if runtime is None:
        snapshot = snapshot or load_package_snapshot(ref.source_path)
        if parse_report.get("package_hash") != snapshot.source_fingerprint:
            raise SemanticLayerError(
                "INVALID_CONFIG", "Package sources changed after parsing; retry validation."
            )
        runtime = Runtime.from_snapshot(snapshot, package_id=ref.package_id)
    try:
        probes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        total_measures = len(runtime._config.measures)
        total_metrics = len(runtime._config.metric_recipes)

        for index, measure in enumerate(runtime._config.measures, start=1):
            if progress is not None:
                progress(f"Validating measure {index}/{total_measures}: {measure.id}")
            probe = _run_probe(
                runtime,
                kind="measure",
                object_id=measure.id,
                query=_probe_query_for_measure(measure),
            )
            probes.append(probe)
            if not probe["ok"]:
                failures.append(_probe_failure(probe))
                if progress is not None:
                    progress(f"WARNING measure failed: {measure.id} ({probe['error']['code']})")

        for index, recipe in enumerate(runtime._config.metric_recipes, start=1):
            if progress is not None:
                progress(f"Validating metric {index}/{total_metrics}: {recipe.id}")
            probe = _run_metric_probe(recipe, runtime)
            probes.append(probe)
            if not probe["ok"]:
                failures.append(_probe_failure(probe))
                if progress is not None:
                    progress(f"WARNING metric failed: {recipe.id} ({probe['error']['code']})")

        for segment in runtime._config.segments:
            # Preview's query: a membership value its column can't hold fails only in the warehouse.
            query = build_segment_query(
                normalize_segment(runtime._config, segment.id),
                include_preview_dimensions=True,
                limit=1,
            )
            probe = _run_probe(runtime, kind="segment", object_id=segment.id, query=query)
            probes.append(probe)
            if not probe["ok"]:
                failures.append(_probe_failure(probe))

        warnings.extend(_filter_value_warnings(runtime))
        passed = sum(1 for probe in probes if probe["ok"])
        failed = len(probes) - passed
        warnings.extend(runtime._seed_warnings)
        return {
            "ok": failed == 0,
            "package": _package_payload(ref, runtime._config),
            "parse": parse_report,
            "summary": {
                "measures_total": total_measures,
                "metric_recipes_total": total_metrics,
                "probes_total": len(probes),
                "passed": passed,
                "failed": failed,
                "warnings": len(warnings),
                "errors": len(failures),
            },
            "warnings": warnings,
            "errors": failures,
            "probes": probes,
        }
    finally:
        if should_close_runtime:
            runtime.close()
