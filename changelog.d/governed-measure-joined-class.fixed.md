- `plan` now holds a draft over a measure when a visible metric aggregates that same measure
  through a filter on a class stored on a parent entity (for example a team's class read from a
  daily team fact), instead of answering with every row the metric leaves out.
