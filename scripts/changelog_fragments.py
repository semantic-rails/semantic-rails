#!/usr/bin/env python3
"""Check, preview, and fold the changelog fragments in changelog.d/ (see its README.md).

Each change adds changelog.d/<slug>.<category>.md instead of editing CHANGELOG.md, so parallel
pull requests do not conflict; `release` folds the fragments into a new version section.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
# Keep a Changelog section order.
CATEGORIES = ("added", "changed", "deprecated", "removed", "fixed", "security")
FRAGMENT_NAME = re.compile(r"(?P<slug>[^.]+)\.(?P<category>[^.]+)\.md")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HEADING_LINE = re.compile(r"\s*#{1,6}(?:\s|$)")
BODY_LINE = re.compile(r"(?:- |  )\s*\S")
VERSION = re.compile(r"\d+\.\d+\.\d+")
UNRELEASED = re.compile(r"^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


class Fragment(NamedTuple):
    path: Path
    category: str
    text: str


def body_problems(text: str) -> list[str]:
    """Check that a body is `- ` bullets with two-space continuation lines."""
    if not text.strip():
        return ["is empty"]
    problems = [] if text.endswith("\n") else ["must end with a newline"]
    for number, line in enumerate(text.splitlines(), start=1):
        if HEADING_LINE.match(line):
            problems.append(f"line {number}: headings are not allowed")
        elif not BODY_LINE.match(line) or (number == 1 and not line.startswith("- ")):
            problems.append(f"line {number}: expected a '- ' bullet or a two-space continuation")
    return problems


def read_fragments(root: Path) -> tuple[list[Fragment], list[str]]:
    """Return the fragments sorted by file name, and one message per problem."""
    fragments: list[Fragment] = []
    problems: list[str] = []
    for path in sorted((root / "changelog.d").glob("*")):
        if path.name == "README.md":
            continue
        where, match = f"changelog.d/{path.name}", FRAGMENT_NAME.fullmatch(path.name)
        if match is None or not path.is_file():
            problems.append(f"{where}: name must be <slug>.<category>.md")
            continue
        if not SLUG.fullmatch(match["slug"]):
            problems.append(f"{where}: slug must be lowercase kebab-case")
        if match["category"] not in CATEGORIES:
            problems.append(f"{where}: category must be one of {', '.join(CATEGORIES)}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{where}: is not valid UTF-8")
            continue
        problems.extend(f"{where}: {problem}" for problem in body_problems(text))
        fragments.append(Fragment(path, match["category"], text))
    return fragments, problems


def render(fragments: list[Fragment]) -> str:
    """Group the bullets under ### headings in Keep a Changelog order."""
    grouped = {c: "".join(f.text for f in fragments if f.category == c) for c in CATEGORIES}
    return "\n".join(f"### {c.capitalize()}\n\n{text}" for c, text in grouped.items() if text)


def is_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def release(root: Path, version: str, day: str, title: str | None) -> list[str]:
    """Fold every fragment into CHANGELOG.md, or return the problems and write nothing."""
    fragments, problems = read_fragments(root)
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if not fragments:
        problems.append("changelog.d/ has no fragments to fold")
    if not VERSION.fullmatch(version):
        problems.append(f"--version {version!r} is not X.Y.Z")
    if not is_iso_date(day):
        problems.append(f"--date {day!r} is not a YYYY-MM-DD date")
    if re.search(rf"^## {re.escape(version)}(?:\s|$)", text, re.MULTILINE):
        problems.append(f"CHANGELOG.md already has a ## {version} section")
    unreleased = UNRELEASED.search(text)
    if unreleased is None:
        return [*problems, "CHANGELOG.md has no ## Unreleased section"]
    if re.search(r"^(?:- |#)", unreleased[1], re.MULTILINE):
        problems.append("CHANGELOG.md has entries under ## Unreleased; move them to changelog.d/")
    if problems:
        return problems

    heading = f"## {version} — {day}" + (f" — {title}" if title else "")
    head, tail = text[: unreleased.end()].rstrip("\n"), text[unreleased.end() :]
    folded = f"{head}\n\n{heading}\n\n{render(fragments)}" + (f"\n{tail}" if tail else "")
    changelog.write_text(folded, encoding="utf-8")
    for fragment in fragments:
        fragment.path.unlink()
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every fragment")
    commands.add_parser("preview", help="print the pending Unreleased section")
    fold = commands.add_parser("release", help="fold the fragments into CHANGELOG.md")
    fold.add_argument("--version", required=True, help="X.Y.Z")
    fold.add_argument("--date", required=True, help="YYYY-MM-DD")
    fold.add_argument("--title", help="optional release title")
    args = parser.parse_args(argv)

    fragments, problems = read_fragments(args.root)
    if args.command == "release":
        problems = release(args.root, args.version, args.date, args.title)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"changelog.d/: {len(fragments)} fragment(s) OK")
    elif args.command == "preview":
        print("## Unreleased\n\n" + (render(fragments) or "No unreleased changes.\n"), end="")
    else:
        print(f"Folded {len(fragments)} fragment(s) into CHANGELOG.md under ## {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
