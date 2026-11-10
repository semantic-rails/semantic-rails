"""A caveat's so_what (what the reader should do) reaches its warning, and a package test
(``caveat_applies``) pins whether a call states a caveat."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_snapshot import semantic_payload
from semantic_rails.package_tools import run_package_tests_report
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

REVENUE = "measure.jaffle.revenue_usd"
STORE_NAME = "dimension.jaffle_store_name"
MARCH = {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "start": "2017-03-01",
    "end": "2017-04-01",
}
SAID = "caveat.test.with_so_what"
UNSAID = "caveat.test.without_so_what"
SO_WHAT = "Compare March revenue with list prices restored, or name the event."
CAVEATS = [
    {
        "id": SAID,
        "kind": "business_event",
        "message": "A March pricing event lowered revenue.",
        "so_what": f"  {SO_WHAT}  ",
        "owner": "pricing_team",
        "object_ids": [REVENUE],
        "time": {"from": "2017-03-01", "to": "2017-04-01"},
    },
    {
        "id": UNSAID,
        "kind": "data_quality",
        "message": "Store names were backfilled.",
        "object_ids": [STORE_NAME],
        "severity": "info",
    },
]


def _write_caveats(package_dir: Path, rows: list[dict]) -> None:
    (package_dir / "caveats.yml").write_text(
        yaml.safe_dump({"semantic_caveats": rows}, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def package_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    package = copy_package_config(
        tmp_path_factory.mktemp("so_what"), "jaffle_shop", preseed_db=True
    )
    _write_caveats(package, CAVEATS)
    return package


@pytest.fixture(scope="module")
def runtime(package_dir: Path) -> Iterator[Runtime]:
    rt = Runtime.from_path(str(package_dir))
    try:
        yield opened(rt)
    finally:
        rt.close()


def _stated(runtime: Runtime, verbosity: str | None) -> dict[str, dict]:
    query = {"select": [REVENUE], "group_by": [STORE_NAME], "time": MARCH}
    payload = runtime.validate(query, **({"verbosity": verbosity} if verbosity else {}))
    return {
        row["details"]["caveat_id"]: row["details"]
        for row in payload["warnings"]
        if row["code"] == "SEMANTIC_CAVEAT_APPLIED"
    }


def test_so_what_loads_stripped(package_dir: Path) -> None:
    config = load_package_config(str(package_dir))
    by_id = {row.id: row for row in config.semantic_caveats}
    assert by_id[SAID].so_what == SO_WHAT
    assert by_id[UNSAID].so_what == ""


@pytest.mark.parametrize("verbosity", [None, "compact", "full"])
def test_warning_carries_so_what_and_owner_by_default(runtime: Runtime, verbosity) -> None:
    stated = _stated(runtime, verbosity)
    assert stated[SAID]["so_what"] == SO_WHAT
    assert stated[SAID]["owner"] == "pricing_team"


def test_compact_leaves_out_what_the_caveat_does_not_say(runtime: Runtime) -> None:
    stated = _stated(runtime, "compact")
    assert "so_what" not in stated[UNSAID]
    assert "owner" not in stated[UNSAID]


def test_minimal_keeps_its_ids_only(runtime: Runtime) -> None:
    stated = _stated(runtime, "minimal")
    assert "so_what" not in stated[SAID]
    assert "owner" not in stated[SAID]


def test_a_non_text_so_what_is_refused(tmp_path: Path) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop")
    _write_caveats(package, [{**CAVEATS[0], "so_what": ["compare", "later"]}])
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    assert raised.value.code == "INVALID_CONFIG"
    assert f"caveat '{SAID}' so_what must be a string (what the reader should do)" in str(
        raised.value
    )


def test_an_unauthored_so_what_keeps_the_package_fingerprint(package_dir: Path) -> None:
    caveats = semantic_payload(load_package_config(str(package_dir)))["semantic_caveats"]
    by_id = {row["id"]: row for row in caveats}
    assert "so_what" not in by_id[UNSAID]
    assert by_id[SAID]["so_what"] == SO_WHAT


# caveat_applies package tests: each entry, and the code and message its author sees.
def _test(caveat: str | None = SAID, *, query: dict | None = None, **extra) -> dict:
    spec = {
        "kind": "caveat_applies",
        "query": query or {"select": [REVENUE], "time": MARCH},
        **({"caveat": caveat} if caveat is not None else {}),
    }
    return {**spec, **extra}


APRIL = {**MARCH, "start": "2017-04-01", "end": "2017-05-01"}
TESTS = {
    "applies": _test(),
    "does_not_apply": _test(query={"select": [REVENUE], "time": APRIL}, applies=False),
    "expected_but_absent": _test(query={"select": [REVENUE], "time": APRIL}),
    "unexpected_but_stated": _test(applies=False),
    "no_caveat": _test(None),
    "unknown_caveat": _test("caveat.test.nope"),
    "applies_not_boolean": _test(applies="no"),
    "refused_query": _test(query={"select": ["measure.jaffle.nope"], "time": MARCH}, applies=False),
}


@pytest.fixture(scope="module")
def results(package_dir: Path) -> dict[str, dict]:
    (package_dir / "tests" / "caveat_applies.yml").write_text(
        yaml.safe_dump({"tests": TESTS}), encoding="utf-8"
    )
    report = run_package_tests_report(resolve_package_reference(path=str(package_dir)))
    return {row["id"]: row for row in report["tests"] if row["id"] in TESTS}


@pytest.mark.parametrize("test_id", ["applies", "does_not_apply"])
def test_caveat_applies_passes(results: dict[str, dict], test_id: str) -> None:
    assert results[test_id]["ok"] is True, results[test_id]


@pytest.mark.parametrize(
    ("test_id", "code", "message"),
    [
        (
            "expected_but_absent",
            "CAVEAT_APPLIES_MISMATCH",
            f"caveat '{SAID}' did not apply to the query",
        ),
        (
            "unexpected_but_stated",
            "CAVEAT_APPLIES_MISMATCH",
            f"caveat '{SAID}' applied to the query, which the test says it must not",
        ),
        (
            "no_caveat",
            "INVALID_CONFIG",
            "a caveat_applies test must name the caveat it checks in caveat",
        ),
        (
            "unknown_caveat",
            "INVALID_CONFIG",
            "caveat_applies names caveat 'caveat.test.nope', which the package does not declare",
        ),
        (
            "applies_not_boolean",
            "INVALID_CONFIG",
            "a caveat_applies test's applies must be true or false",
        ),
    ],
)
def test_caveat_applies_fails_with_what_the_author_sees(
    results: dict[str, dict], test_id: str, code: str, message: str
) -> None:
    error = results[test_id]["error"]
    assert results[test_id]["ok"] is False
    assert (error["code"], error["message"]) == (code, message)


def test_caveat_applies_lists_what_was_stated(results: dict[str, dict]) -> None:
    details = results["unexpected_but_stated"]["error"]["details"]
    assert details["applied_caveats"] == [SAID]
    assert details["expected_applies"] is False
    assert results["unknown_caveat"]["error"]["details"]["known_caveats"] == sorted([SAID, UNSAID])


def test_a_refused_query_fails_even_the_negative_form(results: dict[str, dict]) -> None:
    error = results["refused_query"]["error"]
    assert results["refused_query"]["ok"] is False
    assert error["code"] == "OBJECT_NOT_FOUND"
    assert "measure.jaffle.nope" in error["message"]
    assert error["details"]["caveat"] == SAID
