"""The REPL home screen: open, create or import a project, or try the sample.

The REPL shows it when it starts at a terminal without a package (no
``--package`` or ``--path``, no ``package.yml`` in the working directory or a
parent, and no local profile), and on the ``home`` command. Nothing opens
implicitly: the bundled sample is one labelled choice.

Create and import write through the Architect services, so they make the same
packages as the Architect MCP, each step in one parse-gated transaction:
:func:`~semantic_rails.architect_service.create_project`, then, for a dbt
project, :mod:`~semantic_rails.dbt_artifacts` and
:meth:`~semantic_rails.architect_service.ArchitectProject.upsert_models`.
"""

from __future__ import annotations

import contextlib
import os
from collections import deque
from pathlib import Path
from typing import Any

from ..architect_scaffold import slug
from ..architect_service import (
    ArchitectProject,
    FirstModel,
    ProjectSpec,
    ProjectWarehouse,
    create_project,
)
from ..cli.common import (
    _EXCLUDED_DISCOVERY_DIRS,
    DEMO_PACKAGE_ID,
    _package_id_from_yaml,
    _package_ref_at,
    _ref_label,
    _repl_capabilities,
    _repl_color,
)
from ..cli.output import _authoring_error_messages
from ..config import list_package_paths
from ..config_validation import PackageReference, resolve_package_reference
from ..dbt_artifacts import dbt_import_models, load_dbt_artifacts
from ..errors import SemanticLayerError
from .backend import Cancelled, Option, PromptBackend, current_backend

MAX_DEPTH = 4
MAX_FOLDERS = 2000
MAX_FOUND = 20
_SKIPPED_DIRS = _EXCLUDED_DISCOVERY_DIRS | {"dbt_packages", "logs", "target", "venv"}
_STAGING_PREFIXES = ("stg_", "int_", "base_")


def run_home(current: PackageReference | None = None) -> PackageReference | None:
    """Ask until a package is chosen; return ``current`` (``None`` at start) on leaving."""

    backend = current_backend()
    if current is None:
        _, color = _repl_capabilities()
        print(_repl_color("No package open.", "1;33", enabled=color))
        print("  There is no package.yml in this folder or its parents, and no default package.")
    while True:
        found = find_packages(Path.cwd().resolve())
        options: list[Option] = [
            ("open", f"Open a project ({len(found)} found here)" if found else "Open a project"),
            ("create", "Create a project"),
            ("import", "Import a dbt project"),
        ]
        if DEMO_PACKAGE_ID in list_package_paths():
            label = f"Try the bundled sample package ({DEMO_PACKAGE_ID}: sample data, not yours)"
            options.append(("sample", label))
        options.append(("leave", f"Back to {_ref_label(current)}" if current else "Quit"))
        try:
            action = backend.choose(
                "What would you like to do?", options, default="open" if found else "create"
            )
        except Cancelled:
            return current
        chosen: PackageReference | None = None
        try:
            if action == "open":
                chosen = _open(backend, found)
            elif action == "create":
                chosen = _create(backend)
            elif action == "import":
                chosen = _import(backend)
            elif action == "sample":
                chosen = resolve_package_reference(package_id=DEMO_PACKAGE_ID)
            else:
                return current
        except (Cancelled, KeyboardInterrupt):
            print("\nCancelled.")
            continue
        except SemanticLayerError as exc:
            print(f"error [{exc.code}]: {exc}")
            continue
        if chosen is not None:
            return chosen


def find_packages(root: Path) -> list[Path]:
    """Semantic Rails package folders at or below ``root``, nearest first.

    The walk is breadth-first and bounded, so starting in a large tree such as a
    home directory stays fast. It goes at most :data:`MAX_DEPTH` folders down,
    never follows symlinks, skips hidden, build and dependency folders, doesn't
    look inside a package, and stops after :data:`MAX_FOLDERS` folders or
    :data:`MAX_FOUND` packages.
    """

    found: list[Path] = []
    queue = deque([(root, 0)])
    visited = 0
    while queue and visited < MAX_FOLDERS and len(found) < MAX_FOUND:
        folder, depth = queue.popleft()
        visited += 1
        if _package_id_from_yaml(folder):
            found.append(folder)
        elif depth < MAX_DEPTH:
            try:
                with os.scandir(folder) as entries:
                    names = sorted(
                        entry.name
                        for entry in entries
                        if entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith(".")
                        and entry.name not in _SKIPPED_DIRS
                    )
            except OSError:
                continue
            queue.extend((folder / name, depth + 1) for name in names)
    return found


