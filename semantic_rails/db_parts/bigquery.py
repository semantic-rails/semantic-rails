"""BigQuery warehouse adapter (``bigquery_native``).

``google-cloud-bigquery`` is not a PEP 249 driver, so this adapter
implements :class:`WarehouseAdapter` directly while reusing the shared
helpers in :mod:`semantic_rails.db_parts.common` (option normalization,
env indirection, lazy driver import, bounded/redacted errors) and the
``limits`` helpers in :mod:`semantic_rails.db_parts.base`.

Connection contract:

- Credentials come from ``credentials_file`` / ``credentials_file_env``
  (service-account JSON) when given, otherwise Application Default
  Credentials (``GOOGLE_APPLICATION_CREDENTIALS`` / gcloud auth).
- The project comes from ``project`` / ``project_env`` (falling back to
  the ADC default project).
- Every query runs with ``QueryJobConfig(default_dataset=
  "<project>.<dataset>")`` so unqualified table names (``jaffle_order``)
  resolve inside the configured dataset.
- ``limits.statement_timeout_ms`` maps to ``QueryJobConfig
  .job_timeout_ms`` — BigQuery's native job timeout: the service
  attempts to stop the job server-side once the timeout elapses.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..dialects import BIGQUERY_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter, _clip_rows, _limit_timeout_seconds
from .common import (
    bounded_error_text,
    import_driver,
    map_double_quoted_identifiers,
    normalize_connection_options,
    option_or_env,
    redacted_error_details,
    require_missing_env,
)

_ENGINE = "bigquery"
_KIND = "bigquery_native"
_LABEL = "BigQuery"


# ---------------------------------------------------------------------------
# GoogleSQL compatibility pass — two semantics-preserving rewrites of the
# compiler's rendered SQL, applied just before execution (mirroring the
# Postgres adapter's documented compat pass). Both compensate for hard
# GoogleSQL limits the portable SQL AST cannot express; string literals
# (single-quoted, including '' escapes) are never touched.
#
# 1. The renderer emits ANSI "double-quoted" identifiers (e.g.
#    AS "dimension.jaffle_store_name"); GoogleSQL treats double quotes
#    as STRING LITERALS, so every double-quoted identifier is re-quoted
#    with backticks (shared helper semantics).
# 2. BigQuery column names — even backtick-quoted, even with flexible
#    column names enabled — may not contain '.' or other punctuation
#    ("Invalid field name … allowed characters"). Compiler aliases like
#    "dimension.jaffle_store_name" are therefore unrepresentable as
#    output fields. Each illegal identifier is rewritten to a
#    deterministic legal name (sanitized prefix + content hash, à la
#    the Postgres long-identifier shortening) so every reference —
#    alias definition, GROUP BY, ORDER BY, outer SELECT — rewrites
#    identically, and the adapter maps result-row keys BACK to the
#    original names so callers see the contract aliases unchanged.
# ---------------------------------------------------------------------------

# Legal BigQuery column-name characters (conservative classic set —
# letters, digits, underscore; must not start with a digit).
_LEGAL_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_IDENT_HASH_CHARS = 10
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
    :func:`semantic_rails.db_parts.common.map_double_quoted_identifiers`
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


class BigQueryNativeAdapter(WarehouseAdapter):
    engine = _ENGINE
    connection_kind = _KIND
    # Honors limits.statement_timeout_ms via QueryJobConfig.job_timeout_ms,
    # BigQuery's server-side job timeout.
    supports_statement_timeout = True

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = normalize_connection_options(
            _ENGINE, _KIND, options or {}, BIGQUERY_CONNECTION_OPTIONS, label=_LABEL
        )
        self._client: Any = None

    # -- connection -----------------------------------------------------
    def _bigquery(self) -> Any:
        return import_driver(
            "google.cloud.bigquery",
            extra="bigquery",
            engine=self.engine,
            connection_kind=self.connection_kind,
        )

    def _resolve_project(self, missing_env: list[str] | None = None) -> str:
        return option_or_env(self.options, "project", missing_env)

    def _resolve_credentials_file(self, missing_env: list[str] | None = None) -> str:
        return option_or_env(self.options, "credentials_file", missing_env)

    def client(self) -> Any:
        """The lazily created ``google.cloud.bigquery.Client``.

        Public so the integration fixture loader can reuse the same
        connection path for load jobs and dataset creation.
        """
        if self._client is None:
            bigquery = self._bigquery()
            missing_env: list[str] = []
            project = self._resolve_project(missing_env)
            credentials_path = self._resolve_credentials_file(missing_env)
            require_missing_env(
                missing_env,
                engine=self.engine,
                connection_kind=self.connection_kind,
                label=_LABEL,
            )
            kwargs: dict[str, Any] = {}
            if project:
                kwargs["project"] = project
            if self.options.get("location"):
                kwargs["location"] = self.options["location"]
            if credentials_path:
                service_account = import_driver(
                    "google.oauth2.service_account",
                    extra="bigquery",
                    engine=self.engine,
                    connection_kind=self.connection_kind,
                )
                kwargs["credentials"] = service_account.Credentials.from_service_account_file(
                    credentials_path
                )
            # No explicit credentials -> Application Default Credentials
            # (GOOGLE_APPLICATION_CREDENTIALS / gcloud auth).
            self._client = bigquery.Client(**kwargs)
        return self._client

    def default_dataset_id(self) -> str:
        """``project.dataset`` for ``QueryJobConfig.default_dataset``.

        Defaulting the namespace is what lets unqualified table names
        like ``jaffle_order`` resolve. Empty when no dataset option is
        configured.
        """
        dataset = self.options.get("dataset", "")
        if not dataset:
            return ""
        if "." in dataset:
            return dataset
        project = self._resolve_project() or str(getattr(self.client(), "project", "") or "")
        return f"{project}.{dataset}" if project else dataset

    # -- WarehouseAdapter contract ---------------------------------------
    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        timeout_s = _limit_timeout_seconds(limits)
        try:
            client = self.client()
            bigquery = self._bigquery()
            job_config = bigquery.QueryJobConfig()
            default_dataset = self.default_dataset_id()
            if default_dataset:
                job_config.default_dataset = default_dataset
            if timeout_s > 0:
                job_config.job_timeout_ms = timeout_s * 1000
            compat_sql, alias_map = _bigquery_compat_sql(sql)
            job = client.query(compat_sql, job_config=job_config)
            rows = [
                {alias_map.get(key, key): value for key, value in row.items()}
                for row in job.result()
            ]
            return _clip_rows(rows, limits)
        except SemanticLayerError:
            raise
        except Exception as exc:
            # Redacted envelope: engine, connection kind, option KEYS,
            # and bounded driver text only — never option values, raw
            # SQL, or result rows.
            raise SemanticLayerError(
                "QUERY_EXECUTION_ERROR",
                f"BigQuery query execution failed: {bounded_error_text(str(exc))}",
                details=redacted_error_details(self.engine, self.connection_kind, self.options),
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the bigquery warehouse (see dialects.py)."""
    kind = str(package.connection.kind or "").strip()
    if kind != _KIND:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported BigQuery connection kind '{kind}'",
            details={"engine": _ENGINE, "connection_kind": kind},
        )
    return BigQueryNativeAdapter(package.connection.options)
