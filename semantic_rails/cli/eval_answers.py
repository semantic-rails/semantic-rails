"""Answer evals: score a package's ``tests/answers.yml`` against its own warehouse.

An answers file freezes what governed calls must return: the rows for a call that has an
answer, and the refusal code for a call that must not. It uses the engine's correctness
answer-file schema (``tests/integration/correctness/shop/tests/answers.yml``), and each
entry runs through the package test runner, :func:`package_tools._run_test`, so ``eval``
and ``test-package`` agree on every verdict. This module adds the scoring: a wrong number
counts against a change more heavily than a missed answer.

- ``failed``: wrong rows, a number for a call that must refuse, a refusal with a different
  code, a warehouse error, or an entry the file can't state. A true/false cell matches only
  a true/false cell, never a number.
- ``missed``: a refusal where rows were expected.
- ``known_wrong``: an entry its ``known_wrong.engine`` marker says still has the wrong
  rows or refusal. A warehouse error still fails it.
- ``skipped``: an entry with a correctness-harness ``variant``, which needs a rewritten
  package this runner does not build.

Exit codes: 0 when clean, 1 for any failure, 2 for misses only (1 with ``strict``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .. import package_tools
from ..ast import PERIOD_PARTIAL
from ..clock import parse_now
from ..config import package_root_for_source
from ..config_validation import PackageReference, parse_config_report
from ..errors import SemanticLayerError
from ..request_context import TrustedAttributes, context_from_policy_context
from ..result_values import result_rows
from ..route_census import defer_census_questions
from ..runtime import Runtime, _time_zone
from ..runtime_parts.joined_call import joined_output_columns
from ..runtime_parts.responses import output_columns
from ..yaml_loader import Yaml12SafeLoader

ANSWERS_FILE = Path("tests") / "answers.yml"
# ``expect`` names what the call must do; ``kind`` is the package test that checks it.
KINDS = {
    "answer": "query_matches_snapshot",
    "clarify": "validate_fails_with_code",
    "refuse": "validate_fails_with_code",
}
# Every key the correctness answer ledger accepts. ``reference_sql``, ``intent``,
# ``partial_query``, ``cites`` and ``tags`` are that harness's checks; eval reads past them.
KEYS = frozenset(
    {
        "kind",
        "query",
        "now",
        "context",
        "expected_rows",
        "expected_rows_by_backend",
        "code",
        "expect",
        "why",
        "cites",
        "tags",
        "variant",
        "reference_sql",
        "clarify",
        "intent",
        "partial_query",
        "known_wrong",
    }
)
FILE_KEYS = frozenset({"tests", "fixture", "now"})
DEFAULT_VARIANT = "utc_authored"
PASSED, FAILED, MISSED, KNOWN_WRONG, SKIPPED = (
    "passed",
    "failed",
    "missed",
    "known_wrong",
    "skipped",
)
OUTCOMES = (PASSED, FAILED, MISSED, KNOWN_WRONG, SKIPPED)
# Codes that say the run broke, not that the package refused the question.
RUN_ERRORS = frozenset(
    {"QUERY_EXECUTION_ERROR", "INTERNAL_ERROR", "SERVER_BUSY", "PACKAGE_LOAD_FAILED"}
)
SHOWN_ROWS = 5


class _DuplicateKey(yaml.constructor.ConstructorError):
    """A mapping names one key twice; a plain load would keep only the last."""

    def __init__(self, key: Any, mark: Any) -> None:
        super().__init__(None, None, f"duplicate key {key!r}", mark)
        self.key = key


class _UniqueKeyLoader(Yaml12SafeLoader):
    """The package YAML loader, refusing a mapping that repeats a key."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[tuple[str, Any]] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue  # ``<<`` keys may be overridden; that's what they're for.
            key = self.construct_object(key_node, deep=True)
            marker = (type(key).__name__, key)
            try:
                hash(marker)
            except TypeError:
                continue  # The base loader refuses an unhashable key itself.
            if marker in seen:
                raise _DuplicateKey(key, key_node.start_mark)
            seen.add(marker)
        return super().construct_mapping(node, deep=deep)


class _DecimalLoader(_UniqueKeyLoader):
    """Read frozen numbers without a binary-float intermediate, as the answer ledger does."""


_DecimalLoader.add_constructor(
    "tag:yaml.org,2002:float",
    lambda loader, node: Decimal(loader.construct_scalar(node).replace("_", "")),
)


