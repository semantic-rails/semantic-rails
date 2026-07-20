"""Scoring intent text against the catalog token index and gating discovery.

The relevance floor is the single check between ``classify_question`` and the
candidate-generation logic in ``plan``/``discover``: does the user's text
share even one content token with anything in the catalog? Intents that fail
the floor get a structured ``low_relevance`` refusal block; intents that pass
but cannot produce any executable candidate downstream get a
``no_viable_candidates`` block. Both blocks share an envelope shape with the
out-of-scope refusal so agents can branch on a single presence-of-key test.

This module owns:

* Compatibility wrappers around the shared catalog-search normalization
  primitives (``_norm``, ``_tokenize``).
* The stopword set stripped before computing overlap (``_INTENT_STOPWORDS``).
* Tokenization / catalog-token-index helpers used as the relevance denominator.
* The floor predicate (``_intent_passes_relevance_floor``).
* The two structured refusal blocks (``_low_relevance_block``,
  ``_no_viable_candidates_block``) and the convenience attacher
  (``_maybe_attach_no_viable_signal``) that planner success paths call
  to keep the gate fields consistent.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from ..catalog_search import (
    CatalogSearchIndex,
    derive_discovery_tokens,
    normalize_search_value,
    tokenize_search_value,
)
from ..schema import PackageConfig


def _derive_discovery_tokens(name: str, label: str, description: str) -> list[str]:
    """Compatibility wrapper for planner imports."""

    return derive_discovery_tokens(name, label, description)


def _norm(value: str) -> str:
    """Compatibility wrapper for metadata helpers."""

    return normalize_search_value(value)


def _tokenize(value: str) -> list[str]:
    """Compatibility wrapper for metadata and planner helpers."""

    return tokenize_search_value(value)


# Stopwords stripped before computing token-overlap relevance against the
# catalog. These tokens appear in nearly any business intent and would
# otherwise mask the "no real overlap" case (e.g. "of an unladen swallow"
# trivially shares "of"/"an" with random catalog entries). Includes common
# verbs that nonsense intents tend to share with the catalog by accident
# ("compile" - we compile SQL; "swallow" - no match - by stripping these
# we surface zero-overlap intents cleanly).
_INTENT_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "did",
        "does",
        "for",
        "from",
        "get",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "many",
        "much",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "per",
        "show",
        "so",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "this",
        "those",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        # neutral query verbs/markers that match catalog text too loosely
        "vs",
        "versus",
        "between",
    }
)


def _content_tokens(text: str) -> set[str]:
    """Tokens with stopwords removed — used for relevance-floor checks."""
    return {tok for tok in _tokenize(text) if tok and tok not in _INTENT_STOPWORDS}


def _catalog_token_index(
    config: PackageConfig,
    *,
    search_index: CatalogSearchIndex | None = None,
) -> frozenset[str]:
    """Return queryable tokens from a runtime-scoped immutable index.

    Used as a denominator for the relevance floor: if no content token in
    the intent appears anywhere in the catalog, the intent is almost
    certainly out-of-domain and ``plan``/``discover`` should refuse
    rather than return a top-scored bonus-only candidate.
    """
    index = search_index or CatalogSearchIndex.from_config(config)
    return index.catalog_tokens


def _catalog_token_doc_freq(
    config: PackageConfig,
    *,
    search_index: CatalogSearchIndex | None = None,
) -> Mapping[str, int]:
    """Document frequency per token across public objects (measures,
    metric_recipes, dimensions, segments). One object = one document,
    counted once even if a token appears in label + search_terms + topic.

    Used by `_relevance_score` to dampen per-token bonuses for common
    tokens (e.g. "customer" in a customer-centric package) so a rare
    token (e.g. "aov") drives the rank. Without this, term-bag scoring
    pushes a candidate that matches the common token three times above
    one that matches the rare token once.
    """
    index = search_index or CatalogSearchIndex.from_config(config)
    return index.token_doc_freq


def _token_idf_weight(token: str, doc_freq: Mapping[str, int] | None) -> float:
    """Return the IDF-style multiplier for a per-token bonus.

    Rare tokens (df<=2) get a 2.0× multiplier; common tokens (df>=12)
    get 0.5×; tokens in between scale continuously via 6/df. The
    blind-agent benchmark established this is the right calibration —
    `aov` (df=2) needs ~2× to flip `metric.sales.aov_usd` above the
    customer_count measures that match `customer` repeatedly, and the
    downward penalty for common tokens is what creates the gap.

    Returns 1.0 when `doc_freq` is None (no IDF context — caller didn't
    pass the catalog frequency map) or when the token is missing.
    """
    if not doc_freq:
        return 1.0
    df = doc_freq.get(token, 0)
    if df <= 0:
        return 1.0
    return max(0.5, min(2.0, 6.0 / float(df)))


def _intent_passes_relevance_floor(
    intent: str, catalog_tokens: Collection[str]
) -> tuple[bool, list[str]]:
    """Return (passes, overlap_tokens). The floor is one shared content
    token between the intent and the catalog after stopword removal —
    deliberately lenient so any real intent passes, and only zero-overlap
    nonsense (``"compile the linux kernel"`` against jaffle_shop) fails."""
    intent_tokens = _content_tokens(intent)
    if not intent_tokens:
        # Empty / pure-stopword intents are fine — they reach the existing
        # "no chosen object" path which already returns 0 candidates.
        return True, []
    # Plural-tolerant match: an intent token matches the catalog if it is
    # present directly, or its singular form (trailing-"s" stripped) is.
    overlap: set[str] = set()
    for token in intent_tokens:
        if (
            token in catalog_tokens
            or token.endswith("s")
            and len(token) > 3
            and token[:-1] in catalog_tokens
        ):
            overlap.add(token)
    return bool(overlap), sorted(overlap)


_TIME_GRAIN_TOKENS: frozenset[str] = frozenset(
    {
        "daily",
        "day",
        "days",
        "weekly",
        "week",
        "weeks",
        "monthly",
        "month",
        "months",
        "quarterly",
        "quarter",
        "quarters",
        "yearly",
        "year",
        "years",
        "date",
        "dates",
        "time",
        "times",
    }
)


def _weak_grounding_tokens(config: PackageConfig) -> set[str]:
    """Tokens that overlap nearly any catalog without naming anything in
    it: time-grain vocabulary plus the package's own namespace tokens
    (every ``measure.jaffle.*`` id contributes "jaffle")."""
    meta = getattr(config, "package", None)
    package_id = str(getattr(meta, "package_id", "") or getattr(config, "package_id", "") or "")
    return set(_TIME_GRAIN_TOKENS) | set(_tokenize(package_id))


def _intent_passes_grounding_floor(
    intent: str, catalog_tokens: Collection[str], *, weak_tokens: set[str]
) -> tuple[bool, list[str]]:
    """Stricter sibling of the relevance floor used by ``plan``: at least
    one overlapping token must be a strong one. "refunds by month" overlaps
    the catalog only through "month" — planning on that would confidently
    answer a question about an object that does not exist. ``discover``
    keeps the lenient floor; this gate only protects executable plans."""
    intent_tokens = _content_tokens(intent)
    if not intent_tokens:
        # Same contract as the relevance floor: empty / pure-stopword
        # intents fall through to the "no chosen object" path.
        return True, []
    strong: set[str] = set()
    for token in intent_tokens:
        if token in weak_tokens:
            continue
        if (
            token in catalog_tokens
            or token.endswith("s")
            and len(token) > 3
            and token[:-1] in catalog_tokens
        ):
            strong.add(token)
    return bool(strong), sorted(strong)


def _weak_grounding_block(
    intent: str, *, overlap_tokens: list[str], catalog_token_sample: list[str]
) -> dict[str, Any]:
    """Structured 'low_relevance' variant for intents whose only catalog
    overlap is boilerplate (time grains, package namespace) — e.g.
    "refunds by month" matching solely on "month"."""
    return {
        "code": "LOW_RELEVANCE",
        "intent": intent,
        "overlap_tokens": overlap_tokens,
        "rationale": (
            "the intent only overlaps the catalog through generic tokens "
            "(time grains or the package name); no measure, metric, or "
            "dimension is actually named"
        ),
        "recovery_hint": (
            "Name a concrete measure or metric (for example 'revenue by "
            "month'), or call the `discover` tool to browse what this "
            "package can answer."
        ),
        "catalog_token_sample": catalog_token_sample,
    }


def _low_relevance_block(
    intent: str, *, overlap_tokens: list[str], catalog_token_sample: list[str]
) -> dict[str, Any]:
    """Structured 'low_relevance' block for plan/discover when the
    intent technically classified as a data_query but shares no content
    tokens with anything in the catalog (the 'unladen swallow' case)."""
    return {
        "code": "LOW_RELEVANCE",
        "intent": intent,
        "overlap_tokens": overlap_tokens,
        "rationale": (
            "no content token in the intent overlaps with any catalog "
            "object name, label, or search term"
        ),
        "recovery_hint": (
            "Call the `discover` tool with simpler terms, or the `catalog` "
            "tool to browse the available objects in this package."
        ),
        "catalog_token_sample": catalog_token_sample,
    }


def _no_viable_candidates_block(
    intent: str,
    *,
    blocked_codes: list[str],
    sample_blocked_messages: list[str],
) -> dict[str, Any]:
    """Structured top-level signal when planning classifies + clears the
    relevance floor but cannot produce a single executable candidate (every
    candidate hits PATH_NOT_FOUND, ENTITY_NOT_REACHABLE, or similar). This is
    the "real analytic, silent fail" branch the v2 adversarial audit flagged:
    YoY-on-segment-membership is a sensible question, but the package can't
    answer it. Without this block, agents see ``candidates=[]`` with no gate
    hint and start guessing. The block is emitted at the same envelope level
    as ``low_relevance`` / ``out_of_scope`` so agents can branch on a single
    presence-of-key test."""
    return {
        "code": "NO_VIABLE_CANDIDATES",
        "intent": intent,
        "blocked_codes": blocked_codes,
        "sample_blocked_messages": sample_blocked_messages[:3],
        "rationale": (
            "The intent passed scope and relevance checks, but every "
            "candidate query failed validation — most commonly because the "
            "requested entity path or analytic pattern isn't modeled in this "
            "package."
        ),
        "recovery_hint": (
            "Inspect the `blocked` array for specific reason codes "
            "(PATH_NOT_FOUND, INVALID_QUERY). Use /api/v1/discover to find "
            "what IS available, or /api/v1/catalog to browse modeled "
            "entities, measures, and metrics."
        ),
    }


def _maybe_attach_no_viable_signal(
    payload: dict[str, Any],
    *,
    intent: str,
) -> dict[str, Any]:
    """Attach a top-level ``no_viable_candidates`` block to a planning
    response when the request classified + cleared the relevance floor but
    every candidate hit validation/path errors. Returns ``payload`` mutated
    in-place. Idempotent: only attaches when ``candidates`` is empty,
    ``blocked`` is non-empty, and no other gate block is already present."""
    if payload.get("candidates"):
        return payload
    blocked_rows = list(payload.get("blocked", []) or [])
    if not blocked_rows:
        return payload
    if (
        payload.get("low_relevance")
        or payload.get("out_of_scope")
        or payload.get("no_viable_candidates")
    ):
        return payload
    blocked_codes: list[str] = []
    blocked_messages: list[str] = []
    for row in blocked_rows:
        why = dict(row.get("why_blocked", {}) or {})
        code = str(why.get("code", "") or "")
        message = str(why.get("message", "") or "")
        if code and code not in blocked_codes:
            blocked_codes.append(code)
        if message:
            blocked_messages.append(message)
    payload["no_viable_candidates"] = _no_viable_candidates_block(
        intent=intent,
        blocked_codes=blocked_codes,
        sample_blocked_messages=blocked_messages,
    )
    return payload
