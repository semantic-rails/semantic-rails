"""CLI entry point for the MetricFlow -> Semantic Rails translator.

Run directly::

    python -m mf2sr \\
        --source /path/to/metricflow/yamls \\
        --output configs/semantic_rails \\
        --package-id shop \\
        --namespace shop \\
        --warehouse duckdb

Or via Snowflake::

    python -m mf2sr --source manifest.json --output out/ \\
        --package-id shop --warehouse snowflake
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .translate import translate


def _warehouse_choices() -> tuple[str, ...]:
    """Every warehouse registered in Semantic Rails' dialect registry.

    Imported lazily so `mf2sr` stays importable without
    `semantic_rails` on the path (the translator itself has no
    dependency on it); the CLI only needs the registry to validate
    `--warehouse`.
    """
    from semantic_rails.dialects import supported_warehouses

    return supported_warehouses()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mf2sr",
        description=(
            "Deterministically translate a MetricFlow YAML directory or "
            "semantic_manifest.json into a Semantic Rails package."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to a MetricFlow YAML directory OR a semantic_manifest.json file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help=(
            "Directory under which `<package-id>/` will be written (e.g. `configs/semantic_rails`)."
        ),
    )
    parser.add_argument(
        "--package-id",
        required=True,
        help="Semantic Rails package id (e.g. `shop`). Drives the namespace too.",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="Semantic Rails namespace prefix. Defaults to `--package-id`.",
    )
    parser.add_argument(
        "--warehouse",
        default="duckdb",
        choices=_warehouse_choices(),
        help="Warehouse kind written into `package.warehouse`.",
    )
    parser.add_argument(
        "--default-db",
        default=None,
        help="DuckDB file path for `package.default_db` (DuckDB only).",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Optional package description.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any warnings are emitted.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = translate(
        Path(args.source),
        Path(args.output),
        package_id=args.package_id,
        namespace=args.namespace,
        warehouse=args.warehouse,
        default_db=args.default_db,
        description=args.description,
    )
    print(f"Wrote package -> {report.package_dir}")
    print(f"  Models:  {len(report.models_emitted)}")
    print(f"  Metrics: {len(report.metrics_emitted)}")
    if report.warnings:
        print(f"  Warnings ({len(report.warnings)}):")
        for warn in report.warnings:
            print(f"    - {warn}")
        if args.strict:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
