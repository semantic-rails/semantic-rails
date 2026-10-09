from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import flake_guard, test_quarantine, test_sharding, update_shard_durations

TODAY = date(2026, 9, 30)
ROOT = Path(__file__).resolve().parents[2]


def manifest(path, **overrides):
    entry = {
        "id": "tests/test_sample.py::test_failure",
        "reason": "Known upstream failure",
        "upstream": "https://github.com/example/project/issues/1",
        "review_by": str(TODAY + timedelta(days=10)),
    }
    entry.update(overrides)
    path.write_text(
        "[[tests]]\n" + "\n".join(f'{key} = "{value}"' for key, value in entry.items()) + "\n"
    )
    return entry["id"]


@pytest.mark.parametrize("offset", [0, 1, 30])
def test_quarantine_accepts_dates_within_window(tmp_path, offset):
    path = tmp_path / "quarantine.toml"
    nodeid = manifest(path, review_by=str(TODAY + timedelta(days=offset)))
    result = test_quarantine.load_quarantine(path, TODAY)
    assert list(result) == [nodeid]
    assert "Known upstream failure" in result[nodeid]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"review_by": "2026-09-29"}, "expired"),
        ({"review_by": "2026-10-31"}, "exceeds 30 days"),
        ({"review_by": "tomorrow"}, "Invalid isoformat"),
        ({"id": ""}, "id must be a nonempty"),
        ({"reason": " "}, "reason must be a nonempty"),
        ({"upstream": "https:///issues/1"}, "HTTPS link"),
        ({"upstream": "http://github.com/example/project/issues/1"}, "HTTPS link"),
    ],
)
def test_quarantine_rejects_bad_entries(tmp_path, overrides, message):
    path = tmp_path / "quarantine.toml"
    manifest(path, **overrides)
    with pytest.raises(ValueError, match=message):
        test_quarantine.load_quarantine(path, TODAY)


@pytest.mark.parametrize(
    "body, message",
    [
        ("tests = []", None),
        ("tests = {}", "tests array"),
        ("tests = [1]", "entries require"),
        ("[[tests]]\nid = 'x'", "entries require"),
        ("tests = []\nextra = 1", "tests array"),
        ("tests = [", "Invalid value"),
    ],
)
def test_quarantine_structure(tmp_path, body, message):
    path = tmp_path / "quarantine.toml"
    path.write_text(body)
    if message:
        with pytest.raises(ValueError, match=message):
            test_quarantine.load_quarantine(path, TODAY)
    else:
        assert test_quarantine.load_quarantine(path, TODAY) == {}


def test_quarantine_rejects_duplicates_and_accepts_toml_date(tmp_path):
    path = tmp_path / "quarantine.toml"
    manifest(path)
    body = path.read_text().replace('"2026-10-10"', "2026-10-10")
    path.write_text(body)
    assert len(test_quarantine.load_quarantine(path, TODAY)) == 1
    path.write_text(body + body)
    with pytest.raises(ValueError, match="duplicate"):
        test_quarantine.load_quarantine(path, TODAY)


