"""Transport-independent, transactional Semantic Rails authoring services.

The Architect MCP and the local REPL need the same package-editing behavior,
but neither surface should know about the other's transport.  This module owns
raw package inventory, scoped YAML mutations, parse-gated rollback, and
in-session undo.  It deliberately inventories authored YAML rather than the
compiled :mod:`semantic_rails.schema` objects so edits return to the exact file
and mapping key that supplied an existing object.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from .architect_scaffold import (
    FirstModel,
    ProjectSpec,
    ProjectWarehouse,
    normalized_package_id,
    project_scaffold_files,
    project_setup_questions,
    project_warehouse_options,
    slug,
)
from .architect_transactions import (
    ABSENT_PROJECT_REVISION,
    ProjectFileSnapshot,
    ProjectFileUpdate,
    ProjectTransaction,
    project_revision,
)
from .config_validation import PackageReference, parse_config_report
from .dialects import connection_option_errors, warehouse_connector
from .errors import SemanticLayerError
from .yaml_loader import safe_load as yaml_safe_load

_INVENTORY_KINDS = {
    "model": "models",
    "entity": "entities",
    "dimension": "dimensions",
    "time": "times",
    "measure": "measures",
    "metric": "metrics",
    "segment": "segments",
}
_CALENDAR_ID = re.compile(r"[a-z0-9_]+")


def _slug(value: str, *, fallback: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "")).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or fallback


def _title(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value or "").replace("_", " ").split())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _foreign_key_entry(columns: list[str], target_key: list[str]) -> dict[str, Any]:
    """A model ``entities`` entry: ``columns`` hold the target entity's ``target_key``."""
    return {} if columns == target_key else {"expr": columns[0] if len(columns) == 1 else columns}


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def _yaml_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml_safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Authoring file '{path}' must contain a YAML mapping",
            details={"path": str(path)},
        )
    return dict(payload)


@dataclass
class ArchitectMutation:
    """One applied authoring change plus enough state for a safe in-session undo."""

    report: dict[str, Any]
    project_path: Path
    _snapshots: tuple[ProjectFileSnapshot, ...] = field(default_factory=tuple, repr=False)
    _active: bool = field(default=True, repr=False)

    @property
    def changed_files(self) -> list[str]:
        return [snapshot.relative_path for snapshot in self._snapshots]

    def undo(self) -> dict[str, Any]:
        """Restore the pre-mutation files unless a later edit changed them."""

        return self.undo_together([self])

    @staticmethod
    def undo_together(parts: list[ArchitectMutation]) -> dict[str, Any]:
        """Restore consecutive mutations in one project transaction or none at all.

        The earliest snapshot supplies each file's original bytes; the latest
        snapshot supplies the digest that must still be present. This also
        handles two changes to the same file without a second undo journal.
        """

        if not parts:
            raise ValueError("Undo needs at least one authoring mutation")
        project_path = parts[0].project_path
        if any(part.project_path != project_path for part in parts):
            raise ValueError("Cannot undo mutations from different projects together")
        # A successful no-op has no snapshots and is inactive from creation;
        # only a prior undo of a real change can conflict with another active part.
        changed_parts = [part for part in parts if part._snapshots]
        first: dict[str, ProjectFileSnapshot] = {}
        last: dict[str, ProjectFileSnapshot] = {}
        chain_conflicts: set[str] = set()
        empty_digest = hashlib.sha256(b"").hexdigest()
        for part in changed_parts:
            for snapshot in part._snapshots:
                previous = last.get(snapshot.relative_path)
                if previous is not None and (
                    previous.after_digest == empty_digest
                    or previous.after_digest != hashlib.sha256(snapshot.content or b"").hexdigest()
                ):
                    # A snapshot records resulting bytes but not resulting
                    # existence. An empty digest is ambiguous, so fail closed.
                    chain_conflicts.add(snapshot.relative_path)
                first.setdefault(snapshot.relative_path, snapshot)
                last[snapshot.relative_path] = snapshot
        changed_files = sorted(first)
        if all(not part._active for part in changed_parts):
            return {
                "ok": True,
                "status": "already_undone",
                "project_path": str(project_path),
                "changed_files": changed_files,
            }

        # Capture the revision before probing digests: a concurrent edit after
        # this point also makes ProjectTransaction reject the whole restore.
        current = project_revision(project_path)
        conflicts = [
            snapshot.relative_path
            for snapshot in last.values()
            if hashlib.sha256(
                snapshot.path.read_bytes() if snapshot.path.exists() else b""
            ).hexdigest()
            != snapshot.after_digest
        ]
        conflicts = sorted(set(conflicts) | chain_conflicts)
        if any(not part._active for part in changed_parts):
            conflicts = sorted(set([*conflicts, *changed_files]))
        if conflicts:
            return {
                "ok": False,
                "status": "undo_conflict",
                "project_path": str(project_path),
                "changed_files": changed_files,
                "conflicting_files": conflicts,
                "errors": [
                    {
                        "code": "CONFIG_CONFLICT",
                        "message": (
                            "Undo was not applied because a changed file was edited after "
                            "this authoring mutation."
                        ),
                        "details": {"conflicting_files": conflicts},
                    }
                ],
            }

        workspace_root = Path(
            str(parts[-1].report.get("workspace_root") or project_path.parent)
        ).resolve()
        transaction = ProjectTransaction(
            project_path,
            workspace_root=workspace_root,
        )
        try:
            outcome = transaction.apply(
                [
                    ProjectFileUpdate(
                        snapshot.relative_path,
                        snapshot.content if snapshot.existed else None,
                        snapshot.mode,
                    )
                    for snapshot in first.values()
                ],
                expected_revision=current,
                idempotency_key=f"internal-undo-{uuid.uuid4()}",
                intent={
                    "operation": "undo",
                    "changed_files": changed_files,
                    "source_revision": parts[-1].report.get("revision", ""),
                },
                validate_after=False,
                allow_internal_paths=any(
                    snapshot.relative_path.startswith(".architect/archive/")
                    for snapshot in first.values()
                ),
                success_status="undone",
                metadata={
                    "operation": "undo",
                    "changed_files": changed_files,
                },
            )
        except SemanticLayerError as exc:
            if exc.code != "CONFIG_CONFLICT":
                raise
            return {
                "ok": False,
                "status": "undo_conflict",
                "project_path": str(project_path),
                "changed_files": changed_files,
                "conflicting_files": changed_files,
                "errors": [
                    {
                        "code": exc.code,
                        "message": str(exc),
                        "details": dict(exc.details or {}),
                    }
                ],
            }
        for part in parts:
            part._active = False
        report = dict(outcome.report)
        if not (project_path / "package.yml").exists():
            # Undoing create_project removes the package: nothing is left to parse.
            report["ok"] = True
            return report
        parse, _ = parse_config_report(PackageReference(source_path=str(project_path)))
        report["parse"] = parse
        report["ok"] = bool(parse.get("ok"))
        return report


__all__ = [
    "ArchitectMutation",
    "ArchitectProject",
    "FirstModel",
    "ProjectSpec",
    "ProjectWarehouse",
    "create_project",
    "project_setup_questions",
    "project_warehouse_options",
]


