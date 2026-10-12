"""``zero_through``: what a package may declare."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.schema import ZeroThrough
from tests.integration.correctness.conftest import _write_variant


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    return _write_variant(tmp_path, "utc_authored")


def _edit(package: Path, name: str, change: Any) -> None:
    path = package / name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _tickets(package: Path, value: Any) -> None:
    _edit(package, "models/tickets.yml", lambda doc: doc["model"].update(zero_through=value))


def _edges(package: Path) -> dict[str, ZeroThrough | None]:
    config = load_package_config(str(package))
    return {row.id: row.zero_through for row in config.measures}


def _default(package: Path, value: Any) -> None:
    (package / "defaults.yml").write_text(yaml.safe_dump({"defaults": {"zero_through": value}}))


def test_a_model_declares_its_edge_and_the_project_default_covers_the_rest(shop):
    _default(shop, "now")
    edges = _edges(shop)
    assert edges["measure.shop.tickets_opened"] == ZeroThrough(
        "loaded", "ticket_loads.through_day", "ticket_loads", "through_day", "date", "UTC"
    )
    assert edges["measure.shop.audited_tickets"].relation == "ticket_audits"
    assert edges["measure.shop.tickets_always_loaded"] == ZeroThrough("now")
    # A model with no zero_through takes the project's; the engine default is the last row.
    assert edges["measure.shop.revenue"] == ZeroThrough("now")
    assert edges["measure.shop.tickets_by_last_row"] == ZeroThrough("now")
    _tickets(shop, "data")
    assert _edges(shop)["measure.shop.tickets_opened"] is None


def test_without_a_declaration_every_measure_reads_its_last_row(shop):
    edges = _edges(shop)
    assert edges["measure.shop.revenue"] is None
    assert edges["measure.shop.seats"] is None


def test_a_mark_is_found_by_its_column_and_a_timestamp_keeps_its_stored_zone(shop):
    def logged_at(doc: dict[str, Any]) -> None:
        doc["model"]["times"] = {
            "logged": {
                "column": "logged_at",
                "kind": "timestamp",
                "column_timezone": "Europe/Paris",
            }
        }

    _edit(shop, "models/ticket_loads.yml", logged_at)
    _tickets(shop, {"loaded": "ticket_loads.logged_at"})
    edge = _edges(shop)["measure.shop.tickets_opened"]
    assert (edge.column, edge.data_type, edge.timezone) == (
        "logged_at",
        "timestamp",
        "Europe/Paris",
    )


@pytest.mark.parametrize(
    "value,problem",
    [
        ("yes", "must be data, now or {loaded: <model>.<field>}"),
        ({"loaded": "ticket_loads"}, "must be data, now or {loaded: <model>.<field>}"),
        ({"loaded": 3}, "must be data, now or {loaded: <model>.<field>}"),
        ({"loaded": "ticket_loads.through_day", "now": True}, "must be data, now"),
        ({"loaded": "export_runs.through_day"}, "there is no model 'export_runs'"),
        ({"loaded": "ticket_loads.run_id"}, "no date or timestamp field of model 'ticket_loads'"),
        ({"loaded": "ticket_loads.missing"}, "no date or timestamp field of model 'ticket_loads'"),
    ],
)
def test_an_edge_that_cant_be_read_is_refused_at_load_with_its_path(shop, value, problem):
    _tickets(shop, value)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(shop))
    assert raised.value.code == "INVALID_CONFIG"
    assert "models.tickets.zero_through" in str(raised.value)
    assert problem in str(raised.value)
    assert "Write zero_through: data | now | {loaded: <model>.<date or timestamp field>}" in str(
        raised.value
    )
    assert raised.value.details["path"] == "models.tickets.zero_through"


def test_a_project_default_is_checked_where_it_is_written(shop):
    _default(shop, "soon")
    with pytest.raises(SemanticLayerError, match="defaults.zero_through must be data, now"):
        load_package_config(str(shop))


def test_a_load_log_read_through_a_relation_pipeline_is_refused(shop):
    spine = {"start": "2024-09-01", "end": "2024-09-29", "column": "through_day"}
    (shop / "relations").mkdir()
    (shop / "relations" / "loads_view.yml").write_text(
        yaml.safe_dump({"relation": {"id": "loads_view", "date_spine": spine}})
    )
    _edit(shop, "models/ticket_loads.yml", lambda doc: doc["model"].update(relation="loads_view"))
    with pytest.raises(SemanticLayerError, match="reads a relation pipeline"):
        load_package_config(str(shop))


def test_a_warehouse_without_loaded_coverage_refuses_a_declared_edge(shop):
    _edit(shop, "package.yml", lambda doc: doc["package"].update(warehouse="clickhouse"))
    with pytest.raises(SemanticLayerError, match="can only be data on ClickHouse"):
        load_package_config(str(shop))


def test_a_written_package_keeps_its_models_edges(tmp_path):
    """The package writer writes each model's edge back on the model, never on a measure."""
    from semantic_rails.config import load_package_snapshot
    from semantic_rails.interop.package_writer import write_package

    starter = (
        Path(__file__).resolve().parents[2] / "configs/examples/semantic_rails_package_starter.yml"
    )
    snapshot = load_package_snapshot(starter)
    config = snapshot.config
    first = config.measures[0]
    declared = replace(
        config,
        measures=[
            replace(row, zero_through=ZeroThrough("now")) if row.entity == first.entity else row
            for row in config.measures
        ],
    )
    directory = write_package(
        declared, tmp_path / "written", namespace=snapshot.normalized["package"]["namespace"]
    )
    loaded = load_package_snapshot(directory).config
    assert {row.id: row.zero_through for row in loaded.measures} == {
        row.id: row.zero_through for row in declared.measures
    }
