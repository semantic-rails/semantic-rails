from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from semantic_rails import architect_service, architect_transactions, dev_cli
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.errors import SemanticLayerError


def _create_project(tmp_path: Path, package_id: str = "service_core") -> Path:
    report = dev_cli.create_project_report(
        package_id=package_id,
        workspace_root=str(tmp_path),
        run_checks=False,
    )
    assert report["ok"] is True, report
    return Path(report["project_path"])


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _metric_spec(label: str = "Extra") -> dict:
    return {
        "label": label,
        "description": f"{label} metric.",
        "kind": "aggregate",
        "measure": "total_amount",
        "value_type": "number",
        "meta": {
            "owner_team": "analytics",
            "review_priority": "low",
            "change_risk": "low",
        },
    }


def _cross_process_upsert(
    project_path: str,
    workspace_root: str,
    expected_revision: str,
    metric_key: str,
    start,
    results,
) -> None:
    start.wait()
    try:
        report = (
            ArchitectProject(project_path, workspace_root=workspace_root)
            .upsert_metric(
                metric_key=metric_key,
                spec=_metric_spec(metric_key),
                expected_revision=expected_revision,
                idempotency_key=f"process-{metric_key}",
            )
            .report
        )
    except SemanticLayerError as exc:
        report = {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "details": dict(exc.details or {}),
            },
        }
    results.put(report)


def test_parse_failure_and_keyboard_interrupt_restore_original_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    before = _tree_bytes(project_path)

    invalid = project.upsert_metric(
        metric_key="invalid_metric",
        spec={"label": "Invalid", "kind": "not_a_metric_kind", "value_type": "number"},
    )

    assert invalid.report["ok"] is False
    assert invalid.report["status"] == "rolled_back_after_parse_error"
    assert invalid.report["rolled_back"] is True
    assert _tree_bytes(project_path) == before

    monkeypatch.setattr(
        architect_transactions,
        "parse_config_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        project.upsert_metric(metric_key="interrupted_metric", spec=_metric_spec("Interrupted"))

    assert _tree_bytes(project_path) == before


def test_undo_conflict_preserves_external_edit_and_reports_file(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    mutation = ArchitectProject(project_path, workspace_root=tmp_path).upsert_metric(
        metric_key="conflicted_metric",
        spec=_metric_spec("Conflicted"),
    )
    metric_path = project_path / "metrics" / "core" / "conflicted_metric.yml"

    assert mutation.report["ok"] is True
    assert metric_path.stat().st_mode & 0o777 == 0o644

    metric_path.write_text(metric_path.read_text(encoding="utf-8") + "# external edit\n")
    report = mutation.undo()

    assert report["ok"] is False
    assert report["status"] == "undo_conflict"
    assert report["conflicting_files"] == ["metrics/core/conflicted_metric.yml"]
    assert metric_path.read_text(encoding="utf-8").endswith("# external edit\n")


def test_undo_reports_restoration_even_when_package_still_has_parse_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = _create_project(tmp_path)
    mutation = ArchitectProject(project_path, workspace_root=tmp_path).upsert_metric(
        metric_key="restored_metric",
        spec=_metric_spec("Restored"),
    )
    metric_path = project_path / "metrics" / "core" / "restored_metric.yml"

    monkeypatch.setattr(
        architect_service,
        "parse_config_report",
        lambda *_args, **_kwargs: (
            {
                "ok": False,
                "errors": [{"code": "INVALID_CONFIG", "message": "unrelated parse error"}],
                "warnings": [],
            },
            None,
        ),
    )
    report = mutation.undo()

    assert report["status"] == "undone"
    assert report["ok"] is False
    assert not metric_path.exists()


def test_nested_model_upsert_does_not_rewrite_unchanged_graph(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    graph_path = project_path / "graph.yml"
    graph_before = graph_path.read_bytes()

    mutation = ArchitectProject(project_path, workspace_root=tmp_path).upsert_model(
        model_id="events",
        entity_key="event",
        relation="raw_events",
        primary_key=["event_id"],
        dimensions={
            "channel": {
                "label": "Channel",
                "kind": "categorical",
            }
        },
    )

    assert mutation.report["ok"] is True
    assert mutation.report["changed_files"] == ["models/core/events.yml"]
    assert graph_path.read_bytes() == graph_before


def test_cross_process_writers_from_one_base_are_serialized(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path, package_id="process_core")
    base_revision = ArchitectProject(project_path, workspace_root=tmp_path).revision()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_cross_process_upsert,
            args=(
                str(project_path),
                str(tmp_path),
                base_revision,
                metric_key,
                start,
                results,
            ),
        )
        for metric_key in ("process_one", "process_two")
    ]
    for process in processes:
        process.start()
    start.set()
    reports = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert sum(bool(report["ok"]) for report in reports) == 1
    conflict = next(report for report in reports if not report["ok"])
    assert conflict["error"]["code"] == "CONFIG_CONFLICT"
    assert conflict["error"]["details"]["conflict_kind"] == "stale_revision"
