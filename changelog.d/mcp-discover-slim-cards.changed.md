- MCP `discover(verbosity="minimal", limit=5)` returns slim cards: id, kind, label, score, a short
  description, default temporal role and availability, plus the reason when a candidate is
  unavailable. Dimension values also keep their raw value, business label, and availability,
  including blocked values. Omitted options keep the v1 full cards and 10-per-kind limit.
