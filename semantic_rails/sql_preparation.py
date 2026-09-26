"""Driver-free final SQL preparation shared by compilation and direct adapter calls.

The compiler owns the executable statement and immutable result-column mapping.
Adapters executing a PreparedQuery must send its SQL unchanged; session timeout
commands and bounded row fetching remain adapter concerns.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .errors import SemanticLayerError

if TYPE_CHECKING:
    from .request_context import TrustedAttributes

ParameterValue = str | int | bool
_SLOT_TYPES: dict[str, type] = {"string": str, "integer": int, "boolean": bool}


@dataclass(frozen=True)
class ParameterSlot:
    """One positional ``?`` placeholder, bound per request from a trusted attribute.

    The value must have exactly the declared type: ``string``, ``integer`` or
    ``boolean``. A compiled statement holds only slots, never their values.
    """

    attribute: str
    type: Literal["string", "integer", "boolean"]

    def __post_init__(self) -> None:
        if self.type not in _SLOT_TYPES:
            raise ValueError(f"Unsupported parameter type {self.type!r}.")


@dataclass(frozen=True)
class PreparedQuery:
    """Executable SQL with physical-to-semantic result column names.

    ``parameters`` lists the statement's ``?`` placeholders in order. Only an
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


def bind_parameters(
    prepared: PreparedQuery, attributes: TrustedAttributes
) -> tuple[ParameterValue, ...]:
    """This request's values for the statement's slots, from its trusted attributes."""
    values = [attributes.get(slot.attribute) for slot in prepared.parameters]
    return checked_parameter_values(prepared, values)


def checked_parameter_values(
    prepared: PreparedQuery, values: Sequence[Any]
) -> tuple[ParameterValue, ...]:
    """Values matching the slots exactly: no NULL, no coercion, no extra or missing value."""
    if len(values) != len(prepared.parameters):
        raise parameters_denied("parameter_count_mismatch")
    for slot, value in zip(prepared.parameters, values, strict=True):
        if value is None:
            raise parameters_denied("missing_attribute", attribute=slot.attribute)
        if type(value) is not _SLOT_TYPES[slot.type]:
            raise parameters_denied("attribute_type_mismatch", attribute=slot.attribute)
    return tuple(values)


def prepare_query(sql: str, warehouse: str) -> PreparedQuery:
    """Apply a warehouse's final syntax/precision rules once, before execution."""
    alias_map: dict[str, str] = {}
    if warehouse == "postgres":
        sql, alias_map = _postgres_compat_sql(sql)
    elif warehouse == "bigquery":
        sql, alias_map = _bigquery_compat_sql(sql)
    elif warehouse == "databricks":
        sql = float_nullif_divisions(rewrite_double_quoted_identifiers(sql))
    elif warehouse == "athena":
        sql = _athena_compat_sql(sql)
    elif warehouse == "snowflake":
        sql = float_nullif_divisions(sql)
    return PreparedQuery(sql, tuple(alias_map.items()))


