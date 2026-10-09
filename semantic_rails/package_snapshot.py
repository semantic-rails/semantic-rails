"""Captured package sources and immutable loaded semantic generations.

A snapshot parses one verified source capture. Authored, normalized, semantic,
and typed views never reopen files; public views are isolated copies. Source
identity includes deployment configuration, while semantic identity excludes
connection, seed, and local database locators.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from .errors import SemanticLayerError
from .schema import PackageConfig, require_boolean_mnpi_package

_SOURCE_SUFFIXES = (".yml", ".yaml", ".json", ".toml")
_SOURCE_EXCLUDED_DIRS = {".git", ".pytest_cache", ".uv-cache", "__pycache__", ".compiled"}
# The package directories the loader and the package tools read YAML from.
PACKAGE_SOURCE_DIRS = frozenset({"models", "relations", "metrics", "segments", "examples", "tests"})


def links_package_input(root: str, link: str) -> bool:
    """Whether the directory symlink ``link`` (relative to ``root``), which nothing follows,
    could hide package input: it is or sits under a directory the package reads, or its tree
    holds a YAML file, a directory symlink or a directory it can't read. A link to a folder of
    data files hides none."""
    if Path(link).parts[0] in PACKAGE_SOURCE_DIRS:
        return True
    unreadable: list[OSError] = []
    for parent, dirnames, filenames in os.walk(os.path.join(root, link), onerror=unreadable.append):
        if any(name.lower().endswith((".yml", ".yaml")) for name in filenames) or any(
            os.path.islink(os.path.join(parent, name)) for name in dirnames
        ):
            return True
    return bool(unreadable)


def _source_files(path: str, links: list[str] | None = None) -> list[str]:
    """The source files under ``path``; directory symlinks the walk skips that could hide
    package input go into ``links``."""
    if os.path.isfile(path):
        # Single-file packages execute sibling examples/tests just like directory
        # packages. Bind those inputs too, without absorbing unrelated packages.
        root = os.path.dirname(path)
        return sorted(
            [
                path,
                *(
                    source
                    for companion in ("examples", "tests")
                    if os.path.isdir(os.path.join(root, companion))
                    for source in _source_files(os.path.join(root, companion))
                ),
            ]
        )
    files: list[str] = []
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(name for name in dirs if name not in _SOURCE_EXCLUDED_DIRS)
        if links is not None:
            links.extend(
                os.path.join(root, name)
                for name in dirs
                if os.path.islink(os.path.join(root, name))
                and links_package_input(path, os.path.relpath(os.path.join(root, name), path))
            )
        files.extend(os.path.join(root, name) for name in names if name.endswith(_SOURCE_SUFFIXES))
    return sorted(files)


@dataclass(frozen=True)
class CapturedSource:
    source_path: str
    is_directory: bool
    files: tuple[tuple[str, bytes], ...] = field(repr=False)
    # Directory symlinks inside a directory package that could hide package input, relative to
    # it; the walk never follows them.
    directory_links: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for name, data in self.files:
            encoded_name = name.encode("utf-8")
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        for name in self.directory_links:
            digest.update(b"directory-link\0" + name.encode("utf-8") + b"\0")
        return digest.hexdigest()

    @property
    def provenance(self) -> tuple[tuple[str, str], ...]:
        return tuple((name, hashlib.sha256(data).hexdigest()) for name, data in self.files)

    @property
    def contents(self) -> dict[str, bytes]:
        root = self.source_path if self.is_directory else os.path.dirname(self.source_path)
        return {os.path.join(root, name): data for name, data in self.files}


def capture_package_source(path: str | Path) -> CapturedSource:
    """Capture a source set, rejecting files that change while being collected.

    Two complete reads must agree, including the inventory. A continuously
    edited package fails closed instead of attaching an unverified fingerprint
    to a mixture of source revisions. Later edits cannot alter captured bytes.
    """
    source = str(Path(path).expanduser().absolute())
    directory = os.path.isdir(source)
    if not directory and not os.path.isfile(source):
        raise SemanticLayerError("INVALID_CONFIG", f"Package source '{source}' does not exist")
    root = source if directory else os.path.dirname(source)

    def read() -> tuple[tuple[tuple[str, bytes], ...], tuple[str, ...]]:
        links: list[str] = []
        files = tuple(
            (Path(name).relative_to(root).as_posix(), Path(name).read_bytes())
            for name in _source_files(source, links if directory else None)
        )
        return files, tuple(sorted(Path(name).relative_to(root).as_posix() for name in links))

    for _ in range(3):
        try:
            first = read()
            if first == read():
                return CapturedSource(source, directory, *first)
        except FileNotFoundError:
            continue
    raise SemanticLayerError(
        "INVALID_CONFIG", "Package sources changed during loading; retry after writes complete."
    )


def canonicalize_semantics(value: Any, *, sort_objects: bool = True) -> Any:
    """Canonical parsed values; only object collections are reordered by ID."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): canonicalize_semantics(item, sort_objects=sort_objects)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        items = [canonicalize_semantics(item, sort_objects=sort_objects) for item in value]
        if (
            sort_objects
            and items
            and all(isinstance(item, dict) and item.get("id") for item in items)
        ):
            return sorted(items, key=lambda item: str(item["id"]))
        return items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def semantic_payload(config: PackageConfig) -> dict[str, Any]:
    semantic = canonicalize_semantics(config)
    for name in ("connection", "default_db", "seed"):
        semantic["package"].pop(name, None)
    return semantic


