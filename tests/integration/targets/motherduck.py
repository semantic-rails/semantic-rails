"""MotherDuck integration target.

Runs when ``SR_MOTHERDUCK_TOKEN`` is set (see ``.env.example``);
otherwise the conformance suite skips it. ``SR_MOTHERDUCK_DATABASE``
optionally overrides the scratch database name (default ``sr_jaffle``);
the adapter creates it lazily if absent.
"""

from __future__ import annotations

import os

from ..harness import IntegrationTarget
from ..loaders.motherduck import MotherDuckFixtureLoader

TARGET = IntegrationTarget(
    warehouse="motherduck",
    connection_kind="motherduck_native",
    connection_options={
        "token_env": "SR_MOTHERDUCK_TOKEN",
        "database": os.environ.get("SR_MOTHERDUCK_DATABASE", "").strip() or "sr_jaffle",
    },
    required_env=("SR_MOTHERDUCK_TOKEN",),
    make_loader=MotherDuckFixtureLoader,
    notes="MotherDuck — DuckDB SQL over md:; bulk load via local read_parquet.",
)
