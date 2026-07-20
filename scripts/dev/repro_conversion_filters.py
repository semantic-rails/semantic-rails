"""Repro for MCP feedback: conversion operand filters silently dropped."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tempfile

from semantic_rails import config as config_module  # noqa: E402
from semantic_rails.runtime import Runtime  # noqa: E402
from tests.semantic_rails.conftest import copy_package_config  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config_path = copy_package_config(tmp, "jaffle_shop", preseed_db=True)
config_module.list_package_paths = lambda: {"jaffle_shop": str(config_path)}
runtime = Runtime("jaffle_shop")

query = {
    "version": 2,
    "select": [
        {
            "as": "a_then_b_conversion_rate",
            "expression": {
                "kind": "conversion",
                "entity": "entity.jaffle_customer",
                "window": {"unit": "day", "value": 28},
                "matching_mode": "first_converted_after_base",
                "base": {
                    "kind": "aggregate",
                    "measure": "measure.jaffle.order_count",
                    "filter": {
                        "all": [
                            {
                                "field": "dimension.jaffle_product_name",
                                "op": "=",
                                "value": "adele-ade",
                            }
                        ]
                    },
                },
                "converted": {
                    "kind": "aggregate",
                    "measure": "measure.jaffle.order_count",
                    "filter": {
                        "all": [
                            {
                                "field": "dimension.jaffle_product_name",
                                "op": "=",
                                "value": "chai and mighty",
                            }
                        ]
                    },
                },
            },
        }
    ],
    "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    "verbosity": "full",
}

report = runtime.validate(query)
print("validate ok:", report.get("ok"))
print("errors:", json.dumps(report.get("errors", []), indent=2)[:2000])
rendered = (report.get("explain") or {}).get("rendered_sql", "")
print("--- filters present in SQL? ---")
print("adele-ade in SQL:", "adele-ade" in rendered)
print("chai and mighty in SQL:", "chai and mighty" in rendered)
print(rendered[:4000])
