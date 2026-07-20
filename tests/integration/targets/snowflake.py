"""Snowflake integration target.

Uses the snowflake_native direct-connection path (account/user/password
env indirection). Skips unless the SR_SNOWFLAKE_* variables are set —
no Snowflake credentials ship with the local/cloud sweep by default.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.snowflake import SnowflakeFixtureLoader

TARGET = IntegrationTarget(
    warehouse="snowflake",
    connection_kind="snowflake_native",
    connection_options={
        "account_env": "SR_SNOWFLAKE_ACCOUNT",
        "user_env": "SR_SNOWFLAKE_USER",
        "password_env": "SR_SNOWFLAKE_PASSWORD",
        "database": os.environ.get("SR_SNOWFLAKE_DATABASE", "SR_JAFFLE"),
        "schema": os.environ.get("SR_SNOWFLAKE_SCHEMA", "PUBLIC"),
        "warehouse": os.environ.get("SR_SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    },
    required_env=("SR_SNOWFLAKE_ACCOUNT", "SR_SNOWFLAKE_USER", "SR_SNOWFLAKE_PASSWORD"),
    make_loader=SnowflakeFixtureLoader,
    notes="Pre-existing first-class connector; covered by the same conformance contract.",
)