def exit_code(summary: dict[str, int], *, strict: bool = False) -> int:
    """0 when clean, 1 for any failure, 2 for misses only; ``strict`` makes a miss fail."""
    if summary.get(FAILED, 0):
        return 1
    if summary.get(MISSED, 0):
        return 1 if strict else 2
    return 0


def answers_path_for(ref: PackageReference, answers_path: str | Path | None = None) -> Path:
    if answers_path:
        return Path(answers_path)
    return Path(package_root_for_source(ref.source_path)) / ANSWERS_FILE


def load_answers(path: Path) -> list[tuple[str, Any]]:
    """The file's entries in order. A top-level ``now`` applies to entries without one.

    Queries load as the package test loader reads them; frozen rows keep exact decimals.
    Raises ``INVALID_CONFIG`` for a file that isn't an answers file, including one that
    names a key twice in a mapping, such as two entries with one name.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _file_error(path, "no answers file; write one with a tests mapping") from exc
    except OSError as exc:
        raise _file_error(path, f"the answers file can't be read ({exc.strerror})") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
        exact = yaml.load(text, Loader=_DecimalLoader)
    except _DuplicateKey as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else 0
        message = f"the answers file names {exc.key!r} twice (line {line}); keys must be unique"
        raise _file_error(path, message) from exc
    except yaml.YAMLError as exc:
        raise _file_error(path, "the answers file is not valid YAML") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tests"), dict):
        raise _file_error(path, "an answers file is a mapping with a tests mapping of entries")
    if not raw["tests"]:
        # A file that checks nothing must not read as a clean pass.
        raise _file_error(path, "the answers file has no entries")
    unknown = sorted(str(key) for key in set(raw) - FILE_KEYS)
    if unknown:
        raise _file_error(path, f"unknown top-level keys: {', '.join(unknown)}")
    shared = {"now": raw["now"]} if "now" in raw else {}
    entries: list[tuple[str, Any]] = []
    # Both loads read the same mapping, so its entries line up in order.
    for (entry_id, spec), frozen in zip(raw["tests"].items(), exact["tests"].values(), strict=True):
        if not isinstance(spec, dict):
            entries.append((str(entry_id), spec))
            continue
        rows = {
            key: frozen[key] for key in ("expected_rows", "expected_rows_by_backend") if key in spec
        }
        entries.append((str(entry_id), {**shared, **spec, **rows}))
    return entries


def entry_problems(spec: Any) -> list[str]:
    """What keeps an entry from stating a checkable expectation; empty when it can run."""
    if not isinstance(spec, dict):
        return ["an entry is a mapping"]
    problems = []
    unknown = sorted(str(key) for key in set(spec) - KEYS)
    if unknown:
        problems.append(f"unknown keys: {', '.join(unknown)}")
    expect = spec.get("expect")
    if expect not in KINDS:
        problems.append("expect must be answer, refuse or clarify")
    elif spec.get("kind") != KINDS[expect]:
        problems.append(f"expect: {expect} needs kind: {KINDS[expect]}")
    query = spec.get("query")
    if not isinstance(query, dict) or not query:
        problems.append("query must be a query call mapping")
    by_backend = spec.get("expected_rows_by_backend", {})
    if expect == "answer":
        if not isinstance(spec.get("expected_rows"), list):
            problems.append("an answer needs expected_rows, a list of rows")
        if not isinstance(by_backend, dict) or any(
            not isinstance(rows, list) for rows in by_backend.values()
        ):
            problems.append("expected_rows_by_backend maps a warehouse to a list of rows")
    elif expect in KINDS:
        if not _text(spec.get("code")):
            problems.append(f"a {expect} entry needs the refusal code")
        if "expected_rows" in spec or "expected_rows_by_backend" in spec:
            problems.append(f"a {expect} entry has no expected rows")
    options = (
        (spec.get("clarify") or {}).get("options")
        if isinstance(spec.get("clarify"), dict)
        else None
    )
    if "clarify" in spec and (
        expect != "clarify"
        or not isinstance(options, list)
        or not options
        or any(not _text(option) for option in options)
        or len(set(options)) != len(options)
    ):
        problems.append("clarify.options lists distinct option ids, on a clarify entry")
    if "now" in spec and not _instant(spec["now"]):
        problems.append("now must be an ISO date or date-time")
    if "context" in spec and (not isinstance(spec["context"], dict) or not spec["context"]):
        problems.append("context must be a nonempty mapping")
    known = spec.get("known_wrong", {})
    if (
        not isinstance(known, dict)
        or set(known) - {"engine", "planner"}
        or not all(_text(reason) for reason in known.values())
    ):
        problems.append("known_wrong maps engine or planner to a reason")
    return problems


def call_arguments(spec: dict[str, Any], now: Any = None) -> dict[str, Any]:
    """The ``now`` and ``context`` an entry sends beside its query.

    Eval asks as an embedding host does, as the answer ledger does: a context's
    ``attributes`` arrive as trusted attributes, so a row filter that reads them applies.
    """
    call = package_tools._call_arguments(spec, now)
    context = call["context"]
    if isinstance(context, dict) and "attributes" in context:
        trusted = TrustedAttributes(context["attributes"])
        call["context"] = context_from_policy_context({**context, "attributes": trusted})
    return call


def score_entry(runtime: Runtime, entry_id: str, spec: Any, now: Any = None) -> dict[str, Any]:
    """One entry's outcome, before its ``known_wrong`` marker is applied."""
    problems = entry_problems(spec)
    if problems:
        query = spec.get("query") if isinstance(spec, dict) else None
        return _result(
            entry_id, spec, FAILED, "the entry is invalid", {"problems": problems}, query
        )
    if spec.get("variant", DEFAULT_VARIANT) != DEFAULT_VARIANT:
        reason = f"variant {spec['variant']!r} needs the correctness harness's rewritten package"
        return _result(entry_id, spec, SKIPPED, reason)
    call = call_arguments(spec, now)
    if spec["expect"] == "answer":
        return _score_answer(runtime, entry_id, spec, call)
    return _score_refusal(runtime, entry_id, spec, call)


