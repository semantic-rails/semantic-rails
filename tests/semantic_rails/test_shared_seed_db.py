import os

import duckdb
import pytest

from semantic_rails.config import resolve_repo_path
from semantic_rails.db import load_csv_dir_to_duckdb
from semantic_rails.runtime import Runtime
from tests.semantic_rails import shared_seed
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.dbt_warehouse import file_digest


def test_shared_seed_query_and_explicit_reseed(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    db = package / "jaffle_shop.duckdb"
    seed = db.resolve()
    before = file_digest(seed)
    assert db.is_symlink()
    assert shared_seed.seed_for("jaffle_shop") == seed
    if os.geteuid() != 0:
        assert (seed.stat().st_mode & 0o777) == 0o444
    runtime = Runtime.from_path(str(package))
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
            }
        )
        assert result["row_count"] > 0
        assert all(w["code"] != "STALE_SEED_DATABASE" for w in result.get("warnings", []))
        with pytest.raises(duckdb.Error):
            duckdb.connect(str(db))
    finally:
        runtime.close()
    load_csv_dir_to_duckdb(str(db), resolve_repo_path("data/jaffle_csv"))
    assert not db.is_symlink()
    assert file_digest(seed) == before


@pytest.mark.parametrize("fallback", [False, True])
def test_private_seed_is_writable(tmp_path, monkeypatch, fallback):
    seed = shared_seed.seed_for("jaffle_shop")
    before = file_digest(seed)
    if fallback:

        def unavailable(*args):
            raise OSError("symlinks unavailable")

        monkeypatch.setattr(os, "symlink", unavailable)
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=not fallback)
    db = package / "jaffle_shop.duckdb"
    assert not db.is_symlink() and not os.path.samefile(db, seed)
    with duckdb.connect(str(db)) as connection:
        connection.execute("ALTER TABLE jaffle_order ADD COLUMN test_marker INTEGER")
    assert file_digest(seed) == before


@pytest.mark.parametrize("change", ["inode", "mode", "hash"])
def test_verify_detects_changed_seed(tmp_path, monkeypatch, change):
    path = tmp_path / "seed.duckdb"
    path.write_bytes(b"seed")
    path.chmod(0o444)
    monkeypatch.setattr(
        shared_seed, "_seeds", {"test": (path, path.stat().st_ino, shared_seed._digest(path))}
    )
    shared_seed.verify()
    if change == "inode":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"seed")
        replacement.chmod(0o444)
        os.replace(replacement, path)
    else:
        path.chmod(0o644)
        if change == "hash":
            path.write_bytes(b"changed")
            path.chmod(0o444)
    with pytest.raises(AssertionError, match="writable=True"):
        shared_seed.verify()


def test_no_preseed_outside_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_seed, "_root", None)
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    assert not (package / "jaffle_shop.duckdb").exists()
