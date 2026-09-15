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

from typing import Any

from ..dialects import BIGQUERY_CONNECTION_OPTIONS
from ..errors import SemanticLayerError, query_execution_error
from ..sql_preparation import PreparedQuery, prepare_query
from .base import WarehouseAdapter, _clip_rows, _limit_timeout_seconds, restore_column_names
from .common import (
    import_driver,
    normalize_connection_options,
    option_or_env,
    redacted_error_details,
    require_missing_env,
)

_ENGINE = "bigquery"
_KIND = "bigquery_native"
_LABEL = "BigQuery"


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
        return self.query_prepared(prepare_query(sql, self.engine), limits=limits)

    def query_prepared(
        self, prepared: PreparedQuery, *, limits: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
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
            job = client.query(prepared.sql, job_config=job_config)
            rows = [dict(row.items()) for row in job.result()]
            return restore_column_names(_clip_rows(rows, limits), prepared)
        except SemanticLayerError:
            raise
        except Exception as exc:
            raise query_execution_error(
                redacted_error_details(self.engine, self.connection_kind, self.options)
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
