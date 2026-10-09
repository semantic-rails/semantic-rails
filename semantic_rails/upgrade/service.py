"""Upgrade a package's legacy forms in one parse-gated transaction, proven or certified.

Invariant: the upgrade writes a package only if it parses as a whole and every rewrite is
proven equivalent on this engine or certified by its rule. A rule after the baseline that
changes the masked semantic fingerprint, or the compiled SQL of an example or test query the
baseline compiles, is refused before anything is written.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import yaml

from ..architect_transactions import ProjectFileUpdate, ProjectTransaction
from ..errors import SemanticLayerError
from ..package_snapshot import LoadedPackageSnapshot, json_fingerprint, load_package_snapshot
from ..route_census import route_census
from ..runtime import Runtime
from .model import Finding, PackageFiles, Rule, YamlPath, plan
from .registry import RULES

_LOAD_ERRORS = (SemanticLayerError, yaml.YAMLError, TypeError, ValueError, AttributeError, KeyError)


@dataclass(frozen=True)
class _State:
    """One staged package: its snapshot or load error, and each query's compile outcome."""

    snapshot: LoadedPackageSnapshot | None
    error: tuple[str, str] | None
    queries: dict[str, tuple[str, ...]]


def _query_key(file: str, path: YamlPath) -> str:
    return f"{file}:{'.'.join(map(str, path))}"


def _stage(transaction: ProjectTransaction, files: PackageFiles, changed: Mapping) -> _State:
    """Load the package with ``changed`` applied and compile its examples and tests (never run)."""
    contents = {k: v for k, v in {**files.contents, **changed}.items() if v is not None}
    staged = PackageFiles(files.source, contents=contents, _directory=files.directory)
    updates = [ProjectFileUpdate(name, data) for name, data in changed.items()]
    with transaction.virtual_project(updates) as root:

        def relative(text: str) -> str:
            return text.replace(str(root.resolve()), "<package>").replace(str(root), "<package>")

        try:
            snapshot = load_package_snapshot(transaction.parse_source(root))
        except _LOAD_ERRORS as exc:
            return _State(None, (getattr(exc, "code", type(exc).__name__), relative(str(exc))), {})
        runtime = Runtime.from_snapshot(snapshot)
        queries: dict[str, tuple[str, ...]] = {}
        try:
            for file, path, query in staged.queries():
                if Path(file).parts[0] not in {"examples", "tests"}:
                    continue
                try:
                    sql = str(runtime.compile(dict(query))["rendered_sql"])
                    queries[_query_key(file, path)] = ("sql", relative(sql))
                except Exception as exc:  # noqa: BLE001 - every failure is compared evidence
                    code = getattr(exc, "code", type(exc).__name__)
                    queries[_query_key(file, path)] = ("error", code, relative(str(exc)))
        finally:
            runtime.close()
    return _State(snapshot, None, queries)


def _children(node: Any, part: str) -> list[Any]:
    if part != "*":
        return [node[part]] if isinstance(node, dict) and part in node else []
    return (
        list(node.values())
        if isinstance(node, dict)
        else list(node)
        if isinstance(node, list)
        else []
    )


def _masked(state: _State, masks: Iterable[str]) -> Any:
    """The semantic payload without each mask's path; ``*`` matches every key or list item."""
    if state.snapshot is None:
        return state.error
    semantic = state.snapshot.semantic
    for mask in masks:
        *parents, leaf = mask.split(".")
        nodes = [semantic]
        for part in parents:
            nodes = [child for node in nodes for child in _children(node, part)]
        for node in nodes:
            if isinstance(node, dict):
                node.pop(leaf, None)
    return semantic


def _first_path(before: Any, after: Any, path: tuple[Any, ...] = ()) -> str:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if before.get(key, ...) != after.get(key, ...):
                return _first_path(before.get(key), after.get(key), (*path, key))
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            if left != right:
                label = left.get("id", index) if isinstance(left, dict) else index
                return _first_path(left, right, (*path, label))
    return ".".join(map(str, path))


