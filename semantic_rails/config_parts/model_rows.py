"""Model keys, relations, dimension kinds and entity displays."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ..schema import DimensionConfig, EntityConfig, RelationConfig, RelationPipelineStep
from .row_values import _ensure_list, _normalize_meta, _slug, _titleize
from .shape_checks import RELATION_STEP_KEYS


def _key_columns_and_role(value: Any, *, default_role: str) -> tuple[list[str], str]:
    allowed_roles = {"primary", "unique", "foreign", "natural"}
    if isinstance(value, dict):
        columns = _ensure_list(value.get("columns", value.get("column")))
        role = str(value.get("role", default_role) or default_role).strip().lower()
    else:
        columns = _ensure_list(value)
        role = default_role
    if role not in allowed_roles:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"key role must be one of {sorted(allowed_roles)}"
        )
    return columns, role


def _held_key_role(columns: Iterable[str], key: list[str], key_roles: Mapping[str, str]) -> str:
    """The role of ``key`` when ``columns`` hold every column of it, else "".

    Columns compare as written, in any order and with extra columns allowed. A key whose columns
    carry different roles reads as "natural".
    """
    held = set(columns)
    if not key or any(column not in held for column in key):
        return ""
    roles = {str(key_roles.get(column, "primary")).lower() for column in key}
    return roles.pop() if len(roles) == 1 else "natural"


def _simple_cases(char: str) -> tuple[str, str]:
    """``char`` in lower and upper case where each is one character; else ``char`` itself."""
    lower, upper = char.lower(), char.upper()
    return (lower if len(lower) == 1 else char, upper if len(upper) == 1 else char)


def _differ_only_in_case(name: str, other: str) -> bool:
    """Whether two names differ only by a one-to-one change of case in some characters.

    A full case fold would also expand a character (``ﬀ`` to ``ff``, ``ß`` to ``ss``) and so
    match two names a warehouse keeps apart; this compares character by character instead.
    """
    if name == other or len(name) != len(other):
        return False
    for char, other_char in zip(name, other, strict=True):
        lower, upper = _simple_cases(char)
        other_lower, other_upper = _simple_cases(other_char)
        if char != other_char and lower != other_lower and upper != other_upper:
            return False
    return True


def _case_only_misspelling(columns: Iterable[str], key: list[str]) -> tuple[str, str] | None:
    """A column that differs from a column of ``key`` only in case, and that key column.

    Warehouses disagree on whether such names, quoted or not, are one column, so the loader
    compares names as written and refuses the ambiguity rather than guess.
    """
    for column in columns:
        if column in key:
            continue
        for key_column in key:
            if _differ_only_in_case(column, key_column):
                return column, key_column
    return None


def _inferred_source_role(
    columns: Iterable[str], key: list[str], key_roles: Mapping[str, str]
) -> str:
    """A source side holds one row per value only on a whole key declared primary or unique."""
    role = _held_key_role(columns, key, key_roles)
    return role if role in {"primary", "unique"} else "foreign"


def _relation_sql_name(relation_id: str) -> str:
    label = relation_id
    if label.startswith("relation."):
        label = label.split("relation.", 1)[1]
    return "rel_" + _slug(label).replace(".", "_")


def _relation_id_for_key(key: str, spec: dict[str, Any], *, namespace: str) -> str:
    explicit = str(spec.get("id", "") or "").strip()
    if explicit.startswith("relation."):
        return explicit
    local = explicit or str(key)
    if local.startswith("relation."):
        return local
    if namespace:
        return f"relation.{namespace}.{_slug(local)}"
    return f"relation.{_slug(local)}"


def _normalize_relation_step(raw: Any) -> RelationPipelineStep:
    # Keys starting with `_` are annotations, beside the one key naming the step's kind.
    step = (
        {key: value for key, value in raw.items() if not str(key).startswith("_")}
        if isinstance(raw, dict)
        else raw
    )
    if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in RELATION_STEP_KEYS:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Relation pipeline step must be one key naming its kind, got {raw!r}",
        )
    ((kind, value),) = step.items()
    return RelationPipelineStep(
        kind=kind, config=dict(value) if isinstance(value, dict) else {"value": value}
    )


def _parse_relations(
    raw: dict[str, Any], *, namespace: str, path: str
) -> tuple[list[RelationConfig], dict[str, str]]:
    rows = raw.get("relations", {}) or {}
    if not isinstance(rows, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: relations must be a mapping")

    relations: list[RelationConfig] = []
    aliases: dict[str, str] = {}
    for key, value in rows.items():
        key, spec = str(key), dict(value or {})
        relation_id = _relation_id_for_key(key, spec, namespace=namespace)
        steps = [_normalize_relation_step(step) for step in list(spec.get("steps", []) or [])]
        if not steps:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"{path}: relation '{key}' must declare steps"
            )
        output_name = str(spec.get("output_name", "") or "").strip() or _relation_sql_name(
            relation_id
        )
        relations.append(
            RelationConfig(
                id=relation_id,
                output_name=output_name,
                steps=steps,
                columns=_ensure_list(spec.get("columns")),
                name=str(spec.get("name", key)),
                label=str(spec.get("label", _titleize(key))),
                description=str(spec.get("description", "")),
                meta=_normalize_meta(spec.get("meta"), f"{path}: relation '{key}'"),
            )
        )
        aliases[key] = relation_id
        aliases[relation_id] = relation_id
        aliases[output_name] = relation_id
    return relations, aliases


def _map_dimension_kind(kind: str) -> str:
    mapping = {
        "id": "id",
        "categorical": "string",
        "boolean": "boolean",
        "integer": "integer",
        "continuous": "number",
        "number": "number",
        "percent": "number",
        "currency": "number",
        "date": "date",
        "timestamp": "timestamp",
    }
    canonical = str(kind or "string").strip().lower() or "string"
    return mapping.get(canonical, canonical)


def _with_displays(
    entities: list[EntityConfig],
    graph_entities: dict[str, Any],
    model_rows: dict[str, dict[str, Any]],
    dimension_lookup: dict[tuple[str, str], str],
    dimensions: list[DimensionConfig],
    *,
    path: str,
) -> list[EntityConfig]:
    """Each graph entity with the dimension its ``display:`` names, which must be one of its own
    model's ``dimensions:`` and not its key. ``entities`` are in ``graph_entities`` order."""

    columns = {row.id: row.column for row in dimensions}
    out: list[EntityConfig] = []
    for entity, (entity_key, spec) in zip(entities, graph_entities.items(), strict=True):
        display = dict(spec or {}).get("display")
        if display is None:
            out.append(entity)
            continue
        model_id = str(dict(spec or {}).get("model", "")).strip()
        declared = dict(model_rows[model_id].get("dimensions", {}) or {})
        dimension_id = (
            dimension_lookup.get((model_id, display))
            if isinstance(display, str) and display in declared
            else None
        )
        if dimension_id is None or columns.get(dimension_id) in entity.key:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' display {display!r} must name a dimension "
                f"of model '{model_id}' other than its key",
            )
        out.append(replace(entity, display=dimension_id))
    return out
