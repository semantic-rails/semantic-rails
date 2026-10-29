"""Driver-free final SQL preparation shared by compilation and direct adapter calls.

The compiler owns the executable statement and immutable result-column mapping.
Adapters executing a PreparedQuery must send its SQL unchanged; session timeout
commands and bounded row fetching remain adapter concerns.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from .errors import SemanticLayerError

ParameterValue = str | int | bool
# The name rule for trusted request attributes, which parameter slots name.
ATTRIBUTE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# A ``uuid`` slot takes a string holding a UUID (``_canonical_uuid``).
_SLOT_TYPES: dict[str, type] = {"string": str, "integer": int, "boolean": bool, "uuid": str}
# A UUID as DuckDB and Postgres both read one: 32 hex digits in any case, hyphenated 8-4-4-4-12
# or not, in braces or not.
_UUID_DIGITS = r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
_UUID = re.compile(rf"{_UUID_DIGITS}|\{{{_UUID_DIGITS}\}}")


@dataclass(frozen=True)
class ParameterSlot:
    """One positional placeholder, bound per request from a trusted attribute.

    The value must have exactly the declared type: ``string``, ``integer``,
    ``boolean`` or ``uuid`` (a string holding a UUID, bound as its canonical lower-case
    text). A slot with an ``index`` binds that item of a list attribute (strings, integers
    or UUIDs), and only a list does. A compiled statement holds only slots, never their
    values.
    """

    attribute: str
    type: Literal["string", "integer", "boolean", "uuid"]
    index: int | None = None

    def __post_init__(self) -> None:
        if self.type not in _SLOT_TYPES:
            raise ValueError(f"Unsupported parameter type {self.type!r}.")
        if type(self.attribute) is not str or not ATTRIBUTE_NAME.fullmatch(self.attribute):
            raise ValueError("Parameter attribute names must match [a-z][a-z0-9_]{0,63}.")
        if self.index is not None and (
            type(self.index) is not int or self.index < 0 or self.type == "boolean"
        ):
            raise ValueError("A list slot's index is an int >= 0, on a string or integer slot.")


@dataclass(frozen=True)
class PreparedQuery:
    """Executable SQL with physical-to-semantic result column names.

    ``parameters`` lists the statement's placeholders in order (``?`` before
    driver finalization, ``$n`` for Postgres). Only an
    adapter that sends values to its driver separately may execute such a
    statement; values are never rendered into the SQL text.
    """

    sql: str
    column_mapping: tuple[tuple[str, str], ...] = ()
    parameters: tuple[ParameterSlot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", tuple(self.parameters))


def parameters_denied(reason: str, **details: str) -> SemanticLayerError:
    # Names the reason and attribute only; bound values never enter errors.
    return SemanticLayerError(
        "POLICY_DENIED",
        "Query parameters could not be bound safely for this request.",
        details={"reason": reason, **details},
    )


def checked_parameter_values(
    prepared: PreparedQuery, values: Sequence[Any]
) -> tuple[ParameterValue, ...]:
    """The values to bind from each slot's attribute value (``checked_slot_value``). A list
    binds only through indexed slots covering every item, so no item is dropped."""
    if len(values) != len(prepared.parameters):
        raise parameters_denied("parameter_count_mismatch")
    bound = tuple(map(checked_slot_value, prepared.parameters, values))
    # Each index is inside its list (checked above), so distinct indexes cover the list
    # exactly when there are as many as items.
    indexes: dict[str, set[int]] = {}
    lengths: dict[str, int] = {}
    for slot, value in zip(prepared.parameters, values, strict=True):
        if slot.index is not None:
            indexes.setdefault(slot.attribute, set()).add(slot.index)
            lengths[slot.attribute] = len(value)
    for attribute, seen in indexes.items():
        if len(seen) != lengths[attribute]:
            raise parameters_denied("parameter_count_mismatch", attribute=attribute)
    return bound


def checked_bound_values(
    prepared: PreparedQuery, values: Sequence[Any]
) -> tuple[ParameterValue, ...]:
    """The values an adapter binds, one per slot: no NULL, no coercion (a UUID only takes its
    canonical text), no extra or missing value. An indexed slot takes its item itself, never
    the list."""
    if len(values) != len(prepared.parameters):
        raise parameters_denied("parameter_count_mismatch")
    return tuple(map(_checked_item, prepared.parameters, values))


def checked_slot_value(slot: ParameterSlot, value: Any) -> ParameterValue:
    """The value ``slot`` binds from its attribute's value, or a denial naming only the
    attribute: the value itself, or for an indexed slot its item of a list."""
    if slot.index is not None and value is not None:
        if type(value) is not tuple or slot.index >= len(value):
            raise parameters_denied("attribute_type_mismatch", attribute=slot.attribute)
        value = value[slot.index]
    return _checked_item(slot, value)


def _checked_item(slot: ParameterSlot, value: Any) -> ParameterValue:
    if value is None:
        raise parameters_denied("missing_attribute", attribute=slot.attribute)
    if type(value) is not _SLOT_TYPES[slot.type]:
        raise parameters_denied("attribute_type_mismatch", attribute=slot.attribute)
    if slot.type == "uuid":
        return _canonical_uuid(slot, cast(str, value))
    return cast(ParameterValue, value)


def _canonical_uuid(slot: ParameterSlot, value: str) -> str:
    """``value`` as its canonical lower-case hyphenated text, which a warehouse with a UUID type
    casts to one (``SqlDialect.uuid_parameter``) and one without compares as text. Any other
    string is denied here, before a statement runs, so no warehouse error can quote it."""
    if not _UUID.fullmatch(value):
        raise parameters_denied("attribute_type_mismatch", attribute=slot.attribute)
    return str(uuid.UUID(hex=value.strip("{}").replace("-", "")))


# Consume complete Postgres identifiers and E-strings before looking for binds.
# Also skip ANSI quotes, comments and dollar quotes in compiler or direct SQL.
_PARAMETER_TOKEN = re.compile(
    r"[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*|"
    r"[A-Za-z_\u0080-\U0010ffff][A-Za-z_0-9$\u0080-\U0010ffff]*|"
    r"(?P<dollar>\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z_0-9\u0080-\U0010ffff]*)?\$)"
    r".*?(?P=dollar)|\?|\$[0-9]+",
    re.DOTALL,
)


# Snowflake also permits backslash escapes and dollar signs in identifiers.
_SNOWFLAKE_PARAMETER_TOKEN = re.compile(
    r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/|"
    r"\$\$.*?\$\$|[A-Za-z_\u0080-\U0010ffff][A-Za-z_0-9$\u0080-\U0010ffff]*|\?|\$[0-9]+",
    re.DOTALL,
)


def postgres_parameter_tokens(sql: str) -> list[re.Match[str]]:
    tokens: list[re.Match[str]] = []
    position = 0
    while match := _PARAMETER_TOKEN.search(sql, position):
        position = match.end()
        if match[0] == "/*":
            depth = 1
            for delimiter in re.compile(r"/\*|\*/").finditer(sql, position):
                depth += 1 if delimiter[0] == "/*" else -1
                if not depth:
                    position = delimiter.end()
                    break
            else:
                position = len(sql)
        elif re.fullmatch(r"\?|\$[0-9]+", match[0]):
            tokens.append(match)
    return tokens


# Each warehouse's documented limit, by connection. Postgres binds at most 65,535 parameters
# in one statement: its protocol's Bind message counts them in 16 bits
# (https://www.postgresql.org/docs/current/protocol-message-formats.html, "Bind").
MAX_PARAMETERS = {"postgres_native": 65_535}
# Snowflake takes at most 200,000 expressions in one list, such as an IN list
# (https://docs.snowflake.com/en/sql-reference/constructs/where, "Usage notes").
# DuckDB documents neither limit.
MAX_LIST_ITEMS = {"snowflake_adbc": 200_000}


def finalize_parameters(prepared: PreparedQuery, connection_kind: str) -> PreparedQuery:
    """Refuse a statement over its warehouse's documented limit, then finalize Postgres bind
    syntax, before the statement reaches execution."""
    statement_limit = MAX_PARAMETERS.get(connection_kind)
    if statement_limit is not None and len(prepared.parameters) > statement_limit:
        raise _over_limit(statement_limit, "parameters in one statement")
    list_limit = MAX_LIST_ITEMS.get(connection_kind)
    # A list's slots are indexed from 0, so its length is its last index plus one.
    if list_limit is not None and any(
        slot.index is not None and slot.index >= list_limit for slot in prepared.parameters
    ):
        raise _over_limit(list_limit, "values in one list")
    if connection_kind != "postgres_native" or not prepared.parameters:
        return prepared
    tokens = postgres_parameter_tokens(prepared.sql)
    if len(tokens) != len(prepared.parameters) or any(m[0] != "?" for m in tokens):
        raise parameters_denied("parameter_placeholder_mismatch")
    pieces, position = [], 0
    for index, token in enumerate(tokens, 1):
        pieces += [prepared.sql[position : token.start()], f"${index}"]
        position = token.end()
    return replace(prepared, sql="".join([*pieces, prepared.sql[position:]]))


def _over_limit(limit: int, unit: str) -> SemanticLayerError:
    # Neither the size nor the values: a trusted list's length is not the caller's to read.
    return SemanticLayerError(
        "POLICY_DENIED",
        f"This request binds more than the warehouse's documented limit of {limit:,} {unit}, "
        "so its row filters can't be applied here.",
        details={"reason": "parameter_limit_exceeded", "limit": limit},
    )


def check_postgres_parameters(prepared: PreparedQuery) -> None:
    """Every slot has exactly one numbered placeholder; refuse bypasses centrally."""
    actual = [m[0] for m in postgres_parameter_tokens(prepared.sql)]
    if not prepared.parameters:
        actual = [token for token in actual if token != "?"]
    expected = [f"${index}" for index in range(1, len(prepared.parameters) + 1)]
    if actual != expected:
        raise parameters_denied("parameter_placeholder_mismatch")


def check_snowflake_parameters(prepared: PreparedQuery) -> None:
    """Every authored slot has one qmark; refuse direct prepared bypasses."""
    actual = [
        match[0]
        for match in _SNOWFLAKE_PARAMETER_TOKEN.finditer(prepared.sql)
        if re.fullmatch(r"\?|\$[0-9]+", match[0])
    ]
    if actual != ["?"] * len(prepared.parameters):
        raise parameters_denied("parameter_placeholder_mismatch")


def literal_end(sql: str, start: int, *, backslash: bool = False) -> int:
    """The index just past the single-quoted literal that opens at ``start``.

    Inside a literal ``''`` is an escaped quote, or, where ``backslash`` (BigQuery, Spark),
    ``\\`` escapes the next character and a quote always closes the literal: those
    dialects render a quote as ``\\'`` (see
    :meth:`semantic_rails.dialects.SqlDialect.quote_string_literal`). An unterminated
    literal runs to the end.
    """
    j = start + 1
    while j < len(sql):
        if backslash and sql[j] == "\\":
            j += 2
        elif sql[j] != "'":
            j += 1
        elif not backslash and sql[j + 1 : j + 2] == "'":
            j += 2
        else:
            return j + 1
    return len(sql)


def literal_spans(sql: str, *, backslash: bool = False) -> list[tuple[int, int]]:
    """Spans of single-quoted string literals (see :func:`literal_end`)."""
    spans: list[tuple[int, int]] = []
    start = sql.find("'")
    while start != -1:
        end = literal_end(sql, start, backslash=backslash)
        spans.append((start, end))
        start = sql.find("'", end)
    return spans


def map_double_quoted_identifiers(
    sql: str, replace: Callable[[str], str], *, backslash: bool = False
) -> str:
    """Quote-aware scanning core for identifier-re-quoting compat passes.

    The renderer emits ANSI ``"identifier"`` quoting; string literals are single-quoted, so
    a quote-aware scan (single-quoted spans copied verbatim, see :func:`literal_end` for
    ``backslash``) can safely rewrite every double-quoted span. Each identifier's inner text
    (with ``""`` escapes collapsed) is passed to ``replace``; its return value is emitted
    verbatim in place of the original quoted span. Callers own the target quoting/escaping
    (see :func:`rewrite_double_quoted_identifiers` and the BigQuery adapter's
    alias-legalizing variant).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = literal_end(sql, i, backslash=backslash)
            out.append(sql[i:j])
            i = j
            continue
        if ch == '"':
            j = i + 1
            ident: list[str] = []
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        ident.append('"')
                        j += 2
                        continue
                    break
                ident.append(sql[j])
                j += 1
            out.append(replace("".join(ident)))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def rewrite_double_quoted_identifiers(
    sql: str, *, quote: str = "`", backslash: bool = False
) -> str:
    """Re-quote double-quoted identifiers for backtick dialects.

    Spark SQL and BigQuery treat double quotes as string literals and
    need backticks; see :func:`map_double_quoted_identifiers` for the
    scanning contract.
    """
    return map_double_quoted_identifiers(
        sql, lambda name: quote + name.replace(quote, quote + quote) + quote, backslash=backslash
    )


