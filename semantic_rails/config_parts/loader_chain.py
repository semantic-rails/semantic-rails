"""Package settings and validation in the strict loader chain."""

from __future__ import annotations

import os
import re
from typing import Any

from ..dialects import (
    connection_option_errors,
    snowflake_adbc_connect_errors,
    snowflake_native_direct_connect_errors,
    supported_warehouses,
    warehouse_connector,
)
from ..errors import SemanticLayerError
from ..schema import (
    OBSERVATION_SCOPES,
    ConnectionSpec,
    PackageConfig,
    PackageMeta,
    PlannerConfig,
    SeedSpec,
)
from .row_values import _ensure_list

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


# A DuckDB database another tool builds (dbt, a loader): the runtime reads it
# as is and never creates or replaces it, so the seed names no source.
SEED_KIND_EXTERNAL = "external"


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


def _parse_package_meta(
    package_raw: dict[str, Any], *, path: str, defaults: dict[str, Any] | None = None
) -> PackageMeta:
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
    if connector.requires_seed and seed.kind == SEED_KIND_EXTERNAL:
        if seed.source or seed.post_sql:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: package.seed.kind 'external' takes no source or post_sql; "
                "another tool builds the database at package.default_db",
            )
    elif connector.requires_seed and (not seed.kind or not seed.source):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: duckdb packages must declare package.seed.kind and package.seed.source "
            "(or package.seed.kind: external when another tool builds the database)",
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
        elif warehouse == "snowflake" and connection.kind == "snowflake_adbc":
            if connection.name:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: snowflake_adbc does not support package.connection.name",
                )
            adbc_errors = snowflake_adbc_connect_errors(connection.options)
            if adbc_errors:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: {warehouse} package.connection has invalid options: {'; '.join(adbc_errors)}",
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

    observation_scope = (defaults or {}).get("observation_scope", OBSERVATION_SCOPES[0])
    if observation_scope not in OBSERVATION_SCOPES:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: defaults.observation_scope must be one of {list(OBSERVATION_SCOPES)}, "
            f"not {observation_scope!r}",
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
        planner=planner_cfg,
        observation_scope=observation_scope,
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
