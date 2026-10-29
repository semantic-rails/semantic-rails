"""``prefer_over:`` on a model: the look-alike models whose questions it answers instead.

A model lists other models by id (``prefer_over: [rooms_hist]``), which makes it the canonical
one of them, so validation stops asking which one is (``LOOKALIKE_MODELS``). The loader stores the listed models' entities on the model's
entity (``EntityConfig.prefer_over``). Each id names another model of the package, and the
declarations never run in a circle, since one model of a group is the canonical one.
"""

from __future__ import annotations

import difflib
from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ..schema import EntityConfig


def _invalid(message: str, **details: Any) -> SemanticLayerError:
    return SemanticLayerError("INVALID_CONFIG", message, details=details)


def _circle(prefer: dict[str, list[str]]) -> list[str]:
    """The first circle of declarations, as the models it runs through and back to the first,
    or ``[]``."""
    done: set[str] = set()
    for start in sorted(prefer):
        path: list[str] = []
        on_path: set[str] = set()
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, index = stack.pop()
            if index == 0:
                if node in on_path:
                    return [*path[path.index(node) :], node]
                if node in done:
                    continue
                path.append(node)
                on_path.add(node)
            targets = prefer.get(node, [])
            if index < len(targets):
                stack.append((node, index + 1))
                stack.append((targets[index], 0))
            else:
                done.add(node)
                on_path.discard(node)
                path.pop()
    return []


def with_prefer_over(
    entities: list[EntityConfig],
    model_rows: dict[str, dict[str, Any]],
    model_to_entity: dict[str, str],
    *,
    path: str,
) -> list[EntityConfig]:
    """``entities``, each with the entities of the models its model lists in ``prefer_over``."""
    prefer: dict[str, list[str]] = {}
    for model_id, model in model_rows.items():
        if "prefer_over" not in model:
            continue
        names = model["prefer_over"]
        label = f"{path}: model '{model_id}' prefer_over"
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            raise _invalid(f"{label} must be a list of model ids", model=model_id)
        listed: list[str] = []
        for name in (name.strip() for name in names):
            if name == model_id:
                raise _invalid(
                    f"{label} names the model itself: list the other models whose questions it "
                    "answers instead",
                    model=model_id,
                )
            if name not in model_to_entity:
                others = sorted(set(model_to_entity) - {model_id})
                close = difflib.get_close_matches(name, others, n=3)
                hint = (
                    f" (did you mean {', '.join(repr(item) for item in close)}?)" if close else ""
                )
                raise _invalid(
                    f"{label} names {name!r}, which is no model of the package{hint}",
                    model=model_id,
                    unknown=name,
                    closest_matches=close,
                )
            if name not in listed:
                listed.append(name)
        if model_id in model_to_entity:
            prefer[model_id] = listed
    if circle := _circle(prefer):
        raise _invalid(
            f"{path}: prefer_over runs in a circle ({' -> '.join(circle)}): one model of a group "
            "is the canonical one, so drop one of these from the other's prefer_over",
            models=circle,
        )
    by_entity = {
        model_to_entity[model_id]: [model_to_entity[name] for name in names]
        for model_id, names in prefer.items()
    }
    return [
        replace(row, prefer_over=by_entity[row.id]) if row.id in by_entity else row
        for row in entities
    ]
