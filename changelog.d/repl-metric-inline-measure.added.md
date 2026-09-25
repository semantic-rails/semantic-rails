- The REPL's `author metric` can create the measure a metric needs without leaving the
  wizard. Cancelling or interrupting the metric takes that measure back, and one `undo`
  reverts both after checking that neither file changed since. If one did, nothing is
  restored: the REPL names the file and the kept changes, and `undo` can still reach them.
