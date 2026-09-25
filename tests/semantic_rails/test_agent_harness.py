"""scripts/agent_harness: scripted model replies against a real stdio MCP server."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.agent_harness import report, run, terminal

SERVER = '''
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")

@mcp.tool()
def add(a: int, b: int) -> dict[str, Any]:
    """Add two numbers; the result is structuredContent."""
    return {"ok": True, "sum": a + b}

@mcp.tool()
def fail(reason: str) -> dict:
    """Refuse, the way Semantic Rails tools report an error, as JSON text only."""
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


def serve(replies: list, usage_kind: str) -> tuple[ThreadingHTTPServer, list[dict]]:
    """A chat endpoint that answers request n with replies[n] and reports full, partial or no usage."""
    requests: list[dict] = []

    class Model(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            reply = replies[len(requests) - 1]
            message = {"content": reply} if isinstance(reply, str) else {"tool_calls": reply}
            usage = {"prompt_tokens": 100 * len(requests), "completion_tokens": 10}
            if usage_kind == "full":
                usage["completion_tokens_details"] = {"reasoning_tokens": 4}
            choice = {"message": {"role": "assistant", **message}, "finish_reason": "stop"}
            reply = {"choices": [choice], **({"usage": usage} if usage_kind != "none" else {})}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args: object) -> None:
            pass

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    return model, requests


@pytest.mark.parametrize(
    ("replies", "usage", "max_tokens", "stop", "turns"),
    [
        (FRICTION, "full", 10_000, "final", 3),
        ([[ADD]] * 4, "partial", 10_000, "loop", 3),
        ([[ADD]] * 4, "full", 150, "max_tokens", 2),
        ([[ADD]] * 4, "none", 10_000, "no usage reported", 1),
    ],
)
def test_run_records_tokens_friction_and_the_check(
    tmp_path, monkeypatch, replies, usage, max_tokens, stop, turns
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
    model, requests = serve(replies, usage)
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
    # a category the server doesn't report is unavailable, never a measured 0
    assert summary["reasoning_tokens"] == (4 * turns if usage == "full" else None)
    assert ("reasoning_tokens" in summary["unavailable"]) == (usage != "full")
    assert (summary["total_tokens"] is None) == (usage == "none")
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
    assert (
        shown[0] == '{"ok":true,"sum":3}' and "REFUSED" in shown[1] and "unknown tool" in shown[4]
    )
    events = (out / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in events].count("call") == 6
    assert all(type(value) is int for value in add.values() if not isinstance(value, dict))
    table = report.report([summary])
    assert (
        table.index("| add | 177 |") < table.index("| ghost | 70 |") < table.index("| fail | 37 |")
    )
    assert "Never called in any run: unused" in table


PROGRAM = """
import os, sys
print("\\x1b[1mready\\x1b[0m", os.environ.get("JAILED", "-"), os.environ.get("AGENT_API_KEY", "-"))
n = 0
while (line := input()) != "quit":
    n += 1
    print(n, "you said", line, flush=True)
sys.exit(3)
"""


def test_terminal_tools_drive_a_program_in_a_pty(tmp_path, monkeypatch):
    (tmp_path / "program.py").write_text(PROGRAM, encoding="utf-8")
    scenario = {
        "task": "Say hello.",
        "terminal": {"echo": f"{sys.executable} {tmp_path / 'program.py'}"},
    }
    (tmp_path / "scenario.yml").write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(terminal, "QUIET", 0.3)
    early, unknown = call("term_type", {"text": "early"}), call("term_start", {"program": "sh"})
    start, hello = call("term_start", {"program": "echo"}), call("term_type", {"text": "hello"})
    bad_wait, stop = call("term_read", {"wait_ms": "soon"}), call("term_type", {"text": "quit"})
    enter = call(
        "term_key", {"key": "enter"}
    )  # three in a row, as when accepting a wizard's defaults
    turns = [[early, unknown, start], [hello, bad_wait], [enter], [enter], [enter], [stop], "Done."]
    model, requests = serve(turns, "full")
    monkeypatch.setenv("AGENT_API_KEY", "not-for-programs")
    out, url = tmp_path / "run", f"http://127.0.0.1:{model.server_port}/v1"
    try:
        argv = [
            str(tmp_path / "scenario.yml"),
            "--out",
            str(out),
            "--model",
            "m",
            "--base-url",
            url,
        ]
        run.main([*argv, "--jail", "env JAILED=yes"])
    finally:
        model.shutdown()

    summary = json.loads((out / "summary.json").read_text())
    assert summary["stop"] == "final" and summary["unused_tools"] == []
    assert summary["friction"]["term_key"]["repeats"] == 0  # the output changed each time
    offered = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert offered == {"term_start", "term_type", "term_key", "term_read"}
    shown = [m["content"] for m in requests[-1]["messages"] if m["role"] == "tool"]
    assert shown[0].startswith("error: no program is running") and "unknown program" in shown[1]
    assert shown[2].strip() == "ready yes -"  # escapes removed; the jail ran it; no API key
    assert "1 you said hello" in shown[3] and "4 you said" in shown[7]
    assert "[the program exited with code 3]" in shown[8]
    assert shown[4].startswith("error: invalid literal")  # a bad argument is an error, not a crash
    friction = summary["friction"]
    assert friction["term_type"]["errors"] == 1 and friction["term_start"]["errors"] == 1
    assert friction["term_read"]["errors"] == 1


def test_terminal_replaces_its_program_and_keeps_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "QUIET", 0.3)
    loud = tmp_path / "loud.py"
    loud.write_text("import sys\nprint('x' * 7000)\nprint(sys.argv[1:], flush=True)\ninput()\n")
    shell = terminal.Terminal({"loud": f"{sys.executable} {loud}"}, tmp_path, [])
    try:
        text, error = shell.call("term_start", {"program": "loud", "args": "--a 'b c'"})
        assert error == "" and text.startswith("[1") and "earlier characters]" in text
        assert text.rstrip().endswith(
            "['--a', 'b c']"
        )  # a string of arguments is split, not spelled out
        assert shell.call("term_read", {"wait_ms": 0}) == ("[no new output]", "")
        first = shell.proc
        shell.call("term_start", {"program": "loud"})
        assert first is not None and first.poll() is not None and shell.proc is not first
        text, error = shell.call("term_key", {"key": "enter"})
        assert error == "" and "[the program exited with code 0]" in text
    finally:
        shell.close()
    assert (
        terminal.clean("abc\rdef\r") == "def"
        and terminal.clean("\x1b[1mbold\x1b[0m\r\n") == "bold\n"
    )
