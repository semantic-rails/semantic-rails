"""Let a model behind an OpenAI-compatible chat API drive MCP servers; record what it cost.

    uv run python scripts/agent_harness/run.py SCENARIO.yml --out RUN_DIR --model MODEL

One request at a time, one run per machine at a time (a lock). AGENT_API_KEY, if set, is sent
as a bearer token. README.md describes the scenario file and the run folder.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import functools
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[2]
SYSTEM = (
    "You work for the user through the tools provided. Complete the request with the tools; "
    "don't ask the user questions. When you are done, reply with a short final answer and no "
    "tool call."
)
TOKENS = ("prompt_tokens", "completion_tokens", "reasoning_tokens")
Route = tuple[str, ClientSession, dict[str, Any]]  # MCP tool name, its session, its input schema


def load_scenario(path: Path, servers: list[str]) -> dict[str, Any]:
    scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
    subs = {"{repo}": str(REPO), "{here}": str(path.resolve().parent), "{python}": sys.executable}

    def fill(text: str) -> str:
        for key, value in subs.items():
            text = text.replace(key, value)
        return text

    merged = {**(scenario.get("servers") or {}), **dict(s.split("=", 1) for s in servers)}
    scenario["servers"] = {name: fill(command) for name, command in merged.items()}
    for key in ("setup", "check"):
        scenario[key] = fill(scenario.get(key) or "")
    scenario.setdefault("name", path.stem)
    return scenario


def chat(base_url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if os.environ.get("AGENT_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['AGENT_API_KEY']}"
    url = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(request, timeout=max(timeout, 1)) as response:
        return json.loads(response.read())


def result_text(result: Any) -> tuple[str, str]:
    """What the model sees (compact structuredContent, else the text blocks), and any error."""
    body = result.structuredContent
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    text = "".join(getattr(block, "text", "") for block in result.content)
    compact = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)  # as mcp_context
    shown = compact if body is not None else text
    if body is None:  # a text-only server may still answer in JSON
        with suppress(ValueError):
            body = json.loads(text)
    if not isinstance(body, dict):
        body = {}
    if not (result.isError or body.get("ok") is False):
        return shown, ""
    detail = body.get("error") or next(iter(body.get("errors") or []), None)
    if isinstance(detail, dict) and "message" in detail:
        return shown, f"{detail.get('code', 'ERROR')}: {detail['message']}"[:300]
    return shown, (json.dumps(detail, default=str) if detail else text or shown)[:300]


def arg_problems(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    props = schema.get("properties") or {}
    missing = [f"missing {key}" for key in schema.get("required") or [] if key not in args]
    return missing + [f"unknown {key}" for key in args if props and key not in props]


async def dispatch(routes: dict[str, Route], name: str, raw: Any, deadline: float) -> tuple:
    """Run one tool call; return (arguments, text for the model, error, argument problems)."""
    try:  # omitted or null arguments mean none
        args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except (TypeError, ValueError):
        args = None
    if not isinstance(args, dict):
        return {"_raw": raw}, "error: arguments must be a JSON object", "bad JSON", ["bad JSON"]
    if name not in routes:
        error = f"error: unknown tool {name!r}; the tools are {', '.join(routes)}"
        return args, error, f"unknown tool {name!r}", []
    tool, session, schema = routes[name]
    timeout = timedelta(seconds=max(1.0, deadline - time.monotonic()))
    try:
        result = await session.call_tool(tool, args, read_timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001 - the model sees the failure, as a host would show it
        return args, f"error: {exc}", f"call failed: {exc}"[:300], arg_problems(schema, args)
    return args, *result_text(result), arg_problems(schema, args)


async def connect(stack: AsyncExitStack, servers: dict[str, str], cwd: Path, errlog: TextIO):
    routes: dict[str, Route] = {}
    tools: list[dict[str, Any]] = []
    for server, command in servers.items():
        argv = shlex.split(command)
        params = StdioServerParameters(command=argv[0], args=argv[1:], cwd=str(cwd))
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        for tool in (await session.list_tools()).tools:
            name = tool.name if tool.name not in routes else f"{server}__{tool.name}"
            schema = tool.inputSchema or {"type": "object", "properties": {}}
            routes[name] = (tool.name, session, schema)
            spec = {"name": name, "description": tool.description or "", "parameters": schema}
            tools.append({"type": "function", "function": spec})
    return routes, tools


class Agent:
    """One conversation: the model's turns, its tool calls, and what each cost."""

    def __init__(self, opts: argparse.Namespace, task: str, routes: dict, events: TextIO):
        self.opts, self.routes, self.events = opts, routes, events
        self.messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
        self.deadline = time.monotonic() + opts.timeout
        self.turns: list[dict[str, int]] = []
        self.friction: defaultdict[str, Counter] = defaultdict(Counter)
        self.errors: defaultdict[str, Counter] = defaultdict(Counter)
        self.seen: Counter[str] = Counter()
        self.streak, self.last, self.final = 0, "", ""

    def log(self, **event: Any) -> None:
        print(json.dumps(event, default=str), file=self.events, flush=True)

    def clip(self, text: str) -> str:
        return text[: self.opts.result_chars] if self.opts.result_chars else text

    async def turn(self, request: dict[str, Any], max_tokens: int) -> str:
        """One model turn and its tool calls; return why to stop, or "" to go on."""
        started = time.monotonic()
        if started >= self.deadline:
            return "timeout"
        body = {**request, "messages": self.messages}
        try:
            reply = await asyncio.to_thread(chat, self.opts.base_url, body, self.deadline - started)
            choice, usage = reply["choices"][0], reply.get("usage") or {}
        except Exception as exc:  # noqa: BLE001 - recorded as the stop reason
            body_text = exc.read()[:300] if isinstance(exc, urllib.error.HTTPError) else b""
            return f"request failed: {exc!r} {body_text.decode(errors='replace')}".strip()
        message, finish = choice["message"], choice.get("finish_reason")
        calls, content = message.get("tool_calls") or [], message.get("content") or ""
        usage = {**(usage.get("completion_tokens_details") or {}), **usage}
        tokens = {key: usage.get(key) for key in TOKENS}  # None: the server didn't report it
        self.turns.append(tokens)
        names = [call["function"]["name"] for call in calls]
        seconds = round(time.monotonic() - started, 2)
        self.log(
            event="turn",
            turn=len(self.turns),
            **tokens,
            finish_reason=finish,
            seconds=seconds,
            content=self.clip(content),
            tool_calls=names,
        )
        if not calls or finish == "length":  # a reply cut off mid-call isn't dispatched
            self.final = content
            return "length" if finish == "length" else "final"
        self.messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        spent_now = (tokens["prompt_tokens"] or 0) + (tokens["completion_tokens"] or 0)
        for call in calls:
            await self.call(call, round(spent_now / len(calls)))
        if self.streak >= 3:
            return "loop"
        if None in (tokens["prompt_tokens"], tokens["completion_tokens"]):
            return "no usage reported"  # the token budget can't be enforced
        spent = sum(turn["prompt_tokens"] + turn["completion_tokens"] for turn in self.turns)
        return "max_tokens" if spent >= max_tokens else ""

    async def call(self, call: dict[str, Any], share: int) -> None:
        name, started = call["function"]["name"], time.monotonic()
        raw = call["function"].get("arguments")
        args, text, error, problems = await dispatch(self.routes, name, raw, self.deadline)
        key = name + json.dumps(args, sort_keys=True, default=str)
        self.seen[key] += 1
        self.streak, self.last = (self.streak + 1 if key == self.last else 1), key
        repeat = self.seen[key] > 1
        wasted = share if error or repeat else 0  # its share of the turn that made it
        counts = {
            "errors": int(bool(error)),
            "repeats": int(repeat),
            "bad_arguments": int(bool(problems)),
        }
        self.friction[name].update(calls=1, wasted_tokens=wasted, **counts)
        if error:
            self.errors[name][" ".join(error.split())[:160]] += 1  # one line per message
        seconds = round(time.monotonic() - started, 2)
        self.log(
            event="call",
            turn=len(self.turns),
            seq=self.seen.total(),
            tool=name,
            arguments=args,
            error=error,
            arg_problems=problems,
            repeat=repeat,
            result_chars=len(text),
            result=self.clip(text),
            seconds=seconds,
        )
        self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": text})

    def stats(self, stop: str) -> dict[str, Any]:
        unavailable = [key for key in TOKENS if any(turn[key] is None for turn in self.turns)]
        totals = {key: sum(turn[key] or 0 for turn in self.turns) for key in TOKENS}
        totals.update(dict.fromkeys(unavailable))  # never report a missing category as 0
        both = None not in (totals["prompt_tokens"], totals["completion_tokens"])
        prompts = [turn["prompt_tokens"] for turn in self.turns]
        return {
            "stop": stop,
            "finished": stop == "final",
            "turns": len(self.turns),
            "tool_calls": self.seen.total(),
            "tool_errors": sum(counts["errors"] for counts in self.friction.values()),
            **totals,
            "unavailable": unavailable,
            "total_tokens": totals["prompt_tokens"] + totals["completion_tokens"] if both else None,
            "peak_prompt_tokens": None if None in prompts else max(prompts, default=0),
            "tools_offered": len(self.routes),
            "unused_tools": sorted(set(self.routes) - set(self.friction)),
            "friction": {
                name: {**counts, "errors_seen": dict(self.errors[name])}
                for name, counts in self.friction.items()
            },
        }


