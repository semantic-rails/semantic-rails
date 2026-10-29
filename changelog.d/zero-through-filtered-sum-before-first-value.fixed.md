- A ratio over a filtered sum of a table that declares how far it is loaded (`zero_through`)
  reads `0.0` in each loaded period with no matching row, even before the summed column holds
  any value. "Each week's refund rate", refunds over revenue, read `NULL` in every week before
  the first refund, because no row had a refund amount yet. A refunded row whose amount is NULL
  still reads `NULL`, and periods after the edge still read `NULL` with `NO_DATA_YET`.