def run_answers_report(
    ref: PackageReference,
    *,
    answers_path: str | Path | None = None,
    strict: bool = False,
    now: Any = None,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    """Score every entry of the package's answers file; the report carries its exit code."""
    path = answers_path_for(ref, answers_path)
    with defer_census_questions():
        report, config = parse_config_report(ref)
    if not report["ok"] or config is None:
        return _report(dict(report["package"]), path, [], strict, list(report["errors"]))
    package = {"id": config.package.package_id, "source_path": ref.source_path}
    try:
        entries = load_answers(path)
    except SemanticLayerError as exc:
        return _report(package, path, [], strict, [_issue(exc)])
    owned = runtime is None
    if runtime is None:
        runtime = Runtime(ref.package_id) if ref.package_id else Runtime.from_path(ref.source_path)
    try:
        results = [_known_wrong(spec, score_entry(runtime, i, spec, now)) for i, spec in entries]
    finally:
        if owned:
            runtime.close()
    return _report(package, path, results, strict, [])


def row_differences(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]], *, ordered: bool = False
) -> dict[str, Any]:
    """The cells that differ, row by row.

    Ordered rows pair by position, which each changed row names (from 1). Otherwise each row that has an exact match pairs with it,
    and each remaining expected row pairs with the remaining actual row that shares the most
    cells with it (its group, usually); one that shares none is missing. A true/false cell
    equals only a true/false cell, never the number 1 or 0.
    """
    changed: list[tuple[int | None, dict[str, Any], dict[str, Any]]] = []
    if ordered:
        pairs = zip(expected, actual, strict=False)
        changed = [(i, e, a) for i, (e, a) in enumerate(pairs, start=1) if not _same_row(e, a)]
        missing, unexpected = expected[len(actual) :], actual[len(expected) :]
    else:
        remaining = list(actual)
        unmatched = []
        for row in expected:
            match = next((other for other in remaining if _same_row(row, other)), None)
            if match is not None:
                remaining.remove(match)
            else:
                unmatched.append(row)
        missing = []
        for row in unmatched:
            best = max(remaining, key=lambda other: _shared(row, other), default=None)
            if best is not None and _shared(row, best):
                remaining.remove(best)
                changed.append((None, row, best))
            else:
                missing.append(row)
        unexpected = remaining
    return {
        "changed_rows": [
            {
                **({"position": i} if i else {}),
                "row": {
                    key: value for key, value in e.items() if key in a and _same(a[key], value)
                },
                "cells": [
                    {"field": key, "expected": e.get(key), "actual": a.get(key)}
                    for key in _keys(e, a)
                    if key not in e or key not in a or not _same(e[key], a[key])
                ],
            }
            for i, e, a in changed
        ],
        "missing_rows": missing,
        "unexpected_rows": unexpected,
    }


