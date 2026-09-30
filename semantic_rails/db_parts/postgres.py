"""Registry factory for the Postgres ADBC profile (``postgres_native``)."""

from __future__ import annotations

from typing import Any

from ..errors import SemanticLayerError
from .adbc import POSTGRES_PROFILE, AdbcAdapter
from .base import WarehouseAdapter


def create_adapter(package: Any, *, db_path: str = "") -> WarehouseAdapter:
    """Keep the Postgres connection kind and option vocabulary stable."""
    kind = str(package.connection.kind or "").strip()
    if kind != POSTGRES_PROFILE.connection_kind:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported Postgres connection kind '{kind}'",
            details={"engine": "postgres", "connection_kind": kind},
        )
    return AdbcAdapter(package.connection.options, profile=POSTGRES_PROFILE)
