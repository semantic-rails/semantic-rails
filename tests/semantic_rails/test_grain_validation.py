"""``time.grain`` is checked at validate, against the call's words and the role's grains.

A typo like ``grain: "fortnight"`` is refused structurally, naming the supported grains, rather
than surfacing at execute time as a raw warehouse error.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

ORDER_TIME = "temporal_role.jaffle_order_time"
REVENUE = [{"expression": "measure.jaffle.revenue_usd", "as": "revenue"}]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """One runtime for the module: every call here only validates."""
    package_dir = copy_package_config(tmp_path_factory.mktemp("grain"), "jaffle_shop")
    package = load_package_config(str(package_dir))
    rt = Runtime.from_config(package, source_path=str(package_dir), package_id="jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


@pytest.mark.parametrize(
    ("surface", "grain"),
    # Grains are lowercase words, like every word of the call: "Month" is not read as "month".
    [("runtime", "Month"), ("mcp", "fortnight")],
)
def test_a_grain_outside_the_call_words_is_refused_with_the_grains(
    runtime: Runtime, surface: str, grain: str
) -> None:
    query = {"select": REVENUE, "time": {"temporal_role": ORDER_TIME, "grain": grain}}
    if surface == "mcp":
        report = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute", {"mode": "validate", "query": query}
        )
        # The top-level `error` mirrors errors[0] for conventional MCP clients.
        assert report["error"]["code"] == "INVALID_QUERY"
    else:
        report = runtime.validate(query)
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "time.grain"
    (hint,) = error["recovery_hints"]
    assert hint["code"] == "FIX_QUERY_CALL"
    assert "day, week, month, quarter or year" in hint["message"]


def test_a_grain_the_role_does_not_support_is_refused_with_its_grains(runtime: Runtime) -> None:
    """The monthly role supports month, quarter and year: week, valid elsewhere, is refused."""
    report = runtime.validate(
        {
            "select": [
                {
                    "expression": "measure.jaffle_monthly_metrics.prior_period_revenue_usd",
                    "as": "prior_revenue",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_monthly_metric_month", "grain": "week"},
        }
    )
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "time.grain"
    assert error["details"]["received"] == "week"
    assert error["details"]["supported_grains"] == ["month", "quarter", "year"]
