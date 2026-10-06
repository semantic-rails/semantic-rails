from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.runtime import Runtime
from tests.semantic_rails import shared_seed


@pytest.fixture(scope="session", autouse=True)
def shared_seed_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    shared_seed.start(tmp_path_factory.getbasetemp())
    yield
    shared_seed.verify()


def write_single_file_package(target: Path, *, namespace: str = "shop") -> Path:
    """Author a standalone package fixture without relying on CLI scaffolding."""
    target.mkdir(parents=True, exist_ok=True)
    template = Path(resolve_repo_path("configs/examples/semantic_rails_package_starter.yml"))
    text = template.read_text(encoding="utf-8")
    text = text.replace("id: shop_starter", f"id: {target.name}")
    text = text.replace("namespace: shop", f"namespace: {namespace}")
    for kind in ("entity", "dimension", "measure", "metric", "segment"):
        text = text.replace(f"{kind}.shop.", f"{kind}.{namespace}.")
    text = text.replace("data/shop_starter.duckdb", f"data/{target.name}.duckdb")
    text = text.replace("data/seed_shop.sql", "data/seed_example.sql")
    package_yml = target / "package.yml"
    package_yml.write_text(text, encoding="utf-8")
    seed = target / "data" / "seed_example.sql"
    seed.parent.mkdir(exist_ok=True)
    seed.write_text(
        """
CREATE OR REPLACE TABLE shop_customer AS
SELECT 'customer_1' AS customer_id, 'new' AS customer_type, TIMESTAMP '2026-01-01 00:00:00' AS first_ordered_at;

CREATE OR REPLACE TABLE shop_order AS
SELECT 'order_1' AS order_id, 'customer_1' AS customer_id, 'web' AS channel, 4200 AS order_total_cents, TIMESTAMP '2026-01-02 00:00:00' AS ordered_at;

CREATE OR REPLACE TABLE shop_order_item AS
SELECT 'item_1' AS order_item_id, 'order_1' AS order_id, 'product_1' AS product_id, 1 AS quantity, 4200 AS line_total_cents;

CREATE OR REPLACE TABLE shop_product AS
SELECT 'product_1' AS product_id, 'beverage' AS product_type;
""".lstrip(),
        encoding="utf-8",
    )
    return package_yml


def copy_package_config(
    tmp_path: Path, package_id: str, *, preseed_db: bool = False, writable: bool = False
) -> Path:
    run_dir = tmp_path / package_id / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    package_dir = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
    package_file = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}.yml"))

    def _preseed_default_db(default_db: Path) -> None:
        seed = shared_seed.seed_for(package_id) if preseed_db else None
        if seed is not None:
            if not writable:
                try:
                    os.symlink(seed, default_db)
                    return
                except OSError:
                    pass
            shutil.copyfile(seed, default_db)

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


def opened(runtime: Runtime) -> Runtime:
    """Open `runtime`'s connection now, so the fixture sharing it across tests owns it.

    A connection opened in a test body is closed at that test's teardown.
    """
    runtime._get_adapter()
    return runtime


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
