"""Hardening regressions from the June 2026 security audit.

Covers: the non-loopback default-resolver warning (F2), Snowflake CLI
error-detail redaction and the envelope-level scrub backstop (F3), and
request-body / pagination bounds on the stdlib transports (F4).
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from types import SimpleNamespace

import pytest

from semantic_rails.api import AppState, Handler
from semantic_rails.db import DuckDBAdapter, SnowflakeCliAdapter, SnowflakeNativeAdapter
from semantic_rails.db_parts.bigquery import BigQueryNativeAdapter
from semantic_rails.db_parts.clickhouse import ClickHouseAdapter
from semantic_rails.db_parts.postgres import PostgresAdapter
from semantic_rails.diagnostics import exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import MAX_REQUEST_BODY_BYTES, SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import _read_json
from semantic_rails.metadata_parts.valid_values import (
    max_valid_values_limit,
    max_valid_values_offset,
)
from semantic_rails.request_context import (
    HeaderPolicyContextResolver,
    set_policy_context_resolver,
    warn_if_default_policy_resolver_exposed,
)

# --- F2: default resolver exposure warning -------------------------------


def test_warning_fires_for_non_loopback_bind_with_default_resolver(capsys):
    assert warn_if_default_policy_resolver_exposed("0.0.0.0", transport="test") is True
    err = capsys.readouterr().err
    assert "self-assert" in err
    assert "set_policy_context_resolver" in err


def test_warning_fires_for_empty_host_binding_all_interfaces(capsys):
    assert warn_if_default_policy_resolver_exposed("", transport="test") is True
    assert "all interfaces" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.1.2.3"])
def test_warning_silent_on_loopback(host, capsys):
    assert warn_if_default_policy_resolver_exposed(host, transport="test") is False
    assert capsys.readouterr().err == ""


def test_warning_silent_with_custom_resolver(capsys):
    class _IdentityResolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            raise NotImplementedError

    set_policy_context_resolver(_IdentityResolver())
    try:
        assert warn_if_default_policy_resolver_exposed("0.0.0.0", transport="test") is False
        assert capsys.readouterr().err == ""
    finally:
        set_policy_context_resolver(HeaderPolicyContextResolver())


# --- F3: Snowflake CLI error redaction + envelope scrub ------------------


@pytest.mark.parametrize(
    "adapter_type,connection_method",
    [
        (PostgresAdapter, "_connection"),
        (BigQueryNativeAdapter, "client"),
        (ClickHouseAdapter, "_client_handle"),
        (SnowflakeNativeAdapter, "_connection"),
        (DuckDBAdapter, ""),
        (SnowflakeCliAdapter, ""),
    ],
)
def test_driver_failures_stay_private_across_public_envelopes(
    adapter_type, connection_method, runtime_factory, monkeypatch, caplog
):
    sql = "SELECT SYNTHETIC_PRIVATE_COLUMN FROM SYNTHETIC_PRIVATE_TABLE"
    private_message = f"SYNTHETIC_PASSWORD=pw; {sql}; SYNTHETIC_ROW=secret"
    original = RuntimeError(private_message)

    def fail(*args, **kwargs):
        raise original

    adapter = adapter_type.__new__(adapter_type)
    adapter.options = {}
    adapter.connection_name = "test"
    if connection_method:
        monkeypatch.setattr(adapter, connection_method, fail)
    elif adapter_type is DuckDBAdapter:
        adapter._db = SimpleNamespace(query=fail)
    else:
        monkeypatch.setattr(
            "semantic_rails.db.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout=private_message, stderr=private_message
            ),
        )

    with pytest.raises(SemanticLayerError) as captured:
        adapter.query(sql)
    error = captured.value
    assert error.code == "QUERY_EXECUTION_ERROR"
    assert error.details["sql_redacted"] is True
    if adapter_type is not SnowflakeCliAdapter:
        assert error.__cause__ is original

    runtime = runtime_factory("jaffle_shop")
    service = SemanticHTTPService(runtime)
    http_payload, status = service.exception_payload(error, stage="http")
    assert status == 400
    mcp_payload = SemanticLayerMCPAdapter(runtime)._error_response(error, {})
    for payload in (http_payload, mcp_payload):
        assert "SYNTHETIC_" not in json.dumps(payload)
        assert payload["errors"][0]["code"] == "QUERY_EXECUTION_ERROR"
    assert "SYNTHETIC_" not in caplog.text


def test_segment_driver_failure_keeps_segment_identity(runtime_factory, monkeypatch):
    runtime = runtime_factory("jaffle_shop")
    adapter = DuckDBAdapter.__new__(DuckDBAdapter)

    def fail(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_DRIVER_MESSAGE")

    adapter._db = SimpleNamespace(query=fail)
    monkeypatch.setattr(runtime, "_get_adapter", lambda: adapter)
    with pytest.raises(SemanticLayerError) as captured:
        runtime.segment_preview("segment.jaffle.high_value_customers", limit=1)
    error = captured.value
    assert error.details["segment_id"] == "segment.jaffle.high_value_customers"
    assert error.details["sql_redacted"] is True
    assert "sql" not in error.details
    assert "SYNTHETIC_" not in json.dumps(exception_issue(error, stage="execute"))


def test_snowflake_cli_failure_details_have_no_raw_output(monkeypatch):
    monkeypatch.setattr(
        "semantic_rails.db.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout='[{"SECRET_ROW": 1}]',
            stderr="SQL compilation error " + "x" * 2000,
        ),
    )
    with pytest.raises(SemanticLayerError) as exc:
        SnowflakeCliAdapter("trial", options={"warehouse": "wh"}).query("select secret from t")
    details = exc.value.details
    assert "stdout" not in details
    assert "stderr" not in details
    assert "options" not in details
    assert "sql" not in details
    assert details["sql_redacted"] is True
    assert details["option_keys"] == ["warehouse"]
    assert details["exit_code"] == 1
    # Driver text may contain SQL and credentials even when short.
    assert "SQL compilation error" not in str(exc.value)
    assert len(str(exc.value)) < 700


def test_snowflake_cli_invalid_json_details_have_no_stdout_or_sql(monkeypatch):
    monkeypatch.setattr(
        "semantic_rails.db.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    with pytest.raises(SemanticLayerError) as exc:
        SnowflakeCliAdapter("trial").query("select secret from t")
    details = exc.value.details
    assert "stdout" not in details
    assert "sql" not in details
    assert details["stdout_redacted"] is True
    assert details["sql_redacted"] is True
    assert details["connection"] == "trial"


def test_exception_issue_scrubs_raw_adapter_output():
    exc = SemanticLayerError(
        "QUERY_EXECUTION_ERROR",
        "boom",
        details={
            "engine": "snowflake",
            "stdout": "rows",
            "stderr": "trace",
            "options": {"password_env": "X"},
            "sql": "select secret from t",
        },
    )
    issue = exception_issue(exc, stage="execution")
    details = issue["details"]
    assert "stdout" not in details
    assert "stderr" not in details
    assert "options" not in details
    assert "sql" not in details
    assert details["sql_redacted"] is True
    assert details["redacted_detail_keys"] == ["options", "stderr", "stdout", "sql"]


def test_exception_issue_keeps_debug_authorized_sql():
    exc = SemanticLayerError(
        "QUERY_EXECUTION_ERROR",
        "boom",
        details={"engine": "duckdb", "sql": "select 1", "sql_debug_authorized": True},
    )
    issue = exception_issue(exc, stage="execution")
    assert issue["details"]["sql"] == "select 1"


# --- F4: request body and pagination bounds ------------------------------


def test_mcp_stdlib_read_json_rejects_oversized_body():
    handler = SimpleNamespace(
        headers={"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}, rfile=None
    )
    with pytest.raises(ValueError, match="exceeds"):
        _read_json(handler)


def test_mcp_stdlib_read_json_rejects_invalid_content_length():
    handler = SimpleNamespace(headers={"Content-Length": "banana"}, rfile=None)
    with pytest.raises(ValueError, match="Content-Length"):
        _read_json(handler)


@contextlib.contextmanager
def _serve_runtime(runtime, package_id: str = "jaffle_shop"):
    state = AppState.__new__(AppState)
    state.runtime = runtime
    state.package_id = package_id

    class _Handler(Handler):
        pass

    _Handler.state = state
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_api_post_rejects_oversized_body_with_413(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        body = json.dumps({"pad": "x" * (MAX_REQUEST_BODY_BYTES + 1)}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/v1/validate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)  # nosec - local test server only
        assert exc.value.code == 413
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert any("KiB" in str(error.get("message", "")) for error in payload.get("errors", []))


def test_valid_values_caps_have_env_overrides(monkeypatch):
    monkeypatch.delenv("SEMANTIC_RAILS_MAX_VALID_VALUES_LIMIT", raising=False)
    monkeypatch.delenv("SEMANTIC_RAILS_MAX_VALID_VALUES_OFFSET", raising=False)
    assert max_valid_values_limit() == 1_000
    assert max_valid_values_offset() == 100_000
    monkeypatch.setenv("SEMANTIC_RAILS_MAX_VALID_VALUES_LIMIT", "5000")
    monkeypatch.setenv("SEMANTIC_RAILS_MAX_VALID_VALUES_OFFSET", "9")
    assert max_valid_values_limit() == 5000
    assert max_valid_values_offset() == 9


def test_segment_preview_clamps_caller_limit(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    preview = runtime.segment_preview("segment.jaffle.high_value_customers", limit=10**9)
    assert preview["preview_row_count"] <= 1_000


def test_valid_values_clamps_caller_limit_and_offset(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    from semantic_rails.metadata import valid_values_payload

    payload = valid_values_payload(
        runtime,
        dimension_id="dimension.jaffle_store_name",
        limit=10**9,
        offset=-5,
    )
    assert len(payload["values"]) <= 1_000
