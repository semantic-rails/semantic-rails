"""The plan cache's copy and the plan's conversion to dicts give what ``copy.deepcopy`` and
``dataclasses.asdict`` give, and take no more of the stack for a deep value than a shallow one."""

from __future__ import annotations

import copy
import sys
from collections import OrderedDict, defaultdict, namedtuple
from dataclasses import asdict, dataclass, field
from typing import Any

import pytest

from semantic_rails.flat_copy import as_dict, deep_copy

Point = namedtuple("Point", "x y")


@dataclass(frozen=True)
class Node:
    left: Any
    right: Any = None
    tags: list[Any] = field(default_factory=list)


@dataclass
class Box:
    items: list[Any]
    named: dict[str, Any]


def _sample() -> dict[str, Any]:
    shared = Box([1, 2], {"k": (1, 2)})
    return {
        "node": Node(Node(1, [shared, shared]), (shared, "s"), tags=[Point(1, [2])]),
        "pair": (1, "two"),
        "counts": defaultdict(list, {"a": [1]}),
        "ordered": OrderedDict(a=1),
        "frozen": frozenset({1, 2}),
        "set": {3, 4},
    }


def test_a_copy_is_deepcopys_copy_sharing_what_it_shares():
    value = _sample()
    copied, expected = deep_copy(value), copy.deepcopy(value)
    assert copied == expected
    box = copied["node"].left.right[0]
    assert box is copied["node"].left.right[1] is copied["node"].right[0]
    assert box is not value["node"].left.right[0]
    box.items.append(3)
    assert value["node"].left.right[0].items == [1, 2]
    assert copied["pair"] is value["pair"]  # a tuple of atoms is its own copy, as deepcopy says
    assert type(copied["counts"]) is defaultdict and type(copied["ordered"]) is OrderedDict


def test_a_value_inside_itself_is_copied_as_deepcopy_copies_it():
    loop: list[Any] = []
    loop.append(loop)
    copied = deep_copy([loop])
    assert copied[0][0] is copied[0] is not loop


def test_a_dataclass_converts_as_asdict_converts_it():
    value = Node(Node(1, [Box([1], {"a": Point(1, 2)})]), {"k": (Point(3, [4]),)})
    assert as_dict(value) == asdict(value)
    assert type(as_dict(value)["right"]["k"][0]) is Point
    with pytest.raises(TypeError):
        as_dict({"not": "a dataclass"})


def _deep(levels: int) -> Node:
    node = Node(0)
    for level in range(levels):
        node = Node(node, [level, {"at": (level,)}])
    return node


def test_a_deep_value_copies_and_converts_without_recursing():
    levels = sys.getrecursionlimit() * 3
    value = _deep(levels)
    copied, converted = deep_copy(value), as_dict(value)
    for level in reversed(range(levels)):
        assert copied.right == [level, {"at": (level,)}] == converted["right"]
        assert copied is not value
        value, copied, converted = value.left, copied.left, converted["left"]
