"""Local developer configuration for the Semantic Rails CLI.

The package directory remains the portable source of truth. This module
only handles machine-local defaults under ``~/.semantic_rails`` so a
developer can omit ``--path`` after opting into a profile.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import SemanticLayerError

SEMANTIC_RAILS_HOME_ENV = "SEMANTIC_RAILS_HOME"
LOCAL_CONFIG_DIRNAME = ".semantic_rails"
LOCAL_PROFILES_FILENAME = "profiles.yml"


@dataclass(frozen=True)
class LocalProfiles:
    path: Path
    exists: bool = False
    version: int = 1
    defaults: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def profile(self) -> str:
        return str(self.defaults.get("profile", "") or "").strip()

    @property
    def target(self) -> str:
        return str(self.defaults.get("target", "") or "").strip()

    @property
    def package_path(self) -> str:
        direct = str(self.defaults.get("package_path", "") or "").strip()
        if direct:
            return direct
        profile = self.profile
        target = self.target
        if not profile or not target:
            return ""
        profile_block = self.profiles.get(profile, {})
        targets = profile_block.get("targets", {})
        if not isinstance(targets, dict):
            return ""
        target_block = targets.get(target, {})
        if not isinstance(target_block, dict):
            return ""
        return str(target_block.get("package_path", "") or "").strip()

    @property
    def profile_names(self) -> list[str]:
        return sorted(self.profiles)

    def target_block(self, *, profile: str = "", target: str = "") -> dict[str, Any]:
        """Return one configured local target without silently choosing another."""

        profile_name = str(profile or self.profile).strip()
        target_name = str(target or self.target).strip()
        if not profile_name or not target_name:
            return {}
        profile_block = self.profiles.get(profile_name, {})
        targets = profile_block.get("targets", {})
        if not isinstance(targets, dict):
            return {}
        target_block = targets.get(target_name, {})
        return dict(target_block) if isinstance(target_block, dict) else {}


def semantic_rails_home() -> Path:
    configured = str(os.environ.get(SEMANTIC_RAILS_HOME_ENV, "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / LOCAL_CONFIG_DIRNAME


def local_profiles_path() -> Path:
    return semantic_rails_home() / LOCAL_PROFILES_FILENAME


def load_local_profiles(path: str | Path | None = None) -> LocalProfiles:
    profiles_path = Path(path).expanduser() if path is not None else local_profiles_path()
    if not profiles_path.exists():
        return LocalProfiles(path=profiles_path)
    try:
        raw = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Local Semantic Rails profile file is not valid YAML: {profiles_path}",
            details={"path": str(profiles_path), "error": str(exc)},
        ) from exc
    if not isinstance(raw, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Local Semantic Rails profile file must be a YAML mapping: {profiles_path}",
            details={"path": str(profiles_path)},
        )

    defaults = _string_map(raw.get("defaults", {}) or {}, path=f"{profiles_path}:defaults")
    profiles = _profiles_map(raw.get("profiles", {}) or {}, path=f"{profiles_path}:profiles")
    try:
        version = int(raw.get("version", 1) or 1)
    except (TypeError, ValueError) as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Local Semantic Rails profile version must be an integer: {profiles_path}",
            details={"path": str(profiles_path), "version": raw.get("version")},
        ) from exc
    return LocalProfiles(
        path=profiles_path,
        exists=True,
        version=version,
        defaults=defaults,
        profiles=profiles,
    )


def resolve_local_package_path(config: LocalProfiles | None = None) -> str:
    loaded = config or load_local_profiles()
    _require_package_target(loaded)
    package_path = loaded.package_path
    if not package_path:
        return ""
    resolved = _resolve_path(package_path, base=loaded.path.parent)
    if not resolved.exists():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Local Semantic Rails profile points at a missing package path: {package_path}",
            details={"path": str(loaded.path), "package_path": package_path},
        )
    return str(resolved)


def init_local_profile(
    *,
    package_path: str,
    profile: str = "local",
    target: str = "dev",
    path: str | Path | None = None,
) -> dict[str, Any]:
    profile_name = _non_empty(profile, "profile")
    target_name = _non_empty(target, "target")
    package_source = _resolve_path(_non_empty(package_path, "package_path"), base=Path.cwd())
    if not package_source.exists():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Package path does not exist: {package_path}",
            details={"package_path": package_path},
        )
    if package_source.is_dir() and not (package_source / "package.yml").is_file():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Package directory must contain package.yml: {package_path}",
            details={"package_path": package_path},
        )

    existing = load_local_profiles(path)
    profiles = dict(existing.profiles)
    profile_block = dict(profiles.get(profile_name, {}) or {})
    targets = dict(profile_block.get("targets", {}) or {})
    target_block = dict(targets.get(target_name, {}) or {})
    target_block.update(
        {
            "package_path": str(package_source),
            "connection": {
                "mode": "package",
                "note": "Use package.yml plus warehouse CLI/env credentials; do not store secrets here.",
            },
        }
    )
    targets[target_name] = target_block
    profile_block["targets"] = targets
    profiles[profile_name] = profile_block

    data = {
        "version": 1,
        "defaults": {
            "package_path": str(package_source),
            "profile": profile_name,
            "target": target_name,
        },
        "profiles": profiles,
    }
    profiles_path = existing.path
    _atomic_write_text(
        profiles_path,
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
    )
    loaded = load_local_profiles(profiles_path)
    return local_profile_report(loaded)


def local_profile_report(config: LocalProfiles | None = None) -> dict[str, Any]:
    loaded = config or load_local_profiles()
    _require_package_target(loaded)
    resolved = ""
    if loaded.package_path:
        resolved = str(_resolve_path(loaded.package_path, base=loaded.path.parent))
    return {
        "ok": True,
        "path": str(loaded.path),
        "exists": loaded.exists,
        "defaults": dict(loaded.defaults),
        "active_profile": loaded.profile,
        "active_target": loaded.target,
        "package_path": loaded.package_path,
        "resolved_package_path": resolved,
        "profiles": loaded.profile_names,
        "scope": "local_cli_only",
    }


def _resolve_path(value: str, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _require_package_target(config: LocalProfiles) -> None:
    connection = config.target_block().get("connection", {})
    if not isinstance(connection, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Local profile connection must be a YAML mapping.",
            details={"path": str(config.path)},
        )
    mode = str(connection.get("mode", "") or "").strip()
    if mode and mode != "package":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Public engine profiles support package targets only.",
            details={"path": str(config.path), "mode": mode},
        )


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace local developer state without leaving a partial YAML file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _non_empty(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Provide {field_name}.",
            details={"field": field_name},
        )
    return text


def _string_map(value: Any, *, path: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path} must be a YAML mapping.",
            details={"path": path},
        )
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _profiles_map(value: Any, *, path: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path} must be a YAML mapping.",
            details={"path": path},
        )
    profiles: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(item, dict):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}.{key} must be a YAML mapping.",
                details={"path": f"{path}.{key}"},
            )
        profiles[str(key)] = dict(item)
    return profiles
