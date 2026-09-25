"""Split-layout starter packages for ``init``, ``project new`` and ``setup --interactive``.

The files come from the one project scaffold,
:func:`semantic_rails.architect_scaffold.project_scaffold_files`, so the CLI
creates the same package and object ids as the Architect MCP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..architect_scaffold import (
    FirstModel,
    ProjectSpec,
    normalized_package_id,
    project_scaffold_files,
)
from ..config_validation import PackageReference
from ..errors import SemanticLayerError
from .common import _quote
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
    spec = ProjectSpec(
        package_id=package_id,
        description=description,
        first_model=FirstModel(
            entity=entity,
            relation=relation,
            primary_key=primary_key,
            time_column=time_column,
            amount_column=amount_column,
        ),
    )
    package_slug = normalized_package_id(spec)
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

    changed: list[str] = []
    for relative, content in project_scaffold_files(spec).items():
        _write_project_file(target, target / relative, content)
        changed.append(relative)

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


def _write_project_file(root: Path, path: Path, content: bytes) -> None:
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
    path.write_bytes(content)
