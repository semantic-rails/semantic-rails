"""Upgrade edit records, source-path iterators and an idempotent, read-only planner."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

from ..config import _authored_direct_fields
from ..errors import SemanticLayerError
from ..expressions import _expression_children
from ..package_snapshot import capture_package_source
from ..yaml_loader import safe_load

YamlPath = tuple[str | int, ...]
Row = tuple[str, YamlPath, dict[str, Any]]
_SECTION_FILES = {
    "defaults": "defaults.yml",
    "graph": "graph.yml",
    "relations": "relations.yml",
    "metrics": "metrics.yml",
    "segments": "segments.yml",
    "semantic_policies": "policies.yml",
    "semantic_caveats": "caveats.yml",
}


@dataclass(frozen=True)
class Edit:
    """One YAML-path change, or a file creation or archival."""

    file: str
    op: Literal["delete", "rename", "replace", "insert", "create", "archive"]
    path: YamlPath = ()
    key: str = ""
    value: Any = None
    after: str = ""


@dataclass(frozen=True)
class Option:
    """An explicit author decision and the edits it permits."""

    id: str
    summary: str
    changes_answers: bool
    edits: tuple[Edit, ...]


@dataclass(frozen=True)
class Finding:
    """A mechanical change, an author choice, or a stop with no available options."""

    rule: str
    file: str
    line: int
    path: YamlPath
    message: str
    edits: tuple[Edit, ...] = ()
    options: tuple[Option, ...] = ()


@dataclass(frozen=True)
class Rule:
    """A detection rule with unchanged meaning, advisory drops, or retired semantics.

    ``refused`` means this engine refuses the legacy form at load, and the rewrite
    was proven when the rule landed. It permits a certified baseline without
    changing the rule's semantic effect.
    """

    id: str
    since: str
    effect: Literal["same_meaning", "drops", "retired"]
    summary: str
    find: Callable[[PackageFiles], Iterable[Finding]]
    masks: tuple[str, ...] = ()
    refused: bool = False


def _walk(
    value: Any, path: YamlPath = (), ancestors: tuple[int, ...] = (), *, expression: bool = False
) -> Iterator[tuple[YamlPath, Any]]:
    yield path, value
    if isinstance(value, (dict, list)) and id(value) not in ancestors:
        items = (
            (_expression_children(value) if expression else value.items())
            if isinstance(value, dict)
            else enumerate(value)
        )
        for key, child in items:
            yield from _walk(child, (*path, key), (*ancestors, id(value)), expression=expression)


class PackageFiles:
    """Captured YAML sources with paths into each authored document, including companions."""

    def __init__(
        self,
        source: str | Path,
        *,
        contents: Mapping[str, bytes] | None = None,
        _directory: bool | None = None,
    ):
        self.source = Path(source).absolute()
        self.directory = self.source.is_dir() if _directory is None else _directory
        self.root = self.source if self.directory else self.source.parent
        self.contents = (
            dict(capture_package_source(source).files) if contents is None else dict(contents)
        )
        roots = {"package.yml", *_SECTION_FILES.values()}
        folders = {"examples", "tests"} | (
            {"models", "relations", "metrics", "segments"} if self.directory else set()
        )
        self.documents = {
            file: safe_load(data)
            for file, data in self.contents.items()
            if (file in roots if self.directory else file == self.source.name)
            or (Path(file).parts[0] in folders and file.endswith((".yml", ".yaml")))
        }

    def _sections(self, section: str) -> Iterator[tuple[str, YamlPath, Any]]:
        singular = section[:-1]
        primary = "package.yml" if self.directory else self.source.name
        section_file = _SECTION_FILES.get(section, "")
        for file, doc in sorted(
            self.documents.items(), key=lambda pair: (len(Path(pair[0]).parts) > 1, pair[0])
        ):
            if file == primary:
                if section in {"examples", "tests"} or (
                    self.directory and section_file in self.documents
                ):
                    continue
            elif Path(file).parts[0] not in {section, section_file}:
                continue
            if file == "policies.yml" and section == "semantic_policies" and isinstance(doc, list):
                yield file, (), doc
            if not isinstance(doc, dict):
                continue
            if section in doc:
                yield file, (section,), doc[section]
            elif Path(file).parts[0] == section and (
                section not in {"examples", "tests"} or singular in doc
            ):
                yield file, (singular,) if singular in doc else (), doc.get(singular, doc)

    def _objects(self, section: str) -> Iterator[Row]:
        rows: dict[str | int, Row] = {}
        for file, path, value in self._sections(section):
            if value is None:
                continue
            if Path(file).parts[0] == section and (not path or path[-1] == section[:-1]):
                key = (
                    (value.get("name") if section in {"metrics", "segments"} else "")
                    or value.get("id")
                    or Path(file).stem
                )
                entries = [(key, path, value)]
            else:
                items = (
                    value.items()
                    if isinstance(value, dict)
                    else enumerate(value)
                    if isinstance(value, list)
                    else ()
                )
                entries = [(key, (*path, key), row) for key, row in items if isinstance(row, dict)]
            for key, child_path, row in entries:
                if section in {"models", "relations", "metrics", "segments"}:
                    identity = (
                        str(row.get("id") or key)
                        if (Path(file).parts[0] == section and section in {"models", "relations"})
                        else key
                    )
                    rows[identity] = (file, child_path, row)
                else:
                    yield file, child_path, row
        yield from rows.values()

    def package(self) -> Iterator[Row]:
        return self._sections("package")

    def defaults(self) -> Iterator[Row]:
        return self._sections("defaults")

    def graph_entities(self) -> Iterator[Row]:
        return self._graph("entities")

    def relationships(self) -> Iterator[Row]:
        yield from self._graph("relationships")
        yield from self._members("joins")

    def _graph(self, section: str) -> Iterator[Row]:
        for file, path, graph in self._sections("graph"):
            rows = graph.get(section) if isinstance(graph, dict) else None
            for key, row in rows.items() if isinstance(rows, dict) else ():
                yield file, (*path, section, key), row

    def models(self) -> Iterator[Row]:
        return self._objects("models")

    def metrics(self) -> Iterator[Row]:
        return self._objects("metrics")

    def segments(self) -> Iterator[Row]:
        return self._objects("segments")

    def policies(self) -> Iterator[Row]:
        return self._objects("semantic_policies")

    def _members(self, section: str) -> Iterator[Row]:
        for file, path, model in self.models():
            for key, row in (model.get(section) or {}).items():
                if isinstance(row, dict):
                    yield file, (*path, section, key), row

    def dimensions(self) -> Iterator[Row]:
        return self._members("dimensions")

    def times(self) -> Iterator[Row]:
        return self._members("times")

    def measures(self) -> Iterator[Row]:
        return self._members("measures")

    def expressions(self) -> Iterator[Row]:
        roots = [row for row in self.queries() if row[1][-1:] == ("membership",)]
        for file, path, row in (*self.metrics(), *self.segments()):
            if row.get("expression"):
                roots.append((file, (*path, "expression"), row["expression"]))
            elif direct := _authored_direct_fields(row, consumed=set()):
                yield file, path, row
                roots.extend((file, (*path, key), child) for key, child in direct.items())
        for file, path, root in roots:
            for child_path, child in _walk(root, path, expression=True):
                if isinstance(child, dict):
                    yield file, child_path, child

    def queries(self) -> Iterator[Row]:
        for section in ("examples", "tests"):
            for file, path, row in self._objects(section):
                if isinstance(row.get("query"), dict):
                    yield file, (*path, "query"), row["query"]
        for file, path, row in self.segments():
            if isinstance(row.get("membership"), dict):
                yield file, (*path, "membership"), row["membership"]

    def choice_key(self, finding: Finding) -> str:
        return json.dumps((finding.rule, finding.file, finding.path), separators=(",", ":"))

    def line(self, file: str, path: YamlPath) -> int:
        """The 1-based line of the key or item at ``path``, else of its nearest ancestor."""
        from yaml.nodes import MappingNode

        from .edits import _document, _entries, _node

        _, root = _document(self.contents[file].decode("utf-8"))
        for depth in range(len(path), 0, -1):
            try:
                parent = _node(root, path[: depth - 1])
                node = (
                    _entries(parent)[path[depth - 1]][0]
                    if isinstance(parent, MappingNode)
                    else _node(parent, path[depth - 1 : depth])
                )
                return node.start_mark.line + 1
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return 1


@dataclass
class Plan:
    """Findings, unresolved decisions and new file bytes; archival uses None."""

    findings: tuple[Finding, ...]
    pending: tuple[Finding, ...]
    files: dict[str, bytes | None]
    reformatted: tuple[str, ...] = ()
    choices: dict[str, Option] = field(default_factory=dict)


def plan(files: PackageFiles, rules: Iterable[Rule], choices: Mapping[str, str]) -> Plan:
    """Plan one conflict-free edit pass and refuse any new finding on its result."""
    from .edits import apply_edits

    rules = tuple(rules)
    findings = tuple(finding for rule in rules for finding in rule.find(files))
    pending: list[Finding] = []
    answered: dict[str, Option] = {}
    selected: list[tuple[Edit, list[YamlPath], int]] = []
    identities: dict[str, int] = {}
    before: set[tuple[int, int]] = set()
    for index, finding in enumerate(findings):
        key = files.choice_key(finding)
        if key in identities:
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                f"Rules '{findings[identities[key]].rule}' and '{finding.rule}' conflict at {finding.file}:{finding.path}",
            )
        identities[key] = index
        if finding.options or not finding.edits:
            if key not in choices:
                pending.append(finding)
                continue
            option = next((option for option in finding.options if option.id == choices[key]), None)
            if option is None:
                raise SemanticLayerError("INVALID_CONFIG", f"Invalid choice for '{key}'")
            answered[key] = option
            edits = option.edits
        else:
            edits = finding.edits
        for edit in edits:
            paths = [edit.path + (edit.key,)] if edit.op == "insert" else [edit.path]
            if edit.op == "rename":
                paths.append((*edit.path[:-1], edit.key))
            if edit.op in {"create", "archive"}:
                paths = [()]
            for other, other_paths, owner in selected:
                if (
                    owner != index
                    and other.file == edit.file
                    and any(
                        a[: len(b)] == b or b[: len(a)] == a for a in paths for b in other_paths
                    )
                ):
                    raise SemanticLayerError(
                        "CONFIG_CONFLICT",
                        f"Rules '{findings[owner].rule}' and '{finding.rule}' conflict at {edit.file}:{edit.path}",
                    )
                if owner != index and other.file == edit.file:
                    for a in paths:
                        for b in other_paths:
                            for left, right in zip(a, b, strict=False):
                                if left != right:
                                    if isinstance(left, int) and isinstance(right, int):
                                        before.add(
                                            (index, owner) if left > right else (owner, index)
                                        )
                                    break
            selected.append((edit, paths, index))
    order: list[int] = []
    remaining = list(dict.fromkeys(owner for _, _, owner in selected))
    while remaining:
        next_owner = next(
            (
                owner
                for owner in remaining
                if not any(after == owner and prior in remaining for prior, after in before)
            ),
            None,
        )
        if next_owner is None:
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                f"Rules {[findings[index].rule for index in remaining]} conflict in list edit order",
            )
        order.append(next_owner)
        remaining.remove(next_owner)
    selected.sort(key=lambda item: order.index(item[2]))
    result: dict[str, bytes | None] = {}
    if set(choices) - set(answered) - {files.choice_key(finding) for finding in pending}:
        raise SemanticLayerError("INVALID_CONFIG", "Unknown upgrade choice")
    reformatted = []
    by_file: dict[str, list[Edit]] = {}
    for edit, _, _ in selected:
        by_file.setdefault(edit.file, []).append(edit)
    for file, file_edits in by_file.items():
        for file_level, batch in groupby(
            file_edits, key=lambda edit: edit.op in {"create", "archive"}
        ):
            if file_level:
                for edit in batch:
                    if edit.op == "archive":
                        result[file] = None
                        continue
                    if file in files.contents or result.get(file) is not None:
                        raise SemanticLayerError("INVALID_CONFIG", f"File '{file}' already exists")
                    from ..architect_scaffold import dump_project_yaml

                    content = (
                        edit.value
                        if isinstance(edit.value, (str, bytes))
                        else dump_project_yaml(edit.value)
                    )
                    result[file] = content.encode("utf-8") if isinstance(content, str) else content
            else:
                text = result.get(file, files.contents.get(file))
                if text is None:
                    raise SemanticLayerError("INVALID_CONFIG", f"Missing file '{file}'")
                new, changed_style = apply_edits(text.decode("utf-8"), batch)
                result[file] = new.encode("utf-8")
                if changed_style and file not in reformatted:
                    reformatted.append(file)
    contents = {**files.contents, **result}
    updated = PackageFiles(
        files.source,
        contents={key: value for key, value in contents.items() if value is not None},
        _directory=files.directory,
    )
    pending_keys = {files.choice_key(finding) for finding in pending}
    for rule in rules:
        for finding in rule.find(updated):
            if updated.choice_key(finding) not in pending_keys:
                raise SemanticLayerError("INVALID_CONFIG", f"rule '{rule.id}' is not idempotent")
    return Plan(findings, tuple(pending), result, tuple(reformatted), answered)
