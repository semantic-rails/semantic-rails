"""Grade the frozen eval questions through the agent harness, once per query-MCP build.

    uv run python scripts/agent_harness/eval_ab.py run --out RUNS --model MODEL \\
        --arms base=/path/to/base/.venv/bin/semantic-rails,head=semantic-rails \\
        [--base-url URL] [--reasoning-effort low] [--cases J01,J02] [--repeats 3]
    uv run python scripts/agent_harness/eval_ab.py summary RUNS

`run` writes one scenario per question of tests/semantic_rails/mcp_context/eval_jaffle.jsonl
and runs it with run.py once per arm and repeat, alternating which arm goes first. An arm is
`name=command`: the `semantic-rails` executable whose `mcp stdio` serves the query MCP. The
first of the two arms is the baseline.
Like Claude Code, the model gets the server's instructions and results cut at 100,000
characters. A folder that already exists is
skipped, so an interrupted run resumes. Each run's check is `eval_ab.py grade`: it re-runs the
query of the model's last successful `execute` (mode `run`) and compares its rows with the
case's frozen answer, using scripts/mcp_context.py's equivalence. The model ends its reply with
`ANSWER_STATUS: answered | cannot_answer | needs_clarification`. Outcomes:

- correct: the rows match, or an unanswerable question was declined;
- silent_wrong: reported as answered, but wrong (or an answer to an unanswerable question);
- declined: an answerable question the model declined or asked about;
- incomplete: the run stopped before a final reply.

`summary` counts outcomes and median tokens per arm, and the paired accuracy change with
a 95% bootstrap interval. Compare arms within one set of runs, not across models.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import mcp_context  # noqa: E402
from scripts.agent_harness import run as harness  # noqa: E402
from semantic_rails.ast import QUERY_INPUT_KEYS  # noqa: E402

STATUS_LINE = (
    "\n\nEnd your reply with exactly one line: 'ANSWER_STATUS: answered' if you ran a query "
    "that answers the question, 'ANSWER_STATUS: cannot_answer' if the data can't answer it, "
    "or 'ANSWER_STATUS: needs_clarification' if the question is ambiguous."
)
STATUSES = ("answered", "cannot_answer", "needs_clarification")
OUTCOMES = ("correct", "silent_wrong", "declined", "incomplete")


def scenario(case: dict[str, Any], arm: str, server: str = "semantic-rails") -> dict[str, Any]:
    return {
        "name": f"{case['id']}-{arm}",
        "task": case["question"] + STATUS_LINE,
        "servers": {"query": f"{server} mcp stdio --path jaffle_shop"},
        "setup": "cp -R {repo}/configs/semantic_rails/jaffle_shop jaffle_shop && mkdir "
        "jaffle_shop/data && cp -R {repo}/data/jaffle_csv {repo}/data/seed_jaffle.sql "
        "jaffle_shop/data/",
        # The check runs in RUN/workdir, beside RUN/transcript.jsonl.
        "check": f"{{python}} {{repo}}/scripts/agent_harness/eval_ab.py grade {case['id']}",
        "max_turns": 12,
    }


def answer_query(transcript: Path) -> dict[str, Any] | None:
    """The Query IR of the last successful ``execute`` that ran (mode ``run``)."""

    query = None
    for line in transcript.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") != "call" or event["tool"] != "execute" or event["error"]:
            continue
        args = event["arguments"]
        if str(args.get("mode") or "run").strip().lower() == "run":
            wrapped = args.get("query")
            query = (
                wrapped
                if isinstance(wrapped, dict)
                else {key: value for key, value in args.items() if key in QUERY_INPUT_KEYS}
            )
    return query


def grade(case: dict[str, Any], final: str, query: dict[str, Any] | None, package: Path) -> str:
    lines = [line for line in final.splitlines() if "ANSWER_STATUS:" in line]
    status = lines[-1].split("ANSWER_STATUS:", 1)[1].strip(" `*'\".").lower() if lines else ""
    if status not in STATUSES:
        status = "answered" if query else "cannot_answer"
    if case["expect"] == "refuse":
        return "silent_wrong" if status == "answered" else "correct"
    if status != "answered":
        return "declined"
    if not query:
        return "silent_wrong"
    aggregations = mcp_context.measure_aggregations(package)
    with mcp_context.QueryMCPClient(package) as client:
        try:
            answer = mcp_context.query_answer(client, query, case, aggregations)
        except ValueError:
            return "silent_wrong"
    ordered = bool(case.get("ordered"))
    right = mcp_context.answers_match(case["gold_result"], answer, ordered=ordered)
    return "correct" if right else "silent_wrong"


def summary(runs: Path) -> str:
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    for path in sorted(runs.glob("*/summary.json")):
        case_id, arm, repeat = path.parent.name.split("-")
        data = json.loads(path.read_text(encoding="utf-8"))
        output = str(data["check"].get("output") or "").split()
        outcome = output[0] if data["finished"] and output else "incomplete"
        rows[(case_id, arm, int(repeat))] = {**data, "outcome": outcome}
    order = runs / "arms.json"
    named = json.loads(order.read_text(encoding="utf-8")) if order.exists() else []
    seen = {arm for _case, arm, _repeat in rows}
    arms = [arm for arm in named if arm in seen] + sorted(seen - set(named))
    lines = [
        "| arm | runs | " + " | ".join(OUTCOMES) + " | median tokens | median tool calls |",
        "|---|---|" + "---|" * (len(OUTCOMES) + 2),
    ]
    for arm in arms:
        mine = [row for key, row in rows.items() if key[1] == arm]
        tokens = statistics.median(row["total_tokens"] or 0 for row in mine)
        calls = statistics.median(row["tool_calls"] for row in mine)
        counts = " | ".join(str(sum(row["outcome"] == o for row in mine)) for o in OUTCOMES)
        lines.append(f"| {arm} | {len(mine)} | {counts} | {tokens:,.0f} | {calls:g} |")
    base, new = (arms + ["", ""])[:2]
    pairs = [
        (row, rows[(case, new, repeat)])
        for (case, arm, repeat), row in rows.items()
        if arm == base and (case, new, repeat) in rows
    ]
    if pairs:
        deltas = [(b["outcome"] == "correct") - (a["outcome"] == "correct") for a, b in pairs]
        sampler = random.Random(0)
        means = sorted(
            statistics.fmean(sampler.choices(deltas, k=len(deltas))) for _ in range(10_000)
        )
        change = statistics.median(b["total_tokens"] or 0 for _a, b in pairs) / max(
            1, statistics.median(a["total_tokens"] or 0 for a, _b in pairs)
        )
        lines.append(
            f"\n{new} vs {base}, {len(pairs)} paired runs: accuracy "
            f"{100 * statistics.fmean(deltas):+.1f} points (95% bootstrap "
            f"{100 * means[249]:+.1f} to {100 * means[9_749]:+.1f}); median tokens "
            f"{100 * (change - 1):+.1f}%."
        )
    missing = sum(row["total_tokens"] is None for row in rows.values())
    if missing:
        lines.append(f"\n{missing} runs report no token usage; their tokens count as 0 above.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--out", type=Path, required=True)
    runner.add_argument("--model", required=True)
    runner.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    runner.add_argument("--reasoning-effort", default="")
    runner.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    runner.add_argument(
        "--arms",
        required=True,
        help="baseline,candidate as name=command (names: letters, digits, _)",
    )
    runner.add_argument("--repeats", type=int, default=1)
    commands.add_parser("grade").add_argument("case")
    commands.add_parser("summary").add_argument("runs", type=Path)
    args = parser.parse_args(argv)
    cases = {case["id"]: case for case in mcp_context.load_eval_cases()}
    if args.command == "grade":
        final = Path(os.environ["AGENT_FINAL_ANSWER"]).read_text(encoding="utf-8")
        query = answer_query(Path("../transcript.jsonl"))
        outcome = grade(cases[args.case], final, query, Path("jaffle_shop"))
        print(outcome)
        return 0 if outcome == "correct" else 1
    if args.command == "summary":
        print(summary(args.runs))
        return 0
    wanted = [case for case in cases if not args.cases or case in args.cases.split(",")]
    arms = dict(arm.partition("=")[::2] for arm in args.arms.split(","))
    if len(arms) != 2 or not all(re.fullmatch(r"\w+", arm) and arms[arm] for arm in arms):
        parser.error(
            "--arms takes two name=command arms, baseline first; names are letters, digits, _"
        )
    order = args.out / "arms.json"
    if order.exists() and json.loads(order.read_text(encoding="utf-8")) != list(arms):
        parser.error(f"{args.out} was run with other arms ({order.read_text(encoding='utf-8')})")
    (args.out / "scenarios").mkdir(parents=True, exist_ok=True)
    order.write_text(json.dumps(list(arms)), encoding="utf-8")
    for index, case_id in enumerate(wanted):
        for repeat in range(args.repeats):
            for arm in list(arms) if index % 2 == 0 else list(arms)[::-1]:
                out = args.out / f"{case_id}-{arm}-{repeat}"
                if out.exists():
                    continue
                path = args.out / "scenarios" / f"{case_id}-{arm}.yml"
                path.write_text(
                    json.dumps(scenario(cases[case_id], arm, arms[arm])), encoding="utf-8"
                )
                command = [str(path), "--out", str(out), "--model", args.model]
                command += ["--base-url", args.base_url, "--host-result-chars", "100000"]
                command += ["--instructions"]
                if args.reasoning_effort:
                    command += ["--reasoning-effort", args.reasoning_effort]
                harness.main(command)
    print(summary(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