def json_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class LoadedPackageSnapshot:
    source_path: str
    source_fingerprint: str
    semantic_fingerprint: str
    provenance: tuple[tuple[str, str], ...]
    source_kind: str
    _config: PackageConfig = field(repr=False, compare=False)
    _authored: dict[str, Any] = field(repr=False, compare=False)
    _normalized: dict[str, Any] = field(repr=False, compare=False)
    _semantic: dict[str, Any] = field(repr=False, compare=False)

    @property
    def config(self) -> PackageConfig:
        return deepcopy(self._config)

    @property
    def authored(self) -> dict[str, Any]:
        return deepcopy(self._authored)

    @property
    def normalized(self) -> dict[str, Any]:
        return deepcopy(self._normalized)

    @property
    def semantic(self) -> dict[str, Any]:
        return deepcopy(self._semantic)

    @classmethod
    def from_config(cls, config: PackageConfig, *, source_path: str = "") -> LoadedPackageSnapshot:
        """Freeze explicitly supplied config without attesting unrelated disk bytes."""
        require_boolean_mnpi_package(config)
        config = deepcopy(config)
        semantic = semantic_payload(config)
        return cls(
            source_path=source_path,
            source_fingerprint=json_fingerprint(
                canonicalize_semantics(config, sort_objects=False)
            ).removeprefix("sha256:"),
            semantic_fingerprint=json_fingerprint(semantic),
            provenance=(),
            source_kind="in_memory",
            _config=config,
            _authored={},
            _normalized={},
            _semantic=semantic,
        )


def load_package_snapshot(path: str | Path | LoadedPackageSnapshot) -> LoadedPackageSnapshot:
    if isinstance(path, LoadedPackageSnapshot):
        return path
    # Parser dependencies remain one-way at module import time.
    from .config import _load_package_source, _parse_package, normalize_package
    from .config_parts.shape_checks import authoring_errors

    source = capture_package_source(path)
    authored = _load_package_source(source.source_path, captured=source)
    version = int(authored.get("schema_version", 0))
    if version != 1:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{source.source_path}: schema_version must be 1 (got {version!r})"
        )
    # Every file-based load runs the authoring check validate-config reports.
    if errors := authoring_errors(authored, path_label=source.source_path):
        raise SemanticLayerError("INVALID_CONFIG", "\n".join(errors), details={"errors": errors})
    normalized = normalize_package(deepcopy(authored))
    config = _parse_package(deepcopy(normalized), path=source.source_path)
    semantic = semantic_payload(config)
    return LoadedPackageSnapshot(
        source_path=source.source_path,
        source_fingerprint=source.fingerprint,
        semantic_fingerprint=json_fingerprint(semantic),
        provenance=source.provenance,
        source_kind="files",
        _config=config,
        _authored=authored,
        _normalized=normalized,
        _semantic=semantic,
    )
