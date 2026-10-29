"""``prefer_over:`` and ``distinct_from:`` on a model: the author's answer to a group of
look-alike models (``LOOKALIKE_MODELS``).

A model lists other models by id. ``prefer_over: [rooms_hist]`` makes it the canonical one of
them, so validation stops asking which one is. ``distinct_from: [store_inventory_snapshots]``
says the two hold different things, so they are no look-alikes at all, whichever of them lists
the other. The loader stores the listed models' entities on the model's entity
(``EntityConfig.prefer_over`` and ``EntityConfig.distinct_from``). Each id names another model
of the package; the ``prefer_over`` lists never run in a circle, since one model of a group is
the canonical one; and no pair is in both, since a model answers only for its look-alikes.
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


def _lists(
    model_rows: dict[str, dict[str, Any]],
    model_to_entity: dict[str, str],
    key: str,
    itself: str,
    *,
    path: str,
) -> dict[str, list[str]]:
    """Each model's ``key`` list of other model ids, checked: a list of ids, each naming another
    model of the package (``itself`` says what to list instead of the model itself)."""
    lists: dict[str, list[str]] = {}
    for model_id, model in model_rows.items():
        if key not in model:
            continue
        names = model[key]
        label = f"{path}: model '{model_id}' {key}"
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            raise _invalid(f"{label} must be a list of model ids", model=model_id)
        listed: list[str] = []
        for name in (name.strip() for name in names):
            if name == model_id:
                raise _invalid(f"{label} names the model itself: {itself}", model=model_id)
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
            lists[model_id] = listed
    return lists


def with_lookalike_lists(
    entities: list[EntityConfig],
    model_rows: dict[str, dict[str, Any]],
    model_to_entity: dict[str, str],
    *,
    path: str,
) -> list[EntityConfig]:
    """``entities``, each with the entities of the models its model lists in ``prefer_over`` and
    in ``distinct_from``."""
    prefer = _lists(
        model_rows,
        model_to_entity,
        "prefer_over",
        "list the other models whose questions it answers instead",
        path=path,
    )
    if circle := _circle(prefer):
        raise _invalid(
            f"{path}: prefer_over runs in a circle ({' -> '.join(circle)}): one model of a group "
            "is the canonical one, so drop one of these from the other's prefer_over",
            models=circle,
        )
    distinct = _lists(
        model_rows,
        model_to_entity,
        "distinct_from",
        "list the other models whose rows are different things",
        path=path,
    )
    for model_id, names in sorted(prefer.items()):
        for name in names:
            if name in distinct.get(model_id, []):
                clash = f"model {model_id!r} names {name!r} in both prefer_over and distinct_from"
            elif model_id in distinct.get(name, []):
                clash = (
                    f"model {model_id!r} prefer_over names {name!r}, and model {name!r} "
                    f"distinct_from names {model_id!r}"
                )
            else:
                continue
            raise _invalid(
                f"{path}: {clash}: a model answers only for its look-alikes, so drop one of them",
                models=[model_id, name],
            )
    model_of = {entity: model_id for model_id, entity in model_to_entity.items()}

    def listed(lists: dict[str, list[str]], entity: str) -> list[str]:
        return [model_to_entity[name] for name in lists.get(model_of.get(entity, ""), [])]

    return [
        replace(row, prefer_over=listed(prefer, row.id), distinct_from=listed(distinct, row.id))
        if model_of.get(row.id) in {*prefer, *distinct}
        else row
        for row in entities
    ]