def _difference(before: _State, after: _State, masks: Iterable[str]) -> dict[str, Any] | None:
    """The first change between two states; queries the baseline refuses are not compared."""
    if after.error is not None:
        return {"load_error": {"code": after.error[0], "message": after.error[1]}}
    left, right = _masked(before, masks), _masked(after, masks)
    if json_fingerprint(left) != json_fingerprint(right):
        return {"semantic_path": _first_path(left, right)}
    for key, outcome in before.queries.items():
        if outcome[0] == "sql" and after.queries.get(key) != outcome:
            return {"query": key, "before": list(outcome), "after": after.queries.get(key)}
    return None


def _in_refused_query(finding: Finding, refused: set[str]) -> bool:
    return any(
        _query_key(finding.file, finding.path[:depth]) in refused
        for depth in range(len(finding.path) + 1)
    )


def _derived_key(intent: Mapping[str, Any], revision: str) -> str:
    payload = json.dumps([intent, revision], sort_keys=True).encode("utf-8")
    return "upgrade-" + hashlib.sha256(payload).hexdigest()[:32]


def _refuse_rule(
    rule: str, difference: Mapping[str, Any], reason: str = "changes what the package answers"
) -> NoReturn:
    raise SemanticLayerError(
        "CONFIG_CONFLICT",
        f"Upgrade rule '{rule}' {reason}; nothing was written. "
        "Rewrite this form by hand, and report the rule.",
        details={
            "conflict_kind": "upgrade_not_equivalent",
            "rule": rule,
            "difference": dict(difference),
        },
    )


def _unloadable(blocker: Rule | None, error: str) -> str:
    """Why no baseline loads: a rule that rewrites the refused form without certifying it,
    or a form no rule covers."""
    if blocker is not None:
        return (
            f"Upgrade rule '{blocker.id}' rewrites a form this engine refuses at load, but the "
            f"rule is not marked refused, so its rewrite cannot be certified: {error} "
            "Rewrite this form by hand, and report the rule."
        )
    return (
        "The package does not load after the upgrade rules for forms this engine refuses, and "
        f"no rule covers this: {error} Fix it by hand, then upgrade again."
    )


def _package_directory(source: Path) -> bool:
    return (source / "package.yml").is_file()


