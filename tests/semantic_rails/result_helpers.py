"""Decode public result cells by their metadata for typed reference assertions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def assert_plan_held(payload: dict[str, Any], code: str) -> None:
    """Pin the reason a diagnostic draft cannot be executed through plan."""
    assert payload["status"] == "low_confidence", payload
    assert payload["why"]["code"] == code, payload
    assert "execute" not in payload.get("next", {}).get("ready_for", []), payload


def held_candidate(payload: dict[str, Any], code: str) -> dict[str, Any]:
    """Read a held draft from the test-only candidate envelope."""
    assert not payload["candidates"], payload
    row = payload["blocked"][0]
    assert row["why_blocked"]["code"] == code, payload
    return row


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