def map_double_quoted_identifiers(sql: str, replace: Callable[[str], str]) -> str:
    """Quote-aware scanning core for identifier-re-quoting compat passes.

    The renderer emits ANSI ``"identifier"`` quoting; the compiler
    renders string LITERALS with single quotes only, so a quote-aware
    scan (single-quoted spans copied verbatim, including ``''``
    escapes) can safely rewrite every double-quoted span.

    This scan deliberately does NOT treat ``\\`` as an escape, even
    though its callers (BigQuery, Spark) are warehouses that do. It
    stays correct because the renderer always emits backslashes
    *doubled* on those dialects — see
    :func:`semantic_rails.dialects.backslash_escaped_string_literal` —
    so a lone ``\\'`` can never appear and the literal boundaries this
    scan finds are the same ones the warehouse finds. Teaching it about
    backslash escapes would instead break the ANSI-conforming callers,
    where ``\\`` is an ordinary character. Each
    identifier's inner text (with ``""`` escapes collapsed) is passed
    to ``replace``; its return value is emitted verbatim in place of
    the original quoted span. Callers own the target quoting/escaping
    (see :func:`rewrite_double_quoted_identifiers` and the BigQuery
    adapter's alias-legalizing variant).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1])
            i = j + 1
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


def rewrite_double_quoted_identifiers(sql: str, *, quote: str = "`") -> str:
    """Re-quote double-quoted identifiers for backtick dialects.

    Spark SQL and BigQuery treat double quotes as string literals and
    need backticks; see :func:`map_double_quoted_identifiers` for the
    scanning contract.
    """
    return map_double_quoted_identifiers(
        sql, lambda name: quote + name.replace(quote, quote + quote) + quote
    )


_DIVIDE_NULLIF_RE = re.compile(r"/\s*NULLIF\s*\(")
_SINGLE_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def literal_spans(sql: str) -> list[tuple[int, int]]:
    """Spans of single-quoted string literals (with ``''`` escapes)."""
    return [m.span() for m in _SINGLE_QUOTED_LITERAL_RE.finditer(sql)]


def float_nullif_divisions(sql: str, *, cast_type: str = "DOUBLE") -> str:
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

    Quote-aware paren matching; ``/ NULLIF(`` occurrences inside
    single-quoted literals are skipped and nested occurrences are
    rewritten recursively.
    """
    spans = literal_spans(sql)

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
        in_string = False
        while index < len(sql) and depth:
            char = sql[index]
            if in_string:
                if char == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 1  # escaped quote inside the literal
                    else:
                        in_string = False
            elif char == "'":
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:  # unbalanced parens — leave the tail untouched
            out.append(sql[pos:])
            return "".join(out)
        out.append(sql[pos : match.start()])
        out.append("/ CAST(NULLIF(")
        out.append(float_nullif_divisions(sql[match.end() : index - 1], cast_type=cast_type))
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


def _postgres_compat_sql(sql: str) -> tuple[str, dict[str, str]]:
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


def _bigquery_compat_sql(sql: str) -> tuple[str, dict[str, str]]:
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

    return map_double_quoted_identifiers(sql, _requote), alias_map


# A comparison operator immediately followed by a single-quoted ISO
# date / timestamp literal — the exact shape of the compiler's
# time-window predicates (`ts >= '2016-09-01' AND ts < '2017-01-01'`).
_ISO_TEMPORAL_LITERAL = r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)?"
_COMPARE_TEMPORAL_RE = re.compile(
    rf"(?P<op><=|>=|<>|!=|<|>|=)(?P<ws>\s*)'(?P<lit>{_ISO_TEMPORAL_LITERAL})'"
)


def _typed_temporal_comparison_literals(sql: str) -> str:
    """Tag ISO date/timestamp literals in comparisons as ``TIMESTAMP``.

    The compiler renders time-window bounds as bare string literals
    (``ts >= '2016-09-01'``). DuckDB implicitly coerces the varchar to
    the column's temporal type; Trino refuses with ``TYPE_MISMATCH:
    Cannot apply operator: timestamp(3) <= varchar(10)``. Rewriting the
    literal to ``TIMESTAMP '2016-09-01'`` restores the comparison with
    DuckDB's semantics (DATE operands still compare fine — Trino
    coerces DATE to TIMESTAMP). Only literals that (a) immediately
    follow a comparison operator and (b) parse as an ISO date or
    timestamp are touched, and operators inside string literals are
    skipped — ordinary string comparisons are never rewritten."""
    spans = literal_spans(sql)

    def _inside_literal(position: int) -> bool:
        return any(start <= position < end for start, end in spans)

    def _replace(match: re.Match[str]) -> str:
        if _inside_literal(match.start()):
            return match.group(0)
        op, ws, lit = match.group("op"), match.group("ws"), match.group("lit")
        return f"{op}{ws or ' '}TIMESTAMP '{lit}'"

    return _COMPARE_TEMPORAL_RE.sub(_replace, sql)


def _athena_compat_sql(sql: str) -> str:
    return float_nullif_divisions(_typed_temporal_comparison_literals(sql), cast_type="DOUBLE")
