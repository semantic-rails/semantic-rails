"""Package artifact builder — backs the ``build-package`` CLI command.

Produces a signed tarball (``semantic-rails-manifest.json`` + YAML
files + checksums) suitable for distribution as a versioned semantic
package. Used by host integrations that want immutable, hash-pinned
package deliverables instead of pointing at a working tree. Excludes
build cruft (``.git``, ``__pycache__``, DuckDB files) by default.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .cache import package_fingerprint
from .config import load_package_config, package_root_for_source, repo_root, resolve_repo_path
from .config_validation import PackageReference, parse_config_report, validate_config_report
from .errors import SemanticLayerError
from .expressions import expr_to_dict
from .policies import package_release_labels
from .renderer import _quote_ident
from .runtime import Runtime
from .yaml_loader import safe_load as yaml_safe_load

ARTIFACT_MANIFEST_NAME = "semantic-rails-manifest.json"
ARTIFACT_SCHEMA_VERSION = 1
_ARTIFACT_EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".uv-cache",
    "__pycache__",
    "node_modules",
    "dist",
}
_ARTIFACT_EXCLUDED_SUFFIXES = {".duckdb", ".db", ".sqlite", ".sqlite3", ".pyc"}


def _semantic_rails_version() -> str:
    try:
        from importlib.metadata import version

        return version("semantic-rails")
    except Exception:  # pragma: no cover - local editable checkouts may not be installed as a dist
        return "0.0.0+local"


def _walk_artifact_dir(directory: Path, arc_prefix: str) -> list[tuple[Path, str]]:
    """Recursively collect files under ``directory`` with artifact arcnames.

    Applies the same exclusion rules as directory-layout packages so a
    single-file package never ships build cruft either.
    """
    rows: list[tuple[Path, str]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if any(part in _ARTIFACT_EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in _ARTIFACT_EXCLUDED_SUFFIXES:
            continue
        arcname = f"{arc_prefix}/{relative.as_posix()}" if arc_prefix else relative.as_posix()
        rows.append((path, arcname))
    return rows


def _resolve_companion_asset(root: Path, declared: str) -> tuple[Path | None, str]:
    """Resolve an asset path declared in package.yml (seed.source / post_sql).

    Returns ``(resolved_path, arcname)``. Mirrors the runtime's asset
    resolution: relative paths resolve against the package root first,
    then the repo root. Absolute declarations and paths with ``..``
    segments return ``(None, "")`` — they cannot be re-rooted portably
    inside an artifact, and a ``..`` arcname would let the artifact
    write outside its extraction root.
    """
    text = str(declared or "").strip()
    if not text:
        return None, ""
    declared_path = Path(text)
    if declared_path.is_absolute() or ".." in declared_path.parts:
        return None, ""
    arcname = declared_path.as_posix()
    package_candidate = root / declared_path
    if package_candidate.exists():
        return package_candidate, arcname
    repo_candidate = Path(resolve_repo_path(text))
    if repo_candidate.exists():
        return repo_candidate, arcname
    # Missing — return the package-root candidate so callers can name
    # the path they expected in a structured error.
    return package_candidate, arcname


def _single_file_companion_assets(
    source: Path, config: Any
) -> tuple[list[tuple[Path, str]], list[dict[str, Any]]]:
    """Companion files a single-file package needs to hydrate itself.

    Returns ``(rows, missing)``. ``rows`` are ``(path, arcname)`` pairs
    for the seed SQL / post_sql assets referenced by ``package.seed``
    plus the sibling ``examples/`` and ``tests/`` directories — the same
    roots ``run-examples`` and ``test-package`` read. ``missing`` lists
    declared seed assets that do not exist on disk.
    """
    root = source.parent
    rows: list[tuple[Path, str]] = []
    missing: list[dict[str, Any]] = []
    seed = getattr(getattr(config, "package", None), "seed", None)
    declared_assets: list[tuple[str, str]] = []
    if seed is not None:
        declared_assets.append(("package.seed.source", str(seed.source or "")))
        declared_assets.append(("package.seed.post_sql", str(seed.post_sql or "")))
    for field, declared in declared_assets:
        resolved, arcname = _resolve_companion_asset(root, declared)
        if resolved is None:
            continue
        if not resolved.exists():
            missing.append({"field": field, "declared": declared, "resolved_path": str(resolved)})
            continue
        if resolved.is_dir():
            rows.extend(_walk_artifact_dir(resolved, arcname))
        elif resolved.suffix.lower() not in _ARTIFACT_EXCLUDED_SUFFIXES:
            rows.append((resolved, arcname))
    for sibling in ("examples", "tests"):
        directory = root / sibling
        if directory.is_dir():
            rows.extend(_walk_artifact_dir(directory, sibling))
    return rows, missing


def _artifact_file_rows(source_path: str, *, config: Any | None = None) -> list[tuple[Path, str]]:
    source = Path(source_path)
    if source.is_file():
        if source.suffix.lower() in _ARTIFACT_EXCLUDED_SUFFIXES:
            return []
        config = config or load_package_config(source_path)
        rows: dict[str, Path] = {source.name: source}
        companion_rows, _ = _single_file_companion_assets(source, config)
        for path, arcname in companion_rows:
            rows.setdefault(arcname, path)
        return [(rows[arcname], arcname) for arcname in sorted(rows)]

    root = Path(package_root_for_source(source_path))
    return _walk_artifact_dir(root, "")


def package_manifest(
    ref: PackageReference, *, config: Any | None = None, checks: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = config or load_package_config(ref.source_path)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": "semantic_rails_package",
        "package": {
            "id": config.package.package_id,
            "name": config.package.name,
            "warehouse": config.package.warehouse,
            "environments": list(config.package.environments or []),
            "release_labels": package_release_labels(config),
        },
        "semantic_rails": {
            "distribution": "semantic-rails",
            "python_package": "semantic_rails",
            "cli": "semantic-rails",
            "project": "semantic-rails",
            "version": _semantic_rails_version(),
        },
        "package_hash": package_fingerprint(ref.source_path),
        "source": {
            "layout": "directory" if Path(ref.source_path).is_dir() else "file",
            "files": [
                arcname for _, arcname in _artifact_file_rows(ref.source_path, config=config)
            ],
        },
        "summary": {
            "entities": len(config.entities),
            "dimensions": len(config.dimensions),
            "temporal_roles": len(config.temporal_roles),
            "measures": len(config.measures),
            "metrics": len(config.metric_recipes),
            "semantic_policies": len(config.semantic_policies),
            "semantic_caveats": len(config.semantic_caveats),
            "segments": len(config.segments),
        },
        "checks": dict(checks or {}),
    }


def _require_companion_assets(ref: PackageReference, config: Any) -> None:
    """Fail artifact builds whose declared seed assets are missing on disk.

    Without this guard a single-file package whose warehouse happens to
    be hydrated already (cached ``.duckdb``) passes every check, yet the
    artifact ships without the seed SQL it needs to hydrate elsewhere.
    """
    source = Path(ref.source_path)
    if not source.is_file():
        return
    _, missing = _single_file_companion_assets(source, config)
    if not missing:
        return
    names = ", ".join(f"{row['field']}={row['declared']!r}" for row in missing)
    raise SemanticLayerError(
        "INVALID_CONFIG",
        f"Cannot build package artifact: declared seed asset(s) not found on disk ({names})",
        details={
            "missing_assets": missing,
            "source_path": ref.source_path,
            "recovery_hints": [
                {
                    "code": "RESTORE_SEED_ASSET",
                    "message": (
                        "Each path in package.seed (source / post_sql) must exist relative to "
                        "the directory containing the package file so the artifact can hydrate "
                        "its own warehouse. Restore the file at the resolved path, or update "
                        "package.seed to point at the file's actual location."
                    ),
                }
            ],
        },
    )


def _write_package_artifact(
    ref: PackageReference, *, output_path: str, manifest: dict[str, Any], config: Any | None = None
) -> dict[str, Any]:
    config = config or load_package_config(ref.source_path)
    _require_companion_assets(ref, config)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode("utf-8")
    with tarfile.open(target, "w:gz") as archive:
        info = tarfile.TarInfo(ARTIFACT_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        archive.addfile(info, fileobj=io.BytesIO(manifest_bytes))
        for source_file, arcname in _artifact_file_rows(ref.source_path, config=config):
            member = PurePosixPath(arcname)
            if member.is_absolute() or ".." in member.parts:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"Refusing to write artifact member outside the package root: {arcname!r}",
                    details={"arcname": arcname},
                )
            archive.add(source_file, arcname=f"package/{arcname}", recursive=False)
    return {
        "path": str(target),
        "manifest_path": ARTIFACT_MANIFEST_NAME,
        "package_hash": manifest["package_hash"],
        "file_count": len(manifest.get("source", {}).get("files", [])),
    }


def run_examples_report(
    ref: PackageReference,
    *,
    parse_report: dict[str, Any] | None = None,
    config: Any | None = None,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    report = parse_report
    if report is None or config is None:
        report, config = parse_config_report(ref)
    if not report["ok"] or config is None:
        return {
            "ok": False,
            "package": dict(report["package"]),
            "summary": {"examples_total": 0, "passed": 0, "failed": 0},
            "examples": [],
            "errors": list(report["errors"]),
        }

    should_close_runtime = runtime is None
    if runtime is None:
        runtime = Runtime(ref.package_id) if ref.package_id else Runtime.from_path(ref.source_path)
    try:
        examples = _load_named_entries(
            Path(package_root_for_source(ref.source_path)) / "examples",
            plural_key="examples",
            singular_key="example",
        )
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for example_id, spec in examples:
            result = _run_example(runtime, example_id, spec)
            results.append(result)
            if not result["ok"]:
                failures.append(
                    {
                        "code": result["error"]["code"],
                        "message": result["error"]["message"],
                        "details": {
                            "example_id": example_id,
                            **dict(result["error"].get("details", {}) or {}),
                        },
                    }
                )
        return {
            "ok": not failures,
            "package": {"id": config.package.package_id, "source_path": ref.source_path},
            "summary": {
                "examples_total": len(results),
                "passed": sum(1 for row in results if row["ok"]),
                "failed": len(failures),
            },
            "examples": results,
            "errors": failures,
        }
    finally:
        if should_close_runtime:
            runtime.close()


def run_package_tests_report(
    ref: PackageReference,
    *,
    parse_report: dict[str, Any] | None = None,
    config: Any | None = None,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    report = parse_report
    if report is None or config is None:
        report, config = parse_config_report(ref)
    if not report["ok"] or config is None:
        return {
            "ok": False,
            "package": dict(report["package"]),
            "summary": {"tests_total": 0, "passed": 0, "failed": 0},
            "tests": [],
            "errors": list(report["errors"]),
        }

    should_close_runtime = runtime is None
    if runtime is None:
        runtime = Runtime(ref.package_id) if ref.package_id else Runtime.from_path(ref.source_path)
    try:
        tests = _load_named_entries(
            Path(package_root_for_source(ref.source_path)) / "tests",
            plural_key="tests",
            singular_key="test",
        )
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for test_id, spec in tests:
            result = _run_test(runtime, test_id, spec)
            results.append(result)
            if not result["ok"]:
                failures.append(
                    {
                        "code": result["error"]["code"],
                        "message": result["error"]["message"],
                        "details": {
                            "test_id": test_id,
                            **dict(result["error"].get("details", {}) or {}),
                        },
                    }
                )
        return {
            "ok": not failures,
            "package": {"id": config.package.package_id, "source_path": ref.source_path},
            "summary": {
                "tests_total": len(results),
                "passed": sum(1 for row in results if row["ok"]),
                "failed": len(failures),
            },
            "tests": results,
            "errors": failures,
        }
    finally:
        if should_close_runtime:
            runtime.close()


def _collect_column_refs(expr: Any, *, out: list[str]) -> None:
    """Walk a SemanticExpr AST and append every ColumnRefExpr.column.

    Used by the warehouse reachability check to catch measure
    expressions that reference columns the warehouse table doesn't have.
    Conservative: ignores literals and table-qualified refs we can't
    attribute to a specific entity from the AST alone.
    """
    if expr is None:
        return
    # ColumnRefExpr is a frozen dataclass — duck-type by attribute.
    column = getattr(expr, "column", None)
    if (
        isinstance(column, str)
        and column
        and not hasattr(expr, "left")
        and not hasattr(expr, "input")
    ):
        # ColumnRefExpr-shaped: only a `column` attribute, plus optional
        # entity/table. Excludes arithmetic/etc. that happen to expose
        # an unrelated `column`-named field.
        out.append(column)
        return
    # Recursively walk known compound fields.
    for attr in (
        "left",
        "right",
        "input",
        "args",
        "operand",
        "condition",
        "true_branch",
        "false_branch",
        "value",
        "expr",
        "expression",
        "predicate",
        "scope_filter",
        "left_expr",
        "right_expr",
        "numerator",
        "denominator",
        "base",
        "converted",
    ):
        child = getattr(expr, attr, None)
        if child is None:
            continue
        if isinstance(child, (list, tuple)):
            for item in child:
                _collect_column_refs(item, out=out)
        else:
            _collect_column_refs(child, out=out)


# Authored table identifiers come from package YAML. The check pipeline
# probes them via DESCRIBE / information_schema lookups, so we have to be
# defensive: validate each dotted part as a plain SQL identifier before
# interpolating, and reject anything else with a structured error rather
# than letting it mutate the probe SQL.
_PROBE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _split_probe_identifier(raw: str) -> tuple[list[str], str]:
    """Split a dotted identifier and validate each part.

    Returns ``(parts, error)``. ``error`` is empty on success. Each part
    is returned with any wrapping double-quotes stripped — callers that
    need a quoted form should call ``_quote_ident``.
    """
    text = (raw or "").strip()
    if not text:
        return [], "table identifier is empty"
    parts: list[str] = []
    for piece in text.split("."):
        unquoted = piece.strip().strip('"')
        if not unquoted or not _PROBE_IDENT_RE.match(unquoted):
            return [], (
                f"refusing to probe table {raw!r}: identifier part "
                f"{piece!r} is not a plain SQL identifier"
            )
        parts.append(unquoted)
    return parts, ""


def _describe_table_columns(adapter: Any, *, warehouse: str, table: str) -> tuple[set[str], str]:
    """Return (column_set, error_message). Empty error means success."""
    warehouse = (warehouse or "").lower()
    parts, ident_error = _split_probe_identifier(table)
    if ident_error:
        return set(), ident_error
    try:
        if warehouse == "duckdb":
            quoted = ".".join(_quote_ident(part) for part in parts)
            rows = adapter.query(f"DESCRIBE {quoted}")
            cols = {str(row.get("column_name") or row.get("name") or "").lower() for row in rows}
            cols.discard("")
            return cols, ""
        if warehouse == "snowflake":
            # Snowflake table identifiers may be DB.SCHEMA.TABLE. We pass
            # the table/schema names as bound parameters so even a
            # validated-but-creatively-named identifier (e.g. one with
            # only legal characters) cannot mutate the probe SQL.
            table_name = parts[-1].upper()
            schema_name = parts[-2].upper() if len(parts) >= 2 else ""
            params: list[str] = [table_name]
            schema_filter = ""
            if schema_name:
                schema_filter = " and upper(table_schema) = ?"
                params.append(schema_name)
            sql = (
                "select column_name from information_schema.columns "
                f"where upper(table_name) = ?{schema_filter}"
            )
            rows = _query_with_params(adapter, sql, tuple(params))
            cols = {
                str(row.get("COLUMN_NAME") or row.get("column_name") or "").lower() for row in rows
            }
            cols.discard("")
            return cols, ""
        return set(), f"unsupported warehouse {warehouse!r} for column reachability"
    except Exception as exc:
        return set(), f"could not describe table {table!r}: {exc}"


def _query_with_params(adapter: Any, sql: str, params: tuple[str, ...]) -> list[dict[str, Any]]:
    """Run a parameterised probe query.

    Adapters that accept positional binds run the parameterised form;
    otherwise we fall back to inlining (after the strict identifier
    validation in ``_split_probe_identifier``, so the values are safe to
    inline as quoted SQL literals).
    """
    try:
        return list(adapter.query(sql, params))
    except TypeError:
        inlined = sql
        for value in params:
            inlined = inlined.replace("?", "'" + value.replace("'", "''") + "'", 1)
        return list(adapter.query(inlined))


def check_warehouse_column_reachability_report(
    ref: PackageReference,
    *,
    parse_report: dict[str, Any] | None = None,
    config: Any | None = None,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    """Probe the warehouse for each authored table and flag dimension /
    measure references to columns that don't exist there.

    Pre-fix behavior: a typo in `dimension.<x>.column:` or a measure
    `expr:` that references a nonexistent column compiled and validated
    cleanly. The failure surfaced only at query time as a DuckDB binder
    error, well after the author had moved on.

    Currently implemented for DuckDB (via `DESCRIBE`) and Snowflake (via
    `information_schema.columns`). Other warehouses return ok=True with
    a warning rather than blocking the check pipeline.
    """
    report = parse_report
    if report is None or config is None:
        report, config = parse_config_report(ref)
    if not report["ok"] or config is None:
        return {
            "ok": False,
            "package": dict(report["package"]),
            "summary": {"tables_probed": 0, "missing_columns": 0},
            "errors": list(report["errors"]),
        }

    should_close_runtime = runtime is None
    if runtime is None:
        runtime = Runtime(ref.package_id) if ref.package_id else Runtime.from_path(ref.source_path)
    try:
        warehouse = (runtime.warehouse_engine or "").lower()
        if warehouse not in {"duckdb", "snowflake"}:
            # Unknown warehouse: skip gracefully, surface a warning so
            # the author knows the check didn't run.
            return {
                "ok": True,
                "package": {"id": config.package.package_id, "source_path": ref.source_path},
                "summary": {"tables_probed": 0, "missing_columns": 0},
                "errors": [],
                "warnings": [
                    {
                        "code": "COLUMN_REACHABILITY_SKIPPED",
                        "message": f"Column reachability not implemented for warehouse {warehouse!r}; skipping",
                    }
                ],
            }

        adapter = runtime._get_adapter()  # noqa: SLF001 — package-private helper

        # Build entity → table map. Skip entities that bind to non-physical
        # relations (date spines, computed pipelines): those won't probe
        # cleanly via DESCRIBE.
        relation_ids = {rel.id for rel in config.relations}
        entity_table: dict[str, str] = {}
        for entity in config.entities:
            if not entity.table or entity.relation_id in relation_ids:
                # Skip relations — pipelines may not exist as plain tables.
                continue
            entity_table[entity.id] = entity.table

        table_columns: dict[str, set[str]] = {}
        probe_errors: list[dict[str, Any]] = []
        for table in sorted(set(entity_table.values())):
            cols, err = _describe_table_columns(adapter, warehouse=warehouse, table=table)
            if err:
                probe_errors.append(
                    {
                        "code": "WAREHOUSE_TABLE_NOT_FOUND",
                        "message": err,
                        "details": {"table": table},
                    }
                )
                continue
            table_columns[table] = cols

        # Check entity key columns against the entity's table. The graph
        # `key:` is the join anchor — a typo'd key column otherwise
        # surfaces only as a join failure at query time.
        errors: list[dict[str, Any]] = list(probe_errors)
        for entity in config.entities:
            table = entity_table.get(entity.id, "")
            if not table or table not in table_columns:
                continue
            for column in entity.key or []:
                if str(column).lower() not in table_columns[table]:
                    errors.append(
                        {
                            "code": "WAREHOUSE_COLUMN_NOT_FOUND",
                            "message": (
                                f"entity {entity.id} declares key column {column!r} which is "
                                f"not present in table {table!r}"
                            ),
                            "details": {
                                "entity_id": entity.id,
                                "column": str(column),
                                "table": table,
                                "available_columns": sorted(table_columns[table]),
                            },
                        }
                    )

        # Check dimension.column against the dimension's entity table.
        for dim in config.dimensions:
            table = entity_table.get(dim.entity, "")
            if not table or table not in table_columns:
                continue
            column = str(dim.column or "").lower()
            if not column:
                continue
            if column not in table_columns[table]:
                errors.append(
                    {
                        "code": "WAREHOUSE_COLUMN_NOT_FOUND",
                        "message": (
                            f"dimension {dim.id} references column {dim.column!r} which is not "
                            f"present in table {table!r}"
                        ),
                        "details": {
                            "dimension_id": dim.id,
                            "column": dim.column,
                            "table": table,
                            "available_columns": sorted(table_columns[table]),
                        },
                    }
                )

        # Probe any extra tables referenced by measures via `source_relation`
        # (fact-table models decouple a measure's source table from the
        # entity it attaches to).
        extra_tables: set[str] = set()
        for measure in config.measures:
            src = str(getattr(measure, "source_relation", "") or "")
            if src and src not in table_columns:
                extra_tables.add(src)
        for table in sorted(extra_tables):
            cols, err = _describe_table_columns(adapter, warehouse=warehouse, table=table)
            if err:
                errors.append(
                    {
                        "code": "WAREHOUSE_TABLE_NOT_FOUND",
                        "message": err,
                        "details": {"table": table},
                    }
                )
                continue
            table_columns[table] = cols

        # Check measure expr column refs against the measure's table.
        # Prefer `source_relation` when set (fact-table models); fall back
        # to the entity's table.
        for measure in config.measures:
            src = str(getattr(measure, "source_relation", "") or "")
            table = src or entity_table.get(measure.entity, "")
            if not table or table not in table_columns:
                continue
            referenced: list[str] = []
            _collect_column_refs(measure.expr, out=referenced)
            for column in referenced:
                if column.lower() not in table_columns[table]:
                    errors.append(
                        {
                            "code": "WAREHOUSE_COLUMN_NOT_FOUND",
                            "message": (
                                f"measure {measure.id} references column {column!r} in expr which is "
                                f"not present in table {table!r}"
                            ),
                            "details": {
                                "measure_id": measure.id,
                                "column": column,
                                "table": table,
                                "available_columns": sorted(table_columns[table]),
                            },
                        }
                    )

        return {
            "ok": not errors,
            "package": {"id": config.package.package_id, "source_path": ref.source_path},
            "summary": {
                "tables_probed": len(table_columns),
                "missing_columns": len(
                    [e for e in errors if e["code"] == "WAREHOUSE_COLUMN_NOT_FOUND"]
                ),
            },
            "errors": errors,
        }
    finally:
        if should_close_runtime:
            runtime.close()


def check_package_report(
    ref: PackageReference,
    *,
    compare_path: str = "",
    base_ref: str = "",
    artifact_path: str = "",
) -> dict[str, Any]:
    parse_report, config = parse_config_report(ref)
    if config is None:
        blockers = [{"check": "parse", **dict(error)} for error in list(parse_report["errors"])]
        return {
            "ok": False,
            "package": dict(parse_report["package"]),
            "package_hash": package_fingerprint(ref.source_path)
            if Path(ref.source_path).exists()
            else "",
            "checks": {
                "parse": parse_report,
                "validate": {"ok": False, "errors": list(parse_report["errors"])},
                "examples": {"ok": False, "errors": list(parse_report["errors"])},
                "tests": {"ok": False, "errors": list(parse_report["errors"])},
            },
            "manifest": {},
            "artifact": {},
            "blockers": blockers,
        }

    runtime = Runtime.from_config(config, source_path=ref.source_path, package_id=ref.package_id)
    try:
        validate_report = validate_config_report(ref, parse_report=parse_report, runtime=runtime)
        reachability_report = check_warehouse_column_reachability_report(
            ref, parse_report=parse_report, config=config, runtime=runtime
        )
        example_report = run_examples_report(
            ref, parse_report=parse_report, config=config, runtime=runtime
        )
        test_report = run_package_tests_report(
            ref, parse_report=parse_report, config=config, runtime=runtime
        )
    finally:
        runtime.close()
    impact = (
        impact_report(ref, compare_path=compare_path, base_ref=base_ref)
        if (compare_path or base_ref) and parse_report["ok"]
        else {
            "changes": [],
            "impact": {
                "impacted_metrics": [],
                "reviewer_teams": [],
                "risk": "low",
                "changed_behavior_count": 0,
            },
            "markdown_summary": "No comparison baseline provided.",
            "comparison": {"label": "", "source_path": ""},
        }
    )
    checks = {
        "parse": {
            "ok": parse_report["ok"],
            "warnings": parse_report["summary"]["warnings"],
            "errors": parse_report["summary"]["errors"],
        },
        "validate": {
            "ok": validate_report["ok"],
            "probes_total": validate_report["summary"]["probes_total"],
            "passed": validate_report["summary"]["passed"],
            "failed": validate_report["summary"]["failed"],
        },
        "column_reachability": {
            "ok": reachability_report["ok"],
            "tables_probed": reachability_report["summary"]["tables_probed"],
            "missing_columns": reachability_report["summary"]["missing_columns"],
        },
        "examples": {
            "ok": example_report["ok"],
            "examples_total": example_report["summary"]["examples_total"],
            "passed": example_report["summary"]["passed"],
            "failed": example_report["summary"]["failed"],
        },
        "tests": {
            "ok": test_report["ok"],
            "tests_total": test_report["summary"]["tests_total"],
            "passed": test_report["summary"]["passed"],
            "failed": test_report["summary"]["failed"],
        },
    }
    ok = bool(
        parse_report["ok"]
        and validate_report["ok"]
        and reachability_report["ok"]
        and example_report["ok"]
        and test_report["ok"]
    )
    manifest = package_manifest(ref, config=config, checks=checks)
    artifact: dict[str, Any] = {}
    if artifact_path and ok:
        artifact = _write_package_artifact(
            ref, output_path=artifact_path, manifest=manifest, config=config
        )
    blockers = _check_errors(
        {
            "checks": {
                "parse": parse_report,
                "validate": validate_report,
                "column_reachability": reachability_report,
                "examples": example_report,
                "tests": test_report,
            }
        }
    )
    return {
        "ok": ok,
        "package": {"id": config.package.package_id, "source_path": ref.source_path},
        "package_hash": manifest["package_hash"],
        "checks": {
            "parse": parse_report,
            "validate": validate_report,
            "column_reachability": reachability_report,
            "examples": example_report,
            "tests": test_report,
            "impact": impact,
        },
        "summary": checks,
        "manifest": manifest,
        "artifact": artifact,
        "blockers": blockers,
    }


def build_package_artifact_report(
    ref: PackageReference,
    *,
    output_path: str,
    compare_path: str = "",
    base_ref: str = "",
) -> dict[str, Any]:
    report = check_package_report(
        ref, compare_path=compare_path, base_ref=base_ref, artifact_path=output_path
    )
    if not report["ok"]:
        return {
            "ok": False,
            "package": dict(report["package"]),
            "package_hash": report.get("package_hash", ""),
            "artifact": {},
            "checks": report["checks"],
            "errors": _check_errors(report),
            "blockers": list(report.get("blockers", []) or _check_errors(report)),
        }
    return {
        "ok": True,
        "package": dict(report["package"]),
        "package_hash": report["package_hash"],
        "artifact": dict(report["artifact"]),
        "manifest": dict(report["manifest"]),
        "checks": report["summary"],
        "blockers": [],
    }


def diff_package_report(
    ref: PackageReference, *, compare_path: str = "", base_ref: str = ""
) -> dict[str, Any]:
    current_config = load_package_config(ref.source_path)
    current_snapshot = _package_snapshot(current_config)
    other_path, compare_label = _comparison_source(
        ref.source_path, compare_path=compare_path, base_ref=base_ref
    )
    previous_config = load_package_config(other_path)
    previous_snapshot = _package_snapshot(previous_config)
    diff = _diff_snapshots(previous_snapshot, current_snapshot)
    return {
        "ok": True,
        "package": {"id": current_config.package.package_id, "source_path": ref.source_path},
        "comparison": {"label": compare_label, "source_path": other_path},
        **diff,
    }


def impact_report(
    ref: PackageReference, *, compare_path: str = "", base_ref: str = ""
) -> dict[str, Any]:
    diff = diff_package_report(ref, compare_path=compare_path, base_ref=base_ref)
    current_config = load_package_config(ref.source_path)
    impacted_metrics = _impacted_metric_ids(current_config, diff["changes"])
    reviewer_teams = sorted(
        {
            str(meta.get("owner_team", "") or "").strip()
            for meta in [
                *(dict(getattr(obj, "meta", {}) or {}) for obj in current_config.measures),
                *(dict(getattr(obj, "meta", {}) or {}) for obj in current_config.metric_recipes),
            ]
            if str(meta.get("owner_team", "") or "").strip()
        }
    )
    risk = "high" if any(change["behavior_change"] for change in diff["changes"]) else "low"
    return {
        **diff,
        "impact": {
            "impacted_metrics": impacted_metrics,
            "reviewer_teams": reviewer_teams,
            "risk": risk,
            "changed_behavior_count": sum(
                1 for change in diff["changes"] if change["behavior_change"]
            ),
        },
        "markdown_summary": _impact_markdown(
            diff["changes"], impacted_metrics, reviewer_teams, risk
        ),
    }


def promote_package_report(
    ref: PackageReference, *, environment: str, compare_path: str = "", base_ref: str = ""
) -> dict[str, Any]:
    config = load_package_config(ref.source_path)
    allowed_environments = list(config.package.environments or [])
    if allowed_environments and environment not in allowed_environments:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Environment '{environment}' is not declared for package '{config.package.package_id}'",
            details={"allowed_environments": allowed_environments},
        )
    parse_report, _ = parse_config_report(ref)
    validate_report = validate_config_report(ref)
    example_report = run_examples_report(ref)
    test_report = run_package_tests_report(ref)
    impact = (
        impact_report(ref, compare_path=compare_path, base_ref=base_ref)
        if (compare_path or base_ref)
        else {
            "changes": [],
            "impact": {
                "impacted_metrics": [],
                "reviewer_teams": [],
                "risk": "low",
                "changed_behavior_count": 0,
            },
            "markdown_summary": "No comparison baseline provided.",
            "comparison": {"label": "", "source_path": ""},
        }
    )
    checks = {
        "parse": parse_report["ok"],
        "validate": validate_report["ok"],
        "examples": example_report["ok"],
        "tests": test_report["ok"],
    }
    blockers = [
        {
            "check": name,
            "code": "PROMOTION_CHECK_FAILED",
            "message": f"{name} check must pass before promotion",
            "details": {"environment": environment},
        }
        for name, passed in checks.items()
        if not passed
    ]
    ok = not blockers
    return {
        "ok": ok,
        "package": {"id": config.package.package_id, "source_path": ref.source_path},
        "environment": environment,
        "release_labels": package_release_labels(config),
        "promotion_checks": checks,
        "blockers": blockers,
        "artifacts": {
            "parse": parse_report,
            "validate": validate_report,
            "examples": example_report,
            "tests": test_report,
            "impact": impact,
        },
    }


def _run_example(runtime: Runtime, example_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    query = dict(spec.get("query", {}) or {})
    if not query:
        return {
            "id": example_id,
            "ok": False,
            "error": {
                "code": "INVALID_CONFIG",
                "message": "example query is required",
                "details": {},
            },
        }
    try:
        result = runtime.query(query)
    except SemanticLayerError as exc:
        return {
            "id": example_id,
            "ok": False,
            "error": {"code": exc.code, "message": str(exc), "details": exc.details},
            "query": query,
        }
    shape = dict(spec.get("expected_shape", {}) or {})
    columns = list(shape.get("columns", []) or [])
    actual_columns = (
        list(result["rows"][0].keys())
        if result["rows"]
        else list(result["normalized_query"].get("group_by", []))
    )
    if columns and actual_columns[: len(columns)] != columns:
        return {
            "id": example_id,
            "ok": False,
            "error": {
                "code": "EXAMPLE_SHAPE_MISMATCH",
                "message": "example columns do not match expected shape",
                "details": {"expected": columns, "actual": actual_columns},
            },
            "query": query,
        }
    row_count = int(result["row_count"])
    min_rows = shape.get("min_rows")
    max_rows = shape.get("max_rows")
    if min_rows is not None and row_count < int(min_rows):
        return {
            "id": example_id,
            "ok": False,
            "error": {
                "code": "EXAMPLE_ROW_COUNT_MISMATCH",
                "message": "example row count is below expected minimum",
                "details": {"min_rows": min_rows, "row_count": row_count},
            },
            "query": query,
        }
    if max_rows is not None and row_count > int(max_rows):
        return {
            "id": example_id,
            "ok": False,
            "error": {
                "code": "EXAMPLE_ROW_COUNT_MISMATCH",
                "message": "example row count is above expected maximum",
                "details": {"max_rows": max_rows, "row_count": row_count},
            },
            "query": query,
        }
    return {
        "id": example_id,
        "ok": True,
        "query": query,
        "row_count": row_count,
        "columns": actual_columns,
    }


def _run_test(runtime: Runtime, test_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    kind = str(spec.get("kind", "") or "").strip()
    query = dict(spec.get("query", {}) or {})
    if kind == "query_returns_columns":
        result = runtime.query(query)
        expected = list(spec.get("columns", []) or [])
        actual = list(result["rows"][0].keys()) if result["rows"] else []
        ok = actual[: len(expected)] == expected
        return _test_result(
            test_id,
            ok,
            "QUERY_COLUMNS_MISMATCH",
            "query result columns differ from expectation",
            {"expected": expected, "actual": actual},
        )
    if kind == "query_row_count_bounds":
        result = runtime.query(query)
        row_count = int(result["row_count"])
        min_rows = spec.get("min_rows")
        max_rows = spec.get("max_rows")
        ok = (min_rows is None or row_count >= int(min_rows)) and (
            max_rows is None or row_count <= int(max_rows)
        )
        return _test_result(
            test_id,
            ok,
            "QUERY_ROW_COUNT_MISMATCH",
            "query row count is outside the expected bounds",
            {"row_count": row_count, "min_rows": min_rows, "max_rows": max_rows},
        )
    if kind == "validate_fails_with_code":
        result = runtime.validate(query)
        code = str(spec.get("code", "") or "")
        actual_code = str((result.get("errors") or [{}])[0].get("code", "") or "")
        ok = (not result["ok"]) and actual_code == code
        return _test_result(
            test_id,
            ok,
            "VALIDATION_CODE_MISMATCH",
            "validation error code did not match expectation",
            {"expected": code, "actual": actual_code},
        )
    if kind == "explain_contains":
        result = runtime.compile(query)
        needle = str(spec.get("text", "") or "")
        blob = json.dumps(result, sort_keys=True, default=str)
        ok = needle in blob
        return _test_result(
            test_id,
            ok,
            "EXPLAIN_TEXT_MISMATCH",
            "explain output did not contain expected text",
            {"needle": needle},
        )
    if kind == "query_matches_snapshot":
        result = runtime.query(query)
        expected_rows = list(spec.get("expected_rows", []) or [])
        actual_rows = _normalize_rows(result["rows"])
        ok = actual_rows == _normalize_rows(expected_rows)
        return _test_result(
            test_id,
            ok,
            "SNAPSHOT_MISMATCH",
            "query rows do not match the expected snapshot",
            {"expected_rows": expected_rows, "actual_rows": actual_rows},
        )
    if kind == "metric_equals_query":
        metric_query = dict(spec.get("metric_query", query) or {})
        expected_query = dict(spec.get("expected_query", {}) or {})
        metric_rows = _normalize_rows(runtime.query(metric_query)["rows"])
        expected_rows = _normalize_rows(runtime.query(expected_query)["rows"])
        ok = metric_rows == expected_rows
        return _test_result(
            test_id,
            ok,
            "METRIC_QUERY_MISMATCH",
            "metric query and expected query returned different results",
            {"metric_rows": metric_rows, "expected_rows": expected_rows},
        )
    return _test_result(
        test_id, False, "INVALID_CONFIG", "unknown package test kind", {"kind": kind}
    )


def _test_result(
    test_id: str, ok: bool, code: str, message: str, details: dict[str, Any]
) -> dict[str, Any]:
    result = {"id": test_id, "ok": ok}
    if not ok:
        result["error"] = {"code": code, "message": message, "details": details}
    return result


def _check_errors(report: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for name, check in dict(report.get("checks", {}) or {}).items():
        if name == "impact" or not isinstance(check, dict):
            continue
        for error in list(check.get("errors", []) or []):
            errors.append({"check": name, **dict(error)})
    return errors


def _load_named_entries(
    root: Path, *, plural_key: str, singular_key: str
) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    if not root.is_dir():
        return entries
    for file_path in sorted([*root.rglob("*.yml"), *root.rglob("*.yaml")]):
        raw = dict(yaml_safe_load(file_path.read_text(encoding="utf-8")) or {})
        mapping = raw.get(plural_key)
        if isinstance(mapping, dict):
            for entry_id, spec in mapping.items():
                entries.append((str(entry_id), dict(spec or {})))
            continue
        single = raw.get(singular_key)
        if isinstance(single, dict):
            entries.append((str(single.get("id", file_path.stem)), dict(single)))
    return entries


def _package_snapshot(config) -> dict[str, Any]:
    return {
        "package": {
            "id": config.package.package_id,
            "environments": list(config.package.environments),
            "release_labels": package_release_labels(config),
        },
        "measures": {
            row.id: {
                "entity": row.entity,
                # The expression and physical source are the highest-risk
                # edits an author can make to a measure; without them in the
                # snapshot, `diff-package` reports "0 changes" for a
                # redefined measure SQL expression.
                "expression": expr_to_dict(row.expr),
                "source_relation": row.source_relation,
                "row_grain": list(row.row_grain),
                "accumulation": {
                    "kind": row.accumulation.kind,
                    "snapshot": row.accumulation.snapshot,
                },
                "default_aggregation": row.default_aggregation,
                "allowed_aggregations": list(row.allowed_aggregations),
                "default_temporal_role": row.default_temporal_role,
                "compatible_temporal_roles": list(row.compatible_temporal_roles),
                "comparison_family": row.comparison_family,
                "comparison_mode": row.comparison_mode,
                "meta": dict(row.meta),
            }
            for row in config.measures
        },
        "metrics": {
            row.id: {
                "kind": row.kind,
                "temporal_role": row.temporal_role,
                "compatible_temporal_roles": list(row.compatible_temporal_roles),
                "comparison_family": row.comparison_family,
                "comparison_mode": row.comparison_mode,
                "expression": expr_to_dict(row.expression),
                "meta": dict(row.meta),
            }
            for row in config.metric_recipes
        },
        "relationships": {
            row.id: {
                "source_entity": row.source_entity,
                "target_entity": row.target_entity,
                "cardinality": row.cardinality,
                "target_key_type": row.target_key_type,
                "source_key_role": row.source_key_role,
                "target_key_role": row.target_key_role,
                "join_semantics": row.join_semantics,
                "safety": row.safety,
                "temporal_validity": dict(row.temporal_validity),
            }
            for row in config.relationships
        },
        "entities": {
            row.id: {
                # Repointing an entity at a different physical relation is a
                # behavior change and must surface in diff/impact reports.
                "table": row.table,
                "allowed_as_root": row.allowed_as_root,
                "key": list(row.key),
                "key_roles": dict(row.key_roles),
                "foreign_keys": {key: list(value) for key, value in row.foreign_keys.items()},
                "foreign_key_roles": dict(row.foreign_key_roles),
            }
            for row in config.entities
        },
        "dimensions": {
            row.id: {"entity": row.entity, "value_domain": row.value_domain}
            for row in config.dimensions
        },
        "policies": {
            row.id: {
                "kind": row.kind,
                "object_ids": list(row.object_ids),
                "action": row.action,
                "rationale": row.rationale,
                "config": dict(row.config),
            }
            for row in config.semantic_policies
        },
        "caveats": {
            row.id: {
                "kind": row.kind,
                "message": row.message,
                "object_ids": list(row.object_ids),
                "entity_values": [dict(item) for item in row.entity_values],
                "time": dict(row.time),
                "audiences": list(row.audiences),
                "environments": list(row.environments),
                "severity": row.severity,
                "owner": row.owner,
            }
            for row in config.semantic_caveats
        },
    }


def _diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    for section in (
        "entities",
        "dimensions",
        "relationships",
        "measures",
        "metrics",
        "policies",
        "caveats",
    ):
        before_rows = dict(before.get(section, {}) or {})
        after_rows = dict(after.get(section, {}) or {})
        # Caveats are advisory by contract: they never alter SQL, rows, or
        # policy, so their edits count as metadata-only in review summaries.
        advisory = section == "caveats"
        ids = sorted(set(before_rows) | set(after_rows))
        for object_id in ids:
            if object_id not in before_rows:
                changes.append(
                    {
                        "object_id": object_id,
                        "kind": section,
                        "change_type": "added",
                        "behavior_change": not advisory,
                        "changed_fields": list(sorted(after_rows[object_id].keys())),
                    }
                )
                continue
            if object_id not in after_rows:
                changes.append(
                    {
                        "object_id": object_id,
                        "kind": section,
                        "change_type": "removed",
                        "behavior_change": not advisory,
                        "changed_fields": list(sorted(before_rows[object_id].keys())),
                    }
                )
                continue
            before_row = before_rows[object_id]
            after_row = after_rows[object_id]
            if before_row == after_row:
                continue
            changed_fields = sorted(
                {
                    key
                    for key in set(before_row) | set(after_row)
                    if before_row.get(key) != after_row.get(key)
                }
            )
            metadata_only = advisory or set(changed_fields).issubset({"meta"})
            changes.append(
                {
                    "object_id": object_id,
                    "kind": section,
                    "change_type": "changed",
                    "behavior_change": not metadata_only,
                    "changed_fields": changed_fields,
                }
            )
    return {
        "summary": {
            "changes_total": len(changes),
            "behavior_changes": sum(1 for change in changes if change["behavior_change"]),
            "metadata_only_changes": sum(1 for change in changes if not change["behavior_change"]),
        },
        "changes": changes,
    }


def _impacted_metric_ids(config, changes: list[dict[str, Any]]) -> list[str]:
    changed_ids = {row["object_id"] for row in changes}
    impacted: list[str] = []
    for recipe in config.metric_recipes:
        serialized = json.dumps(expr_to_dict(recipe.expression), sort_keys=True)
        if recipe.id in changed_ids or any(changed_id in serialized for changed_id in changed_ids):
            impacted.append(recipe.id)
    return sorted(set(impacted))


def _impact_markdown(
    changes: list[dict[str, Any]], impacted_metrics: list[str], reviewer_teams: list[str], risk: str
) -> str:
    lines = [
        "# Semantic Impact Report",
        "",
        f"- Risk: `{risk}`",
        f"- Changes: `{len(changes)}`",
        f"- Impacted metrics: `{len(impacted_metrics)}`",
    ]
    if reviewer_teams:
        lines.append(f"- Reviewer teams: `{', '.join(reviewer_teams)}`")
    lines.append("")
    for change in changes[:20]:
        lines.append(
            f"- `{change['change_type']}` `{change['kind']}` `{change['object_id']}` fields={', '.join(change['changed_fields'])}"
        )
    if impacted_metrics:
        lines.extend(["", "## Impacted Metrics"])
        lines.extend([f"- `{metric_id}`" for metric_id in impacted_metrics[:20]])
    return "\n".join(lines)


def _comparison_source(source_path: str, *, compare_path: str, base_ref: str) -> tuple[str, str]:
    if compare_path:
        return str(Path(compare_path).resolve()), str(Path(compare_path).resolve())
    if base_ref:
        extracted_path = _extract_package_from_git(source_path, base_ref)
        return extracted_path, base_ref
    raise SemanticLayerError(
        "INVALID_CONFIG", "Provide either compare_path or base_ref for diff and impact reports"
    )


def _extract_package_from_git(source_path: str, base_ref: str) -> str:
    package_root = Path(package_root_for_source(source_path)).resolve()
    repo = Path(repo_root()).resolve()
    try:
        rel_package_path = package_root.relative_to(repo).as_posix()
    except ValueError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Package path '{package_root}' is not inside the git repo"
        ) from exc
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", base_ref, "--", rel_package_path],
        cwd=repo,
        text=True,
    ).splitlines()
    if not files:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"No package files found for '{rel_package_path}' at git ref '{base_ref}'",
        )
    temp_root = Path(tempfile.mkdtemp(prefix="semantic-rails-package-"))
    for relative_file in files:
        target_path = temp_root / relative_file
        target_path.parent.mkdir(parents=True, exist_ok=True)
        content = subprocess.check_output(["git", "show", f"{base_ref}:{relative_file}"], cwd=repo)
        target_path.write_bytes(content)
    return str(temp_root / rel_package_path)


def _normalize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [dict(row) for row in rows]
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, default=str))
