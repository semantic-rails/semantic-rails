"""A terminal the model drives: start one of the scenario's programs in a pty, type, press keys.

The model picks a program by name and may add arguments; the program starts directly (no
shell), with TERM=dumb and NO_COLOR, so it prints plain text. Each tool returns what the
program printed since the last call (once output has paused for 1.5 seconds), escape sequences
removed, or an error.
"""

from __future__ import annotations

import os
import pty
import re
import select
import shlex
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
KEYS = {
    "enter": "\r",
    "tab": "\t",
    "space": " ",
    "backspace": "\x7f",
    "escape": "\x1b",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "ctrl_c": "\x03",
    "ctrl_d": "\x04",
}
TAIL = 6000  # characters of new output a tool returns
QUIET = 1.5  # seconds without output that end a read


def spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    parameters = {"type": "object", "properties": properties, "required": required}
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def clean(raw: str) -> str:
    """Plain text as a terminal would leave it: escapes removed, carriage returns applied."""
    lines = ESCAPES.sub("", raw).replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in lines)


class Terminal:
    """One program at a time; `jail` prefixes its command (for example a network-less namespace)."""

    def __init__(self, programs: dict[str, str], cwd: Path, jail: list[str]) -> None:
        self.programs, self.cwd, self.jail = programs, cwd, jail
        self.proc: subprocess.Popen | None = None
        self.fd = -1

    def tools(self) -> list[dict]:
        names = ", ".join(f"{name} ({command})" for name, command in self.programs.items())
        wait = {"type": "integer", "description": "milliseconds to wait for output (default 10000)"}
        return [
            spec(
                "term_start",
                f"Start a program in a terminal, replacing any running one. Programs: {names}.",
                {
                    "program": {"type": "string", "enum": list(self.programs)},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "extra arguments",
                    },
                },
                ["program"],
            ),
            spec(
                "term_type",
                "Type text into the running program, then press Enter unless enter is false.",
                {"text": {"type": "string"}, "enter": {"type": "boolean"}, "wait_ms": wait},
                ["text"],
            ),
            spec(
                "term_key",
                "Press one key in the running program.",
                {"key": {"type": "string", "enum": list(KEYS)}, "wait_ms": wait},
                ["key"],
            ),
            spec(
                "term_read", "Wait for more output from the running program.", {"wait_ms": wait}, []
            ),
        ]

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        """Run one terminal tool; return (text for the model, error)."""
        wait_ms = args.get("wait_ms")
        wait = min(max(int(10_000 if wait_ms is None else wait_ms), 0), 60_000) / 1000
        if name == "term_start":
            if args.get("program") not in self.programs:
                return (
                    f"error: unknown program; choose one of {', '.join(self.programs)}",
                    "unknown program",
                )
            self.close()
            extra = args.get("args") or []  # a string of arguments is split like a command line
            extra = shlex.split(extra) if isinstance(extra, str) else [str(arg) for arg in extra]
            argv = [*self.jail, *shlex.split(self.programs[args["program"]]), *extra]
            env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1", "COLUMNS": "100", "LINES": "30"}
            self.fd, child = pty.openpty()
            try:
                self.proc = subprocess.Popen(
                    argv,
                    stdin=child,
                    stdout=child,
                    stderr=child,
                    cwd=self.cwd,
                    env=env,
                    start_new_session=True,
                )
            finally:
                os.close(child)
                if self.proc is None:  # it didn't start; the caller reports the error
                    self.close()
        elif self.proc is None:
            return "error: no program is running; call term_start first", "no program running"
        elif name == "term_type":
            self.write(str(args.get("text", "")) + ("\r" if args.get("enter", True) else ""))
        elif name == "term_key":
            if args.get("key") not in KEYS:
                return f"error: unknown key; choose one of {', '.join(KEYS)}", "unknown key"
            self.write(KEYS[args["key"]])
        return self.read(wait), ""

    def write(self, text: str) -> None:
        if self.proc is not None and self.proc.poll() is None:  # else read() reports the exit
            os.write(self.fd, text.encode())

    def read(self, wait: float) -> str:
        """Output until it has paused for QUIET seconds, or `wait` seconds pass without any."""
        chunks, deadline = [], time.monotonic() + wait
        while (left := deadline - time.monotonic()) > 0 or not chunks:  # wait 0: one poll
            ready, _, _ = select.select(
                [self.fd], [], [], max(0, min(left, QUIET) if chunks else left)
            )
            if not ready:
                break
            try:
                data = os.read(self.fd, 65536)
            except OSError:  # Linux: the pty closed
                data = b""
            if not data:  # the program exited; let it be reaped so its exit code shows
                with suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=2)  # type: ignore[union-attr]
                break
            chunks.append(data)
        text = clean(b"".join(chunks).decode(errors="replace"))
        if len(text) > TAIL:
            text = f"[{len(text) - TAIL} earlier characters]\n" + text[-TAIL:]
        if self.proc is not None and self.proc.poll() is not None:
            text += f"\n[the program exited with code {self.proc.returncode}]"
        return text or "[no new output]"

    def close(self) -> None:
        if self.proc is not None:
            with suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        if self.fd >= 0:
            os.close(self.fd)
        self.proc, self.fd = None, -1
