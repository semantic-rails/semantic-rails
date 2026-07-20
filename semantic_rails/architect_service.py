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
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from .architect_transactions import (
    ProjectFileSnapshot,
    ProjectFileUpdate,
    ProjectTransaction,
    project_revision,
)
from .config_validation import PackageReference, parse_config_report
from .errors import SemanticLayerError

_INVENTORY_KINDS = {
    "model": "models",
    "entity": "entities",
    "dimension": "dimensions",
    "time": "times",
    "measure": "measures",
    "metric": "metrics",
    "segment": "segments",
}


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


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def _yaml_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
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

        if not self._active:
            return {
                "ok": True,
                "status": "already_undone",
                "project_path": str(self.project_path),
                "changed_files": self.changed_files,
            }

        conflicts = [
            snapshot.relative_path
            for snapshot in self._snapshots
            if hashlib.sha256(
                snapshot.path.read_bytes() if snapshot.path.exists() else b""
            ).hexdigest()
            != snapshot.after_digest
        ]
        if conflicts:
            return {
                "ok": False,
                "status": "undo_conflict",
                "project_path": str(self.project_path),
                "changed_files": self.changed_files,
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
            str(self.report.get("workspace_root") or self.project_path.parent)
        ).resolve()
        transaction = ProjectTransaction(
            self.project_path,
            workspace_root=workspace_root,
        )
        current = project_revision(self.project_path)
        try:
            outcome = transaction.apply(
                [
                    ProjectFileUpdate(
                        snapshot.relative_path,
                        snapshot.content if snapshot.existed else None,
                        snapshot.mode,
                    )
                    for snapshot in self._snapshots
                ],
                expected_revision=current,
                idempotency_key=f"internal-undo-{uuid.uuid4()}",
                intent={
                    "operation": "undo",
                    "changed_files": self.changed_files,
                    "source_revision": self.report.get("revision", ""),
                },
                validate_after=False,
                allow_internal_paths=any(
                    snapshot.relative_path.startswith(".architect/archive/")
                    for snapshot in self._snapshots
                ),
                success_status="undone",
                metadata={
                    "operation": "undo",
                    "changed_files": self.changed_files,
                },
            )
        except SemanticLayerError as exc:
            if exc.code != "CONFIG_CONFLICT":
                raise
            return {
                "ok": False,
                "status": "undo_conflict",
                "project_path": str(self.project_path),
                "changed_files": self.changed_files,
                "conflicting_files": self.changed_files,
                "errors": [
                    {
                        "code": exc.code,
                        "message": str(exc),
                        "details": dict(exc.details or {}),
                    }
                ],
            }
        self._active = False
        parse, _ = parse_config_report(PackageReference(source_path=str(self.project_path)))
        report = dict(outcome.report)
        report["parse"] = parse
        report["ok"] = bool(parse.get("ok"))
        return report


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
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        keys = _as_list(primary_key)
        if not keys:
            raise SemanticLayerError(
                "INVALID_CONFIG", "primary_key must contain at least one column"
            )

        raw = self._raw_inventory()
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
        documents = self._load_documents(model_path, graph_path)

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
        if existing_entity is None:
            desired_entity.update({"label": _title(entity_slug), "allowed_as_root": True})
        entities[entity_slug] = desired_entity
        graph["entities"] = entities
        graph_doc["graph"] = graph
        graph_changed = desired_entity != existing_spec
        if not graph_changed and graph_path != model_path:
            documents.pop(graph_path)

        source_file = self._relative(existing_model.source_path) if existing_model else ""
        target_file = self._relative(model_path)
        return self._commit(
            documents,
            kind="model",
            key=model_slug,
            existed=existing_model is not None,
            source_file=source_file,
            target_file=target_file,
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=key,
            dry_run=dry_run,
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
            },
            extra={
                "entity": {
                    "key": entity_slug,
                    "existing": existing_entity is not None,
                    "source_file": self._relative(existing_entity.source_path)
                    if existing_entity
                    else "",
                    "target_file": self._relative(graph_path),
                }
            },
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
    ) -> ArchitectMutation:
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
        updates = [
            ProjectFileUpdate(
                self._relative(path),
                yaml.safe_dump(
                    documents[path],
                    sort_keys=False,
                    allow_unicode=False,
                ).encode("utf-8"),
                (path.stat().st_mode & 0o777) if path.exists() else None,
            )
            for path in documents
        ]
        outcome = ProjectTransaction(
            self.project_path,
            workspace_root=self.workspace_root,
        ).apply(
            updates,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            intent={**deepcopy(intent), "expected_revision": expected_revision},
            dry_run=dry_run,
            validate_after=validate_after,
            success_status="upserted",
            metadata=metadata,
        )
        mutation = ArchitectMutation(
            report=outcome.report,
            project_path=self.project_path,
            _snapshots=outcome.snapshots,
            _active=bool(outcome.snapshots),
        )
        return mutation

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
