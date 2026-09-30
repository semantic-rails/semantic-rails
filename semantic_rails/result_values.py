"""Warehouse-independent JSON values for public result rows."""

from __future__ import annotations

import base64
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from .errors import SemanticLayerError


def _refuse() -> SemanticLayerError:
    # Never include the value: result rows can contain sensitive data.
    return SemanticLayerError(
        "RESULT_VALUE_UNSUPPORTED",
        "A result value cannot be represented by the JSON result contract.",
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _number(value: int | float | Decimal) -> int | float | str:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal.is_finite():
        raise _refuse()
    number = float(decimal)
    if math.isfinite(number):
        if number.is_integer() and Decimal(int(number)) == decimal:
            return int(number)
        # JSON uses the float's shortest decimal representation. Converting
        # that representation back must preserve the original decimal value.
        if Decimal(str(number)) == decimal:
            return number
    return _decimal_text(decimal)


def _interval(value: timedelta) -> str:
    micros = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    sign = "-" if micros < 0 else ""
    days, micros = divmod(abs(micros), 86_400_000_000)
    hours, micros = divmod(micros, 3_600_000_000)
    minutes, micros = divmod(micros, 60_000_000)
    seconds, fraction = divmod(micros, 1_000_000)
    second_text = str(seconds)
    if fraction:
        second_text += "." + f"{fraction:06d}".rstrip("0")
    return f"{sign}P{days}DT{hours}H{minutes}M{second_text}S"


def _value(value: Any) -> tuple[Any, dict[str, str]]:
    if value is None:
        return None, {"type": "null"}
    if isinstance(value, bool):
        return value, {"type": "boolean"}
    if isinstance(value, (int, float, Decimal)):
        return _number(value), {"type": "decimal"}
    if isinstance(value, datetime):
        aware = value.utcoffset() is not None
        if aware:
            try:
                value = value.astimezone(UTC)
            except (OverflowError, ValueError):
                raise _refuse() from None
        return value.isoformat(), {"type": "timestamp", "timezone": "aware" if aware else "naive"}
    if isinstance(value, date):
        return value.isoformat(), {"type": "date"}
    if isinstance(value, time):
        aware = value.utcoffset() is not None
        if aware:
            value = datetime.combine(date(2000, 1, 1), value).astimezone(UTC).timetz()
        return value.isoformat(), {"type": "time", "timezone": "aware" if aware else "naive"}
    if isinstance(value, timedelta):
        return _interval(value), {"type": "interval"}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(value).decode("ascii"), {"type": "binary", "encoding": "base64"}
    if isinstance(value, UUID):
        return str(value), {"type": "uuid"}
    if isinstance(value, str):
        return value, {"type": "string"}
    if isinstance(value, (list, dict)):
        # Structured columns remain structured. Scalars requiring typed
        # string metadata inside them are refused here.
        return _json_container(value), {"type": "array" if isinstance(value, list) else "object"}
    raise _refuse()


def _json_container(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = _number(value)
        if isinstance(number, str):
            raise _refuse()
        return number
    if isinstance(value, list):
        return [_json_container(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_container(item) for key, item in value.items()}
    raise _refuse()


def _declared_value(value: Any, declared_type: str) -> Any:
    """Restore explicit semantic types for adapters that return JSON text.

    Never guess from a string's contents: numeric-looking text remains text.
    """
    if not isinstance(value, str):
        return value
    try:
        if declared_type in {
            "decimal",
            "number",
            "integer",
            "float",
            "count",
            "currency",
            "percent",
        }:
            return Decimal(value)
        if declared_type in {"timestamp", "datetime"}:
            return datetime.fromisoformat(value)
        if declared_type == "time":
            return time.fromisoformat(value)
        if declared_type == "date":
            return date.fromisoformat(value)
    except (ValueError, ArithmeticError):
        raise _refuse() from None
    return value


def result_rows(
    rows: list[dict[str, Any]], *, output_columns: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Encode rows once, retaining observed logical types beside their values.

    All public row producers use this boundary, including injected adapters.
    Nulls do not override a non-null type. Conflicting column types refuse
    rather than making numeric strings indistinguishable from text.
    """
    encoded: list[dict[str, Any]] = []
    types: dict[str, dict[str, str]] = {}
    declared = {
        column["field"]: (
            "timestamp"
            if str(column.get("semantic_id", "")).startswith("temporal_role.")
            else column.get("type", "")
        )
        for column in output_columns or []
    }
    for row in rows:
        output: dict[str, Any] = {}
        for column, value in row.items():
            declared_type = declared.get(column, "")
            value = _declared_value(value, declared_type)
            # Some engines return DATE for a midnight temporal bucket and
            # others TIMESTAMP. Its semantic output type is still a timestamp.
            if (
                declared_type in ("timestamp", "datetime")
                and isinstance(value, date)
                and not isinstance(value, datetime)
            ):
                value = datetime.combine(value, time())
            item, metadata = _value(value)
            prior = types.get(column, {"type": "null"})
            if prior["type"] == "null":
                types[column] = metadata
            elif metadata["type"] != "null" and metadata != prior:
                raise _refuse()
            output[column] = item
        encoded.append(output)
    return {"rows": encoded, "column_types": types}
