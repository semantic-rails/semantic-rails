"""Decode public result cells by their metadata for typed reference assertions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def typed_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Restore decimal, date and timestamp values without guessing from text."""
    decoders = {"decimal": Decimal, "date": date.fromisoformat, "timestamp": datetime.fromisoformat}
    return [
        {
            key: decoders[kind](value) if value is not None and kind in decoders else value
            for key, value in row.items()
            for kind in [result["column_types"][key]["type"]]
        }
        for row in result["rows"]
    ]
