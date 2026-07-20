"""Programmatically generate a benchmark corpus across named plan patterns.

Walks the ``jaffle_shop`` catalog and emits intent templates per pattern
in :mod:`semantic_rails.planner.patterns`. For each intent, runs
``plan_payload``, records the status and which pattern fired, and writes
the results to ``benchmarks/scorecards/generated_plan_corpus.json``.

The headline signal is **expected-pattern-fired rate** — for each
generated intent we know which pattern *should* match (we built the
intent from that pattern's vocabulary), so we can measure how often
the orchestrator gets it right. Misses reveal pattern gaps (intents
that pass the orchestrator's scope gate but no pattern claims them,
or that a different pattern preempts).

Run:

    .venv/bin/python scripts/generate_benchmark_corpus.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Ensure we import the in-tree package, not whatever is on PYTHONPATH.
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Runtime construction (mirrors scripts/benchmark_plan.py)
# ---------------------------------------------------------------------------


def _build_runtime(package_id: str = "jaffle_shop"):
    """Build a Runtime against a fresh copy of the package config."""

    from semantic_rails import config as cfg
    from semantic_rails.config import resolve_repo_path
    from semantic_rails.runtime import Runtime

    tmp = Path(tempfile.mkdtemp())
    run_dir = tmp / package_id / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    src = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
    target = run_dir / package_id
    shutil.copytree(src, target)
    package_path = target / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text()) or {})
    pkg = dict(raw.get("package", {}) or {})
    default_db = target / f"{package_id}.duckdb"
    pkg["default_db"] = f"{package_id}.duckdb"
    raw["package"] = pkg
    package_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    seeded = Path(resolve_repo_path("data/jaffle_shop.duckdb"))
    if seeded.exists():
        shutil.copy2(seeded, default_db)
    cfg.list_package_paths = lambda: {package_id: str(target)}
    return Runtime(package_id)


def _payload_size(payload: dict[str, Any]) -> int:
    """Wire size of the response, in bytes."""

    return len(json.dumps(payload, default=str, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Intent generation
# ---------------------------------------------------------------------------


# Measure ids whose label/vocabulary doesn't match the pattern matchers'
# simple "revenue / order / inventory / menu" target-term vocabulary, or
# which represent derived/window measures the inline pattern won't fire
# on (mtd/qtd/ytd/rolling/cumulative/prior/mom/delivered).
_MEASURE_NO_GO_PREFIXES: tuple[str, ...] = (
    "measure.jaffle.rolling_",
    "measure.jaffle.revenue_mtd_",
    "measure.jaffle.revenue_qtd_",
    "measure.jaffle.revenue_ytd_",
    "measure.jaffle.prior_",
    "measure.jaffle.month_over_month_",
    "measure.jaffle.delivered_",
    "measure.jaffle.cumulative_",
    "measure.jaffle.median_",
)


@dataclass(frozen=True)
class GeneratedCase:
    """One generated benchmark case.

    ``expected_pattern`` is the pattern we *built the intent for*. If
    the orchestrator returns a different pattern (or none), that's a
    miss — which is the headline signal of this benchmark.
    """

    intent: str
    expected_pattern: str
    generator: str  # which template emitted this intent


def _natural_label(label: str) -> str:
    """Lowercase a catalog label for use in an English intent."""

    return str(label or "").lower().strip()


def _qualified_metric_rollup_intents() -> list[GeneratedCase]:
    """Pattern requires threshold + qualifying entity + target measure.

    The pattern's ``_target_measure_terms`` recognizes only a narrow
    vocabulary (revenue / order / inventory / menu / arr / logo). For
    jaffle, that means template the target as "revenue" or "orders"
    and the predicate as "orders" / "customers" / "sessions".
    """

    cases: list[GeneratedCase] = []

    # entity_word, predicate_word combos that resolve cleanly to a
    # qualifying entity. We deliberately exercise both the "with at
    # least N" and "with more than N" trigger phrasings.
    entity_predicates: list[tuple[str, str, str]] = [
        ("stores", "store", "orders"),
        ("stores", "store", "sessions"),
        ("stores", "store", "customers"),
        ("customers", "customer", "orders"),
        ("customers", "customer", "items"),
        ("customers", "customer", "sessions"),
    ]
    targets = ("revenue", "orders")
    thresholds = ((3, "at least"), (10, "more than"), (25, "at least"))
    top_ns = (5, 10, 20)

    for entity_plural, _entity_singular, predicate in entity_predicates:
        for target in targets:
            for n in top_ns:
                k, trigger = thresholds[(n // 5) % len(thresholds)]
                intent = f"top {n} {entity_plural} by {target} with {trigger} {k} {predicate}"
                cases.append(
                    GeneratedCase(
                        intent=intent,
                        expected_pattern="qualified_metric_rollup",
                        generator="qualified_metric_rollup.top_n",
                    )
                )

    # No top-N — just the qualification phrase.
    bare_phrases = [
        ("stores", "store", "orders", "more than", 50),
        ("stores", "store", "customers", "at least", 100),
        ("customers", "customer", "orders", "at least", 5),
        ("customers", "customer", "items", "more than", 20),
        ("stores", "store", "sessions", "at least", 200),
    ]
    for entity_plural, _entity_singular, predicate, trigger, k in bare_phrases:
        for target in ("revenue", "orders"):
            intent = f"{target} by {entity_plural} with {trigger} {k} {predicate}"
            cases.append(
                GeneratedCase(
                    intent=intent,
                    expected_pattern="qualified_metric_rollup",
                    generator="qualified_metric_rollup.bare_phrase",
                )
            )

    return cases


def _inline_period_shift_intents(runtime: Any) -> list[GeneratedCase]:
    """Pattern needs a YoY/MoM/WoW/QoQ trigger + a measure whose label
    contains one of the pattern's vocabulary words.

    The pattern's ``_target_measure_terms`` returns ``["revenue"]``,
    ``["order"]``, ``["inventory"]``, or ``["active", "menu"]``. So
    templates that put one of those nouns in the intent will hit.
    """

    cases: list[GeneratedCase] = []
    base_words = ("revenue", "orders", "inventory", "active menu")
    shifts: list[tuple[str, str]] = [
        ("YoY by month", "yoy"),
        ("MoM", "mom"),
        ("WoW", "wow"),
        ("QoQ", "qoq"),
        ("year over year by month", "yoy_long"),
        ("vs last year by month", "vs_last_year"),
        ("vs last month", "vs_last_month"),
    ]
    for word in base_words:
        for phrase, tag in shifts:
            intent = f"{word} {phrase}"
            cases.append(
                GeneratedCase(
                    intent=intent,
                    expected_pattern="inline_period_shift",
                    generator=f"inline_period_shift.{tag}",
                )
            )

    # Specific measure-label variants — exercise the food/drink/item
    # subspecies of "revenue" / "order" so the corpus reflects real
    # catalog richness.
    rich_variants = [
        "food revenue YoY by month",
        "drink revenue YoY by month",
        "food orders MoM",
        "drink orders MoM",
        "gross order value YoY by month",
        "item revenue YoY by month",
    ]
    for intent in rich_variants:
        cases.append(
            GeneratedCase(
                intent=intent,
                expected_pattern="inline_period_shift",
                generator="inline_period_shift.label_variant",
            )
        )

    # Silence unused-arg warning while keeping the signature general
    # for future label-driven expansion.
    _ = runtime
    return cases


def _filtered_adoption_funnel_intents() -> list[GeneratedCase]:
    """Pattern triggers on 'adoption' / 'funnel' or 'signup' + 'received'.

    Jaffle has ``metric.adoption.signup_to_send_conversion_rate_28d``,
    so the pattern resolves cleanly to that.
    """

    cases: list[GeneratedCase] = []
    templates = [
        ("signup to send adoption funnel", "no_group"),
        ("signup to send adoption funnel by store", "by_store"),
        ("adoption funnel by store", "by_store_short"),
        ("signup to send conversion funnel", "alt_phrase"),
        (
            "adoption funnel for stores with order rate over 10%",
            "with_threshold_10",
        ),
        (
            "adoption funnel for stores with order rate over 25%",
            "with_threshold_25",
        ),
        ("signup to send adoption funnel by month", "by_month"),
        ("signup to send adoption rate by store", "rate_by_store"),
    ]
    for intent, tag in templates:
        cases.append(
            GeneratedCase(
                intent=intent,
                expected_pattern="filtered_adoption_funnel",
                generator=f"filtered_adoption_funnel.{tag}",
            )
        )
    return cases


def _distribution_entity_value_intents() -> list[GeneratedCase]:
    """Pattern is hardcoded to ARR + account.

    Jaffle has no ARR measure, so these will not realize. We still
    include them — the gap is the signal. Whether they land as
    ``out_of_scope`` (scope gate fails on 'arr') or ``unrealizable``
    (passes gate but no pattern claims) is itself a useful data point.
    """

    cases: list[GeneratedCase] = []
    templates = [
        ("p80 ARR per account", "p80"),
        ("80th percentile ARR by account", "80th_pctl"),
        ("top decile ARR by account", "decile"),
        ("p95 ARR per account", "p95"),
        ("median ARR per account", "median"),
    ]
    for intent, tag in templates:
        cases.append(
            GeneratedCase(
                intent=intent,
                expected_pattern="distribution_entity_value",
                generator=f"distribution_entity_value.{tag}",
            )
        )
    return cases


def _scoped_predicate_ratio_intents() -> list[GeneratedCase]:
    """Pattern requires '%'/'pct'/'share' + 'sms' + 'push' + paying-logo or arr.

    Jaffle has none of these tokens; the scope gate will reject most.
    Same rationale as distribution: the gap is the signal.
    """

    cases: list[GeneratedCase] = []
    templates = [
        ("% of paying logos using both SMS and push", "paying_logos"),
        ("% of accounts using both sms and push", "accounts"),
        ("share of paying logos sending sms and push", "share_phrasing"),
        (
            "ratio of paying logos with sms and push activity",
            "ratio_phrasing",
        ),
        ("pct of paying logos using sms and push channels", "pct_phrasing"),
    ]
    for intent, tag in templates:
        cases.append(
            GeneratedCase(
                intent=intent,
                expected_pattern="scoped_predicate_ratio",
                generator=f"scoped_predicate_ratio.{tag}",
            )
        )
    return cases


def _runtime_metric_arithmetic_intents() -> list[GeneratedCase]:
    """Pattern requires all of {email, sms, push} + {total|sum|message}.

    Jaffle has no email/sms/push metrics, so these miss — but they
    still help size the gap between built-in patterns and the legacy
    fallback (which often catches these as ``ok`` with empty pattern).
    """

    cases: list[GeneratedCase] = []
    templates = [
        ("total messages (email + sms + push) by month", "by_month"),
        ("email + sms + push total messages", "no_group"),
        ("sum of email sms push messages by store", "by_store"),
        ("total email sms push messages by month", "alt_phrasing"),
        ("monthly total messages email sms push", "monthly"),
    ]
    for intent, tag in templates:
        cases.append(
            GeneratedCase(
                intent=intent,
                expected_pattern="runtime_metric_arithmetic",
                generator=f"runtime_metric_arithmetic.{tag}",
            )
        )
    return cases


def _generate_cases(runtime: Any) -> list[GeneratedCase]:
    """Run every per-pattern generator and return a sorted, deduped list."""

    raw: list[GeneratedCase] = []
    raw.extend(_qualified_metric_rollup_intents())
    raw.extend(_inline_period_shift_intents(runtime))
    raw.extend(_filtered_adoption_funnel_intents())
    raw.extend(_distribution_entity_value_intents())
    raw.extend(_scoped_predicate_ratio_intents())
    raw.extend(_runtime_metric_arithmetic_intents())

    # Dedupe on (intent, expected_pattern) — keep determinism by sorting.
    seen: dict[tuple[str, str], GeneratedCase] = {}
    for case in raw:
        seen.setdefault((case.intent, case.expected_pattern), case)
    return sorted(seen.values(), key=lambda c: (c.expected_pattern, c.intent))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _evaluate(runtime: Any, cases: list[GeneratedCase]) -> list[dict[str, Any]]:
    """Run plan against each generated case."""

    from semantic_rails.planner import plan_payload

    rows: list[dict[str, Any]] = []
    for case in cases:
        planned = plan_payload(runtime, intent=case.intent)
        plan_pattern = (planned.get("best") or {}).get("pattern", "") or ""
        rows.append(
            {
                **asdict(case),
                "plan_status": planned.get("status"),
                "plan_pattern": plan_pattern,
                "plan_payload_bytes": _payload_size(planned),
                "plan_expected_pattern_fired": plan_pattern == case.expected_pattern,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    plan_ok = sum(1 for r in rows if r["plan_status"] == "ok")
    expected_fired = sum(1 for r in rows if r["plan_expected_pattern_fired"])
    statuses = Counter(r["plan_status"] for r in rows)
    by_pattern: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_pattern.setdefault(row["expected_pattern"], {"total": 0, "fired": 0})
        bucket["total"] += 1
        if row["plan_expected_pattern_fired"]:
            bucket["fired"] += 1
    return {
        "total": total,
        "plan_status_ok": plan_ok,
        "plan_expected_pattern_fired": expected_fired,
        "plan_expected_pattern_fired_rate": (round(expected_fired / total, 4) if total else 0.0),
        "plan_status_breakdown": dict(sorted(statuses.items())),
        "by_pattern": dict(sorted(by_pattern.items())),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print()
    print("# Generated benchmark corpus — summary")
    print()
    total = summary["total"]
    fired = summary["plan_expected_pattern_fired"]
    rate = summary["plan_expected_pattern_fired_rate"] * 100.0
    print(f"- total generated intents: {total}")
    print(f"- plan status='ok'        : {summary['plan_status_ok']}/{total}")
    print(f"- plan expected-pattern-fired: {fired}/{total} ({rate:.1f}%)")
    print()
    print("## Plan status breakdown")
    for status, count in summary["plan_status_breakdown"].items():
        print(f"- {status}: {count}")
    print()
    print("## By expected pattern")
    for name, bucket in summary["by_pattern"].items():
        fired = bucket["fired"]
        total = bucket["total"]
        rate = (fired / total) * 100.0 if total else 0.0
        print(f"- {name}: {fired}/{total} ({rate:.1f}%)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "scorecards" / "generated_plan_corpus.json",
        help="Path to write the generated corpus JSON.",
    )
    args = parser.parse_args(argv)

    runtime = _build_runtime()
    try:
        cases = _generate_cases(runtime)
        rows = _evaluate(runtime, cases)
    finally:
        runtime.close()

    summary = _summary(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "summary": summary,
                "cases": rows,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    _print_summary(summary)
    print()
    print(f"Wrote {len(rows)} cases to {args.output.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
