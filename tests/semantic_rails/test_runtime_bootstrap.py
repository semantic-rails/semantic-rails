from __future__ import annotations

from pathlib import Path

from semantic_rails import config as config_module
from semantic_rails import runtime as runtime_module
from semantic_rails.db import Database
from semantic_rails.runtime import Runtime


def test_runtime_reseeds_stale_duckdb_when_expected_tables_are_missing(
    package_config_factory, monkeypatch
):
    _, config_path = package_config_factory("jaffle_shop")
    package_dir = Path(config_path)
    package_id = "jaffle_shop"
    monkeypatch.setattr(config_module, "list_package_paths", lambda: {package_id: str(package_dir)})

    package_yml = package_dir / "package.yml"
    declared_db = Path(
        __import__("yaml").safe_load(package_yml.read_text(encoding="utf-8"))["package"][
            "default_db"
        ]
    )
    db_path = declared_db if declared_db.is_absolute() else package_dir / declared_db
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = Database.connect(str(db_path))
    try:
        db.execute("CREATE OR REPLACE TABLE jaffle_order AS SELECT 1 AS order_id")
    finally:
        db.close()

    runtime = Runtime(package_id)
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                        "as": "delivered_revenue",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
                    "grain": "day",
                },
                "limit": 5,
            }
        )
        assert result["row_count"] > 0
        assert "delivered_revenue" in result["rows"][0]
    finally:
        runtime.close()


def test_repo_managed_package_seed_can_fall_back_to_package_local_asset(
    monkeypatch, tmp_path: Path
):
    package_root = tmp_path / "package_root"
    package_seed = package_root / "data" / "seed.sql"
    package_seed.parent.mkdir(parents=True)
    package_seed.write_text("SELECT 1;", encoding="utf-8")
    runtime = Runtime.__new__(Runtime)
    runtime.package_root = str(package_root)
    runtime.prefer_package_root_assets = False
    monkeypatch.setattr(runtime_module, "resolve_repo_path", lambda value: str(tmp_path / value))

    resolved = runtime._resolve_asset_path("data/seed.sql", kind="seed_source")

    assert resolved == str(package_seed)
