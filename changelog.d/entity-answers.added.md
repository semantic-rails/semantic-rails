- Graph entities accept `display:`, the dimension that names one row in an answer. A term
  naming an entity after "by", "each" or "every", or ranked ("top 2 accounts by MRR"), groups
  by the entity's key and that dimension, so "revenue by store" answers one row per store
  even when two stores share a name. A ranking keeps the stated count and direction ("top
  three", "bottom 1"), and is ready only when the question names the value it ranks by (a
  measure's or metric's whole name, label or alias) or the caller's select does: "top 5
  customers" and "which store had the most customers" stay held.
- `plan` answers "which accounts closed last week" and "who upgraded last week" with the
  entity's rows, listed by name without zero rows. "Who" that could mean several entities
  asks which.
