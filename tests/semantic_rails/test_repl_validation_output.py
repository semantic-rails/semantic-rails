"""REPL validation names its database, preserves existing files, and deduplicates errors."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml

from semantic_rails.cli import output, scaffold
from semantic_rails.config_validation import PackageReference
from semantic_rails.repl import backend, shell
from semantic_rails.repl.backend import PlainBackend


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The starter package (it reads raw_events), with its DuckDB file not yet built."""

    monkeypatch.chdir(tmp_path)
    project = Path(
        scaffold.create_project_report(
            package_id="shop", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )
    (project / "data" / "shop.duckdb").unlink(missing_ok=True)
    return project


def _tables(database: Path, *names: str) -> None:
    with duckdb.connect(str(database)) as connection:
        for name in names:
            connection.execute(f"create table {name} (id integer)")


def _notice(project: Path) -> str:
    return shell._operational_notice(PackageReference(source_path=str(project)), "runtime")


def test_a_missing_file_may_be_built_from_the_seed(shop: Path) -> None:
    assert _notice(shop) == (
        "Operational check: runtime may create the missing DuckDB file "
        "./shop/data/shop.duckdb from the package seed "
        "(csv_dir_duckdb data/shop_csv), then query it. "
        "Validation reports a missing or unusable seed."
    )


def test_the_notice_uses_the_selected_database_path(shop: Path) -> None:
    package_path = shop / "package.yml"
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
    package["package"]["default_db"] = "data/selected.duckdb"
    package_path.write_text(yaml.safe_dump(package), encoding="utf-8")

    assert "./shop/data/selected.duckdb" in _notice(shop)
    assert "shop.duckdb" not in _notice(shop)


def test_an_existing_file_missing_a_package_relation_is_not_marked_for_rebuild(shop: Path) -> None:
    _tables(shop / "data" / "shop.duckdb", "customers", "orders")

    assert _notice(shop) == (
        "Operational check: runtime queries the existing DuckDB file ./shop/data/shop.duckdb. "
        "It is never rebuilt or replaced; validation reports missing relations "
        "or an unreadable file."
    )


def test_a_complete_existing_file_is_not_rebuilt(shop: Path) -> None:
    _tables(shop / "data" / "shop.duckdb", "raw_events", "notes")

    assert "It is never rebuilt or replaced" in _notice(shop)


def test_an_external_database_is_never_rebuilt(shop: Path) -> None:
    _tables(shop / "data" / "shop.duckdb", "customers")
    package_path = shop / "package.yml"
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
    package["package"]["seed"] = {"kind": "external"}
    package_path.write_text(yaml.safe_dump(package), encoding="utf-8")

    assert "It is never rebuilt or replaced" in _notice(shop)


def test_a_missing_external_database_is_not_created(shop: Path) -> None:
    package_path = shop / "package.yml"
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
    package["package"]["seed"] = {"kind": "external"}
    package_path.write_text(yaml.safe_dump(package), encoding="utf-8")

    assert _notice(shop) == (
        "Operational check: runtime found no DuckDB file at ./shop/data/shop.duckdb. "
        "The package uses an external seed, so validation reports the missing file "
        "and does not create it."
    )


def test_a_broken_database_link_is_not_built_through(shop: Path) -> None:
    (shop / "data" / "shop.duckdb").symlink_to(shop / "data" / "missing.duckdb")

    assert _notice(shop) == (
        "Operational check: runtime found a broken link at the DuckDB file "
        "./shop/data/shop.duckdb. Validation reports it and does not build through the link."
    )


def test_an_unreadable_file_is_not_marked_for_replacement(shop: Path) -> None:
    (shop / "data" / "shop.duckdb").write_text("not a database", encoding="utf-8")

    assert "It is never rebuilt or replaced" in _notice(shop)


def test_another_warehouse_keeps_the_general_warning(
    shop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SimpleNamespace(warehouse="snowflake", close=lambda: None)
    monkeypatch.setattr(shell, "_runtime_from_ref", lambda _: runtime)

    assert (
        _notice(shop) == "Operational check: runtime may query or refresh the snowflake warehouse."
    )


def test_unloadable_package_keeps_a_cautious_warning(shop: Path) -> None:
    package_path = shop / "package.yml"
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
    package["package"]["warehouse"] = "snowflake"
    package_path.write_text(yaml.safe_dump(package), encoding="utf-8")

    assert _notice(shop) == (
        "Operational check: runtime may query the snowflake warehouse. "
        "Package details could not be read; validation will report why."
    )


@pytest.mark.parametrize("answer", ["", "y"])
def test_validation_never_replaces_an_existing_file_with_missing_relations(
    answer: str,
    shop: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = shop / "data" / "shop.duckdb"
    _tables(database, "customers", "orders")
    before, inode = database.read_bytes(), database.stat().st_ino
    prompts: list[str] = []
    backend.set_backend(PlainBackend())
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or answer)

    try:
        shell._handle_repl_line("validate runtime", PackageReference(source_path=str(shop)))
    finally:
        backend.set_backend(None)

    assert prompts == ["Continue with operational validation? [y/N]: "]
    shown = capsys.readouterr().out
    assert "existing DuckDB file ./shop/data/shop.duckdb" in shown
    assert "It is never rebuilt or replaced" in shown
    if answer:
        assert "lacks relations the package reads" in shown
    else:
        assert "Validation cancelled" in shown
    assert database.read_bytes() == before and database.stat().st_ino == inode


@pytest.mark.parametrize("scenario", ["external", "broken_link"])
def test_confirmed_validation_does_not_create_an_unmanaged_database(
    scenario: str, shop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = shop / "data" / "shop.duckdb"
    if scenario == "external":
        package_path = shop / "package.yml"
        package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
        package["package"]["seed"] = {"kind": "external"}
        package_path.write_text(yaml.safe_dump(package), encoding="utf-8")
    else:
        database.symlink_to(shop / "data" / "missing.duckdb")
    backend.set_backend(PlainBackend())
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    try:
        shell._handle_repl_line("validate runtime", PackageReference(source_path=str(shop)))
    finally:
        backend.set_backend(None)

    assert not database.exists()
    assert database.is_symlink() is (scenario == "broken_link")


def test_an_error_every_probe_reports_prints_once_with_a_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_seed = {"message": "package.seed.source 'data/missing_csv' not found"}
    output._print_project_validation(
        {
            "package": {"id": "shop"},
            "checks": {},
            "errors": [missing_seed, missing_seed, missing_seed, {"message": "Another problem."}],
        }
    )

    assert capsys.readouterr().out.splitlines()[-3:] == [
        "Errors:",
        "  package.seed.source 'data/missing_csv' not found (3 times)",
        "  Another problem.",
    ]
