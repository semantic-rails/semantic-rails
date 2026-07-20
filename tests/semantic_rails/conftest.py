from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.runtime import Runtime


def copy_package_config(tmp_path: Path, package_id: str, *, preseed_db: bool = False) -> Path:
    run_dir = tmp_path / package_id / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    package_dir = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
    package_file = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}.yml"))
    seeded_db = (
        Path(resolve_repo_path("data/jaffle_shop.duckdb")) if package_id == "jaffle_shop" else None
    )

    def _preseed_default_db(default_db: Path) -> None:
        if preseed_db and seeded_db is not None and seeded_db.exists():
            shutil.copy2(seeded_db, default_db)

    # default_db stays relative so the copied package passes the same
    # containment validation a real shared package would; it resolves
    # inside the copied package root (which package_fingerprint ignores
    # for non-YAML files).
    if package_dir.is_dir():
        target = run_dir / package_id
        shutil.copytree(package_dir, target)
        package_path = target / "package.yml"
        raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
        package = dict(raw.get("package", {}) or {})
        package["default_db"] = f"{package_id}.duckdb"
        raw["package"] = package
        package_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        _preseed_default_db(target / f"{package_id}.duckdb")
        return target

    raw = dict(yaml.safe_load(package_file.read_text(encoding="utf-8")) or {})
    package = dict(raw.get("package", {}) or {})
    default_db = run_dir / f"{package_id}.duckdb"
    package["default_db"] = f"{package_id}.duckdb"
    raw["package"] = package

    target = run_dir / f"{package_id}.yml"
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _preseed_default_db(default_db)
    return target


@pytest.fixture()
def package_config_factory(tmp_path: Path) -> Callable[[str], tuple[object, Path]]:
    def _build(package_id: str) -> tuple[object, Path]:
        config_path = copy_package_config(tmp_path, package_id)
        return load_package_config(str(config_path)), config_path

    return _build


@pytest.fixture()
def runtime_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], Runtime]:
    def _build(package_id: str) -> Runtime:
        config_path = copy_package_config(tmp_path, package_id, preseed_db=True)
        monkeypatch.setattr(
            config_module, "list_package_paths", lambda: {package_id: str(config_path)}
        )
        return Runtime(package_id)

    return _build