def upgrade_project(
    project: str | Path,
    *,
    workspace_root: str | Path,
    dry_run: bool = True,
    choices: Mapping[str, str] | None = None,
    expected_revision: str | None = None,
    idempotency_key: str | None = None,
    rules: tuple[Rule, ...] = RULES,
) -> dict[str, Any]:
    """Preview or write one upgrade. ``None`` uses the current revision and a fresh write
    key (a derived key for previews); a write through the Architect passes its own."""
    source = Path(project).expanduser().resolve()
    if not source.is_file() and not _package_directory(source):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"project upgrade needs a package directory with package.yml or a package file; '{source}' is not one",
            details={"project_path": str(source)},
        )
    transaction = ProjectTransaction(
        source.parent if source.is_file() else source,
        workspace_root=workspace_root,
        package_file=source.name if source.is_file() else None,
    )
    revision = transaction.current_revision()
    choices = dict(choices or {})
    intent = {"operation": "upgrade_project", "choices": choices}
    if transaction.package_file is not None:
        intent["package_file"] = transaction.package_file
    expected = (
        revision
        if expected_revision is None or (dry_run and not expected_revision)
        else expected_revision
    )
    key = (
        (_derived_key(intent, revision) if dry_run else str(uuid4()))
        if idempotency_key is None or (dry_run and not idempotency_key)
        else idempotency_key
    )
    if not dry_run:
        # An empty, non-validating preview reuses the transaction's argument, receipt and
        # revision checks before planning. The real apply checks them again under its lock.
        checked = transaction.apply(
            [],
            expected_revision=expected,
            idempotency_key=key,
            intent=intent,
            dry_run=True,
            validate_after=False,
            routes="off",
        )
        if checked.report["status"] == "replayed":
            return checked.report
    files = PackageFiles(source)
    result = plan(files, rules, choices)
    if not result.findings:
        unchanged = _stage(transaction, files, {})
        if unchanged.error is not None:
            raise SemanticLayerError(*unchanged.error)
        return {
            "ok": True,
            "status": "up_to_date",
            "project_path": str(transaction.project_path),
            "revision": revision,
            "rules": [],
            "changes": [],
            "next_actions": [],
        }
    hits = {rule.id: [f for f in result.findings if f.rule == rule.id] for rule in rules}
    mechanical = [rule for rule in rules if any(f.edits and not f.options for f in hits[rule.id])]

    # Retired or explicitly refused forms may obtain a loadable baseline. All other
    # mechanical rules must pass the proof, regardless of their registry position.
    retired = [rule for rule in mechanical if rule.effect == "retired"]
    candidates = [rule for rule in mechanical if rule.effect == "retired" or rule.refused]
    prefix: list[Rule] | None = None
    for count in range(len(candidates) + 1):
        changed = plan(files, candidates[:count], {}).files if count else {}
        baseline = _stage(transaction, files, changed)
        if baseline.error is None:
            prefix = candidates[:count]
            break
    # Without a baseline, name the first other rule whose rewrite would make the package load:
    # it rewrites a refused form but cannot certify it. None means no rule covers the error.
    blocker: Rule | None = None
    if prefix is None:
        others = [rule for rule in mechanical if rule not in candidates]
        for count in range(1, len(others) + 1):
            staged = _stage(transaction, files, plan(files, [*candidates, *others[:count]], {}).files)
            if staged.error is None:
                blocker = others[count - 1]
                break
    after = [rule for rule in mechanical if rule not in (prefix or [])]
    final = baseline
    if prefix is not None and after:
        final = _stage(transaction, files, plan(files, mechanical, {}).files)
    masks = sorted({mask for rule in after for mask in rule.masks})
    if prefix is not None and (difference := _difference(baseline, final, masks)) is not None:
        # Re-apply one rule at a time and name the first that changes something; the last
        # rule is to blame when no shorter prefix differs.
        rule = after[-1]
        for count in range(1, len(after)):
            step = _stage(transaction, files, plan(files, [*prefix, *after[:count]], {}).files)
            step_masks = sorted({mask for applied in after[:count] for mask in applied.masks})
            if (step_difference := _difference(baseline, step, step_masks)) is not None:
                rule, difference = after[count - 1], step_difference
                break
        _refuse_rule(rule.id, difference)

    refused = {key for key, outcome in baseline.queries.items() if outcome[0] == "error"}
    stops = {
        rule.id: "; ".join(
            f"stop: {f.file}:{f.line} {f.message}"
            for f in hits[rule.id]
            if not f.edits and not f.options
        )
        for rule in rules
        if any(not f.edits and not f.options for f in hits[rule.id])
    }

    def tier(rule: Rule) -> str:
        if rule.id in stops:
            return "unverified"
        if rule not in mechanical:
            return "choice"
        if prefix is None:
            return "unverified"
        if rule in prefix:
            return "certified"
        edits = [f for f in hits[rule.id] if f.edits and not f.options]
        affected = {key for key in refused if any(_in_refused_query(f, {key}) for f in edits)}
        if affected and rule.effect != "retired":
            isolated_rules = [*retired, *(r for r in prefix if r not in retired), rule]
            isolated = _stage(transaction, files, plan(files, isolated_rules, {}).files)
            if isolated.error is not None or any(
                isolated.queries.get(key, ("error",))[0] != "sql" for key in affected
            ):
                return "unverified"
        if all(_in_refused_query(f, refused) for f in edits):
            return "certified"
        return "proven"

    rule_rows: list[dict[str, Any]] = [
        {
            "id": rule.id,
            "since": rule.since,
            "effect": rule.effect,
            "summary": rule.summary,
            "tier": tier(rule),
            **({"reason": stops[rule.id]} if rule.id in stops else {}),
            "hits": [
                {"file": f.file, "line": f.line, "path": list(f.path), "message": f.message}
                for f in hits[rule.id]
            ],
        }
        for rule in rules
        if hits[rule.id]
    ]
    tiers = {row["tier"] for row in rule_rows}
    if not dry_run:
        for row in rule_rows:
            if row["tier"] == "unverified" and "reason" not in row and baseline.error is not None:
                code, message = baseline.error
                raise SemanticLayerError(
                    "CONFIG_CONFLICT",
                    f"{_unloadable(blocker, message)} Nothing was written.",
                    details={
                        "conflict_kind": "upgrade_not_equivalent",
                        "rule": blocker.id if blocker else None,
                        "difference": {
                            "tier": "unverified",
                            "load_error": {"code": code, "message": message},
                        },
                    },
                )
            if row["tier"] == "unverified":
                _refuse_rule(
                    row["id"], {"tier": "unverified"}, row.get("reason", "cannot be verified")
                )
    compared = [key for key in baseline.queries if key not in refused]
    loaded = prefix is not None
    proof = {
        "tier": next((t for t in ("unverified", "certified", "proven") if t in tiers), "choice"),
        "baseline": "none"
        if prefix is None
        else "as_is"
        if not prefix
        else "after_certified_rules",
        "fingerprint_before": json_fingerprint(_masked(baseline, masks)) if loaded else None,
        "fingerprint_after": json_fingerprint(_masked(final, masks)) if loaded else None,
        "masks": masks,
        "examples": sum(key.startswith("examples/") for key in compared),
        "tests": sum(key.startswith("tests/") for key in compared),
    }
    next_actions = []
    if final.snapshot is None:
        next_actions.append(_unloadable(blocker, final.error[1] if final.error else ""))
    else:
        if undecided := route_census(final.snapshot.config)["undecided"]:
            pairs = ", ".join(
                f"{row['source_entity']} -> {row['target_entity']}" for row in undecided[:3]
            )
            more = f" and {len(undecided) - 3} more" if len(undecided) > 3 else ""
            next_actions.append(
                f"Decide the join route of {len(undecided)} entity pairs ({pairs}{more}; "
                "project_status lists them in route_census.undecided): record each as a "
                "graph.path_preferences row, which record_route_decision writes."
            )
        next_actions.extend(
            f"Example {key} does not compile: {outcome[1]}."
            for key, outcome in final.queries.items()
            if key.startswith("examples/") and outcome[0] == "error"
        )

    preview = dry_run or bool(result.pending)
    outcome = transaction.apply(
        [ProjectFileUpdate(name, data) for name, data in result.files.items()],
        expected_revision=expected,
        idempotency_key=key,
        intent=intent,
        dry_run=preview,
        validate_after=True,
        success_status="upgraded",
        routes="guard",
        metadata={
            "rules": rule_rows,
            "proof": proof,
            "choices": [
                {
                    "key": key,
                    "option": option.id,
                    "summary": option.summary,
                    "changes_answers": option.changes_answers,
                }
                for key, option in result.choices.items()
            ],
            "choices_pending": [
                {
                    "key": files.choice_key(f),
                    "file": f.file,
                    "line": f.line,
                    "question": f.message,
                    "options": [
                        {"id": o.id, "summary": o.summary, "changes_answers": o.changes_answers}
                        for o in f.options
                    ],
                }
                for f in result.pending
                if f.options
            ],
            "next_actions": next_actions,
            "reformatted": list(result.reformatted),
        },
    )
    report = outcome.report
    if stops:
        report.update({"ok": False, "status": "unverified"})
    elif result.pending:
        report.update({"ok": False, "status": "choices_pending"})
    return report


def legacy_form_hint(source: str | Path, action: str, rules: tuple[Rule, ...] = RULES) -> list[str]:
    """One next action naming the upgrade when rules match; none when planning fails."""
    path = Path(source)
    try:
        count = len(plan(PackageFiles(path), rules, {}).findings) if _package_directory(path) else 0
    except Exception:  # noqa: BLE001 - a hint never turns a report into an error
        return []
    if not count:
        return []
    return [f"{count} legacy forms have upgrade rules: {action} to rewrite them in one change."]
