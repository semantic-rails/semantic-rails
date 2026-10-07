"""Package failures remain readable over the real CLI's stdio protocol."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.conftest import copy_package_config

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUESTS = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "startup-test", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "discover", "arguments": {"terms": ""}},
    },
]


def _stdio(
    path: Path, *, inferred: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    env = os.environ.copy()
    # Keep inherited startup hooks while allowing an inferred package in another cwd.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails", "mcp", "stdio"]
        + ([] if inferred else ["--path", str(path)]),
        cwd=path if inferred else REPO_ROOT,
        env=env,
        input="".join(json.dumps(request) + "\n" for request in REQUESTS),
        capture_output=True,
        text=True,
        timeout=60,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == 3, (proc.stdout, proc.stderr)
    for reply, message_id in zip(replies, [1, 2, 3], strict=True):
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == message_id
        assert ("result" in reply) != ("error" in reply)
    return proc, replies


@pytest.mark.parametrize("inferred", [False, True])
def test_invalid_package_returns_engine_error_over_stdio(tmp_path: Path, inferred: bool) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop")
    graph_path = package / "graph.yml"
    raw = yaml.safe_load(graph_path.read_text())
    raw["graph"]["relationships"]["orders_customer"]["path_preference"] = 1
    graph_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    engine_error = raised.value
    assert engine_error.code == "INVALID_CONFIG"
    assert "path_preference" in str(engine_error)
    assert "record the route" in str(engine_error)

    proc, replies = _stdio(package, inferred=inferred)

    assert proc.returncode == 1
    assert str(engine_error) in proc.stderr
    for reply in replies:
        error = reply["error"]
        assert error["code"] == -32603
        assert error["message"] == str(engine_error)
        assert error["data"] == {
            "code": engine_error.code,
            "details": {**engine_error.details, "config_path": str(package)},
        }


@pytest.mark.parametrize(
    ("contents", "message"),
    [("schema_version: [", "expected"), ("schema_version: 99", "schema_version must be 1")],
)
def test_invalid_yaml_file_returns_structured_startup_error(
    tmp_path: Path, contents: str, message: str
) -> None:
    path = tmp_path / "package.yml"
    path.write_text(contents)

    proc, replies = _stdio(path)

    assert proc.returncode == 1
    for reply in replies:
        error = reply["error"]
        assert error["data"]["code"] == "INVALID_CONFIG"
        assert error["data"]["details"]["config_path"] == str(path)
        assert message in error["message"]
        assert error["message"] in proc.stderr


def test_missing_package_path_returns_structured_startup_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.yml"

    proc, replies = _stdio(path)

    assert proc.returncode == 1
    for reply in replies:
        assert reply["error"]["data"] == {
            "code": "INVALID_CONFIG",
            "details": {"config_path": str(path)},
        }
        assert str(path) in reply["error"]["message"]
        assert "does not exist" in reply["error"]["message"]


@pytest.mark.parametrize(
    ("relative_path", "keys", "value", "code", "details"),
    [
        (
            "models/core/orders.yml",
            ("model", "times"),
            ["ordered_at"],
            "INTERNAL_ERROR",
            {
                "exception_type": "AttributeError",
                "exception_message": "'list' object has no attribute 'items'",
            },
        ),
        (
            "package.yml",
            ("schema_version",),
            yaml.safe_load(".inf"),
            "INTERNAL_ERROR",
            {
                "exception_type": "OverflowError",
                "exception_message": "cannot convert float infinity to integer",
            },
        ),
        (
            "metrics/extensions/advanced_metrics.yml",
            ("metrics", "sales.session_to_order_conversion_rate_7d", "expression", "matching_mode"),
            yaml.safe_load("2026-10-03"),
            "CONVERSION_MATCHING_MODE_REQUIRED",
            {"received_value": "2026-10-03"},
        ),
    ],
    ids=["list-times", "infinite-schema-version", "date-matching-mode"],
)
def test_unexpected_package_values_return_structured_startup_error(
    tmp_path: Path,
    relative_path: str,
    keys: tuple[str, ...],
    value: Any,
    code: str,
    details: dict[str, Any],
) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", writable=True)
    path = package / relative_path
    raw = yaml.safe_load(path.read_text())
    target = raw
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    path.write_text(yaml.safe_dump(raw, sort_keys=False))

    proc, replies = _stdio(package)

    assert proc.returncode == 1
    for reply in replies:
        error = reply["error"]
        assert error["code"] == -32603
        assert error["data"]["code"] == code
        assert error["data"]["details"]["config_path"] == str(package)
        for key, expected in details.items():
            assert error["data"]["details"][key] == expected
        assert error["message"] in proc.stderr


def test_valid_package_keeps_handshake_and_tools(tmp_path: Path) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)

    proc, replies = _stdio(package)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    initialized, listed, called = [reply["result"] for reply in replies]
    assert initialized["protocolVersion"] == "2025-11-25"
    assert initialized["serverInfo"] == {"name": "semantic-rails", "version": "v2"}
    assert [tool["name"] for tool in listed["tools"]] == [
        "discover",
        "inspect",
        "valid-values",
        "plan",
        "execute",
        "segment",
    ]
    assert called["isError"] is False
    assert called["structuredContent"]["ok"] is True
    assert called["structuredContent"]["package_id"] == "jaffle_shop"
    assert "measure.jaffle.order_count" in called["structuredContent"]["catalog"]["measure_ids"]
    assert json.loads(called["content"][0]["text"]) == called["structuredContent"]