_DIVIDE_NULLIF_RE = re.compile(r"/\s*NULLIF\s*\(")


def float_nullif_divisions(sql: str, *, cast_type: str = "DOUBLE", backslash: bool = False) -> str:
    """Make ``x / NULLIF(y, 0)`` divide as floats, matching DuckDB.

    The compiler guards every ratio it emits with exactly this idiom,
    and DuckDB's ``/`` is always float division. Several warehouses
    diverge — integer division truncates (Postgres, Trino: ``1 / 4 =
    0``) or DECIMAL division loses scale (Spark, Trino) — so
    count-over-count ratios (conversion rates, shares) silently
    collapse or lose precision. Rewriting the guard to
    ``CAST(NULLIF(y, 0) AS <cast_type>)`` promotes the whole division
    to floating point with no effect on already-float ratios.
    ``cast_type`` is the warehouse's float-ish type — ``DOUBLE``
    (Spark, Trino) or ``DOUBLE PRECISION`` (Postgres).

    Quote-aware paren matching (see :func:`literal_end` for ``backslash``);
    ``/ NULLIF(`` occurrences inside single-quoted literals are skipped and nested
    occurrences are rewritten recursively.
    """
    spans = literal_spans(sql, backslash=backslash)

    def _inside_literal(position: int) -> bool:
        return any(start <= position < end for start, end in spans)

    out: list[str] = []
    pos = 0
    while True:
        match = _DIVIDE_NULLIF_RE.search(sql, pos)
        if match is None:
            out.append(sql[pos:])
            return "".join(out)
        if _inside_literal(match.start()):
            out.append(sql[pos : match.end()])
            pos = match.end()
            continue
        depth = 1
        index = match.end()
        while index < len(sql) and depth:
            char = sql[index]
            if char == "'":
                index = literal_end(sql, index, backslash=backslash)
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:  # unbalanced parens — leave the tail untouched
            out.append(sql[pos:])
            return "".join(out)
        out.append(sql[pos : match.start()])
        out.append("/ CAST(NULLIF(")
        inner = sql[match.end() : index - 1]
        out.append(float_nullif_divisions(inner, cast_type=cast_type, backslash=backslash))
        out.append(f") AS {cast_type})")
        pos = index