async def run(opts: argparse.Namespace, scenario: dict[str, Any], cwd: Path) -> tuple[str, dict]:
    with (
        (opts.out / "transcript.jsonl").open("w", encoding="utf-8") as events,
        (opts.out / "servers.log").open("w", encoding="utf-8") as errlog,
    ):
        async with AsyncExitStack() as stack:
            routes, tools = await connect(stack, scenario["servers"], cwd, errlog)
            agent = Agent(opts, scenario["task"], routes, events)
            schema_chars = len(json.dumps(tools))
            agent.log(event="tools", tools=list(routes), schema_chars=schema_chars)
            request = {"model": opts.model, "tools": tools, "max_tokens": opts.turn_tokens}
            if opts.reasoning_effort:
                request["reasoning_effort"] = opts.reasoning_effort
            stop = "max_turns"
            for _ in range(scenario["max_turns"]):
                if reason := await agent.turn(request, scenario["max_tokens"]):
                    stop = reason
                    break
    return agent.final, {**agent.stats(stop), "tool_schema_chars": schema_chars}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="run folder to create")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    parser.add_argument("--reasoning-effort", default="", help="sent when set")
    parser.add_argument("--server", action="append", default=[], help="NAME=COMMAND to add")
    parser.add_argument("--workdir", type=Path, help="work here, not in a fresh RUN_DIR/workdir")
    parser.add_argument("--max-turns", type=int, help="overrides the scenario (default 30)")
    parser.add_argument("--max-tokens", type=int, help="overrides the scenario (default 500000)")
    parser.add_argument("--turn-tokens", type=int, default=8192, help="max_tokens per request")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds for the agent loop")
    parser.add_argument("--result-chars", type=int, default=2000, help="0 keeps results whole")
    opts = parser.parse_args(argv)
    if any("=" not in server for server in opts.server):
        parser.error("--server takes NAME=COMMAND")
    scenario = load_scenario(opts.scenario, opts.server)
    for key, default in (("max_turns", 30), ("max_tokens", 500_000)):
        value = getattr(opts, key)
        scenario[key] = value if value is not None else scenario.get(key, default)
    with Path(tempfile.gettempdir(), "agent-harness.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another agent-harness run is in progress on this machine")
        return execute(opts, scenario)


