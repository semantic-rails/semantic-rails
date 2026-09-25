"""The REPL home screen, driven with plain line prompts through the public REPL entry point."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from semantic_rails.architect_scaffold import ProjectSpec, project_scaffold_files
from semantic_rails.architect_service import ArchitectProject, create_project
from semantic_rails.cli.reports import project_validation_report
from semantic_rails.config_validation import PackageReference
from semantic_rails.repl import home, shell
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_dbt_artifacts


class _TTY(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory and home, with plain prompts."""

    folder = tmp_path / "work"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEMANTIC_RAILS_UI", "plain")
    return folder


def _repl(
    monkeypatch: pytest.MonkeyPatch, *replies: str | type[BaseException], **start: str
) -> str:
    """Run the REPL at a terminal, answering each prompt in turn; return what it printed."""

    pending = list(replies)

    def reply(prompt: str = "") -> str:
        print(prompt)
        assert pending, f"unexpected prompt {prompt!r}"
        value = pending.pop(0)
        if isinstance(value, type):
            raise value
        return value

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, "stdout", _TTY())
    monkeypatch.setattr("builtins.input", reply)
    shell.run_interactive_shell(**start)
    assert not pending, f"unused replies {pending}"
    return str(sys.stdout.getvalue())


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _package(root: Path) -> Path:
    create_project(root, ProjectSpec(package_id=root.name), workspace_root=root.parent)
    return root


@pytest.mark.parametrize("leave", ["leave", "cancel", EOFError, KeyboardInterrupt])
def test_leaving_the_home_screen_opens_and_writes_nothing(
    work: Path, monkeypatch: pytest.MonkeyPatch, leave: str | type[BaseException]
) -> None:
    printed = _repl(monkeypatch, leave)

    assert "No package open." in printed
    assert "4. Try the bundled sample package (jaffle_shop: sample data, not yours)" in printed
    assert "Governed questions" not in printed and "package  " not in printed
    assert list(work.iterdir()) == []


def test_create_builds_the_shared_scaffold_and_opens_it(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    printed = _repl(monkeypatch, "create", "", "", "exit")

    target = work / "my_package"
    assert "Choose [create]: " in printed
    assert "[ok] ./my_package is ready and parses." in printed
    assert f"package  {target.resolve()}" in printed
    assert _files(target) == set(project_scaffold_files(ProjectSpec(package_id="my_package")))


@pytest.mark.parametrize(
    ("taken", "replies", "message"),
    [
        (False, ("create", "Sales Data", "n", "leave"), "Will create ./sales_data"),
        (True, ("create", "", "leave"), "./my_package already exists; choose another folder"),
    ],
)
def test_create_writes_nothing_when_declined_or_the_folder_is_taken(
    work: Path, monkeypatch: pytest.MonkeyPatch, taken: bool, replies: tuple[str, ...], message: str
) -> None:
    if taken:
        (work / "my_package").mkdir()
        (work / "my_package" / "notes.txt").write_text("mine", encoding="utf-8")
    before = _files(work)

    printed = _repl(monkeypatch, *replies)

    assert message in printed
    assert _files(work) == before and not (work / "sales_data").exists()


def test_the_scan_finds_nearby_packages_only(work: Path) -> None:
    _package(work / "shop")
    _package(work / "a" / "b" / "sales")
    for hidden in (".cache", "node_modules", "target", "a/b/c/d/e"):
        _package(work / hidden / "skipped")
    _package(work / "shop" / "nested" / "inner")  # inside a package
    (work / "hpack").mkdir()
    (work / "hpack" / "package.yml").write_text("name: not-ours\n", encoding="utf-8")
    (work / "linked").symlink_to(work / "shop")

    assert home.find_packages(work) == [work / "shop", work / "a" / "b" / "sales"]


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        (("open", "", "exit"), "semantic-rails [shop] › "),  # the first found project
        (("open", "path", "shop", "exit"), "semantic-rails [shop] › "),
        (("open", "path", "a", "leave"), "error [INVALID_CONFIG]: No package.yml in ./a"),
    ],
)
def test_open_picks_a_found_project_or_a_folder(
    work: Path, monkeypatch: pytest.MonkeyPatch, replies: tuple[str, ...], expected: str
) -> None:
    _package(work / "shop")
    (work / "a").mkdir()

    printed = _repl(monkeypatch, *replies)

    assert "1. Open a project (1 found here) (recommended)" in printed
    assert "1. shop  ./shop (recommended)" in printed
    assert expected in printed


def _dbt(work: Path, adapter: str = "duckdb") -> None:
    database = build_dbt_warehouse(work / "dbt" / "dev.duckdb")
    write_dbt_artifacts(database, work / "dbt" / "target")
    manifest = work / "dbt" / "target" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["metadata"]["adapter_type"] = adapter
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (work / "dbt" / "dbt_project.yml").write_text("name: shop_dbt\n", encoding="utf-8")


def test_import_creates_a_project_from_the_keyed_dbt_models(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dbt(work)
    monkeypatch.chdir(work / "dbt")

    printed = _repl(monkeypatch, "import", "", "", "", "", "exit")

    target = work / "dbt" / "shop_dbt"
    assert "dbt target/ folder (after dbt build and dbt docs generate) [./target]: " in printed
    assert "[x] 6. dim_products  main_marts.dim_products" in printed
    assert "[ ] 1. stg_customers" in printed
    assert "Will create ./shop_dbt with 5 dbt model(s)." in printed
    assert "Point your dbt profile's DuckDB path at ./shop_dbt/data/shop_dbt.duckdb" in printed
    assert {"customers", "order_lines", "orders", "products"} <= {
        path.stem for path in (target / "models" / "dbt").glob("*.yml")
    }
    assert "kind: external" in (target / "package.yml").read_text(encoding="utf-8")
    report = project_validation_report(PackageReference(source_path=str(target)), mode="parse")
    assert report["ok"] is True, report


def _failing_upsert(*_args: object, **_kwargs: object) -> None:
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    ("adapter", "fail", "message"),
    [
        ("snowflake", False, "The REPL imports dbt-duckdb projects, not snowflake"),
        ("duckdb", True, "Cancelled."),
    ],
)
def test_import_keeps_nothing_when_it_cannot_finish(
    work: Path, monkeypatch: pytest.MonkeyPatch, adapter: str, fail: bool, message: str
) -> None:
    _dbt(work, adapter)
    if fail:
        monkeypatch.setattr(ArchitectProject, "upsert_models", _failing_upsert)
    replies = ("import", "dbt/target", *(("", "", "") if fail else ()), "leave")

    printed = _repl(monkeypatch, *replies)

    assert message in printed
    assert not (work / "shop_dbt").exists()


def test_the_sample_opens_only_when_chosen(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _repl(monkeypatch, "sample", "exit")

    assert "package  jaffle_shop (bundled sample package, not your data)" in printed


def test_home_in_a_session_goes_back_or_switches(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = _package(work / "shop")

    printed = _repl(monkeypatch, "home", "leave", "home", "sample", "exit", path=str(shop))

    assert "No package open." not in printed
    assert f"5. Back to {shop}" in printed
    assert printed.count("Using ") == 1
    assert "Using jaffle_shop (bundled sample package, not your data)" in printed
