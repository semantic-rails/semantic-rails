"""Immutable catalog text index used by question lookup surfaces.

The index contains only package-derived text.  Request-specific availability
and policy filtering deliberately stay in ``metadata.discover_payload`` so an
index can be shared by every request handled by one runtime generation without
leaking objects across policy contexts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .schema import PackageConfig

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DERIVED_TOKEN_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "by", "with", "is"}
)


def normalize_search_value(value: str) -> str:
    """Return the compact lowercase-alphanumeric form used by discover."""

    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def tokenize_search_value(value: str) -> list[str]:
    """Tokenize with the historical ASCII boundaries used by discover."""

    tokens: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", str(value or "")):
        normalized = normalize_search_value(part)
        if normalized:
            tokens.append(normalized)
    return tokens


def derive_discovery_tokens(name: str, label: str, description: str) -> list[str]:
    """Derive stable search tokens from authored catalog text."""

    tokens: list[str] = []
    seen: set[str] = set()
    for source in (name or "", label or "", description or ""):
        camel_split = _CAMEL_BOUNDARY_RE.sub(" ", str(source))
        for token in re.findall(r"[a-z0-9]+", camel_split.lower()):
            if token in _DERIVED_TOKEN_STOPWORDS or len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


@dataclass(frozen=True)
class SearchTerms:
    """Question text normalized once for a complete catalog score pass."""

    text: str
    lowered: str
    normalized: str
    tokens: tuple[str, ...]

    @classmethod
    def from_text(cls, value: str) -> SearchTerms:
        text = str(value or "")
        return cls(
            text=text,
            lowered=text.lower(),
            normalized=normalize_search_value(text),
            tokens=tuple(tokenize_search_value(text)),
        )


@dataclass(frozen=True)
class CatalogSearchDocument:
    """Pre-normalized immutable fields for one discover candidate."""

    object_id: str
    exact_fields: frozenset[str]
    name_label_fields: tuple[str, ...]
    derived_tokens: tuple[str, ...]
    topic_fields: tuple[str, ...]
    description_fields: tuple[str, ...]
    comparison_fields: tuple[str, ...]
    lowered_title: str

    @classmethod
    def from_fields(
        cls,
        *,
        object_id: str,
        name: str = "",
        label: str = "",
        description: str = "",
        topics: tuple[str, ...] = (),
        comparison_peers: tuple[str, ...] = (),
        clock_variants: tuple[str, ...] = (),
    ) -> CatalogSearchDocument:
        def _normalized(values: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(
                normalized
                for value in values
                if value
                if (normalized := normalize_search_value(value))
            )

        exact_fields = frozenset(_normalized((name, label, object_id)))
        return cls(
            object_id=object_id,
            exact_fields=exact_fields,
            name_label_fields=_normalized((label, name)),
            derived_tokens=_normalized(tuple(derive_discovery_tokens(name, label, description))),
            topic_fields=_normalized(topics),
            description_fields=_normalized((description,)),
            comparison_fields=_normalized((*comparison_peers, *clock_variants)),
            lowered_title=f"{label} {name}".lower(),
        )

    @classmethod
    def from_candidate(cls, candidate: Mapping[str, Any]) -> CatalogSearchDocument:
        """Compile an ad-hoc candidate not present in a runtime index."""

        return cls.from_fields(
            object_id=str(candidate.get("id", "") or ""),
            name=str(candidate.get("name", "") or ""),
            label=str(candidate.get("label", "") or ""),
            description=str(candidate.get("description", "") or ""),
            topics=tuple(str(value) for value in list(candidate.get("topics", []) or [])),
            comparison_peers=tuple(
                str(value) for value in list(candidate.get("comparison_peers", []) or [])
            ),
            clock_variants=tuple(
                str(value) for value in list(candidate.get("clock_variants", []) or [])
            ),
        )


@dataclass(frozen=True)
class CatalogSearchIndex:
    """Compiled text metadata for one immutable ``PackageConfig`` snapshot."""

    documents: Mapping[str, CatalogSearchDocument]
    catalog_tokens: frozenset[str]
    token_doc_freq: Mapping[str, int]

    @classmethod
    def from_config(cls, config: PackageConfig) -> CatalogSearchIndex:
        documents: dict[str, CatalogSearchDocument] = {}
        catalog_tokens: set[str] = set()
        token_doc_freq: dict[str, int] = {}

        collections = (
            ("measure", config.measures),
            ("metric", config.metric_recipes),
            ("dimension", config.dimensions),
            ("segment", config.segments),
            ("entity", config.entities),
            ("temporal_role", config.temporal_roles),
            ("value_domain", config.value_domains),
        )
        frequency_kinds = {"measure", "metric", "dimension", "segment"}
        for kind, collection in collections:
            for row in collection:
                document = _document_from_row(row)
                documents[document.object_id] = document
                row_tokens = _search_tokens_for_row(row)
                catalog_tokens.update(row_tokens)
                catalog_tokens.update(tokenize_search_value(str(getattr(row, "id", "") or "")))
                if kind in frequency_kinds:
                    for token in row_tokens:
                        token_doc_freq[token] = token_doc_freq.get(token, 0) + 1

        dimensions = {row.id: row for row in config.dimensions}
        for domain in config.value_domains:
            dimension = dimensions.get(domain.dimensions[0]) if domain.dimensions else None
            if dimension is None:
                continue
            for value in domain.values:
                object_id = f"{dimension.id}={value.value}"
                documents[object_id] = CatalogSearchDocument.from_fields(
                    object_id=object_id,
                    name=str(value.value),
                    label=str(value.label),
                    description=str(value.description or value.label),
                    topics=tuple(str(topic) for topic in list(dimension.topics or [])),
                )

        catalog_tokens.discard("")
        return cls(
            documents=MappingProxyType(documents),
            catalog_tokens=frozenset(catalog_tokens),
            token_doc_freq=MappingProxyType(token_doc_freq),
        )

    def document(self, object_id: str) -> CatalogSearchDocument | None:
        return self.documents.get(str(object_id or ""))


def _document_from_row(row: Any) -> CatalogSearchDocument:
    return CatalogSearchDocument.from_fields(
        object_id=str(getattr(row, "id", "") or ""),
        name=str(getattr(row, "name", "") or ""),
        label=str(getattr(row, "label", "") or ""),
        description=str(getattr(row, "description", "") or ""),
        topics=tuple(str(value) for value in list(getattr(row, "topics", []) or [])),
        comparison_peers=tuple(
            str(value) for value in list(getattr(row, "comparison_peers", []) or [])
        ),
        clock_variants=tuple(
            str(value) for value in list(getattr(row, "clock_variants", []) or [])
        ),
    )


def _search_tokens_for_row(row: Any) -> set[str]:
    tokens: set[str] = set()
    for attr in ("name", "label", "description"):
        tokens.update(tokenize_search_value(str(getattr(row, attr, "") or "")))
    tokens.update(
        derive_discovery_tokens(
            str(getattr(row, "name", "") or ""),
            str(getattr(row, "label", "") or ""),
            str(getattr(row, "description", "") or ""),
        )
    )
    for topic in list(getattr(row, "topics", []) or []):
        tokens.update(tokenize_search_value(str(topic)))
    tokens.discard("")
    return tokens


__all__ = [
    "CatalogSearchDocument",
    "CatalogSearchIndex",
    "SearchTerms",
    "derive_discovery_tokens",
    "normalize_search_value",
    "tokenize_search_value",
]
