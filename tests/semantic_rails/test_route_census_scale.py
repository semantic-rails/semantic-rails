"""On a large warehouse, only the route forks the package's own questions hit need a decision.

The synthetic campus package (``campus_package.py``): 74 entities in 17 subject areas, 123
relationships walked both ways, 5,402 entity pairs a question can need, 3,199 of them with two
or more routes and no recorded route, and 690 routes through undeclared child rows. One lookup
among the routes answers 142 of the 3,199 (the lookup default), so 3,057 stay questions. Its
twelve example questions hit eight forks, four of which a lookup answers: the parse report
warns about the other four alone, and about no crossing. Every other pair is asked when a
question needs it, never guessed.

The same campus written forward-only answers 291 pairs, each by lookups (253 of them without
the lookup default). Opened both ways, it answers every one of them by the same route; an
engine without the lookup default refuses 104 of those 253 once reverse hops add routes.

Every pair's resolution is pinned by a digest, so a route-ladder change that moves an answer
fails here; ``scripts/dev/route_snapshot.py`` dumps and compares the pairs to review the move.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.route_census import census_pairs, resolve_pairs
from tests.semantic_rails.campus_package import write_campus_package

# The fork each example question hits that no lookup default answers, from its fact to the
# entity it groups by. A lookup answers the other four (award and event attendance by school,
# loan by campus, space unit by department).
NEEDED = {
    ("enrollment", "department"),
    ("housing_assignment", "building"),
    ("paycheck", "department"),
    ("ticket", "department"),
}
# How every pair resolves: beyond the four-hop ceiling, PATH_NOT_FOUND.
RESOLUTIONS = {
    "refused AMBIGUOUS_PATH": 3057,
    "only_route": 1050,
    "refused PATH_NOT_FOUND": 1034,
    "default": 142,
    "colocated_key": 119,
}
DIGEST = "c8de35407071dc586cbc0821c3406b7258da0b415fbc70bbacdd05dfa73bf372"


def test_a_large_package_warns_only_about_the_forks_its_questions_hit(tmp_path):
    package = write_campus_package(tmp_path)
    started = time.perf_counter()
    report, config = parse_config_report(PackageReference(source_path=str(package)))
    seconds = time.perf_counter() - started
    assert report["ok"] and config is not None
    census = report["route_census"]
    assert len(census_pairs(config)) == 5402
    assert {name: len(rows) for name, rows in census.items()} == {
        "undecided": 3057,
        "assumed": 87,
        "defaults": 142,
        "pass_through": 690,
    }
    needed = {
        (row["source_entity"], row["target_entity"]): row["needed_by"]
        for row in census["undecided"]
        if row["needed_by"]
    }
    assert needed == {
        (f"entity.campus_{fact}", f"entity.campus_{target}"): [f"example.{fact}_by_{target}"]
        for fact, target in NEEDED
    }
    (warning,) = [row for row in report["warnings"] if row["code"] == "ROUTES_UNDECIDED"]
    assert (warning["details"]["count"], warning["details"]["unwalked"]) == (4, 3053)
    assert not any(row["needed_by"] for row in census["pass_through"])
    assert not [row for row in report["warnings"] if row["code"] == "ROUTE_PASS_THROUGH"]
    # Locally about 3 seconds; the bound only catches a search that stopped scaling.
    assert seconds < 60

    # Every pair's resolution, as the reviewed snapshot.
    resolutions = {
        f"{start} -> {target}": f"refused {outcome.refused}"
        if outcome.refused
        else f"{outcome.basis}: {', '.join(outcome.path)}"
        for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items()
    }
    assert Counter(value.split(":")[0] for value in resolutions.values()) == RESOLUTIONS
    digest = hashlib.sha256(json.dumps(resolutions, sort_keys=True).encode()).hexdigest()
    assert digest == DIGEST


def test_opening_reverse_hops_keeps_every_answer_a_forward_only_package_gives(tmp_path):
    """An author who kept every relationship forward-only to avoid route questions can open
    reverse hops: each pair the forward-only package answers (by lookups, the only routes it
    has) keeps that answer, as the one lookup among the new routes."""
    forward = write_campus_package(tmp_path / "forward", traversal=["forward"])
    both = write_campus_package(tmp_path / "both")
    one_way, two_way = (load_package_config(str(package)) for package in (forward, both))
    answered = {
        pair: outcome.path
        for pair, outcome in resolve_pairs(one_way, census_pairs(one_way)).items()
        if not outcome.refused
    }
    assert len(answered) == 291
    reopened = resolve_pairs(two_way, list(answered))
    assert {pair: outcome.path for pair, outcome in reopened.items()} == answered
    assert Counter(outcome.basis for outcome in reopened.values()) == {
        "default": 142,
        "colocated_key": 119,
        "only_route": 30,
    }
