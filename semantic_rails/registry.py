"""Alias and canonical-name registry for catalog/resolve lookups.

Indexes every semantic object (measure, metric, dimension, segment,
entity, …) by both canonical id and configured aliases, returning
:class:`SemanticObject` records to the catalog and resolve payloads.
Owns the case- and punctuation-insensitive normalization used to make
``resolve("Average Order Value")`` and ``resolve("aov_usd")`` land on
the same canonical id.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import SemanticLayerError
from .schema import PackageConfig


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


@dataclass(frozen=True)
class SemanticObject:
    id: str
    kind: str
    canonical_name: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AliasCandidate:
    id: str
    kind: str
    canonical_name: str
    confidence: float
    rationale: str


class Registry:
    def __init__(self, config: PackageConfig) -> None:
        self.config = config
        self.by_id: dict[str, SemanticObject] = {}
        self.by_kind: dict[str, list[SemanticObject]] = {}
        self.alias_index: dict[str, list[SemanticObject]] = {}
        self._build()

    def _add(self, obj: SemanticObject) -> None:
        self.by_id[obj.id] = obj
        self.by_kind.setdefault(obj.kind, []).append(obj)
        for alias in [obj.id, obj.canonical_name, obj.display_name, *obj.aliases]:
            key = _norm(alias)
            if key:
                self.alias_index.setdefault(key, []).append(obj)

    def _build(self) -> None:
        for entity in self.config.entities:
            payload = asdict(entity)
            self._add(
                SemanticObject(
                    entity.id,
                    "entity",
                    str(payload.get("name") or entity.id),
                    str(
                        payload.get("label")
                        or payload.get("description")
                        or entity.id.split(".")[-1]
                    ),
                    list(entity.aliases),
                    payload,
                )
            )
        for dimension in self.config.dimensions:
            payload = asdict(dimension)
            self._add(
                SemanticObject(
                    dimension.id,
                    "dimension",
                    str(payload.get("name") or dimension.id),
                    str(
                        payload.get("label")
                        or payload.get("description")
                        or dimension.id.split(".")[-1]
                    ),
                    list(dimension.aliases),
                    payload,
                )
            )
        for role in self.config.temporal_roles:
            payload = asdict(role)
            self._add(
                SemanticObject(
                    role.id,
                    "temporal_role",
                    str(payload.get("name") or role.id),
                    str(
                        payload.get("label") or payload.get("description") or role.id.split(".")[-1]
                    ),
                    list(role.aliases),
                    payload,
                )
            )
        for rel in self.config.relationships:
            payload = asdict(rel)
            self._add(
                SemanticObject(
                    rel.id,
                    "relationship",
                    str(payload.get("name") or rel.id),
                    str(
                        payload.get("label") or payload.get("description") or rel.id.split(".")[-1]
                    ),
                    list(rel.aliases),
                    payload,
                )
            )
        for domain in self.config.value_domains:
            payload = asdict(domain)
            self._add(
                SemanticObject(
                    domain.id, "value_domain", domain.id, domain.id.split(".")[-1], [], payload
                )
            )
        for measure in self.config.measures:
            payload = asdict(measure)
            self._add(
                SemanticObject(
                    measure.id,
                    "measure",
                    str(payload.get("name") or measure.id),
                    str(
                        payload.get("label")
                        or payload.get("description")
                        or measure.id.split(".")[-1]
                    ),
                    list(measure.aliases),
                    payload,
                )
            )
        for recipe in self.config.metric_recipes:
            payload = asdict(recipe)
            self._add(
                SemanticObject(
                    recipe.id,
                    "metric",
                    str(payload.get("name") or recipe.id),
                    str(
                        payload.get("label")
                        or payload.get("description")
                        or recipe.id.split(".")[-1]
                    ),
                    list(recipe.aliases),
                    payload,
                )
            )
        for segment in self.config.segments:
            payload = asdict(segment)
            self._add(
                SemanticObject(
                    segment.id,
                    "segment",
                    str(payload.get("name") or segment.id),
                    str(
                        payload.get("label")
                        or payload.get("description")
                        or segment.id.split(".")[-1]
                    ),
                    list(segment.aliases),
                    payload,
                )
            )

    def list_objects(self, kind: str | None = None) -> list[SemanticObject]:
        if kind is None:
            return sorted(self.by_id.values(), key=lambda row: (row.kind, row.id))
        return sorted(self.by_kind.get(kind, []), key=lambda row: row.id)

    def get(self, object_id: str) -> SemanticObject:
        if object_id not in self.by_id:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"Unknown object '{object_id}'",
                details={"object_id": object_id},
            )
        return self.by_id[object_id]

    def resolve(self, term: str, *, kind: str = "") -> dict[str, Any]:
        key = _norm(term)
        candidates = [obj for obj in self.alias_index.get(key, []) if not kind or obj.kind == kind]
        if not candidates:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"No object matched '{term}'",
                details={"term": term, "kind": kind},
            )
        dedup = {obj.id: obj for obj in candidates}
        rows = [
            AliasCandidate(
                id=obj.id,
                kind=obj.kind,
                canonical_name=obj.canonical_name,
                confidence=1.0 if _norm(obj.canonical_name) == key else 0.92,
                rationale="exact canonical match"
                if _norm(obj.canonical_name) == key
                else "alias match",
            )
            for obj in dedup.values()
        ]
        rows.sort(key=lambda row: (-row.confidence, row.kind, row.canonical_name))
        if len(rows) > 1:
            raise SemanticLayerError(
                "AMBIGUOUS_ALIAS",
                f"Alias '{term}' is ambiguous",
                details={"term": term, "kind": kind, "candidates": [asdict(row) for row in rows]},
            )
        return {
            "term": term,
            "kind": kind,
            "selected": asdict(rows[0]),
            "object": asdict(self.get(rows[0].id)),
        }
