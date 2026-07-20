"""Athena integration target.

Locators use ``*_env`` indirection (values live in ``.env`` — see
``.env.example``); AWS credentials are NOT connection options — boto3
and PyAthena pick them up from the ambient credential chain
(``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``), which is why they
appear in ``required_env`` but never in ``connection_options``.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.athena import AthenaFixtureLoader

TARGET = IntegrationTarget(
    warehouse="athena",
    connection_kind="athena_native",
    connection_options={
        "region_env": "AWS_REGION",
        "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
        "database": os.environ.get("SR_ATHENA_DATABASE", "sr_jaffle"),
    },
    required_env=(
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SR_ATHENA_S3_STAGING_DIR",
        "SR_ATHENA_DATABASE",
    ),
    make_loader=AthenaFixtureLoader,
    notes=(
        "Fixture staged as parquet under the S3 staging bucket and exposed "
        "as Hive external tables; percentiles are exact (sorted ARRAY_AGG "
        "linear interpolation — Trino has no PERCENTILE_CONT aggregate)."
    ),
)