# NAMEDATALEN-1: PostgreSQL silently truncates longer identifiers, which
# makes two long compiler-generated CTE names that share a 63-byte prefix
# collide ("WITH query name ... specified more than once").
_MAX_IDENTIFIER_BYTES = 63
_IDENT_HASH_CHARS = 10

# Tokens: single-quoted literal | quoted identifier | bare identifier.
_SQL_TOKEN_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")+\"|[A-Za-z_][A-Za-z0-9_]*")


def _shortened_identifier(name: str) -> str:
    """Deterministically shorten a too-long identifier, keeping a readable
    prefix and a content hash so distinct names stay distinct and every
    reference to the same name rewrites identically."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_IDENT_HASH_CHARS]
    keep = _MAX_IDENTIFIER_BYTES - _IDENT_HASH_CHARS - 1
    prefix = name.encode("utf-8")[:keep].decode("utf-8", errors="ignore")
    return f"{prefix}_{digest}"


def _shorten_long_identifiers(sql: str, alias_map: dict[str, str]) -> str:
    """Rewrite identifiers longer than 63 bytes (quoted or bare) to a
    hash-suffixed 63-byte form. PostgreSQL truncates ALL identifiers —
    quoted ones included — at NAMEDATALEN-1 bytes, so without this two
    long CTE names differing only after byte 63 collide. SQL keywords
    are never this long and string literals are skipped, so the rewrite
    is name-renaming only."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("'"):
            return token  # string literal — data, never rewritten
        if token.startswith('"'):
            inner = token[1:-1].replace('""', '"')
            if len(inner.encode("utf-8")) <= _MAX_IDENTIFIER_BYTES:
                return token
            safe = _shortened_identifier(inner)
            alias_map[safe] = inner
            return '"' + safe.replace('"', '""') + '"'
        if len(token.encode("utf-8")) <= _MAX_IDENTIFIER_BYTES:
            return token
        safe = _shortened_identifier(token)
        alias_map[safe] = token
        return safe

    return _SQL_TOKEN_RE.sub(_replace, sql)