def execute(opts: argparse.Namespace, scenario: dict[str, Any]) -> int:
    opts.out.mkdir(parents=True)  # refuses an existing folder
    cwd = (opts.workdir or opts.out / "workdir").resolve()
    cwd.mkdir(exist_ok=True)
    shell = functools.partial(subprocess.run, shell=True, cwd=cwd, text=True)
    if scenario["setup"]:
        shell(scenario["setup"], check=True)
    started, utc = time.monotonic(), datetime.now(UTC).isoformat(timespec="seconds")
    final, stats = asyncio.run(run(opts, scenario, cwd))
    wall = round(time.monotonic() - started, 1)
    answer = opts.out / "final_answer.txt"
    answer.write_text(final, encoding="utf-8")
    check: dict[str, Any] = {"command": scenario["check"], "exit_code": None}
    if scenario["check"]:
        env = {**os.environ, "AGENT_FINAL_ANSWER": str(answer.resolve())}
        try:
            done = shell(scenario["check"], env=env, capture_output=True, timeout=600)
            check.update(exit_code=done.returncode, output=(done.stdout + done.stderr)[-2000:])
        except subprocess.TimeoutExpired:
            check["output"] = "the check timed out after 600 seconds"
    summary = {
        **{key: scenario[key] for key in ("name", "servers", "max_turns", "max_tokens")},
        "scenario_sha256": hashlib.sha256(opts.scenario.read_bytes()).hexdigest(),
        **{key: getattr(opts, key) for key in ("model", "base_url", "reasoning_effort")},
        "started_utc": utc,
        "wall_seconds": wall,
        "success": check["exit_code"] == 0 if scenario["check"] else None,
        "check": check,
        **stats,
    }
    (opts.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    keys = ("stop", "success", "turns", "tool_calls", "total_tokens", "wall_seconds")
    print(opts.out, " ".join(f"{key}={summary[key]}" for key in keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
