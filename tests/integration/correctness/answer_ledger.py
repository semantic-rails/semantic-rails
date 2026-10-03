"""Declaration-backed answers and independent, portable reference SQL."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from semantic_rails import package_tools, result_values
from semantic_rails.runtime import _time_zone
from semantic_rails.runtime_parts.responses import output_columns

from .conftest import SHOP, VARIANTS, _rows, _runtime, _write_variant

ROOT = SHOP.parents[3]
LEDGER = SHOP / "tests" / "answers.yml"
KEYS = {"kind", "query", "expected_rows", "code", "expect", "why", "cites", "tags", "variant", "reference_sql", "clarify", "intent", "known_wrong"}  # fmt: skip
TAGS = {"null-vs-zero", "list-vs-conjunction", "second-fact", "time-window", "clock", "child-scope", "refusal", "planner", "empty-result"}  # fmt: skip
TABLES = ["orders", "refunds", "signups"]
DEFINITIONS = str(SHOP.relative_to(ROOT)) + "/models/"
KINDS = {
    "answer": "query_matches_snapshot",
    "clarify": "validate_fails_with_code",
    "refuse": "validate_fails_with_code",
}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def load_entries():
    return package_tools._load_named_entries(
        SHOP / "tests", plural_key="tests", singular_key="test"
    )


def resolves(citation):
    try:
        name, anchor = citation.split("#")
        path = (ROOT / name).resolve()
        if not anchor or not path.is_relative_to(ROOT) or Path(name).is_absolute():
            return False
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".md":
            slugs, counts = set(), {}
            for heading in re.findall(r"^#{1,6} (.+)$", text, re.M):
                slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                n = counts.get(slug, 0)
                counts[slug] = n + 1
                slugs.add(f"{slug}-{n}" if n else slug)
            return anchor in slugs
        if path.suffix in {".yml", ".yaml"}:
            value = yaml.safe_load(text)
            for key in anchor.split("."):
                value = value[key]
            return True
        if path.suffix == ".py":
            for node in ast.parse(text).body:
                if getattr(node, "name", None) == anchor:
                    return True
                targets = getattr(node, "targets", [getattr(node, "target", None)])
                if any(isinstance(target, ast.Name) and target.id == anchor for target in targets):
                    return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError, SyntaxError):
        pass
    return False


def fingerprint(runtime, tables):
    data = []
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = _rows(runtime, f"DESCRIBE {quoted}")
        expressions = []
        for name, kind, *_ in columns:
            column = '"' + name.replace('"', '""') + '"'
            if kind == "TIMESTAMP WITH TIME ZONE":
                column = f"epoch_us({column})"
            expressions.append(f"CAST({column} AS VARCHAR)")
        sql = f"SELECT {', '.join(expressions)} FROM {quoted} ORDER BY "
        rows = _rows(runtime, sql + ", ".join(str(i + 1) for i in range(len(columns))))
        data.append([table, [col[0] for col in columns], rows])
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def messages(entries, fixture, runtime):
    errors = []
    for case_id, spec in entries:
        cites = spec.get("cites", [])
        tags = spec.get("tags", [])
        known = spec.get("known_wrong", {})
        answer = spec.get("expect") == "answer"
        cited = isinstance(cites, list) and bool(cites) and all(resolves(cite) for cite in cites)
        declaration = cited and any(cite.startswith(("docs/", DEFINITIONS)) for cite in cites)
        valid_known = isinstance(known, dict) and set(known) <= {"engine", "planner"}
        valid_known = valid_known and ("planner" not in known or text(spec.get("intent")))
        kind = KINDS.get(spec.get("expect"))
        checks = {
            "unknown keys": bool(set(spec) - KEYS),
            "unknown tags": not isinstance(tags, list)
            or any(not text(t) or t not in TAGS for t in tags),
            "unresolved citation": not cited,
            "expect/kind mismatch": not kind or kind != spec.get("kind"),
            "missing reference_sql": answer and not text(spec.get("reference_sql")),
            "missing declaration citation": answer and not declaration,
            "invalid known_wrong": not valid_known or not all(text(r) for r in known.values()),
            "invalid variant": spec.get("variant", "utc_authored") not in VARIANTS,
            "invalid expectation": answer and not isinstance(spec.get("expected_rows"), list),
        }
        errors.extend(f"{case_id}: {rule}" for rule, failed in checks.items() if failed)
    if set(fixture) != {"tables", "data_sha256"} or fixture.get("tables") != TABLES:
        errors.append("invalid fixture header")
    elif fingerprint(runtime, fixture["tables"]) != fixture["data_sha256"]:
        errors.append("stale fixture fingerprint")
    return errors


def encode(runtime, spec, reference=None):
    compiled = runtime._compile(spec["query"], policy_context={})
    columns = output_columns(runtime._config, compiled)
    rows = spec["expected_rows"]
    if reference is not None:
        keys = list(rows[0]) if rows else [col["field"] for col in columns]
        rows = [dict(zip(keys, row, strict=True)) for row in reference(spec["reference_sql"])]
    encoded = result_values.result_rows(
        rows, output_columns=columns, zone=_time_zone(runtime._config, compiled)
    )
    return {**encoded, "output_columns": columns}


def comparable(result, *, ordered=False, positional=False):
    rows = [package_tools._typed_rows({**result, "rows": [row]})[0] for row in result["rows"]]
    if positional:
        rows = [tuple(row[col["field"]] for col in result["output_columns"]) for row in rows]
    if not ordered:
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["show", "fingerprint"])
    parser.add_argument("id", nargs="?")
    args = parser.parse_args()
    entries = dict(load_entries())
    spec = entries.get(args.id, {})
    if args.command == "show" and spec.get("expect") != "answer":
        parser.error("show requires an answer case ID")
    with TemporaryDirectory(prefix="sr-answer-ledger-") as root:
        runtime = _runtime(_write_variant(Path(root), spec.get("variant", "utc_authored")))
        try:
            if args.command == "fingerprint":
                print(fingerprint(runtime, yaml.safe_load(LEDGER.read_text())["fixture"]["tables"]))
            else:
                rows = encode(runtime, spec, lambda sql: _rows(runtime, sql))

                # Emit numeric YAML, not the JSON contract's decimal strings.
                yaml.SafeDumper.add_representer(
                    Decimal, lambda d, v: d.represent_scalar("tag:yaml.org,2002:float", str(v))
                )
                data = {"expected_rows": comparable(rows, ordered=True)}
                print(yaml.safe_dump(data, sort_keys=False), end="")
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
