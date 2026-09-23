"""Package lifecycle commands: parse, validate, check, build, examples,
tests, diff, impact, promote, doctor, init and import.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ...config import package_root_for_source, resolve_repo_path
from ...config_validation import (
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)
from ...contracts import export_metric_portability, export_semantic_contract
from ...errors import SemanticLayerError
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
from .project import cmd_init_project


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


def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.output).expanduser().resolve()
    if target.exists() and any(target.iterdir()) and not args.force:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Target directory '{target}' is not empty; pass --force to overwrite starter files",
        )
    target.mkdir(parents=True, exist_ok=True)
    # resolve_repo_path checks the repo root and the installed data-files
    # root (share/semantic-rails/), so this works from a source checkout and
    # from a pip-installed wheel — the starter template ships as a data file.
    starter = Path(resolve_repo_path("configs/examples/semantic_rails_package_starter.yml"))
    if not starter.exists():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Bundled starter template not found "
            "(configs/examples/semantic_rails_package_starter.yml). Reinstall the "
            "package or run from a source checkout.",
        )
    package_text = starter.read_text(encoding="utf-8")
    package_id = str(args.package_id or target.name).strip()
    namespace = str(args.namespace or package_id.replace("-", "_")).strip()
    package_text = package_text.replace("id: shop_starter", f"id: {package_id}")
    package_text = package_text.replace("namespace: shop", f"namespace: {namespace}")
    # Fully-qualified ids in the template are derived from the starter's
    # `namespace: shop`; rewrite them so expression references (e.g.
    # measure.shop.line_revenue_usd) keep resolving under the new namespace.
    for kind in ("entity", "dimension", "measure", "metric", "segment"):
        package_text = package_text.replace(f"{kind}.shop.", f"{kind}.{namespace}.")
    package_text = package_text.replace(
        "default_db: data/shop_starter.duckdb", f"default_db: data/{package_id}.duckdb"
    )
    package_text = package_text.replace(
        "source: data/seed_shop.sql", "source: data/seed_example.sql"
    )
    # The starter header tells readers to validate the repo-internal
    # template path; point the generated file's header at itself instead.
    package_text = package_text.replace(
        "uv run semantic-rails parse-config --path "
        "configs/examples/semantic_rails_package_starter.yml",
        f"semantic-rails validate-config --path {target / 'package.yml'}",
    )
    (target / "package.yml").write_text(package_text, encoding="utf-8")
    data_dir = target / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "seed_example.sql").write_text(
        """
CREATE OR REPLACE TABLE shop_customer AS
SELECT 'customer_1' AS customer_id, 'new' AS customer_type, TIMESTAMP '2026-01-01 00:00:00' AS first_ordered_at;

CREATE OR REPLACE TABLE shop_order AS
SELECT 'order_1' AS order_id, 'customer_1' AS customer_id, 'web' AS channel, 4200 AS order_total_cents, TIMESTAMP '2026-01-02 00:00:00' AS ordered_at;

CREATE OR REPLACE TABLE shop_order_item AS
SELECT 'item_1' AS order_item_id, 'order_1' AS order_id, 'product_1' AS product_id, 1 AS quantity, 4200 AS line_total_cents;

CREATE OR REPLACE TABLE shop_product AS
SELECT 'product_1' AS product_id, 'beverage' AS product_type;
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _print(
        {
            "ok": True,
            "path": str(target),
            "package_id": package_id,
            "files": ["package.yml", "data/seed_example.sql"],
        }
    )


def cmd_import(args: argparse.Namespace) -> None:
    """Translate an external semantic-layer config into a Semantic Rails
    package directory. Today supports `--from metricflow` (a MetricFlow
    YAML directory or a dbt-emitted `semantic_manifest.json`). No
    MetricFlow runtime is required — the translator reads YAML/JSON
    files standalone."""
    if args.source_format == "metricflow":
        from mf2sr import translate

        report = translate(
            Path(args.source),
            Path(args.output),
            package_id=args.package_id,
            namespace=args.namespace,
            warehouse=args.warehouse,
            default_db=args.default_db,
            description=args.description,
        )
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


def cmd_init_dispatch(args: argparse.Namespace) -> None:
    split_requested = bool(getattr(args, "split", False))
    single_file_requested = bool(getattr(args, "single_file", False))
    package_name = str(getattr(args, "name", "") or "")
    if split_requested and single_file_requested:
        raise SemanticLayerError(
            "INVALID_CONFIG", "Choose either --split or --single-file, not both"
        )
    if single_file_requested:
        if package_name and not getattr(args, "package_id", ""):
            args.package_id = package_name
        if not getattr(args, "output", ""):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Provide --output when creating a single-file package.",
            )
        cmd_init(args)
        return
    if split_requested or package_name:
        cmd_init_project(args)
        return
    if not getattr(args, "output", ""):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Provide a package name (`semantic-rails init my_package`) or --output for the legacy single-file scaffold.",
        )
    cmd_init(args)