def postgres_compat_sql(sql: str) -> tuple[str, dict[str, str]]:
    # PostgreSQL integer division truncates (1 / 4 = 0) while DuckDB's
    # `/` is always float division; the shared pass casts the compiler's
    # NULLIF guard to PG's float type, DOUBLE PRECISION.
    alias_map: dict[str, str] = {}
    rewritten = _shorten_long_identifiers(sql, alias_map)
    return float_nullif_divisions(rewritten, cast_type="DOUBLE PRECISION"), alias_map


# Legal BigQuery column-name characters (conservative classic set —
# letters, digits, underscore; must not start with a digit).
_LEGAL_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_FIELD_CHARS = 128  # well under BigQuery's 300-char field limit


def _safe_field_name(name: str) -> str:
    """Deterministic legal column name for an illegal identifier: a
    sanitized readable prefix plus a content hash, so distinct names
    stay distinct and every reference rewrites identically."""
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = "_" + sanitized
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_IDENT_HASH_CHARS]
    keep = _MAX_FIELD_CHARS - _IDENT_HASH_CHARS - 1
    return f"{sanitized[:keep]}_{digest}"


def bigquery_compat_sql(sql: str) -> tuple[str, dict[str, str]]:
    """Backtick-requote identifiers and legalize illegal field names.

    Returns ``(rewritten_sql, alias_map)`` where ``alias_map`` maps each
    substituted safe name back to the original identifier, for restoring
    result-row keys. The quote-aware scan (single-quoted literals copied
    verbatim) is the shared
    :func:`map_double_quoted_identifiers`
    core; only the legalization + alias mapping is BigQuery-specific.
    """
    alias_map: dict[str, str] = {}

    def _requote(name: str) -> str:
        if _LEGAL_FIELD_RE.match(name):
            return "`" + name + "`"
        safe = _safe_field_name(name)
        alias_map[safe] = name
        return "`" + safe + "`"

    return map_double_quoted_identifiers(sql, _requote, backslash=True), alias_map
