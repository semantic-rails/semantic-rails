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
    assert start.wait(timeout=10), "the parent never signalled the writers to start"
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
        return project.write_files(
            [
                {
                    "path": "package.yml",
                    "content": (project_path / "package.yml").read_text(encoding="utf-8"),
                }
            ],
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
        thread.join(timeout=60)
        assert not thread.is_alive(), "a writer thread is still waiting for the project lock"

    assert len(seen) == 600 and max(seen) == 1
    assert locks.created > 1  # the entry was collected and recreated while threads contended
    impatient = architect_transactions.ProjectTransaction(
        project_path, workspace_root=tmp_path, lock_timeout_seconds=0.1
    )
    with transaction._exclusive_lock():  # noqa: SLF001
        for _ in range(2):  # a waiter that times out leaves the holder's entry in place
            with pytest.raises(SemanticLayerError, match="Timed out"), impatient._exclusive_lock():  # noqa: SLF001
                pass
    assert str(transaction._lock_path) not in locks  # noqa: SLF001


@pytest.mark.parametrize(
    ("operation", "reported", "refusal"),
    [
        ("write", "written", "exists and overwrite=false"),
        ("archive", "written", "does not exist"),
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
            return project.write_files(
                [{"path": "notes/today.md", "content": "done\n", "overwrite": False}],
                expected_revision=revision,
                idempotency_key=key,
            )
        return project.write_files(
            [{"path": "notes.md", "archive": True}], expected_revision=revision, idempotency_key=key
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


_EVENTS = {
    "model_id": "events",
    "entity_key": "event",
    "relation": "raw_events",
    "primary_key": ["event_id"],
}


def _events_model(project_path: Path) -> dict:
    return yaml_loader.load_yaml_file(project_path / "models" / "core" / "events.yml")["model"]


def test_model_upsert_relabels_an_object_without_rewriting_it(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    before = _events_model(project_path)

    # A label-only update used to replace the whole time role with {label: ...},
    # dropping its column, kind, class and default while parse still passed.
    mutation = project.upsert_model(**_EVENTS, times={"occurred_at": {"label": "Event time"}})

    assert mutation.report["ok"] is True, mutation.report
    assert "dropped_fields" not in mutation.report
    assert _events_model(project_path)["times"]["occurred_at"] == {
        **before["times"]["occurred_at"],
        "label": "Event time",
    }
    mutation = project.upsert_model(
        **_EVENTS, measures={"total_amount": {"description": "Amount, summed."}}
    )
    assert mutation.report["ok"] is True, mutation.report
    measure = _events_model(project_path)["measures"]["total_amount"]
    assert measure == {**before["measures"]["total_amount"], "description": "Amount, summed."}


def test_model_upsert_rewrites_an_object_and_reports_what_drops(tmp_path: Path) -> None:
    # Any other update rewrites the object, as wizards that rebuild it and leave out
    # fields that no longer apply expect: a kind change must not keep the old expr.
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    counted = {"kind": "entity_count", "entity_key": "event_id", "value_type": "count"}

    preview = project.upsert_model(**_EVENTS, measures={"total_amount": counted}, dry_run=True)
    mutation = project.upsert_model(**_EVENTS, measures={"total_amount": counted})

    assert mutation.report["ok"] is True, mutation.report
    assert _events_model(project_path)["measures"]["total_amount"] == counted
    dropped = [
        f"measures.total_amount.{field}" for field in ("accumulation", "default_agg", "expr")
    ]
    assert set(dropped) <= set(mutation.report["dropped_fields"])
    assert preview.report["dropped_fields"] == mutation.report["dropped_fields"]


def test_models_upsert_reports_dropped_fields_per_model(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    mutation = ArchitectProject(project_path, workspace_root=tmp_path).upsert_models(
        models=[{**_EVENTS, "dimensions": {"event_type": {"kind": "categorical"}}}]
    )

    assert mutation.report["ok"] is True, mutation.report
    assert mutation.report["models"][0]["dropped_fields"] == ["dimensions.event_type.label"]


def test_mcp_upsert_model_returns_dropped_fields(tmp_path: Path) -> None:
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    from semantic_rails.architect_mcp import create_architect_mcp_server
    from semantic_rails.architect_transactions import project_revision

    project_path = _create_project(tmp_path)
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def call(**values: object) -> dict:
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(
                "upsert_model",
                {
                    "project_path": project_path.name,
                    "expected_revision": project_revision(project_path),
                    "idempotency_key": str(values),
                    **_EVENTS,
                    **values,
                },
            )
            return dict(result.structuredContent or {})

    relabeled = asyncio.run(call(times={"occurred_at": {"label": "Event time"}}))
    rewritten = asyncio.run(call(dimensions={"event_type": {"kind": "categorical"}}))

    assert relabeled["ok"] is True and "dropped_fields" not in relabeled
    assert rewritten["ok"] is True
    assert rewritten["dropped_fields"] == ["dimensions.event_type.label"]


@pytest.mark.parametrize("selection", ["model", "metric", "both"])
def test_batch_commits_interdependent_measure_and_metric_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    before = _tree_bytes(project_path)
    paths = {"model": "models/core/events.yml", "metric": "metrics/core.yml"}
    files = [
        {
            "path": path,
            "content": (project_path / path).read_text().replace("total_amount", "renamed_amount"),
        }
        for kind, path in paths.items()
        if selection in (kind, "both")
    ]
    parses = []
    original = architect_transactions.parse_config_report

    def parse(*args, **kwargs):
        parses.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(architect_transactions, "parse_config_report", parse)
    mutation = project.write_files(files)

    assert len(parses) == 1
    if selection == "both":
        assert mutation.report["status"] == "written", mutation.report
        assert set(mutation.changed_files) == set(paths.values())
        assert mutation.report["files"] == [entry["path"] for entry in files]
        for entry in files:
            assert (project_path / entry["path"]).read_text() == entry["content"]
    else:
        assert mutation.report["status"] == "rolled_back_after_parse_error"
        assert _tree_bytes(project_path) == before


def test_batch_rename_updates_metric_example_and_test_and_undo_restores_all(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    assert project.upsert_check(
        kind="test",
        key="amount_columns",
        spec={
            "kind": "query_returns_columns",
            "query": {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.service_core.total_amount"},
                        "as": "total_amount",
                    }
                ],
            },
            "columns": ["total_amount"],
        },
    ).report["ok"]
    before = _authored_bytes(project_path)
    paths = ["metrics/core.yml", "examples/core.yml", "tests/core.yml"]
    # Rename the metric's public key while retaining its measure reference.
    metric = yaml_loader.load_yaml_file(project_path / paths[0])
    metric["metrics"]["renamed_amount"] = metric["metrics"].pop("total_amount")
    files = [{"path": paths[0], "content": architect_service.dump_project_yaml(metric)}]
    files.extend(
        {
            "path": path,
            "content": (project_path / path).read_text().replace("total_amount", "renamed_amount"),
        }
        for path in paths[1:]
    )
    mutation = project.write_files(files)

    assert mutation.report["ok"], mutation.report
    assert set(mutation.changed_files) == set(paths)
    for entry in files:
        assert (project_path / entry["path"]).read_text() == entry["content"]
    assert mutation.undo()["status"] == "undone"
    assert _authored_bytes(project_path) == before


def test_batch_write_and_archives_share_destination_preserve_modes_and_undo(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    sources = {"notes/old.md": b"old\n", "notes/old.bin": b"\x00\xff"}
    (project_path / "notes").mkdir()
    for path, content in sources.items():
        (project_path / path).write_bytes(content)
        (project_path / path).chmod(0o640)
    (project_path / "notes/current.md").write_text("draft\n")
    (project_path / "notes/current.md").chmod(0o600)
    before = _authored_bytes(project_path)
    mutation = project.write_files(
        [
            {"path": "notes/current.md", "content": "done\n"},
            *({"path": path, "archive": True} for path in sources),
        ],
        reason="Replaced notes",
    )

    assert mutation.report["status"] == "written", mutation.report
    destinations = mutation.report["archived_to"]
    roots = {str(Path(destination).parent) for destination in destinations.values()}
    assert len(roots) == 1
    for source, destination in destinations.items():
        assert destination.startswith(".architect/archive/")
        assert destination.endswith("/" + source)
        assert not (project_path / source).exists()
        assert (project_path / destination).read_bytes() == sources[source]
        assert (project_path / destination).stat().st_mode & 0o777 == 0o640
    reason_path = project_path / next(iter(destinations.values()))
    reason_path = reason_path.parent.parent / "ARCHIVE_REASON.txt"
    assert reason_path.read_text() == "Replaced notes"
    assert (project_path / "notes/current.md").stat().st_mode & 0o777 == 0o600
    assert mutation.undo()["ok"]
    assert _authored_bytes(project_path) == before
    assert not reason_path.exists()
    assert all(not (project_path / destination).exists() for destination in destinations.values())


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{"path": "notes.md", "content": "a"}, {"path": "notes.md", "content": "b"}],
        [{"path": "notes.md", "content": "a"}, {"path": "./notes.md", "content": "b"}],
        [{"path": "notes/a.md", "content": "a"}, {"path": "notes\\a.md", "content": "b"}],
        [{"path": "notes.md", "content": "a"}, {"path": "notes.md", "archive": True}],
        [{"path": "notes.md", "content": "a", "archive": True}],
        [{"path": "new.md", "content": "a"}, {"path": "missing.md", "archive": True}],
        [
            {"path": "new.md", "content": "a"},
            {"path": "package.yml", "content": "a", "overwrite": False},
        ],
        [{"path": "../outside.yml", "content": "a"}],
        [{"path": "/outside.yml", "content": "a"}],
        [
            {"path": ".architect/archive/manual.md", "content": "a"},
            {"path": "notes.md", "archive": True},
        ],
        [{"path": ".architect/archive/manual.md", "archive": True}],
        [{"path": ".git/config", "content": "a"}],
        [{"path": ".", "content": "a"}],
        [{"path": "notes.md", "archive": False}],
        [{"path": "notes.md", "content": 123}],
        [{"path": "notes.md", "content": "a", "overwrite": "false"}],
        [{"path": "notes.md", "content": "a", "unknown": True}],
        [{"path": "ARCHIVE_REASON.txt", "archive": True}],
    ],
)
def test_batch_refusals_leave_every_file_and_receipt_unchanged(tmp_path: Path, files: list) -> None:
    project_path = _create_project(tmp_path)
    (project_path / "notes.md").write_text("original\n")
    (project_path / "ARCHIVE_REASON.txt").write_text("original reason\n")
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    receipts = tmp_path / ".semantic-rails/architect-transactions"
    before = (_tree_bytes(project_path), _tree_bytes(receipts))

    with pytest.raises(SemanticLayerError) as caught:
        project.write_files(files, reason="New reason")

    assert caught.value.code in {"INVALID_MCP_ARGUMENTS", "INVALID_CONFIG"}
    assert (_tree_bytes(project_path), _tree_bytes(receipts)) == before
    assert not (project_path.parent / "outside.yml").exists()


def test_batch_idempotency_content_digest_and_stale_revision(tmp_path: Path) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    revision = project.revision()
    files = [
        {"path": f"notes/{name}.md", "content": name, "overwrite": False} for name in ("a", "b")
    ]
    first = project.write_files(files, expected_revision=revision, idempotency_key="batch-retry")
    before_retry = _tree_bytes(project_path)
    replay = project.write_files(files, expected_revision=revision, idempotency_key="batch-retry")

    assert first.report["status"] == "written"
    assert replay.report["status"] == "replayed"
    assert replay.report["changes"] == first.report["changes"]
    for key, changed_files, kind in (
        ("batch-retry", [files[0], {**files[1], "content": "changed"}], "idempotency_key_reuse"),
        ("stale-batch", files, "stale_revision"),
    ):
        with pytest.raises(SemanticLayerError) as caught:
            project.write_files(changed_files, expected_revision=revision, idempotency_key=key)
        assert caught.value.code == "CONFIG_CONFLICT"
        assert caught.value.details["conflict_kind"] == kind
    assert _tree_bytes(project_path) == before_retry


@pytest.mark.parametrize("valid", [True, False])
def test_batch_preview_has_per_file_diffs_and_never_writes(tmp_path: Path, valid: bool) -> None:
    project_path = _create_project(tmp_path)
    project = ArchitectProject(project_path, workspace_root=tmp_path)
    (project_path / "notes.md").write_text("draft\n")
    files = [{"path": "new.md", "content": "new\n"}, {"path": "notes.md", "archive": True}]
    if not valid:
        files.append({"path": "package.yml", "content": "schema_version: [\n"})
    before = _tree_bytes(project_path)
    revision = project.revision()
    preview = project.write_files(
        files, reason="Preview", idempotency_key="preview-batch", dry_run=True
    )

    assert preview.report["status"] == ("preview" if valid else "preview_invalid")
    assert preview.report["revision"] == revision
    assert all(change["diff"] for change in preview.report["changes"])
    assert {entry["path"] for entry in files} <= {
        change["path"] for change in preview.report["changes"]
    }
    assert _tree_bytes(project_path) == before
    if not valid:
        rollback = project.write_files(files, reason="Preview", idempotency_key="preview-batch")
        assert rollback.report["status"] == "rolled_back_after_parse_error"
        assert _tree_bytes(project_path) == before
    else:
        assert (
            project.write_files(files, reason="Preview", idempotency_key="preview-batch").report[
                "status"
            ]
            == "written"
        )