def _open(backend: PromptBackend, found: list[Path]) -> PackageReference:
    options: list[Option] = [
        (str(root), f"{_package_id_from_yaml(root)}  {_shown_path(root)}") for root in found
    ]
    options.append(("path", "Enter a folder path"))
    picked = "path"
    if found:
        picked = backend.choose("Open which project?", options, default=options[0][0])
    folder = _resolve(backend.text("Package folder")) if picked == "path" else Path(picked)
    ref = _package_ref_at(folder)
    if ref is None:
        raise SemanticLayerError("INVALID_CONFIG", f"No package.yml in {_shown_path(folder)}")
    return ref


def _create(backend: PromptBackend) -> PackageReference | None:
    target = _new_folder(backend, "my_package")
    print(
        f"Will create {_shown_path(target)}: a starter that runs at once (one model, its "
        "metrics and two sample rows on DuckDB). Add your own tables with `author model`."
    )
    if not backend.confirm("Create it?", default=True):
        return None
    spec = ProjectSpec(package_id=target.name)
    return _opened(create_project(target, spec, workspace_root=target.parent).report, target)


def _import(backend: PromptBackend) -> PackageReference | None:
    cwd = Path.cwd().resolve()
    dbt_root = next((d for d in [cwd, *cwd.parents] if (d / "dbt_project.yml").is_file()), None)
    target_dir = backend.text(
        "dbt target/ folder (after dbt build and dbt docs generate)",
        default=_shown_path(dbt_root / "target") if dbt_root else "",
    )
    dbt = load_dbt_artifacts(_resolve(target_dir))
    if dbt.adapter_type != "duckdb":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"The REPL imports dbt-duckdb projects, not {dbt.adapter_type or 'this adapter'}; "
            "use the Architect MCP's create_project and import_dbt_project",
        )
    models = {key: row for key, row in dbt.relations.items() if row.resource_type == "model"}
    selected = backend.multi_choose(
        "Import which dbt models? (each needs a key in dbt: unique and not_null tests)",
        [(key, f"{row.name}  {row.relation}") for key, row in models.items()],
        defaults=[
            key
            for key, row in models.items()
            if row.primary_key and not row.name.startswith(_STAGING_PREFIXES)
        ],
    )
    if not selected:
        return None
    items, skipped, _ = dbt_import_models(dbt, selected)
    first = next((m for m in items if m["times"] and len(m["primary_key"]) == 1), None)
    if first is None:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "None of the chosen dbt models has both a one-column key in dbt and a time column",
            details={"skipped_models": skipped},
        )
    target = _new_folder(backend, slug(dbt.project_name, fallback="imported"))
    print(f"Will create {_shown_path(target)} with {len(items)} dbt model(s).")
    for row in skipped:
        print(f"  skipping {row['dbt_model']}: {row['reason']}")
    if not backend.confirm("Import them?", default=True):
        return None
    first_model = FirstModel(
        entity=first["entity_key"],
        relation=first["relation"],
        primary_key=first["primary_key"][0],
        time_column=next(iter(first["times"])),
    )
    spec = ProjectSpec(
        target.name, warehouse=ProjectWarehouse(data="external"), first_model=first_model
    )
    created = create_project(target, spec, workspace_root=target.parent)
    report = created.report
    if report.get("ok"):
        report = {"ok": False, "status": "interrupted"}
        try:
            project = ArchitectProject(target, workspace_root=target.parent)
            report = project.upsert_models(items, group="dbt").report
        finally:
            if not report.get("ok"):  # a failed or interrupted import keeps no half-made project
                created.undo()
                for folder in [*sorted(target.rglob("*"), reverse=True), target]:
                    with contextlib.suppress(OSError):
                        folder.rmdir()  # only folders the undo left empty
    ref = _opened(report, target)
    if ref is not None:
        print(
            f"Point your dbt profile's DuckDB path at {_shown_path(target)}/data/{target.name}"
            ".duckdb and run dbt build. The project reads that file and never rebuilds it."
        )
    return ref


def _new_folder(backend: PromptBackend, default: str) -> Path:
    folder = _resolve(backend.text("New project folder", default=default))
    target = folder.parent / slug(folder.name, fallback=default)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{_shown_path(target)} already exists; choose another folder"
        )
    return target


def _opened(report: dict[str, Any], target: Path) -> PackageReference | None:
    if not report.get("ok"):
        print(f"[error] {report.get('status', 'failed')}; the project was not kept.")
        for message in _authoring_error_messages(report)[:5]:
            print(f"  - {message}")
        return None
    print(f"[ok] {_shown_path(target)} is ready and parses.")
    return _package_ref_at(target)


def _resolve(typed: str) -> Path:
    return Path(typed.strip() or ".").expanduser().resolve()


def _shown_path(path: Path) -> str:
    try:
        return f"./{path.relative_to(Path.cwd().resolve()).as_posix()}"
    except ValueError:
        return str(path)
