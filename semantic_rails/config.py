"""Package discovery and YAML→PackageConfig loader.

Exposes :func:`load_package_config`, :func:`list_package_ids`,
:func:`get_package_path`, and ``repo_root`` helpers. Reads each
package's ``package.yml`` / ``graph.yml`` / ``models/*.yml`` set,
applies operational-contract overlays, parses expressions, and returns
a typed :class:`semantic_rails.schema.PackageConfig` from one captured
:class:`LoadedPackageSnapshot`. Only package-discovery paths are cached.
"""

from __future__ import annotations

import os
import re as re
import sysconfig
from dataclasses import replace as replace
from datetime import date as date
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config_parts.aggregate_rows import _ROLLUP_HOLDS as _ROLLUP_HOLDS
from .config_parts.aggregate_rows import _merge_variant_spec as _merge_variant_spec
from .config_parts.aggregate_rows import _resolve_variant_specs as _resolve_variant_specs
from .config_parts.loader_chain import _EXTERNAL_PATHS_ENV as _EXTERNAL_PATHS_ENV
from .config_parts.loader_chain import SEED_KIND_EXTERNAL as SEED_KIND_EXTERNAL
from .config_parts.loader_chain import _ensure_unique_object_ids as _ensure_unique_object_ids
from .config_parts.loader_chain import _parse_connection_spec as _parse_connection_spec
from .config_parts.loader_chain import _parse_package_meta as _parse_package_meta
from .config_parts.loader_chain import (
    ensure_contained_package_path as ensure_contained_package_path,
)
from .config_parts.loader_chain import (
    external_package_paths_allowed as external_package_paths_allowed,
)
from .config_parts.lookup_measures import lookup_measure_spec as lookup_measure_spec
from .config_parts.lookup_measures import resolve_lookup_measures as resolve_lookup_measures
from .config_parts.measure_governance import with_published_flags as with_published_flags
from .config_parts.measure_rows import _authored_additive as _authored_additive
from .config_parts.measure_rows import _default_topics as _default_topics
from .config_parts.measure_rows import _derive_measure_semantics as _derive_measure_semantics
from .config_parts.measure_rows import _normalize_accumulation as _normalize_accumulation
from .config_parts.measure_rows import _suggested_aggregations as _suggested_aggregations
from .config_parts.metric_rows import _DIRECT_EXPRESSION_FIELDS as _DIRECT_EXPRESSION_FIELDS
from .config_parts.metric_rows import _MEASURE_REF_KINDS as _MEASURE_REF_KINDS
from .config_parts.metric_rows import _PARTITIONED_KINDS as _PARTITIONED_KINDS
from .config_parts.metric_rows import _WINDOWED_DIRECT_KINDS as _WINDOWED_DIRECT_KINDS
from .config_parts.metric_rows import _authored_direct_fields as _authored_direct_fields
from .config_parts.metric_rows import _input_measure as _input_measure
from .config_parts.metric_rows import _parse_metric_expression as _parse_metric_expression
from .config_parts.metric_rows import _resolve_partition_by as _resolve_partition_by
from .config_parts.metric_rows import _resolve_refs_in_ast as _resolve_refs_in_ast
from .config_parts.metric_rows import (
    _translate_direct_fields_to_expression as _translate_direct_fields_to_expression,
)
from .config_parts.metric_rows import (
    _translate_metric_direct_fields as _translate_metric_direct_fields,
)
from .config_parts.model_rows import _key_columns_and_role as _key_columns_and_role
from .config_parts.model_rows import _map_dimension_kind as _map_dimension_kind
from .config_parts.model_rows import _normalize_relation_step as _normalize_relation_step
from .config_parts.model_rows import _parse_relations as _parse_relations
from .config_parts.model_rows import _relation_id_for_key as _relation_id_for_key
from .config_parts.model_rows import _relation_sql_name as _relation_sql_name
from .config_parts.model_rows import _with_displays as _with_displays
from .config_parts.package_loader import normalize_package
from .config_parts.package_rows import _parse_package as _parse_package
from .config_parts.policy_rows import _ALLOWED_CAVEAT_KINDS as _ALLOWED_CAVEAT_KINDS
from .config_parts.policy_rows import _ALLOWED_CAVEAT_SEVERITIES as _ALLOWED_CAVEAT_SEVERITIES
from .config_parts.policy_rows import _package_object_ids as _package_object_ids
from .config_parts.policy_rows import _parse_caveat_time as _parse_caveat_time
from .config_parts.policy_rows import _parse_caveats as _parse_caveats
from .config_parts.policy_rows import _parse_path_policy as _parse_path_policy
from .config_parts.policy_rows import _parse_path_preferences as _parse_path_preferences
from .config_parts.policy_rows import _require_iso_date as _require_iso_date
from .config_parts.policy_rows import _validate_caveat_refs as _validate_caveat_refs
from .config_parts.policy_rows import _validate_policy_scopes as _validate_policy_scopes
from .config_parts.route_rows import RouteRowError as RouteRowError
from .config_parts.route_rows import check_route_row as check_route_row
from .config_parts.route_rows import entity_references as entity_references
from .config_parts.route_rows import require_rows_agree as require_rows_agree
from .config_parts.row_values import _ensure_dict_list as _ensure_dict_list
from .config_parts.row_values import _ensure_list as _ensure_list
from .config_parts.row_values import _merge_meta as _merge_meta
from .config_parts.row_values import _normalize_examples as _normalize_examples
from .config_parts.row_values import _normalize_meta as _normalize_meta
from .config_parts.row_values import _optional_int as _optional_int
from .config_parts.row_values import _slug as _slug
from .config_parts.row_values import _titleize as _titleize
from .config_parts.row_values import _unique_aliases as _unique_aliases
from .config_parts.temporal_rows import _TIME_GRAIN_ORDER as _TIME_GRAIN_ORDER
from .config_parts.temporal_rows import _coarser_time_grains as _coarser_time_grains
from .config_parts.temporal_rows import (
    _parse_external_discontinuities as _parse_external_discontinuities,
)
from .config_parts.temporal_rows import _parse_validity_windows as _parse_validity_windows
from .dialects import (
    connection_option_errors,
    snowflake_native_direct_connect_errors,
    supported_warehouses,
    warehouse_connector,
)
from .dialects import dialect_for_warehouse as dialect_for_warehouse
from .dialects import snowflake_adbc_connect_errors as snowflake_adbc_connect_errors
from .errors import SemanticLayerError
from .expressions import (
    parse_config_expression,
    parse_semantic_expression,
)
from .expressions import validate_expression_calls as validate_expression_calls
from .meta_contract import load_meta_contract
from .operational import (
    load_operational_contract,
    merge_operational_payloads,
    normalize_operational_payload,
    validate_operational_payload,
)
from .package_snapshot import (
    PACKAGE_SOURCE_DIRS,
    CapturedSource,
    LoadedPackageSnapshot,
    links_package_input,
    load_package_snapshot,
)
from .policy_rules import policy_action as policy_action
from .row_filters import validate_row_filters as validate_row_filters
from .schema import DEFAULT_PATH_HOP_LIMIT as DEFAULT_PATH_HOP_LIMIT
from .schema import MAX_PATH_HOP_LIMIT as MAX_PATH_HOP_LIMIT
from .schema import OBSERVATION_SCOPES as OBSERVATION_SCOPES
from .schema import (
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
    PathPreferenceConfig,
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
from .schema import PathPolicyConfig as PathPolicyConfig
from .schema import PlannerConfig as PlannerConfig
from .schema import require_boolean_mnpi as require_boolean_mnpi
from .temporal_support import _date_key as _date_key
from .yaml_loader import load_yaml_file, safe_load

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
    "load_package_snapshot",
    "LoadedPackageSnapshot",
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
    "semantic_rails_home",
    "snowflake_native_direct_connect_errors",
    "supported_warehouses",
    "validate_operational_payload",
    "warehouse_connector",
]


