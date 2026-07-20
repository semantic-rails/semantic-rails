"""BigQuery integration target (``bigquery_native``).

Env vars (mirroring ``.env.example``):

- ``GOOGLE_APPLICATION_CREDENTIALS`` — service-account JSON path
  (BigQuery Data Editor + Job User, scoped to the dataset)
- ``SR_BIGQUERY_PROJECT`` — GCP project id
- ``SR_BIGQUERY_DATASET`` — dataset holding the fixture tables

The dataset name is read from the environment at import time (with a
default) because connection options are plain strings and the option
schema has no ``dataset_env`` indirection — the dataset is a locator,
not a secret.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.bigquery import BigQueryFixtureLoader

_DATASET = os.environ.get("SR_BIGQUERY_DATASET", "").strip() or "semantic_rails_it"

TARGET = IntegrationTarget(
    warehouse="bigquery",
    connection_kind="bigquery_native",
    connection_options={
        "project_env": "SR_BIGQUERY_PROJECT",
        "credentials_file_env": "GOOGLE_APPLICATION_CREDENTIALS",
        "dataset": _DATASET,
    },
    required_env=(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SR_BIGQUERY_PROJECT",
        "SR_BIGQUERY_DATASET",
    ),
    make_loader=BigQueryFixtureLoader,
    notes="BigQuery Sandbox blocks INSERT DML — the fixture loads via parquet load jobs.",
)
