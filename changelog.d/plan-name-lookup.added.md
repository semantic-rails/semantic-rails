- `plan` answers a question that names a row by its name ("How many orders did Acme place last
  month?", "What's Globex's MRR?"), with or without the entity's word. It looks the capitalized
  or quoted name up once in the text `display:` dimension of each entity the question's measure
  reaches, under the caller's row filters, and reads the one row whose display holds the name as
  whole words: the draft filters on that row's key and groups by its display, so the answer
  names it. A name matching several rows returns `needs_clarification` listing them; a name
  matching none stays held. It is the only warehouse read `plan` makes.
- Query IR `where` filters take `ILIKE` and `NOT ILIKE`, a `LIKE` in any case that compiles the
  same on every warehouse.
