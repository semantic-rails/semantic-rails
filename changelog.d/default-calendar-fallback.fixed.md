- A query on the default calendar no longer fills from another calendar when the package
  declares only non-default ones (for example only a fiscal calendar). It borrowed that
  calendar's periods, so fiscal quarters and years missed every Gregorian period and read 0;
  it now uses the implicit Gregorian calendar. Answers change for such packages, including
  bounded `fill` windows, which now follow the rule for a calendar whose `date_day` is a
  `date`.