SEMANTIC_RAILS_HOME_ENV = "SEMANTIC_RAILS_HOME"
LOCAL_CONFIG_DIRNAME = ".semantic_rails"


def semantic_rails_home() -> Path:
    """The user's Semantic Rails folder: profiles, MCP state and generated caches."""
    configured = str(os.environ.get(SEMANTIC_RAILS_HOME_ENV, "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / LOCAL_CONFIG_DIRNAME


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


# Each root block file of a directory package and the key it is read through.
_BLOCK_FILES = (
    ("defaults.yml", "defaults"),
    ("graph.yml", "graph"),
    ("relations.yml", "relations"),
    ("metrics.yml", "metrics"),
    ("segments.yml", "segments"),
    ("policies.yml", "semantic_policies"),
    ("caveats.yml", "semantic_caveats"),
)
# Every root YAML file and directory of YAML anything reads from a package directory: the
# loader's files and object directories, and the examples and tests the package tools run.
_PACKAGE_ROOT_FILES = frozenset({"package.yml", *(name for name, _ in _BLOCK_FILES)})
_OBJECT_DIRS = ("models", "relations", "metrics", "segments")
_PACKAGE_ROOT_DIRS = PACKAGE_SOURCE_DIRS


def _unread_root_errors(
    path: str, contents: dict[str, bytes] | None, links: tuple[str, ...] = ()
) -> list[str]:
    """One error per root YAML file, and per root directory holding YAML, that nothing reads,
    and per directory symlink that could hide package input, which nothing follows. Root names
    starting with ``_`` or ``.`` are left alone. ``links`` are the captured directory symlinks,
    relative to ``path``."""
    if contents is not None:
        sources = [os.path.relpath(name, path) for name in contents]
    else:
        sources = []
        found: list[str] = []
        for root, dirnames, filenames in os.walk(path):
            if root == path:
                dirnames[:] = [name for name in dirnames if not name.startswith(("_", "."))]
            found.extend(
                os.path.relpath(os.path.join(root, name), path)
                for name in dirnames
                if os.path.islink(os.path.join(root, name))
                and links_package_input(path, os.path.relpath(os.path.join(root, name), path))
            )
            sources.extend(os.path.relpath(os.path.join(root, name), path) for name in filenames)
        links = tuple(found)
    errors = [
        f"{os.path.join(path, link)} is a directory symlink — the loader does not follow it, so "
        "this would silently change behavior; copy or link the files instead"
        for link in sorted(links)
        if not Path(link).parts[0].startswith(("_", "."))
    ]
    unread: set[tuple[str, bool]] = set()
    for source in sources:
        top, *rest = Path(source).parts
        known = _PACKAGE_ROOT_DIRS if rest else _PACKAGE_ROOT_FILES
        if (
            source.endswith((".yml", ".yaml"))
            and not top.startswith(("_", "."))
            and top not in known
        ):
            unread.add((top, bool(rest)))
    for name, is_dir in sorted(unread):
        if is_dir:
            what = f"{os.path.join(path, name)}{os.sep} is not a package directory"
            fix = (
                f"write these {name} in {name}.yml"
                if name in ("policies", "caveats")
                else f"move them under {', '.join(f'{d}/' for d in sorted(_PACKAGE_ROOT_DIRS))}"
                " or start the directory name with '_'"
            )
        else:
            what = f"{os.path.join(path, name)} is not a package file"
            stem = os.path.splitext(name)[0]
            if stem + ".yml" in _PACKAGE_ROOT_FILES:
                fix = f"rename it {stem}.yml"
            elif stem in ("examples", "tests"):
                fix = f"write these entries under {stem}/"
            else:
                fix = (
                    f"move its contents into {', '.join(sorted(_PACKAGE_ROOT_FILES))} or start "
                    "its name with '_'"
                )
        errors.append(
            f"{what} — its YAML is ignored by the loader, so this would silently change "
            f"behavior; {fix}"
        )
    return errors


def _merge_package_dir(path: str, *, captured: CapturedSource | None = None) -> dict[str, Any]:
    contents = captured.contents if captured is not None else None

    def is_file(filename: str) -> bool:
        return filename in contents if contents is not None else os.path.isfile(filename)

    def load(filename: str) -> dict[str, Any]:
        return (
            dict(safe_load(contents[filename]) or {})
            if contents is not None
            else _load_yaml_file(filename)
        )

    package_path = os.path.join(path, "package.yml")
    if not is_file(package_path):
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Package directory '{path}' is missing package.yml"
        )

    # Each file is read through one wrapper key; record every other root key it would drop.
    errors: list[str] = []

    def drops(filename: str, doc: dict[str, Any], wrappers: tuple[str, ...], fix: str) -> None:
        for key in sorted(str(k) for k in doc if str(k) not in wrappers):
            if not key.startswith("_"):
                errors.append(
                    f"{filename} has unknown key {key!r} — unknown keys are ignored by the "
                    f"loader, so this would silently change behavior; {fix}"
                )

    raw = load(package_path)
    if raw.get("relations") not in (None, {}, []):
        errors.append(
            f"{package_path} declares relations:, which a directory package keeps in "
            "relations.yml or relations/; move it there"
        )
    merged: dict[str, Any] = dict(raw)
    merged.setdefault("defaults", {})
    merged.setdefault("graph", {})
    merged.setdefault("models", {})
    merged.setdefault("relations", {})
    merged.setdefault("metrics", {})
    merged.setdefault("segments", {})

    # A later definition replaces an earlier one wholesale, so each is read from one place only.
    origins: dict[str, dict[str, str]] = {}
    for block in _OBJECT_DIRS:
        authored = raw.get(block)
        origins[block] = (
            dict.fromkeys(map(str, authored), package_path) if isinstance(authored, dict) else {}
        )

    def define(block: str, object_id: str, filename: str) -> None:
        if first := origins[block].get(object_id):
            errors.append(
                f"{filename} defines {block[:-1]} {object_id!r}, which {first} also defines — "
                f"the definition in {first} is ignored by the loader, so this would silently "
                "change behavior; keep one definition"
            )
        origins[block][object_id] = filename

    for filename, key in _BLOCK_FILES:
        full = os.path.join(path, filename)
        if is_file(full):
            if raw.get(key):
                errors.append(
                    f"{package_path} declares {key!r}, which {full} replaces — the package.yml "
                    "block is ignored by the loader, so this would silently change behavior; "
                    "keep the block in one of the two files"
                )
            doc = load(full)
            list_keys = {"semantic_policies", "semantic_caveats"}
            if key in doc:
                drops(full, doc, (key,), f"the loader reads only {key!r} from this file")
            elif key not in list_keys:
                drops(full, doc, (), f"write this file's contents under a top-level {key!r} key")
            value = doc.get(key, doc if key in list_keys else {})
            # `semantic_policies:` and `semantic_caveats:` are lists; every other
            # block is a mapping. Don't wrap a list in `dict(...)` — that
            # silently fails with "update sequence element #0 has length 3".
            if key in list_keys:
                merged[key] = list(value or [])
            else:
                merged[key] = dict(value or {})
            if key in origins:
                origins[key] = dict.fromkeys(map(str, merged[key]), full)

    def _yaml_files(root: str) -> list[str]:
        if contents is not None:
            prefix = root + os.sep
            return sorted(
                name
                for name in contents
                if name.startswith(prefix) and name.endswith((".yml", ".yaml"))
            )
        files: list[str] = []
        for current_root, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                if filename.endswith(".yml") or filename.endswith(".yaml"):
                    files.append(os.path.join(current_root, filename))
        return files

    # Each object folder has one file shape: one model per file under `model:`, and a
    # `relations:`, `metrics:` or `segments:` map in every file of the other three.
    for block in _OBJECT_DIRS:
        folder = os.path.join(path, block)
        if captured is None and not os.path.isdir(folder):
            continue
        wrapper = "model" if block == "models" else block
        objects = dict(merged.get(block, {}) or {})
        for file_path in _yaml_files(folder):
            doc = load(file_path)
            if wrapper not in doc:
                shape = "one model under `model:`" if block == "models" else f"a `{block}:` map"
                errors.append(
                    f"{file_path} must hold {shape}, the one file shape in {block}/; rewrite its "
                    f"contents under `{wrapper}:`"
                )
                continue
            drops(file_path, doc, (wrapper,), f"the loader reads only {wrapper!r} from this file")
            stem = os.path.splitext(os.path.basename(file_path))[0]
            entries = (
                {stem: dict(doc["model"] or {})}
                if block == "models"
                else {str(key): dict(spec or {}) for key, spec in dict(doc[block] or {}).items()}
            )
            for key, spec in entries.items():
                if block in {"models", "relations"}:
                    key = spec["id"] = str(spec.get("id") or key)
                define(block, key, file_path)
                objects[key] = spec
        merged[block] = objects

    links = captured.directory_links if captured is not None else ()
    errors.extend(_unread_root_errors(path, contents, links))
    if errors:
        raise SemanticLayerError("INVALID_CONFIG", "\n".join(errors), details={"errors": errors})
    return merged


def _load_package_source(path: str, *, captured: CapturedSource | None = None) -> dict[str, Any]:
    if captured.is_directory if captured is not None else os.path.isdir(path):
        return _merge_package_dir(path, captured=captured)
    if captured is not None:
        return dict(safe_load(captured.contents[path]) or {})
    return _load_yaml_file(path)


def load_package_config(path: str | LoadedPackageSnapshot) -> PackageConfig:
    return load_package_snapshot(path).config


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
