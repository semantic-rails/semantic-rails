from __future__ import annotations

from pathlib import Path

from semantic_rails.catalog_search import SearchTerms
from semantic_rails.metadata import _relevance_score, discover_payload
from semantic_rails.metadata_parts.object_metadata import _comparison_metadata
from semantic_rails.metadata_parts.relevance import _catalog_token_doc_freq
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config


def _candidate(row: object, kind: str) -> dict[str, object]:
    return {
        "id": str(getattr(row, "id", "") or ""),
        "kind": kind,
        "name": str(getattr(row, "name", "") or ""),
        "label": str(getattr(row, "label", "") or ""),
        "description": str(getattr(row, "description", "") or ""),
        "topics": list(getattr(row, "topics", []) or []),
        "available": True,
        "root_entity": str(
            getattr(row, "entity", "") or (getattr(row, "id", "") if kind == "entity" else "")
        ),
        "review_priority": str(
            dict(getattr(row, "meta", {}) or {}).get("review_priority", "") or ""
        ),
        **_comparison_metadata(row),
    }


def test_compiled_documents_preserve_relevance_scores(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        index = runtime._get_catalog_search_index()
        doc_freq = _catalog_token_doc_freq(runtime.config, search_index=index)
        collections = (
            ("measure", runtime.config.measures),
            ("metric", runtime.config.metric_recipes),
            ("dimension", runtime.config.dimensions),
            ("segment", runtime.config.segments),
            ("entity", runtime.config.entities),
        )
        for terms in (
            "revenue by store",
            "top customers by aov",
            "monthly order count",
            "snapshot inventory",
        ):
            search_terms = SearchTerms.from_text(terms)
            for kind, collection in collections:
                for config_row in collection:
                    candidate = _candidate(config_row, kind)
                    expected = _relevance_score(
                        terms,
                        candidate,
                        preferred_kind=kind,
                        token_doc_freq=doc_freq,
                    )
                    indexed = _relevance_score(
                        terms,
                        candidate,
                        preferred_kind=kind,
                        token_doc_freq=doc_freq,
                        search_document=index.document(str(candidate["id"])),
                        search_terms=search_terms,
                    )
                    assert indexed == expected, (terms, candidate["id"])
    finally:
        runtime.close()


def test_search_index_is_reused_without_caching_policy_visibility(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    hidden_id = "measure.jaffle.lifetime_spend_before_tax_usd"
    try:
        index = runtime._get_catalog_search_index()
        assert index.document(hidden_id) is not None

        unrestricted = discover_payload(runtime, terms="lifetime spend before tax", limit=20)
        restricted = discover_payload(
            runtime,
            terms="lifetime spend before tax",
            limit=20,
            partial_query={"policy_context": {"audience": "ops"}},
        )

        assert any(row["id"] == hidden_id for row in unrestricted["measures"])
        assert all(row["id"] != hidden_id for row in restricted["measures"])
        assert runtime._get_catalog_search_index() is index
    finally:
        runtime.close()


def test_reload_discards_index_and_rebuilds_from_edited_config(tmp_path: Path) -> None:
    package_root = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(package_root))
    try:
        original = runtime._get_catalog_search_index()
        assert "qzxlookup" not in original.catalog_tokens

        model_file = package_root / "models" / "core" / "customers.yml"
        model_file.write_text(
            model_file.read_text(encoding="utf-8").replace(
                "label: Customer count",
                "label: Customer qzxlookup count",
                1,
            ),
            encoding="utf-8",
        )

        result = runtime.reload()
        assert result["changed"] is True
        assert runtime._catalog_search_index is None

        rebuilt = runtime._get_catalog_search_index()
        assert rebuilt is not original
        assert "qzxlookup" in rebuilt.catalog_tokens
        assert "qzxlookup" not in original.catalog_tokens
    finally:
        runtime.close()
