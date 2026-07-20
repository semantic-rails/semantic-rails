"""ClickHouse integration target.

Runs against any reachable ClickHouse server (e.g. the local
docker-compose service). Required env vars (see .env.example):
``SR_CLICKHOUSE_HOST``, ``SR_CLICKHOUSE_USER``,
``SR_CLICKHOUSE_PASSWORD``; ``SR_CLICKHOUSE_PORT`` and
``SR_CLICKHOUSE_DATABASE`` default to 8123 / sr_jaffle.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.clickhouse import ClickHouseFixtureLoader

TARGET = IntegrationTarget(
    warehouse="clickhouse",
    connection_kind="clickhouse_native",
    connection_options={
        "host_env": "SR_CLICKHOUSE_HOST",
        "port": os.environ.get("SR_CLICKHOUSE_PORT", "8123"),
        "database": os.environ.get("SR_CLICKHOUSE_DATABASE", "sr_jaffle"),
        "user_env": "SR_CLICKHOUSE_USER",
        "password_env": "SR_CLICKHOUSE_PASSWORD",
    },
    required_env=("SR_CLICKHOUSE_HOST", "SR_CLICKHOUSE_USER", "SR_CLICKHOUSE_PASSWORD"),
    make_loader=ClickHouseFixtureLoader,
    notes="HTTP protocol via clickhouse-connect; non-equi JOIN ON enabled per session.",
)