def _score_answer(
    runtime: Runtime, entry_id: str, spec: dict[str, Any], call: dict[str, Any]
) -> dict[str, Any]:
    spec = _for_backend(spec, runtime.warehouse)
    query = spec["query"]
    try:
        report = package_tools._run_test(runtime, entry_id, {**spec, **call})
    except SemanticLayerError as exc:
        if exc.code in RUN_ERRORS:
            return _run_failure(entry_id, spec, exc.code, _issue(exc))
        reason = f"refused with {exc.code} where rows were expected"
        return _result(entry_id, spec, MISSED, reason, _issue(exc))
    if not report["ok"] and report["error"]["code"] != "SNAPSHOT_MISMATCH":
        error = report["error"]
        return _result(entry_id, spec, FAILED, error["message"], error["details"])
    # The runner compares rows as a multiset with Python equality, where true equals 1, so
    # rows it passes are compared again keeping true/false apart from numbers. An order_by
    # also fixes their order.
    ordered = bool(query.get("order_by"))
    try:
        expected = _expected_rows(runtime, query, call, spec["expected_rows"])
        if report["ok"] or ordered:
            actual = _answer_rows(runtime, query, call, spec["expected_rows"])
        else:
            actual = report["error"]["details"]["actual_rows"]
    except SemanticLayerError as exc:
        return _run_failure(entry_id, spec, exc.code, _issue(exc))
    if not ordered:
        expected = package_tools._normalize_rows(expected)
    differences = row_differences(expected, actual, ordered=ordered)
    if report["ok"] and not any(differences.values()):
        return _result(entry_id, spec, PASSED)
    in_order = ordered and not any(row_differences(expected, actual).values())
    reason = "the rows are in a different order" if in_order else "wrong rows"
    return _result(entry_id, spec, FAILED, reason, differences)


def _score_refusal(
    runtime: Runtime, entry_id: str, spec: dict[str, Any], call: dict[str, Any]
) -> dict[str, Any]:
    code = spec["code"]
    try:
        report = package_tools._run_test(runtime, entry_id, {**spec, **call})
    except SemanticLayerError as exc:
        return _refused(entry_id, spec, exc.code, _issue(exc))
    if report["ok"]:
        return _clarification(runtime, entry_id, spec, call)
    actual = report["error"]["details"]["actual"]
    if actual:
        return _refused(entry_id, spec, actual, {"expected": code, "actual": actual})
    # The call validates, so ask it: a refusal at execution still refuses.
    try:
        result = runtime.query(spec["query"], **call)
    except SemanticLayerError as exc:
        return _refused(entry_id, spec, exc.code, _issue(exc))
    rows = package_tools._typed_rows(result)
    reason = f"returned {len(rows)} row(s) for a call that must refuse with {code}"
    details = {"row_count": len(rows), "rows": rows[:SHOWN_ROWS]}
    return _result(entry_id, spec, FAILED, reason, details)


def _refused(entry_id: str, spec: dict[str, Any], actual: str, details: dict[str, Any]):
    if actual == spec["code"]:
        return _result(entry_id, spec, PASSED)
    if actual in RUN_ERRORS:
        return _run_failure(entry_id, spec, actual, details)
    reason = f"refused with {actual} where {spec['code']} was expected"
    return _result(entry_id, spec, FAILED, reason, details)


def _clarification(
    runtime: Runtime, entry_id: str, spec: dict[str, Any], call: dict[str, Any]
) -> dict[str, Any]:
    """A clarify entry that names its options must offer exactly those, in order."""
    expected = (spec.get("clarify") or {}).get("options")
    if expected is None:
        return _result(entry_id, spec, PASSED)
    error = runtime.validate(spec["query"], **call)["errors"][0]
    clarification = dict(error.get("details", {}).get("clarification") or {})
    actual = [option.get("id") for option in clarification.get("options") or []]
    if actual == expected:
        return _result(entry_id, spec, PASSED)
    details = {"expected_options": expected, "actual_options": actual}
    return _result(entry_id, spec, FAILED, "the clarification offers different options", details)


def _known_wrong(spec: Any, result: dict[str, Any]) -> dict[str, Any]:
    """A ``known_wrong.engine`` entry may have the wrong rows or refusal; once it passes, the
    marker must go. It never excuses a call that failed to run."""
    reason = spec.get("known_wrong", {}).get("engine") if isinstance(spec, dict) else None
    if (
        not reason
        or result["outcome"] == SKIPPED
        or "run_error" in result
        or "problems" in result.get("details", {})
    ):
        return result
    if result["outcome"] == PASSED:
        return {
            **result,
            "outcome": FAILED,
            "reason": "passes, so its known_wrong.engine marker must be removed",
        }
    return {**result, "outcome": KNOWN_WRONG, "known_wrong": reason}


