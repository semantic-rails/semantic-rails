"""``zero_through``: how far a table counts as loaded, so its empty periods read 0 up to there.

A model's ``zero_through``, else the project's ``defaults.zero_through``, is one of:

- ``data`` (the engine default): the last row at or before ``now``; no record is kept.
- ``now``: the table counts as loaded through the request's ``now``.
- ``{loaded: <model>.<field>}``: ``MAX`` of a date or timestamp field of a model (usually a
  load log, never the facts themselves) that records how far the load has completed.

The model's entity and every measure of the model carry the resolved edge
(``EntityConfig.zero_through``, ``MeasureConfig.zero_through``); only an additive sum or count
reads it (``compiler_parts.empty_groups``), and an inline aggregate over the model's rows takes
the entity's (``compiler_parts.bind``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..errors import SemanticLayerError
from ..schema import ZeroThrough

SHAPE = "zero_through: data | now | {loaded: <model>.<date or timestamp field>}"
# Where loaded coverage never runs, a declared edge could change nothing: refused, not ignored.
_NO_COVERAGE = {"clickhouse": "ClickHouse"}
# A field a load log may name: (relation, column, data type, storage zone), or why it can't.
FieldOf = Callable[[str, str], tuple[str, str, str, str] | str]


def zero_through(
    value: Any, *, where: str, path: str, warehouse: str, field_of: FieldOf
) -> ZeroThrough | None:
    """The edge ``value`` declares at ``where`` (None for ``data``), or ``INVALID_CONFIG`` with
    the path and the shape to write."""

    def invalid(problem: str) -> SemanticLayerError:
        return SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: {where} {problem}. Write {SHAPE}.",
            details={"path": where},
        )

    if value is None or value == "data":
        return None
    if warehouse in _NO_COVERAGE:
        raise invalid(
            f"can only be data on {_NO_COVERAGE[warehouse]}, which reads no loaded coverage"
        )
    if value == "now":
        return ZeroThrough(kind="now")
    loaded = (
        value.get("loaded") if isinstance(value, Mapping) and set(value) == {"loaded"} else None
    )
    model, _, name = str(loaded or "").partition(".")
    if not isinstance(loaded, str) or not model or not name:
        raise invalid(f"must be data, now or {{loaded: <model>.<field>}}, not {value!r}")
    found = field_of(model, name)
    if isinstance(found, str):
        raise invalid(f"names {loaded}, {found}")
    relation, column, data_type, timezone = found
    return ZeroThrough("loaded", loaded, relation, column, data_type, timezone)


def field_reader(
    model_rows: Mapping[str, Mapping[str, Any]],
    pipelines: Mapping[str, str],
    fields: Mapping[tuple[str, str], tuple[str, str, str]],
) -> FieldOf:
    """``field_of`` over the loaded package: ``fields`` maps (model, field key) to the field's
    column, data type and storage zone; a field is also found by its column."""

    def field_of(model: str, name: str) -> tuple[str, str, str, str] | str:
        if model not in model_rows:
            return f"but there is no model {model!r}"
        relation = str(model_rows[model].get("relation", "") or "").strip()
        if relation in pipelines:
            return "whose model reads a relation pipeline; name a model over a table or view"
        found = fields.get((model, name)) or next(
            (row for (owner, _), row in fields.items() if owner == model and row[0] == name),
            None,
        )
        if found is None or found[1] not in {"date", "timestamp"}:
            return (
                f"which is no date or timestamp field of model {model!r}; declare it under the "
                "model's times: with kind: date or timestamp"
            )
        return (relation, *found)

    return field_of
