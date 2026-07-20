"""Warehouse-adapter base class and limits contract.

Defines :class:`WarehouseAdapter` — the abstract interface every
warehouse adapter implements — and the helpers that translate the
``limits`` dict (``statement_timeout_ms``, ``max_rows``) into adapter
actions. Lives under :mod:`semantic_rails.db_parts` so both
:mod:`semantic_rails.db` (DuckDB) and
:mod:`semantic_rails.db_parts.snowflake` (Snowflake) can implement
the contract without a circular import. The public surface is
re-exported through :mod:`semantic_rails.db`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConnectionCredentialProvider(Protocol):
    """Host-owned provider for in-memory warehouse credentials.

    Implementations are normally bound to one vault/repository lookup and
    return credentials only when an adapter first connects. Values never need
    to enter package YAML, process environment variables, or temporary files.
    Engines pass only non-secret connection identity metadata to the provider.
    """

    def credentials_for(
        self,
        *,
        warehouse: str,
        connection_kind: str,
        connection_name: str,
    ) -> Mapping[str, Any]: ...

    # Providers that retain revealed values may additionally implement
    # ``clear()``. Adapters call it best-effort from ``close()`` before
    # dropping their provider reference; it is intentionally optional so a
    # provider can fetch ephemeral credentials on every call without keeping
    # mutable secret state.


class QueryRows(list[dict[str, Any]]):
    """Result rows with an exact signal that the adapter stopped early."""

    def __init__(self, rows=(), *, truncated: bool = False):
        super().__init__(rows)
        self.truncated = bool(truncated)


class WarehouseAdapter(ABC):
    engine: str
    # Whether this adapter actually honors `limits.statement_timeout_ms`
    # at the database boundary. Adapters that can't enforce timeouts at
    # the warehouse level set this to False; the runtime surfaces a
    # `warnings` entry so the caller learns the limit was best-effort.
    # Defaults to False — adapters that can enforce timeouts must
    # explicitly opt in. (DuckDB: False; SnowflakeCliAdapter: True via
    # session-level ALTER SESSION.)
    supports_statement_timeout: bool = False

    @abstractmethod
    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute SQL and return rows.

        `limits` is an optional dict with these recognized keys:

        - `statement_timeout_ms` — abort the query after N ms (best-effort
          per adapter; check `supports_statement_timeout`)
        - `max_rows` — fetch at most N+1 rows where the driver supports
          bounded cursor reads, return N, and mark truncation

        Per-adapter enforcement is best-effort; backends that don't
        support a given knob ignore it. The default semantics are
        documented in `docs/QUERY_API.md` (request "limits" block).
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


def _clip_rows(rows: list[dict[str, Any]], limits: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not limits:
        return rows
    cap = _limit_max_rows(limits)
    if cap is None:
        return rows
    if len(rows) <= cap:
        return rows
    return QueryRows(rows[:cap], truncated=True)


def _limit_max_rows(limits: dict[str, Any] | None) -> int | None:
    if not limits or not limits.get("max_rows"):
        return None
    try:
        cap = int(limits["max_rows"])
    except (TypeError, ValueError):
        return None
    return cap if cap >= 0 else None


def _limit_timeout_seconds(limits: dict[str, Any] | None) -> int:
    ms = _limit_timeout_milliseconds(limits)
    if ms <= 0:
        return 0
    return max(1, (ms + 999) // 1000)


def _limit_timeout_milliseconds(limits: dict[str, Any] | None) -> int:
    if not limits:
        return 0
    raw = limits.get("statement_timeout_ms")
    if raw is None:
        return 0
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return 0
    if ms <= 0:
        return 0
    return ms
