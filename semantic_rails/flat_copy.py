"""``copy.deepcopy`` and ``dataclasses.asdict`` that keep a flat stack.

The standard ones recurse two to four Python frames for every level of the value they copy. A
compiled plan nests as deep as the call it compiles, so copying one would take more of the stack
than compiling it. These walk the value with a stack of their own and give the same result: the
same types, the same shared and repeated references, the same values.
"""

from __future__ import annotations

import copy
import copyreg
from dataclasses import fields, is_dataclass
from typing import Any

_ATOMIC = frozenset(
    {type(None), bool, int, float, complex, str, bytes, type, range, type(Ellipsis)}
)


_CONTAINER = object()
# What an object's reduce protocol names to make a bare instance (typeshed omits it).
_NEWOBJ = getattr(copyreg, "__newobj__")  # noqa: B009


def _plain_state(value: Any) -> dict[str, Any] | None:
    """The ``__dict__`` that ``copy.deepcopy`` would copy into a new ``type(value)``, when it
    copies ``value`` by its reduce protocol into a bare instance and nothing else; else None."""
    cls = type(value)
    if (
        hasattr(value, "__deepcopy__")
        or cls in copyreg.dispatch_table
        or hasattr(value, "__setstate__")
    ):
        return None
    try:
        reduced = value.__reduce_ex__(4)
    except Exception:  # noqa: BLE001 - copy.deepcopy reports it
        return None
    if not isinstance(reduced, tuple) or len(reduced) < 3:
        return None
    if reduced[0] is not _NEWOBJ or reduced[1] != (cls,):
        return None
    if any(item is not None for item in reduced[3:]):
        return None
    state = reduced[2]
    if state is None:
        return {}
    return state if type(state) is dict else None


def deep_copy(value: Any) -> Any:
    """``copy.deepcopy(value)``, walked without recursion through dicts, lists, tuples and plain
    objects (a dataclass among them); any other value is copied by ``copy.deepcopy`` itself."""
    memo: dict[int, Any] = {}
    held: list[Any] = []  # keeps every value read alive, so no id is reused while copying
    open_ids: set[int] = set()
    # (value, None) to expand it; (value, state) to build it once its parts are copied, where
    # state is a plain object's ``__dict__`` and ``_CONTAINER`` for a dict, list or tuple.
    stack: list[tuple[Any, Any]] = [(value, None)]
    while stack:
        item, state = stack.pop()
        key = id(item)
        kind = type(item)
        if state is None:
            if key in memo or kind in _ATOMIC:
                continue
            if kind is dict:
                children: list[Any] = [part for pair in item.items() for part in pair]
                state = _CONTAINER
            elif kind is list or kind is tuple:
                children, state = list(item), _CONTAINER
            else:
                state = _plain_state(item)
                if state is None:
                    memo[key] = copy.deepcopy(item, memo)
                    continue
                children = [state]
            if key in open_ids:  # a value inside itself: copy.deepcopy keeps the cycle
                return copy.deepcopy(value)
            open_ids.add(key)
            held.append(item)
            held.append(state)
            stack.append((item, state))
            stack.extend((child, None) for child in reversed(children))
            continue
        open_ids.discard(key)
        if kind is dict:
            memo[key] = {_copied(memo, k): _copied(memo, v) for k, v in item.items()}
        elif kind is list:
            memo[key] = [_copied(memo, part) for part in item]
        elif kind is tuple:
            parts = tuple(_copied(memo, part) for part in item)
            same = all(new is old for new, old in zip(parts, item, strict=True))
            memo[key] = item if same else parts
        else:
            new = _NEWOBJ(kind)
            new.__dict__.update(_copied(memo, state))
            memo[key] = new
    return _copied(memo, value)


def _copied(memo: dict[int, Any], value: Any) -> Any:
    return value if type(value) in _ATOMIC else memo[id(value)]


def as_dict(value: Any) -> Any:
    """``dataclasses.asdict(value)``, walked without recursion: a dataclass becomes a dict of its
    fields, a list, tuple or dict keeps its type with its items converted, and any other value is
    ``copy.deepcopy``'d. Like ``asdict``, a value referenced twice is converted twice."""
    if not (is_dataclass(value) and not isinstance(value, type)):
        raise TypeError("as_dict() should be called on dataclass instances")
    results: list[Any] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    # Each entry is (value, 0) to expand it or (value, n) to build it from the last n results.
    while stack:
        item, count = stack.pop()
        if count == 0:
            if type(item) in _ATOMIC:
                results.append(item)
                continue
            if is_dataclass(item) and not isinstance(item, type):
                parts = [getattr(item, field.name) for field in fields(item)]
            elif isinstance(item, list | tuple):
                parts = list(item)
            elif isinstance(item, dict):
                parts = [part for pair in item.items() for part in pair]
            else:
                results.append(copy.deepcopy(item))
                continue
            stack.append((item, -len(parts) - 1))
            stack.extend((part, 0) for part in reversed(parts))
            continue
        size = -count - 1
        parts = results[len(results) - size :] if size else []
        del results[len(results) - size :]
        results.append(_built(item, parts))
    return results[0]


def _built(item: Any, parts: list[Any]) -> Any:
    if is_dataclass(item):
        return {field.name: part for field, part in zip(fields(item), parts, strict=True)}
    if isinstance(item, tuple) and hasattr(item, "_fields"):
        return type(item)(*parts)
    if isinstance(item, list | tuple):
        return type(item)(parts)
    pairs = zip(parts[::2], parts[1::2], strict=True)
    if hasattr(type(item), "default_factory"):
        made = type(item)(item.default_factory)
        made.update(pairs)
        return made
    return type(item)(pairs)
