"""Shared ceilings for auxiliary value reads."""

from __future__ import annotations

import os

MAX_VALID_VALUES_LIMIT = 1_000
MAX_VALID_VALUES_OFFSET = 100_000


def _env_cap(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def max_valid_values_limit() -> int:
    return _env_cap("SEMANTIC_RAILS_MAX_VALID_VALUES_LIMIT", MAX_VALID_VALUES_LIMIT)


def max_valid_values_offset() -> int:
    return _env_cap("SEMANTIC_RAILS_MAX_VALID_VALUES_OFFSET", MAX_VALID_VALUES_OFFSET)
