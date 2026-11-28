"""``semantic-rails eval``: score a package's answers file, for CI on a model change."""

from __future__ import annotations

import argparse
from typing import Any

from ...config_validation import resolve_package_reference
from ..common import _add_now_arg, _add_optional_reference_args, _print
from ..eval_answers import (
    FAILED,
    KNOWN_WRONG,
    MISSED,
    PASSED,
    SKIPPED,
    compact,
    run_answers_report,
)

LABELS = {PASSED: "PASS", FAILED: "FAIL", MISSED: "MISS", KNOWN_WRONG: "KNOWN", SKIPPED: "SKIP"}


def add_eval_cli(sub: Any, package_choices: list[str]) -> None:
    parser = sub.add_parser(
        "eval",
        description=(
            "Run the package's answers file (tests/answers.yml) against its warehouse and score "
            "it: a wrong row, or a number for a call that must refuse, fails; a refusal where "
            "rows were expected is a miss. Exits 0 when clean, 1 on any failure, 2 on misses "
            "only. See docs/EVAL.md."
        ),
    )
    _add_optional_reference_args(parser, package_choices, required=True)
    parser.add_argument(
        "--answers",
        default="",
        help="Path to the answers file. Default: tests/answers.yml in the package.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit 1 on a miss too.")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    _add_now_arg(parser)
    parser.set_defaults(func=cmd_eval)


def cmd_eval(args: argparse.Namespace) -> None:
    ref = resolve_package_reference(package_id=args.package, path=args.path)
    report = run_answers_report(
        ref, answers_path=args.answers or None, strict=args.strict, now=args.now or None
    )
    if args.json:
        _print(report)
    else:
        print("\n".join(report_lines(report)))
    if report["exit_code"]:
        raise SystemExit(report["exit_code"])


def report_lines(report: dict[str, Any]) -> list[str]:
    """The report for a person: problems first, then one summary line."""
    lines = [f"eval {report['package'].get('id', '')}: {report['answers_path']}"]
    for error in report["errors"]:
        lines.append(f"ERROR [{error.get('code', '')}] {error.get('message', '')}")
    for result in report["answers"]:
        if result["outcome"] == PASSED:
            continue
        lines.append(f"{LABELS[result['outcome']]}  {result['id']}: {result.get('reason', '')}")
        if result["outcome"] == SKIPPED:
            continue
        lines.append(f"      call: {compact(result.get('query'))}")
        lines.extend(f"      {line}" for line in _detail_lines(result))
    summary = report["summary"]
    counts = ", ".join(
        f"{summary[outcome]} {outcome.replace('_', ' ')}"
        for outcome in (PASSED, FAILED, MISSED, KNOWN_WRONG, SKIPPED)
        if summary[outcome] or outcome == PASSED
    )
    strict = " (strict)" if report["strict"] else ""
    lines.append(
        f"{summary['answers_total']} answers: {counts}. Exit {report['exit_code']}{strict}."
    )
    return lines


def _detail_lines(result: dict[str, Any]) -> list[str]:
    details = dict(result.get("details") or {})
    lines = [f"problem: {problem}" for problem in details.get("problems", [])]
    if result.get("known_wrong"):
        lines.append(f"known wrong: {result['known_wrong']}")
    for change in details.get("changed_rows", []):
        row = compact(change["row"]) if change["row"] else "(no shared cells)"
        if "position" in change:
            row = f"{change['position']} {row}" if change["row"] else str(change["position"])
        cells = "; ".join(
            f"{cell['field']} expected {compact(cell['expected'])}, got {compact(cell['actual'])}"
            for cell in change["cells"]
        )
        lines.append(f"row {row}: {cells}")
    lines.extend(f"missing row: {compact(row)}" for row in details.get("missing_rows", []))
    lines.extend(f"unexpected row: {compact(row)}" for row in details.get("unexpected_rows", []))
    if "row_count" in details:
        lines.extend(f"returned row: {compact(row)}" for row in details.get("rows", []))
    if result["outcome"] in {MISSED, FAILED} and "message" in details:
        lines.append(f"{details.get('code', '')}: {details['message']}")
    return lines
