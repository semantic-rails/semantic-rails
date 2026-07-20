"""Package asset paths must stay inside the package root.

A shared package is untrusted input: an absolute ``default_db`` could
create or replace any process-writable file, a ``../`` seed source could
read files outside the package (and leak them into built artifacts), and
a ``../`` arcname in a package artifact could escape the extraction root
on unpack. These tests pin the containment rules end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.config import (
    ensure_contained_package_path,
    load_package_config,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import _resolve_companion_asset, _write_package_artifact
from semantic_rails.runtime import Runtime


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_package(package_dir: Path, *, package_overrides: dict) -> Path:
    package = {
        "id": "containment_demo",
        "name": "containment_demo",
        "description": "Path containment fixture",
        "default_db": "data/example.duckdb",
        "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
    }
    package.update(package_overrides)
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": package,
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                }
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "id": "entity.demo_order",
                        "name": "demo.Order",
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    }
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "order_fact",
                "grain": ["order_id"],
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                    }
                },
            }
        },
    )
    return package_dir


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_db": "../outside.duckdb"},
        {"default_db": "data/../../outside.duckdb"},
        {"default_db": "/tmp/outside.duckdb"},
        {"seed": {"kind": "sql_script", "source": "../secret.sql", "post_sql": ""}},
        {"seed": {"kind": "sql_script", "source": "/etc/secret.sql", "post_sql": ""}},
        {"seed": {"kind": "sql_script", "source": "data/seed.sql", "post_sql": "../post.sql"}},
    ],
    ids=[
        "default_db_traversal",
        "default_db_nested_traversal",
        "default_db_absolute",
        "seed_source_traversal",
        "seed_source_absolute",
        "post_sql_traversal",
    ],
)
def test_load_rejects_paths_outside_package_root(tmp_path: Path, overrides: dict, monkeypatch):
    monkeypatch.delenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", raising=False)
    package_dir = _write_package(tmp_path / "pkg", package_overrides=overrides)
    with pytest.raises(SemanticLayerError) as excinfo:
        load_package_config(str(package_dir))
    assert excinfo.value.code == "INVALID_CONFIG"
    assert "package root" in str(excinfo.value)


def test_env_override_allows_external_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package_dir = _write_package(
        tmp_path / "pkg",
        package_overrides={"default_db": str(tmp_path / "outside.duckdb")},
    )
    config = load_package_config(str(package_dir))
    assert config.package.default_db == str(tmp_path / "outside.duckdb")


def test_memory_and_relative_paths_pass(monkeypatch):
    monkeypatch.delenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", raising=False)
    assert ensure_contained_package_path(":memory:", field="package.default_db") == ":memory:"
    assert (
        ensure_contained_package_path("data/seed.sql", field="package.seed.source")
        == "data/seed.sql"
    )
    assert ensure_contained_package_path("", field="package.default_db") == ""


def test_windows_style_paths_are_rejected(monkeypatch):
    monkeypatch.delenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", raising=False)
    with pytest.raises(SemanticLayerError):
        ensure_contained_package_path("C:\\evil.duckdb", field="package.default_db")
    with pytest.raises(SemanticLayerError):
        ensure_contained_package_path("data\\..\\..\\evil.sql", field="package.seed.source")


def test_runtime_resolver_rejects_traversal_for_programmatic_configs(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", raising=False)
    runtime = Runtime.__new__(Runtime)
    runtime.package_root = str(tmp_path)
    runtime.prefer_package_root_assets = True
    with pytest.raises(SemanticLayerError):
        runtime._resolve_asset_path("../outside.duckdb", kind="default_db")
    with pytest.raises(SemanticLayerError):
        runtime._resolve_asset_path("/etc/passwd", kind="seed_source")


def test_companion_asset_drops_traversal_declarations(tmp_path: Path):
    resolved, arcname = _resolve_companion_asset(tmp_path, "../secret.sql")
    assert resolved is None
    assert arcname == ""
    resolved, arcname = _resolve_companion_asset(tmp_path, "/etc/secret.sql")
    assert resolved is None
    assert arcname == ""


def test_artifact_writer_refuses_traversal_arcnames(tmp_path: Path, monkeypatch):
    from semantic_rails import package_tools

    secret = tmp_path / "secret.sql"
    secret.write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(
        package_tools, "_artifact_file_rows", lambda *args, **kwargs: [(secret, "../secret.sql")]
    )
    monkeypatch.setattr(package_tools, "load_package_config", lambda *_: object())
    monkeypatch.setattr(package_tools, "_require_companion_assets", lambda *args, **kwargs: None)

    class _Ref:
        source_path = str(tmp_path / "pkg.yml")
        package_id = "containment_demo"

    with pytest.raises(SemanticLayerError) as excinfo:
        _write_package_artifact(
            _Ref(),
            output_path=str(tmp_path / "artifact.tar.gz"),
            manifest={"package_hash": "x", "source": {"files": []}},
        )
    assert "artifact member" in str(excinfo.value)