def plugin_run(tmp_path, source, *, parallel=False, sharded=False, **overrides):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(source)
    manifest(tmp_path / "quarantine.toml", **{"review_by": str(date.today()), **overrides})
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\n"
        "from scripts import test_quarantine\n"
        "test_quarantine.QUARANTINE = Path(__file__).parent / 'quarantine.toml'\n"
        "pytest_plugins = ['scripts.test_quarantine', 'scripts.test_sharding']\n"
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    command = [sys.executable, "-m", "pytest", "-q", "-c", str(config), "--validate-quarantine"]
    if parallel:
        command.extend(["-p", "xdist.plugin", "-n", "2"])
    return subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.getenv("PYTHONPATH")])),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            **({} if sharded else {"SR_SHARD_COUNT": "1", "SR_SHARD_INDEX": "0"}),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def test_failure(): assert False\n", "1 xfailed"),
        ("def test_failure(): pass\n", "1 xpassed"),
        ("import pytest\n@pytest.mark.xfail(strict=True)\ndef test_failure(): pass\n", "1 xpassed"),
    ],
)
def test_collection_hook_runs_quarantined_tests_with_nonstrict_xfail(tmp_path, source, expected):
    result = plugin_run(tmp_path, source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout


@pytest.mark.parametrize("parallel", [False, True])
def test_collection_hook_rejects_stale_exact_parameter_id(tmp_path, parallel):
    result = plugin_run(
        tmp_path,
        "import pytest\n@pytest.mark.parametrize('x', [1])\ndef test_failure(x): pass\n",
        parallel=parallel,
        id="tests/test_sample.py::test_failure[removed]",
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "quarantine tests no longer exist" in output
    assert "test_failure[removed]" in output
    assert "INTERNALERROR" not in output


@pytest.mark.parametrize("parallel", [False, True])
def test_collection_hook_reports_expiry_before_tests_run(tmp_path, parallel):
    result = plugin_run(
        tmp_path,
        "def test_failure(): assert False, 'must not run'\n",
        parallel=parallel,
        review_by=str(date.today() - timedelta(days=1)),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "review_by expired" in output
    assert "INTERNALERROR" not in output
    assert "must not run" not in output


def write_file(root, name, source):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_guard_selects_changed_tests_and_all_direct_import_forms(tmp_path):
    imports = [
        "import semantic_rails.db",
        "from semantic_rails.db import Adapter",
        "from semantic_rails import db as adapter",
        "def helper():\n    from semantic_rails.db import Adapter",
    ]
    for i, source in enumerate(imports):
        write_file(tmp_path, f"tests/semantic_rails/test_{i}.py", source)
    write_file(tmp_path, "tests/semantic_rails/test_changed.py", "")
    write_file(tmp_path, "tests/semantic_rails/test_unrelated.py", "import semantic_rails.config")
    write_file(tmp_path, "tests/integration/test_warehouse.py", "import semantic_rails.db")
    selected = flake_guard.select_tests(
        tmp_path,
        [
            "semantic_rails/db.py",
            "tests/semantic_rails/test_changed.py",
            "tests/semantic_rails/test_deleted.py",
        ],
    )
    assert selected == ["tests/semantic_rails/test_changed.py"] + [
        f"tests/semantic_rails/test_{i}.py" for i in range(4)
    ]


def test_guard_package_change_and_cap_prioritize_changed_test(tmp_path, capsys):
    for i in range(3):
        write_file(tmp_path, f"tests/semantic_rails/test_{i}.py", "import semantic_rails.db")
    selected = flake_guard.select_tests(
        tmp_path, ["semantic_rails/__init__.py", "tests/semantic_rails/test_2.py"], cap=2
    )
    assert selected == ["tests/semantic_rails/test_2.py", "tests/semantic_rails/test_0.py"]
    assert "capped out 1 files" in capsys.readouterr().out


def test_guard_does_not_select_tests_for_unrelated_change(tmp_path):
    write_file(tmp_path, "tests/semantic_rails/test_sample.py", "import semantic_rails.db")
    assert flake_guard.select_tests(tmp_path, ["README.md"]) == []


@pytest.mark.parametrize("budget, count", [(1000, 3), (120, 2), (119, 1), (59, 0)])
def test_guard_sizes_selection_by_workers_and_drops_only_the_tail(capsys, budget, count):
    files = ["tests/test_changed.py", "tests/test_importer_a.py", "tests/test_importer_b.py"]
    selected, estimate = flake_guard.size_selection(
        files, dict.fromkeys(files, 200.0), workers=4, budget=budget
    )
    assert selected == files[:count]
    assert estimate == count * 50
    assert estimate * flake_guard.FIT_MARGIN <= budget
    assert len(files) == 3  # Preserve the caller's selection.
    output = capsys.readouterr().out
    if count < 3:
        assert "::notice::Flake guard dropped files" in output
        assert str(files[count:]) in output
        assert all(file not in output for file in selected)
    else:
        assert output == ""


def test_guard_sums_module_and_class_durations_without_matching_other_files(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_sample" time="20"/>'
        '<testcase classname="tests.test_sample.TestGroup" time="40"/>'
        '<testcase classname="tests.test_sample_other" time="999"/>'
        '<testcase classname="tests.semantic_rails.test_import" time="10"/>'
        "</testsuite></testsuites>"
    )
    assert flake_guard.measured_durations(
        report,
        ["tests/test_sample.py", "tests/semantic_rails/test_import.py", "tests/test_missing.py"],
    ) == {"tests/test_sample.py": 60, "tests/semantic_rails/test_import.py": 10}


def test_guard_sizes_first_repetition_from_main_report(monkeypatch, tmp_path, capsys):
    files = ["tests/test_changed.py", "tests/test_importer.py"]
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.test_changed" time="100"/>'
        '<testcase classname="tests.test_importer" time="200"/></testsuite>'
    )
    calls = []

    class Process:
        def __init__(self, command, **kwargs):
            calls.append(command)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(flake_guard.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(flake_guard.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(flake_guard.subprocess, "Popen", Process)
    assert flake_guard.run_repetitions(files, tmp_path, 1120, report) == 0
    assert len(calls) == 3
    assert all(command[-1] == files[0] and files[1] not in command for command in calls)
    assert all(command[command.index("-n") + 1] == "2" for command in calls)
    assert "::notice::Flake guard dropped files" in capsys.readouterr().out


@pytest.mark.parametrize("failed_repetition", [None, 1, 2, 3])
def test_guard_repeats_three_times_but_never_retries_failure(
    monkeypatch, tmp_path, capsys, failed_repetition
):
    calls = []

    class Process:
        def __init__(self, command, **kwargs):
            calls.append(command)
            if len(calls) == failed_repetition:
                report = next(
                    part.split("=", 1)[1] for part in command if part.startswith("--junitxml=")
                )
                Path(report).write_text(
                    '<testsuites><testsuite><testcase classname="tests.test_sample" '
                    'name="test_failure"><failure/></testcase></testsuite></testsuites>'
                )

        def wait(self, timeout=None):
            return int(len(calls) == failed_repetition)

    monkeypatch.setattr(flake_guard.subprocess, "Popen", Process)
    monkeypatch.setattr(flake_guard.os, "cpu_count", lambda: 2)
    result = flake_guard.run_repetitions(["tests/test_sample.py"], tmp_path, time.monotonic() + 30)
    assert result == int(failed_repetition is not None)
    assert len(calls) == (failed_repetition or 3)
    seeds = {next(part for part in call if part.startswith("--flake-seed=")) for call in calls}
    assert len(seeds) == len(calls)
    assert all(call[call.index("-n") + 1] == "2" for call in calls)
    output = capsys.readouterr().out
    if failed_repetition:
        assert f"intermittent: investigate; repetition {failed_repetition}" in output
        assert "tests.test_sample::test_failure" in output


@pytest.mark.parametrize(
    "content, files",
    [
        (None, ["tests/test_sample.py"]),
        ("<truncated", ["tests/test_sample.py"]),
        (
            '<testsuite><testcase classname="tests.test_sample" time="10"/></testsuite>',
            ["tests/test_sample.py"],
        ),
        (
            '<testsuite><testcase classname="tests.test_sample" time="10"/></testsuite>',
            ["tests/test_sample.py", "tests/test_missing.py"],
        ),
    ],
)
@pytest.mark.parametrize("budget_ends_in", [1, 2])
def test_guard_budget_ending_a_repetition_kills_workers_and_passes_inconclusive(
    monkeypatch, tmp_path, capsys, content, files, budget_ends_in
):
    killed = []
    calls = []
    clock = [1000.0]
    report = tmp_path / "results.xml"
    if content is not None:
        report.write_text(content)

    class Process:
        pid = 123

        def __init__(self, command, **kwargs):
            assert kwargs["start_new_session"]
            assert f"--timeout={flake_guard.TEST_TIMEOUT_SECONDS}" in command
            assert "--max-worker-restart=0" in command
            calls.append(command)

        def wait(self, timeout=None):
            if timeout is None:
                return -9
            if len(calls) == budget_ends_in:
                # Every test passes its own limit, but a busy runner makes the whole run outlast the budget.
                clock[0] += timeout
                raise subprocess.TimeoutExpired("pytest", timeout)
            clock[0] += 5.0
            return 0

    monkeypatch.setattr(flake_guard.subprocess, "Popen", Process)
    monkeypatch.setattr(flake_guard.os, "killpg", lambda *args: killed.append(args))
    monkeypatch.setattr(flake_guard.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(flake_guard.time, "monotonic", lambda: clock[0])
    assert flake_guard.run_repetitions(files, tmp_path, clock[0] + 290, report) == 0
    assert len(calls) == budget_ends_in
    assert killed == [(123, flake_guard.signal.SIGKILL)]
    output = capsys.readouterr().out
    assert (
        f"::notice::Flake guard inconclusive: {budget_ends_in - 1} of 3 repetitions passed; "
        f"repetition {budget_ends_in} ran out of the time budget: {files}"
    ) in output
    assert "intermittent" not in output


def test_guard_skips_a_repetition_that_cannot_fit_and_passes_inconclusive(
    monkeypatch, tmp_path, capsys
):
    calls = []
    clock = [1000.0]

    class Process:
        def __init__(self, command, **kwargs):
            calls.append(command)

        def wait(self, timeout=None):
            clock[0] += 100.0  # each repetition takes 100 s
            return 0

    monkeypatch.setattr(flake_guard.subprocess, "Popen", Process)
    monkeypatch.setattr(flake_guard.time, "monotonic", lambda: clock[0])
    # 290 s budget: repetitions 1 and 2 fit; repetition 3 would need ~100 s with 90 s left.
    assert flake_guard.run_repetitions(["tests/test_sample.py"], tmp_path, clock[0] + 290) == 0
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert "Flake guard inconclusive: 2 of 3 repetitions passed" in output
    assert "intermittent" not in output


def write_guard_project(monkeypatch, tmp_path, timeout_method):
    """A project loading this repository's own flake guard plugin, as the guarded tests do."""
    write_file(tmp_path, "pytest.ini", f"[pytest]\ntimeout_method = {timeout_method}\n")
    write_file(tmp_path, "conftest.py", 'pytest_plugins = ["scripts.test_quarantine"]\n')
    monkeypatch.setenv("PYTHONPATH", str(flake_guard.ROOT))


@pytest.mark.parametrize("timeout_method", ["thread", "signal"])
def test_guard_fails_a_test_over_its_own_limit_before_the_budget_ends(
    monkeypatch, tmp_path, capsys, timeout_method
):
    # Real pytest: the hung test would pass after 20 s, well inside the 60 s budget, unless its own limit stops it.
    write_guard_project(monkeypatch, tmp_path, timeout_method)
    write_file(
        tmp_path,
        "test_sample.py",
        "import time\n\n\ndef test_quick():\n    pass\n\n\ndef test_hangs():\n    time.sleep(20)\n",
    )
    monkeypatch.setattr(flake_guard, "TEST_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(flake_guard.os, "cpu_count", lambda: 2)
    started = time.monotonic()
    assert flake_guard.run_repetitions(["test_sample.py"], tmp_path, started + 60) == 1
    assert time.monotonic() - started < 20
    output = capsys.readouterr().out
    assert "intermittent: investigate; repetition 1; test_sample::test_hangs" in output
    assert "test_quick" not in output.split("intermittent")[-1]


@pytest.mark.parametrize("timeout_method", ["thread", "signal"])
@pytest.mark.parametrize(
    "failing_test, limit",
    [
        ("def test_fails():\n    assert False\n", flake_guard.TEST_TIMEOUT_SECONDS),
        ("def test_fails():\n    time.sleep(20)\n", 1),
    ],
    ids=["fails-at-once", "over-its-limit"],
)
def test_guard_fails_a_repetition_whose_failure_comes_before_the_budget_ends(
    monkeypatch, tmp_path, capsys, timeout_method, failing_test, limit
):
    # Real pytest: a slow test still running when the budget ends must not hide an earlier failure.
    write_guard_project(monkeypatch, tmp_path, timeout_method)
    write_file(
        tmp_path,
        "test_sample.py",
        "import time\nfrom pathlib import Path\n\nimport pytest\n\n\n"
        + failing_test
        + "\n\n@pytest.mark.timeout(60)\ndef test_slow():\n"
        "    time.sleep(30)\n    Path('slow-finished').touch()\n",
    )
    monkeypatch.setattr(flake_guard, "TEST_TIMEOUT_SECONDS", limit)
    monkeypatch.setattr(flake_guard.os, "cpu_count", lambda: 2)
    started = time.monotonic()
    assert flake_guard.run_repetitions(["test_sample.py"], tmp_path, started + 15) == 1
    assert time.monotonic() - started < 30
    assert not (tmp_path / "slow-finished").exists()
    output = capsys.readouterr().out
    failure = output.split("intermittent: investigate; repetition 1; ")[1].splitlines()[0]
    assert failure == "test_sample.py::test_fails"
    assert "inconclusive" not in output


def test_guard_expired_budget_and_empty_selection(monkeypatch, tmp_path):
    def no_process(*args, **kwargs):
        pytest.fail("must not start pytest")

    monkeypatch.setattr(flake_guard.subprocess, "Popen", no_process)
    assert flake_guard.run_repetitions([], tmp_path, time.monotonic() - 1) == 0
    assert flake_guard.run_repetitions(["test.py"], tmp_path, time.monotonic() - 1) == 0


def test_seeded_collection_is_repeatable_and_changes_order(monkeypatch):
    monkeypatch.setattr(test_quarantine, "load_quarantine", lambda _: {})

    class Config:
        def getoption(self, name):
            return 42 if name == "--flake-seed" else False

    original = [SimpleNamespace(nodeid=str(i)) for i in range(20)]
    first, second = original.copy(), original.copy()
    test_quarantine.pytest_collection_modifyitems(Config(), first)
    test_quarantine.pytest_collection_modifyitems(Config(), second)
    assert first == second and first != original


@pytest.mark.parametrize("content", [None, "<truncated", "<testsuites/>"])
def test_guard_handles_missing_or_incomplete_failure_reports(tmp_path, content):
    report = tmp_path / "results.xml"
    if content is not None:
        report.write_text(content)
    assert "pytest" in flake_guard.failure_names(report)[0]


def test_guard_diffs_merge_group_base_including_removed_and_renamed_modules(monkeypatch, tmp_path):
    base = "a" * 40
    commands = []
    selected = []
    report = tmp_path / "results.xml"
    monkeypatch.setattr(
        sys, "argv", ["flake_guard.py", "--base", base, "--durations-from", str(report)]
    )

    def diff(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 20
        return SimpleNamespace(stdout="semantic_rails/old.py\nsemantic_rails/new.py\n")

    def select(root, changed):
        selected.extend(changed)
        return []

    monkeypatch.setattr(flake_guard.subprocess, "run", diff)
    monkeypatch.setattr(flake_guard, "select_tests", select)

    def run(files, root, deadline, durations_from):
        assert files == [] and root == flake_guard.ROOT
        assert durations_from == report
        return 0

    monkeypatch.setattr(flake_guard, "run_repetitions", run)
    assert flake_guard.main() == 0
    assert commands == [
        ["git", "diff", "--name-only", "--no-renames", "--diff-filter=ACDM", base, "HEAD", "--"]
    ]
    assert selected == ["semantic_rails/old.py", "semantic_rails/new.py"]


def test_workflow_limits_guard_to_hosted_merge_groups_and_validates_quarantine():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    backend = workflow["jobs"]["backend"]
    assert backend["runs-on"] == "ubuntu-latest"
    guard = next(
        step for step in backend["steps"] if step.get("name") == "Catch intermittent failures"
    )
    assert guard["if"] == "github.event_name == 'merge_group' && matrix.python-version == '3.12'"
    assert guard["timeout-minutes"] <= 5
    assert guard["env"]["MERGE_BASE"] == "${{ github.event.merge_group.base_sha }}"
    assert not guard.get("continue-on-error")
    assert '--base "$MERGE_BASE" --durations-from backend-results.xml' in guard["run"]
    assert any("--validate-quarantine" in step.get("run", "") for step in backend["steps"])


@pytest.mark.parametrize(
    "cases, succeeds",
    [
        ([], False),
        ([("unrelated", False)], False),
        ([("test_adbc_adapter", False)], False),
        ([("test_adbc_snowflake", False)], False),
        ([("test_adbc_adapter", True), ("test_adbc_snowflake", False)], False),
        ([("test_adbc_adapter", False), ("test_adbc_snowflake", True)], False),
        ([("test_adbc_adapter", False), ("test_adbc_snowflake", False)], True),
    ],
)
def test_adbc_ci_guard_requires_both_modules_without_skips(tmp_path, cases, succeeds):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    guard = next(
        step
        for step in workflow["jobs"]["backend"]["steps"]
        if step.get("name") == "Verify ADBC tests ran without skips"
    )
    script = guard["run"].split("uv run --no-sync python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    suite = ET.Element("testsuite")
    for module, skipped in cases:
        case = ET.SubElement(
            suite, "testcase", classname=f"tests.semantic_rails.{module}", name="test_value"
        )
        if skipped:
            ET.SubElement(case, "skipped")
    ET.ElementTree(suite).write(tmp_path / "backend-results.xml")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), "SR_SHARD_COUNT": "1", "SR_SHARD_INDEX": "0"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if succeeds else 1), result.stdout + result.stderr


def test_dependabot_updates_uv_lock_weekly_and_keeps_actions():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert [entry["package-ecosystem"] for entry in config["updates"]] == ["uv", "github-actions"]
    python = config["updates"][0]
    assert python["schedule"]["interval"] == "weekly"
    assert set(python["groups"]) == {"python-deps", "python-deps-major"}


def test_release_hygiene_accepts_only_the_ci_manifests():
    from scripts.verify_release_readiness import HYGIENE_FORBIDDEN_PATH_PATTERNS

    assert not any(
        pattern.search("tests/quarantine.toml") for pattern in HYGIENE_FORBIDDEN_PATH_PATTERNS
    )
    assert not any(
        pattern.search("tests/shard_durations.json") for pattern in HYGIENE_FORBIDDEN_PATH_PATTERNS
    )
    assert any(
        pattern.search("tests/shard_durations.json.bak")
        for pattern in HYGIENE_FORBIDDEN_PATH_PATTERNS
    )
    assert any(
        pattern.search("tests/unrelated.toml") for pattern in HYGIENE_FORBIDDEN_PATH_PATTERNS
    )


@pytest.mark.parametrize("count", [1, 3, 4, 7])
def test_file_shards_partition_complete_collection(monkeypatch, tmp_path, count):
    (tmp_path / "tests").mkdir()
    for file in range(30):
        (tmp_path / f"tests/test_{file}.py").touch()
    monkeypatch.setenv("SR_SHARD_COUNT", str(count))
    items = [
        SimpleNamespace(nodeid=f"tests/test_{file}.py::test_value[{case}]")
        for file in range(30)
        for case in range(3)
    ]
    partitions = []
    for index in range(count):
        monkeypatch.setenv("SR_SHARD_INDEX", str(index))
        selected = items.copy()
        deselected = []
        config = SimpleNamespace(
            rootpath=tmp_path,
            hook=SimpleNamespace(
                pytest_deselected=lambda items, target=deselected: target.extend(items)
            ),
        )
        hook = test_sharding.pytest_collection_modifyitems(config, selected)
        next(hook)
        assert selected == items  # Other hooks still see the full collection.
        with pytest.raises(StopIteration):
            next(hook)
        assert len(selected) + len(deselected) == len(items)
        partition = {item.nodeid for item in selected}
        assert all(not partition & previous for previous in partitions)
        partitions.append(partition)
        for file in range(30):
            assert sum(node.startswith(f"tests/test_{file}.py::") for node in partition) in (0, 3)
        assert all(test_sharding.in_shard(item.nodeid, tmp_path) for item in selected)
    assert set.union(*partitions) == {item.nodeid for item in items}


@pytest.mark.parametrize("count,index", [("0", "0"), ("3", "-1"), ("3", "3"), ("x", "0")])
def test_shards_reject_invalid_configuration(monkeypatch, count, index):
    monkeypatch.setenv("SR_SHARD_COUNT", count)
    monkeypatch.setenv("SR_SHARD_INDEX", index)
    with pytest.raises(pytest.UsageError):
        test_sharding.in_shard("tests/test_sample.py::test_value")


@pytest.mark.parametrize("parallel", [False, True])
def test_quarantine_validates_ids_outside_current_shard(monkeypatch, tmp_path, parallel):
    # The temporary project has one file, owned by shard zero.
    monkeypatch.setenv("SR_SHARD_COUNT", "3")
    monkeypatch.setenv("SR_SHARD_INDEX", "1")
    result = plugin_run(tmp_path, "def test_failure(): pass\n", parallel=parallel, sharded=True)
    assert result.returncode == 5, result.stdout + result.stderr  # No tests in this shard.
    assert "quarantine tests no longer exist" not in result.stdout + result.stderr


def test_backend_matrix_and_push_policy():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    backend = jobs["backend"]
    assert backend["strategy"]["matrix"]["shard"] == [0, 1, 2, 3]
    assert backend["env"] == {"SR_SHARD_COUNT": "4", "SR_SHARD_INDEX": "${{ matrix.shard }}"}
    assert "github.event_name != 'push'" in backend["if"]
    assert "github.event_name == 'merge_group'" in backend["if"]
    assert "needs.changes.outputs.backend == 'true'" in backend["if"]
    assert not backend["strategy"]["fail-fast"]
    assert "backend" in jobs["all-checks"]["needs"]
    clickhouse = next(
        step for step in backend["steps"] if step.get("name", "").startswith("ClickHouse")
    )
    assert "matrix.shard == 0" in clickhouse["if"]
    assert clickhouse["env"] == {"SR_SHARD_COUNT": "1", "SR_SHARD_INDEX": "0"}
    upload = next(
        step for step in backend["steps"] if step.get("name") == "Upload backend test results"
    )
    assert (
        upload["with"]["name"]
        == "backend-results-py${{ matrix.python-version }}-${{ matrix.shard }}"
    )


@pytest.mark.parametrize(
    "event, versions",
    [
        ("pull_request", ["3.12"]),
        ("merge_group", ["3.11", "3.12", "3.13", "3.14"]),
        ("workflow_dispatch", ["3.11", "3.12", "3.13", "3.14"]),
    ],
)
def test_backend_matrix_covers_versions_for_each_event(event, versions):
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    matrix = jobs["backend"]["strategy"]["matrix"]
    expression = matrix["python-version"]
    assert expression == (
        "${{ fromJSON(github.event_name == 'pull_request' && '[\"3.12\"]' "
        '|| \'["3.11", "3.12", "3.13", "3.14"]\') }}'
    )
    # Evaluate this constrained expression with Python's equivalent boolean operators.
    translated = (
        expression.removeprefix("${{ ")
        .removesuffix(" }}")
        .replace("github.event_name", repr(event))
        .replace("&&", "and")
        .replace("||", "or")
    )
    selected = eval(translated, {"__builtins__": {}, "fromJSON": json.loads})
    assert selected == versions
    assert len(selected) * len(matrix["shard"]) == (4 if event == "pull_request" else 16)
    assert jobs["all-checks"]["needs"] == [
        "changes",
        "backend",
        "lint",
        "security",
        "docs",
        "postgres",
    ]


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "workflow_dispatch"])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_aggregate_requires_event_backend_matrix(event, result):
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    gate = jobs["all-checks"]
    step = gate["steps"][0]
    script = step["run"].split("python3 - <<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs["backend"]["result"] = result
    if event == "merge_group":
        needs["changes"]["result"] = "skipped"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RESULTS": json.dumps(needs), "EVENT": event},
        capture_output=True,
        text=True,
        timeout=10,
    )
    succeeds = result == "success" or (result == "skipped" and event != "merge_group")
    assert completed.returncode == (0 if succeeds else 1), completed.stderr
    if not succeeds:
        assert "backend" in completed.stderr


def test_real_collections_have_complete_disjoint_shards(tmp_path):
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    for index in range(12):
        (tmp_path / f"test_{index}.py").write_text(
            "import pytest\n@pytest.mark.parametrize('value', [1, 2])\n"
            "def test_value(value): pass\n"
        )
    partitions = []
    for index in range(3):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-c",
                str(config),
                "-p",
                "scripts.test_sharding",
                str(tmp_path),
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "SR_SHARD_COUNT": "3",
                "SR_SHARD_INDEX": str(index),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        partition = {line for line in result.stdout.splitlines() if "::test_value[" in line}
        assert all(not partition & previous for previous in partitions)
        partitions.append(partition)
    assert len(set.union(*partitions)) == 24


def test_guard_repeats_only_files_in_its_shard(monkeypatch):
    files = test_sharding.test_files(ROOT)[:30]
    monkeypatch.setenv("SR_SHARD_COUNT", "3")
    monkeypatch.setenv("SR_SHARD_INDEX", "1")
    monkeypatch.setattr(sys, "argv", ["flake_guard.py", "--base", "a" * 40])
    monkeypatch.setattr(
        flake_guard.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="")
    )
    monkeypatch.setattr(flake_guard, "select_tests", lambda *args: files)
    expected = [file for file in files if test_sharding.in_shard(file)]
    assert expected and len(expected) < len(files)

    def run(selected, *args):
        assert selected == expected
        return 0

    monkeypatch.setattr(flake_guard, "run_repetitions", run)
    assert flake_guard.main() == 0


@pytest.mark.parametrize("count", [1, 2, 4])
def test_cost_assignment_is_deterministic_and_balanced(count):
    costs = {f"test_{index}.py": float(cost) for index, cost in enumerate([9, 8, 7, 6, 5, 4, 3, 2])}
    files = list(costs)
    owners = test_sharding.assign_files(files, costs, count)
    assert owners == test_sharding.assign_files(list(reversed(files)), costs, count)
    assert set(owners) == set(files)
    loads = [sum(costs[file] for file in files if owners[file] == index) for index in range(count)]
    assert max(loads) - min(loads) <= max(costs.values())
    if count == 2:
        assert loads == [22, 22]
        assert owners == {
            "test_0.py": 0,
            "test_1.py": 1,
            "test_2.py": 1,
            "test_3.py": 0,
            "test_4.py": 0,
            "test_5.py": 1,
            "test_6.py": 1,
            "test_7.py": 0,
        }


def test_unknown_files_use_median_cost():
    assert test_sharding.assign_files(["a", "b", "new"], {"a": 10, "b": 2}, 2) == {
        "a": 0,
        "new": 1,
        "b": 1,
    }
    assert test_sharding.assign_files(["b", "a"], {}, 2) == {"a": 0, "b": 1}


@pytest.mark.parametrize("cost", [0, -1, float("nan"), float("inf")])
def test_cost_assignment_rejects_invalid_costs(cost):
    with pytest.raises(ValueError, match="finite and positive"):
        test_sharding.assign_files(["a"], {"a": cost}, 2)


def test_duration_table_takes_slowest_file_total_including_classes(tmp_path):
    reports = []
    for index, times in enumerate([(2, 3), (4, 5)]):
        report = tmp_path / f"{index}.xml"
        report.write_text(
            "<testsuites><testsuite>"
            f'<testcase classname="tests.test_sample" time="{times[0]}"/>'
            f'<testcase classname="tests.test_sample.TestClass" time="{times[1]}"/>'
            '<testcase classname="tests.test_other" time="1"/>'
            "</testsuite></testsuites>"
        )
        reports.append(report)
    assert update_shard_durations.duration_table(reports, ["tests/test_sample.py"]) == {
        "tests/test_sample.py": 9
    }
    assert update_shard_durations.duration_table(
        list(reversed(reports)), ["tests/test_sample.py"]
    ) == {"tests/test_sample.py": 9}


def test_subset_collection_preserves_full_suite_ownership(monkeypatch, tmp_path):
    (tmp_path / "tests").mkdir()
    files = [f"tests/test_{index}.py" for index in range(8)]
    for file in files:
        (tmp_path / file).touch()
    monkeypatch.setenv("SR_SHARD_COUNT", "4")
    full = test_sharding.shard_owners(tmp_path, 4)
    assert set(full.values()) == {0, 1, 2, 3}
    for index in range(4):
        monkeypatch.setenv("SR_SHARD_INDEX", str(index))
        selected = [SimpleNamespace(nodeid=files[3] + "::test_value")]
        config = SimpleNamespace(
            rootpath=tmp_path, hook=SimpleNamespace(pytest_deselected=lambda **kw: None)
        )
        hook = test_sharding.pytest_collection_modifyitems(config, selected)
        next(hook)
        with pytest.raises(StopIteration):
            next(hook)
        assert bool(selected) == (full[files[3]] == index)
        assert test_sharding.in_shard(files[3], tmp_path) == bool(selected)


def test_duration_generator_warns_for_oversize_file(monkeypatch, tmp_path, capsys):
    report = tmp_path / "report.xml"
    report.write_text('<testsuite><testcase classname="tests.test_slow" time="3000"/></testsuite>')
    monkeypatch.setattr(update_shard_durations, "ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_slow.py").touch()
    output = tmp_path / "durations.json"
    monkeypatch.setattr(
        sys, "argv", ["update_shard_durations.py", str(report), "--output", str(output)]
    )
    update_shard_durations.main()
    assert (
        "tests/test_slow.py alone exceeds target; split this file by hand"
        in capsys.readouterr().out
    )
    assert json.loads(output.read_text()) == {"tests/test_slow.py": 3000}
