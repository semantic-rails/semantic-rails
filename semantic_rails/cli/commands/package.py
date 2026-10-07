"""Package lifecycle commands: parse, validate, check, build, examples,
tests, diff, impact, promote, doctor, init, import and export.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ...config import package_root_for_source
from ...config_validation import (
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)
from ...contracts import export_metric_portability, export_semantic_contract
from ...errors import SemanticLayerError
from ...interop.ossie import import_ossie, write_ossie_export
from ...mcp import SemanticLayerMCPAdapter
from ...package_tools import (
    build_package_artifact_report,
    check_package_report,
    check_warehouse_column_reachability_report,
    diff_package_report,
    impact_report,
    promote_package_report,
    run_examples_report,
    run_package_tests_report,
)
from ...runtime import Runtime
from ..common import _print, _print_stderr


def cmd_doctor(args: argparse.Namespace) -> None:
    checks = []
    ref = resolve_package_reference(
        package_id=getattr(args, "package", ""), path=getattr(args, "path", "")
    )
    parse_report, _ = parse_config_report(ref, progress=lambda _: None)
    checks.append(
        {
            "name": "parse_config",
            "ok": bool(parse_report.get("ok")),
            "errors": list(parse_report.get("errors", []) or []),
        }
    )
    validate_report = (
        validate_config_report(ref, progress=lambda _: None)
        if parse_report.get("ok")
        else {"ok": False, "errors": parse_report.get("errors", [])}
    )
    checks.append(
        {
            "name": "validate_config",
            "ok": bool(validate_report.get("ok")),
            "errors": list(validate_report.get("errors", []) or []),
        }
    )
    try:
        runtime = Runtime.from_path(ref.source_path) if ref.source_path else Runtime(ref.package_id)
        try:
            adapter = SemanticLayerMCPAdapter(runtime)
            checks.append(
                {
                    "name": "mcp_adapter",
                    "ok": bool(adapter.list_tools()),
                    "tool_count": len(adapter.list_tools()),
                }
            )
            checks.append(
                {
                    "name": "warehouse_config",
                    "ok": True,
                    "warehouse": runtime.warehouse,
                    "connection_kind": runtime._config.package.connection.kind,
                    "connectivity_checked": False,
                }
            )
        finally:
            runtime.close()
    except Exception as exc:
        checks.append(
            {
                "name": "runtime_load",
                "ok": False,
                "error": str(exc),
                "hint": "The package failed to load — fix the errors above, then re-run doctor.",
            }
        )
    # Dockerfile presence is informational only: a standalone package
    # author has no Dockerfile and that must not fail doctor. Look next
    # to the package source, not the current working directory.
    package_root = Path(package_root_for_source(ref.source_path)) if ref.source_path else Path(".")
    dockerfile = package_root / "Dockerfile"
    checks.append(
        {
            "name": "dockerfile",
            "ok": True,
            "present": dockerfile.exists(),
            "path": str(dockerfile),
            "note": ("informational — only needed for container deploys; see docs/DEPLOYMENT.md"),
        }
    )
    failing = [check for check in checks if not bool(check.get("ok"))]
    _print(
        {
            "ok": not failing,
            "package": ref.display_name,
            "checks": checks,
            "failing_checks": [str(check.get("name", "")) for check in failing],
        }
    )


def cmd_import(args: argparse.Namespace) -> None:
    """Translate an external semantic-layer config into a Semantic Rails
    package directory. Today supports `--from metricflow` (a MetricFlow
    YAML directory or a dbt-emitted `semantic_manifest.json`). No
    MetricFlow runtime is required — the translator reads YAML/JSON
    files standalone. `--from ossie` reads an Apache Ossie document and
    the Semantic Rails sidecar beside it."""
    if args.source_format == "ossie":
        try:
            _print(
                import_ossie(
                    args.source,
                    args.output,
                    package_id=args.package_id,
                    namespace=args.namespace or "",
                    default_db=args.default_db or "",
                )
            )
        except FileExistsError as exc:
            raise SemanticLayerError("CONFIG_CONFLICT", str(exc)) from exc
        return
    if args.source_format == "metricflow":
        from mf2sr import translate

        try:
            report = translate(
                Path(args.source),
                Path(args.output),
                package_id=args.package_id,
                namespace=args.namespace,
                warehouse=args.warehouse,
                default_db=args.default_db,
                description=args.description,
                keep_schema=args.keep_schema,
            )
        except FileExistsError as exc:
            raise SemanticLayerError("CONFIG_CONFLICT", str(exc)) from exc
        _print(
            {
                "ok": True,
                "package_dir": str(report.package_dir),
                "models_emitted": report.models_emitted,
                "metrics_emitted": report.metrics_emitted,
                "warnings": report.warnings,
            }
        )
        return
    raise SemanticLayerError(
        "INVALID_CONFIG",
        f"--from {args.source_format!r} is not a supported source format",
    )


def cmd_parse_config(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report, _ = parse_config_report(ref, progress=_print_stderr)
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_export_contract(args: argparse.Namespace) -> None:
    """Emit the canonical framework-neutral semantic validation contract."""

    ref = resolve_package_reference(package_id=args.package, path=args.path)
    exporter = (
        export_metric_portability
        if getattr(args, "format", "validation") == "metrics"
        else export_semantic_contract
    )
    payload = exporter(ref.source_path)
    rendered = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        return
    print(rendered, end="")


def cmd_export(args: argparse.Namespace) -> None:
    """Export a package to an external semantic-model format. Today supports `--format ossie`."""
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    _print(write_ossie_export(ref.source_path, args.output))


def cmd_validate_config(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = validate_config_report(ref, progress=None if args.quiet else _print_stderr)
    # Column reachability runs here too (not just in `check`): a dimension
    # or measure pointing at a column the warehouse doesn't have should
    # fail the command the author actually runs first, not query time.
    if report["ok"]:
        reachability = check_warehouse_column_reachability_report(ref)
        report["column_reachability"] = {
            "ok": reachability["ok"],
            "summary": dict(reachability.get("summary", {})),
            "errors": list(reachability.get("errors", [])),
        }
        if not reachability["ok"]:
            report["ok"] = False
            report["errors"] = list(report.get("errors", [])) + list(reachability.get("errors", []))
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)
    if not getattr(args, "no_manifest", False):
        from ...manifest import write_manifest

        runtime = Runtime.from_path(ref.source_path) if ref.source_path else Runtime(ref.package_id)
        try:
            path = write_manifest(runtime)
        except Exception as exc:  # noqa: BLE001 — manifest write is best-effort
            # Always surface a write failure — best-effort doesn't mean silent.
            _print_stderr(f"manifest write failed: {exc}")
        else:
            if not args.quiet:
                _print_stderr(f"manifest written: {path}")


def cmd_check(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = check_package_report(
        ref,
        compare_path=args.compare_path,
        base_ref=args.base_ref,
        artifact_path=args.artifact,
    )
    _print(_check_cli_payload(report, full=args.full))
    if not report["ok"]:
        raise SystemExit(1)


def cmd_build_package(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = build_package_artifact_report(
        ref,
        output_path=args.output,
        compare_path=args.compare_path,
        base_ref=args.base_ref,
    )
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_run_examples(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = run_examples_report(ref)
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_test_package(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = run_package_tests_report(ref)
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_diff_package(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    _print(diff_package_report(ref, compare_path=args.compare_path, base_ref=args.base_ref))


def cmd_impact_report(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    _print(impact_report(ref, compare_path=args.compare_path, base_ref=args.base_ref))


def cmd_promote_package(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = promote_package_report(
        ref, environment=args.environment, compare_path=args.compare_path, base_ref=args.base_ref
    )
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)


def _check_cli_payload(report: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    if full:
        return report
    payload = {
        "ok": bool(report.get("ok", False)),
        "package": dict(report.get("package", {}) or {}),
        "package_hash": str(report.get("package_hash", "") or ""),
        "summary": dict(report.get("summary", {}) or {}),
        "manifest": dict(report.get("manifest", {}) or {}),
        "artifact": dict(report.get("artifact", {}) or {}),
        "blockers": list(report.get("blockers", []) or []),
    }
    if not payload["ok"]:
        errors = []
        for name, check in dict(report.get("checks", {}) or {}).items():
            if name == "impact" or not isinstance(check, dict):
                continue
            for error in list(check.get("errors", []) or []):
                errors.append({"check": name, **dict(error)})
        payload["errors"] = errors
    return payload
