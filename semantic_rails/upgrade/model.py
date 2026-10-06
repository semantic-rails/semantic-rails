"""Upgrade edit records, source-path iterators and an idempotent, read-only planner."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import _DIRECT_EXPRESSION_FIELDS
from ..errors import SemanticLayerError
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
    """A detection rule with unchanged meaning, advisory drops, or retired semantics."""

    id: str
    since: str
    effect: Literal["same_meaning", "drops", "retired"]
    summary: str
    find: Callable[[PackageFiles], Iterable[Finding]]
    masks: tuple[str, ...] = ()


def _walk(
    value: Any, path: YamlPath = (), ancestors: tuple[int, ...] = ()
) -> Iterator[tuple[YamlPath, Any]]:
    yield path, value
    if isinstance(value, (dict, list)) and id(value) not in ancestors:
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            yield from _walk(child, (*path, key), (*ancestors, id(value)))


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
            if Path(file).parts[0] == section and (not path or path[-1] == section[:-1]):
                key = (
                    (value.get("name") if section in {"metrics", "segments"} else "")
                    or value.get("id")
                    or Path(file).stem
                )
                entries = [(key, path, value)]
            else:
                items = value.items() if isinstance(value, dict) else enumerate(value or [])
                entries = [(key, (*path, key), row) for key, row in items if isinstance(row, dict)]
            for key, child_path, row in entries:
                if section in {"models", "relations", "metrics", "segments"}:
                    identity = row.get("id") or key if section in {"models", "relations"} else key
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
            for key, row in graph.get(section, {}).items():
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
            for key, row in model.get(section, {}).items():
                if isinstance(row, dict):
                    yield file, (*path, section, key), row

    def dimensions(self) -> Iterator[Row]:
        return self._members("dimensions")

    def times(self) -> Iterator[Row]:
        return self._members("times")

    def measures(self) -> Iterator[Row]:
        return self._members("measures")

    def expressions(self) -> Iterator[Row]:
        for file, path, row in (*self.metrics(), *self.segments()):
            for child_path, child in _walk(row, path):
                relative = child_path[len(path) :]
                if isinstance(child, dict) and (
                    not relative
                    or "expression" in relative
                    or relative[0] in _DIRECT_EXPRESSION_FIELDS
                ):
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
        value = self.documents[finding.file]
        object_id = ""
        for part in (None, *finding.path):
            if part is not None:
                value = value[part]
            if isinstance(value, dict):
                object_id = str(value.get("id") or value.get("name") or object_id)
        return (
            f"{finding.rule}:{object_id or finding.file + ':' + '.'.join(map(str, finding.path))}"
        )


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
    selected: list[tuple[Edit, list[YamlPath], Finding]] = []
    for finding in findings:
        if finding.options or not finding.edits:
            key = files.choice_key(finding)
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
                    owner is not finding
                    and other.file == edit.file
                    and any(
                        a[: len(b)] == b or b[: len(a)] == a for a in paths for b in other_paths
                    )
                ):
                    raise SemanticLayerError(
                        "CONFIG_CONFLICT",
                        f"Rules '{owner.rule}' and '{finding.rule}' conflict at {edit.file}:{edit.path}",
                    )
            selected.append((edit, paths, finding))
    result: dict[str, bytes | None] = {}
    if set(choices) - set(answered) - {files.choice_key(finding) for finding in pending}:
        raise SemanticLayerError("INVALID_CONFIG", "Unknown upgrade choice")
    reformatted = []
    for edit, _, _ in selected:
        if edit.op == "archive":
            result[edit.file] = None
        elif edit.op == "create":
            if edit.file in files.contents:
                raise SemanticLayerError("INVALID_CONFIG", f"File '{edit.file}' already exists")
            from ..architect_scaffold import dump_project_yaml

            content = (
                edit.value
                if isinstance(edit.value, (str, bytes))
                else dump_project_yaml(edit.value)
            )
            result[edit.file] = content.encode("utf-8") if isinstance(content, str) else content
        else:
            text = result.get(edit.file, files.contents.get(edit.file))
            if text is None:
                raise SemanticLayerError("INVALID_CONFIG", f"Missing file '{edit.file}'")
            new, changed_style = apply_edits(text.decode("utf-8"), (edit,))
            result[edit.file] = new.encode("utf-8")
            if changed_style and edit.file not in reformatted:
                reformatted.append(edit.file)
    contents = {**files.contents, **result}
    updated = PackageFiles(
        files.source,
        contents={key: value for key, value in contents.items() if value is not None},
        _directory=files.directory,
    )
    for rule in rules:
        for finding in rule.find(updated):
            if finding not in pending:
                raise SemanticLayerError("INVALID_CONFIG", f"rule '{rule.id}' is not idempotent")
    return Plan(findings, tuple(pending), result, tuple(reformatted), answered)