def _expected_rows(
    runtime: Runtime, query: dict[str, Any], call: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The frozen rows encoded as the call's own columns encode them, in their order."""
    payload = runtime.engine_payload(query, **call)
    if "parts" in payload:
        columns, zone = joined_output_columns(runtime, payload)
    else:
        compiled = runtime._compile(payload, policy_context={})
        columns = output_columns(runtime._config, compiled)
        zone = _time_zone(runtime._config, compiled)
    return _each_typed(result_rows(rows, output_columns=columns, zone=zone))


def _answer_rows(
    runtime: Runtime, query: dict[str, Any], call: dict[str, Any], expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The call's rows in its order, without a series flag the frozen rows leave out."""
    result = runtime.query(query, **call)
    if not any(PERIOD_PARTIAL in row for row in expected):
        rows = [{k: v for k, v in row.items() if k != PERIOD_PARTIAL} for row in result["rows"]]
        result = {**result, "rows": rows}
    return _each_typed(result)


def _each_typed(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [package_tools._typed_rows({**result, "rows": [row]})[0] for row in result["rows"]]


def _for_backend(spec: dict[str, Any], backend: str) -> dict[str, Any]:
    """A warehouse's own frozen representation, when the file freezes one."""
    rows = spec.get("expected_rows_by_backend") or {}
    return {**spec, "expected_rows": rows[backend]} if backend in rows else spec


def _report(
    package: dict[str, Any],
    path: Path,
    results: list[dict[str, Any]],
    strict: bool,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = {"answers_total": len(results)}
    summary.update({outcome: sum(r["outcome"] == outcome for r in results) for outcome in OUTCOMES})
    code = 1 if errors else exit_code(summary, strict=strict)
    return {
        "ok": code == 0,
        "exit_code": code,
        "strict": strict,
        "package": package,
        "answers_path": str(path),
        "summary": summary,
        "answers": results,
        "errors": errors,
    }


def _result(
    entry_id: str,
    spec: Any,
    outcome: str,
    reason: str = "",
    details: dict[str, Any] | None = None,
    query: Any = None,
) -> dict[str, Any]:
    spec = spec if isinstance(spec, dict) else {}
    result: dict[str, Any] = {
        "id": entry_id,
        "outcome": outcome,
        "expect": spec.get("expect"),
        "query": spec.get("query") if query is None else query,
    }
    if spec.get("code"):
        result["code"] = spec["code"]
    if reason:
        result["reason"] = reason
    if details:
        result["details"] = details
    return result


def _run_failure(
    entry_id: str, spec: dict[str, Any], code: str, details: dict[str, Any]
) -> dict[str, Any]:
    """A call the warehouse or engine failed to run: a failure no marker excuses."""
    return {
        **_result(entry_id, spec, FAILED, f"the call failed: {code}", details),
        "run_error": code,
    }


def _issue(exc: SemanticLayerError) -> dict[str, Any]:
    return {"code": exc.code, "message": str(exc), "details": dict(exc.details)}


def _file_error(path: Path, message: str) -> SemanticLayerError:
    return SemanticLayerError("INVALID_CONFIG", f"{path}: {message}", details={"path": str(path)})


def _shared(row: dict[str, Any], other: dict[str, Any]) -> int:
    return sum(1 for key, value in row.items() if key in other and _same(other[key], value))


def _same(value: Any, other: Any) -> bool:
    """Equal, with true and false kept apart from 1 and 0."""
    return isinstance(value, bool) == isinstance(other, bool) and bool(value == other)


def _same_row(row: dict[str, Any], other: dict[str, Any]) -> bool:
    return row.keys() == other.keys() and all(
        _same(value, other[key]) for key, value in row.items()
    )


def _keys(*rows: dict[str, Any]) -> Iterable[str]:
    return list(dict.fromkeys(key for row in rows for key in row))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _instant(value: Any) -> bool:
    try:
        return _text(value) and bool(parse_now(value))
    except SemanticLayerError:
        return False


def compact(value: Any) -> str:
    """One line of JSON for a report, numbers as written."""
    return json.dumps(value, sort_keys=False, default=str, separators=(", ", ": "))
