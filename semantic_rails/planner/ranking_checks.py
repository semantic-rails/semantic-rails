from __future__ import annotations

import re
from typing import Any

from ._base import _NUMBER_WORDS, _object_text, _singular, _tokens
from .coverage import CoverageGap, _core_text, _plain, _time_block
from .time_phrases import _TIME_UNITS
from .visibility import visible_dimensions

# Ranking requests: "top 5 products", "the 3 lowest-selling products", "the 5
# customers who spent the most", "which store had the most orders", "rank
# stores by revenue". "At least 10 orders" is a threshold and "the 5 most
# recent months" a window; neither is a ranking.
_SUPERLATIVES = frozenset(
    {
        "best",
        "biggest",
        "fewest",
        "greatest",
        "highest",
        "largest",
        "least",
        "lowest",
        "most",
        "smallest",
        "worst",
    }
)
_ASCENDING = frozenset({"bottom", "fewest", "least", "lowest", "smallest", "worst"})
_RECENCY = frozenset({"earliest", "latest", "least-recent", "most-recent", "newest", "oldest"})
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_YEAR_NUMBER_RE = re.compile(r"(?:19|20)\d{2}")
# Words that end a ranked noun phrase ("which store had ...", "top 5 products by ...").
_PHRASE_BREAKS = frozenset(
    [
        "by",
        "in",
        "for",
        "with",
        "from",
        "of",
        "on",
        "at",
        "per",
        "that",
        "who",
        "which",
        "whose",
        "where",
        "when",
        "during",
        "over",
        "has",
        "had",
        "have",
        "is",
        "was",
        "were",
        "are",
        "does",
        "did",
        "do",
        "sells",
        "sold",
        "made",
        "makes",
        "drove",
        "drives",
        "generated",
        "generates",
        "brought",
        "got",
        "gets",
        "saw",
        "sees",
        "spent",
        "spends",
    ]
)
# Shares of a population: "the top decile of customers" is a threshold.
_SHARE_WORDS = frozenset(
    {
        "decile",
        "deciles",
        "fifth",
        "half",
        "pct",
        "percent",
        "percentile",
        "percentiles",
        "quartile",
        "quartiles",
        "quintile",
        "quintiles",
        "third",
        "tier",
    }
)
# Words that open the clause naming a ranking's order ("the store with the most
# orders", "3 products that sold the least").
_RELATIVE = frozenset({"having", "that", "which", "who", "whose", "with"})
# Words that can't be the thing ranked ("which one is ...", "which have ...").
_NOT_RANKED = frozenset(
    {
        *_PHRASE_BREAKS,
        *_SUPERLATIVES,
        *_NUMBER_WORDS,
        *_SHARE_WORDS,
        "a",
        "all",
        "an",
        "and",
        "any",
        "be",
        "been",
        "bottom",
        "can",
        "could",
        "each",
        "every",
        "it",
        "its",
        "me",
        "my",
        "one",
        "ones",
        "or",
        "our",
        "should",
        "some",
        "the",
        "their",
        "them",
        "these",
        "they",
        "this",
        "those",
        "to",
        "top",
        "us",
        "we",
        "what",
        "will",
        "would",
        "you",
        "your",
    }
)


