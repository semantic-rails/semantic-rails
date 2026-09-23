"""Transport-independent, transactional Semantic Rails authoring services.

The Architect MCP and the local REPL need the same package-editing behavior,
but neither surface should know about the other's transport.  This module owns
raw package inventory, scoped YAML mutations, parse-gated rollback, and
in-session undo.  It deliberately inventories authored YAML rather than the
compiled :mod:`semantic_rails.schema` objects so edits return to the exact file
and mapping key that supplied an existing object.
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
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
from .config import _slug as _engine_slug
from .config_parts.package_loader import _slug as _loader_slug
from .config_validation import PackageReference, parse_config_report
from .errors import SemanticLayerError

_RELATIONSHIP_CARDINALITIES = {
    "many_to_one": "many_to_one",
    "n:1": "many_to_one",
    "one_to_one": "one_to_one",
    "1:1": "one_to_one",
    "one_to_many": "one_to_many",
    "1:n": "one_to_many",
    "many_to_many": "many_to_many",
    "m:n": "many_to_many",
}

_RELATIONSHIP_SAFETY = ("safe", "requires_rewrite", "unsafe")
_FLIPPED_DIRECTION = {"forward": "reverse", "reverse": "forward"}

_CALENDAR_ID = re.compile(r"[a-z0-9_]+")
# Dimension kinds only a calendar entity may carry.
_DATE_KINDS = frozenset({"date", "timestamp", "datetime", "time"})

_REMOVABLE_KINDS = {
    **{
        kind: kind
        for kind in (
            "model",
            "dimension",
            "time",
            "measure",
            "metric",
            "segment",
            "example",
            "test",
        )
    },
    **{
        f"{kind}s": kind
        for kind in ("model", "dimension", "measure", "metric", "segment", "example", "test")
    },
    "times": "time",
    "relationship": "relationship",
    "relationships": "relationship",
}

# What upsert_model(replace=True) keeps: identity, relationships and calendar.
_KEPT_ON_REPLACE = ("id", "entities", "calendar_id")

# Package checks: example questions and package tests.
_CHECKS = ("example", "test")
TEST_KINDS = (
    "query_returns_columns",
    "query_row_count_bounds",
    "query_matches_snapshot",
    "validate_fails_with_code",
    "explain_contains",
    "metric_equals_query",
)
MAX_PREVIEW_ROWS = 200

# What a segment's membership: block holds (config.py reads them from there only).
_MEMBERSHIP_KEYS = frozenset(
    {"where", "metric_filters", "time", "temporal_role_overrides", "path_policy"}
)

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


def _inferred_relationship_id(model_key: str, target: str) -> str:
    """The id the engine gives the relationship a model's entity reference implies."""
    return f"relationship.{_engine_slug(model_key)}_{_engine_slug(target)}"


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

    def _begin(
        self,
        expected_revision: str | None,
        idempotency_key: str | None,
        intent: dict[str, Any],
    ) -> tuple[str, str, ArchitectMutation | None]:
        """The mutation identity, and its earlier result when this call already ran.

        Runs before anything inspects the project, so a retried mutation replays
        its receipt and a stale writer gets ``CONFIG_CONFLICT`` instead of an
        error from checks against a project that has since changed.
        """
        expected, key = self._mutation_identity(expected_revision, idempotency_key)
        outcome = ProjectTransaction(
            self.project_path, workspace_root=self.workspace_root
        ).preflight(
            expected_revision=expected,
            idempotency_key=key,
            intent={**deepcopy(intent), "expected_revision": expected},
        )
        if outcome is None:
            return expected, key, None
        return (
            expected,
            key,
            ArchitectMutation(report=outcome.report, project_path=self.project_path, _active=False),
        )

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
        replace: bool = False,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update a model and its primary graph entity.

        Fields merge into an existing model; ``replace=True`` rewrites it from
        the arguments instead, keeping only its entity references (see
        :meth:`upsert_relationship`) and calendar. ``calendar=True`` makes the
        entity the package calendar for ``calendar_id`` (default
        ``"default"``): ``kind: time``, not a query root. ``calendar=False``
        makes a calendar a regular entity again, once its ``kind: date``
        dimensions are gone; ``None`` leaves it as it is. On a regular model,
        ``calendar_id`` binds its times to that calendar.
        """
        intent = {
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
            "replace": replace,
        }
        expected, key, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
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
            replace=replace,
        )
        if staged["calendar_changed"]:
            self._check_calendars(raw, documents)
        if not staged["graph_changed"] and staged["graph_path"] != staged["model_path"]:
            documents.pop(staged["graph_path"])
        extra: dict[str, Any] = {"entity": staged["entity"]}
        if staged["replaced"]:
            extra["dropped"] = [
                {field: value for field, value in row.items() if field != "spec"}
                for row in staged["dropped"]
            ]
            extra["dropped_fields"] = staged["dropped_fields"]
            extra["impact"] = self._guarded_impact(
                self._updates(documents), staged["dropped"], f"replacing model {model_id!r}"
            )
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
            intent=intent,
            extra=extra,
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
        columns) and optionally ``"to_columns"`` (the target's). A target is an
        entity already in the package or created by this batch; a relation
        resolves to the entity of the model reading it. Each becomes an entry in
        the model's ``entities`` block (``expr`` when the column differs from the
        target's key), which strict packages read as a many-to-one relationship.
        A reference whose target is missing, or which points at a column other
        than the target's key, is reported under ``skipped_references``.
        """
        intent = {"operation": "upsert_models", "models": deepcopy(models), "group": group}
        expected, key, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
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
        relation_entities = {
            str(row.spec.get("relation") or ""): self._primary_entity_for_model(
                row, raw["entities"]
            )
            for row in raw["models"]
        }
        relation_entities.update(
            {
                str(item.get("relation") or ""): fact["entity_key"]
                for item, fact in zip(models, staged, strict=True)
            }
        )
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item, fact in zip(models, staged, strict=True):
            for reference in list(item.get("references") or []):
                target = str(
                    reference.get("entity")
                    or relation_entities.get(str(reference.get("relation") or ""), "")
                )
                columns = _as_list(reference.get("columns"))
                to_columns = _as_list(reference.get("to_columns"))
                target_key = entity_keys.get(target, [])
                reason = ""
                if not target or target not in entity_keys:
                    reason = "the target is not a model in this package or batch"
                elif target == fact["entity_key"]:
                    reason = "a model cannot reference its own entity"
                elif not columns or len(columns) != len(target_key):
                    reason = f"the columns do not match the width of {target}'s key {target_key}"
                elif to_columns and to_columns != target_key:
                    reason = f"it points at {to_columns}, not {target}'s key {target_key}"
                if reason:
                    skipped.append({"model": fact["model"], **reference, "reason": reason})
                    continue
                model = self._staged_model(documents[fact["model_path"]], fact["model"])
                entry = (
                    {}
                    if columns == target_key
                    else {"expr": columns[0] if len(columns) == 1 else columns}
                )
                model["entities"] = {**dict(model.get("entities", {}) or {}), target: entry}
                added.append({"model": fact["model"], "entity": target, "columns": columns})
        if any(fact["calendar_changed"] for fact in staged):
            self._check_calendars(raw, documents)
        graph_paths = {fact["graph_path"] for fact in staged}
        model_paths = {fact["model_path"] for fact in staged}
        if not any(fact["graph_changed"] for fact in staged):
            for path in graph_paths - model_paths:
                documents.pop(path, None)
        dropped = [row for fact in staged for row in fact["dropped"]]
        replaced: dict[str, Any] = {}
        if any(fact["replaced"] for fact in staged):
            replaced = {
                "dropped": [
                    {field: value for field, value in row.items() if field != "spec"}
                    for row in dropped
                ],
                "dropped_fields": {
                    fact["model"]: fact["dropped_fields"]
                    for fact in staged
                    if fact["dropped_fields"]
                },
                "impact": self._guarded_impact(
                    self._updates(documents), dropped, "replacing these models"
                ),
            }
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
            intent=intent,
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
                **replaced,
            },
        )

    @staticmethod
    def _staged_model(doc: dict[str, Any], model_slug: str) -> dict[str, Any]:
        """The live model mapping inside a staged document."""
        if "models" in doc:
            rows = dict(doc["models"])
            return rows[_models_key(rows, model_slug)]
        return doc["model"]

    def upsert_relationship(
        self,
        *,
        from_entity: str,
        to_entity: str,
        columns: list[str],
        to_columns: list[str] | None = None,
        cardinality: str = "",
        name: str = "",
        allowed_directions: list[str] | None = None,
        safety: str = "",
        path_preference: int | None = None,
        label: str = "",
        description: str = "",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Relate two entities through key columns, in the form strict packages use.

        ``columns`` are ``from_entity``'s columns holding ``to_entity``'s key;
        ``to_columns``, when given, must be that key. The columns go in the
        ``entities:`` block of ``from_entity``'s model (with ``expr:`` when
        named differently from the key), which is a safe many-to-one
        relationship on its own. A ``one_to_one`` cardinality, a ``name``,
        ``allowed_directions``, ``safety``, ``path_preference``, ``label`` or
        ``description`` also writes ``graph.relationships.<name>``; an existing
        entry for the same pair is updated in place, keeping what is not
        passed. ``one_to_many`` is recorded from the many side (``to_columns``
        are then the foreign key on ``to_entity``'s model, and directions are
        read from the many side too). ``many_to_many`` needs a bridge model,
        related many-to-one to each side.
        """
        intent = {
            "operation": "upsert_relationship",
            "from_entity": from_entity,
            "to_entity": to_entity,
            "columns": list(columns or []),
            "to_columns": list(to_columns or []),
            "cardinality": cardinality,
            "name": name,
            "allowed_directions": list(allowed_directions or []),
            "safety": safety,
            "path_preference": path_preference,
            "label": label,
            "description": description,
        }
        expected, key, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
        requested = str(cardinality or "").strip().lower()
        kind = _RELATIONSHIP_CARDINALITIES.get(requested) if requested else None
        if requested and kind is None:
            choices = ", ".join(dict.fromkeys(_RELATIONSHIP_CARDINALITIES.values()))
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"cardinality must be one of {choices} (got {cardinality!r})",
            )
        if kind == "many_to_many":
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "a many_to_many relationship needs a bridge model: model the link table, "
                "then relate it many_to_one to each side",
            )
        directions = [str(item).strip().lower() for item in allowed_directions or []]
        if allowed_directions is not None and (
            not directions or not set(directions) <= {"forward", "reverse"}
        ):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "allowed_directions must be forward, reverse or both "
                f"(got {list(allowed_directions)!r})",
            )
        source, target = str(from_entity or "").strip(), str(to_entity or "").strip()
        if "bridge" in {source, target}:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "'bridge' is an option of a model's entities block, not an entity name",
            )
        fk_columns, target_columns = _as_list(columns), _as_list(to_columns)
        fk_param, target_param = "columns", "to_columns"
        if kind == "one_to_many":
            # Recorded from the many side, so the pair and its directions flip.
            source, target = target, source
            fk_columns, target_columns = target_columns, fk_columns
            fk_param, target_param = target_param, fk_param
            directions = [_FLIPPED_DIRECTION[direction] for direction in directions]
            kind = "many_to_one"
        if not fk_columns:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "a one_to_many relationship is recorded on the many side: pass to_columns, "
                "the foreign key on to_entity's model"
                if fk_param == "to_columns"
                else "columns must name the foreign-key columns on from_entity's model",
            )
        if any(not column.strip() for column in [*fk_columns, *target_columns]):
            raise SemanticLayerError("INVALID_CONFIG", "column names must not be blank")
        if path_preference is not None and (
            isinstance(path_preference, bool) or int(path_preference) < 1
        ):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "path_preference must be a positive integer; lower is preferred (default 100)",
            )
        if safety and safety not in _RELATIONSHIP_SAFETY:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"safety must be one of {', '.join(_RELATIONSHIP_SAFETY)} (got {safety!r})",
            )
        raw = self._raw_inventory()
        source_row = self._find_raw(raw["entities"], source)
        target_row = self._find_raw(raw["entities"], target)
        for entity, row in ((source, source_row), (target, target_row)):
            if row is None:
                raise SemanticLayerError(
                    "OBJECT_NOT_FOUND",
                    f"entity {entity!r} is not in this package",
                    details={"entity": entity},
                )
        assert source_row is not None and target_row is not None
        if source == target:
            raise SemanticLayerError("INVALID_CONFIG", "an entity cannot reference itself")
        target_key = _as_list(target_row.spec.get("key"))
        if target_columns and target_columns != target_key:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{target} is related through its key {target_key}, "
                f"not {target_param} {target_columns}",
                details={"entity": target, "key": target_key},
            )
        if len(fk_columns) != len(target_key):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{fk_param} {fk_columns} do not match the width of {target}'s key {target_key}",
                details={fk_param: fk_columns, "key": target_key},
            )
        model_row = self._entity_model(raw, source_row)
        if model_row is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"entity {source!r} has no model to hold the relationship",
                details={"entity": source},
            )

        documents = self._load_documents(model_row.source_path, source_row.source_path)
        graph = dict(documents[source_row.source_path].get("graph", {}) or {})
        relationships = dict(graph.get("relationships", {}) or {})
        loaded_ids = self._relationship_ids(raw, relationships)
        inferred_id = _inferred_relationship_id(model_row.key, target)
        pair_entries = [
            str(entry_name)
            for entry_name, entry in relationships.items()
            if isinstance(entry, dict)
            and sorted(_as_list(entry.get("entities"))) == sorted([source, target])
        ]
        if len(pair_entries) > 1:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"graph.relationships has several entries for {source} and {target} "
                f"({', '.join(pair_entries)}); keep one",
                details={"relationships": pair_entries},
            )
        existing_name = pair_entries[0] if pair_entries else ""
        current = dict(relationships.get(existing_name, {}) or {}) if existing_name else {}
        if existing_name:
            declared = _as_list(current.get("entities"))
            if declared != [source, target]:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"graph.relationships.{existing_name} declares this relationship from the "
                    f"{declared[0]} side ({declared}); upsert_relationship records it from the "
                    f"many side ({source} to {target}), so remove that entry first",
                    details={"relationship": existing_name},
                )
            if name and _slug(name, fallback="") != _slug(existing_name, fallback=""):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{source} and {target} are already related as "
                    f"graph.relationships.{existing_name}; pass name={existing_name!r} or "
                    "leave name empty",
                    details={"relationship": existing_name},
                )
            joined_on = _as_list(current.get("target"))
            if joined_on and joined_on != target_key:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"graph.relationships.{existing_name} joins {target} on {joined_on}, not "
                    f"its key {target_key}; upsert_relationship only relates to keys",
                    details={"relationship": existing_name},
                )
            relationship_name = existing_name
            relationship_id = str(
                current.get("id") or f"relationship.{_loader_slug(existing_name)}"
            )
        elif name:
            relationship_name = _slug(name, fallback="relationship")
            relationship_id = f"relationship.{relationship_name}"
        else:
            relationship_name = _slug(f"{model_row.key}_{target}", fallback="relationship")
            relationship_id = inferred_id
        if not existing_name and relationship_name in relationships:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"graph.relationships.{relationship_name} already relates other entities; "
                "choose another name",
                details={"relationship": relationship_name},
            )
        owner_of_id = loaded_ids.get(relationship_id)
        if owner_of_id is not None and owner_of_id != (source, target):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{relationship_id} already names the relationship from {owner_of_id[0]} to "
                f"{owner_of_id[1]}; choose another name",
                details={"relationship": relationship_id},
            )
        options: dict[str, Any] = {
            field_name: value
            for field_name, value in (
                ("allowed_directions", directions or None),
                ("safety", safety or None),
                ("path_preference", path_preference),
                ("label", label or None),
                ("description", description or None),
            )
            if value is not None
        }
        effective = kind or _RELATIONSHIP_CARDINALITIES.get(
            str(current.get("cardinality", "") or "").strip().lower(), "many_to_one"
        )

        model_doc = documents[model_row.source_path]
        model, wrapper = self._model_for_update(model_doc, model_row, model_slug=model_row.key)
        model_entities = dict(model.get("entities", {}) or {})
        # A model whose `bridge` option is off infers no joins from its
        # entities block, so the relationship exists only as an explicit entry.
        with_override = bool(
            existing_name
            or options
            or effective != "many_to_one"
            or relationship_id != inferred_id
            or not model_entities.get("bridge", True)
        )
        previous = model_entities.get(target)
        entry = {
            field_name: value
            for field_name, value in dict(previous if isinstance(previous, dict) else {}).items()
            if field_name != "expr"
        }
        if fk_columns != target_key:
            entry["expr"] = fk_columns[0] if len(fk_columns) == 1 else fk_columns
        # The loader takes the first entity in the block as the model's own.
        model["entities"] = {
            source: model_entities.get(source) or {},
            **{
                entity: value
                for entity, value in model_entities.items()
                if entity not in {source, target}
            },
            target: entry,
        }
        self._store_model(model_doc, wrapper, model_row.key, model)
        if with_override:
            spec = {**current, "entities": [source, target], **options}
            if kind or "cardinality" not in current:
                spec["cardinality"] = effective
            if "via" in current:
                spec["via"] = list(fk_columns)
            if "id" not in current and relationship_id != (
                f"relationship.{_loader_slug(relationship_name)}"
            ):
                spec["id"] = relationship_id
            graph["relationships"] = {**relationships, relationship_name: spec}
            documents[source_row.source_path]["graph"] = graph
        elif source_row.source_path != model_row.source_path:
            documents.pop(source_row.source_path)
        return self._commit(
            documents,
            kind="relationship",
            key=relationship_name,
            existed=previous is not None or bool(existing_name),
            source_file=self._relative(model_row.source_path),
            target_file=self._relative(model_row.source_path),
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=key,
            dry_run=dry_run,
            intent=intent,
            extra={
                "relationship": {
                    "name": relationship_name,
                    "id": relationship_id,
                    "from_entity": source,
                    "to_entity": target,
                    "columns": fk_columns,
                    "to_columns": target_key,
                    "cardinality": effective,
                    "model": model_row.key,
                    "override": with_override,
                }
            },
        )

    def _relationship_ids(
        self, raw: dict[str, list[_RawObject]], relationships: dict[str, Any]
    ) -> dict[str, tuple[str, str]]:
        """The id of every relationship the package loads, mapped to its entity pair."""
        ids: dict[str, tuple[str, str]] = {}
        overridden: set[tuple[str, str]] = set()
        for entry_name, entry in relationships.items():
            pair = _as_list(dict(entry).get("entities")) if isinstance(entry, dict) else []
            if len(pair) != 2:
                continue
            overridden.add((pair[0], pair[1]))
            entry_id = str(dict(entry).get("id") or f"relationship.{_loader_slug(entry_name)}")
            ids[entry_id] = (pair[0], pair[1])
        for model_row in raw["models"]:
            references = dict(model_row.spec.get("entities", {}) or {})
            if not references.get("bridge", True):
                continue  # infers no joins
            primary = self._primary_entity_for_model(model_row, raw["entities"])
            for target in references:
                if target in {primary, "bridge"} or (primary, str(target)) in overridden:
                    continue
                ids[_inferred_relationship_id(model_row.key, str(target))] = (
                    primary,
                    str(target),
                )
        return ids

    def _entity_model(
        self, raw: dict[str, list[_RawObject]], entity: _RawObject
    ) -> _RawObject | None:
        """The model an entity lives on, resolved the way the loader does."""
        bound = str(entity.spec.get("model") or "")
        if bound:
            return self._find_raw(raw["models"], bound)
        owner = next(
            (
                row
                for row in raw["models"]
                if self._primary_entity_for_model(row, raw["entities"]) == entity.key
            ),
            None,
        )
        return owner or self._find_raw(raw["models"], entity.key)

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
        replace: bool = False,
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
        if entity_slug == "bridge":
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "'bridge' is an option of a model's entities block, not an entity name; "
                "choose another entity_key",
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
        if existing_model is not None and (
            str(existing_model.spec.get("kind") or "model").strip().lower() == "fact"
        ):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{model_slug} is a fact model; upsert_model manages entity models",
                details={"model": model_slug},
            )
        dropped: list[dict[str, Any]] = []
        dropped_fields: list[str] = []
        if replace and existing_model is not None:
            kept = {"dimensions": dimensions, "times": times, "measures": measures}
            dropped = [
                self._removed_row(row, model=model_slug)
                for plural, fields in kept.items()
                for row in raw[plural]
                if row.model_key == model_slug and row.key not in dict(fields or {})
            ]
            given = {"relation", "description"} | {
                field
                for field, value in (
                    ("label", label),
                    ("dimensions", dimensions),
                    ("times", times),
                    ("measures", measures),
                    ("joins", joins),
                )
                if value
            }
            dropped_fields = sorted(set(model) - set(_KEPT_ON_REPLACE) - given)
            model = {field: model[field] for field in _KEPT_ON_REPLACE if field in model}
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
        was_calendar = (
            str((existing_entity.spec if existing_entity else {}).get("kind") or "").strip().lower()
            == "time"
        )
        requested_calendar = str(calendar_id or "").strip()
        if requested_calendar and not _CALENDAR_ID.fullmatch(requested_calendar):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "calendar_id must be lowercase letters, digits and underscores "
                f"(got {calendar_id!r})",
            )
        if calendar:
            model["calendar_id"] = requested_calendar or str(model.get("calendar_id") or "default")
        elif calendar is False and was_calendar:
            dated = sorted(
                str(key)
                for key, spec in {
                    **dict(model.get("dimensions", {}) or {}),
                    **dict(dimensions or {}),
                }.items()
                if str(dict(spec or {}).get("kind", "") or "").strip().lower() in _DATE_KINDS
            )
            if dated:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"only a calendar may have kind: date dimensions; remove "
                    f"{', '.join(dated)} before making {entity_slug} a regular entity",
                    details={"dimensions": dated},
                )
            model.pop("calendar_id", None)
        if requested_calendar and calendar is not True:
            # A regular model's calendar_id binds its times to that calendar.
            model["calendar_id"] = requested_calendar
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
            "dropped": dropped,
            "dropped_fields": dropped_fields,
            "replaced": bool(replace and existing_model is not None),
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

    def _check_calendars(
        self, raw: dict[str, list[_RawObject]], documents: dict[Path, dict[str, Any]]
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
        file_name: str = "",
        replace: bool = False,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update a metric; an existing one stays in its file.

        A new metric goes in ``metrics/<file_name>`` when given (several
        metrics can share it), else ``metrics/<group>/<metric_key>.yml``.
        ``spec`` merges into an existing metric unless ``replace``.
        """
        intent = {
            "operation": "upsert_metric",
            "metric_key": metric_key,
            "spec": spec,
            "group": group,
            "file_name": file_name,
            "replace": replace,
        }
        expected, idempotency, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
        key = str(metric_key or "").strip()
        if not key:
            raise SemanticLayerError("INVALID_CONFIG", "metric_key is required")
        raw = self._raw_inventory()
        existing = self._find_raw(raw["metrics"], key)
        path = (
            existing.source_path
            if existing is not None
            else self._target_path(
                f"metrics/{_slug(file_name.rsplit('.', 1)[0], fallback='core')}.yml"
                if file_name
                else f"metrics/{_slug(group, fallback='core')}/{_slug(key, fallback='metric')}.yml"
            )
        )
        documents = self._load_documents(path)
        doc = documents[path]
        current = dict(existing.spec if existing is not None else {})
        merged = (
            deepcopy(dict(spec or {})) if replace else {**current, **deepcopy(dict(spec or {}))}
        )
        self._store_mapping_object(doc, existing, wrapper="metrics", key=key, spec=merged)
        extra: dict[str, Any] = {}
        if replace and existing is not None:
            # A rewritten metric can break what builds on it (segments, derived metrics).
            extra["impact"] = self._guarded_impact(
                self._updates(documents), [], f"replacing metric {key!r}"
            )
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
            intent=intent,
            extra=extra,
        )

    def upsert_segment(
        self,
        *,
        segment_key: str,
        spec: dict[str, Any],
        file_name: str = "core.yml",
        replace: bool = False,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update a segment in ``segments/<file_name>``.

        ``spec`` takes ``entity``, ``basis_metric``, ``label``, ``description``,
        ``preview_dimensions`` and ``membership``: ``where`` and/or
        ``metric_filters``, optionally ``time``, ``temporal_role_overrides``
        and ``path_policy``. Membership fields outside ``membership:`` are
        refused (the engine would ignore them and select the whole
        population), as is a segment the engine cannot validate. ``spec``
        merges into an existing segment unless ``replace``.
        """
        intent = {
            "operation": "upsert_segment",
            "segment_key": segment_key,
            "spec": spec,
            "file_name": file_name,
            "replace": replace,
        }
        expected, idempotency, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
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
        merged = (
            deepcopy(dict(spec or {})) if replace else {**current, **deepcopy(dict(spec or {}))}
        )
        _check_segment_shape(key, merged)
        self._store_mapping_object(doc, existing, wrapper="segments", key=key, spec=merged)
        self._check_segment(self._updates(documents), key, merged)
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
            intent=intent,
        )

    def _check_segment(
        self, updates: list[ProjectFileUpdate], key: str, spec: dict[str, Any]
    ) -> None:
        """Refuse a segment the engine cannot validate once the change is applied."""
        from .runtime import Runtime

        transaction = ProjectTransaction(self.project_path, workspace_root=self.workspace_root)
        with transaction.virtual_project(updates) as proposed:
            parse, _ = parse_config_report(PackageReference(source_path=str(proposed)))
            if not parse.get("ok"):
                return  # the transaction's parse gate reports it
            runtime = Runtime.from_path(str(proposed))
            try:
                report = runtime.segment_validate(
                    _canonical_id("segment", key, spec, namespace=self._namespace())
                )
            finally:
                runtime.close()
        if not report.get("ok"):
            error = dict((report.get("errors") or [{}])[0] or {})
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"segment {key!r} does not validate ({error.get('code', '')}: "
                f"{error.get('message', '')})",
                details={"segment": key, "error": error},
            )

    def _namespace(self) -> str:
        package = dict(_yaml_load(self._target_path("package.yml")).get("package", {}) or {})
        return str(package.get("namespace") or package.get("id") or self.project_path.name)

    def remove_object(
        self,
        *,
        kind: str,
        key: str,
        model: str = "",
        reason: str = "",
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Remove one object and keep its YAML under ``.architect/archive/``.

        ``kind`` is model, dimension, time, measure, metric, segment,
        relationship, example or test; ``model`` picks the model when a
        dimension, time or measure key exists on several. A relationship is
        named as :meth:`upsert_relationship` reports it (``orders_customer``).
        Removing a model also removes its entity, other models' references to
        that entity and ``graph.relationships`` entries naming it.

        A removal that would stop a measure, metric or segment from compiling
        is refused, naming them: remove or change those first. The report's
        ``impact`` lists examples and tests it breaks (``broken``), authored
        files that still mention a removed id (``references``) and the
        behaviour changes against the current package (``behavior``).
        """
        singular = _REMOVABLE_KINDS.get(str(kind or "").strip().lower())
        if singular is None:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"kind must be one of {', '.join(sorted(set(_REMOVABLE_KINDS.values())))}",
                details={"kind": kind},
            )
        name = str(key or "").strip()
        if not name:
            raise SemanticLayerError("INVALID_CONFIG", "key is required")
        intent = {
            "operation": "remove_object",
            "kind": singular,
            "key": name,
            "model": model,
            "reason": reason,
        }
        expected, idempotency, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
        raw = self._raw_inventory()
        documents: dict[Path, dict[str, Any]] = {}
        deletions: list[Path] = []
        removed: list[dict[str, Any]] = []
        if singular in {"metric", "segment", "example", "test"}:
            self._remove_mapping_object(raw, singular, name, documents, deletions, removed)
        elif singular in {"dimension", "time", "measure"}:
            self._remove_model_field(raw, singular, name, str(model or ""), documents, removed)
        elif singular == "relationship":
            self._remove_relationship(raw, name, documents, removed)
        else:
            self._remove_model(raw, name, documents, deletions, removed)
        archive_path = (
            f".architect/archive/{hashlib.sha256(idempotency.encode('utf-8')).hexdigest()[:16]}"
            "/removed.yml"
        )
        archive = ProjectFileUpdate(
            archive_path,
            yaml.safe_dump(
                {"reason": reason, "removed": removed}, sort_keys=False, allow_unicode=False
            ).encode("utf-8"),
        )
        impact = self._guarded_impact(
            self._updates(documents, tuple(deletions)), removed, f"removing {singular} {name!r}"
        )
        return self._commit(
            documents,
            kind=singular,
            key=name,
            existed=True,
            source_file=str(removed[0]["source_file"]),
            target_file="",
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=idempotency,
            dry_run=dry_run,
            intent=intent,
            extra={
                "removed": [
                    {field_name: value for field_name, value in row.items() if field_name != "spec"}
                    for row in removed
                ],
                "archived_to": archive_path,
                "impact": impact,
            },
            deletions=tuple(deletions),
            extra_updates=(archive,),
            operation="removed",
            success_status="removed",
        )

    def _document(self, documents: dict[Path, dict[str, Any]], path: Path) -> dict[str, Any]:
        """The staged document for ``path``, loaded on first use."""
        if path not in documents:
            documents.update(self._load_documents(path))
        return documents[path]

    def _removed_row(self, row: _RawObject, **fields: Any) -> dict[str, Any]:
        return {
            "kind": row.kind,
            "key": row.key,
            "id": row.object_id,
            "source_file": self._relative(row.source_path),
            **fields,
            "spec": deepcopy(row.spec),
        }

    def _definitions(self, singular: str, key: str) -> list[tuple[Path, str, str]]:
        """Every place an object named ``key`` is defined, including shadowed ones.

        Returns ``(path, wrapper, mapping key)``; the wrapper is the plural
        mapping (``metrics:``), or the singular one for a one-object file.
        """
        plural = f"{singular}s"
        package_path = self._target_path("package.yml")
        root_path = self._target_path(f"{plural}.yml")
        sources = [package_path]
        if singular != "model" and root_path.exists():
            sources.append(root_path)
        sources.extend(self._yaml_files(self._target_path(plural)))
        found: list[tuple[Path, str, str]] = []
        for path in sources:
            doc = _yaml_load(path)
            if isinstance(doc.get(plural), dict):
                for mapping_key, spec in doc[plural].items():
                    if (
                        _defined_as(singular, str(mapping_key), dict(spec or {}), single=False)
                        == key
                    ):
                        found.append((path, plural, str(mapping_key)))
            elif doc and path not in (package_path, root_path):
                spec = dict(doc.get(singular, doc) or {})
                if _defined_as(singular, path.stem, spec, single=True) == key:
                    found.append((path, singular, ""))
        return found

    def _drop_definitions(
        self,
        singular: str,
        definitions: list[tuple[Path, str, str]],
        documents: dict[Path, dict[str, Any]],
        deletions: list[Path],
        removed: list[dict[str, Any]],
        in_use: _RawObject,
        **fields: Any,
    ) -> None:
        """Remove every definition, keeping each one's YAML for the archive."""
        package_path = self._target_path("package.yml")
        root_path = self._target_path(f"{singular}s.yml")
        for path, wrapper, mapping_key in definitions:
            if wrapper == singular:
                spec = dict(_yaml_load(path).get(singular, _yaml_load(path)) or {})
                deletions.append(path)
            else:
                doc = self._document(documents, path)
                rows = dict(doc.get(wrapper, {}) or {})
                spec = dict(rows.pop(mapping_key, None) or {})
                if rows or path == root_path:
                    doc[wrapper] = rows  # an emptied root file still masks package.yml
                elif path == package_path or len(doc) > 1:
                    doc.pop(wrapper, None)
                else:
                    deletions.append(path)
            removed.append(
                {
                    "kind": singular,
                    "key": in_use.key,
                    "id": in_use.object_id,
                    "source_file": self._relative(path),
                    **fields,
                    **({} if path == in_use.source_path else {"shadowed": True}),
                    "spec": spec,
                }
            )

    def _remove_mapping_object(
        self,
        raw: dict[str, list[_RawObject]],
        singular: str,
        name: str,
        documents: dict[Path, dict[str, Any]],
        deletions: list[Path],
        removed: list[dict[str, Any]],
    ) -> None:
        row = self._find_raw(raw[f"{singular}s"], name)
        if row is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"{singular} {name!r} is not in this package",
                details={"kind": singular, "key": name},
            )
        self._drop_definitions(
            singular,
            self._definitions(singular, name),
            documents,
            deletions,
            removed,
            row,
            # Examples and tests have no public id to look for elsewhere.
            **({"id": ""} if singular in _CHECKS else {}),
        )

    def _remove_model_field(
        self,
        raw: dict[str, list[_RawObject]],
        singular: str,
        name: str,
        model: str,
        documents: dict[Path, dict[str, Any]],
        removed: list[dict[str, Any]],
    ) -> None:
        plural = _INVENTORY_KINDS[singular]
        rows = [
            row for row in raw[plural] if row.key == name and (not model or row.model_key == model)
        ]
        if not rows:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"{singular} {name!r} is not on "
                + (f"model {model!r}" if model else "any model in this package"),
                details={"kind": singular, "key": name, "model": model},
            )
        if len(rows) > 1:
            models = sorted(row.model_key for row in rows)
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{singular} {name!r} is on several models ({', '.join(models)}); pass model",
                details={"kind": singular, "key": name, "models": models},
            )
        row = rows[0]
        owner = self._find_raw(raw["models"], row.model_key)
        assert owner is not None
        doc = self._document(documents, owner.source_path)
        spec, wrapper = self._model_for_update(doc, owner, model_slug=owner.key)
        fields = dict(spec.get(plural, {}) or {})
        fields.pop(name, None)
        if fields:
            spec[plural] = fields
        else:
            spec.pop(plural, None)
        self._store_model(doc, wrapper, owner.key, spec)
        removed.append(self._removed_row(row, model=owner.key))

    def _graph_document(
        self, raw: dict[str, list[_RawObject]], documents: dict[Path, dict[str, Any]]
    ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        """The graph's file, its staged document, and a copy of its graph block."""
        path = raw["entities"][0].source_path if raw["entities"] else self._target_path("graph.yml")
        doc = self._document(documents, path)
        return path, doc, dict(doc.get("graph", {}) or {})

    def _remove_relationship(
        self,
        raw: dict[str, list[_RawObject]],
        name: str,
        documents: dict[Path, dict[str, Any]],
        removed: list[dict[str, Any]],
    ) -> None:
        graph_path, graph_doc, graph = self._graph_document(raw, documents)
        relationships = dict(graph.get("relationships", {}) or {})
        wanted = {_slug(name, fallback=""), _slug(name.removeprefix("relationship."), fallback="")}
        pair: list[str] = []
        for entry_name, entry in relationships.items():
            declared = _as_list(dict(entry).get("entities")) if isinstance(entry, dict) else []
            entry_id = str(dict(entry).get("id") or "") if isinstance(entry, dict) else ""
            if len(declared) == 2 and (
                str(entry_name) == name
                or _slug(str(entry_name), fallback="") in wanted
                or entry_id == name
            ):
                pair = declared
        if not pair:
            for model_row in raw["models"]:
                references = dict(model_row.spec.get("entities", {}) or {})
                if not references.get("bridge", True):
                    continue  # infers no joins, so holds no relationships
                primary = self._primary_entity_for_model(model_row, raw["entities"])
                for target in references:
                    if target in {primary, "bridge"}:
                        continue
                    inferred = _inferred_relationship_id(model_row.key, str(target))
                    if (
                        name == inferred
                        or _slug(f"{model_row.key}_{target}", fallback="") in wanted
                    ):
                        pair = [primary, str(target)]
        if not pair:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"relationship {name!r} is not in this package",
                details={"kind": "relationship", "key": name},
            )
        source, target = pair
        self._drop_relationship_entries(graph_path, graph_doc, graph, {(source, target)}, removed)
        source_row = self._find_raw(raw["entities"], source)
        owner = self._entity_model(raw, source_row) if source_row is not None else None
        if owner is not None and target in dict(owner.spec.get("entities", {}) or {}):
            self._drop_entity_references(documents, owner, {target}, removed)

    def _drop_relationship_entries(
        self,
        graph_path: Path,
        graph_doc: dict[str, Any],
        graph: dict[str, Any],
        pairs: set[tuple[str, str]],
        removed: list[dict[str, Any]],
        *,
        entities: frozenset[str] = frozenset(),
    ) -> None:
        """Drop graph.relationships entries for ``pairs`` or naming ``entities``."""
        relationships = dict(graph.get("relationships", {}) or {})
        kept = {}
        for entry_name, entry in relationships.items():
            pair = _as_list(dict(entry).get("entities")) if isinstance(entry, dict) else []
            if (len(pair) == 2 and tuple(pair) in pairs) or set(pair) & entities:
                removed.append(
                    {
                        "kind": "relationship",
                        "key": str(entry_name),
                        "id": str(
                            dict(entry).get("id") or f"relationship.{_loader_slug(entry_name)}"
                        ),
                        "source_file": self._relative(graph_path),
                        "entry": f"graph.relationships.{entry_name}",
                        "spec": deepcopy(entry),
                    }
                )
                continue
            kept[entry_name] = entry
        if kept != relationships:
            if kept:
                graph["relationships"] = kept
            else:
                graph.pop("relationships", None)
            graph_doc["graph"] = graph

    def _drop_entity_references(
        self,
        documents: dict[Path, dict[str, Any]],
        owner: _RawObject,
        targets: set[str],
        removed: list[dict[str, Any]],
    ) -> None:
        """Drop ``owner``'s references to ``targets`` from its entities block."""
        doc = self._document(documents, owner.source_path)
        spec, wrapper = self._model_for_update(doc, owner, model_slug=owner.key)
        references = dict(spec.get("entities", {}) or {})
        for target in sorted(targets & set(references)):
            removed.append(
                {
                    "kind": "relationship",
                    "key": _slug(f"{owner.key}_{target}", fallback="relationship"),
                    "id": _inferred_relationship_id(owner.key, target),
                    "source_file": self._relative(owner.source_path),
                    "model": owner.key,
                    "spec": {target: deepcopy(references.pop(target))},
                }
            )
        spec["entities"] = references
        self._store_model(doc, wrapper, owner.key, spec)

    def _remove_model(
        self,
        raw: dict[str, list[_RawObject]],
        name: str,
        documents: dict[Path, dict[str, Any]],
        deletions: list[Path],
        removed: list[dict[str, Any]],
    ) -> None:
        owner = self._find_raw(raw["models"], name)
        if owner is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"model {name!r} is not in this package",
                details={"kind": "model", "key": name},
            )
        self._drop_definitions(
            "model", self._definitions("model", owner.key), documents, deletions, removed, owner
        )
        for plural in ("dimensions", "times", "measures"):
            removed.extend(
                self._removed_row(row, model=owner.key)
                for row in raw[plural]
                if row.model_key == owner.key
            )
        own = dict(owner.spec.get("entities", {}) or {})
        primary = self._primary_entity_for_model(owner, raw["entities"])
        if own.get("bridge", True):
            removed.extend(
                {
                    "kind": "relationship",
                    "key": _slug(f"{owner.key}_{target}", fallback="relationship"),
                    "id": _inferred_relationship_id(owner.key, str(target)),
                    "source_file": self._relative(owner.source_path),
                    "model": owner.key,
                    "spec": {target: deepcopy(own[target])},
                }
                for target in own
                if target not in {primary, "bridge"}
            )
        entities = {
            row.key
            for row in raw["entities"]
            if (bound := self._entity_model(raw, row)) is not None and bound.key == owner.key
        }
        graph_path, graph_doc, graph = self._graph_document(raw, documents)
        graph_entities = dict(graph.get("entities", {}) or {})
        for entity in sorted(entities):
            row = self._find_raw(raw["entities"], entity)
            assert row is not None
            removed.append(self._removed_row(row))
            graph_entities.pop(entity, None)
        graph["entities"] = graph_entities
        graph_doc["graph"] = graph
        self._drop_relationship_entries(
            graph_path, graph_doc, graph, set(), removed, entities=frozenset(entities)
        )
        for other in raw["models"]:
            if other.key != owner.key and entities & set(
                dict(other.spec.get("entities", {}) or {})
            ):
                self._drop_entity_references(documents, other, entities, removed)

    def _guarded_impact(
        self, updates: list[ProjectFileUpdate], removed: list[dict[str, Any]], change: str
    ) -> dict[str, Any]:
        """:meth:`_change_impact`, refusing a change that breaks a definition."""
        impact = self._change_impact(updates, removed)
        broken = [
            row for row in impact["broken"] or [] if row["kind"] in {"measure", "metric", "segment"}
        ]
        if broken:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{change} breaks "
                + ", ".join(f"{row['kind']} {row['id']}" for row in broken)
                + "; remove or change those first",
                details={"broken": broken, "impact": impact},
            )
        return impact

    def _change_impact(
        self, updates: list[ProjectFileUpdate], removed: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """What a change breaks, where removed ids are still mentioned, and its impact.

        ``broken`` lists measures, metrics, segments, examples and tests that
        compile now but not after the change (or fail differently);
        ``references`` lists authored files still naming a removed id;
        ``behavior`` is the package impact report against the current package.
        """
        from .package_tools import impact_report

        transaction = ProjectTransaction(self.project_path, workspace_root=self.workspace_root)
        patterns = {
            object_id: re.compile(rf"(?<![\w.]){re.escape(object_id)}(?:__\w+)?(?![\w.])")
            for object_id in sorted({str(row["id"]) for row in removed if row.get("id")})
        }
        references = []
        for relative, content in sorted(transaction.proposed_files(updates).items()):
            text = content.decode("utf-8", errors="replace")
            found = [object_id for object_id, pattern in patterns.items() if pattern.search(text)]
            if found:
                references.append({"file": relative, "ids": found})
        impact: dict[str, Any] = {
            "broken": None,
            "rerouted": None,
            "references": references,
            "behavior": {},
        }
        with transaction.virtual_project(updates) as proposed:
            parse, _ = parse_config_report(PackageReference(source_path=str(proposed)))
            if not parse.get("ok"):
                impact["behavior"] = {"ok": False, "errors": list(parse.get("errors", []) or [])}
                return impact
            try:
                before = _cached_sweep(str(self.project_path), transaction.current_revision())
                after = _compile_sweep(proposed)
            except Exception as exc:  # the engine could not load one side at all
                impact["behavior"] = {
                    "ok": False,
                    "errors": [
                        {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
                    ],
                }
                return impact
            impact["broken"] = [
                {"id": object_id, "kind": probe.kind, "code": probe.code, "message": probe.message}
                for object_id, probe in sorted(after.items())
                if _newly_broken(object_id, probe, before.get(object_id))
            ]
            # Still compiles, but to other SQL: a join that now takes another path.
            impact["rerouted"] = [
                {"id": object_id, "kind": probe.kind}
                for object_id, probe in sorted(after.items())
                if not probe.code
                and object_id in before
                and not before[object_id].code
                and before[object_id].sql != probe.sql
            ]
            report = impact_report(
                PackageReference(source_path=str(proposed)), compare_path=str(self.project_path)
            )
            impact["behavior"] = {
                "ok": True,
                "summary": report["summary"],
                "changes": report["changes"],
                "impacted_metrics": report["impact"]["impacted_metrics"],
                "risk": report["impact"]["risk"],
            }
        return impact

    def upsert_example(
        self,
        *,
        example_key: str,
        spec: dict[str, Any],
        file_name: str = "core.yml",
        replace: bool = False,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update an example question in ``examples/<file_name>``.

        ``spec`` takes ``question``, ``query`` and optionally ``expected_shape``
        (``columns``, ``min_rows``, ``max_rows``); it merges into an existing
        example unless ``replace``. The query must compile against the package.
        """
        return self._upsert_check(
            "example",
            example_key,
            spec,
            file_name=file_name,
            replace=replace,
            validate_after=validate_after,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    def upsert_test(
        self,
        *,
        test_key: str,
        spec: dict[str, Any],
        file_name: str = "core.yml",
        replace: bool = False,
        capture_snapshot: bool = False,
        validate_after: bool = True,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> ArchitectMutation:
        """Create or update a package test in ``tests/<file_name>``.

        ``spec.kind`` is one of ``TEST_KINDS``, with that kind's fields (see
        ``docs/ARCHITECT_MCP.md``); it merges into an existing test unless
        ``replace``. Queries must compile, and a ``validate_fails_with_code``
        query must fail with its ``code``. ``capture_snapshot`` runs a
        ``query_matches_snapshot`` query against the warehouse and writes its
        rows (at most 200) as ``expected_rows``.
        """
        return self._upsert_check(
            "test",
            test_key,
            spec,
            file_name=file_name,
            replace=replace,
            capture_snapshot=capture_snapshot,
            validate_after=validate_after,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    def preview_query(self, query: dict[str, Any], *, max_rows: int = 20) -> dict[str, Any]:
        """Run ``query`` against the package's warehouse and return at most ``max_rows`` rows.

        ``max_rows`` is capped at 200. Values come back JSON-ready: numbers as
        int or float, dates and times as ISO strings. Like runtime validation,
        this may build a seeded DuckDB database.
        """
        cap = max(1, min(int(max_rows), MAX_PREVIEW_ROWS))
        payload = _capped(query, cap)
        rows, columns = self._query_rows(payload)
        return {
            "ok": True,
            "project_path": str(self.project_path),
            "columns": columns,
            "rows": [{key: _json_value(value) for key, value in row.items()} for row in rows[:cap]],
            "row_count": min(len(rows), cap),
            "truncated": len(rows) > cap,
        }

    def _query_rows(self, query: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        from .runtime import Runtime

        runtime = Runtime.from_path(str(self.project_path))
        try:
            result = runtime.query(query)
        finally:
            runtime.close()
        rows = [dict(row) for row in result["rows"]]
        columns = list(rows[0]) if rows else list(result.get("output_columns", []) or [])
        return rows, [
            str(column.get("field") or column.get("name") or column)
            if isinstance(column, dict)
            else str(column)
            for column in columns
        ]

    def _upsert_check(
        self,
        kind: str,
        key: str,
        spec: dict[str, Any],
        *,
        file_name: str,
        replace: bool,
        capture_snapshot: bool = False,
        validate_after: bool,
        expected_revision: str | None,
        idempotency_key: str | None,
        dry_run: bool,
    ) -> ArchitectMutation:
        """Upsert an example or a package test after checking its queries."""
        intent = {
            "operation": f"upsert_{kind}",
            f"{kind}_key": key,
            "spec": spec,
            "file_name": file_name,
            "replace": replace,
            **({"capture_snapshot": True} if capture_snapshot else {}),
        }
        expected, idempotency, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
        name = str(key or "").strip()
        if not name:
            raise SemanticLayerError("INVALID_CONFIG", f"{kind}_key is required")
        plural = f"{kind}s"
        raw = self._raw_inventory()
        existing = self._find_raw(raw[plural], name)
        path = (
            existing.source_path
            if existing is not None
            else self._target_path(
                f"{plural}/{_slug(str(file_name).rsplit('.', 1)[0], fallback='core')}.yml"
            )
        )
        documents = self._load_documents(path)
        current = dict(existing.spec if existing is not None else {})
        merged = (
            deepcopy(dict(spec or {})) if replace else {**current, **deepcopy(dict(spec or {}))}
        )
        if capture_snapshot:
            if merged.get("kind") != "query_matches_snapshot":
                raise SemanticLayerError(
                    "INVALID_CONFIG", "capture_snapshot applies to kind: query_matches_snapshot"
                )
            query = merged.get("query")
            if not isinstance(query, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG", f"test {name!r}: query must be a query object"
                )
            rows, _ = self._query_rows(_capped(query, MAX_PREVIEW_ROWS))
            if len(rows) > MAX_PREVIEW_ROWS:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"the query returns more than {MAX_PREVIEW_ROWS} rows; a snapshot keeps at "
                    "most that many, so add a limit",
                )
            merged["expected_rows"] = _snapshot_rows(rows)
        self._check_queries(kind, name, merged)
        self._store_mapping_object(documents[path], existing, wrapper=plural, key=name, spec=merged)
        return self._commit(
            documents,
            kind=kind,
            key=name,
            existed=existing is not None,
            source_file=self._relative(existing.source_path) if existing else "",
            target_file=self._relative(path),
            validate_after=validate_after,
            expected_revision=expected,
            idempotency_key=idempotency,
            dry_run=dry_run,
            intent=intent,
        )

    def _check_queries(self, kind: str, name: str, spec: dict[str, Any]) -> None:
        """Refuse an example or test whose queries the package cannot answer."""
        from .runtime import Runtime

        test_kind = str(spec.get("kind", "") or "") if kind == "test" else ""
        if kind == "test" and test_kind not in TEST_KINDS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"test kind must be one of {', '.join(TEST_KINDS)} (got {test_kind!r})",
                details={"test": name},
            )
        problems = _check_fields(test_kind, spec)
        if problems:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{kind} {name!r}: " + "; ".join(problems),
                details={kind: name, "problems": problems},
            )
        queries = (
            {
                # The runner reads metric_query when the key is there, else query.
                "metric_query": spec["metric_query"]
                if "metric_query" in spec
                else spec.get("query"),
                "expected_query": spec.get("expected_query"),
            }
            if test_kind == "metric_equals_query"
            else {"query": spec.get("query")}
        )
        checked: dict[str, dict[str, Any]] = {}
        for field_name, query in queries.items():
            if not isinstance(query, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG", f"{kind} {name!r}: {field_name} must be a query object"
                )
            checked[field_name] = query
        runtime = Runtime.from_path(str(self.project_path))
        try:
            if test_kind == "validate_fails_with_code":
                result = runtime.validate(dict(spec["query"]))
                code = str((result.get("errors") or [{}])[0].get("code", "") or "")
                if result.get("ok") or code != str(spec["code"]):
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"test {name!r} expects the query to fail with {spec['code']}, but it "
                        + (f"fails with {code}" if code else "is valid"),
                        details={"test": name, "expected": spec["code"], "actual": code},
                    )
                return
            for field_name, query in checked.items():
                try:
                    runtime.compile(deepcopy(query))
                except Exception as exc:  # an engine crash refuses the query too
                    code = getattr(exc, "code", type(exc).__name__)
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"{kind} {name!r}: the {field_name.replace('_', ' ')} does not compile "
                        f"({code}: {exc})",
                        details={kind: name, "error": {"code": code, "message": str(exc)}},
                    ) from exc
        finally:
            runtime.close()

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

        source = self._target_path(relative_path)
        source_relative = self._relative(source)
        intent = {
            "operation": "archive_project_file",
            "relative_path": source_relative,
            "reason": reason,
        }
        expected, key, replay = self._begin(expected_revision, idempotency_key, intent)
        if replay is not None:
            return replay
        if not source.exists() or not source.is_file():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "File to archive does not exist",
                details={"relative_path": relative_path},
            )
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
            intent={**intent, "expected_revision": expected},
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
        deletions: tuple[Path, ...] = (),
        extra_updates: tuple[ProjectFileUpdate, ...] = (),
        operation: str = "",
        success_status: str = "upserted",
    ) -> ArchitectMutation:
        operation = operation or ("updated" if existed else "created")
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
            [*self._updates(documents, deletions), *extra_updates],
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            intent={**deepcopy(intent), "expected_revision": expected_revision},
            dry_run=dry_run,
            validate_after=validate_after,
            allow_internal_paths=bool(extra_updates),
            success_status=success_status,
            metadata=metadata,
        )
        mutation = ArchitectMutation(
            report=outcome.report,
            project_path=self.project_path,
            _snapshots=outcome.snapshots,
            _active=bool(outcome.snapshots),
        )
        return mutation

    def _updates(
        self, documents: dict[Path, dict[str, Any]], deletions: tuple[Path, ...] = ()
    ) -> list[ProjectFileUpdate]:
        """File updates for the staged documents that changed, plus deleted files.

        A document whose data is unchanged is not rewritten, so it keeps its
        comments and layout.
        """
        return [
            *(
                ProjectFileUpdate(
                    self._relative(path),
                    yaml.safe_dump(documents[path], sort_keys=False, allow_unicode=False).encode(
                        "utf-8"
                    ),
                    (path.stat().st_mode & 0o777) if path.exists() else None,
                )
                for path in documents
                if path not in deletions
                and (not path.exists() or documents[path] != _yaml_load(path))
            ),
            *(ProjectFileUpdate(self._relative(path), None) for path in deletions),
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
        checks = {
            f"{kind}s": self._mapping_inventory(
                package_doc=package_doc,
                package_path=package_path,
                plural=f"{kind}s",
                singular=kind,
                namespace=namespace,
            )
            for kind in _CHECKS
        }
        return {
            "models": models,
            "entities": entities,
            "dimensions": nested["dimensions"],
            "times": nested["times"],
            "measures": nested["measures"],
            "metrics": metrics,
            "segments": segments,
            **checks,
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
            rows = dict(doc.get("models", {}) or {})
            return dict(rows.get(_models_key(rows, existing.key), {}) or {}), "models"
        return dict(doc.get("model", doc) or {}), "model"

    @staticmethod
    def _store_model(doc: dict[str, Any], wrapper: str, key: str, model: dict[str, Any]) -> None:
        if wrapper == "models":
            models = dict(doc.get("models", {}) or {})
            models[_models_key(models, key)] = model
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


def _models_key(rows: dict[str, Any], model_id: str) -> str:
    """The key a ``models:`` mapping holds a model under (its ``id`` may differ)."""
    for mapping_key, spec in rows.items():
        if str(dict(spec or {}).get("id") or mapping_key) == model_id:
            return str(mapping_key)
    return model_id


def _defined_as(singular: str, fallback: str, spec: dict[str, Any], *, single: bool) -> str:
    """The key an object definition is known by, as the inventory reads it."""
    if singular == "model":
        return str(spec.get("id") or fallback)
    if single:
        return str(spec.get("name") or spec.get("id") or fallback)
    return fallback


def _capped(query: dict[str, Any], cap: int) -> dict[str, Any]:
    """``query`` asking for at most ``cap + 1`` rows, to see whether there are more."""
    payload = deepcopy(dict(query or {}))
    limit = payload.get("limit")
    if limit is None:
        payload["limit"] = cap + 1
    elif isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise SemanticLayerError(
            "INVALID_QUERY", "limit must be a non-negative integer", details={"limit": limit}
        )
    else:
        payload["limit"] = min(limit, cap + 1)
    return payload


def _snapshot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warehouse rows as a snapshot test holds them, checked to match themselves.

    The check is the test runner's own: the rows, written to YAML and read
    back, must compare equal to the warehouse rows.
    """
    from .package_tools import _normalize_rows

    snapshot = [
        {column: _snapshot_value(column, value) for column, value in row.items()} for row in rows
    ]
    reloaded = yaml.safe_load(yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True))
    if _normalize_rows(reloaded or []) != _normalize_rows(rows):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "these rows don't read back from YAML as the same values, so a snapshot of them "
            "could never pass; test them with query_returns_columns or query_row_count_bounds",
        )
    return snapshot