def create_project(
    project_path: str | os.PathLike[str],
    spec: ProjectSpec,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    expected_revision: str = ABSENT_PROJECT_REVISION,
    idempotency_key: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> ArchitectMutation:
    """Create a new package from ``spec`` in one parse-gated transaction.

    ``project_path`` is resolved against ``workspace_root`` (default: the
    current directory) and must stay inside it and end with the package id.
    A non-empty directory is replaced only with ``overwrite``; the transaction
    still requires ``expected_revision`` to match it. The returned mutation
    carries the transaction report and a one-step ``undo``.
    """
    _validate_project_connection_options(spec)
    root = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd().resolve()
    raw = Path(project_path).expanduser()
    if raw.is_symlink():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Architect project paths may not be symlinks",
            details={"project_path": str(raw)},
        )
    project = (raw if raw.is_absolute() else root / raw).resolve()
    if not _within(project, root):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Architect authoring only writes inside its configured workspace root",
            details={"workspace_root": str(root), "requested_path": str(project)},
        )
    package_id = normalized_package_id(spec)
    if project.name != package_id:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "For schema_version: 1 packages, package_id must match the project directory name",
            details={"package_id": package_id, "project_directory": project.name},
        )
    files = project_scaffold_files(spec)
    transaction = ProjectTransaction(project, workspace_root=root)

    def prepare(current: str) -> tuple[list[ProjectFileUpdate], dict[str, bytes] | None]:
        if not overwrite and current != ABSENT_PROJECT_REVISION:
            # A directory containing only generated warehouse data still has
            # the absent revision. An existing package requires overwrite.
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Project directory already holds a package; pass overwrite=true to replace "
                "its starter files",
                details={"project_path": str(project)},
            )
        retire: list[ProjectFileUpdate] = []
        trusted_scaffold = True
        if overwrite:
            retire, trusted_scaffold = _guard_and_retire_scaffold_model(
                project, root, files, current
            )
        updates = [
            ProjectFileUpdate(
                relative_path,
                content,
                ((project / relative_path).stat().st_mode & 0o777)
                if (project / relative_path).exists()
                else None,
            )
            for relative_path, content in files.items()
        ]
        return [*updates, *retire], files if trusted_scaffold else None

    key = f"internal-{uuid.uuid4()}" if idempotency_key is None else str(idempotency_key)
    outcome = transaction.apply(
        (),
        expected_revision=expected_revision,
        idempotency_key=key,
        intent={
            "operation": "create_project",
            "expected_revision": expected_revision,
            "project_path": str(project),
            "spec": _spec_intent(spec),
            "overwrite": overwrite,
        },
        dry_run=dry_run,
        validate_after=True,
        success_status="created",
        metadata={
            "operation": "created",
            "package_id": package_id,
            "warehouse": spec.warehouse.kind,
            "data": spec.warehouse.data,
            "next_actions": _create_next_actions(spec),
        },
        prepare_updates=prepare,
    )
    return ArchitectMutation(
        report=outcome.report,
        project_path=project,
        _snapshots=outcome.snapshots,
        _active=bool(outcome.snapshots),
    )


def _validate_project_connection_options(spec: ProjectSpec) -> None:
    """Reject invalid options before they become scaffold bytes or a receipt intent."""
    warehouse = str(spec.warehouse.kind or "duckdb").strip().lower()
    connector = warehouse_connector(warehouse)
    options = spec.warehouse.connection_options
    if not isinstance(options, dict) or (
        connector is not None
        and connector.connection_kinds
        and connection_option_errors(warehouse, spec.warehouse.connection_kind, options)
    ):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Connection options contain unsupported or malformed fields; use supported "
            "option names and environment or file references",
            details={"reason": "invalid_connection_options"},
        )


def _guard_and_retire_scaffold_model(
    project: Path, workspace_root: Path, new_files: dict[str, bytes], current_revision: str
) -> tuple[list[ProjectFileUpdate], bool]:
    """Prove replaced scaffold bytes before interpreting the old graph or retiring a model."""
    transaction = ProjectTransaction(project, workspace_root=workspace_root)
    existing: dict[str, bytes] = {}
    changed_targets: set[str] = set()
    for name, content in new_files.items():
        path = project / name
        if path.is_symlink():
            raise SemanticLayerError("INVALID_CONFIG", "Project scaffold files may not be symlinks")
        if path.is_file():
            before = path.read_bytes()
            existing[name] = before
            if before != content:
                changed_targets.add(name)
        else:
            changed_targets.add(name)
    graph_path = project / "graph.yml"
    if current_revision != ABSENT_PROJECT_REVISION and not graph_path.is_file():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "The prior scaffold graph is missing; restore it or remove the old project explicitly",
            details={"reason": "scaffold_provenance_missing", "path": "graph.yml"},
        )
    trusted = (current_revision == ABSENT_PROJECT_REVISION and not existing) or (
        bool(existing) and transaction.matches_creation_files(existing)
    )
    if changed_targets and not trusted:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "An existing scaffold file is modified or has no creation receipt; "
            "archive or remove it explicitly before overwrite",
            details={"reason": "scaffold_source_modified", "paths": sorted(changed_targets)},
        )
    if not graph_path.exists():
        return [], trusted
    if graph_path.is_symlink():
        raise SemanticLayerError("INVALID_CONFIG", "Project graph may not be a symlink")
    try:
        graph = yaml_safe_load(graph_path.read_bytes())
        entities = graph["graph"]["entities"]
        if len(entities) != 1:
            return [], trusted
        entity, graph_row = next(iter(entities.items()))
        entity = str(entity)
        if slug(entity, fallback="") != entity:
            return [], trusted
        model_id = entity if entity.endswith("s") else f"{entity}s"
        if graph_row["model"] != model_id:
            return [], trusted
    except (KeyError, TypeError, AttributeError, yaml.YAMLError):
        return [], trusted
    old_path = f"models/core/{model_id}.yml"
    model_path = project / old_path
    if not model_path.exists():
        return [], trusted
    if model_path.is_symlink():
        raise SemanticLayerError("INVALID_CONFIG", "Project model may not be a symlink")
    if (
        old_path in new_files
        and graph_path.read_bytes() == new_files["graph.yml"]
        and model_path.read_bytes() == new_files[old_path]
    ):
        return [], trusted  # The model is already identical; no retirement is needed.
    if not transaction.matches_creation_files({**existing, old_path: model_path.read_bytes()}):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "The prior scaffold first model is modified or has no creation receipt; "
            "remove or archive it explicitly before overwrite",
            details={"reason": "scaffold_model_modified", "path": old_path},
        )
    return (
        ([], trusted) if old_path in new_files else ([ProjectFileUpdate(old_path, None)], trusted)
    )


def _spec_intent(spec: ProjectSpec) -> dict[str, Any]:
    return {
        "package_id": spec.package_id,
        "description": spec.description,
        "warehouse": vars(spec.warehouse)
        | {"connection_options": dict(spec.warehouse.connection_options)},
        "first_model": vars(spec.first_model),
        "environments": list(spec.environments),
    }


