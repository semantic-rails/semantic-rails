"""One immutable seed per pytest worker, including direct conftest imports."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.db import build_seed_database

_root: Path | None = None
_seeds: dict[str, tuple[Path, int, bytes]] = {}


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def start(basetemp: Path) -> None:
    global _root
    _root = basetemp / "shared-seed"
    _seeds.clear()


def seed_for(package_id: str) -> Path | None:
    if _root is None or package_id != "jaffle_shop":
        return None
    if package_id not in _seeds:
        config = load_package_config(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
        seed = config.package.seed
        path = _root / f"{package_id}.duckdb"
        tmp = build_seed_database(
            str(path),
            kind=seed.kind,
            source=resolve_repo_path(seed.source),
            post_sql=resolve_repo_path(seed.post_sql) if seed.post_sql else "",
            null_strings=seed.null_strings,
            package_id=package_id,
        )
        try:
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        _seeds[package_id] = (path, path.stat().st_ino, _digest(path))
    return _seeds[package_id][0]


def verify() -> None:
    for path, inode, digest in _seeds.values():
        try:
            info = path.stat()
            unchanged = (
                info.st_ino == inode
                and stat.S_IMODE(info.st_mode) == 0o444
                and _digest(path) == digest
            )
        except OSError:
            unchanged = False
        assert unchanged, f"Shared seed changed: {path}; use writable=True for a private copy"
