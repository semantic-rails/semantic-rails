"""Apply YAML-path edits with source marks, preserving bytes outside changed spans."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from functools import lru_cache
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

from ..architect_scaffold import dump_project_yaml
from ..errors import SemanticLayerError
from ..yaml_loader import Yaml12SafeLoader, safe_load

YamlPath = tuple[str | int, ...]


def _get(value: Any, path: YamlPath) -> Any:
    for part in path:
        value = value[part]
    return value


def _change(document: Any, edit: Any) -> Any:
    if edit.op == "replace" and not edit.path:
        return deepcopy(edit.value)
    parent = _get(document, edit.path if edit.op == "insert" else edit.path[:-1])
    key = edit.key if edit.op == "insert" else edit.path[-1]
    if edit.op in {"insert", "rename"}:
        if not isinstance(parent, dict) or edit.key in parent:
            raise ValueError("New key must be unique in a mapping")
        if edit.after and edit.after not in parent:
            raise ValueError("Insertion anchor does not exist")
    if edit.op == "delete":
        del parent[key]
    elif edit.op == "rename":
        entries = [(edit.key if name == key else name, value) for name, value in parent.items()]
        if key not in parent:
            raise KeyError(key)
        parent.clear()
        parent.update(entries)
    elif edit.op == "replace":
        _get(document, edit.path)
        parent[key] = deepcopy(edit.value)
    elif edit.op == "insert":
        parent[key] = deepcopy(edit.value)
    else:
        raise ValueError(f"Unsupported edit '{edit.op}'")
    return document


def _node(root: Node, path: YamlPath, text: str) -> Node:
    for part in path:
        if isinstance(root, MappingNode):
            root = next(
                value
                for key, value in root.value
                if safe_load(text[key.start_mark.index : key.end_mark.index]) == part
            )
        elif isinstance(root, SequenceNode) and isinstance(part, int):
            root = root.value[part]
        else:
            raise ValueError("Path does not name a YAML node")
    return root


def _end(node: Node) -> int:
    if isinstance(node, MappingNode) and not node.flow_style and node.value:
        return max(_end(child) for pair in node.value for child in pair)
    if isinstance(node, SequenceNode) and not node.flow_style and node.value:
        return max(_end(child) for child in node.value)
    return node.end_mark.index


def _line_end(text: str, end: int) -> int:
    if end > 0 and text[end - 1] == "\n":
        return end
    newline = text.find("\n", end)
    return len(text) if newline < 0 else newline + 1


def _render(value: Any, *, flow: bool = False) -> str:
    rendered = (
        yaml.safe_dump(value, sort_keys=False, default_flow_style=True, width=10**9)
        if flow
        else dump_project_yaml(value)
    )
    return rendered.removesuffix("...\n").rstrip("\n")


@lru_cache(maxsize=1)
def _anchors(text: str) -> tuple[int, ...]:
    return tuple(
        token.start_mark.index
        for token in yaml.scan(text, Loader=Yaml12SafeLoader)
        if isinstance(token, (AliasToken, AnchorToken))
    )


def _splice(text: str, root: Node, expected: Any, edit: Any) -> tuple[str, bool]:
    try:
        target = _node(root, edit.path, text)
        parent = _node(root, edit.path[:-1], text) if edit.path else root
    except (StopIteration, yaml.YAMLError):
        return dump_project_yaml(expected), True  # Inherited merge keys have no direct span.
    anchors = _anchors(text)
    ancestors = [_node(root, edit.path[:depth], text) for depth in range(len(edit.path) + 1)]
    if any(
        target.start_mark.index <= position < target.end_mark.index for position in anchors
    ) or any(node.start_mark.index in anchors for node in ancestors):
        return (
            _render(expected, flow=True) + "\n"
            if getattr(root, "flow_style", False)
            else dump_project_yaml(expected)
        ), True
    if edit.op in {"replace", "rename"}:
        scalar = target
        if edit.op == "rename" and isinstance(parent, MappingNode):
            scalar = next(key for key, value in parent.value if value is target)
        value = edit.key if edit.op == "rename" else edit.value
        if isinstance(scalar, ScalarNode) and not isinstance(value, (dict, list)):
            rendered = _render(value, flow=True)
            if "\n" not in rendered and scalar.style not in {"|", ">"}:
                return text[: scalar.start_mark.index] + rendered + text[
                    scalar.end_mark.index :
                ], False
    mapping = (
        target
        if edit.op == "insert"
        or (edit.op == "replace" and isinstance(target, (MappingNode, SequenceNode)))
        else parent
    )
    if isinstance(mapping, MappingNode) and not mapping.flow_style:
        entries = mapping.value
        if edit.op == "delete" and len(entries) > 1:
            key, value = next(pair for pair in entries if pair[1] is target)
            start = text.rfind("\n", 0, key.start_mark.index) + 1
            if not text[start : key.start_mark.index].strip():
                end = _line_end(text, _end(value))
                return text[:start] + text[end:], False
        if edit.op == "insert" and entries:
            key, value = (
                entries[-1]
                if not edit.after
                else next(
                    pair
                    for pair in entries
                    if safe_load(text[pair[0].start_mark.index : pair[0].end_mark.index])
                    == edit.after
                )
            )
            position = _line_end(text, _end(value))
            indentation = " " * entries[0][0].start_mark.column
            newline = "\r\n" if "\r\n" in text else "\n"
            rendered = (
                newline.join(
                    indentation + line for line in _render({edit.key: edit.value}).splitlines()
                )
                + newline
            )
            prefix = newline if position and text[position - 1] != "\n" else ""
            return text[:position] + prefix + rendered + text[position:], False
    if isinstance(mapping, (MappingNode, SequenceNode)) and mapping.flow_style:
        path = edit.path if mapping is target else edit.path[:-1]
        rendered = _render(_get(expected, path), flow=True)
        return text[: mapping.start_mark.index] + rendered + text[mapping.end_mark.index :], True
    return dump_project_yaml(expected), True


def apply_edits(text: str, edits: Iterable[Any]) -> tuple[str, bool]:
    """Return edited YAML and its reformat flag; file-level edits belong to the caller."""
    reformatted = False
    for edit in edits:
        if edit.op in {"create", "archive"}:
            continue
        try:
            expected = _change(deepcopy(safe_load(text)), edit)
            root = yaml.compose(text, Loader=Yaml12SafeLoader)
            if root is None:
                raise ValueError("Cannot edit an empty document")
            result, fallback = _splice(text, root, expected, edit)
            if safe_load(result) != expected:
                raise ValueError("YAML splice differs from the path edit")
        except (
            yaml.YAMLError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            StopIteration,
            RecursionError,
        ) as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"Cannot apply {edit.op} at {edit.file}:{edit.path}: {exc}"
            ) from exc
        text, reformatted = result, reformatted or fallback
    return text, reformatted
