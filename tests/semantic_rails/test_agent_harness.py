"""scripts/agent_harness: scripted model replies against a real stdio MCP server."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.agent_harness import report, run

SERVER = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")

@mcp.tool()
def add(a: int, b: int) -> dict:
    """Add two numbers."""
    return {"ok": True, "sum": a + b}

@mcp.tool()
def fail(reason: str) -> dict:
    """Refuse, the way Semantic Rails tools report an error."""
    return {"ok": False, "error": {"code": "REFUSED", "message": reason}}

@mcp.tool()
def unused() -> str:
    """Never called."""
    return "unused"

mcp.run()
'''


def call(name: str, args: object) -> dict:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": raw}}


ADD = call("add", {"a": 1, "b": 2})
FRICTION = [  # errors, a repeat, bad arguments, an unknown tool, then an answer
    [ADD, call("fail", {"reason": "no"}), call("add", {"a": 1})],
    [ADD, call("ghost", {}), call("add", "{not json")],
    "The sum is 3.",
]


def serve(replies: list) -> tuple[ThreadingHTTPServer, list[dict]]:
    """A chat endpoint that answers request n with replies[n] and reports usage."""
    requests: list[dict] = []

    class Model(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            reply = replies[len(requests) - 1]
            message = {"content": reply} if isinstance(reply, str) else {"tool_calls": reply}
            usage = {"prompt_tokens": 100 * len(requests), "completion_tokens": 10}
            usage["completion_tokens_details"] = {"reasoning_tokens": 4}
            choice = {"message": {"role": "assistant", **message}, "finish_reason": "stop"}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [choice], "usage": usage}).encode())

        def log_message(self, *args: object) -> None:
            pass

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    return model, requests


@pytest.mark.parametrize(
    ("replies", "max_tokens", "stop", "turns"),
    [
        (FRICTION, 10_000, "final", 3),
        ([[ADD]] * 4, 10_000, "loop", 3),
        ([[ADD]] * 4, 150, "max_tokens", 2),
    ],
)
def test_run_records_tokens_friction_and_the_check(
    tmp_path, monkeypatch, replies, max_tokens, stop, turns
):
    (tmp_path / "server.py").write_text(SERVER, encoding="utf-8")
    scenario = {
        "task": "Add 1 and 2.",
        "servers": {"fixture": f"{sys.executable} {tmp_path / 'server.py'}"},
        "check": 'grep -q 3 "$AGENT_FINAL_ANSWER"',
        "max_tokens": max_tokens,
    }
    (tmp_path / "scenario.yml").write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # the run lock
    model, requests = serve(replies)
    url, out = f"http://127.0.0.1:{model.server_port}/v1", tmp_path / "run"
    try:
        run.main(
            [str(tmp_path / "scenario.yml"), "--out", str(out), "--model", "m", "--base-url", url]
        )
    finally:
        model.shutdown()

    summary = json.loads((out / "summary.json").read_text())
    assert (summary["stop"], summary["turns"], summary["success"]) == (stop, turns, stop == "final")
    assert requests[0]["model"] == "m" and requests[0]["tools"][0]["function"]["name"] == "add"
    assert summary["reasoning_tokens"] == 4 * turns
    if stop != "final":
        return
    totals = [summary[key] for key in ("prompt_tokens", "completion_tokens", "tool_calls")]
    assert totals == [600, 30, 6]
    assert summary["unused_tools"] == ["unused"]
    add, fail, ghost = (summary["friction"][name] for name in ("add", "fail", "ghost"))
    # add: a missing argument in turn 1 (110 tokens / 3 calls), a repeat and bad JSON in turn 2
    assert [add[key] for key in ("calls", "errors", "repeats", "bad_arguments")] == [4, 2, 1, 2]
    assert add["wasted_tokens"] == 37 + 70 + 70
    assert fail["errors_seen"] == {"REFUSED: no": 1}
    assert ghost["errors"] == 1 and "unknown tool" in next(iter(ghost["errors_seen"]))
    shown = [m["content"] for m in requests[2]["messages"] if m["role"] == "tool"]
    assert '"sum": 3' in shown[0] and "REFUSED" in shown[1] and "unknown tool" in shown[4]
    events = (out / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in events].count("call") == 6
    assert all(type(value) is int for value in add.values() if not isinstance(value, dict))
    table = report.report([summary])
    assert (
        table.index("| add | 177 |") < table.index("| ghost | 70 |") < table.index("| fail | 37 |")
    )
    assert "Never called in any run: unused" in table
