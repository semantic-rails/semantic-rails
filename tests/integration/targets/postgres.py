"""Postgres conformance target.

Local docker Postgres 16 by default (``make warehouses-up``; see
``.env.example`` for the ``SR_POSTGRES_*`` variables) — any reachable
Postgres works. Host/user/password use ``*_env`` indirection so the
package options never carry values; port/database/schema are plain
locators read from the environment with the docker-compose defaults.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.postgres import PostgresFixtureLoader

TARGET = IntegrationTarget(
    warehouse="postgres",
    connection_kind="postgres_native",
    connection_options={
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "schema": os.environ.get("SR_POSTGRES_SCHEMA", "public"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    },
    required_env=("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD"),
    make_loader=PostgresFixtureLoader,
    notes="Local docker Postgres 16 (make warehouses-up) or any reachable instance.",
)
