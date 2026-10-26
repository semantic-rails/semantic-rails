"""On a large warehouse, only the route forks the package's own questions hit need a decision.

The synthetic campus package (``campus_package.py``): 74 entities in 17 subject areas, 123
relationships walked both ways, 5,402 entity pairs a question can need, 3,199 of them with two
or more routes and no recorded route, and 690 routes through undeclared child rows. Its twelve
example questions hit eight of those forks: the parse report warns about those eight alone,
and about no crossing. Every other pair is asked when a question needs it, never guessed.

Every pair's resolution is pinned by a digest, so a route-ladder change that moves an answer
fails here; ``scripts/dev/route_snapshot.py`` dumps and compares the pairs to review the move.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter

from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.route_census import census_pairs, resolve_pairs
from tests.semantic_rails.campus_package import write_campus_package

# The fork each example question hits, from its fact to the entity it groups by.
NEEDED = {
    ("award", "school"),
    ("enrollment", "department"),
    ("event_attendance", "school"),
    ("housing_assignment", "building"),
    ("loan", "campus"),
    ("paycheck", "department"),
    ("space_unit", "department"),
    ("ticket", "department"),
}
# How every pair resolves: beyond the four-hop ceiling, PATH_NOT_FOUND.
RESOLUTIONS = {
    "refused AMBIGUOUS_PATH": 3199,
    "only_route": 1050,
    "refused PATH_NOT_FOUND": 1034,
    "colocated_key": 119,
}
DIGEST = "5ec25a197acdaa4c6fcfa4488f7ae5d2cdbdf4b2ff0f08502bfe5ec63d6abdb1"


def test_a_large_package_warns_only_about_the_forks_its_questions_hit(tmp_path):
    package = write_campus_package(tmp_path)
    started = time.perf_counter()
    report, config = parse_config_report(PackageReference(source_path=str(package)))
    seconds = time.perf_counter() - started
    assert report["ok"] and config is not None
    census = report["route_census"]
    assert len(census_pairs(config)) == 5402
    assert {name: len(rows) for name, rows in census.items()} == {
        "undecided": 3199,
        "assumed": 87,
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
    assert (warning["details"]["count"], warning["details"]["unwalked"]) == (8, 3191)
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