def _snapshot_value(column: str, value: Any) -> Any:
    """A warehouse value as YAML holds it, or a refusal naming the column."""
    if value is None or isinstance(value, (bool, str, int, date)):
        return value
    if isinstance(value, (float, Decimal)) and _finite(value):
        number = Decimal(repr(value)) if isinstance(value, float) else value
        return int(number) if number == number.to_integral_value() else float(value)
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(column, item) for item in value]
    if isinstance(value, dict):
        return {str(key): _snapshot_value(column, item) for key, item in value.items()}
    raise SemanticLayerError(
        "INVALID_CONFIG",
        f"column {column!r} has {type(value).__name__} values, which a YAML snapshot can't hold "
        "exactly; test it with query_returns_columns or query_row_count_bounds",
        details={"column": column, "type": type(value).__name__},
    )


def _finite(value: float | Decimal) -> bool:
    return value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)


def _json_value(value: Any) -> Any:
    """A warehouse value as JSON can hold it (NaN and infinities become null)."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, (float, Decimal)):
        if not _finite(value):
            return None
        number = Decimal(repr(value)) if isinstance(value, float) else value
        return int(number) if number == number.to_integral_value() else float(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _check_fields(test_kind: str, spec: dict[str, Any]) -> list[str]:
    """What is missing or malformed in an example (test_kind "") or a test of that kind."""
    problems: list[str] = []

    def need(field: str, check: Any, expected: str) -> None:
        if field not in spec or spec[field] is None:
            problems.append(f"needs {field}")
        elif not check(spec[field]):
            problems.append(f"{field} must be {expected}")

    def count(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def strings(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def mapping(value: Any) -> bool:
        return isinstance(value, dict)

    if test_kind == "metric_equals_query":
        need("metric_query" if "metric_query" in spec else "query", mapping, "a query object")
        need("expected_query", mapping, "a query object")
    else:
        need("query", mapping, "a query object")
    if test_kind == "":
        if "question" in spec and not text(spec["question"]):
            problems.append("question must be text")
        shape = spec.get("expected_shape")
        if shape is not None and not mapping(shape):
            problems.append("expected_shape must be a mapping")
        elif shape:
            for field in ("min_rows", "max_rows"):
                if field in shape and not count(shape[field]):
                    problems.append(f"expected_shape.{field} must be a non-negative integer")
            if "columns" in shape and not strings(shape["columns"]):
                problems.append("expected_shape.columns must be a list of names")
    elif test_kind == "query_returns_columns":
        need("columns", strings, "a list of names")
    elif test_kind == "query_row_count_bounds":
        bounds = [field for field in ("min_rows", "max_rows") if spec.get(field) is not None]
        if not bounds:
            problems.append("needs min_rows or max_rows")
        problems.extend(
            f"{field} must be a non-negative integer" for field in bounds if not count(spec[field])
        )
        if len(bounds) == 2 and not problems and spec["min_rows"] > spec["max_rows"]:
            problems.append("min_rows must not exceed max_rows")
    elif test_kind == "query_matches_snapshot":
        need(
            "expected_rows",
            lambda rows: isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
            "a list of rows",
        )
    elif test_kind == "validate_fails_with_code":
        need("code", text, "an error code")
    elif test_kind == "explain_contains":
        need("text", text, "text")
    return problems


def _check_segment_shape(key: str, spec: dict[str, Any]) -> None:
    """Refuse segment fields the engine would ignore, and a segment with no membership."""
    from .config_validation import _SEGMENT_KEYS

    misplaced = sorted(set(spec) & _MEMBERSHIP_KEYS)
    if misplaced:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"segment {key!r}: {', '.join(misplaced)} belong under membership:; outside it the "
            "engine ignores them and the segment selects the whole population",
            details={"segment": key, "fields": misplaced},
        )
    unknown = sorted(set(spec) - _SEGMENT_KEYS)
    membership = spec.get("membership")
    if isinstance(membership, dict):
        unknown += [f"membership.{name}" for name in sorted(set(membership) - _MEMBERSHIP_KEYS)]
    if unknown:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"segment {key!r} has unknown fields: {', '.join(unknown)}",
            details={"segment": key, "fields": unknown},
        )
    if not isinstance(membership, dict) or not (
        membership.get("where") or membership.get("metric_filters")
    ):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"segment {key!r} needs membership: where and/or metric_filters",
            details={"segment": key},
        )


def _issue(exc: SemanticLayerError) -> dict[str, Any]:
    return {"code": exc.code, "message": str(exc), "details": dict(exc.details or {})}


@dataclass(frozen=True)
class _Probe:
    """How one measure, metric, segment, example or test compiles."""

    kind: str
    code: str  # empty when it compiles
    message: str = ""
    sql: str = ""  # digest of the rendered SQL when it compiles


# Codes on which validate_project retries a metric probe with a time axis.
_TIME_RETRY_CODES = frozenset({"INVALID_QUERY", "INVALID_TEMPORAL_ROLE", "PREDICATE_GRAIN_UNSAFE"})


def _compile_sweep(project: Path) -> dict[str, _Probe]:
    """Compile everything a package defines or checks, without querying the warehouse.

    Measures and metrics are probed the way ``validate_project`` probes them,
    and a metric with a time axis also over that axis (``<id>@time``);
    segments go through ``segment_validate``; example and test queries are
    compiled as written. Any failure, including an engine crash, is recorded
    rather than raised.
    """
    from .config import package_root_for_source
    from .config_validation import (
        _default_time_spec_for_metric,
        _probe_query_for_measure,
        _probe_query_for_metric,
    )
    from .package_tools import _load_named_entries
    from .runtime import Runtime

    runtime = Runtime.from_path(str(project))
    probes: dict[str, _Probe] = {}

    def check(object_id: str, kind: str, build: Any) -> _Probe:
        try:
            compiled = runtime.compile(build())
        except SemanticLayerError as exc:
            probe = _Probe(kind, exc.code, str(exc))
        except Exception as exc:  # the engine crashed on it; still a result
            probe = _Probe(kind, type(exc).__name__, str(exc))
        else:
            # The same package compiled from another directory may name its
            # own files, so the path is left out of the comparison.
            sql = str(compiled.get("rendered_sql") or "")
            for location in {str(project), str(Path(project).resolve())}:
                sql = sql.replace(location, "<project>")
            probe = _Probe(kind, "", sql=hashlib.sha256(sql.encode("utf-8")).hexdigest())
        probes[object_id] = probe
        return probe

    try:
        config = runtime.snapshot.config
        for measure in config.measures:
            check(measure.id, "measure", lambda measure=measure: _probe_query_for_measure(measure))
        for recipe in config.metric_recipes:
            probe = check(
                recipe.id, "metric", lambda recipe=recipe: _probe_query_for_metric(recipe, runtime)
            )
            if "time" in _probe_query_or_empty(recipe, runtime):
                continue
            timed = check(
                f"{recipe.id}@time",
                "metric",
                lambda recipe=recipe: {
                    **_probe_query_for_metric(recipe, runtime),
                    "time": _default_time_spec_for_metric(recipe, runtime),
                },
            )
            if probe.code in _TIME_RETRY_CODES and not timed.code:
                probes[recipe.id] = timed  # validate_project's retry
        for segment in config.segments:
            try:
                report = runtime.segment_validate(segment.id)
            except Exception as exc:
                probes[segment.id] = _Probe("segment", type(exc).__name__, str(exc))
                continue
            error = dict((report.get("errors") or [{}])[0] or {})
            probes[segment.id] = _Probe(
                "segment",
                "" if report.get("ok") else str(error.get("code", "") or "INVALID"),
                "" if report.get("ok") else str(error.get("message", "")),
            )
        root = Path(package_root_for_source(str(project)))
        for kind in _CHECKS:
            for entry_id, spec in _load_named_entries(
                root / f"{kind}s", plural_key=f"{kind}s", singular_key=kind
            ):
                query = dict(spec or {}).get("query")
                if isinstance(query, dict):
                    check(f"{kind}.{entry_id}", kind, lambda query=query: deepcopy(query))
    finally:
        runtime.close()
    return probes


def _newly_broken(object_id: str, probe: _Probe, before: _Probe | None) -> bool:
    """Whether a probe fails now where it did not, or fails differently.

    A metric's ``@time`` probe is a second look at an existing metric, so it
    only counts when that metric compiled over time before the change.
    """
    if not probe.code:
        return False
    if before is None:
        return not object_id.endswith("@time")
    return probe.code != before.code


def _probe_query_or_empty(recipe: Any, runtime: Any) -> dict[str, Any]:
    from .config_validation import _probe_query_for_metric

    try:
        return dict(_probe_query_for_metric(recipe, runtime))
    except Exception:
        return {}


@functools.lru_cache(maxsize=4)
def _cached_sweep(project: str, revision: str) -> dict[str, _Probe]:
    """The sweep of a project at one revision; previews of one state reuse it."""
    del revision  # part of the cache key only
    return _compile_sweep(Path(project))


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