def _ranking_request(text: str, nouns: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """Parse a ranking request into (clause, limit, direction, noun, requires_order, count_at).

    ``limit`` is None when the question fixes no count ("the top products"),
    and ``direction`` is None when it fixes no order ("rank stores by
    revenue"). A bare superlative ranks the noun after it only when that noun
    is a time unit ("the highest revenue month"), follows a hyphenated
    superlative ("best-selling products"), or follows "best"/"worst" and is
    one of the catalog's dimension ``nouns`` ("the best store"): in "the
    highest revenue", revenue is what is measured, not what is ranked.
    """

    words = [word for word, _start, _end in _ranking_words(text)]
    for index in range(len(words)):
        request = _ranking_at(words, index, nouns)
        if request is not None:
            return request
    return None


def _ranking_words(text: str) -> list[tuple[str, int, int]]:
    """The words of a ranking question with their character spans in its lowercase form."""

    # "top-3 stores" is "top 3 stores" (the same length, so spans still index the question).
    lowered = re.sub(r"\b(top|bottom|best|worst)-(\d+)\b", r"\1 \2", str(text or "").lower())
    return [(match.group(0), *match.span()) for match in _WORD_RE.finditer(lowered)]


def _ranking_at(words: list[str], index: int, nouns: frozenset[str]) -> dict[str, Any] | None:
    word = words[index]
    before = words[index - 1] if index else ""
    if word in {"rank", "ranking"}:
        # "rank stores by revenue", "ranking of the stores by revenue"
        start = index + 1
        while start < len(words) and words[start] in {"all", "of", "our", "the"}:
            start += 1
        noun, end = _noun_phrase(words, start)
        if noun and end < len(words) and words[end] == "by":
            return _ranking(words, index, end, None, _stated_direction(words, end), noun, True)
        return None
    if word == "ranked" and index + 1 < len(words) and words[index + 1] == "by":
        # "stores ranked by revenue"
        if before and before not in _NOT_RANKED:
            return _ranking(
                words, index - 1, index + 2, None, _stated_direction(words, index), before, True
            )
        return None
    if word in {"top", "bottom"}:
        # "top 5 products", "bottom three stores", "the top store", "top 2000 customers"
        direction: str | None = "DESC" if word == "top" else "ASC"
        cursor = index + 1
        count = _count(words, cursor, years=True)
        if count is not None:
            cursor += 1
        superlative = _superlative(words, cursor)
        if superlative:
            direction = superlative
            cursor += 1
        noun, end = _noun_phrase(words, cursor)
        if not noun:
            return None
        limit = count if count is not None else (1 if _singular(noun) == noun else None)
        at = index + 1 if count is not None else None
        return _ranking(words, index, end, limit, direction, noun, False, at)
    count = _count(words, index, years=False)
    if count is not None:
        # "the 3 lowest-selling products", "5 best-selling products",
        # "the 5 customers who spent the most"
        cursor = index + 1
        if _recency(words, cursor):
            return None
        superlative = _superlative(words, cursor)
        if superlative:
            cursor += 1
        noun, end = _noun_phrase(words, cursor)
        direction = superlative or _superlative_after(words, end)
        # "5 best-selling products", "the 3 stores with the least revenue",
        # "3 stores with the highest revenue" (a relative clause names the order).
        relative = end < len(words) and words[end] in _RELATIVE
        qualified = bool(superlative) or before == "the" or relative
        if not noun or not direction or not qualified:
            return None
        start = index - 1 if before == "the" else index
        return _ranking(words, start, end, count, direction, noun, False, index)
    if word == "the" and index + 1 < len(words) and words[index + 1] not in _NOT_RANKED:
        # "the store with the most orders", "the product that sold the least"
        noun, end = _noun_phrase(words, index + 1)
        if noun and words[end : end + 1] and words[end] in _RELATIVE:
            direction = _superlative_after(words, end)
            if direction:
                limit = 1 if _singular(noun) == noun else None
                return _ranking(words, index, end, limit, direction, noun, False)
    if word == "which":
        # "which store had the most orders", "which of the stores ...",
        # "which 2 stores ..."
        cursor = index + 1
        count = _count(words, cursor, years=False)
        at = cursor if count is not None else None
        if count is not None:
            cursor += 1
        one = words[cursor : cursor + 1] == ["of"] and cursor + 1 < len(words)
        if one:
            cursor += 2 if words[cursor + 1] in {"our", "the", "these", "those"} else 1
        noun, end = _noun_phrase(words, cursor)
        direction = _superlative_after(words, end)
        if not noun or not direction:
            return None
        limit = count if count is not None else (1 if one or _singular(noun) == noun else None)
        return _ranking(words, index, end, limit, direction, noun, False, at)
    superlative = _superlative(words, index)
    if superlative and _count(words, index + 1, years=True) is not None:
        # "best 3 stores by revenue", "highest 5 products"
        noun, end = _noun_phrase(words, index + 2)
        if noun:
            count = _count(words, index + 1, years=True)
            return _ranking(words, index, end, count, superlative, noun, False, index + 1)
    if superlative:
        # "the best-selling product", "the highest revenue month", "the best store"
        noun, end = _noun_phrase(words, index + 1)
        head = _singular(noun)
        if noun and (
            "-" in word or head in _TIME_UNITS or (word in {"best", "worst"} and head in nouns)
        ):
            start = index - 1 if before == "the" else index
            return _ranking(
                words, start, end, 1 if head == noun else None, superlative, noun, False
            )
    return None


def _ranking(
    words: list[str],
    start: int,
    end: int,
    limit: int | None,
    direction: str | None,
    noun: str,
    requires_order: bool,
    count_at: int | None = None,
) -> dict[str, Any]:
    return {
        "clause": " ".join(words[start:end]),
        "limit": limit,
        "direction": direction,
        "noun": noun,
        "requires_order": requires_order,
        # The index of the word that states the count, when the question states one.
        "count_at": count_at,
    }


def _count(words: list[str], index: int, *, years: bool) -> int | None:
    """The count at words[index] ("5", "five"); a 20xx number counts only after top/bottom."""

    if index >= len(words):
        return None
    word = words[index]
    if word in _NUMBER_WORDS:
        return _NUMBER_WORDS[word]
    if not word.isdigit() or (not years and _YEAR_NUMBER_RE.fullmatch(word)):
        return None
    return int(word)


def _recency(words: list[str], index: int) -> bool:
    """ "most recent", "latest": an ordering by time, which a window answers."""

    if index >= len(words):
        return False
    if words[index] in _RECENCY:
        return True
    return words[index] in {"least", "most"} and words[index + 1 : index + 2] == ["recent"]


def _superlative(words: list[str], index: int) -> str | None:
    """The sort direction a superlative at words[index] asks for, if it ranks."""

    if index >= len(words) or _recency(words, index):
        return None
    base = words[index].split("-", 1)[0]
    if base not in _SUPERLATIVES:
        return None
    if base in {"least", "most"} and index and words[index - 1] == "at":
        return None  # "at least 10 orders" is a threshold
    return "ASC" if base in _ASCENDING else "DESC"


def _superlative_after(words: list[str], start: int) -> str | None:
    for index in range(start, len(words)):
        direction = _superlative(words, index)
        if direction:
            return direction
    return None


def _stated_direction(words: list[str], start: int) -> str | None:
    """The order a "rank ... by" request states, if any ("lowest first", "descending")."""

    for word in words[start:]:
        if word in {"asc", "ascending", "increasing"}:
            return "ASC"
        if word in {"desc", "descending", "decreasing"}:
            return "DESC"
    return _superlative_after(words, start)


def _noun_phrase(words: list[str], start: int) -> tuple[str, int]:
    """The ranked noun phrase at words[start]: its head word and the index after it."""

    end = start
    while (
        end < len(words)
        and end - start < 2
        and words[end] not in _NOT_RANKED
        and not words[end].isdigit()
    ):
        end += 1
    if end == start:
        return "", start
    head = words[end - 1]
    return (head[:-2] if head.endswith("'s") else head), end


def _ranking_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """A ranking request loses its limit, its sort or the thing being ranked."""

    config = runtime._config
    request = _ranking_request(text, _dimension_nouns(config))
    if request is None:
        return []
    order_by = [row for row in list(query.get("order_by") or []) if isinstance(row, dict)]
    problems: list[str] = []
    if request["limit"] is not None and query.get("limit") != request["limit"]:
        problems.append("limit")
    # Every ranking needs the requested order, even when no count was stated.
    # A draft's own limit can otherwise silently return the opposite end.
    first = order_by[0] if order_by else {}
    direction = str(first.get("direction") or "ASC").upper()
    if (
        not first
        or not _orders_by_a_value(first, query)
        or (request["direction"] and direction != request["direction"])
    ):
        problems.append("order")
    ranked_ids = _ranking_measure_ids(config, text, request)
    selected = [row for row in list(query.get("select") or []) if isinstance(row, dict)]
    ordered = next((row for row in selected if row.get("as") == first.get("field")), {})
    expression = ordered.get("expression")
    ordered_id = (
        expression.get("measure") or expression.get("metric")
        if isinstance(expression, dict)
        else None
    )
    if first and not ordered and "order" not in problems:
        problems.append("order")
    if len(ranked_ids) == 1 and ordered_id not in ranked_ids:
        problems.append("ranked_measure")
    elif len(ranked_ids) > 1 or (not ranked_ids and len(selected) > 1):
        problems.append("ranked_measure_uncertain")
    noun = _singular(request["noun"])
    time = _time_block(query)
    if noun in _TIME_UNITS:
        if str(time.get("grain", "") or "") != noun:
            problems.append("ranked_time_grain")
    else:
        ranked = {
            str(row.id)
            for row in visible_dimensions(config)
            if noun in {_singular(token) for token in _tokens(_object_text(row))}
        }
        grouped = {str(item) for item in list(query.get("group_by") or [])}
        if ranked and not ranked & grouped:
            problems.append("ranked_dimension")
    if not problems:
        return []
    return [
        CoverageGap(
            kind="ranking_unrealized",
            clause=request["clause"],
            message=(
                "The question asks for a ranking, but the draft does not return the requested "
                "rows in order: " + ", ".join(problems) + "."
            ),
            expected={
                "limit": request["limit"],
                "direction": request["direction"],
                "ranked": request["noun"],
                "order_by": "a selected value",
            },
            actual={
                "limit": query.get("limit"),
                "order_by": order_by,
                "group_by": list(query.get("group_by") or []),
                "grain": time.get("grain"),
            },
            recovery_hint={
                "kind": "provide_ranking",
                "message": (
                    "Group by the ranked dimension (or set time.grain for ranked periods), order "
                    "by the measure in the requested direction"
                    + (", and set the requested limit" if request["limit"] is not None else "")
                    + ", then validate."
                ),
            },
        )
    ]


def _orders_by_a_value(order: dict[str, Any], query: dict[str, Any]) -> bool:
    """Whether an order_by entry sorts by a selected value, not a group or the period."""

    name = order.get("field")
    if not isinstance(name, str):
        return True
    grouped = {str(item) for item in list(query.get("group_by") or [])}
    return name != "time" and name not in grouped and not name.startswith("dimension.")


def _ranking_measure_ids(config: Any, text: str, request: dict[str, Any]) -> set[str]:
    """Resolve an explicitly named ranking measure without guessing among catalog names."""

    normalized = re.sub(r"\b(top|bottom|best|worst)-(\d+)\b", r"\1 \2", text.lower())
    words = _WORD_RE.findall(normalized)
    clause = request["clause"].split()
    start = next(
        (
            index + len(clause)
            for index in range(len(words))
            if words[index : index + len(clause)] == clause
        ),
        len(words),
    )
    tail = words[start:]
    if not clause or clause[-1] != "by":
        anchor = next(
            (
                index
                for index, word in enumerate(tail)
                if word in {"by", "most", "least", "highest", "lowest", "fewest"}
            ),
            None,
        )
        if anchor is None:
            return set()
        tail = tail[anchor + 1 :]
    while tail and tail[0] in {"the", "total"}:
        tail = tail[1:]
    phrase: list[str] = []
    for word in tail:
        if word in _PHRASE_BREAKS | {"among", "except", "excluding", "vs", "versus", "but"}:
            break
        phrase.append(word)
    sought = tuple(_singular(word) for word in _plain(" ".join(phrase)).split())
    if not sought:
        return set()
    matched: set[str] = set()
    for row in [*getattr(config, "measures", []), *getattr(config, "metric_recipes", [])]:
        object_id = str(getattr(row, "id", "") or "")
        fields = [
            object_id.rsplit(".", 1)[-1],
            str(getattr(row, "name", "") or "").rsplit(".", 1)[-1],
            str(getattr(row, "label", "") or ""),
            *[str(alias) for alias in getattr(row, "aliases", []) or []],
        ]
        for candidate in fields:
            tokens = [_singular(word) for word in _plain(candidate).split()]
            while tokens and tokens[-1] in {"usd"}:
                tokens.pop()
            if tuple(tokens) == sought:
                matched.add(object_id)
                break
    return matched


def _dimension_nouns(config: Any) -> frozenset[str]:
    return frozenset(
        _singular(token) for row in visible_dimensions(config) for token in _tokens(_core_text(row))
    )
