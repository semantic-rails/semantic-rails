"""Per-warehouse integration targets.

Each module in this package defines ``TARGET = IntegrationTarget(...)``
for one registered warehouse. The conformance suite auto-discovers
every module here (see ``harness.discover_targets``) and the registry
coverage test fails when a warehouse registered in
``semantic_rails.dialects`` has no target module — so adding a dialect
without wiring it into the conformance suite is impossible to merge.
"""
