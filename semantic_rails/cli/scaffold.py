"""Split-layout starter package scaffolding for ``init`` and ``project new``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config_validation import PackageReference
from ..errors import SemanticLayerError
from .common import _quote, _slug, _title
from .reports import project_validation_report


def create_project_report(
    *,
    package_id: str,
    output: str = "",
    workspace_root: str = "",
    description: str = "",
    entity: str = "event",
    relation: str = "raw_events",
    primary_key: str = "event_id",
    time_column: str = "occurred_at",
    amount_column: str = "amount",
    force: bool = False,
    run_checks: bool = True,
) -> dict[str, Any]:
    package_slug = _slug(package_id)
    target = _project_target(package_slug, output=output, workspace_root=workspace_root)
    _validate_project_target(target)
    if target.name != package_slug:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Directory package paths must end with the package id",
            details={"package_id": package_slug, "target": str(target)},
        )
    if target.exists() and any(target.iterdir()) and not force:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Target directory '{target}' is not empty; pass --force to overwrite starter files",
            details={"target": str(target)},
        )
    _reject_symlink_tree(target)

    entity_slug = _slug(entity)
    relation_slug = _slug(relation, fallback="raw_events")
    primary_key_slug = _slug(primary_key, fallback="event_id")
    time_column_slug = _slug(time_column, fallback="occurred_at")
    amount_column_slug = _slug(amount_column, fallback="amount")
    documents = _starter_documents(
        package_id=package_slug,
        description=description or f"{_title(package_slug)} Semantic Rails package.",
        entity=entity_slug,
        relation=relation_slug,
        primary_key=primary_key_slug,
        time_column=time_column_slug,
        amount_column=amount_column_slug,
    )

    changed: list[str] = []
    for relative, payload in documents["yaml"].items():
        path = target / relative
        _write_project_text(
            target,
            path,
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        )
        changed.append(relative)

    for relative, content in documents["text"].items():
        _write_project_text(target, target / relative, content)
        changed.append(relative)

    csv_path = target / "data" / f"{package_slug}_csv" / f"{relation_slug}.csv"
    _write_project_text(target, csv_path, documents["csv"])
    changed.append(csv_path.relative_to(target).as_posix())

    ref = PackageReference(source_path=str(target))
    checks = (
        project_validation_report(ref, mode="full") if run_checks else {"ok": True, "checks": {}}
    )
    ok = bool(checks.get("ok", False))
    return {
        "ok": ok,
        "status": "created" if ok else "created_with_check_failures",
        "package_id": package_slug,
        "project_path": str(target),
        "changed_files": changed,
        "checks": checks.get("checks", {}),
        "summary": checks.get("summary", {}),
        "example_query_path": "examples/core.yml",
        "next_actions": [
            f"semantic-rails project status --path {_quote(target)}",
            f"semantic-rails project validate --path {_quote(target)}",
            f"semantic-rails profile init --package-path {_quote(target)}",
            f'semantic-rails ask --path {_quote(target)} "total amount by event type" --run',
            f"semantic-rails mcp setup --path {_quote(target)}",
        ],
    }


def _starter_documents(
    *,
    package_id: str,
    description: str,
    entity: str,
    relation: str,
    primary_key: str,
    time_column: str,
    amount_column: str,
) -> dict[str, Any]:
    entity_title = _title(entity)
    package_title = _title(package_id)
    model_id = f"{entity}s" if not entity.endswith("s") else entity
    dimension_id = f"dimension.{package_id}_{entity}_event_type"
    temporal_role = f"temporal_role.{package_id}_{entity}_{time_column}"
    count_measure = f"measure.{package_id}.{entity}_count"
    amount_measure = f"measure.{package_id}.total_amount"
    amount_metric = f"metric.{package_id}.total_amount"

    return {
        "yaml": {
            "package.yml": {
                "schema_version": 1,
                "package": {
                    "id": package_id,
                    "namespace": package_id,
                    "name": package_id,
                    "description": description,
                    "warehouse": "duckdb",
                    "default_db": f"data/{package_id}.duckdb",
                    "schema_strict": True,
                    "environments": ["development", "staging", "production"],
                    "seed": {
                        "kind": "csv_dir_duckdb",
                        "source": f"data/{package_id}_csv",
                    },
                },
                "defaults": {
                    "dimension": {"groupable": True, "filterable": True},
                    "time": {
                        "timezone": "UTC",
                        "default_query_axis": False,
                        "supported_grains": ["day", "week", "month", "quarter", "year"],
                    },
                    "measure": {"subject_entity": "self", "aggregation_entity": "self"},
                    "relationship": {"traversal": ["forward", "reverse"]},
                },
            },
            "graph.yml": {
                "graph": {
                    "entities": {
                        entity: {
                            "label": entity_title,
                            "key": [primary_key],
                            "model": model_id,
                            "allowed_as_root": True,
                        }
                    }
                }
            },
            f"models/core/{model_id}.yml": {
                "model": {
                    "id": model_id,
                    "label": f"{entity_title} events",
                    "description": f"One row per {entity.replace('_', ' ')}.",
                    "relation": relation,
                    "entities": {entity: {}},
                    "times": {
                        time_column: {
                            "label": _title(time_column),
                            "column": time_column,
                            "kind": "timestamp",
                            "class": "event_time",
                            "default": True,
                        }
                    },
                    "dimensions": {
                        "event_type": {
                            "label": "Event type",
                            "kind": "categorical",
                            "domain": ["starter", "follow_up"],
                        }
                    },
                    "measures": {
                        f"{entity}_count": {
                            "label": f"{entity_title} count",
                            "kind": "entity_count",
                            "entity_key": primary_key,
                            "accumulation": {"kind": "event"},
                            "value_type": "count",
                            "meta": {
                                "owner_team": "analytics",
                                "review_priority": "medium",
                                "change_risk": "low",
                            },
                        },
                        "total_amount": {
                            "label": "Total amount",
                            "kind": "aggregate",
                            "expr": amount_column,
                            "default_agg": "sum",
                            "accumulation": {"kind": "flow"},
                            "value_type": "number",
                            "meta": {
                                "owner_team": "analytics",
                                "review_priority": "medium",
                                "change_risk": "low",
                            },
                        },
                    },
                }
            },
            "metrics/core/starter.yml": {
                "metrics": {
                    "total_amount": {
                        "label": "Total amount",
                        "description": (
                            f"Governed total amount for the {package_title} starter package."
                        ),
                        "kind": "aggregate",
                        "measure": "total_amount",
                        "value_type": "number",
                        "meta": {
                            "owner_team": "analytics",
                            "review_priority": "medium",
                            "change_risk": "low",
                        },
                    }
                }
            },
            "examples/core.yml": {
                "examples": {
                    "starter_amount_by_type": {
                        "question": "Total amount by event type",
                        "query": {
                            "version": 1,
                            "select": [
                                {"expression": {"metric": amount_metric}, "as": "total_amount"}
                            ],
                            "group_by": [dimension_id],
                            "time": {"temporal_role": temporal_role, "grain": "day"},
                            "order_by": [{"field": "time", "direction": "ASC"}],
                            "limit": 10,
                        },
                        "expected_shape": {
                            "columns": [
                                dimension_id,
                                f"{temporal_role}__day",
                                "total_amount",
                            ],
                            "min_rows": 1,
                        },
                    }
                }
            },
            "tests/core.yml": {
                "tests": {
                    "starter_count_returns_rows": {
                        "kind": "query_row_count_bounds",
                        "query": {
                            "version": 1,
                            "select": [
                                {"expression": {"measure": count_measure}, "as": "row_count"}
                            ],
                            "time": {"temporal_role": temporal_role, "grain": "day"},
                            "limit": 10,
                        },
                        "min_rows": 1,
                    },
                    "starter_amount_columns": {
                        "kind": "query_returns_columns",
                        "query": {
                            "version": 1,
                            "select": [
                                {"expression": {"measure": amount_measure}, "as": "total_amount"}
                            ],
                            "group_by": [dimension_id],
                            "limit": 10,
                        },
                        "columns": [dimension_id, "total_amount"],
                    },
                }
            },
        },
        "text": {
            ".gitignore": "data/*.duckdb\ndata/*.sqlite\ndata/*.sqlite3\n.compiled/\n",
        },
        "csv": (
            f"{primary_key},{time_column},event_type,{amount_column}\n"
            "1,2026-01-01T09:00:00,starter,100.0\n"
            "2,2026-01-02T09:00:00,follow_up,75.5\n"
        ),
    }


def _project_target(package_id: str, *, output: str, workspace_root: str) -> Path:
    if output:
        return Path(output).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd().resolve()
    if workspace_root and root.exists() and not root.is_dir():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "--workspace-root must be a directory",
            details={"workspace_root": str(root)},
        )
    configs_root = root / "configs" / "semantic_rails"
    if configs_root.is_dir():
        return configs_root / package_id
    return root / package_id


def _validate_project_target(target: Path) -> None:
    if target.is_symlink():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Target project directory must not be a symlink",
            details={"target": str(target)},
        )
    if target.exists() and not target.is_dir():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Target project path must be a directory",
            details={"target": str(target)},
        )


def _reject_symlink_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Refusing to write through symlinks in the target project directory",
                details={"target": str(root), "symlink": str(path)},
            )


def _write_project_text(root: Path, path: Path, content: str) -> None:
    root_resolved = root.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Generated project files must stay inside the target directory",
            details={"target": str(root), "path": str(path)},
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Refusing to overwrite a symlink",
            details={"path": str(path)},
        )
    path.write_text(content, encoding="utf-8")
