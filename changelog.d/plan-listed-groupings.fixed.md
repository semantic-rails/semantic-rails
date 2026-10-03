- `plan` is no longer ready when the draft drops a grouping the question lists after a comma:
  "repair cost by incident name, incident" grouped by the incident name alone is held, since two
  incidents that share a name would be added into one row. Each grouping the question lists,
  apart from clock terms and declared values, needs its own matching dimension in the draft, and
  only unmatched terms appear in `why.details.dropped_groupings`; "repair cost by repair", with
  an entity named like the measure, is held instead of answering one total. The check only holds
  a plan: the draft and every other plan are unchanged.
- A listed grouping that names an entity is satisfied only by that entity's own key dimension,
  or by the single declared dimension of that entity whose own words name it, so "order count
  by customer history, month" is no longer ready when grouped by the customer id alone: an
  entity with a composite key is never satisfied, and the plan is not ready. A dimension's
  words for this check are its label, aliases and the last part of its name, not its id.
- `plan` no longer calls ready a draft that picked one reading of a grouping that dimensions of
  several entities match, none of them the measure's own: "order count by month and name"
  (Customer name or Store name) is not ready, with the term in `why.details.ambiguous_groupings`.
  "Customer name" or "store name" in the question, or the dimension in the caller's
  `partial_query` group_by, settles it.
