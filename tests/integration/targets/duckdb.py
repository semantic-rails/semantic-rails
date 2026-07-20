"""Reference target: local DuckDB.

The fixture's reference database IS the seeded DuckDB file, so this
target needs no loader and no environment — it always runs, and every
other warehouse's results are compared against it.
"""

from __future__ import annotations

from ..harness import IntegrationTarget

TARGET = IntegrationTarget(
    warehouse="duckdb",
    is_reference=True,
    notes="Local reference — always available; other targets must match its rows.",
)
