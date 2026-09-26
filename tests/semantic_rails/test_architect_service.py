from __future__ import annotations

import multiprocessing
import threading
import time
import weakref
from pathlib import Path

import pytest

from semantic_rails import architect_service, architect_transactions, yaml_loader
from semantic_rails.architect_service import ArchitectMutation, ArchitectProject
from semantic_rails.cli.scaffold import create_project_report
from semantic_rails.errors import SemanticLayerError


def _create_project(tmp_path: Path, package_id: str = "service_core") -> Path:
    report = create_project_report(
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


def _authored_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".architect" not in path.parts
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


@pytest.mark.parametrize("order", ["noop_active", "active_noop", "all_noop"])
def test_combined_undo_ignores_noop_parts_without_losing_active_changes(
    tmp_path: Path, order: str
) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    before = _authored_bytes(project_path)

    def noop() -> ArchitectMutation:
        return project.write_file(
            relative_path="package.yml",
            content=(project_path / "package.yml").read_text(encoding="utf-8"),
            validate_after=False,
        )

    if order == "noop_active":
        parts = [noop(), project.upsert_metric(metric_key="extra", spec=_metric_spec())]
    elif order == "active_noop":
        parts = [project.upsert_metric(metric_key="extra", spec=_metric_spec()), noop()]
    else:
        parts = [noop(), noop()]
    assert [bool(part._snapshots) for part in parts] == {
        "noop_active": [False, True],
        "active_noop": [True, False],
        "all_noop": [False, False],
    }[order]

    report = ArchitectMutation.undo_together(parts)

    assert report["status"] == ("already_undone" if order == "all_noop" else "undone")
    assert _authored_bytes(project_path) == before


def test_combined_undo_rejects_a_previously_undone_part(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    first = project.upsert_metric(metric_key="first", spec=_metric_spec("First"))
    assert first.undo()["status"] == "undone"
    assert first.undo()["status"] == "already_undone"
    second = project.upsert_metric(metric_key="second", spec=_metric_spec("Second"))
    before = _authored_bytes(project_path)

    report = ArchitectMutation.undo_together([first, second])

    assert report["status"] == "undo_conflict"
    assert _authored_bytes(project_path) == before
    assert second._active is True


def test_combined_undo_same_file_snapshots_restore_earliest_bytes(
    tmp_path: Path,
) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    before = _authored_bytes(project_path)
    first = project.upsert_metric(metric_key="extra", spec=_metric_spec("First"))
    second = project.upsert_metric(metric_key="extra", spec=_metric_spec("Second"), replace=True)
    metric_path = project_path / "metrics" / "core" / "extra.yml"
    after_second = metric_path.read_bytes()
    metric_path.write_bytes(after_second + b"\n# external edit\n")
    after_external_edit = _authored_bytes(project_path)

    assert ArchitectMutation.undo_together([first, second])["status"] == "undo_conflict"
    assert _authored_bytes(project_path) == after_external_edit

    metric_path.write_bytes(after_second)
    assert ArchitectMutation.undo_together([first, second])["status"] == "undone"
    assert _authored_bytes(project_path) == before


def test_combined_undo_preserves_an_edit_between_same_file_mutations(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    first = project.upsert_metric(metric_key="extra", spec=_metric_spec("First"))
    metric_path = project_path / "metrics" / "core" / "extra.yml"
    metric_path.write_bytes(metric_path.read_bytes() + b"\n# external note\n")
    second = project.upsert_metric(metric_key="extra", spec=_metric_spec("Second"), replace=True)
    after_second = _authored_bytes(project_path)

    report = ArchitectMutation.undo_together([first, second])

    assert report["status"] == "undo_conflict"
    assert report["conflicting_files"] == ["metrics/core/extra.yml"]
    assert _authored_bytes(project_path) == after_second
    assert first._active is True and second._active is True


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


def test_model_upsert_reads_yaml_1_2_so_no_and_on_stay_strings(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    model_path = project_path / "models" / "core" / "events.yml"
    authored = model_path.read_text(encoding="utf-8")
    categorical = "      kind: categorical\n"
    assert categorical in authored
    model_path.write_text(
        authored.replace(categorical, categorical + "      domain: [no, on]\n", 1)
    )

    mutation = ArchitectProject(project_path, workspace_root=tmp_path).upsert_model(
        model_id="events",
        entity_key="event",
        relation="raw_events",
        primary_key=["event_id"],
        dimensions={"channel": {"kind": "categorical"}},
    )

    assert mutation.report["ok"] is True, mutation.report
    dimensions = yaml_loader.load_yaml_file(model_path)["model"]["dimensions"]
    assert dimensions["event_type"]["domain"] == ["no", "on"]


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


def test_in_process_project_lock_excludes_threads_across_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = _create_project(tmp_path, package_id="lock_core")
    transaction = architect_transactions.ProjectTransaction(project_path, workspace_root=tmp_path)

    class CountingLocks(weakref.WeakValueDictionary):
        created = 0

        def setdefault(self, key, default=None):
            value = super().setdefault(key, default)
            self.created += value is default
            return value

    locks = CountingLocks()
    monkeypatch.setattr(architect_transactions, "_LOCAL_LOCKS", locks)
    # Stub the lock file, so only the in-process lock can keep the threads apart.
    monkeypatch.setattr(architect_transactions, "_try_file_lock", lambda _descriptor: True)
    monkeypatch.setattr(architect_transactions, "_release_file_lock", lambda _descriptor: None)
    inside: list[int] = []
    seen: list[int] = []

    def writer(index: int) -> None:
        for turn in range(100):
            with transaction._exclusive_lock():  # noqa: SLF001
                inside.append(index)
                seen.append(len(inside))
                time.sleep(0.0002)
                inside.remove(index)
            time.sleep((index + turn) % 3 / 1000)  # lets every thread let go at times

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 600 and max(seen) == 1
    assert locks.created > 1  # the entry was collected and recreated while threads contended
    assert str(transaction._lock_path) not in locks  # noqa: SLF001


@pytest.mark.parametrize(
    ("operation", "reported", "refusal"),
    [
        ("write", "created", "exists and overwrite=false"),
        ("archive", "archived", "does not exist"),
    ],
)
def test_a_retried_file_write_or_archive_replays(
    tmp_path: Path, operation: str, reported: str, refusal: str
) -> None:
    project_path = _create_project(tmp_path)
    (project_path / "notes.md").write_text("draft\n", encoding="utf-8")
    project = ArchitectProject(project_path, workspace_root=tmp_path)

    def call(key: str, revision: str) -> ArchitectMutation:
        if operation == "write":
            return project.write_file(
                relative_path="notes/today.md",
                content="done\n",
                overwrite=False,
                expected_revision=revision,
                idempotency_key=key,
            )
        return project.archive_file(
            relative_path="notes.md", expected_revision=revision, idempotency_key=key
        )

    before = project.revision()
    first = call("first", before)
    retried = call("first", before)

    assert first.report["ok"] is True, first.report
    assert first.report["operation"] == reported  # decided under the transaction lock
    assert retried.report["status"] == "replayed"
    assert retried.report["original_status"] == first.report["status"]
    with pytest.raises(SemanticLayerError, match=refusal):
        call("second", project.revision())
