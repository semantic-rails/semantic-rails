"""`author model` picks a table from the warehouse and writes the whole model in one change.

The package is the starter scaffold, reading the dbt-shaped DuckDB warehouse from
``dbt_warehouse.py``. Warehouse introspection lists its tables and suggests a key,
clocks, dimensions and measures for one. Without a database to read, or for a
warehouse other than DuckDB, the wizard asks for the table by name as before.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.architect_service import ProjectSpec, ProjectWarehouse, create_project
from semantic_rails.cli import scaffold
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.repl import backend, shell
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse

ORDERS = "main_marts.fct_orders ("


class _Script:
    """A prompt backend that answers by label and records what it was asked."""

    name = "script"
    filters_long_lists = True

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.offered: dict[str, Any] = {}
        self.defaults: dict[str, Any] = {}

    def _key(self, label: str) -> str:
        return next((key for key in self.answers if label.startswith(key)), label)

    def text(self, label: str, *, default: str = "") -> str:
        self.asked.append(label)
        return str(self.answers.get(self._key(label), default))

    def confirm(self, label: str, *, default: bool) -> bool:
        self.asked.append(label)
        return bool(self.answers.get(self._key(label), default))

    def choose(self, label: str, options: Sequence[tuple[str, str]], *, default: str = "") -> str:
        self.asked.append(label)
        self.offered[label], self.defaults[label] = [text for _, text in options], default
        wanted = self.answers.get(self._key(label))
        if wanted is None:
            return default
        return next(value for value, text in options if str(wanted) in text)

    def multi_choose(
        self, label: str, options: Sequence[tuple[str, str]], *, defaults: Collection[str] = ()
    ) -> list[str]:
        self.asked.append(label)
        self.defaults[label] = list(defaults)
        return list(self.answers.get(self._key(label), defaults))

    def show_yaml(self, payload: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    yield
    backend.set_backend(None)


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    project = Path(
        scaffold.create_project_report(
            package_id="shop", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )
    build_dbt_warehouse(project / "data" / "shop.duckdb")
    with duckdb.connect(str(project / "data" / "shop.duckdb")) as connection:
        # A bookkeeping clock, which introspection suggests with low confidence.
        connection.execute("ALTER TABLE main_marts.fct_orders ADD COLUMN updated_at TIMESTAMP")
    return project


def _author(project: Path, answers: dict[str, Any]) -> tuple[_Script, list[Any]]:
    script = _Script(answers)
    backend.set_backend(script)
    undo: list[Any] = []
    shell._handle_repl_line(
        "author model", PackageReference(source_path=str(project)), undo_stack=undo
    )
    return script, undo


def _model(project: Path, name: str) -> dict[str, Any]:
    return yaml.safe_load((project / "models" / "core" / f"{name}.yml").read_text("utf-8"))["model"]


def _files(project: Path) -> dict[Path, bytes]:
    return {
        p: p.read_bytes() for p in project.rglob("*") if p.is_file() and ".architect" not in p.parts
    }


def test_a_table_becomes_a_whole_model_in_one_change(
    shop: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _files(shop)
    script, undo = _author(shop, {"Table to model": ORDERS, "Create this model?": True})

    model = _model(shop, "orders")
    assert model["relation"] == "main_marts.fct_orders"
    assert list(model["times"]) == ["ordered_at", "delivered_at"]  # updated_at was left out
    assert model["times"]["ordered_at"]["default"] is True
    assert model["dimensions"] == {"status": {"kind": "categorical"}}
    assert model["measures"]["order_count"]["entity_key"] == "order_id"
    assert model["measures"]["order_total"]["value_type"] == "currency"
    assert model["measures"]["order_total"]["currency"] == "USD"
    assert model["measures"]["line_count"]["value_type"] == "number"
    assert all(
        measure["meta"]["owner_team"] == "analytics" for measure in model["measures"].values()
    )
    graph = yaml.safe_load((shop / "graph.yml").read_text("utf-8"))
    assert graph["graph"]["entities"]["order"]["key"] == ["order_id"]
    assert len(undo) == 1 and undo[0].report["parse"]["ok"] is True
    assert script.defaults["Which of these are money amounts?"] == ["order_total"]
    # table, entity, key, clocks, dimensions, measures, money, currency, confirm
    assert len(script.asked) == 9
    output = capsys.readouterr().out
    assert "Suggested key: order_id (high:" in output
    assert "customer_id -> main_marts.dim_customers(customer_id)" in output

    shell._handle_repl_line("undo", PackageReference(source_path=str(shop)), undo_stack=undo)
    assert _files(shop) == before


def test_the_table_list_marks_modeled_tables_and_defaults_to_an_unmodeled_one(shop: Path) -> None:
    _author(shop, {"Table to model": ORDERS, "Create this model?": True})
    script, _ = _author(
        shop,
        {
            "Table to model": "Type a table name instead",
            "Model key": "orders",
            "Create this model?": False,
        },
    )

    label = next(key for key in script.offered if key.startswith("Table to model"))
    listed = script.offered[label]
    assert listed[0].startswith("raw_customers (table, 4 columns")
    assert any(text.startswith(ORDERS) and text.endswith("; modeled by orders)") for text in listed)
    assert "main_staging.stg_orders (view, 6 columns)" in listed
    assert listed[-1] == "Type a table name instead"
    assert script.defaults[label] == "raw_customers"


def test_a_low_confidence_suggestion_ticked_by_the_person_is_kept(shop: Path) -> None:
    script, _ = _author(
        shop,
        {
            "Table to model": ORDERS,
            "Time columns": ["ordered_at", "updated_at"],
            "Create this model?": True,
        },
    )

    clocks = next(key for key in script.defaults if key.startswith("Time columns"))
    assert script.defaults[clocks] == ["ordered_at", "delivered_at"]
    assert list(_model(shop, "orders")["times"]) == ["ordered_at", "updated_at"]


def test_type_a_table_name_uses_the_typed_flow(shop: Path) -> None:
    script, _ = _author(
        shop,
        {
            "Table to model": "Type a table name instead",
            "Model key": "orders",
            "Create this model?": False,
        },
    )

    assert "Warehouse table or relation (for example raw_orders)" in script.asked


def test_without_a_database_file_it_says_why_and_types_the_table_in(
    shop: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (shop / "data" / "shop.duckdb").unlink()

    script, _ = _author(shop, {"Model key": "orders", "Create this model?": False})

    assert "Can't list the warehouse tables: DuckDB database" in capsys.readouterr().out
    assert script.asked[0] == "Model key"


def test_a_warehouse_other_than_duckdb_types_the_table_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warehouse = ProjectWarehouse("postgres", "external", connection_kind="postgres_native")
    create_project("pg_shop", ProjectSpec("pg_shop", warehouse=warehouse), workspace_root=tmp_path)

    script, _ = _author(tmp_path / "pg_shop", {"Model key": "orders", "Create this model?": False})

    assert script.asked[:2] == ["Model key", "Business label"]
    assert "Can't list" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("entity", "message"),
    [
        ("order", "Entity `order` already belongs to model `orders`"),
        ("orders", "Model `orders` already exists"),
    ],
)
def test_an_existing_model_is_not_overwritten(shop: Path, entity: str, message: str) -> None:
    _author(shop, {"Table to model": ORDERS, "Create this model?": True})
    before = (shop / "models" / "core" / "orders.yml").read_bytes()

    with pytest.raises(SemanticLayerError, match=message):
        _author(shop, {"Table to model": ORDERS, "Business entity": entity})

    assert (shop / "models" / "core" / "orders.yml").read_bytes() == before
