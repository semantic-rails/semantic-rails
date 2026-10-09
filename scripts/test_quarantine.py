"""Expiring quarantine and seeded collection order for the test suite."""

from __future__ import annotations

import random
import tomllib
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

QUARANTINE = Path(__file__).resolve().parents[1] / "tests" / "quarantine.toml"


def load_quarantine(path: Path, today: date | None = None) -> dict[str, str]:
    """Return node IDs and reasons, rejecting malformed or overdue entries."""
    today = today or date.today()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"tests"} or not isinstance(data["tests"], list):
        raise ValueError("quarantine must contain a tests array")
    result = {}
    for entry in data["tests"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "reason", "upstream", "review_by"}:
            raise ValueError("quarantine entries require id, reason, upstream and review_by")
        for key in ("id", "reason", "upstream"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError(f"quarantine {key} must be a nonempty string")
        nodeid = entry["id"]
        if nodeid in result:
            raise ValueError(f"duplicate quarantine test: {nodeid}")
        link = urlsplit(entry["upstream"])
        if link.scheme != "https" or not link.netloc:
            raise ValueError(f"quarantine upstream must be an HTTPS link: {nodeid}")
        review_by = entry["review_by"]
        if isinstance(review_by, str):
            review_by = date.fromisoformat(review_by)
        if type(review_by) is not date:
            raise ValueError(f"quarantine review_by must be a date: {nodeid}")
        if not today <= review_by <= today + timedelta(days=30):
            raise ValueError(f"quarantine review_by expired or exceeds 30 days: {nodeid}")
        result[nodeid] = f"{entry['reason']} ({entry['upstream']}; review by {review_by})"
    return result


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--validate-quarantine", action="store_true", help="check IDs in full collection"
    )
    parser.addoption("--flake-seed", type=int, help="shuffle collected tests with this shared seed")
    parser.addoption(
        "--flake-failures", type=Path, help="append each failed test ID to this file as it fails"
    )


class FailureLog:
    """Record failures as they are reported, so a run killed before its report still shows them."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            with self.path.open("a", encoding="utf-8") as log:
                log.write(report.nodeid + "\n")


def pytest_configure(config: pytest.Config) -> None:
    # Validate expiry on the controller before xdist starts any workers.
    try:
        load_quarantine(QUARANTINE)
    except (OSError, ValueError) as exc:
        raise pytest.UsageError(str(exc)) from exc
    failures = config.getoption("--flake-failures")
    # The xdist controller also receives worker crashes, which never reach a worker's own hooks.
    if failures is not None and not hasattr(config, "workerinput"):
        config.pluginmanager.register(FailureLog(failures))


def collection_error(config: pytest.Config, message: str) -> None:
    # A collection report reaches the xdist controller; UsageError in a worker
    # can instead disappear behind an unrelated worker-shutdown error.
    config.hook.pytest_collectreport(
        report=pytest.CollectReport(
            nodeid="tests/quarantine.toml",
            outcome="failed",
            longrepr=message,
            result=[],
            location=("tests/quarantine.toml", 0, "quarantine"),
        )
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    try:
        entries = load_quarantine(QUARANTINE)
    except (OSError, ValueError) as exc:
        collection_error(config, str(exc))
        return
    if config.getoption("--validate-quarantine"):
        missing = entries.keys() - {item.nodeid for item in items}
        if missing:
            collection_error(
                config, f"quarantine tests no longer exist: {', '.join(sorted(missing))}"
            )
            return
    for item in items:
        if item.nodeid in entries:
            item.add_marker(
                pytest.mark.xfail(reason=entries[item.nodeid], strict=False), append=False
            )
    seed = config.getoption("--flake-seed")
    if seed is not None:
        # xdist workers must collect exactly the same order.
        random.Random(seed).shuffle(items)