def _create_next_actions(spec: ProjectSpec) -> list[str]:
    actions = []
    if spec.warehouse.kind == "duckdb" and spec.warehouse.data == "external":
        actions.append(
            "Build the database at default_db (for example with dbt build) before runtime "
            "validation; Semantic Rails reads it and never rebuilds it."
        )
    actions.extend(
        [
            "Run validate_project with mode=runtime before trusting queries.",
            "Use upsert_model to add dimensions, measures, or entities, and upsert_relationship "
            "to relate them.",
            "When comparing changes, run impact_project with compare_path or base_ref before "
            "opening a release review.",
        ]
    )
    return actions


@dataclass(frozen=True)
class _RawObject:
    kind: str
    key: str
    object_id: str
    source_path: Path
    wrapper: str
    spec: dict[str, Any]
    model_key: str = ""


class ArchitectProject:
    """A workspace-scoped Semantic Rails package authoring session."""

    def __init__(
        self,
        project_path: str | os.PathLike[str],
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> None:
        raw_project = Path(project_path).expanduser()
        if not raw_project.is_absolute():
            base = Path(workspace_root).expanduser() if workspace_root is not None else Path.cwd()
            raw_project = base / raw_project
        if raw_project.is_symlink():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect project paths may not be symlinks",
                details={"project_path": str(raw_project)},
            )
        project = raw_project.resolve()
        root = (
            Path(workspace_root).expanduser().resolve() if workspace_root is not None else project
        )
        if not _within(project, root):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect authoring only writes inside its configured workspace root",
                details={"workspace_root": str(root), "requested_path": str(project)},
            )
        if not project.is_dir() or not (project / "package.yml").is_file():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Project path must be a Semantic Rails directory package with package.yml",
                details={"project_path": str(project)},
            )
        if (project / "package.yml").is_symlink():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect package.yml may not be a symlink",
                details={"project_path": str(project)},
            )
        self.project_path = project
        self.workspace_root = root

    def revision(self) -> str:
        """Return the deterministic authored-source revision for this project."""

        return project_revision(self.project_path)

    def _mutation_identity(
        self,
        expected_revision: str | None,
        idempotency_key: str | None,
    ) -> tuple[str, str]:
        # Internal REPL callers retain an ergonomic API, but still capture an
        # optimistic base before reading project files and always receive a
        # unique transaction identity.
        expected = self.revision() if expected_revision is None else str(expected_revision)
        key = f"internal-{uuid.uuid4()}" if idempotency_key is None else str(idempotency_key)
        return expected, key

    def inventory(self) -> dict[str, list[dict[str, Any]]]:
        """Return effective raw objects and the YAML source for every supported kind."""

        raw = self._raw_inventory()
        result: dict[str, list[dict[str, Any]]] = {}
        for plural in _INVENTORY_KINDS.values():
            rows = raw.get(plural, [])
            result[plural] = [self._inventory_row(item) for item in rows]
        return result

    def find_similar(
        self,
        kind: str,
        key: str,
        label: str,
        description: str = "",
    ) -> list[dict[str, Any]]:
        """Find conservative same-kind semantic neighbours before an upsert."""

        selected = str(kind or "").strip().lower()
        plural_to_singular = {plural: singular for singular, plural in _INVENTORY_KINDS.items()}
        singular = plural_to_singular.get(selected, selected.rstrip("s"))
        plural = _INVENTORY_KINDS.get(singular)
        if plural is None:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"Unsupported authoring object kind '{kind}'",
                details={"supported_kinds": sorted(_INVENTORY_KINDS)},
            )
        query_label = _normal_text(label or key)
        query_tokens = _semantic_tokens(label or key)
        query_description = _semantic_tokens(description)
        matches: list[dict[str, Any]] = []
        for row in self.inventory()[plural]:
            if str(row.get("key", "")) == str(key):
                continue
            candidate_label = str(row.get("label") or row.get("name") or row.get("key") or "")
            normalized = _normal_text(candidate_label)
            tokens = _semantic_tokens(candidate_label)
            shared = query_tokens & tokens
            union = query_tokens | tokens
            token_score = len(shared) / len(union) if union else 0.0
            sequence_score = SequenceMatcher(None, query_label, normalized).ratio()
            reasons: list[str] = []
            score = 0.0
            if query_label and query_label == normalized:
                score = 1.0
                reasons.append("identical label")
            elif len(shared) >= 2 and token_score >= 0.6:
                score = max(score, token_score)
                reasons.append(f"shared label terms: {', '.join(sorted(shared))}")
            elif sequence_score >= 0.84:
                score = max(score, sequence_score)
                reasons.append("very similar label")
            candidate_description = _semantic_tokens(str(row.get("description", "") or ""))
            description_shared = query_description & candidate_description
            if reasons and description_shared:
                score = min(1.0, score + min(0.08, len(description_shared) * 0.02))
                reasons.append(
                    f"shared description terms: {', '.join(sorted(description_shared)[:3])}"
                )
            if not reasons:
                continue
            matches.append(
                {
                    "kind": singular,
                    "key": row.get("key"),
                    "id": row.get("id"),
                    "label": row.get("label"),
                    "description": row.get("description"),
                    "source_file": row.get("source_file"),
                    "score": round(score, 3),
                    "reasons": reasons,
                    "reason": "; ".join(reasons),
                }
            )
        return sorted(matches, key=lambda row: (-float(row["score"]), str(row["key"])))[:5]

    def upsert_model(
        self,
        *,
        model_id: str,
        entity_key: str,
        relation: str,
        primary_key: list[str],
        dimensions: dict[str, Any] | None = None,
        times: dict[str, Any] | None = None,
        measures: dict[str, Any] | None = None,
        joins: dict[str, Any] | None = None,
        group: str = "core",
        description: str = "",
        label: str = "",
        calendar: bool | None = None,
        calendar_id: str = "",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update a model and its primary graph entity.

        ``calendar=True`` makes the entity the package calendar for
        ``calendar_id`` (default ``"default"``): ``kind: time``, not a query
        root. ``calendar=False`` makes a calendar a regular entity again;
        ``None`` leaves it as it is. On a regular model, ``calendar_id`` binds
        its times to that calendar.
        """
        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        documents: dict[Path, dict[str, Any]] = {}
        raw = self._raw_inventory()
        staged = self._stage_model(
            raw,
            documents,
            model_id=model_id,
            entity_key=entity_key,
            relation=relation,
            primary_key=primary_key,
            dimensions=dimensions,
            times=times,
            measures=measures,
            joins=joins,
            group=group,
            description=description,
            label=label,
            calendar=calendar,
            calendar_id=calendar_id,
        )
        if not staged["graph_changed"] and staged["graph_path"] != staged["model_path"]:
            documents.pop(staged["graph_path"])
        return self._commit(
            documents,
            kind="model",
            key=staged["model"],
            existed=staged["existed"],
            source_file=staged["source_file"],
            target_file=staged["target_file"],
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=key,
            dry_run=dry_run,
            check=partial(self._check_calendars, raw, documents)
            if staged["calendar_changed"]
            else None,
            intent={
                "operation": "upsert_model",
                "model_id": model_id,
                "entity_key": entity_key,
                "relation": relation,
                "primary_key": primary_key,
                "dimensions": dimensions,
                "times": times,
                "measures": measures,
                "joins": joins,
                "group": group,
                "description": description,
                "label": label,
                "calendar": calendar,
                "calendar_id": calendar_id,
            },
            extra={"entity": staged["entity"]},
        )

    def upsert_models(
        self,
        models: list[dict[str, Any]],
        *,
        group: str = "core",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update several models in one parse-gated transaction.

        Each item takes :meth:`upsert_model`'s arguments plus optional
        ``references``: foreign keys, as ``{"entity": <key>}`` or
        ``{"relation": <relation>}`` with ``"columns"`` (this model's key
        columns) and optionally ``"to_columns"`` (the target's). An entity may
        be in the package or created by this batch; a relation resolves only
        when exactly one existing package entity reads it. Each becomes
        an entry in the model's ``entities`` block (``expr`` when the column
        differs from the target's key), which strict packages read as a
        many-to-one relationship. A reference whose target is missing or
        ambiguous, or which points at a column other than the target's key, is
        reported under ``skipped_references``.
        """
        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        if not models:
            raise SemanticLayerError("INVALID_CONFIG", "upsert_models needs at least one model")
        for field_name in ("model_id", "entity_key"):
            names = [_slug(str(item.get(field_name) or ""), fallback="") for item in models]
            repeated = sorted({name for name in names if names.count(name) > 1})
            if repeated:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"each model in one batch needs its own {field_name}; repeated: "
                    + ", ".join(repeated),
                    details={field_name: repeated},
                )
        raw = self._raw_inventory()
        documents: dict[Path, dict[str, Any]] = {}
        staged = [
            self._stage_model(
                raw,
                documents,
                **{"group": group, **{k: v for k, v in item.items() if k != "references"}},
            )
            for item in models
        ]
        entity_keys: dict[str, list[str]] = {
            row.key: _as_list(row.spec.get("key")) for row in raw["entities"]
        }
        for fact in staged:
            graph = dict(documents[fact["graph_path"]].get("graph", {}) or {})
            for name, spec in dict(graph.get("entities", {}) or {}).items():
                entity_keys[str(name)] = _as_list(dict(spec or {}).get("key"))
        readers: dict[str, set[str]] = {}
        for row in raw["models"]:
            entity = self._primary_entity_for_model(row, raw["entities"])
            if row.spec.get("relation") and entity in entity_keys:
                readers.setdefault(str(row.spec["relation"]), set()).add(entity)
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        pending: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], list[str]]]] = {}
        for item, fact in zip(models, staged, strict=True):
            for reference in list(item.get("references") or []):
                candidates = readers.get(str(reference.get("relation") or ""), set())
                target = str(reference.get("entity") or "")
                if not target and len(candidates) == 1:
                    target = next(iter(candidates))
                columns = _as_list(reference.get("columns"))
                to_columns = _as_list(reference.get("to_columns"))
                target_key = entity_keys.get(target, [])
                if not target and len(candidates) > 1:
                    reason = f"the relation has multiple eligible entities: {', '.join(sorted(candidates))}"
                elif not target:
                    reason = (
                        "no existing package entity reads this relation; name the entity for "
                        "targets created in this batch"
                    )
                elif target not in entity_keys:
                    reason = "the target is not a model in this package or batch"
                elif target == fact["entity_key"]:
                    reason = "a model cannot reference its own entity"
                elif not columns or len(columns) != len(target_key):
                    reason = f"the columns do not match the width of {target}'s key {target_key}"
                elif to_columns and to_columns != target_key:
                    reason = f"it points at {to_columns}, not {target}'s key {target_key}"
                else:
                    pending.setdefault((fact["model"], target), []).append(
                        (fact, reference, columns)
                    )
                    continue
                skipped.append({"model": fact["model"], **reference, "reason": reason})
        for (_, target), references in pending.items():
            if len({tuple(columns) for _, _, columns in references}) > 1:
                for fact, reference, _ in sorted(
                    references, key=lambda row: (tuple(row[2]), str(row[1]))
                ):
                    skipped.append(
                        {
                            "model": fact["model"],
                            **reference,
                            "reason": (
                                f"multiple foreign keys to {target} use different columns; "
                                "one entity cannot represent both relationships"
                            ),
                        }
                    )
                continue
            fact, _, columns = references[0]
            model = self._staged_model(documents[fact["model_path"]], fact["model"])
            model["entities"] = {
                **dict(model.get("entities", {}) or {}),
                target: _foreign_key_entry(columns, entity_keys[target]),
            }
            added.append({"model": fact["model"], "entity": target, "columns": columns})
        graph_paths = {fact["graph_path"] for fact in staged}
        model_paths = {fact["model_path"] for fact in staged}
        if not any(fact["graph_changed"] for fact in staged):
            for path in graph_paths - model_paths:
                documents.pop(path, None)
        return self._commit(
            documents,
            kind="models",
            key=",".join(fact["model"] for fact in staged),
            existed=any(fact["existed"] for fact in staged),
            source_file="",
            target_file="",
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=key,
            dry_run=dry_run,
            check=partial(self._check_calendars, raw, documents)
            if any(fact["calendar_changed"] for fact in staged)
            else None,
            intent={"operation": "upsert_models", "models": deepcopy(models), "group": group},
            extra={
                "models": [
                    {
                        "model": fact["model"],
                        "entity": fact["entity_key"],
                        "existed": fact["existed"],
                        "target_file": fact["target_file"],
                    }
                    for fact in staged
                ],
                "references": added,
                "skipped_references": skipped,
            },
        )

    @staticmethod
    def _staged_model(doc: dict[str, Any], model_slug: str) -> dict[str, Any]:
        """The live model mapping inside a staged document."""
        if "models" in doc:
            return dict(doc["models"])[model_slug]
        return doc["model"]

    def _stage_model(
        self,
        raw: dict[str, list[_RawObject]],
        documents: dict[Path, dict[str, Any]],
        *,
        model_id: str,
        entity_key: str,
        relation: str,
        primary_key: list[str],
        dimensions: dict[str, Any] | None = None,
        times: dict[str, Any] | None = None,
        measures: dict[str, Any] | None = None,
        joins: dict[str, Any] | None = None,
        group: str = "core",
        description: str = "",
        label: str = "",
        calendar: bool | None = None,
        calendar_id: str = "",
    ) -> dict[str, Any]:
        """Apply one model upsert to ``documents`` (files load on first use).

        Several models can be staged into one ``documents`` and committed as a
        single transaction; the returned facts describe this model's change.
        """
        keys = _as_list(primary_key)
        if not keys:
            raise SemanticLayerError(
                "INVALID_CONFIG", "primary_key must contain at least one column"
            )
        requested_model = str(model_id or "").strip()
        requested_entity = str(entity_key or "").strip()
        existing_model = self._find_raw(raw["models"], requested_model)
        existing_entity = self._find_raw(raw["entities"], requested_entity)
        model_slug = (
            existing_model.key
            if existing_model is not None
            else _slug(requested_model, fallback="model")
        )
        entity_slug = (
            existing_entity.key
            if existing_entity is not None
            else _slug(requested_entity, fallback=model_slug)
        )
        model_path = (
            existing_model.source_path
            if existing_model is not None
            else self._target_path(f"models/{_slug(group, fallback='core')}/{model_slug}.yml")
        )
        graph_path = (
            existing_entity.source_path
            if existing_entity is not None
            else (
                raw["entities"][0].source_path
                if raw["entities"]
                else self._target_path("graph.yml")
            )
        )
        for path in (model_path, graph_path):
            if path not in documents:
                documents.update(self._load_documents(path))

        model_doc = documents[model_path]
        model, model_wrapper = self._model_for_update(
            model_doc, existing_model, model_slug=model_slug
        )
        model.update(
            {
                "id": model_slug,
                "relation": relation,
                "entities": {**dict(model.get("entities", {}) or {}), entity_slug: {}},
                "description": description
                or model.get("description", f"One row per {_title(entity_slug)}."),
            }
        )
        if label:
            model["label"] = label
        requested_calendar = str(calendar_id or "").strip()
        if requested_calendar and not _CALENDAR_ID.fullmatch(requested_calendar):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "calendar_id must be lowercase letters, digits and underscores "
                f"(got {calendar_id!r})",
            )
        was_calendar = existing_entity is not None and (
            str(existing_entity.spec.get("kind") or "").strip().lower() == "time"
        )
        if calendar:
            model["calendar_id"] = requested_calendar or str(model.get("calendar_id") or "default")
        elif requested_calendar:
            # A regular model's calendar_id binds its times to that calendar.
            model["calendar_id"] = requested_calendar
        elif calendar is False and was_calendar:
            model.pop("calendar_id", None)
        if dimensions is not None:
            model["dimensions"] = {
                **dict(model.get("dimensions", {}) or {}),
                **deepcopy(dict(dimensions or {})),
            }
        if times is not None:
            model["times"] = {
                **dict(model.get("times", {}) or {}),
                **deepcopy(dict(times or {})),
            }
        if measures is not None:
            model["measures"] = {
                **dict(model.get("measures", {}) or {}),
                **deepcopy(dict(measures or {})),
            }
        if joins is not None:
            model["joins"] = {
                **dict(model.get("joins", {}) or {}),
                **deepcopy(dict(joins or {})),
            }
        self._store_model(model_doc, model_wrapper, model_slug, model)

        graph_doc = documents[graph_path]
        graph = dict(graph_doc.get("graph", {}) or {})
        entities = dict(graph.get("entities", {}) or {})
        existing_spec = dict(entities.get(entity_slug, {}) or {})
        # Strict v1 derives entity IDs and names. Preserve explicit authoring
        # that was already present, but never inject the legacy fields.
        desired_entity = {
            **existing_spec,
            "key": existing_spec.get("key") if _as_list(existing_spec.get("key")) == keys else keys,
            "model": model_slug,
        }
        if existing_entity is None and entity_slug not in entities:
            desired_entity.update({"label": _title(entity_slug), "allowed_as_root": True})
        if calendar:
            desired_entity.update({"kind": "time", "allowed_as_root": False})
        elif calendar is False and was_calendar:
            desired_entity.pop("kind", None)
            desired_entity["allowed_as_root"] = True
        entities[entity_slug] = desired_entity
        graph["entities"] = entities
        graph_doc["graph"] = graph
        return {
            "model": model_slug,
            "entity_key": entity_slug,
            "calendar_changed": calendar is not None or bool(requested_calendar),
            "existed": existing_model is not None,
            "model_path": model_path,
            "graph_path": graph_path,
            "graph_changed": desired_entity != existing_spec,
            "source_file": self._relative(existing_model.source_path) if existing_model else "",
            "target_file": self._relative(model_path),
            "entity": {
                "key": entity_slug,
                "existing": existing_entity is not None,
                "source_file": self._relative(existing_entity.source_path)
                if existing_entity
                else "",
                "target_file": self._relative(graph_path),
            },
        }

    @staticmethod
    def _check_calendars(
        raw: dict[str, list[_RawObject]], documents: dict[Path, dict[str, Any]]
    ) -> None:
        """Refuse staged calendars the engine would read ambiguously.

        One calendar entity per calendar_id; a default calendar once there is
        any (a query without a calendar_id fills from it, and would otherwise
        fall back to another calendar's grains); and a regular model may only
        be bound to a calendar that exists.
        """
        models = {row.key: dict(row.spec) for row in raw["models"]}
        entities = {row.key: dict(row.spec) for row in raw["entities"]}
        for path, doc in documents.items():
            graph = doc.get("graph")
            if isinstance(graph, dict) and "entities" in graph:
                entities = {
                    str(key): dict(spec or {})
                    for key, spec in dict(graph.get("entities") or {}).items()
                }
            if isinstance(doc.get("model"), dict):
                models[str(doc["model"].get("id") or path.stem)] = dict(doc["model"])
            for key, spec in dict(doc.get("models", {}) or {}).items():
                models[str(dict(spec or {}).get("id") or key)] = dict(spec or {})
        calendars: dict[str, str] = {}
        for key, spec in entities.items():
            if str(spec.get("kind") or "").strip().lower() != "time":
                continue
            model = models.get(str(spec.get("model") or key), {})
            calendar_id = str(model.get("calendar_id") or "default").strip().lower()
            if calendar_id in calendars:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"calendar_id {calendar_id!r} would belong to both {calendars[calendar_id]!r} "
                    f"and {key!r}; a package has one calendar per calendar_id",
                    details={"calendar_id": calendar_id},
                )
            calendars[calendar_id] = key
        if calendars and "default" not in calendars:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "a package with calendars needs a default one (calendar_id: default); a query "
                "without a calendar_id fills from it",
                details={"calendars": sorted(calendars)},
            )
        calendar_models = {str(entities[key].get("model") or key) for key in calendars.values()}
        for key, spec in models.items():
            bound = str(spec.get("calendar_id") or "").strip().lower()
            if key not in calendar_models and bound not in {"", "default", *calendars}:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"model {key!r} is bound to calendar {bound!r}, which no calendar declares",
                    details={"model": key, "calendar_id": bound},
                )

    def upsert_metric(
        self,
        *,
        metric_key: str,
        spec: dict[str, Any],
        group: str = "core",
        replace: bool = False,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        expected, idempotency = self._mutation_identity(expected_revision, idempotency_key)
        key = str(metric_key or "").strip()
        if not key:
            raise SemanticLayerError("INVALID_CONFIG", "metric_key is required")
        raw = self._raw_inventory()
        existing = self._find_raw(raw["metrics"], key)
        path = (
            existing.source_path
            if existing is not None
            else self._target_path(
                f"metrics/{_slug(group, fallback='core')}/{_slug(key, fallback='metric')}.yml"
            )
        )
        documents = self._load_documents(path)
        doc = documents[path]
        current = dict(existing.spec if existing is not None else {})
        merged = (
            deepcopy(dict(spec or {})) if replace else {**current, **deepcopy(dict(spec or {}))}
        )
        self._store_mapping_object(doc, existing, wrapper="metrics", key=key, spec=merged)
        return self._commit(
            documents,
            kind="metric",
            key=key,
            existed=existing is not None,
            source_file=self._relative(existing.source_path) if existing else "",
            target_file=self._relative(path),
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=idempotency,
            dry_run=dry_run,
            intent={
                "operation": "upsert_metric",
                "metric_key": metric_key,
                "spec": spec,
                "group": group,
                "replace": replace,
            },
        )

    def upsert_segment(
        self,
        *,
        segment_key: str,
        spec: dict[str, Any],
        file_name: str = "core.yml",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        expected, idempotency = self._mutation_identity(expected_revision, idempotency_key)
        key = str(segment_key or "").strip()
        if not key:
            raise SemanticLayerError("INVALID_CONFIG", "segment_key is required")
        raw = self._raw_inventory()
        existing = self._find_raw(raw["segments"], key)
        path = (
            existing.source_path
            if existing is not None
            else self._target_path(
                f"segments/{_slug(file_name.rsplit('.', 1)[0], fallback='core')}.yml"
            )
        )
        documents = self._load_documents(path)
        doc = documents[path]
        current = dict(existing.spec if existing is not None else {})
        merged = {**current, **deepcopy(dict(spec or {}))}
        self._store_mapping_object(doc, existing, wrapper="segments", key=key, spec=merged)
        return self._commit(
            documents,
            kind="segment",
            key=key,
            existed=existing is not None,
            source_file=self._relative(existing.source_path) if existing else "",
            target_file=self._relative(path),
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=idempotency,
            dry_run=dry_run,
            intent={
                "operation": "upsert_segment",
                "segment_key": segment_key,
                "spec": spec,
                "file_name": file_name,
            },
        )

    def upsert_relationship(
        self,
        *,
        from_entity: str,
        to_entity: str,
        columns: list[str],
        cardinality: str = "many_to_one",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Relate ``from_entity`` to ``to_entity`` through foreign-key ``columns``.

        ``columns`` are columns of ``from_entity``'s model that hold
        ``to_entity``'s key, in key order. They go in that model's ``entities``
        block (as ``expr`` when named differently from the key), which strict
        packages read as a many-to-one relationship. ``one_to_one``, or an
        existing ``graph.relationships`` entry for the pair, also records the
        cardinality there. The project is checked under the transaction lock,
        after receipt replay and the revision check.
        """
        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        source, target = str(from_entity or "").strip(), str(to_entity or "").strip()
        foreign_key = [column.strip() for column in _as_list(columns)]
        kind = str(cardinality or "").strip().lower()
        if kind not in {"many_to_one", "one_to_one"}:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"cardinality must be many_to_one or one_to_one (got {cardinality!r}); relate "
                "one_to_many from the many side, and many_to_many through a bridge model",
            )
        if source == target or not foreign_key or not all(foreign_key):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "from_entity and to_entity must differ, and columns must not be blank",
            )

        def prepare(_: str) -> tuple[list[ProjectFileUpdate], None]:
            raw = self._raw_inventory()
            source_row = self._find_raw(raw["entities"], source)
            target_row = self._find_raw(raw["entities"], target)
            model_row = next(
                (
                    row
                    for row in raw["models"]
                    if self._primary_entity_for_model(row, raw["entities"]) == source
                ),
                None,
            )
            if source_row is None or target_row is None or model_row is None:
                missing = [
                    name for name, row in ((source, source_row), (target, target_row)) if not row
                ]
                raise SemanticLayerError(
                    "OBJECT_NOT_FOUND",
                    f"No package entity {missing[0]!r}"
                    if missing
                    else f"Entity {source!r} has no model to hold the foreign key",
                    details={"entities": missing or [source]},
                )
            target_key = _as_list(target_row.spec.get("key"))
            if len(foreign_key) != len(target_key):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"columns {foreign_key} do not match the width of {target}'s key {target_key}",
                    details={"columns": foreign_key, "key": target_key},
                )
            graph_path = source_row.source_path
            documents = self._load_documents(model_row.source_path, graph_path)
            model_doc = documents[model_row.source_path]
            model, wrapper = self._model_for_update(model_doc, model_row, model_slug=model_row.key)
            foreign = dict(dict(model.get("keys") or {}).get("foreign") or {})
            if target in dict(model.get("joins") or {}) or target in foreign:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"Model {model_row.key!r} relates {target} in a legacy joins: or keys.foreign: "
                    "block, which overrides the entities block; edit or remove that entry instead",
                    details={"model": model_row.key},
                )
            entities = dict(model.get("entities", {}) or {})
            # The loader reads the block's first entity as the model's own.
            model["entities"] = {
                source: entities.get(source) or {},
                **{name: spec for name, spec in entities.items() if name not in {source, target}},
                target: _foreign_key_entry(foreign_key, target_key),
            }
            self._store_model(model_doc, wrapper, model_row.key, model)
            graph = dict(documents[graph_path].get("graph", {}) or {})
            relationships = dict(graph.get("relationships", {}) or {})
            existing = next(
                (
                    str(name)
                    for name, entry in relationships.items()
                    if isinstance(entry, dict)
                    and _as_list(entry.get("entities")) == [source, target]
                ),
                "",
            )
            name = existing or f"{model_row.key}_{target}"
            if kind == "one_to_one" or existing:
                if not existing and name in relationships:
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"graph.relationships.{name} already relates other entities",
                        details={"relationship": name},
                    )
                # via/target would override the columns written above.
                entry = {
                    field_name: value
                    for field_name, value in dict(relationships.get(name) or {}).items()
                    if field_name not in {"via", "target"}
                }
                relationships[name] = {**entry, "entities": [source, target], "cardinality": kind}
                documents[graph_path]["graph"] = {**graph, "relationships": relationships}
            elif graph_path != model_row.source_path:
                documents.pop(graph_path)
            return self._file_updates(documents), None

        outcome = ProjectTransaction(self.project_path, workspace_root=self.workspace_root).apply(
            (),
            expected_revision=expected,
            idempotency_key=key,
            intent={
                "operation": "upsert_relationship",
                "expected_revision": expected,
                "from_entity": source,
                "to_entity": target,
                "columns": foreign_key,
                "cardinality": kind,
            },
            dry_run=dry_run,
            validate_after=validate_after,
            success_status="upserted",
            metadata={
                "relationship": {
                    "from_entity": source,
                    "to_entity": target,
                    "columns": foreign_key,
                    "cardinality": kind,
                }
            },
            prepare_updates=prepare,
        )
        return ArchitectMutation(
            report=outcome.report,
            project_path=self.project_path,
            _snapshots=outcome.snapshots,
            _active=bool(outcome.snapshots),
        )

    def write_file(
        self,
        *,
        relative_path: str,
        content: str,
        overwrite: bool = True,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Write one raw UTF-8 project file through the transaction boundary."""

        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        path = self._target_path(relative_path)
        if path.exists() and not overwrite:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Target file exists and overwrite=false",
                details={"relative_path": relative_path},
            )
        relative = self._relative(path)
        outcome = ProjectTransaction(
            self.project_path,
            workspace_root=self.workspace_root,
        ).apply(
            [
                ProjectFileUpdate(
                    relative,
                    str(content).encode("utf-8"),
                    (path.stat().st_mode & 0o777) if path.exists() else None,
                )
            ],
            expected_revision=expected,
            idempotency_key=key,
            intent={
                "operation": "write_project_file",
                "expected_revision": expected,
                "relative_path": relative,
                "content": str(content),
                "overwrite": overwrite,
            },
            dry_run=dry_run,
            validate_after=validate_after,
            success_status="written",
            metadata={
                "operation": "updated" if path.exists() else "created",
                "relative_path": relative,
                "target_file": relative,
            },
        )
        return ArchitectMutation(
            report=outcome.report,
            project_path=self.project_path,
            _snapshots=outcome.snapshots,
            _active=bool(outcome.snapshots),
        )

    def archive_file(
        self,
        *,
        relative_path: str,
        reason: str = "",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Archive one project file through an atomic move-like transaction."""

        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        source = self._target_path(relative_path)
        if not source.exists() or not source.is_file():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "File to archive does not exist",
                details={"relative_path": relative_path},
            )
        source_relative = self._relative(source)
        archive_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        destination_relative = f".architect/archive/{archive_id}/{source_relative}"
        updates = [
            ProjectFileUpdate(
                destination_relative,
                source.read_bytes(),
                source.stat().st_mode & 0o777,
            ),
            ProjectFileUpdate(source_relative, None),
        ]
        if reason:
            updates.append(
                ProjectFileUpdate(
                    f".architect/archive/{archive_id}/ARCHIVE_REASON.txt",
                    str(reason).encode("utf-8"),
                )
            )
        outcome = ProjectTransaction(
            self.project_path,
            workspace_root=self.workspace_root,
        ).apply(
            updates,
            expected_revision=expected,
            idempotency_key=key,
            intent={
                "operation": "archive_project_file",
                "expected_revision": expected,
                "relative_path": source_relative,
                "reason": reason,
            },
            dry_run=dry_run,
            validate_after=validate_after,
            allow_internal_paths=True,
            success_status="archived",
            metadata={
                "operation": "archived",
                "relative_path": source_relative,
                "source_file": source_relative,
                "archived_to": destination_relative,
            },
        )
        return ArchitectMutation(
            report=outcome.report,
            project_path=self.project_path,
            _snapshots=outcome.snapshots,
            _active=bool(outcome.snapshots),
        )

    def _commit(
        self,
        documents: dict[Path, dict[str, Any]],
        *,
        kind: str,
        key: str,
        existed: bool,
        source_file: str,
        target_file: str,
        validate_after: bool,
        expected_revision: str,
        idempotency_key: str,
        dry_run: bool,
        intent: dict[str, Any],
        extra: dict[str, Any] | None = None,
        check: Callable[[], None] | None = None,
    ) -> ArchitectMutation:
        """Apply staged ``documents``; ``check`` runs under the transaction
        lock, after receipt replay and the revision check."""

        def checked(_: str) -> tuple[list[ProjectFileUpdate], None]:
            if check is not None:
                check()
            return [], None

        operation = "updated" if existed else "created"
        metadata: dict[str, Any] = {
            "operation": operation,
            "existing": existed,
            "kind": kind,
            "key": key,
            "project_path": str(self.project_path),
            "workspace_root": str(self.workspace_root),
            "source_file": source_file,
            "target_file": target_file,
            "mutation": {
                "kind": kind,
                "key": key,
                "operation": operation,
                "existing": existed,
                "source_file": source_file,
                "target_file": target_file,
            },
        }
        if extra:
            metadata.update(deepcopy(extra))
        outcome = ProjectTransaction(
            self.project_path,
            workspace_root=self.workspace_root,
        ).apply(
            self._file_updates(documents),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            intent={**deepcopy(intent), "expected_revision": expected_revision},
            dry_run=dry_run,
            validate_after=validate_after,
            success_status="upserted",
            metadata=metadata,
            prepare_updates=None if check is None else checked,
        )
        mutation = ArchitectMutation(
            report=outcome.report,
            project_path=self.project_path,
            _snapshots=outcome.snapshots,
            _active=bool(outcome.snapshots),
        )
        return mutation

    def _file_updates(self, documents: dict[Path, dict[str, Any]]) -> list[ProjectFileUpdate]:
        return [
            ProjectFileUpdate(
                self._relative(path),
                yaml.safe_dump(doc, sort_keys=False, allow_unicode=False).encode("utf-8"),
                (path.stat().st_mode & 0o777) if path.exists() else None,
            )
            for path, doc in documents.items()
        ]

    def _raw_inventory(self) -> dict[str, list[_RawObject]]:
        package_path = self._target_path("package.yml")
        package_doc = _yaml_load(package_path)
        package = dict(package_doc.get("package", {}) or {})
        namespace = str(package.get("namespace") or package.get("id") or self.project_path.name)

        graph_source = package_path
        graph = dict(package_doc.get("graph", {}) or {})
        graph_path = self._target_path("graph.yml")
        if graph_path.exists():
            graph_source = graph_path
            graph = dict(_yaml_load(graph_path).get("graph", {}) or {})
        entities = [
            _RawObject(
                kind="entity",
                key=str(key),
                object_id=_canonical_id("entity", str(key), dict(spec or {}), namespace=namespace),
                source_path=graph_source,
                wrapper="graph.entities",
                spec=dict(spec or {}),
            )
            for key, spec in dict(graph.get("entities", {}) or {}).items()
        ]

        model_map: dict[str, _RawObject] = {}
        for key, spec in dict(package_doc.get("models", {}) or {}).items():
            raw_model = dict(spec or {})
            model_id = str(raw_model.get("id") or key)
            model_map[model_id] = _RawObject(
                kind="model",
                key=model_id,
                object_id=model_id,
                source_path=package_path,
                wrapper="models",
                spec=raw_model,
            )
        models_dir = self._target_path("models")
        for path in self._yaml_files(models_dir):
            doc = _yaml_load(path)
            if "models" in doc:
                rows = dict(doc.get("models", {}) or {})
                wrapper = "models"
            else:
                raw_model = dict(doc.get("model", doc) or {})
                key = str(raw_model.get("id") or path.stem)
                rows = {key: raw_model}
                wrapper = "model"
            for key, spec in rows.items():
                raw_model = dict(spec or {})
                model_id = str(raw_model.get("id") or key)
                model_map[model_id] = _RawObject(
                    kind="model",
                    key=model_id,
                    object_id=model_id,
                    source_path=path,
                    wrapper=wrapper,
                    spec=raw_model,
                )
        models = list(model_map.values())

        nested: dict[str, list[_RawObject]] = {
            "dimensions": [],
            "times": [],
            "measures": [],
        }
        for model_entry in models:
            primary_entity = self._primary_entity_for_model(model_entry, entities)
            for plural, singular in (
                ("dimensions", "dimension"),
                ("times", "time"),
                ("measures", "measure"),
            ):
                for key, spec in dict(model_entry.spec.get(plural, {}) or {}).items():
                    row = dict(spec or {})
                    object_id = _canonical_id(
                        singular,
                        str(key),
                        row,
                        namespace=namespace,
                        entity_key=primary_entity,
                    )
                    nested[plural].append(
                        _RawObject(
                            kind=singular,
                            key=str(key),
                            object_id=object_id,
                            source_path=model_entry.source_path,
                            wrapper=f"{model_entry.wrapper}.{plural}",
                            spec=row,
                            model_key=model_entry.key,
                        )
                    )

        metrics = self._mapping_inventory(
            package_doc=package_doc,
            package_path=package_path,
            plural="metrics",
            singular="metric",
            namespace=namespace,
        )
        segments = self._mapping_inventory(
            package_doc=package_doc,
            package_path=package_path,
            plural="segments",
            singular="segment",
            namespace=namespace,
        )
        return {
            "models": models,
            "entities": entities,
            "dimensions": nested["dimensions"],
            "times": nested["times"],
            "measures": nested["measures"],
            "metrics": metrics,
            "segments": segments,
        }

    def _mapping_inventory(
        self,
        *,
        package_doc: dict[str, Any],
        package_path: Path,
        plural: str,
        singular: str,
        namespace: str,
    ) -> list[_RawObject]:
        rows: dict[str, _RawObject] = {}

        def add(path: Path, key: str, spec: dict[str, Any], wrapper: str) -> None:
            object_id = _canonical_id(singular, str(key), spec, namespace=namespace)
            rows[str(key)] = _RawObject(
                kind=singular,
                key=str(key),
                object_id=object_id,
                source_path=path,
                wrapper=wrapper,
                spec=dict(spec),
            )

        for key, spec in dict(package_doc.get(plural, {}) or {}).items():
            add(package_path, str(key), dict(spec or {}), plural)

        root_path = self._target_path(f"{plural}.yml")
        if root_path.exists():
            rows = {}
            doc = _yaml_load(root_path)
            for key, spec in dict(doc.get(plural, {}) or {}).items():
                add(root_path, str(key), dict(spec or {}), plural)

        directory = self._target_path(plural)
        for path in self._yaml_files(directory):
            doc = _yaml_load(path)
            if plural in doc:
                file_rows = dict(doc.get(plural, {}) or {})
                wrapper = plural
            else:
                spec = dict(doc.get(singular, doc) or {})
                key = str(spec.get("name") or spec.get("id") or path.stem)
                file_rows = {key: spec}
                wrapper = singular
            for key, spec in file_rows.items():
                add(path, str(key), dict(spec or {}), wrapper)
        return list(rows.values())

    def _yaml_files(self, directory: Path) -> list[Path]:
        if not directory.exists():
            return []
        self._assert_safe_target(directory)
        files: list[Path] = []
        for root, dirnames, filenames in os.walk(directory, followlinks=False):
            root_path = Path(root)
            symlink_dirs = [name for name in dirnames if (root_path / name).is_symlink()]
            if symlink_dirs:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect authoring does not traverse symlinked project directories",
                    details={"paths": [str(root_path / name) for name in symlink_dirs]},
                )
            dirnames.sort()
            for filename in sorted(filenames):
                if not filename.endswith((".yml", ".yaml")):
                    continue
                path = root_path / filename
                self._assert_safe_target(path)
                files.append(path)
        return files

    def _inventory_row(self, item: _RawObject) -> dict[str, Any]:
        spec = deepcopy(item.spec)
        return {
            "kind": item.kind,
            "key": item.key,
            "id": item.object_id,
            "label": str(spec.get("label", "") or ""),
            "name": str(spec.get("name", "") or ""),
            "description": str(spec.get("description", "") or ""),
            "model_key": item.model_key,
            "parent": item.model_key,
            "existing": True,
            "source_file": self._relative(item.source_path),
            "relative_path": self._relative(item.source_path),
            "target_file": self._relative(item.source_path),
            "spec": spec,
        }

    @staticmethod
    def _primary_entity_for_model(model: _RawObject, entities: list[_RawObject]) -> str:
        for entity in entities:
            if str(entity.spec.get("model", "") or "") == model.key:
                return entity.key
        explicit = str(model.spec.get("entity", "") or "").strip()
        if explicit:
            return explicit
        exposed = model.spec.get("entities", {}) or {}
        if isinstance(exposed, dict):
            keys = [str(key) for key in exposed if str(key) != "bridge"]
            if keys:
                return keys[0]
        return model.key

    @staticmethod
    def _find_raw(rows: list[_RawObject], key: str) -> _RawObject | None:
        return next((row for row in rows if row.key == key), None)

    def _model_for_update(
        self,
        doc: dict[str, Any],
        existing: _RawObject | None,
        *,
        model_slug: str,
    ) -> tuple[dict[str, Any], str]:
        if existing is None:
            return {}, "model"
        if existing.wrapper == "models":
            return dict(dict(doc.get("models", {}) or {}).get(existing.key, {}) or {}), "models"
        return dict(doc.get("model", doc) or {}), "model"

    @staticmethod
    def _store_model(doc: dict[str, Any], wrapper: str, key: str, model: dict[str, Any]) -> None:
        if wrapper == "models":
            models = dict(doc.get("models", {}) or {})
            models[key] = model
            doc["models"] = models
            return
        doc.clear()
        doc["model"] = model

    @staticmethod
    def _store_mapping_object(
        doc: dict[str, Any],
        existing: _RawObject | None,
        *,
        wrapper: str,
        key: str,
        spec: dict[str, Any],
    ) -> None:
        if existing is not None and existing.wrapper == wrapper.rstrip("s"):
            doc.clear()
            doc[existing.wrapper] = spec
            return
        rows = dict(doc.get(wrapper, {}) or {})
        rows[key] = spec
        doc[wrapper] = rows

    def _load_documents(self, *paths: Path) -> dict[Path, dict[str, Any]]:
        documents: dict[Path, dict[str, Any]] = {}
        for path in paths:
            self._assert_safe_target(path)
            documents.setdefault(path, _yaml_load(path))
        return documents

    def _target_path(self, relative: str) -> Path:
        raw = str(relative or "").strip().lstrip("/")
        if not raw:
            raise SemanticLayerError("INVALID_CONFIG", "A project-relative path is required")
        path = Path(os.path.abspath(self.project_path / raw))
        if not _within(path, self.project_path):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Authoring paths must stay inside the project directory",
                details={"relative_path": relative},
            )
        self._assert_safe_target(path)
        return path

    def _assert_safe_target(self, path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        if not _within(absolute, self.project_path):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Authoring target is outside the project directory",
                details={"path": str(path)},
            )
        current = absolute
        while _within(current, self.project_path):
            if current.is_symlink():
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect authoring refuses symlinked project paths",
                    details={"path": str(current)},
                )
            if current == self.project_path:
                break
            current = current.parent

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.project_path).as_posix()


def _normal_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _semantic_tokens(value: str) -> set[str]:
    return {token for token in _normal_text(value).split() if len(token) >= 2}


def _canonical_id(
    kind: str,
    key: str,
    spec: dict[str, Any],
    *,
    namespace: str,
    entity_key: str = "",
) -> str:
    explicit = str(spec.get("as") or spec.get("id") or "").strip()
    if explicit:
        return explicit
    key_slug = _slug(key, fallback=kind)
    entity_slug = _slug(entity_key, fallback="model")
    if kind == "entity":
        return f"entity.{namespace}_{key_slug}"
    if kind == "dimension":
        return f"dimension.{namespace}_{entity_slug}_{key_slug}"
    if kind == "time":
        return f"temporal_role.{namespace}_{entity_slug}_{key_slug}"
    if kind == "measure":
        return f"measure.{namespace}.{key_slug}"
    if kind == "metric":
        return f"metric.{namespace}.{key_slug}"
    if kind == "segment":
        return f"segment.{namespace}.{key_slug}"
    return key


__all__ = ["ArchitectMutation", "ArchitectProject"]
