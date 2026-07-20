"""AWS Athena warehouse adapter.

PyAthena is a PEP 249 driver, so the adapter is a thin
:class:`~semantic_rails.db_parts.common.DbApiAdapter` subclass: it
implements ``_create_connection`` plus a documented, literal-aware
Trino compatibility pass over the rendered SQL (see
``_athena_compat_sql``). Connection locators
(``region`` / ``s3_staging_dir``) may be literals or env-indirected
(``*_env``); AWS credentials are NEVER connection options — they come
from the ambient boto3 credential chain (``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` env vars, shared config, instance profile).

Athena has no per-statement timeout mechanism (query lifetimes are
governed by workgroup limits), so ``supports_statement_timeout`` stays
False and the runtime surfaces best-effort-limit warnings.
"""

from __future__ import annotations

import re
from typing import Any

from ..dialects import ATHENA_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter
from .common import (
    DbApiAdapter,
    float_nullif_divisions,
    import_driver,
    literal_spans,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
)

_LABEL = "Athena"


# ---------------------------------------------------------------------------
# Athena (Trino) compatibility pass — two semantics-preserving rewrites of
# the compiler's rendered SQL, applied just before execution. Both
# compensate for hard Trino behaviors the portable SQL AST cannot express
# (mirrors the documented pattern in semantic_rails/db_parts/postgres.py):
# the temporal-literal typing below, plus the shared literal-aware
# `common.float_nullif_divisions` pass with Trino's DOUBLE cast (Trino
# integer division truncates and decimal division keeps the operand
# scale, while DuckDB's `/` is always float division).
# The pass never rewrites text INSIDE string literals.
# ---------------------------------------------------------------------------

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


class AthenaAdapter(DbApiAdapter):
    engine = "athena"
    connection_kind = "athena_native"
    # Athena exposes no per-statement timeout (workgroup-level limits
    # only), so limits.statement_timeout_ms is best-effort.
    supports_statement_timeout = False

    def __init__(self, options: dict[str, Any] | None = None):
        super().__init__()
        self.options = normalize_connection_options(
            "athena",
            self.connection_kind,
            options or {},
            ATHENA_CONNECTION_OPTIONS,
            label=_LABEL,
        )

    def _create_connection(self) -> Any:
        driver = import_driver(
            "pyathena",
            extra="athena",
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        missing_env: list[str] = []
        region = option_or_env(self.options, "region", missing_env)
        s3_staging_dir = option_or_env(self.options, "s3_staging_dir", missing_env)
        require_missing_env(
            missing_env, engine=self.engine, connection_kind=self.connection_kind, label=_LABEL
        )
        missing_options = [
            name
            for name, value in (("region", region), ("s3_staging_dir", s3_staging_dir))
            if not value
        ]
        if missing_options:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Athena connection requires region and s3_staging_dir "
                "(literal or *_env indirection)",
                details={
                    "engine": self.engine,
                    "connection_kind": self.connection_kind,
                    "missing_options": missing_options,
                },
            )
        # Default the namespace from connection options so UNQUALIFIED
        # table names (jaffle_order, …) resolve. AWS credentials come
        # from the ambient boto3 chain — never from options.
        return driver.connect(
            s3_staging_dir=s3_staging_dir,
            region_name=region,
            schema_name=self.options.get("database") or "default",
            work_group=self.options.get("workgroup") or None,
        )

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return super().query(_athena_compat_sql(sql), limits=limits)


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the athena warehouse (see dialects.py)."""
    return AthenaAdapter(package.connection.options)
