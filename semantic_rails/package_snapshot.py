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
from .schema import PackageConfig

_SOURCE_SUFFIXES = (".yml", ".yaml", ".json", ".toml")
_SOURCE_EXCLUDED_DIRS = {".git", ".pytest_cache", ".uv-cache", "__pycache__", ".compiled"}


def _source_files(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    files: list[str] = []
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(name for name in dirs if name not in _SOURCE_EXCLUDED_DIRS)
        files.extend(os.path.join(root, name) for name in names if name.endswith(_SOURCE_SUFFIXES))
    return sorted(files)


@dataclass(frozen=True)
class CapturedSource:
    source_path: str
    is_directory: bool
    files: tuple[tuple[str, bytes], ...] = field(repr=False)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for name, data in self.files:
            encoded_name = name.encode("utf-8")
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
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

    def read() -> tuple[tuple[str, bytes], ...]:
        return tuple(
            (os.path.relpath(name, root), Path(name).read_bytes()) for name in _source_files(source)
        )

    for _ in range(3):
        try:
            first = read()
            if first == read():
                return CapturedSource(source, directory, first)
        except FileNotFoundError:
            continue
    raise SemanticLayerError(
        "INVALID_CONFIG", "Package sources changed during loading; retry after writes complete."
    )


def canonicalize_semantics(value: Any) -> Any:
    """Canonical parsed values; only object collections are reordered by ID."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): canonicalize_semantics(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        items = [canonicalize_semantics(item) for item in value]
        if items and all(isinstance(item, dict) and item.get("id") for item in items):
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


def _json_fingerprint(value: Any) -> str:
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
        config = deepcopy(config)
        semantic = semantic_payload(config)
        return cls(
            source_path=source_path,
            source_fingerprint=_json_fingerprint(canonicalize_semantics(config)),
            semantic_fingerprint=_json_fingerprint(semantic),
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

    source = capture_package_source(path)
    authored = _load_package_source(source.source_path, captured=source)
    version = int(authored.get("schema_version", 0))
    if version != 1:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{source.source_path}: schema_version must be 1 (got {version!r})"
        )
    normalized = normalize_package(deepcopy(authored))
    config = _parse_package(deepcopy(normalized), path=source.source_path)
    semantic = semantic_payload(config)
    return LoadedPackageSnapshot(
        source_path=source.source_path,
        source_fingerprint=source.fingerprint,
        semantic_fingerprint=_json_fingerprint(semantic),
        provenance=source.provenance,
        source_kind="files",
        _config=config,
        _authored=authored,
        _normalized=normalized,
        _semantic=semantic,
    )
