"""Rank the friction across agent-harness runs: which tools cost the model the most.

    uv run python scripts/agent_harness/report.py RUN_DIR [RUN_DIR ...]

A tool's wasted tokens are the turn tokens spent on its failed or repeated calls. Tools rank by
wasted tokens, then errors; each shows its most frequent error message.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def report(runs: list[dict]) -> str:
    rows = [
        "| run | success | stop | turns | calls | errors | tokens | compactions |",
        "|---|---|---|---|---|---|---|---|",
    ]
    tools: defaultdict[str, Counter] = defaultdict(Counter)
    errors: defaultdict[str, Counter] = defaultdict(Counter)
    for run in runs:
        rows.append(
            f"| {run['name']} | {run['success']} | {run['stop']} | {run['turns']} "
            f"| {run['tool_calls']} | {run['tool_errors']} | {run['total_tokens']} "
            f"| {len(run.get('compactions') or [])} |"
        )
        for name, counts in run["friction"].items():
            errors[name].update(counts["errors_seen"])
            tools[name].update(
                {key: value for key, value in counts.items() if key != "errors_seen"}
            )
    rows += ["", "| tool | wasted tokens | errors | repeats | bad arguments | calls | top error |"]
    rows.append("|---|---|---|---|---|---|---|")
    for name, c in sorted(
        tools.items(), key=lambda item: (-item[1]["wasted_tokens"], -item[1]["errors"])
    ):
        top = errors[name].most_common(1)[0][0].replace("|", "/") if errors[name] else ""
        rows.append(
            f"| {name} | {c['wasted_tokens']} | {c['errors']} | {c['repeats']} "
            f"| {c['bad_arguments']} | {c['calls']} | {top[:120]} |"
        )
    unused = set().union(*(run["unused_tools"] for run in runs)) - set(tools)
    rows += ["", f"Never called in any run: {', '.join(sorted(unused)) or 'none'}"]
    return "\n".join(rows)


if __name__ == "__main__":
    print(report([json.loads(Path(arg, "summary.json").read_text()) for arg in sys.argv[1:]]))
