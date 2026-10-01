"""AWS Athena warehouse adapter.

PyAthena is a PEP 249 driver, so the adapter is a thin
:class:`~semantic_rails.db_parts.common.DbApiAdapter` subclass: it
implements ``_create_connection``. SQL preparation belongs to the dialect.
Connection locators
(``region`` / ``s3_staging_dir``) may be literals or env-indirected
(``*_env``); AWS credentials are NEVER connection options — they come
from the ambient boto3 credential chain (``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` env vars, shared config, instance profile).

Athena has no per-statement timeout mechanism (query lifetimes are
governed by workgroup limits), so ``supports_statement_timeout`` stays
False and the runtime surfaces best-effort-limit warnings.
"""

from __future__ import annotations

import time
from typing import Any

from ..dialects import ATHENA_CONNECTION_OPTIONS
from ..errors import SemanticLayerError
from .base import WarehouseAdapter
from .common import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DbApiAdapter,
    client_wait_timeout,
    import_driver,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
    timeout_option,
)

_LABEL = "Athena"


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

    def _query_cursor(self, connection: Any, timeout_seconds: int) -> Any:
        read_timeout = timeout_option(
            self.options,
            "read_timeout_seconds",
            DEFAULT_READ_TIMEOUT_SECONDS,
            engine=self.engine,
            connection_kind=self.connection_kind,
            label=_LABEL,
        )
        deadline = time.monotonic() + client_wait_timeout(read_timeout, timeout_seconds)

        def on_poll(execution: Any) -> None:
            if (
                execution.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}
                and time.monotonic() >= deadline
            ):
                cursor.cancel()
                raise TimeoutError("Athena query polling timed out")

        cursor = connection.cursor(on_poll=on_poll)
        return cursor

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
        botocore_config = import_driver(
            "botocore.config",
            extra="athena",
            engine=self.engine,
            connection_kind=self.connection_kind,
        )
        # Default the namespace from connection options so UNQUALIFIED
        # table names (jaffle_order, …) resolve. AWS credentials come
        # from the ambient boto3 chain — never from options.
        return driver.connect(
            s3_staging_dir=s3_staging_dir,
            region_name=region,
            schema_name=self.options.get("database") or "default",
            work_group=self.options.get("workgroup") or None,
            config=botocore_config.Config(
                connect_timeout=timeout_option(
                    self.options,
                    "connect_timeout_seconds",
                    DEFAULT_CONNECT_TIMEOUT_SECONDS,
                    engine=self.engine,
                    connection_kind=self.connection_kind,
                    label=_LABEL,
                ),
                read_timeout=timeout_option(
                    self.options,
                    "read_timeout_seconds",
                    DEFAULT_READ_TIMEOUT_SECONDS,
                    engine=self.engine,
                    connection_kind=self.connection_kind,
                    label=_LABEL,
                ),
            ),
        )


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Registry entry point for the athena warehouse (see dialects.py)."""
    return AthenaAdapter(package.connection.options)
